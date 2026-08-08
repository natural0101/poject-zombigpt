"""The RPC secret and the file that finds a running server.

The token's rules are all about where it must *not* be, so most of these tests
assert an absence. An absence is easy to assert vacuously — `assert x not in y`
passes when `y` is empty — so each one first establishes that the thing it is
looking for exists somewhere it is allowed to be.

The descriptor's rules are all about refusing to use it. A descriptor outlives a
killed sidecar, and on POSIX so does the socket file, so "the file is there"
proves nothing about whether anything is listening. Connecting to a stale
address is not a hang: the pid can have been reused, and then the client reads a
different process's silence as the core's state.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import pytest

from pz_agent_cli.supervisor import SidecarRpc
from pz_agent_core.rpc import token as token_module
from pz_agent_core.rpc.descriptor import (
    DESCRIPTOR_FILENAME,
    DESCRIPTOR_FORMAT,
    FAMILY_PIPE,
    FAMILY_UNIX,
    DescriptorError,
    RpcDescriptor,
    StaleDescriptor,
    descriptor_path,
    load_descriptor,
    remove_descriptor,
    runtime_dir,
    write_descriptor,
)
from pz_agent_core.rpc.token import (
    MIN_TOKEN_BYTES,
    TOKEN_FILENAME,
    TokenError,
    issue_token,
    read_token,
    revoke_token,
)
from pz_agent_core.rpc.transport import RpcClient
from pz_agent_core.rpc.wire import RpcRequest, RpcResponse
from pz_agent_core.version import RPC_PROTOCOL_VERSION


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "pz-agent"


def _running(state: Path) -> RpcDescriptor:
    """A descriptor for this process, with its token beside it."""
    issue_token(runtime_dir(state))
    descriptor = RpcDescriptor(
        address=str(state / "core-rpc.sock"), family=FAMILY_UNIX, pid=os.getpid()
    )
    write_descriptor(state, descriptor)
    return descriptor


class TestTheToken:
    def test_a_token_is_at_least_thirty_two_random_bytes(self, state: Path) -> None:
        token = issue_token(runtime_dir(state))

        assert len(token) >= MIN_TOKEN_BYTES
        assert token == read_token(runtime_dir(state))

    def test_every_run_gets_a_new_one(self, state: Path) -> None:
        """A token that outlived its process would re-authorise a stale client."""
        first = issue_token(runtime_dir(state))
        second = issue_token(runtime_dir(state))

        assert first != second
        assert read_token(runtime_dir(state)) == second

    def test_the_file_holds_exactly_the_bytes_that_were_issued(self, state: Path) -> None:
        """Length as well as content, because the failure was a length change.

        On Windows `os.open` defaults to text mode, so `os.write` turned every
        `0x0A` in the token into `0x0D 0x0A`. About one token in eight contains a
        newline, so one Windows run in eight wrote 33 bytes, and the client read
        a secret that had never been issued. It surfaced as an authentication
        refusal, which is the last place anyone would look for an encoding bug.
        """
        for _ in range(64):
            token = issue_token(runtime_dir(state))
            path = runtime_dir(state) / TOKEN_FILENAME

            assert path.stat().st_size == MIN_TOKEN_BYTES
            assert path.read_bytes() == token

    def test_a_token_containing_a_newline_survives_verbatim(self, state: Path) -> None:
        """The specific byte, forced rather than waited for.

        The loop above meets a newline eventually; this one guarantees it, so
        the case cannot pass by luck on a run where no token happened to contain
        one.
        """
        forced = b"\x01" * 15 + b"\n" + b"\x02" * 16
        assert len(forced) == MIN_TOKEN_BYTES

        path = runtime_dir(state)
        path.mkdir(parents=True, exist_ok=True)
        (path / TOKEN_FILENAME).write_bytes(forced)

        assert read_token(path) == forced
        assert (path / TOKEN_FILENAME).stat().st_size == MIN_TOKEN_BYTES

    def test_the_token_is_opened_in_binary_mode_where_that_is_a_distinction(
        self, state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The only form of this assertion a POSIX machine can make.

        `O_BINARY` is 0 on POSIX, so asserting `flags & os.O_BINARY` here is
        asserting `flags & 0 == 0`, which is true of everything. That was the
        first version of this test and it passed against the very code that
        shipped the bug — a guard that cannot fail, written to catch a defect
        that had already happened.

        So the module's `_BINARY` is replaced with the value Windows actually
        uses, and the flags are checked for it. The substitution is what makes
        this a test rather than a tautology: on POSIX there is no observable
        difference, and the only honest thing left to check is that the call
        site would carry the flag if the platform had one.
        """
        windows_o_binary = 0x8000
        monkeypatch.setattr(token_module, "_BINARY", windows_o_binary)
        seen: list[int] = []
        real = os.open

        def recording(path: object, flags: int, mode: int = 0o777, **kwargs: object) -> int:
            seen.append(flags)
            return real(path, flags & ~windows_o_binary, mode, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "open", recording)
        issue_token(runtime_dir(state))

        assert seen, "issue_token did not open anything by descriptor"
        assert all(flags & windows_o_binary for flags in seen), (
            "the token file is opened in text mode; on Windows that turns a "
            "newline byte inside the secret into two bytes"
        )

    def test_the_binary_flag_is_the_platform_s_own(self) -> None:
        """And it must still be whatever the running platform says it is."""
        assert getattr(os, "O_BINARY", 0) == token_module._BINARY

    def test_two_tokens_are_never_the_same(self, state: Path) -> None:
        """Cheap, but it is the assertion that fails if the CSPRNG is swapped out."""
        issued = {issue_token(runtime_dir(state)) for _ in range(32)}

        assert len(issued) == 32

    def test_the_token_lives_apart_from_the_descriptor(self, state: Path) -> None:
        """A descriptor is meant to be read; the key must not travel with it."""
        token = _running(state)
        del token

        recorded = descriptor_path(state).read_bytes()
        secret = read_token(runtime_dir(state))

        assert secret, "nothing to look for — the assertion below would be vacuous"
        assert secret not in recorded
        assert secret.hex().encode() not in recorded
        assert json.loads(recorded)["token_file"] == TOKEN_FILENAME

    @pytest.mark.skipif(sys.platform == "win32", reason="no POSIX mode bits on Windows")
    def test_the_file_is_not_readable_by_anyone_else(self, state: Path) -> None:
        issue_token(runtime_dir(state))

        mode = (runtime_dir(state) / TOKEN_FILENAME).stat().st_mode

        assert mode & 0o077 == 0, oct(mode)

    def test_a_clean_shutdown_removes_it(self, state: Path) -> None:
        issue_token(runtime_dir(state))

        assert revoke_token(runtime_dir(state)) is True
        assert not (runtime_dir(state) / TOKEN_FILENAME).exists()
        assert revoke_token(runtime_dir(state)) is False

    def test_revoking_never_raises_because_it_runs_during_shutdown(self, state: Path) -> None:
        """An exception here would leave the descriptor behind as well."""
        assert revoke_token(runtime_dir(state)) is False
        assert revoke_token(Path("/definitely/not/a/directory")) is False

    def test_a_truncated_token_is_refused_rather_than_used(self, state: Path) -> None:
        """A short file is a partial write or somebody else's, and either way
        authenticating with it means authenticating with a known value."""
        issue_token(runtime_dir(state))
        (runtime_dir(state) / TOKEN_FILENAME).write_bytes(b"short")

        with pytest.raises(TokenError, match="expected at least"):
            read_token(runtime_dir(state))

    def test_an_absent_token_says_the_sidecar_is_not_running(self, state: Path) -> None:
        with pytest.raises(TokenError, match="not running"):
            read_token(runtime_dir(state))

    def test_no_error_message_contains_the_token(self, state: Path) -> None:
        """An exception is written into a traceback before any redactor sees it."""
        token = issue_token(runtime_dir(state))
        (runtime_dir(state) / TOKEN_FILENAME).write_bytes(token[:8])

        with pytest.raises(TokenError) as caught:
            read_token(runtime_dir(state))

        assert token.hex() not in str(caught.value)
        assert token[:8].hex() not in str(caught.value)


