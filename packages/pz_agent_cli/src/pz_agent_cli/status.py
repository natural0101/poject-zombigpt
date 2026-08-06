"""``pz-agent status`` — what the exchange directory says right now.

Read-only, and deliberately shallow: it reports the heartbeats, the session
file, the lock and the capability revision, which is exactly the set of facts
that distinguishes "the mod is not loaded" from "no save is open" from "the
sidecar died". It never infers a state it did not read — an absent heartbeat is
reported as absent, not as "the game is closed", because a game sitting on the
main menu writes no heartbeat either.

The capability line follows the same rule and is read from the record the
sidecar wrote, never re-derived: this command must not scan a Lua tree to answer
a question about what already happened, and a second scan here could disagree
with the one the running sidecar is actually gated on. The planner line is read
the same way and for the same reason — the record says which provider a running
loop is on and whether it fell back to the deterministic path, and re-resolving
the provider here could answer differently from the sidecar that is running.

The backup line is read rather than recorded, because unlike the other two it is
a fact about *now*: which backups exist, and whether one of them names the save
the mod is reporting this second. It is three states and never two — no backup
at all, a backup that cannot be attributed to this save, and an attributed one
with its id and age — for the reason the capability line is three: collapsing
the middle case into either neighbour tells a user either that they have no
backup when they do, or that they are covered when nothing has shown it. It
deliberately does not re-hash anything, so it never says "verified"; that check
belongs to the sidecar that is about to rely on it and to the restore itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pz_agent_core.ipc.atomic import DocumentError, read_json_document
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.platform.backup import attributed_to
from pz_agent_core.protocol import JsonDict
from pz_agent_core.session.handshake import SessionDescriptor, SessionError
from pz_agent_core.session.heartbeat import Heartbeat, HeartbeatMonitor, Peer, PeerLiveness
from pz_agent_core.session.lock import LockError, LockInfo

from .autonomy import (
    PLANNER_FILE_NAME,
    PlannerRecord,
    observed_save,
    read_planner_record,
    workspace_backups,
)
from .context import EXIT_FAILURE, EXIT_OK, CliContext, Workspace, resolve_workspace
from .doctor import environment_facts
from .output import Printer
from .runtime import CAPABILITY_FILE_NAME, CapabilityRecord, read_capability_record


@dataclass(frozen=True, slots=True)
class PeerStatus:
    """One peer's liveness with its detail already redacted.

    The monitor formats its detail around the heartbeat file's absolute path, so
    the redaction happens here rather than at the point of printing: every
    consumer of a :class:`StatusReport` then gets the safe string, and a new one
    cannot reintroduce the leak by rendering the field directly.
    """

    peer: str
    alive: bool
    detail: str
    heartbeat: Heartbeat | None = None

    def to_dict(self) -> JsonDict:
        return {
            "peer": self.peer,
            "alive": self.alive,
            "detail": self.detail,
            "heartbeat": None if self.heartbeat is None else self.heartbeat.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class BackupStatus:
    """Which of three states this machine is in about a backup for this save.

    ``backup_id`` is set only for the third one. The first two are told apart by
    ``count``: zero backups is "there is no safety net here at all", and backups
    that exist but name a different save — or no save — is "there is one, and
    nothing has shown it covers what you are playing". ``detail`` says which of
    the several ways the middle case was reached, because they need different
    things done about them: no session attached is a backup to re-take, a save id
    that simply does not match is a save that was never backed up.
    """

    root: str
    count: int
    detail: str
    observed_save_id: str = ""
    backup_id: str = ""
    created_at: str = ""
    age_ms: int | None = None

    @property
    def attributed(self) -> bool:
        """True only when a backup here names the save the mod is reporting."""
        return bool(self.backup_id)

    def to_dict(self) -> JsonDict:
        return {
            "root": self.root,
            "count": self.count,
            "attributed": self.attributed,
            "detail": self.detail,
            "observed_save_id": self.observed_save_id,
            "backup_id": self.backup_id,
            "created_at": self.created_at,
            "age_ms": self.age_ms,
        }


@dataclass(frozen=True, slots=True)
class StatusReport:
    """Everything ``status`` read, with nothing inferred from an absence."""

    ipc_root: str
    exists: bool
    game: PeerStatus | None = None
    sidecar: PeerStatus | None = None
    session: SessionDescriptor | None = None
    session_problem: str = ""
    lock: LockInfo | None = None
    panic_stop: bool = False
    environment: JsonDict | None = None
    #: What the last sidecar resolved about the game's API, or None when none
    #: has ever recorded it here. Absent and unresolved are different answers.
    capabilities: CapabilityRecord | None = None
    #: Which planner the last sidecar assembled, or None when none has ever
    #: recorded one here. The same three-way distinction applies.
    planner: PlannerRecord | None = None
    #: What can be said right now about a backup covering the save being played.
    #: None only when there is no backup root to read, which is the same machine
    #: on which there is no game profile at all.
    backup: BackupStatus | None = None

    @property
    def attached(self) -> bool:
        """True only when a session file and a matching live game heartbeat exist."""
        if self.session is None or self.game is None or self.game.heartbeat is None:
            return False
        return self.game.alive and self.game.heartbeat.session_id == self.session.session_id

    def to_dict(self) -> JsonDict:
        return {
            "ipc_root": self.ipc_root,
            "exists": self.exists,
            "attached": self.attached,
            "panic_stop": self.panic_stop,
            "game": None if self.game is None else self.game.to_dict(),
            "sidecar": None if self.sidecar is None else self.sidecar.to_dict(),
            "session": None if self.session is None else self.session.to_dict(),
            "session_problem": self.session_problem,
            "lock": None if self.lock is None else self.lock.to_dict(),
            "environment": self.environment or {},
            "capabilities": None if self.capabilities is None else self.capabilities.to_dict(),
            "planner": None if self.planner is None else self.planner.to_dict(),
            "backup": None if self.backup is None else self.backup.to_dict(),
        }


def _peer_status(liveness: PeerLiveness, workspace: Workspace) -> PeerStatus:
    return PeerStatus(
        peer=liveness.peer.value,
        alive=liveness.alive,
        detail=workspace.redactor.text(liveness.detail),
        heartbeat=liveness.heartbeat,
    )


def game_liveness(ctx: CliContext, workspace: Workspace) -> PeerLiveness | None:
    """The game's heartbeat verdict, or ``None`` when there is no exchange directory."""
    if workspace.ipc_root is None or not workspace.ipc_root.is_dir():
        return None
    monitor = HeartbeatMonitor(IpcLayout(workspace.ipc_root), clock=ctx.clock_ms)
    return monitor.liveness(Peer.GAME)


