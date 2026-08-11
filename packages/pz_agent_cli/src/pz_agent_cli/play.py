"""``pz-agent play`` — one command from a cold start to an armed agent.

Sections 6 and 7 of ``docs/QUICKSTART.md`` are a sequence a user repeats every
session: start the sidecar, launch the game, load a save, check that the mod
attached, arm. Each step is its own command with its own failure text, and the
ordinary case — nothing running, the game not open yet — asks somebody to type
three of them with a wait in the middle whose length they have to judge for
themselves. ``play`` is that sequence and nothing more. It composes the handlers
that already exist rather than reaching past them, and every step ends in
something it *observed* rather than in something it asked for.

Three properties are what make composing them safe.

**Every wait is bounded twice**, by a deadline on the injected clock and by a
count of polls, for the reason :class:`~pz_agent_cli.core_services._ControlWaiter`
bounds its own: a clock that stops moving — a frozen test clock, a suspended
laptop, a machine whose time went backwards — must not be able to turn a bounded
wait into a hang.

**Arming is confirmed by the game, not by this process.** ``play`` writes the
same single-shot control request ``pz-agent arm`` writes, and then waits for the
game's own heartbeat to report ``armed=true`` in the mode that was asked for.
The loop's two-phase arm (:meth:`~pz_agent_cli.runtime.SidecarLoop.arm`) is what
grants the authority; this command only reports what the mod published about it.
A wait that runs out is a failure carrying what the heartbeat actually said —
never a success in a quieter tone of voice.

**Nothing here is a lever the other commands do not have.** ``play`` refuses in
front of a panic-stop sentinel with the remedy the arm path prints and never
clears one, never re-asks after a refusal, and never touches the game process:
launching Project Zomboid and loading a save stay the user's, which is why step
three is a wait with instructions rather than a spawn.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, TypeVar

from pz_agent_core.ipc.clocks import Clock
from pz_agent_core.protocol import SessionMode
from pz_agent_core.session.heartbeat import Heartbeat

from .config import load_config
from .context import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_USAGE,
    CliContext,
    Workspace,
    resolve_workspace,
)
from .output import Printer
from .runtime import Sleeper, system_sleep_ms
from .status import StatusReport, collect_status, game_liveness
from .supervisor import GameRunningProbe, SidecarSupervisor, SupervisorState

#: Seconds ``--wait-game`` waits when the user names no other number. Long
#: enough to cover launching the game, sitting through the main menu and loading
#: a save on a slow disk, which is what the wait is actually for.
DEFAULT_WAIT_GAME_S: Final = 300

#: The most ``--wait-game`` will accept. A ceiling rather than an opinion: an
#: unbounded wait is the one thing this command must not offer, and a user who
#: wants longer than an hour wants ``pz-agent start`` and their own patience.
MAX_WAIT_GAME_S: Final = 3600

#: How often the game wait re-reads the heartbeat. One second: the mod publishes
#: at several hertz, and a faster poll would only spend a file read to learn the
#: same thing sooner than a human can act on it.
GAME_POLL_MS: Final = 1000

#: How long the arm wait gives the game to confirm, and how often it looks.
#: Six times :data:`~pz_agent_cli.runtime.DEFAULT_ARM_CONFIRM_TIMEOUT_MS`, so the
#: loop's own two-phase deadline expires *inside* this wait and its verdict —
#: published as a control decision and visible in ``status`` — is the thing that
#: resolves the arm, rather than this command giving up first and reporting a
#: silence the loop had already explained.
ARM_CONFIRM_CEILING_MS: Final = 30_000

ARM_POLL_MS: Final = 1000

_T = TypeVar("_T")


def add_play_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``play`` command."""
    from .app import ARM_MODES  # noqa: PLC0415 — app.py imports this module while loading

    play = subparsers.add_parser(
        "play", help="start the sidecar, wait for the game, arm, and report"
    )
    play.add_argument(
        "--mode",
        choices=ARM_MODES,
        default=ARM_MODES[0],
        help="how much authority to ask for (default: assisted)",
    )
    play.add_argument(
        "--observe",
        action="store_true",
        help="stop once the game has attached; ask for no authority at all",
    )
    play.add_argument(
        "--wait-game",
        type=int,
        default=DEFAULT_WAIT_GAME_S,
        metavar="SECONDS",
        help=f"how long to wait for the game to attach (1..{MAX_WAIT_GAME_S}, default: 300)",
    )
    play.add_argument("--json", action="store_true")


