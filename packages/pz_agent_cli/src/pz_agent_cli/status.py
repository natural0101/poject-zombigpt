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

The memory line is a record for the third time, and the state it exists to make
visible is "wired but nothing loaded yet": an agent that honours no reservation
because none has been read is indistinguishable from one that honours them and
has none, right up until it eats something.

The voice line is the record *and* the setting, kept apart on purpose. Nothing
starts the companion but the user — no sidecar spawns it — so "configured" and
"listening" are separate facts, and a machine where they disagree is exactly the
one whose owner is talking to a microphone nothing is reading. It also says
where a listening companion's goals go, and reads that off the record rather
than describing today's wiring over the top of it: a goal travels to a *second*
process over the Core RPC link, so "something is listening" and "a goal it hears
can land" are two facts and not one, and a record left by the build that had no
goal route at all is still reported as what it was.

``--watch`` is the same reading on a timer, printed short. It adds exactly one
fact the one-shot report cannot carry — what the goal channel holds — and it
adds it over the Core RPC link, which is a *second process* answering rather
than a file being read. That makes the goal block three-state for the reason
every other block here is: no answer at all, an answer, and the middle case of
a channel this process could not reach. "Unreachable" is never rendered as "no
goals", because a user watching an empty queue that is actually a dead link
would be watching an agent they think is idle and is in fact unreachable.

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
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, TextIO

from pz_agent_core.goals import GoalRecord
from pz_agent_core.ipc.atomic import DocumentError, read_json_document
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.platform.backup import attributed_to
from pz_agent_core.protocol import JsonDict
from pz_agent_core.session.handshake import SessionDescriptor, SessionError
from pz_agent_core.session.heartbeat import Heartbeat, HeartbeatMonitor, Peer, PeerLiveness
from pz_agent_core.session.lock import LockError, LockInfo
from pz_agent_mcp.ports import GoalChannelStatus
from pz_agent_mcp.remote.client import CoreLink, RemoteCoreError, RemoteGoalPort

from .autonomy import (
    PLANNER_FILE_NAME,
    PlannerRecord,
    observed_save,
    read_planner_record,
    workspace_backups,
)
from .context import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, CliContext, Workspace, resolve_workspace
from .doctor import environment_facts
from .memory import MEMORY_RECORD_NAME, MemoryRecord, read_memory_record
from .output import Printer
from .runtime import CAPABILITY_FILE_NAME, CapabilityRecord, read_capability_record
from .voice import VoiceStatus, collect_voice_status

#: The fastest ``--watch`` will redraw, whatever the user asked for. Every frame
#: stats and reads the whole exchange directory, and the mod publishes its
#: heartbeat on a cadence measured in seconds — so a tighter loop buys no fresher
#: fact and spends the disk to find that out.
WATCH_MIN_INTERVAL: Final = 0.5

#: How many frames one invocation will draw before it stops on its own. A watch
#: ends when the user interrupts it, and this is the bound for the case where
#: nobody ever does: a terminal left open over a weekend. At the floor interval
#: it is most of a day, so it never truncates a watch anybody is looking at, and
#: it says why it stopped so the ending is never mistaken for a crash.
MAX_WATCH_FRAMES: Final = 100_000

#: How long one frame will wait on the goal channel. Bounded well under the
#: floor interval: a frame that blocked longer than the redraw period would make
#: the HUD's own clock a function of a link that is not answering.
GOAL_QUEUE_PROBE_SECONDS: Final = 2.0

#: How many waiting goals one frame names. The backlog is another process's
#: answer and is bounded by the queue rather than by this display, so the
#: display bounds what it prints and says how many it left out.
MAX_PENDING_SHOWN: Final = 3

#: What a field prints when the answer carried ``None``. Never "no" and never
#: "0": an older peer's codec that cannot carry progress or a takeover marker
#: answers the same ``None`` as a channel that has nothing to report, and only
#: one of those means the agent is not paused.
UNREPORTED: Final = "unreported"

#: Clear the screen, then put the cursor at the top left. Written to a terminal
#: and to nothing else — a pipe gets a separator line instead, because escape
#: bytes in a file somebody later greps are corruption with a redraw's excuse.
CLEAR_SCREEN: Final = "\x1b[2J\x1b[H"