def collect_status(ctx: CliContext, workspace: Workspace) -> StatusReport:
    """Read the exchange directory once and report what was there."""
    root = workspace.ipc_root
    # Read in both branches: the capability record lives in the state directory,
    # so a machine whose exchange directory has never existed can still show
    # that a sidecar tried and could not resolve anything.
    capabilities = read_capability_record(workspace.state_dir / CAPABILITY_FILE_NAME)
    planner = read_planner_record(workspace.state_dir / PLANNER_FILE_NAME)
    # Backups live in the state directory too, so a machine whose exchange
    # directory has never existed still has a backup answer worth printing.
    backup = collect_backup_status(ctx, workspace)
    if root is None or not root.is_dir():
        return StatusReport(
            ipc_root=workspace.redact(root),
            exists=False,
            environment=environment_facts(workspace),
            capabilities=capabilities,
            planner=planner,
            backup=backup,
        )
    layout = IpcLayout(root)
    monitor = HeartbeatMonitor(layout, clock=ctx.clock_ms)
    session, problem = _read_session(layout)
    lock = _read_lock(layout)
    return StatusReport(
        ipc_root=workspace.redact(root),
        exists=True,
        game=_peer_status(monitor.liveness(Peer.GAME), workspace),
        sidecar=_peer_status(monitor.liveness(Peer.SIDECAR), workspace),
        session=session,
        session_problem=workspace.redactor.text(problem),
        lock=lock,
        panic_stop=layout.panic_stop.is_file(),
        environment=environment_facts(workspace),
        capabilities=capabilities,
        planner=planner,
        backup=backup,
    )


