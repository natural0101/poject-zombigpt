"""``pz-agent status`` — what the exchange directory says right now.

Read-only, and deliberately shallow: it reports the heartbeats, the session
file, the lock and the capability revision, which is exactly the set of facts
that distinguishes "the mod is not loaded" from "no save is open" from "the
sidecar died". It never infers a state it did not read — an absent heartbeat is
reported as absent, not as "the game is closed", because a game sitting on the
main menu writes no heartbeat either.
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
    if root is None or not root.is_dir():
        return StatusReport(
            ipc_root=workspace.redact(root),
            exists=False,
            environment=environment_facts(workspace),
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
    printer.field("attached", "yes" if report.attached else "no")


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
