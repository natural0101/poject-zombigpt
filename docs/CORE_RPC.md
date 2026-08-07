# Local Core RPC

Two processes run on one machine and only one of them owns anything.

The **sidecar** (`pz-agent`) holds the session, the observation store, the action
engine, the capability report and the memory. The **MCP server**
(`pz-agent-mcp`) holds none of it: an MCP client launches it as a subprocess and
speaks JSON-RPC over its stdin and stdout, so it is a separate process by the
protocol's design, not by ours. Everything it reports, it has to ask for.

This document is the asking. It is implemented in
`packages/pz_agent_core/src/pz_agent_core/rpc/` and pinned by
`schemas/core_rpc_request.schema.json` and
`schemas/core_rpc_response.schema.json`.

## Local, and nothing else

The transport is a Windows named pipe (`AF_PIPE`) or a Unix socket (`AF_UNIX`).
There is no TCP address, no port, and no configuration that would produce one. A
descriptor naming `AF_INET` is refused rather than dialled.

This is not caution for its own sake. The system acts on a running game on the
user's own computer; there is no capability that a network address would add and
no user who wants one. An address reachable from another machine would be a new
attack surface in exchange for nothing.

## The envelope

One request per connection, one answer, then close. A connection that lives for
one exchange needs no liveness logic, and getting liveness logic wrong is how a
process ends up holding a socket with nobody on the other end.

```json
{
  "format": "pz-agent-core-rpc/1",
  "protocol": "1.0",
  "id": "3f1c9a2b",
  "method": "session.status",
  "params": {}
}
```

```json
{
  "format": "pz-agent-core-rpc/1",
  "protocol": "1.0",
  "id": "3f1c9a2b",
  "ok": true,
  "result": { "mode": "OBSERVE", "armed": false }
}
```

`id` is chosen by the client and echoed back. One request per connection means a
mismatched id cannot be a reordering — it means the answer belongs to something
else, so it is refused rather than matched.

`ok` is always present and never inferred. A reader that took an absent `error`
for success would turn a dropped field into a success; one that took it for
failure would invent a failure with no reason. Both are worse than saying the
answer was unreadable.

### JSON, never pickle

`multiprocessing.connection` offers `send`/`recv`, which pickle, one letter away
from `send_bytes`/`recv_bytes`, which do not. A pickle stream is code execution
by construction: a process that can write to the pipe can run anything in the
process that reads it, and the token below authenticates the *connection*, not
each message.

So this package uses `send_bytes`/`recv_bytes` only, and there is no code path in
it that unpickles. `tests/unit/test_rpc_wire.py` feeds a pickle whose `__reduce__`
would raise on load, and `tests/unit/test_rpc_transport.py` poisons
`Connection.recv` and `Connection.send` for the duration of a real call, so
reaching for the convenient call fails in the suite rather than in a user's
process.

### Bounds

| Direction | Cap | Why that side |
| --- | --- | --- |
| Request | 64 KiB | A method name and a small parameter object. The largest real one submits an action, which is a few hundred bytes. |
| Response | 4 MiB | The core answers with observations, and a tier-2 snapshot of a nested inventory is the biggest document this system has. |

Both are checked on the bytes, before parsing. A cap applied after `json.loads`
has already paid the cost it was there to avoid.

A response that overflows is replaced by a `TOO_LARGE` error rather than
dropped: the client is waiting, and a server that answered nothing would leave
it to the deadline.

### Errors

| Code | Meaning |
| --- | --- |
| `MALFORMED` | Not an envelope this build can read. |
| `TOO_LARGE` | Over the cap for its direction. |
| `PROTOCOL_MISMATCH` | The peer's major version differs. |
| `UNKNOWN_METHOD` | The router has no such name. |
| `CORE_REFUSED` | The core's own refusal, carried verbatim. |
| `TIMEOUT`, `UNAVAILABLE` | The client's own; a server never sends these. |

**An error message names the field and the reason, never the value.** This
string reaches a traceback, a log line and eventually a bug report, and it is
written before any redactor sees it. A message that echoed what it rejected
would put game text, a path, or a token somewhere nothing redacts.

### Versioning

`protocol` is major-compatible: a peer on `1.7` talks to a `1.0` build, because a
minor bump only adds methods. A different major is refused rather than attempted.

The realistic cause of a mismatch is an executable left behind by an earlier
install talking to a newer sidecar — both halves ship together, so two majors on
one machine means two installs. A client that guessed at the difference would
report the core's state incorrectly rather than not at all.

