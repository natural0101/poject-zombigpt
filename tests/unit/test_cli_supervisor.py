"""Process lifecycle, the control channel, and the "is the game running?" probe."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from pz_agent_cli.supervisor import (
    CONTROL_FILE_NAME,
    DEFAULT_STALE_AFTER_MS,
    PID_FILE_NAME,
    ControlKind,
    ControlRequest,
    GameRunning,
    GameRunningProbe,
    PidFile,
    PidRecord,
    ProcessListing,
    SidecarSupervisor,
    SpawnDiedError,
    SupervisorError,
    SupervisorState,
    _default_spawner,
    game_running_for_restore,
    list_processes,
    probe_game_running,
)
from pz_agent_core.protocol import SessionMode
from pz_agent_core.session.heartbeat import Heartbeat, Peer, PeerLiveness
from pz_agent_core.version import PRODUCT_VERSION
from tests.fixtures.ipc_builders import BASE_TIME_MS, FakeClock


class SpawnRecorder:
    """A stand-in for :data:`~pz_agent_cli.supervisor.Spawner`."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.calls: list[tuple[tuple[str, ...], Path, Path]] = []

    def __call__(self, argv: object, cwd: Path, log_path: Path) -> int:
        assert isinstance(argv, list)
        self.calls.append((tuple(str(item) for item in argv), cwd, log_path))
        return self.pid