class _CapturingHandler(logging.Handler):
    """Keeps every spelling a log record could smuggle a value out in."""

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.texts: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.texts.append(f"{record.msg!r} {record.args!r} {record.getMessage()}")


class TestNoLogRecordCarriesTheToken:
    def test_a_real_publish_dial_answer_shutdown_logs_the_token_nowhere(self, state: Path) -> None:
        """E12-M01-T002: observed over log records, not inferred from exceptions.

        The writers this exercises are the real ones — ``issue_token`` into the
        real runtime directory, the real transport authenticating with the key,
        the sidecar's publish/serve/unpublish lifecycle — while every Python
        log record they could emit is captured: the root logger catches all
        propagating loggers, and ``multiprocessing``'s own logger is tapped
        directly because it does not propagate. The state directory's files
        are swept for the token and its hex as well, since "the sidecar's log"
        is a file before it is anything else. A ``logging.info("issued %s",
        token.hex())`` added to any of these paths fails here; before this
        test, it failed nothing.
        """
        capture = _CapturingHandler()
        root = logging.getLogger()
        mp_logger = logging.getLogger("multiprocessing")
        previous_levels = (root.level, mp_logger.level)
        root.addHandler(capture)
        mp_logger.addHandler(capture)
        root.setLevel(logging.DEBUG)
        mp_logger.setLevel(logging.DEBUG)

        def echo(request: RpcRequest) -> RpcResponse:
            return RpcResponse(id=request.id, ok=True, result={"method": request.method})

        rpc = SidecarRpc(state_dir=state, handler=echo)
        try:
            rpc.start()
            token = read_token(runtime_dir(state))
            # The canary proves the trap is armed: a capture that silently
            # caught nothing would make every absence below vacuous.
            logging.getLogger("pz_agent_core.rpc").debug("canary %s", "record")
            answer = RpcClient(load_descriptor(state), authkey=token, deadline=10.0).call(
                "session.status"
            )
            assert answer.ok

            swept = {
                path.name: path.read_bytes()
                for path in state.rglob("*")
                if path.is_file() and path.name != TOKEN_FILENAME
            }
            rpc.close()
        finally:
            root.removeHandler(capture)
            mp_logger.removeHandler(capture)
            root.setLevel(previous_levels[0])
            mp_logger.setLevel(previous_levels[1])

        assert any("canary" in text for text in capture.texts), "the capture caught nothing"
        assert DESCRIPTOR_FILENAME in swept, "the published files were not swept"
        spellings = (token.hex(), token.hex().upper(), repr(token), token.decode("latin-1"))
        for text in capture.texts:
            for spelling in spellings:
                assert spelling not in text, f"a log record carries the token: {text[:80]!r}"
        for name, raw in swept.items():
            assert token not in raw, f"{name} carries the token bytes"
            assert token.hex().encode() not in raw, f"{name} carries the token hex"