#: What separates two frames when the destination is not a terminal.
FRAME_SEPARATOR: Final = "=" * 60


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
    #: What the last sidecar had in force about what the user set aside and
    #: where home is, or None when none has ever recorded it here.
    memory: MemoryRecord | None = None
    #: What can be said right now about a backup covering the save being played.
    #: None only when there is no backup root to read, which is the same machine
    #: on which there is no game profile at all.
    backup: BackupStatus | None = None
    #: Whether voice is configured, on which adapter, and whether a companion is
    #: listening. Never None: the configuration answers even on a machine where
    #: no companion has ever run, and "not configured" is an answer.
    voice: VoiceStatus | None = None

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
            "memory": None if self.memory is None else self.memory.to_dict(),
            "backup": None if self.backup is None else self.backup.to_dict(),
            "voice": None if self.voice is None else self.voice.to_dict(),
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
    memory = read_memory_record(workspace.state_dir / MEMORY_RECORD_NAME)
    # Backups live in the state directory too, so a machine whose exchange
    # directory has never existed still has a backup answer worth printing.
    backup = collect_backup_status(ctx, workspace)
    # Voice for the same reason twice over: the setting lives in config.toml and
    # the record in the state directory, and neither needs an exchange directory
    # to be readable — a user whose game has never run still asked for voice.
    voice = collect_voice_status(ctx, workspace)
    if root is None or not root.is_dir():
        return StatusReport(
            ipc_root=workspace.redact(root),
            exists=False,
            environment=environment_facts(workspace),
            capabilities=capabilities,
            planner=planner,
            memory=memory,
            backup=backup,
            voice=voice,
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
        memory=memory,
        backup=backup,
        voice=voice,
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
        _render_memory(report.memory, printer)
        _render_backup(report.backup, printer)
        _render_voice(report.voice, printer)
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
    _render_memory(report.memory, printer)
    _render_backup(report.backup, printer)
    _render_voice(report.voice, printer)
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


def _render_memory(record: MemoryRecord | None, printer: Printer) -> None:
    """What the sidecar has in force about reservations and the home point.

    Five states, and the first three are the ones that would otherwise look
    identical from outside — an agent that ignores a reservation, an agent that
    has not been told about the save yet, and an agent whose memory file will not
    read back all behave like an agent with nothing reserved. Only the second of
    those is harmless, and it is the only one this prints as ordinary.
    """
    if record is None:
        printer.field("memory", "no sidecar has loaded one in this state directory")
        return
    if not record.wired:
        printer.field("memory", "NOT WIRED — nothing you reserved reaches the agent")
        printer.field("memory detail", record.detail)
        printer.line("    §7.9 rests on the item's own tags alone while this says NOT WIRED")
        return
    if not record.readable:
        printer.field("memory", f"UNREADABLE — {record.detail}")
        printer.line("    every item type reads as reserved, so nothing is spent unasked")
        return
    if not record.loaded:
        printer.field("memory", "not loaded — no observation has named a save yet")
        printer.field("memory detail", record.detail)
        return
    if record.empty:
        printer.field("memory", "loaded and empty — nothing reserved, no home point")
    else:
        home = "a home point" if record.home else "no home point"
        printer.field("memory", f"loaded — {record.reservations} reservation(s) and {home}")
    printer.field("memory file", record.root)
    for note in record.notes:
        printer.field("memory note", note)


def _render_voice(state: VoiceStatus | None, printer: Printer) -> None:
    """Whether anything is listening, on what, and what it cannot do.

    The adapter printed for a running companion is the one it *constructed*, not
    the one the file names: they are the same in every ordinary case, and the
    case where they differ is the only one worth a line here.

    The goal route is printed either way. A companion with one is worth a line
    because the route runs through a *second process*, so "it is listening" and
    "a goal it hears can land" are two different facts; a companion without one
    is worth a line because an agent that hears «поешь» and does nothing looks
    exactly like one that did not hear it. Records written before the Core RPC
    link carried goals are the ones that say so, and this reads what they wrote
    rather than describing today's build over the top of them.
    """
    if state is None:
        printer.field("voice", "not read")
        return
    record = state.record
    if record is not None and record.running:
        started = f"pid {record.pid}" if record.pid else "pid not recorded"
        printer.field("voice", f"{record.adapter} — LISTENING ({started})")
        if record.adapter != state.adapter:
            printer.field(
                "voice detail",
                f"config.toml names {state.adapter}; the running companion is on {record.adapter}",
            )
        if record.goals_routed:
            printer.line(
                "    a spoken goal is submitted to the sidecar over the Core RPC link, so it "
                "needs one running; a spoken stop does not"
            )
        else:
            printer.line(
                "    this companion was started without a goal route: a spoken stop reaches "
                "the game, a spoken goal reaches no planner"
            )
        for note in record.notes:
            printer.field("voice note", note)
        return
    if not state.enabled:
        printer.field("voice", f"off — {state.detail}")
        return
    printer.field("voice", f"{state.adapter} — configured, nothing listening")
    printer.field("voice detail", state.detail)
    if record is not None:
        printer.field("last companion", record.detail)


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


def probe_goal_queue(
    workspace: Workspace, *, deadline: float = GOAL_QUEUE_PROBE_SECONDS
) -> GoalChannelStatus | None:
    """What the running sidecar's goal channel holds, or ``None`` for no channel.

    One read-only ``goal.status`` call over the Core RPC link, dialled exactly
    the way :func:`~pz_agent_cli.voice.probe_goal_channel` dials it and for the
    same reason: a descriptor on disk and a live pid both report a routed
    channel about a sidecar that has stopped answering, and the only reading
    that does not is the answer itself. It submits nothing and cancels nothing,
    so it is safe against a live armed session.

    ``None`` is every way of not having an answer — no descriptor, nothing
    listening, a refusal, a reply this build cannot read — and the caller
    renders all of them as unreachable rather than as an empty queue.
    """
    port = RemoteGoalPort(CoreLink(state_dir=workspace.state_dir, deadline=deadline))
    try:
        return port.status()
    except RemoteCoreError:
        # The link's own sentence — which names the file and 'pz-agent start' —
        # is discarded here on purpose: a HUD line has no room for it and a
        # watch that stopped to print it would end the display over a sidecar
        # the user is watching *for*. The diagnosis is one command away in
        # 'pz-agent goal status', which prints that sentence in full; what is
        # lost is a detail, and what is kept is the running watch.
        return None


def _writes_to_a_terminal(stream: TextIO) -> bool:
    """Whether *stream* is a terminal, with "it would not say" meaning no.

    Guessing "terminal" about a pipe puts escape bytes into a file; guessing
    "pipe" about a terminal costs a redraw that scrolls instead of repainting.
    Only one of those two is a corrupted artefact, so a stream that raises when
    asked — a detached or closed one — is treated as a pipe.
    """
    try:
        return stream.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _suspension_marker(record: GoalRecord) -> str:
    """What a waiting goal stepped aside for, or nothing at all.

    Printed only when the record carries it. "Suspended: no" would be a claim
    assembled from a default the wire may never have carried, and this display
    does not make those.
    """
    return "" if record.suspended_by is None else f" (stepped aside for {record.suspended_by})"


def _progress_line(status: GoalChannelStatus) -> str:
    """The deterministic drive's phase and counters, or the word for silence."""
    progress = status.progress
    if progress is None:
        return UNREPORTED
    counters = " ".join(f"{name}={value}" for name, value in sorted(progress.counters.items()))
    return f"{progress.phase} {counters}".strip()


def _paused_line(status: GoalChannelStatus, workspace: Workspace) -> str:
    """The takeover marker, redacted, or the word — never "no"."""
    paused = status.paused
    if paused is None:
        return UNREPORTED
    return (
        f"{paused.kind} {paused.goal_id} parked at {paused.paused_at_ms}ms "
        f"({workspace.redactor.text(paused.reason)})"
    )


def render_goal_block(
    status: GoalChannelStatus | None, printer: Printer, workspace: Workspace
) -> None:
    """The queue as the sidecar answered it, or the fact that it did not.

    Three states and never two. An unreachable channel is said to be
    unreachable, because a HUD that printed "no goals" about a link nothing
    answered would show an idle agent to a user whose agent is unreachable —
    and those two look identical only from here.
    """
    if status is None:
        printer.field("goals", "unreachable — the sidecar is not serving RPC")
        return
    active = status.active
    if active is None:
        printer.field("goals", "no goal is active")
    else:
        printer.field("goals", f"{active.kind.value} {active.goal_id}{_suspension_marker(active)}")
        printer.field("progress", _progress_line(status))
    printer.field("paused", _paused_line(status, workspace))
    pending = status.pending
    if not pending:
        printer.field("waiting", "nothing is waiting")
        return
    shown = ", ".join(
        f"{record.kind.value}{_suspension_marker(record)}" for record in pending[:MAX_PENDING_SHOWN]
    )
    unshown = len(pending) - MAX_PENDING_SHOWN
    tail = f", +{unshown} more" if unshown > 0 else ""
    printer.field("waiting", f"{len(pending)} — {shown}{tail}")


def render_watch_frame(
    report: StatusReport,
    goals: GoalChannelStatus | None,
    printer: Printer,
    workspace: Workspace,
    *,
    frame: int,
) -> None:
    """One frame: the few facts that move, and the queue behind them.

    Deliberately not :func:`render_status`. The full report is a page, and a
    page redrawn every two seconds is one a user cannot read a change out of;
    what moves while somebody watches is the liveness of the two peers, the
    arming, the session and the goals. Everything the full report prints that
    is a *setting* — the capability, planner, memory, backup and voice records —
    stays in the one-shot command, where it is read once and thought about.
    """
    printer.heading(f"pz-agent status — frame {frame}")
    game = report.game
    if game is not None and game.heartbeat is not None:
        beat = game.heartbeat
        mode = beat.mode.value if beat.mode is not None else "unknown"
        printer.field(
            "game",
            f"{'live' if game.alive else 'stale'} — build {beat.build or 'unknown'}, "
            f"armed {'yes' if beat.armed else 'no'}, mode {mode}",
        )
    else:
        printer.field("game", game.detail if game is not None else "no heartbeat")
    sidecar = report.sidecar
    printer.field("sidecar", sidecar.detail if sidecar is not None else "no heartbeat")
    if report.session is not None:
        save = report.session.save_id or "no save recorded"
        printer.field("session", f"{report.session.session_id} — {save}")
    elif report.session_problem:
        printer.field("session", report.session_problem)
    else:
        printer.field("session", "none")
    # Only when it is there: a line reading "panic stop: no" on every frame is a
    # line a watching user stops seeing, and this is the one they must not.
    if report.panic_stop:
        printer.field("panic stop", "a panic-stop sentinel is present")
    render_goal_block(goals, printer, workspace)


def _frames(count: int) -> str:
    return "1 frame" if count == 1 else f"{count} frames"


def run_status_watch(
    ctx: CliContext,
    *,
    interval: float,
    as_json: bool,
    max_frames: int | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    goal_source: Callable[[Workspace], GoalChannelStatus | None] | None = None,
) -> int:
    """Handler for ``pz-agent status --watch``.

    A live display, and three refusals before it starts. ``--json`` is refused
    rather than served per frame: a stream of documents separated by nothing is
    not a format anything parses, and a machine reader that wants this is asking
    for a poll loop it should own — its interval, its back-off and its own idea
    of when to stop. An interval of zero or less is refused because it names no
    schedule, and one under :data:`WATCH_MIN_INTERVAL` is raised to the floor
    with the note said out loud, since silently doing something other than what
    was asked is how a user concludes the flag does nothing.

    ``max_frames``, ``sleeper`` and ``goal_source`` are seams for the tests, and
    only for them: the shipped invocation passes none of the three, so what a
    test drives is this function with the real reader, the real link and the
    real loop. The bound the tests inject is *inside*
    :data:`MAX_WATCH_FRAMES`, which applies whether or not they do.

    The note and every refusal go to stderr. On a terminal the first frame
    erases the screen, so a note printed to stdout would be visible for exactly
    one interval and then gone.
    """
    printer = Printer(ctx.stdout, ctx.stderr)
    if as_json:
        printer.error(
            "--watch does not emit JSON: a redrawn screen is not a document, and a stream "
            "of them is not one either. Poll 'pz-agent status --json' on your own schedule."
        )
        return EXIT_USAGE
    if interval <= 0:
        printer.error(f"--interval must be greater than zero seconds, got {interval}")
        return EXIT_USAGE
    delay = interval
    if delay < WATCH_MIN_INTERVAL:
        delay = WATCH_MIN_INTERVAL
        printer.error(
            f"note: --interval {interval} raised to {WATCH_MIN_INTERVAL} s — a tighter loop "
            "re-reads the exchange directory without the game having published anything new"
        )
    workspace = resolve_workspace(ctx)
    if workspace.ipc_root is None:
        # The same verdict the one-shot command reports on this machine, taken
        # before the loop rather than redrawn by it: there is no directory here
        # for anything to appear in, so no amount of waiting changes the answer.
        printer.error(
            "there is no Zomboid directory here, so there is no exchange directory to watch; "
            "run 'pz-agent doctor'"
        )
        return EXIT_FAILURE
    source = probe_goal_queue if goal_source is None else goal_source
    budget = MAX_WATCH_FRAMES if max_frames is None else min(max_frames, MAX_WATCH_FRAMES)
    terminal = _writes_to_a_terminal(ctx.stdout)
    frames = 0
    try:
        while frames < budget:
            if terminal:
                # Straight to the stream rather than through the printer, which
                # only writes whole lines: a newline after the home sequence
                # would start every frame one row down from the one it painted
                # over, and the display would crawl down the screen.
                print(CLEAR_SCREEN, end="", file=ctx.stdout)
            elif frames:
                printer.line(FRAME_SEPARATOR)
            report = collect_status(ctx, workspace)
            render_watch_frame(report, source(workspace), printer, workspace, frame=frames + 1)
            frames += 1
            if frames < budget:
                sleeper(delay)
    except KeyboardInterrupt:
        # Ctrl-C is how this command is meant to end, so it is an ending and not
        # a failure: the closing line says how much was shown and the exit code
        # says the command did what it was asked.
        printer.line(f"watch ended after {_frames(frames)}")
        return EXIT_OK
    closing = f"watch ended after {_frames(frames)}"
    if max_frames is None:
        # Nothing interrupted it and no test bounded it, so this is the ceiling
        # above, and an ending nobody asked for has to say that it was one.
        closing = f"{closing} — the watch ceiling; run it again to keep watching"
    printer.line(closing)
    return EXIT_OK