def collect_backup_status(ctx: CliContext, workspace: Workspace) -> BackupStatus | None:
    """Ask, without guessing, whether a backup here covers the save being played.

    Two reads and one exact comparison: the save id the mod last published, and
    the save ids the local backups recorded when they were taken. Nothing is
    inferred from how many backups there are or from which is newest — the
    middle state exists precisely so that "there is a backup" never has to stand
    in for "there is a backup of *this*".
    """
    manager = workspace_backups(workspace, clock=ctx.now)
    if manager is None:
        return None
    now_ms = ctx.clock_ms()
    observed = observed_save(workspace.ipc_root, now_ms=now_ms)
    redact = workspace.redactor.text
    root = workspace.redact(manager.backup_root)
    try:
        records = manager.list_backups()
    except OSError as exc:
        return BackupStatus(
            root=root,
            count=0,
            detail=redact(f"the backup root could not be read ({exc.strerror or exc})"),
            observed_save_id=observed.save_id or "",
        )
    if not records:
        return BackupStatus(
            root=root,
            count=0,
            detail="nothing has been backed up here",
            observed_save_id=observed.save_id or "",
        )
    if observed.save_id is None:
        return BackupStatus(
            root=root,
            count=len(records),
            detail=redact(observed.detail),
        )
    matches = attributed_to(records, observed.save_id)
    if not matches:
        return BackupStatus(
            root=root,
            count=len(records),
            detail="no backup here records the save the mod is reporting",
            observed_save_id=observed.save_id,
        )
    newest = matches[0]
    created_at_ms = newest.created_at_ms
    return BackupStatus(
        root=root,
        count=len(records),
        detail=redact(observed.detail),
        observed_save_id=observed.save_id,
        backup_id=newest.backup_id,
        created_at=newest.created_at,
        age_ms=None if created_at_ms is None else now_ms - created_at_ms,
    )


def _read_lock(layout: IpcLayout) -> LockInfo | None:
    """Read the sidecar lock record without claiming anything.

    Deliberately not through :class:`~pz_agent_core.session.lock.SidecarLock`:
    that class exists to *take* the lock and needs a session id to do it, and
    status has no session and must not invent one.
    """
    try:
        payload = read_json_document(layout.sidecar_lock)
    except DocumentError:
        return None
    try:
        return LockInfo.from_dict(payload)
    except LockError:
        return None


