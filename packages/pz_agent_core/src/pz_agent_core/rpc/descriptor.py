"""The runtime descriptor: how a client finds a running sidecar, and when it must not.

``<state-dir>/runtime/core-rpc.json`` is the only thing that connects the two
processes. The MCP executable is launched by an MCP client with no arguments but
a state directory, so it cannot be told the address — it reads it here.

The descriptor is deliberately *not* a secret. It carries the address, the
protocol version and the owning process id, all of which a reader may see. The
token lives in its own file next to it, because a descriptor that carried the
key would make every reader of the address a holder of it.

**The stale case is the one that matters.** A descriptor outlives the process
that wrote it whenever the sidecar is killed rather than stopped, and on Linux
the socket file outlives it too. A client that connected to whatever the file
named would, at best, hang; at worst it would reach a *different* process that
had since been given the same pid, and report that process's silence as the
core's state. So a descriptor is checked before it is used:

* the file must be small enough to be a descriptor at all;
* the format and protocol major must match this build;
* the recorded process must still be alive;
* the token file must still exist.

:func:`load_descriptor` refuses on any of those and says which. "The sidecar is
not running" is a good answer; connecting to something else is not.

The size check comes first and is made on the bytes, before any parse: a file
that large is not a truncated descriptor, it is something else living where this
one belongs, and finding that out by decoding it costs the whole file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from pz_agent_core.jsonbytes import deepest_nesting
from pz_agent_core.rpc.token import TOKEN_FILENAME
from pz_agent_core.version import RPC_PROTOCOL_VERSION, protocol_major

__all__ = [
    "DESCRIPTOR_FILENAME",
    "DESCRIPTOR_FORMAT",
    "MAX_DESCRIPTOR_BYTES",
    "MAX_DESCRIPTOR_DEPTH",
    "RUNTIME_DIRNAME",
    "DescriptorError",
    "OversizeDescriptor",
    "RpcDescriptor",
    "StaleDescriptor",
    "load_descriptor",
    "remove_descriptor",
    "write_descriptor",
]

RUNTIME_DIRNAME: Final = "runtime"
DESCRIPTOR_FILENAME: Final = "core-rpc.json"
DESCRIPTOR_FORMAT: Final = "pz-agent-core-rpc-descriptor/1"

#: The two transport families this project uses. ``AF_PIPE`` is a Windows named
#: pipe; ``AF_UNIX`` is a socket file. Recorded because a client on the wrong
#: platform for the address should say so rather than fail inside the socket
#: layer with an error about a filename.
FAMILY_PIPE: Final = "AF_PIPE"
FAMILY_UNIX: Final = "AF_UNIX"

#: The largest process id that can name a real process. ``pid_t`` is a signed
#: 32-bit integer on the platforms this runs on, so a value past this is not a
#: stale pid but an impossible one — and handing it to the liveness probe makes
#: ``os.kill`` raise a bare ``OverflowError`` rather than report the process
#: absent. Bounded here so the loader answers with its own refusal instead.
MAX_PID: Final = 2**31 - 1

#: The most this file can be and still be a descriptor.
#:
#: A descriptor is six fields, five of them fixed in shape and short: the format
#: string (30 bytes), a protocol version, a family name, the token file's name,
#: and a process id — ten digits at the widest, since a Windows pid is 32 bits.
#: Only the address varies, and the operating system bounds that too: a Windows
#: pipe name is at most 256 characters after ``\\.\pipe\``, which is 269 bytes
#: once JSON doubles the backslashes, and an ``AF_UNIX`` path is limited to 108
#: bytes by ``sun_path``. Indented and with every key, the whole document comes
#: to under half a kilobyte in the worst case either platform allows.
#:
#: 8 KiB is that with a sixteen-fold margin, and it is deliberately the same
#: number ``pz_agent_mcp.__main__.MAX_DESCRIPTOR_BYTES`` uses for its own prefix
#: read of this file. Equal caps mean the two readers refuse the same set of
#: files: a smaller cap here would refuse a file that the classifier there still
#: parses whole, and the user would be sent hunting for a second install over a
#: file neither half wrote.
MAX_DESCRIPTOR_BYTES: Final = 8 * 1024

#: Deepest array/object nesting a descriptor file may carry. A descriptor is a
#: flat object of six scalar fields, so this is generous; it exists because a
#: file left where the descriptor belongs can nest arbitrarily deep *inside* the
#: byte cap above — a few thousand open brackets is well under 8 KiB — and
#: ``json.loads`` recurses once per nesting level, overflowing the interpreter
#: with a ``RecursionError`` that the byte cap cannot see and that this loader's
#: contract does not name. Measured on the raw bytes before parsing — see
#: :mod:`pz_agent_core.jsonbytes`, and the same guard in :mod:`pz_agent_core.rpc.wire`
#: — so the parser is never handed a document that could take it there.
MAX_DESCRIPTOR_DEPTH: Final = 64


class DescriptorError(RuntimeError):
    """The descriptor is missing, unreadable, or not one of ours."""


class OversizeDescriptor(DescriptorError):
    """The file where the descriptor belongs is bigger than one can be.

    Separate from a malformed descriptor because the remedy is different. A
    malformed descriptor is a *descriptor* — truncated by a crash, or written by
    another build — and restarting the sidecar replaces it. A file past this cap
    was never a descriptor: something else is writing where this file lives, and
    ``RpcSupervisor`` clears an unusable descriptor on the way up, so a sidecar
    started on top of it destroys the evidence of what. The refusal says to take
    a copy first for that reason.

    Not a :class:`StaleDescriptor` for the same reason: nothing here says the
    sidecar stopped, so "start it again" is the wrong thing to tell anyone.
    """


class StaleDescriptor(DescriptorError):
    """The descriptor is well-formed but the process it names is gone.

    Separate from :class:`DescriptorError` because the remedy differs: a
    malformed descriptor is a bug or a foreign file, and a stale one just means
    start the sidecar. A caller that could not tell them apart would tell the
    user to reinstall when they only needed to run the launcher.
    """


@dataclass(frozen=True, slots=True)
class RpcDescriptor:
    """Where the server is listening, and who is listening there."""

    address: str
    family: str
    pid: int
    protocol: str = RPC_PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": DESCRIPTOR_FORMAT,
            "protocol": self.protocol,
            "family": self.family,
            "address": self.address,
            "pid": self.pid,
            # A name, not a path: the file sits beside this one, and recording
            # an absolute path would put the profile directory — and so the
            # account name — into a file that gets read and quoted freely.
            "token_file": TOKEN_FILENAME,
        }


def runtime_dir(state_dir: Path) -> Path:
    """``<state-dir>/runtime``, where both the descriptor and the token live."""
    return state_dir / RUNTIME_DIRNAME


def descriptor_path(state_dir: Path) -> Path:
    return runtime_dir(state_dir) / DESCRIPTOR_FILENAME


def write_descriptor(state_dir: Path, descriptor: RpcDescriptor) -> Path:
    """Record *descriptor*, atomically, and return where it was written.

    Atomic because a client may read at any moment and a half-written
    descriptor parses as malformed — which is the one failure that would send a
    user to reinstall rather than to restart.
    """
    directory = runtime_dir(state_dir)
    path = directory / DESCRIPTOR_FILENAME
    data = (
        json.dumps(descriptor.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)
    except OSError as exc:
        raise DescriptorError(
            f"{path.name}: cannot be written ({exc.strerror or 'unknown error'})"
        ) from exc
    return path


def _alive(pid: int) -> bool:
    """Whether *pid* names a live process, on either platform.

    Not ``os.kill(pid, 0)``: that signal number is meaningless on Windows, where
    :func:`os.kill` terminates the process instead of probing it — so the
    liveness check would kill the sidecar it was asking about. The portable
    probe is opening the process; on POSIX there is no such call, so the signal
    form is used there and only there.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes  # noqa: PLC0415  (Windows only, and only on this path)

        #: PROCESS_QUERY_LIMITED_INFORMATION — enough to learn that a process
        #: exists without asking for the right to do anything to it.
        access = 0x1000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(access, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            #: STILL_ACTIVE. A finished process keeps a queryable handle until
            #: it is closed, so the handle opening is not by itself liveness.
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Somebody else's process: it exists, and it is not ours. Treated as
        # alive so the caller refuses rather than overwriting a live address.
        return True
    return True


def load_descriptor(state_dir: Path, *, check_alive: bool = True) -> RpcDescriptor:
    """Read the descriptor and satisfy every precondition for using it.

    Args:
        state_dir: the sidecar's state directory.
        check_alive: whether the recorded process must still exist. Only a test
            that has no process to point at should pass ``False``; a client that
            skipped this would connect to whatever now holds the address.

    Raises:
        StaleDescriptor: the process is gone, or the token file is not there.
        OversizeDescriptor: the file is past :data:`MAX_DESCRIPTOR_BYTES`.
        DescriptorError: missing, malformed, foreign, or an incompatible major.
    """
    path = descriptor_path(state_dir)
    try:
        # Binary, because the cap counts bytes and Windows text mode would fold
        # every CRLF into one byte before the count ever saw it — a file crafted
        # out of line endings would then read as small and be parsed.
        with path.open("rb") as handle:
            # One byte past the cap: a file of exactly the cap is legal, and a
            # read that stopped at the cap could not tell it from a longer one.
            raw = handle.read(MAX_DESCRIPTOR_BYTES + 1)
            oversize = len(raw) > MAX_DESCRIPTOR_BYTES
            # The true size, for the refusal to quote. The read stopped early by
            # design, so its own length would only ever say "one past the cap",
            # which tells the reader nothing about what is actually there. The
            # floor keeps the number honest if the file shrinks under us.
            size = max(os.fstat(handle.fileno()).st_size, len(raw)) if oversize else len(raw)
    except FileNotFoundError:
        raise StaleDescriptor(f"{path.name}: no descriptor; the sidecar is not running") from None
    except OSError as exc:
        raise DescriptorError(
            f"{path.name}: cannot be read ({exc.strerror or 'unknown error'})"
        ) from None

    if oversize:
        # The filename and two numbers, and nothing else: this path runs through
        # the user's profile directory and so carries their account name, and
        # this string reaches a traceback before any redactor does.
        raise OversizeDescriptor(
            f"{DESCRIPTOR_FILENAME}: is {size} bytes and the limit is "
            f"{MAX_DESCRIPTOR_BYTES}; nothing this build writes there is that large, so that "
            "file is not a descriptor and something else is writing where it goes — take a "
            "copy of it before starting the sidecar, which replaces it"
        )

    # Depth is measured on the raw bytes before parsing: ``json.loads`` recurses
    # once per nesting level, so a foreign file of a few thousand open brackets —
    # small enough to clear the byte cap above — would overflow the interpreter
    # with a ``RecursionError`` this loader does not name, and catching that after
    # the fact is not safe because the stack is already spent when it fires.
    if deepest_nesting(raw) > MAX_DESCRIPTOR_DEPTH:
        raise DescriptorError(f"{path.name}: nests deeper than {MAX_DESCRIPTOR_DEPTH}")
    try:
        document: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        # ``ValueError`` rather than only its ``json.JSONDecodeError`` subclass: a
        # bare integer literal of thousands of digits — again inside the byte cap —
        # trips CPython's integer-string-conversion ceiling, which raises a plain
        # ``ValueError`` from inside ``json.loads`` that the subclass would miss and
        # let escape raw. The message names neither the digits nor the bytes.
        raise DescriptorError(f"{path.name}: is not readable JSON") from exc
    if not isinstance(document, dict):
        raise DescriptorError(f"{path.name}: must be a JSON object")
    if document.get("format") != DESCRIPTOR_FORMAT:
        raise DescriptorError(
            f"{path.name}: format {document.get('format')!r} is not {DESCRIPTOR_FORMAT!r}"
        )

    protocol = document.get("protocol")
    if not isinstance(protocol, str):
        raise DescriptorError(f"{path.name}: has no protocol version")
    try:
        theirs = protocol_major(protocol)
    except ValueError as exc:
        raise DescriptorError(f"{path.name}: has an unreadable protocol version") from exc
    if theirs != protocol_major(RPC_PROTOCOL_VERSION):
        raise DescriptorError(
            f"{path.name}: the sidecar speaks protocol {protocol}, this build speaks "
            f"{RPC_PROTOCOL_VERSION}; both halves come from one install, so this means "
            "two of them are installed"
        )

    address = document.get("address")
    family = document.get("family")
    pid = document.get("pid")
    if not isinstance(address, str) or not address:
        raise DescriptorError(f"{path.name}: has no address")
    # ``isinstance`` before the membership test, not only for tidiness: a
    # ``family`` that JSON delivered as a list or an object is unhashable, and
    # ``x in {a, b}`` raises a bare ``TypeError`` on an unhashable ``x`` before
    # any refusal is built. The type check turns that into the same named
    # refusal a wrong family string already gets.
    if not isinstance(family, str) or family not in {FAMILY_PIPE, FAMILY_UNIX}:
        raise DescriptorError(f"{path.name}: family {family!r} is not one this build speaks")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise DescriptorError(f"{path.name}: has no process id")
    # A pid past the platform's ``pid_t`` cannot name a real process, and it is
    # not inert: ``os.kill(pid, 0)`` on a value beyond a C ``long`` raises a
    # bare ``OverflowError`` from inside the liveness probe below, where the
    # loader promises only its own typed refusals. Refused here as the
    # impossible id it is.
    if pid > MAX_PID:
        raise DescriptorError(f"{path.name}: process id is beyond the platform maximum")

    if check_alive:
        if not _alive(pid):
            raise StaleDescriptor(
                f"{path.name}: names process {pid}, which is gone; the sidecar stopped "
                "without cleaning up"
            )
        if not (path.parent / TOKEN_FILENAME).exists():
            raise StaleDescriptor(
                f"{TOKEN_FILENAME}: no token beside the descriptor; the sidecar is "
                "shutting down or was interrupted"
            )

    return RpcDescriptor(address=address, family=family, pid=pid, protocol=protocol)


def remove_descriptor(state_dir: Path) -> bool:
    """Delete the descriptor. Returns whether there was one.

    Never raises: this runs on the shutdown path beside :func:`revoke_token`,
    and an exception here would leave the token behind.
    """
    try:
        descriptor_path(state_dir).unlink()
    except OSError:
        return False
    return True