class SignalRecorder:
    """A stand-in for :data:`~pz_agent_cli.supervisor.Signaller`."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self.raises = raises

    def __call__(self, pid: int, signal_number: int) -> None:
        self.calls.append((pid, signal_number))
        if self.raises is not None:
            raise self.raises


def _supervisor(
    tmp_path: Path,
    clock: FakeClock,
    *,
    spawn: SpawnRecorder | None = None,
    signaller: SignalRecorder | None = None,
    signals_supported: bool = True,
) -> SidecarSupervisor:
    return SidecarSupervisor(
        tmp_path / "pz-agent",
        clock=clock,
        spawn=spawn or SpawnRecorder(),
        signaller=signaller or SignalRecorder(),
        signals_supported=signals_supported,
    )


# ---------------------------------------------------------------------------
# the pid record
# ---------------------------------------------------------------------------


def test_the_pid_is_recorded_where_status_can_find_it(tmp_path: Path) -> None:
    clock = FakeClock()
    spawn = SpawnRecorder(pid=1234)
    supervisor = _supervisor(tmp_path, clock, spawn=spawn)

    outcome = supervisor.start(["python", "-m", "pz_agent_cli", "start", "--foreground"])

    assert outcome.started is True
    assert outcome.record is not None
    assert outcome.record.pid == 1234
    written = json.loads((tmp_path / "pz-agent" / PID_FILE_NAME).read_text(encoding="utf-8"))
    assert written["pid"] == 1234
    assert written["started_at_ms"] == BASE_TIME_MS
    assert written["version"] == PRODUCT_VERSION
    assert spawn.calls[0][0][-1] == "--foreground"


def test_status_distinguishes_never_started_from_running_from_stopped(tmp_path: Path) -> None:
    clock = FakeClock()
    supervisor = _supervisor(tmp_path, clock)

    assert supervisor.status().state is SupervisorState.NEVER_STARTED

    supervisor.start(["python", "-m", "pz_agent_cli"])
    assert supervisor.status().state is SupervisorState.RUNNING

    clock.advance(DEFAULT_STALE_AFTER_MS + 1)
    stopped = supervisor.status()
    assert stopped.state is SupervisorState.STOPPED
    assert stopped.alive is False
    assert "no longer ticking" in stopped.detail


def test_a_never_started_supervisor_says_so_rather_than_reporting_a_dead_one(
    tmp_path: Path,
) -> None:
    status = _supervisor(tmp_path, FakeClock()).status()

    assert status.state is SupervisorState.NEVER_STARTED
    assert status.record is None
    assert "no sidecar has recorded itself" in status.detail


def test_starting_twice_is_refused_while_the_first_is_still_ticking(tmp_path: Path) -> None:
    clock = FakeClock()
    spawn = SpawnRecorder()
    supervisor = _supervisor(tmp_path, clock, spawn=spawn)
    supervisor.start(["python"])

    second = supervisor.start(["python"])

    assert second.started is False
    assert "already running" in second.detail
    assert len(spawn.calls) == 1


def test_a_stale_record_does_not_block_a_restart(tmp_path: Path) -> None:
    clock = FakeClock()
    spawn = SpawnRecorder()
    supervisor = _supervisor(tmp_path, clock, spawn=spawn)
    supervisor.start(["python"])
    clock.advance(DEFAULT_STALE_AFTER_MS + 1)

    second = supervisor.start(["python"])

    assert second.started is True
    assert len(spawn.calls) == 2


def test_start_refuses_an_empty_command_line(tmp_path: Path) -> None:
    with pytest.raises(SupervisorError, match=r"start requires a command line"):
        _supervisor(tmp_path, FakeClock()).start([])


def test_an_unreadable_pid_file_reads_as_no_record_rather_than_a_live_holder(
    tmp_path: Path,
) -> None:
    """A record naming nobody can be refreshed by nobody; treating it as live wedges start."""
    clock = FakeClock()
    supervisor = _supervisor(tmp_path, clock)
    supervisor.state_dir.mkdir(parents=True, exist_ok=True)
    (supervisor.state_dir / PID_FILE_NAME).write_text("{not json", encoding="utf-8")

    assert supervisor.status().state is SupervisorState.NEVER_STARTED
    assert supervisor.start(["python"]).started is True


def test_only_the_owning_process_refreshes_its_record(tmp_path: Path) -> None:
    clock = FakeClock()
    pid_file = PidFile(tmp_path / PID_FILE_NAME, clock=clock)
    pid_file.claim(os.getpid())
    clock.advance(500)

    refreshed = pid_file.refresh()

    assert refreshed is not None
    assert refreshed.refreshed_at_ms == BASE_TIME_MS + 500
    assert refreshed.started_at_ms == BASE_TIME_MS


def test_a_record_owned_by_another_process_is_not_stamped_over(tmp_path: Path) -> None:
    clock = FakeClock()
    pid_file = PidFile(tmp_path / PID_FILE_NAME, clock=clock)
    pid_file.claim(os.getpid() + 1)

    assert pid_file.refresh() is None


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("pid", 0, r"pid must be positive"),
        ("pid", -1, r"pid must be positive"),
        ("started_at_ms", -1, r"timestamps must be non-negative"),
        ("refreshed_at_ms", -1, r"timestamps must be non-negative"),
    ],
)
def test_a_pid_record_refuses_impossible_values(field_name: str, value: int, message: str) -> None:
    fields = {"pid": 1, "started_at_ms": 0, "refreshed_at_ms": 0}
    fields[field_name] = value
    with pytest.raises(SupervisorError, match=message):
        PidRecord(
            pid=fields["pid"],
            started_at_ms=fields["started_at_ms"],
            refreshed_at_ms=fields["refreshed_at_ms"],
        )


def test_a_record_from_the_future_is_not_treated_as_fresher_than_possible() -> None:
    record = PidRecord(pid=1, started_at_ms=0, refreshed_at_ms=1_000)

    assert record.age_ms(500) == 0
    assert record.is_stale(500, 1) is False


# ---------------------------------------------------------------------------
# stopping
# ---------------------------------------------------------------------------


def test_stop_writes_the_request_and_signals_the_process(tmp_path: Path) -> None:
    clock = FakeClock()
    signaller = SignalRecorder()
    supervisor = _supervisor(tmp_path, clock, spawn=SpawnRecorder(pid=99), signaller=signaller)
    supervisor.start(["python"])

    outcome = supervisor.request_stop()

    assert outcome.requested is True
    assert outcome.signalled is True
    assert signaller.calls == [(99, int(signal.SIGTERM))]
    request = supervisor.control.read()
    assert request is not None
    assert request.kind is ControlKind.STOP


def test_stop_still_leaves_the_request_where_signals_cannot_be_delivered(
    tmp_path: Path,
) -> None:
    """On Windows ``os.kill`` terminates rather than signals, so the file is the mechanism."""
    clock = FakeClock()
    signaller = SignalRecorder()
    supervisor = _supervisor(tmp_path, clock, signaller=signaller, signals_supported=False)
    supervisor.start(["python"])

    outcome = supervisor.request_stop()

    assert outcome.requested is True
    assert outcome.signalled is False
    assert signaller.calls == []
    assert (supervisor.state_dir / CONTROL_FILE_NAME).is_file()


def test_a_signal_that_cannot_be_delivered_does_not_fail_the_stop(tmp_path: Path) -> None:
    clock = FakeClock()
    signaller = SignalRecorder(raises=ProcessLookupError("no such process"))
    supervisor = _supervisor(tmp_path, clock, signaller=signaller)
    supervisor.start(["python"])

    outcome = supervisor.request_stop()

    assert outcome.requested is True
    assert outcome.signalled is False
    assert "could not be delivered" in outcome.detail


def test_stopping_something_that_was_never_started_is_reported_as_such(tmp_path: Path) -> None:
    outcome = _supervisor(tmp_path, FakeClock()).request_stop()

    assert outcome.requested is False
    assert "no sidecar has recorded itself" in outcome.detail


def test_stopping_a_dead_record_still_leaves_a_request_and_says_why(tmp_path: Path) -> None:
    clock = FakeClock()
    supervisor = _supervisor(tmp_path, clock)
    supervisor.start(["python"])
    clock.advance(DEFAULT_STALE_AFTER_MS + 1)

    outcome = supervisor.request_stop()

    assert outcome.requested is True
    assert outcome.signalled is False
    assert "not ticking" in outcome.detail


# ---------------------------------------------------------------------------
# the control channel
# ---------------------------------------------------------------------------


def test_arm_and_disarm_publish_requests_a_loop_can_read(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path, FakeClock())

    armed = supervisor.arm(SessionMode.AUTONOMOUS)
    assert armed.kind is ControlKind.ARM
    assert armed.mode is SessionMode.AUTONOMOUS

    disarmed = supervisor.disarm()
    assert disarmed.kind is ControlKind.DISARM
    assert disarmed.mode is None
    pending = supervisor.control.read()
    assert pending is not None
    assert pending.kind is ControlKind.DISARM


def test_a_later_request_replaces_an_unconsumed_earlier_one(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path, FakeClock())
    supervisor.arm(SessionMode.ASSISTED)

    supervisor.disarm()

    request = supervisor.control.read()
    assert request is not None
    assert request.kind is ControlKind.DISARM


def test_arming_into_observe_is_refused_as_a_disarm_in_disguise() -> None:
    with pytest.raises(SupervisorError, match=r"arming into OBSERVE is a disarm"):
        ControlRequest(kind=ControlKind.ARM, issued_at_ms=0, nonce="n", mode=SessionMode.OBSERVE)


def test_only_an_arm_request_may_name_a_mode() -> None:
    with pytest.raises(SupervisorError, match=r"only an arm request names a mode"):
        ControlRequest(kind=ControlKind.STOP, issued_at_ms=0, nonce="n", mode=SessionMode.ASSISTED)


def test_a_control_request_must_carry_a_nonce() -> None:
    with pytest.raises(SupervisorError, match=r"must carry a nonce"):
        ControlRequest(kind=ControlKind.STOP, issued_at_ms=0, nonce="")


def test_an_unparseable_control_file_is_ignored_rather_than_obeyed(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path, FakeClock())
    supervisor.state_dir.mkdir(parents=True, exist_ok=True)
    (supervisor.state_dir / CONTROL_FILE_NAME).write_text('{"kind": "detonate"}', encoding="utf-8")

    assert supervisor.control.read() is None
    assert supervisor.control.pending() is True


def test_starting_clears_a_request_the_previous_run_never_consumed(tmp_path: Path) -> None:
    clock = FakeClock()
    supervisor = _supervisor(tmp_path, clock)
    supervisor.arm(SessionMode.AUTONOMOUS)

    supervisor.start(["python"])

    assert supervisor.control.pending() is False


# ---------------------------------------------------------------------------
# is the game running?
# ---------------------------------------------------------------------------


def _liveness(*, alive: bool) -> PeerLiveness:
    beat = Heartbeat(
        peer=Peer.GAME,
        session_id="s",
        nonce="n",
        seq=0,
        timestamp_ms=BASE_TIME_MS,
        version=PRODUCT_VERSION,
    )
    return PeerLiveness(Peer.GAME, beat, alive=alive, detail="fresh" if alive else "silent")


def test_a_live_heartbeat_settles_it_without_reading_the_process_table() -> None:
    def refuse() -> ProcessListing:
        raise AssertionError("the process table must not be consulted when the mod is beating")

    probe = probe_game_running(heartbeat=_liveness(alive=True), lister=refuse)

    assert probe.verdict is GameRunning.RUNNING
    assert probe.unsafe_to_restore is True


def test_a_process_list_that_names_the_game_reports_it_running() -> None:
    listing = ProcessListing(names=("explorer.exe", "ProjectZomboid64.exe", "steam.exe"))

    probe = probe_game_running(heartbeat=None, lister=lambda: listing)

    assert probe.verdict is GameRunning.RUNNING
    assert probe.matches == ("ProjectZomboid64.exe",)


def test_a_process_list_that_ran_and_found_nothing_reports_it_closed() -> None:
    listing = ProcessListing(names=("explorer.exe", "steam.exe"))

    probe = probe_game_running(heartbeat=_liveness(alive=False), lister=lambda: listing)

    assert probe.verdict is GameRunning.NOT_RUNNING
    assert probe.unsafe_to_restore is False
    assert game_running_for_restore(probe) is False


def test_a_process_list_that_could_not_be_produced_reports_it_may_be_running() -> None:
    """ "We could not tell" is not permission to overwrite a save."""
    listing = ProcessListing(problem="tasklist.exe is not available on this machine")

    probe = probe_game_running(heartbeat=None, lister=lambda: listing)

    assert probe.verdict is GameRunning.MAY_BE_RUNNING
    assert probe.unsafe_to_restore is True
    assert game_running_for_restore(probe) is True
    assert "tasklist.exe is not available" in probe.detail


def test_a_truncated_process_list_cannot_prove_an_absence() -> None:
    listing = ProcessListing(names=("explorer.exe",), truncated=True)

    probe = probe_game_running(heartbeat=None, lister=lambda: listing)

    assert probe.verdict is GameRunning.MAY_BE_RUNNING
    assert game_running_for_restore(probe) is True


def test_the_game_is_matched_case_insensitively_and_without_an_extension() -> None:
    listing = ProcessListing(names=("projectzomboid64",))

    assert probe_game_running(heartbeat=None, lister=lambda: listing).verdict is (
        GameRunning.RUNNING
    )


def test_the_probe_serialises_the_verdict_it_reached() -> None:
    probe = GameRunningProbe(verdict=GameRunning.MAY_BE_RUNNING, detail="nothing could be read")

    assert probe.to_dict() == {
        "verdict": "may_be_running",
        "detail": "nothing could be read",
        "matches": [],
        "unsafe_to_restore": True,
    }


def test_a_listing_tool_that_is_absent_produces_a_problem_not_an_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty listing would read as "nothing is running", which is the dangerous claim."""

    def missing(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("no such tool")

    monkeypatch.setattr(subprocess, "run", missing)

    listing = list_processes()

    assert listing.usable is False
    assert "is not available on this machine" in listing.problem
    assert listing.names == ()


def test_a_listing_tool_that_hangs_produces_a_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def hang(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd="ps", timeout=5.0)

    monkeypatch.setattr(subprocess, "run", hang)

    listing = list_processes()

    assert listing.usable is False
    assert "did not finish within" in listing.problem


def test_a_listing_tool_that_fails_produces_a_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=["ps"], returncode=2, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fail)

    listing = list_processes()

    assert listing.usable is False
    assert "exited with status 2" in listing.problem


