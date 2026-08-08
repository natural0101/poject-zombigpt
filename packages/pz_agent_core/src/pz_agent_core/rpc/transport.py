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

**Every wait on the peer is bounded — with one asymmetry, stated rather than
papered over.**

A ``Connection`` read has no timeout of its own, and ``poll`` only promises
that a first byte has arrived, so the raw pair leaves two unbounded waits: the
authentication handshake, whose stdlib reads block forever, and a read whose
peer sends the length header and then trickles the payload. Here the handshake
runs through a poll guard (:class:`_Guarded`) that spends the call's deadline
and no more, and on the ``AF_UNIX`` family — where the descriptor under the
connection is a socket — a watchdog (:func:`_cut_at`) severs the link with
``shutdown`` when the deadline expires, so even a mid-message stall ends in
the transport's own error within the budget. The server holds every stage of
an accepted connection — challenge, wait for the request, read, reply — to its
idle budget the same way.

``AF_PIPE`` is the asymmetry. A named pipe is not a socket: there is nothing a
second thread can ``shutdown`` under a blocked read or write, and closing the
handle from outside races the reader. So on Windows the wait for a message to
arrive is bounded by the poll guard, but a read that has already started has
no hard deadline on Windows — and neither does a write the peer has stopped
draining, so a reply to a peer that asked and then never reads its answer can
also outlive the budget there. Both stalls need a peer that stays alive while
holding the pipe — narrower than the socket trickle, because the pipe's
message framing delivers small messages whole and a gone peer fails the call
at once. That residue is documented here because claiming a bound that does
not hold would be worse than naming the one that does.
"""

from __future__ import annotations

import os
import secrets
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from multiprocessing import AuthenticationError
from multiprocessing.connection import (
    Client,
    Connection,
    Listener,
    answer_challenge,
    deliver_challenge,
)
from pathlib import Path
from typing import Final, cast

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
#: user a spinner and no way out. This one budget covers the whole call:
#: connect, handshake, send, and the read of the answer.
DEFAULT_DEADLINE_SECONDS: Final = 10.0

#: The server's per-stage budget for one accepted connection: how long it lets
#: the challenge go unanswered, a request go unsent, a started frame go
#: unfinished, or a reply go unread before dropping the peer. A connection
#: that stalls at any of those points is either a crashed client or a probe,
#: and either way it must not hold a thread. Injectable per server
#: (``idle_seconds``) so a test can observe the drop without waiting a minute.
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


class _DeadlineCut(Exception):
    """A wait on the peer crossed its deadline.

    Internal: every path that raises it translates it before it escapes, so
    callers see the transport's own error with the budget named, never a bare
    signal.
    """


class _Guarded:
    """The two methods the stdlib challenge uses, with every read bounded.

    ``deliver_challenge`` and ``answer_challenge`` read with no timeout, which
    is the unbounded handshake this transport used to have: a peer that
    accepts and then never answers holds the caller forever. The challenge
    functions only ever call ``send_bytes`` and ``recv_bytes``, so handing
    them this wrapper in place of the connection makes each of their reads
    wait on ``poll`` with whatever is left of the deadline and give up rather
    than block. The poll guard works on both families; on the socket family
    :func:`_cut_at` additionally covers a peer that stalls after the first
    byte of a message.
    """

    __slots__ = ("_connection", "_deadline_at")

    def __init__(self, connection: Connection, deadline_at: float) -> None:
        self._connection = connection
        self._deadline_at = deadline_at

    def recv_bytes(self, maxlength: int | None = None) -> bytes:
        remaining = self._deadline_at - time.monotonic()
        if remaining <= 0 or not self._connection.poll(remaining):
            raise _DeadlineCut
        return self._connection.recv_bytes(maxlength)

    def send_bytes(self, buffer: bytes) -> None:
        self._connection.send_bytes(buffer)

    def as_connection(self) -> Connection:
        """This wrapper, typed as what the challenge functions are annotated to take.

        They use exactly the two methods above; the cast is the narrowest way
        to hand them a bounded connection without copying their protocol here.
        """
        return cast(Connection, self)


@contextmanager
def _cut_at(connection: Connection, deadline_at: float) -> Iterator[threading.Event]:
    """Sever the link at *deadline_at*, so no read or write on it outlives it.

    ``recv_bytes`` has no timeout and ``poll`` only promises a first byte, so
    a peer that sends a length header and then trickles the payload passes
    every poll and would hold the reader forever. On the socket family the fix
    is real: the descriptor under the connection is a socket, ``shutdown``
    from the timer thread makes a blocked read return end-of-file at once, and
    the yielded event tells the caller the failure was the deadline rather
    than the peer hanging up. This is safe because a connection here serves
    exactly one exchange — a cut that lands just after success severs a link
    that was about to be closed anyway.

    On Windows the yielded event never fires: a named pipe is not a socket and
    there is nothing a second thread can shut down under a blocked read. The
    module docstring names that asymmetry and the poll-shaped bound that
    remains.
    """
    expired = threading.Event()
    if sys.platform == "win32":
        yield expired
        return
    watched = socket.fromfd(connection.fileno(), socket.AF_UNIX, socket.SOCK_STREAM)

    def cut() -> None:
        expired.set()
        # Discarding the OSError from shutting down a socket whose peer
        # already hung up: the link this cut exists to sever is gone either
        # way, which is the outcome the cut wanted.
        with suppress(OSError):
            watched.shutdown(socket.SHUT_RDWR)

    timer = threading.Timer(max(deadline_at - time.monotonic(), 0.0), cut)
    timer.daemon = True
    timer.start()
    try:
        yield expired
    finally:
        timer.cancel()
        watched.close()


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

    Every stage of serving one connection is bounded by the idle budget: the
    authentication challenge, the wait for the request, the read of a frame
    that has started arriving, and the write of the reply. The stdlib would
    run the challenge inside ``accept`` with unbounded reads — a peer that
    connects and never speaks would hold the accept loop forever — so the
    listener is created without an authkey and the same stdlib challenge runs
    here instead, against the budget.

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
        idle_seconds: float = IDLE_SECONDS,
    ) -> None:
        if idle_seconds <= 0:
            raise ValueError(f"idle_seconds must be positive, got {idle_seconds}")
        self._family = family or local_family()
        self._handler = handler
        self._authkey = authkey
        self._idle_seconds = idle_seconds
        # No authkey here: `Listener.accept` would run the challenge with
        # unbounded reads. `_authenticate` runs the identical challenge —
        # same stdlib functions, same order, wire-compatible with a stdlib
        # `Client` — bounded by the idle budget.
        self._listener = Listener(
            address,
            family="AF_PIPE" if self._family == FAMILY_PIPE else "AF_UNIX",
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
        """Accept one connection, authenticate it, answer it, close it.

        Returns whether the loop should continue. A failed accept — which is
        what :meth:`close` looks like from in here — returns ``False`` rather
        than raising, so the loop below ends quietly on shutdown instead of
        logging a traceback every time the sidecar stops.
        """
        try:
            connection = self._listener.accept()
        except (OSError, EOFError):
            return False
        except Exception:
            # Anything else out of `accept` is a caller's malformed approach,
            # not an event worth stopping the server for; it is also not
            # worth logging in detail, because the interesting field would be
            # the key.
            return not self._stopping.is_set()
        with closing(connection):
            if self._stopping.is_set():
                # The connection `close` opened to unblock this accept.
                # Challenging it would mean waiting on a peer that exists
                # only to be a wake-up call.
                return False
            if not self._authenticate(connection):
                # The peer failed or abandoned the challenge. Dropping it and
                # serving the next caller keeps one bad client from being a
                # denial of service against the good ones.
                return not self._stopping.is_set()
            self._exchange(connection)
        return True

    def _authenticate(self, connection: Connection) -> bool:
        """Run the challenge with the peer; ``True`` when it held the key.

        A wait on another process, so it is bounded: the challenge is two
        small messages, and a peer that has not produced them within the idle
        budget is not slow — it is gone, or it is a probe holding the line
        open.
        """
        deadline_at = time.monotonic() + self._idle_seconds
        guarded = _Guarded(connection, deadline_at).as_connection()
        try:
            with _cut_at(connection, deadline_at):
                deliver_challenge(guarded, self._authkey)
                answer_challenge(guarded, self._authkey)
        except (AuthenticationError, AssertionError, _DeadlineCut, OSError, EOFError):
            # Discarding which of the ways the challenge failed: wrong key,
            # not the challenge protocol at all (the stdlib asserts on that),
            # deadline crossed, peer hung up. The remedy is the same for all
            # of them — drop the connection, keep serving — and the only
            # detail a message could add is the key itself.
            return False
        return True

    def _exchange(self, connection: Connection) -> None:
        try:
            # The idle poll sits inside the guard because a hang-up surfaces
            # from it directly on Windows: a named pipe whose peer vanished
            # raises ``BrokenPipeError`` out of ``poll``, where a Unix socket
            # answers the poll and raises ``EOFError`` from the recv below.
            # One fact, two spellings — with the poll outside the guard, the
            # Windows spelling unwound ``serve_forever`` and one abandoned
            # client took the sidecar down with it.
            if not connection.poll(self._idle_seconds):
                return
            with _cut_at(connection, time.monotonic() + self._idle_seconds):
                data = connection.recv_bytes(maxlength=MAX_REQUEST_BYTES)
        except (OSError, EOFError):
            # Discarding whether the frame overflowed the cap (the recv layer
            # refuses an over-cap length before reading the payload), stalled
            # past the idle budget mid-frame, or the peer hung up: there is
            # nobody left to answer, and the next connection is unaffected.
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
        # Bounded like the read (on the socket family; the pipe residue is
        # the module docstring's): a peer that asked and then stopped reading
        # would otherwise park this thread in `send_bytes` once the buffer
        # filled. The suppress discards the send failure itself — the peer is
        # gone, and dropping the answer to a caller that hung up is the
        # smaller loss than holding the thread for it.
        with (
            suppress(OSError, EOFError),
            _cut_at(connection, time.monotonic() + self._idle_seconds),
        ):
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

    A new connection per call, matching the server. One deadline covers the
    whole exchange rather than each syscall: connect, handshake, send and the
    read of the answer all spend from the same budget, enforced by the poll
    guard on every wait and — on the socket family — by a watchdog that cuts
    the link when the budget runs out, so even a peer that answers one byte a
    second cannot stretch the call past the deadline. (On Windows the
    mid-read and mid-send bounds are weaker; see the module docstring.)
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
        """Make one call, every wait inside it bounded by the client's deadline.

        Raises:
            RpcUnavailable: the server could not be reached, closed the
                connection, or did not answer within the deadline.
            RpcError: the answer was not a response this build understands.
        """
        request = RpcRequest(id=secrets.token_hex(8), method=method, params=dict(params or {}))
        data = encode_request(request)
        deadline_at = time.monotonic() + self._deadline
        try:
            connection = self._dial()
        except (OSError, EOFError) as exc:
            raise RpcUnavailable(f"{method}: the sidecar is not answering ({exc})") from None
        with closing(connection), _cut_at(connection, deadline_at) as cut:
            self._handshake(method, connection, deadline_at, cut)
            try:
                connection.send_bytes(data)
                remaining = deadline_at - time.monotonic()
                if remaining <= 0 or not connection.poll(remaining):
                    raise RpcUnavailable(f"{method}: no answer within {self._deadline:g}s")
                answer = connection.recv_bytes(maxlength=MAX_RESPONSE_BYTES)
            except (OSError, EOFError) as exc:
                if cut.is_set():
                    raise RpcUnavailable(
                        f"{method}: no answer within {self._deadline:g}s "
                        "(the read was cut at the deadline)"
                    ) from None
                raise RpcUnavailable(
                    f"{method}: the sidecar closed the connection ({exc})"
                ) from None
        response = decode_response(answer)
        if response.id != request.id:
            # One request per connection, so a mismatched id cannot be a
            # reordering; it means the answer belongs to something else.
            raise RpcError(f"{method}: the answer does not match the request")
        return response

    def _dial(self) -> Connection:
        """Connect, without the stdlib handshake — that runs bounded, in :meth:`call`.

        The socket family dials by hand for two reasons: the connect itself
        carries a timeout (a full backlog on a wedged sidecar blocks
        ``connect``, and the stdlib dial has no way to say for how long), and
        the descriptor under the returned connection is then one the watchdog
        can cut. The pipe family keeps the stdlib dial: ``CreateFile`` opens
        or fails without blocking, and a busy pipe is retried against
        multiprocessing's own twenty-second cap — a real bound, though a
        looser one than this client's deadline.
        """
        if self._descriptor.family == FAMILY_PIPE:
            return Client(self._descriptor.address, family="AF_PIPE")
        if not unix_socket_supported():
            # A descriptor naming an AF_UNIX socket on a platform that has none
            # is a sidecar this machine cannot reach — a mismatched or stale
            # descriptor, not a bug here. Raised as OSError so `call` maps it to
            # "the sidecar is not answering" (and the entry point to
            # EXIT_NOT_WIRED) rather than crashing with the AttributeError that
            # `socket.AF_UNIX` throws where the attribute does not exist.
            raise OSError("the descriptor names an AF_UNIX socket and this platform has none")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self._deadline)
            sock.connect(self._descriptor.address)
            # Back to blocking before the descriptor is handed over: the
            # timeout flag lives on the file description, the reads are
            # bounded by the poll guard and the watchdog instead, and a
            # non-blocking descriptor would turn them into instant errors.
            sock.settimeout(None)
        except OSError:
            sock.close()
            raise
        return Connection(sock.detach())

    def _handshake(
        self,
        method: str,
        connection: Connection,
        deadline_at: float,
        cut: threading.Event,
    ) -> None:
        """The stdlib challenge, in the stdlib client order, against the deadline."""
        timed_out = RpcUnavailable(
            f"{method}: the sidecar accepted the connection but did not finish "
            f"the handshake within {self._deadline:g}s"
        )
        guarded = _Guarded(connection, deadline_at).as_connection()
        try:
            answer_challenge(guarded, self._authkey)
            deliver_challenge(guarded, self._authkey)
        except _DeadlineCut:
            raise timed_out from None
        except (AuthenticationError, AssertionError):
            # AssertionError is the stdlib's way of saying the peer did not
            # speak the challenge protocol at all; both spell "not our
            # sidecar with our token" to the caller, and neither message may
            # quote the key.
            raise RpcUnavailable(
                f"{method}: the sidecar refused this connection; the token does not "
                "match, which means the sidecar restarted since this process read it"
            ) from None
        except (OSError, EOFError) as exc:
            if cut.is_set():
                raise timed_out from None
            raise RpcUnavailable(f"{method}: the sidecar closed the connection ({exc})") from None


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
