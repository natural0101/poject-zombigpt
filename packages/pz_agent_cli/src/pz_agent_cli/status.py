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
with the one the running sidecar is actually gated on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pz_agent_core.ipc.atomic import DocumentError, read_json_document
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.protocol import JsonDict
from pz_agent_core.session.handshake import SessionDescriptor, SessionError
from pz_agent_core.session.heartbeat import Heartbeat, HeartbeatMonitor, Peer, PeerLiveness
from pz_agent_core.session.lock import LockError, LockInfo

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
    if root is None or not root.is_dir():
        return StatusReport(
            ipc_root=workspace.redact(root),
            exists=False,
            environment=environment_facts(workspace),
            capabilities=capabilities,
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