class TestFindingAServer:
    def test_a_descriptor_for_a_live_process_is_accepted(self, state: Path) -> None:
        written = _running(state)

        assert load_descriptor(state) == written

    def test_no_descriptor_means_the_sidecar_is_not_running(self, state: Path) -> None:
        with pytest.raises(StaleDescriptor, match="not running"):
            load_descriptor(state)

    def test_a_descriptor_naming_a_dead_process_is_refused(self, state: Path) -> None:
        """The case that matters: killed rather than stopped, file left behind."""
        issue_token(runtime_dir(state))
        write_descriptor(
            state,
            RpcDescriptor(address="/tmp/x.sock", family=FAMILY_UNIX, pid=_a_dead_pid()),
        )

        with pytest.raises(StaleDescriptor, match="which is gone"):
            load_descriptor(state)

    def test_a_descriptor_whose_token_is_gone_is_refused(self, state: Path) -> None:
        """Shutdown revokes the token first; the window between is not connectable."""
        _running(state)
        revoke_token(runtime_dir(state))

        with pytest.raises(StaleDescriptor, match="no token"):
            load_descriptor(state)

    def test_a_different_major_is_refused_with_the_reason(self, state: Path) -> None:
        _running(state)
        document = json.loads(descriptor_path(state).read_text(encoding="utf-8"))
        document["protocol"] = "2.0"
        descriptor_path(state).write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(DescriptorError, match="two of them are installed"):
            load_descriptor(state)

    @pytest.mark.parametrize(
        "mutation",
        [
            {"format": "somebody-elses/1"},
            {"address": ""},
            {"family": "AF_INET"},
            {"pid": 0},
            {"pid": "1234"},
            {"pid": True},
        ],
        ids=["foreign", "no-address", "network-family", "no-pid", "text-pid", "bool-pid"],
    )
    def test_a_descriptor_that_is_not_ours_is_refused(
        self, state: Path, mutation: dict[str, object]
    ) -> None:
        _running(state)
        document = json.loads(descriptor_path(state).read_text(encoding="utf-8"))
        document.update(mutation)
        descriptor_path(state).write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(DescriptorError):
            load_descriptor(state)

    @pytest.mark.parametrize("body", ["", "not json", "[]", '"text"', "null"])
    def test_a_descriptor_that_is_not_a_json_object_is_refused(
        self, state: Path, body: str
    ) -> None:
        _running(state)
        descriptor_path(state).write_text(body, encoding="utf-8")

        with pytest.raises(DescriptorError):
            load_descriptor(state)

    def test_an_af_inet_address_is_refused_because_this_link_is_local(self, state: Path) -> None:
        """There is nothing here to reach from another machine, and there must not be."""
        _running(state)
        document = json.loads(descriptor_path(state).read_text(encoding="utf-8"))
        document["family"] = "AF_INET"
        document["address"] = "0.0.0.0:9000"
        descriptor_path(state).write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(DescriptorError, match="family"):
            load_descriptor(state)


