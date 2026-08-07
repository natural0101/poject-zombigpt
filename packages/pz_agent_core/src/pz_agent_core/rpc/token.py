"""The RPC authentication token: where it comes from and where it must not go.

:mod:`multiprocessing.connection` authenticates a connection with a shared
secret (``authkey``). On Windows the transport is a named pipe and on Linux a
Unix socket, and on both the filesystem permission is the first line of defence
— but a named pipe's default ACL is more permissive than a mode-0600 file, and
the socket path lives under a state directory the user can hand to anyone. So
the token is the line that actually holds, and the rules around it are stricter
than the transport's.

* **32 random bytes**, from :func:`secrets.token_bytes`, which is the CSPRNG.
* **A new one every run.** A token that outlived the process it authorised would
  let a stale client from a previous session reconnect to a new one.
* **Its own file**, never the descriptor. The descriptor names the address and
  is meant to be readable; putting the secret in it would make every reader of
  the address a holder of the key.
* **Deleted on a clean shutdown**, so a crashed run is distinguishable from a
  finished one by whether the key is still there.
* **Never logged, never in a support bundle, never in an exception message.**
  The last of those is why :class:`TokenError` says what failed and not what was
  read: an exception is the one string that reliably escapes into a traceback,
  a log line and a bug report, and it is written before any redactor sees it.

The file is created with mode ``0600`` where the platform has modes. On Windows
it inherits the profile's ACL, which is the same protection the save files and
the config get; the token being per-run is what limits the exposure there.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from typing import Final

__all__ = [
    "MIN_TOKEN_BYTES",
    "TOKEN_FILENAME",
    "TokenError",
    "issue_token",
    "read_token",
    "revoke_token",
]

#: Length of a freshly issued token, and the shortest one that will be accepted.
#: 32 bytes is 256 bits: far beyond brute force, and small enough that the file
#: is one line.
MIN_TOKEN_BYTES: Final = 32

# The name matches the security linter's "hardcoded password" heuristic; the
# value is the name of the file the secret lives in, which is not a secret.
TOKEN_FILENAME: Final = "core-rpc.key"  # noqa: S105


class TokenError(RuntimeError):
    """A token could not be issued or read.

    The message names the file and the reason and never the contents. A caller
    that wants to know why in more detail has the exception chain; a log has
    only this line, and a log is what gets attached to an issue.
    """


def issue_token(runtime_dir: Path) -> bytes:
    """Generate a token, store it, and return it.

    Any token already there is replaced: a run authorises itself, not whatever
    the last one left behind.

    Raises:
        TokenError: the directory or the file could not be written.
    """
    token = secrets.token_bytes(MIN_TOKEN_BYTES)
    path = runtime_dir / TOKEN_FILENAME
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        # Opened by descriptor with the mode set at creation rather than
        # written and then chmod-ed: between those two calls the secret is on
        # disk world-readable, and that window is exactly when a token is worth
        # stealing. O_TRUNC rather than unlink-and-create so there is no moment
        # with no file at all.
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        try:
            os.write(descriptor, token)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise TokenError(
            f"{path.name}: cannot be written ({exc.strerror or 'unknown error'})"
        ) from None
    return token


def read_token(runtime_dir: Path) -> bytes:
    """The token a running server issued.

    Raises:
        TokenError: the file is missing, unreadable, or too short to be one of
            ours. A short file means a truncated write or somebody else's file,
            and using it would authenticate a connection with a secret that is
            not secret.
    """
    path = runtime_dir / TOKEN_FILENAME
    try:
        token = path.read_bytes()
    except FileNotFoundError:
        raise TokenError(f"{path.name}: no token; the sidecar is not running") from None
    except OSError as exc:
        raise TokenError(
            f"{path.name}: cannot be read ({exc.strerror or 'unknown error'})"
        ) from None
    if len(token) < MIN_TOKEN_BYTES:
        raise TokenError(f"{path.name}: is {len(token)} bytes, expected at least {MIN_TOKEN_BYTES}")
    return token


def revoke_token(runtime_dir: Path) -> bool:
    """Delete the token. Returns whether there was one to delete.

    Never raises for an absent file: this runs on the shutdown path, where the
    only thing worse than a leftover token is an exception that stops the rest
    of the cleanup.
    """
    path = runtime_dir / TOKEN_FILENAME
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True
