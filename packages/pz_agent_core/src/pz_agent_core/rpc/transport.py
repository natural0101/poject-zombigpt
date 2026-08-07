"""The listener and the dialler, and the two families they speak.

:mod:`multiprocessing.connection` gives a local, authenticated, message-framed
link on both platforms with no dependency and no port. What it does *not* give
safely is its own default serialisation: ``Connection.send`` pickles, and a
pickle stream read by a process is code that process runs. Only
:meth:`~multiprocessing.connection.Connection.send_bytes` and
:meth:`~multiprocessing.connection.Connection.recv_bytes` are used here, and
:mod:`.wire` is what turns those bytes into a message.

**Two families, chosen by platform, not by preference.**

``AF_PIPE`` on Windows: ``\\\\.\\pipe\\<name>``. It is not a filesystem object,
so there is nothing to clean up if the process dies and nothing to leave behind
in the state directory.

``AF_UNIX`` elsewhere: a socket file under the runtime directory. It *is* a
filesystem object, it does outlive a killed process, and that is exactly the
stale case the descriptor's liveness check exists for.

Windows is the platform this ships on and Linux is the platform the tests run
on, so both are real rather than one being a fallback. A test that only ever
exercised the family it happens to be running under would leave the shipped one
uncovered, which is the shape of the twenty-four failures this branch started
with.
"""

from __future__ import annotations

import os
import secrets
import socket
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import closing, suppress
from dataclasses import dataclass
from multiprocessing.connection import Client, Connection, Listener
from pathlib import Path
from typing import Final

from pz_agent_core.rpc.descriptor import FAMILY_PIPE, FAMILY_UNIX, RpcDescriptor
from pz_agent_core.rpc.wire import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    ErrorCode,
    RpcError,
    RpcRequest,
    RpcResponse,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)

__all__ = [
    "DEFAULT_DEADLINE_SECONDS",
    "AddressTooLong",
    "RpcClient",
    "RpcServer",
    "RpcUnavailable",
    "local_family",
    "new_address",
]


class AddressTooLong(RpcError):
    """A POSIX socket path exceeds what the kernel will bind.

    Separate from a generic failure because the remedy is specific and the
    generic error is misleading: the socket layer reports it as an error about
    a filename, which reads like a missing directory.
    """

    code = ErrorCode.UNAVAILABLE


#: How long a client waits for one answer. Long enough for the core to read an
#: observation off disk, short enough that a wedged sidecar does not wedge the
#: MCP client that launched us — an MCP client with a hung tool call shows the
#: user a spinner and no way out.
DEFAULT_DEADLINE_SECONDS: Final = 10.0

#: How long the server waits for a request on an accepted connection before
#: dropping it. A connection that authenticated and then said nothing is either
#: a crashed client or a probe, and either way it must not hold a thread.
IDLE_SECONDS: Final = 60.0

#: The socket file's name length matters on POSIX: `sun_path` is 108 bytes on
#: Linux and 104 on macOS, and a state directory under a long profile path eats
#: most of it. Short by design.
_SOCKET_NAME: Final = "core-rpc.sock"

#: Longest ``AF_UNIX`` address that binds everywhere this project is tested.
#: The kernel's ``sun_path`` is 108 bytes on Linux and 104 on macOS, both
#: including the terminator; 100 clears the smaller one with room to spare.
#:
#: This is a POSIX-only limit, and POSIX is not the platform this ships on — a
#: Windows named pipe has no such bound. It is enforced anyway because the
#: alternative is an ``OSError`` from inside the socket layer whose message is
#: about a filename, which reads like a missing directory rather than a path
#: that is thirty characters too long.
_SUN_PATH_MAX: Final = 100


class RpcUnavailable(RpcError):
    """The server could not be reached, or stopped answering mid-call."""

    code = ErrorCode.UNAVAILABLE


def local_family() -> str:
    """The transport family this platform uses."""
    return FAMILY_PIPE if sys.platform == "win32" else FAMILY_UNIX


def new_address(runtime_dir: Path, *, family: str | None = None) -> str:
    """An address for a new server, in *family* (this platform's by default).

    The Windows form carries random bytes because a named pipe is a global
    name: two sidecars for two Windows accounts would collide on a fixed one,
    and the second would fail to bind with an error about a file. The POSIX form
    is a path inside the state directory, which is already per-user.
    """
    chosen = family or local_family()
    if chosen == FAMILY_PIPE:
        return rf"\\.\pipe\pz-agent-core-{os.getpid()}-{secrets.token_hex(8)}"
    address = str(runtime_dir / _SOCKET_NAME)
    length = len(address.encode("utf-8"))
    if length > _SUN_PATH_MAX:
        # The length, never the path: it runs through the profile directory and
        # so carries the account name, and this message ends up in a log.
        raise AddressTooLong(
            f"the socket path is {length} bytes and the limit is {_SUN_PATH_MAX}; "
            "move the state directory closer to the root, or run on Windows, where "
            "the transport is a named pipe and has no such limit"
        )
    return address