class TestWriting:
    def test_the_descriptor_records_the_format_and_this_protocol(self, state: Path) -> None:
        _running(state)

        document = json.loads(descriptor_path(state).read_text(encoding="utf-8"))

        assert document["format"] == DESCRIPTOR_FORMAT
        assert document["protocol"] == RPC_PROTOCOL_VERSION

    def test_it_is_written_whole_or_not_at_all(self, state: Path) -> None:
        """A half-written descriptor parses as malformed, which sends a user to
        reinstall when they only needed to restart."""
        _running(state)

        leftovers = [
            path.name for path in runtime_dir(state).iterdir() if path.name.endswith(".tmp")
        ]

        assert leftovers == []
        assert json.loads(descriptor_path(state).read_text(encoding="utf-8"))

    def test_the_descriptor_names_the_token_file_rather_than_its_path(self, state: Path) -> None:
        """An absolute path would carry the profile directory, and the account name."""
        _running(state)

        document = json.loads(descriptor_path(state).read_text(encoding="utf-8"))

        assert document["token_file"] == TOKEN_FILENAME
        assert "/" not in document["token_file"]
        assert "\\" not in document["token_file"]

    def test_removing_it_is_safe_when_there_is_nothing_to_remove(self, state: Path) -> None:
        assert remove_descriptor(state) is False
        _running(state)
        assert remove_descriptor(state) is True
        assert not descriptor_path(state).exists()

    def test_the_descriptor_is_named_where_the_mcp_executable_will_look(self, state: Path) -> None:
        """The MCP process is launched with a state directory and nothing else."""
        _running(state)

        assert descriptor_path(state) == state / "runtime" / DESCRIPTOR_FILENAME

    @pytest.mark.parametrize("family", [FAMILY_UNIX, FAMILY_PIPE])
    def test_both_families_round_trip(self, state: Path, family: str) -> None:
        """Windows ships the pipe and the tests run on the socket; neither is a fallback."""
        issue_token(runtime_dir(state))
        address = r"\\.\pipe\pz-agent-core-1" if family == FAMILY_PIPE else "/tmp/x.sock"
        write_descriptor(state, RpcDescriptor(address=address, family=family, pid=os.getpid()))

        assert load_descriptor(state).family == family
        assert load_descriptor(state).address == address


def _a_dead_pid() -> int:
    """A pid nothing holds.

    Spawned and reaped rather than picked out of the air: a high number is not
    reliably free, and a test that silently pointed at a live process would pass
    for the wrong reason.
    """
    import subprocess  # noqa: PLC0415

    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait(timeout=30)
    return child.pid