def _read_session(layout: IpcLayout) -> tuple[SessionDescriptor | None, str]:
    if not layout.session.is_file():
        return None, ""
    try:
        payload: Any = json.loads(layout.session.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"session.json cannot be read ({exc})"
    if not isinstance(payload, dict):
        return None, "session.json does not hold a JSON object"
    try:
        return SessionDescriptor.from_dict(payload), ""
    except SessionError as exc:
        return None, f"session.json is malformed ({exc})"


def render_status(report: StatusReport, printer: Printer) -> None:
    """Print the human-readable form."""
    printer.heading("pz-agent status")
    printer.field("exchange directory", report.ipc_root or "(none)")
    if not report.exists:
        printer.field("state", "no exchange directory; nothing has ever connected")
        _render_capabilities(report.capabilities, printer)
        _render_planner(report.planner, printer)
        _render_backup(report.backup, printer)
        printer.line()
        printer.line("Run pz-agent doctor — it distinguishes a missing Zomboid directory")
        printer.line("from a mod that has never been installed.")
        return
    game = report.game
    if game is not None and game.heartbeat is not None:
        beat = game.heartbeat
        printer.field("game", f"{'live' if game.alive else 'stale'} — {game.detail}")
        printer.field("build", beat.build or "unknown")
        printer.field("armed", "yes" if beat.armed else "no")
        printer.field("mode", beat.mode.value if beat.mode is not None else "unknown")
        printer.field("player present", "yes" if beat.player_present else "no")
        printer.field("active action", beat.active_action_id or "none")
    else:
        printer.field("game", game.detail if game is not None else "no heartbeat")
    sidecar = report.sidecar
    printer.field("sidecar", sidecar.detail if sidecar is not None else "no heartbeat")
    if report.session is not None:
        printer.field("session", report.session.session_id)
        printer.field("session mode", report.session.mode.value)
        printer.field("generation", str(report.session.generation))
        printer.field("save", report.session.save_id or "none recorded")
    elif report.session_problem:
        printer.field("session", report.session_problem)
    else:
        printer.field("session", "none")
    if report.lock is not None:
        printer.field("sidecar lock", f"held by pid {report.lock.pid}")
    if report.panic_stop:
        printer.field("panic stop", "a panic-stop sentinel is present")
    _render_capabilities(report.capabilities, printer)
    _render_planner(report.planner, printer)
    _render_backup(report.backup, printer)
    printer.field("attached", "yes" if report.attached else "no")


def _render_capabilities(record: CapabilityRecord | None, printer: Printer) -> None:
    """The capability line, which never reads an absence as a verdict.

    Three distinct states, printed as three distinct sentences: nothing has ever
    resolved them here, a sidecar tried and could not, or a sidecar did and this
    is what it found. Collapsing the middle one into the first would tell a user
    whose game files are unreadable that they simply have not started yet, and
    collapsing it into the third would show them an empty capability list with no
    hint that the list is empty because nothing was looked at.
    """
    if record is None:
        printer.field("capabilities", "no sidecar has resolved them in this state directory")
        return
    if not record.resolved:
        printer.field("capabilities", f"NOT RESOLVED — {record.detail}")
        printer.line("    every capability-gated action is refused until this is fixed")
        return
    printer.field(
        "capabilities",
        f"{len(record.usable)} usable, {len(record.verified)} verified by a live run",
    )
    if record.verified:
        printer.field("verified", ", ".join(record.verified))
    for note in record.notes:
        printer.field("capability note", note)


def _render_planner(record: PlannerRecord | None, printer: Printer) -> None:
    """The planner line, which distinguishes "not wired" from "nothing to plan".

    A sidecar with no planner is not a quiet one: it is one that will never
    propose anything however it is armed, and that has to be legible here rather
    than only as an agent that does nothing. A provider that was configured and
    could not be built is the same class of fact pointed the other way — the
    deterministic path is answering, and the user has to be able to see that it
    is not the model they set up.
    """
    if record is None:
        printer.field("planner", "no sidecar has assembled one in this state directory")
        return
    if not record.wired:
        printer.field("planner", "NOT WIRED — this sidecar proposes nothing on its own")
        printer.field("planner detail", record.detail)
        printer.line("    arming it in any mode grants an authority nothing exercises")
        return
    if record.fell_back:
        printer.field("planner", f"{record.active} — FELL BACK from {record.configured}")
        printer.field("fallback reason", record.fallback_reason)
    else:
        printer.field("planner", f"{record.active} — {record.detail}")
    for note in record.notes:
        printer.field("planner note", note)


def _render_backup(state: BackupStatus | None, printer: Printer) -> None:
    """The backup line: none, one that is not this save's, or one that is.

    The first two both mean autonomy will ask rather than act, and they are still
    printed differently, because what the user has to do about them is different
    and because "you have no backup" is false on a machine with nine of them.
    """
    if state is None:
        printer.field("backup", "no game profile was found, so there is no backup root to read")
        return
    if state.count == 0:
        printer.field("backup", f"none in {state.root} — {state.detail}")
        printer.line("    autonomy asks rather than acts until a backup covers this save")
        return
    if not state.attributed:
        printer.field("backup", f"{state.count} here, none attributable to this save")
        printer.field("backup detail", state.detail)
        printer.line("    autonomy asks rather than acts until a backup covers this save")
        return
    printer.field("backup", f"{state.backup_id} — of the save now open ({state.observed_save_id})")
    printer.field("taken", _age(state.age_ms, state.created_at))


def _age(age_ms: int | None, created_at: str) -> str:
    """How long ago a backup was taken, or the timestamp when that is unreadable.

    A negative age is printed as what it is rather than as "0 seconds ago": a
    backup stamped in the future means the two clocks disagree, and that is
    something to see rather than to round away.
    """
    if age_ms is None:
        return f"{created_at} (its timestamp could not be read as an instant)"
    if age_ms < 0:
        return f"{created_at}, which is {-age_ms // 1000} s in the future by this clock"
    seconds = age_ms // 1000
    if seconds < 90:
        return f"{seconds} s ago"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h ago"
    return f"{hours // 24} days ago"


def run_status(ctx: CliContext, *, as_json: bool) -> int:
    """Handler for ``pz-agent status``."""
    workspace = resolve_workspace(ctx)
    report = collect_status(ctx, workspace)
    printer = Printer(ctx.stdout, ctx.stderr)
    if as_json:
        printer.json(report.to_dict())
    else:
        render_status(report, printer)
    # "Nothing is attached" is a normal state and reports success: the command
    # was asked what the exchange directory says, and it said so. Only a machine
    # with no Zomboid directory at all — where there was nothing to inspect —
    # is a failure, and doctor is the command that explains that one.
    return EXIT_OK if workspace.ipc_root is not None else EXIT_FAILURE