def test_the_real_listing_finds_this_python_process() -> None:
    """The default lister has to work on the machine it runs on, or it proves nothing."""
    listing = list_processes()

    assert listing.usable is True
    assert listing.names != ()


def test_a_listing_longer_than_the_cap_is_marked_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "\n".join(f"proc{index}" for index in range(5_000)).encode("utf-8")

    def flood(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=["ps"], returncode=0, stdout=body, stderr=b"")

    monkeypatch.setattr(subprocess, "run", flood)
    monkeypatch.setattr(os, "name", "posix")

    listing = list_processes()

    assert listing.truncated is True
    assert len(listing.names) == 4096


# ---------------------------------------------------------------------------
# the default spawner: a fork that succeeded is not a start
# ---------------------------------------------------------------------------


class TestTheChildIsConfirmedAlive:
    """``start`` reported success on the strength of ``Popen`` returning.

    That means a fork succeeded. It says nothing about whether the program ran,
    and a sidecar that died on its first import left ``start`` printing
    "sidecar started as pid N" and exiting 0 — after which ``arm`` failed for
    reasons that named nothing and ``stop`` reported "no such process", also
    exiting 0.

    These use a real subprocess rather than a fake spawner, because the
    behaviour under test *is* the spawner: every other test in this file injects
    a recorder, which is exactly why nothing caught this.
    """

    def test_a_child_that_exits_at_once_is_not_reported_as_started(self, tmp_path: Path) -> None:
        log = tmp_path / "sidecar.out"

        with pytest.raises(SpawnDiedError) as died:
            _default_spawner(
                [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
                tmp_path,
                log,
            )

        assert died.value.exit_code == 3
        assert "boom" in died.value.log_tail, "the child's own words are the diagnosis"
        assert "exited immediately" in str(died.value)

    def test_a_child_that_keeps_running_is_reported_as_started(self, tmp_path: Path) -> None:
        """The other direction, without which a gate that always refuses passes."""
        log = tmp_path / "sidecar.out"

        pid = _default_spawner([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, log)

        assert pid > 0
        try:
            os.kill(pid, 0)
        finally:
            os.kill(pid, signal.SIGKILL)

    def test_start_surfaces_the_death_and_claims_no_pid(self, tmp_path: Path) -> None:
        """A record for a process already gone would make status say STOPPED.

        "It crashed" and "it never ran" are different things to tell someone,
        and keeping them apart is the whole reason status has three outcomes.
        """
        supervisor = SidecarSupervisor(state_dir=tmp_path / "state")

        outcome = supervisor.start([sys.executable, "-c", "raise SystemExit(4)"])

        assert not outcome.started
        assert "exit code 4" in outcome.detail
        assert supervisor.status().state is SupervisorState.NEVER_STARTED