`RPC_PROTOCOL_VERSION` lives in `pz_agent_core.version` beside the mod protocol
version and is deliberately separate from it: the mod↔sidecar protocol is
constrained by what Kahlua can encode and this one is not, so they move at
different speeds. `scripts/check_versions.py` fails if the schemas drift from
either.

## Finding a running server

`<state-dir>/runtime/core-rpc.json`:

```json
{
  "format": "pz-agent-core-rpc-descriptor/1",
  "protocol": "1.0",
  "family": "AF_PIPE",
  "address": "\\\\.\\pipe\\pz-agent-core-4812-9f8e7d6c5b4a3210",
  "pid": 4812,
  "token_file": "core-rpc.key"
}
```

The MCP executable is launched with a state directory and nothing else, so this
is the only way it can learn the address.

The descriptor is **not** a secret and is meant to be readable. It names the
token file rather than containing the token, and by name rather than by path: an
absolute path would carry the profile directory, and so the account name, into a
file that gets read and quoted freely.

### Why a descriptor is checked before it is used

A descriptor outlives the process that wrote it whenever the sidecar is killed
rather than stopped, and on POSIX the socket file outlives it too. So the file
being there proves nothing about whether anything is listening.

`load_descriptor` refuses unless all of:

* the format and the protocol major match this build;
* the recorded process is still alive;
* the token file is still beside it.

The middle one matters most. Connecting to a stale address is not merely a hang:
a pid can be reused, and then the client reads a *different* process's silence as
the core's state. "The sidecar is not running" is a good answer; connecting to
something else is not.

Liveness is `OpenProcess` + `GetExitCodeProcess` on Windows and `os.kill(pid, 0)`
on POSIX. Not `os.kill` on both: on Windows that call terminates the process
instead of probing it, so the liveness check would kill the sidecar it was asking
about.

## The token

`<state-dir>/runtime/core-rpc.key`, and every rule about it is about where it
must not be.

* **32 random bytes** from `secrets.token_bytes` — the CSPRNG, not `random`.
* **A new one every run.** A token that outlived its process would let a stale
  client from a previous session reconnect to a new one.
* **Its own file**, created with mode `0600` by descriptor — the mode is set at
  creation rather than by a later `chmod`, because between those two calls the
  secret is on disk world-readable, and that window is exactly when it is worth
  stealing. On Windows it inherits the profile ACL, which is what protects the
  saves and the config; being per-run is what limits the exposure there.
* **Deleted on a clean shutdown**, so a crashed run is distinguishable from a
  finished one by whether the key is still there.
* **Never logged, never in a support bundle, never in an exception message.**

A file shorter than 32 bytes is refused rather than used: it means a truncated
write or somebody else's file, and authenticating with it would mean
authenticating with a value that is not secret.

## Deadlines

A client waits **10 seconds** for one answer. Long enough for the core to read an
observation off disk; short enough that a wedged sidecar does not wedge the MCP
client that launched us, because an MCP client with a hung tool call shows the
user a spinner and no way out.

The deadline covers the whole exchange rather than each syscall. A server that
accepted and then answered one byte a second would satisfy any per-read timeout
and never finish.

The server drops a connection that authenticated and then said nothing for 60
seconds. That is a crashed client or a probe, and either way it must not hold a
thread.

## Failure is an answer

A handler that raises becomes a `CORE_REFUSED` response. The server outlives its
handlers: a method that propagated would take down the link every client shares,
and the client would see a closed socket rather than the reason.

## Shutdown

Closing a listening socket does **not** wake a thread blocked in `accept` on it.
`RpcServer.close` therefore opens a throwaway connection to its own address; the
`accept` returns it, the loop sees the stop flag, and it ends. Deliberately not
`Client` for the wake — that performs the authentication handshake and waits for
a reply, so with nothing in `accept` it would block instead of unblocking.

On POSIX the socket file is removed too, so a restart binds rather than failing
on an address in use by nobody.

## Both families, everywhere

Windows ships the named pipe. The test suite runs on Linux and binds the Unix
socket. Neither is a fallback for the other, and a test that only exercised the
family it happens to be running under would leave the shipped one uncovered —
which is the shape of the twenty-four Windows failures this branch started with.

So the descriptor and envelope tests cover both families on every platform,
`tests/unit/test_rpc_transport.py` asserts which family it is on rather than
assuming, and the pipe-name and socket-path shapes are each checked from the
other platform.

One asymmetry is real and is not hidden: `sun_path` is 108 bytes on Linux and 104
on macOS, so a POSIX address under a deep state directory will not bind. That is
refused up front with the length and the remedy — never with the path, which runs
through the profile directory. A named pipe has no such limit.