@dataclass(frozen=True, slots=True)
class _Poller:
    """A wait bounded twice over: by a deadline, and by a count of attempts.

    The count is not belt and braces. The deadline is read off an injected clock,
    and this package injects clocks that a test freezes and that an operating
    system can step backwards over a suspend — either of which leaves a
    deadline-only loop waiting forever for an instant that never arrives.
    """

    clock: Clock
    sleep: Sleeper
    ceiling_ms: int
    period_ms: int

    @property
    def ceiling_s(self) -> int:
        """The ceiling as the number a message prints."""
        return self.ceiling_ms // 1000

    @property
    def polls(self) -> int:
        """Attempts this poller will make before it gives up, whatever the clock says."""
        return max(1, self.ceiling_ms // self.period_ms + 1)

    def until(self, look: Callable[[], _T | None]) -> _T | None:
        """Call *look* until it answers with something, or the bounds run out.

        The final call after the loop is deliberate: it accounts for the last
        sleep, so a condition that became true during it is seen rather than
        missed by one poll.
        """
        deadline = self.clock() + self.ceiling_ms
        for _ in range(self.polls):
            found = look()
            if found is not None:
                return found
            if self.clock() >= deadline:
                return None
            self.sleep(self.period_ms)
        return look()


@dataclass(frozen=True, slots=True)
class _Report:
    """Where this invocation's words go: one JSON document, or lines for a human.

    Progress notes go to stderr under ``--json`` rather than being dropped. A
    command that waits five minutes in silence and then prints a document is one
    a user kills; the notes are what say it is still waiting, and stdout stays a
    single parseable document either way.
    """

    printer: Printer
    as_json: bool

    def note(self, text: str) -> None:
        """One line of progress."""
        if self.as_json:
            self.printer.error(text)
        else:
            self.printer.line(text)

    def refuse(self, detail: str, *, code: int = EXIT_FAILURE) -> int:
        """Report that nothing was played, and why."""
        if self.as_json:
            self.printer.json({"played": False, "detail": detail})
        else:
            self.printer.error(detail)
        return code


def run_play(ctx: CliContext, args: argparse.Namespace, *, sleep: Sleeper = system_sleep_ms) -> int:
    """Handle a parsed ``play`` invocation.

    ``sleep`` is injected for the reason every other bounded wait in this package
    injects one: the tests drive the whole command — real parser, real dispatch,
    real handlers — and must not spend a second of wall clock per poll to do it.
    """
    # app.py imports this module while it is loading, so its helpers come in here
    # rather than at module scope, where the import would be a cycle.
    from .app import (  # noqa: PLC0415
        _render_validation,
        _sidecar_argv,
        build_supervisor,
        probe_game,
    )

    printer = Printer(ctx.stdout, ctx.stderr)
    out = _Report(printer=printer, as_json=bool(args.json))
    mode = SessionMode(str(args.mode).upper())
    wait_game_s = int(args.wait_game)
    if not 1 <= wait_game_s <= MAX_WAIT_GAME_S:
        return out.refuse(
            f"--wait-game is a number of seconds between 1 and {MAX_WAIT_GAME_S}, "
            f"and {wait_game_s} is not one. Nothing was started.",
            code=EXIT_USAGE,
        )
    workspace = resolve_workspace(ctx)
    validation = load_config(workspace.config_path)
    if not validation.ok:
        if not out.as_json:
            _render_validation(
                validation,
                workspace_path=workspace.redact(workspace.config_path),
                printer=printer,
            )
        return out.refuse(
            f"{len(validation.errors)} configuration error(s) in "
            f"{workspace.redact(workspace.config_path)}: the sidecar was not started and "
            "nothing was armed. Run pz-agent validate-config to see them."
        )
    if workspace.ipc_root is None:
        return out.refuse(
            "no Zomboid directory was found, so there is no exchange directory to attach "
            "to. Run pz-agent doctor and read PZD003."
        )
    supervisor = build_supervisor(ctx, workspace)
    refusal = _ensure_sidecar(supervisor, workspace, out, argv=_sidecar_argv(workspace))
    if refusal is not None:
        return out.refuse(refusal)
    found = _wait_for_game(
        ctx,
        workspace,
        out,
        poller=_Poller(
            clock=ctx.clock_ms,
            sleep=sleep,
            ceiling_ms=wait_game_s * 1000,
            period_ms=GAME_POLL_MS,
        ),
    )
    if isinstance(found, str):
        return out.refuse(found)
    if args.observe:
        return _summarise(out, workspace, report=found, game=probe_game(ctx, workspace))
    confirmed = _arm_and_confirm(
        ctx, workspace, out, supervisor=supervisor, report=found, mode=mode, sleep=sleep
    )
    if isinstance(confirmed, str):
        return out.refuse(confirmed)
    return _summarise(
        out, workspace, report=found, game=probe_game(ctx, workspace), armed=confirmed
    )


def _ensure_sidecar(
    supervisor: SidecarSupervisor,
    workspace: Workspace,
    out: _Report,
    *,
    argv: list[str],
) -> str | None:
    """Make sure a sidecar is running, and say which of the two ways that came about.

    Returns the refusal text when none could be started, and ``None`` when one is
    running — the one this call started, or the one that already was. Reusing a
    running sidecar rather than restarting it is not an optimisation: a second
    loop on one exchange directory interleaves the command stream, and the
    supervisor refuses it a moment later anyway.
    """
    status = supervisor.status()
    if status.state is SupervisorState.RUNNING:
        out.note(f"sidecar: already running — {workspace.redactor.text(status.detail)}")
        return None
    outcome = supervisor.start(argv)
    if not outcome.started:
        return (
            f"the sidecar could not be started: {workspace.redactor.text(outcome.detail)}. "
            f"Anything it printed on the way out is in "
            f"{workspace.redact(supervisor.spawn_log)}. Nothing was armed."
        )
    out.note(f"sidecar: {workspace.redactor.text(outcome.detail)}")
    out.note(f"sidecar log: {workspace.redact(supervisor.spawn_log)}")
    return None


def _wait_for_game(
    ctx: CliContext,
    workspace: Workspace,
    out: _Report,
    *,
    poller: _Poller,
) -> StatusReport | str:
    """Wait, bounded, for a game that has attached to this sidecar.

    The cheap probe runs first on every poll and the whole report only behind it:
    :func:`~pz_agent_cli.status.collect_status` reads the backup root, the
    capability record, the memory record and the configuration, and none of that
    changes the answer to "has the mod attached yet?".

    The instruction block is printed once, before the wait, and never inside it.
    A user who has to be told what to do needs telling at the start; the same
    three lines every second are noise they will scroll past.
    """

    def look() -> StatusReport | None:
        liveness = game_liveness(ctx, workspace)
        if liveness is None or not liveness.alive:
            return None
        report = collect_status(ctx, workspace)
        return report if report.attached else None

    already = look()
    if already is not None:
        out.note("game: already attached to this session")
        return already
    out.note("")
    out.note(f"Waiting up to {poller.ceiling_s} s for the game. In Project Zomboid:")
    out.note("  1. launch the game")
    out.note("  2. Mods -> enable PZ Agent Bridge (a mod enabled now needs a restart)")
    out.note("  3. load a SINGLEPLAYER save — multiplayer is refused at the handshake")
    out.note("docs/QUICKSTART.md section 5 is the same three steps in full.")
    out.note("")
    found = poller.until(look)
    if found is not None:
        out.note("game: attached")
        return found
    liveness = game_liveness(ctx, workspace)
    said = (
        "there is no exchange directory to read"
        if liveness is None
        else workspace.redactor.text(liveness.detail)
    )
    return (
        f"the sidecar is running and the game never appeared: after {poller.ceiling_s} s "
        f"the game heartbeat says {said}, and no session is attached. Nothing was armed. "
        "Launch Project Zomboid, load a singleplayer save with PZ Agent Bridge enabled, "
        "then run pz-agent play again."
    )


def _arm_and_confirm(
    ctx: CliContext,
    workspace: Workspace,
    out: _Report,
    *,
    supervisor: SidecarSupervisor,
    report: StatusReport,
    mode: SessionMode,
    sleep: Sleeper,
) -> Heartbeat | str:
    """Ask for authority, and wait for the game to say it was granted.

    The order of the two refusals above the request matters. A game already
    armed in the mode being asked for needs no request at all — a redundant
    single-shot file would be one more thing for the loop to consume and one
    more way for a stale request to be lying around. A panic-stop sentinel is
    checked *before* anything is written, because writing a request the loop is
    certain to refuse spends the user's wait to tell them what this process
    could already see.
    """
    # The session this run attached to. Every confirmation below is measured
    # against it, so an armed heartbeat belonging to some other session is not
    # mistaken for an answer to this request.
    attached = report.session.session_id if report.session is not None else None
    beat = report.game.heartbeat if report.game is not None else None
    if beat is not None and beat.armed and beat.mode is mode and beat.session_id == attached:
        out.note(f"arm: the game already reports armed in {mode.value}; nothing was requested")
        return beat
    if report.panic_stop:
        return (
            "a panic-stop sentinel is present; clear it in the game before arming. "
            "play does not clear it and no command does — the latch is the game's. "
            "Nothing was requested and nothing was armed."
        )
    request = supervisor.arm(mode)
    out.note(
        f"arm: asked the sidecar for {mode.value} (request {request.nonce}); waiting up to "
        f"{ARM_CONFIRM_CEILING_MS // 1000} s for the game to confirm it"
    )
    poller = _Poller(
        clock=ctx.clock_ms, sleep=sleep, ceiling_ms=ARM_CONFIRM_CEILING_MS, period_ms=ARM_POLL_MS
    )
    confirmed = poller.until(lambda: _armed_in(ctx, workspace, mode, session_id=attached))
    if confirmed is not None:
        return confirmed
    return _arm_never_confirmed(ctx, workspace, supervisor, mode, session_id=attached)


def _armed_in(
    ctx: CliContext, workspace: Workspace, mode: SessionMode, *, session_id: str | None
) -> Heartbeat | None:
    """The game's heartbeat, but only when it reports armed in *mode*, for *us*.

    The mode is compared rather than assumed. A loop that granted ``ASSISTED``
    to a request for ``AUTONOMOUS`` has not done what was asked, and reporting
    that as success is precisely the fabricated postcondition the two-phase arm
    exists to prevent.

    The session is compared for the same reason, one step further out. The mod
    publishes while the game runs, so a heartbeat left by an earlier sidecar can
    be fresh, armed and in the mode being asked for while naming a session this
    process never attached to — and crediting it would report authority nobody
    granted here. ``StatusReport.attached`` already makes this comparison to
    decide whether a game is *ours*; the confirmation had been reading the file
    without it, which is the one place the answer matters most.

    An unknown session is not a match. If the descriptor could not be read there
    is nothing to compare against, and a confirmation that cannot identify whose
    arm it is describing is not a confirmation.
    """
    if session_id is None:
        return None
    liveness = game_liveness(ctx, workspace)
    if liveness is None or not liveness.alive:
        return None
    beat = liveness.heartbeat
    if beat is None or not beat.armed or beat.mode is not mode:
        return None
    if beat.session_id != session_id:
        return None
    return beat


def _arm_never_confirmed(
    ctx: CliContext,
    workspace: Workspace,
    supervisor: SidecarSupervisor,
    mode: SessionMode,
    *,
    session_id: str | None = None,
) -> str:
    """What the game and the sidecar actually said when the confirmation did not come.

    The session mismatch is reported ahead of the armed flag because it changes
    what the flag means: "the game reports armed" beside a heartbeat naming
    another session would send a reader looking for a fault in the arm, when
    what they are seeing is somebody else's arm.
    """
    liveness = game_liveness(ctx, workspace)
    beat = None if liveness is None else liveness.heartbeat
    if beat is not None and session_id is not None and beat.session_id != session_id:
        return (
            f"the arm into {mode.value} was requested and never confirmed: the game's "
            f"heartbeat belongs to session {beat.session_id}, not to the session this "
            f"sidecar attached ({session_id}). Nothing this process did was armed. A "
            "heartbeat from an earlier session usually means the game is still running "
            "against a save that a previous sidecar attached to: stop the game, or run "
            "pz-agent stop and start again so both sides agree on one session."
        )
    if beat is None:
        said = "the game is publishing no readable heartbeat"
    else:
        if beat.armed is None:
            state = "publishes no armed flag at all"
        elif beat.armed:
            state = "reports armed"
        else:
            state = "reports not armed"
        seen = beat.mode.value if beat.mode is not None else "an unrecorded mode"
        said = f"the game {state}, in {seen}"
    status = supervisor.status()
    return (
        f"the arm into {mode.value} was requested and never confirmed within "
        f"{ARM_CONFIRM_CEILING_MS // 1000} s: {said}, and the sidecar says "
        f"{workspace.redactor.text(status.detail)}. Nothing was forced. Run pz-agent status "
        "for the loop's own reason, and pz-agent arm to ask again."
    )


def _summarise(
    out: _Report,
    workspace: Workspace,
    *,
    report: StatusReport,
    game: GameRunningProbe,
    armed: Heartbeat | None = None,
) -> int:
    """The success summary, built from what was read rather than from what was asked.

    ``armed`` is the confirming heartbeat, or ``None`` under ``--observe``. The
    mode printed is that heartbeat's in the first case and the one the game is
    reporting in the second — never the mode on the command line, which under
    ``--observe`` was never asked for and in the confirmed case is what the
    heartbeat already says.

    The three headlines are three different things that happened, and the
    difference is worth the branch: a session this command armed, a session that
    was already armed when ``--observe`` looked at it, and a session sitting
    where ``start`` leaves one. Printing the first sentence for the second would
    credit ``play`` with an authority somebody else granted.
    """
    redact = workspace.redactor.text
    observed = report.game.heartbeat if report.game is not None else None
    beat = armed if armed is not None else observed
    mode = beat.mode if beat is not None and beat.mode is not None else SessionMode.OBSERVE
    is_armed = bool(beat is not None and beat.armed)
    session_id = "" if report.session is None else report.session.session_id
    build = "" if beat is None or beat.build is None else redact(beat.build)
    if out.as_json:
        out.printer.json(
            {
                "played": True,
                "mode": mode.value,
                "session_id": session_id,
                "build": build,
                "game": game.to_dict(),
                "armed": is_armed,
            }
        )
        return EXIT_OK
    out.printer.line("")
    if armed is not None:
        out.printer.line(f"armed in {mode.value} — the game confirmed it")
    elif is_armed:
        out.printer.line(f"attached; the game was already armed in {mode.value}")
    else:
        out.printer.line("attached, and nothing was armed")
    out.printer.field("mode", mode.value)
    out.printer.field("session", session_id or "none recorded")
    out.printer.field("build", build or "unknown")
    out.printer.field("game", redact(game.detail))
    out.printer.line("")
    out.printer.line("From here:")
    out.printer.line("  pz-agent status --watch     the session and the goal queue, live")
    out.printer.line("  pz-agent goal status        what the agent is working on")
    out.printer.line("  pz-agent stop               end the session")
    return EXIT_OK