#: What a server does with a decoded request. Returning a response rather than
#: raising keeps the core's refusals — which are ordinary and expected — off the
#: exception path, where they would be indistinguishable from a transport fault.
Handler = Callable[[RpcRequest], RpcResponse]


@dataclass(frozen=True, slots=True)
class _Served:
    address: str
    family: str


class RpcServer:
    """Accepts local connections and answers one request per connection.

    One request per connection on purpose. The MCP executable makes a handful of
    calls per tool invocation and the cost of a connection is a few hundred
    microseconds locally, so keeping them open buys nothing and costs the thing
    that matters: a long-lived connection has to be reaped when its peer dies,
    and getting that wrong is how a process ends up holding a socket nobody is
    on the other end of. A connection that lives for one exchange needs no
    liveness logic at all.

    Not a context manager by accident — :meth:`close` must run even when the
    sidecar is coming down badly, so the caller owns it explicitly and
    :meth:`serve_forever` never assumes it will be reached.
    """

    def __init__(
        self,
        address: str,
        *,
        authkey: bytes,
        handler: Handler,
        family: str | None = None,
    ) -> None:
        self._family = family or local_family()
        self._handler = handler
        self._authkey = authkey
        self._listener = Listener(
            address,
            family="AF_PIPE" if self._family == FAMILY_PIPE else "AF_UNIX",
            authkey=authkey,
        )
        self._served = _Served(address=str(self._listener.address), family=self._family)
        self._stopping = threading.Event()

    @property
    def address(self) -> str:
        return self._served.address

    @property
    def family(self) -> str:
        return self._served.family

    def descriptor(self) -> RpcDescriptor:
        """What a client needs in order to find this server."""
        return RpcDescriptor(address=self.address, family=self.family, pid=os.getpid())

    def serve_once(self) -> bool:
        """Accept one connection, answer it, close it.

        Returns whether a connection was served. A failed accept — which is what
        :meth:`close` looks like from in here — returns ``False`` rather than
        raising, so the loop below ends quietly on shutdown instead of logging a
        traceback every time the sidecar stops.
        """
        try:
            connection = self._listener.accept()
        except (OSError, EOFError):
            return False
        except Exception:
            # `multiprocessing` raises AuthenticationError, which is not an
            # OSError. A wrong key is a caller's problem and not an event worth
            # stopping the server for; it is also not worth logging in detail,
            # because the interesting field would be the key.
            return not self._stopping.is_set()
        with closing(connection):
            if self._stopping.is_set():
                # The connection `close` opened to unblock this accept. Answering
                # it would mean waiting IDLE_SECONDS for a request that is never
                # coming, which is a minute of shutdown for nothing.
                return False
            self._exchange(connection)
        return True

    def _exchange(self, connection: Connection) -> None:
        if not connection.poll(IDLE_SECONDS):
            return
        try:
            data = connection.recv_bytes(maxlength=MAX_REQUEST_BYTES)
        except (OSError, EOFError):
            return
        try:
            request = decode_request(data)
        except RpcError as exc:
            self._reply(
                connection,
                RpcResponse(
                    id="unknown",
                    ok=False,
                    error_code=getattr(exc, "code", ErrorCode.MALFORMED),
                    error_message=str(exc),
                ),
            )
            return
        try:
            response = self._handler(request)
        except Exception as exc:
            # The server outlives its handlers. A method that raised would
            # otherwise take down the link every client shares, and the client
            # would see a closed socket rather than the reason.
            response = RpcResponse(
                id=request.id,
                ok=False,
                error_code=ErrorCode.CORE_REFUSED,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        self._reply(connection, response)

    def _reply(self, connection: Connection, response: RpcResponse) -> None:
        with suppress(OSError, EOFError):
            connection.send_bytes(encode_response(response))

    def serve_forever(self) -> None:
        """Answer connections until :meth:`close` is called."""
        while not self._stopping.is_set():
            if not self.serve_once():
                return

    def _wake(self) -> None:
        """Unblock a thread parked in ``accept``.

        Closing the listener does not do it. On Linux a thread blocked in
        ``accept`` on a Unix socket stays blocked when the socket is closed
        underneath it, so ``close`` returned and ``serve_forever`` kept running
        — a sidecar that never finished shutting down. The portable wake is a
        connection: ``accept`` returns it, the loop sees the stop flag, and it
        ends.

        Deliberately *not* ``Client``: that performs the authentication
        handshake and waits for the other side to answer it, so if nothing is
        in ``accept`` — the server was never started, or it already stopped —
        the wake would block instead of the thing it was meant to unblock. A
        bare connect wakes ``accept`` and needs no reply.
        """
        if self._family == FAMILY_UNIX:
            with suppress(OSError), socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                sock.connect(self.address)
            return
        # A Windows named pipe is opened as a file. `ConnectNamedPipe` completes
        # the moment a client opens the other end, which is all this needs.
        with suppress(OSError), open(self.address, "rb"):
            pass

    def close(self) -> None:
        """Stop listening. Safe to call twice, and from another thread."""
        already = self._stopping.is_set()
        self._stopping.set()
        if not already:
            self._wake()
        with suppress(OSError):
            self._listener.close()
        if self._family == FAMILY_UNIX:
            # `Listener.close` unlinks the socket file itself, but only if it
            # got that far; an interrupted shutdown can leave it. Removing it
            # here means a restart binds rather than failing on an address that
            # is in use by nobody.
            with suppress(OSError):
                Path(self.address).unlink()


class RpcClient:
    """Dials a server, sends one request, reads one answer.

    A new connection per call, matching the server. The deadline covers the
    whole exchange rather than each syscall: a server that accepted, then
    answered one byte a second, would satisfy any per-read timeout and still
    never finish.
    """

    def __init__(
        self,
        descriptor: RpcDescriptor,
        *,
        authkey: bytes,
        deadline: float = DEFAULT_DEADLINE_SECONDS,
    ) -> None:
        if deadline <= 0:
            raise ValueError(f"deadline must be positive, got {deadline}")
        self._descriptor = descriptor
        self._authkey = authkey
        self._deadline = deadline

    def call(self, method: str, params: dict[str, object] | None = None) -> RpcResponse:
        """Make one call.

        Raises:
            RpcUnavailable: the server could not be reached, closed the
                connection, or did not answer within the deadline.
            RpcError: the answer was not a response this build understands.
        """
        request = RpcRequest(id=secrets.token_hex(8), method=method, params=dict(params or {}))
        data = encode_request(request)
        try:
            connection = Client(
                self._descriptor.address,
                family="AF_PIPE" if self._descriptor.family == FAMILY_PIPE else "AF_UNIX",
                authkey=self._authkey,
            )
        except (OSError, EOFError) as exc:
            raise RpcUnavailable(f"{method}: the sidecar is not answering ({exc})") from None
        except Exception as exc:
            raise RpcUnavailable(
                f"{method}: the sidecar refused this connection; the token does not match, "
                "which means the sidecar restarted since this process read it"
            ) from None if not isinstance(exc, RpcError) else exc
        with closing(connection):
            try:
                connection.send_bytes(data)
                if not connection.poll(self._deadline):
                    raise RpcUnavailable(f"{method}: no answer within {self._deadline:g}s")
                answer = connection.recv_bytes(maxlength=MAX_RESPONSE_BYTES)
            except (OSError, EOFError) as exc:
                raise RpcUnavailable(
                    f"{method}: the sidecar closed the connection ({exc})"
                ) from None
        response = decode_response(answer)
        if response.id != request.id:
            # One request per connection, so a mismatched id cannot be a
            # reordering; it means the answer belongs to something else.
            raise RpcError(f"{method}: the answer does not match the request")
        return response


def unix_socket_supported() -> bool:
    """Whether this platform has ``AF_UNIX`` at all.

    Windows has had it since build 17063, but :mod:`multiprocessing` does not
    use it there, so the tests that exercise the POSIX family skip rather than
    fail on a Windows runner. Skipping the family a platform does not use is
    not the same as skipping a failure: the pipe family is exercised in its
    place, and the wire and descriptor tests cover both shapes everywhere.
    """
    return hasattr(socket, "AF_UNIX")


def families_here() -> Iterator[str]:
    """Every family that can actually be bound on this machine."""
    if sys.platform == "win32":
        yield FAMILY_PIPE
    elif unix_socket_supported():
        yield FAMILY_UNIX
