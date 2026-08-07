"""The conversation state machine: wake, command, clarify, stop.

Synchronous on purpose. Everything here is a decision, not an IO, and keeping it
free of ``await`` is what lets the stop path be a straight line of function calls
from "the recogniser heard a word" to "the session port has been told to stop" —
with no scheduler in between deciding when that happens.

The order of checks in :meth:`VoiceSession.handle` is the specification, not an
implementation detail:

1. **Stop, unconditionally.** Before the wake gate, before the final/interim
   test, before the confidence threshold, before classification. A stop works
   from ``IDLE`` with no wake word, from a low-confidence interim transcript,
   and while a plan is running. Anything placed above it here would be a state
   from which the user cannot stop the agent by speaking.
2. Interim transcripts go no further. They are a guess, and a guess may cancel
   speech but may not start work.
3. A wake session must be open, and it expires.
4. A transcript the recogniser is unsure of produces a question, never an
   action.

What leaves this module towards the core is a :class:`~pz_agent_core.goals.
GoalKind` and a range-checked :class:`~pz_agent_core.goals.GoalParams` inside a
:class:`~pz_agent_core.goals.GoalRequest`. Both are closed vocabularies declared
in the core; the transcript itself is read by the matcher in :mod:`.intent` and
then dropped; it is never stored, never forwarded and never spoken back.

The goal channel, not the plan channel
--------------------------------------

``docs/control/DECISIONS.md`` § "The voice companion routes through
``goal.submit``, not ``plan.execute``" decided this and gave the reason:
``PlanRequest.goal`` is a ``str`` with nowhere to carry a quantity, so
"почитай двенадцать страниц" could only reach a planner as text — and text off a
microphone is the one thing that may not cross. :data:`_GOAL_KIND` is that
decision's mapping table, reproduced here because this is the module that
applies it, and checked against the core's own tables at import.

:attr:`~.messages.VoiceGoal.RESUME` is deliberately absent from that table. It
names nothing to *do*; it names something to do about work already submitted,
which is what :meth:`~pz_agent_mcp.ports.GoalPort.status` and the channel's own
activation answer. Inventing a fifth :class:`~pz_agent_core.goals.GoalKind` for
it would be a stub wearing an enum member's clothes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from pz_agent_core.goals import (
    GOAL_SPECS,
    MAX_GOAL_STEPS,
    MAX_GOAL_WALL_MS,
    MIN_GOAL_WALL_MS,
    NUMERIC_RANGES,
    PARAM_NAMES,
)
from pz_agent_core.ipc.clocks import Clock
from pz_agent_core.protocol import ActionStatus, DangerLevel, ReasonCode

from . import phrases
from .config import (
    DEFAULT_VOICE_CONFIG,
    MAX_PLAN_REAL_SECONDS,
    MAX_PLAN_STEPS,
    VoiceConfig,
)
from .events import TtsEventStream
from .intent import classify, extract_quantities, is_stop
from .messages import (
    IntentRefusal,
    OutputKind,
    VoiceGoal,
    VoiceInput,
    VoiceIntent,
    VoiceOutput,
)
from .ports import (
    GoalBudget,
    GoalKind,
    GoalParams,
    GoalPort,
    GoalRecord,
    GoalRequest,
    GoalState,
    IdFactory,
    PlanRecord,
    SessionSnapshot,
    StopReport,
    VoiceServices,
)
from .queue import UtteranceQueue
from .state import VoiceState, VoiceTurn

__all__ = ["TOPIC_CLARIFY", "TOPIC_PLAN", "TOPIC_SESSION", "TOPIC_STOP", "VoiceSession"]

#: One subject per topic. Two messages about the plan collapse; a message about
#: the plan never collapses a message about the stop.
TOPIC_STOP: Final = "stop"
TOPIC_PLAN: Final = "plan"
TOPIC_CLARIFY: Final = "clarify"
TOPIC_SESSION: Final = "session"

#: The spoken goals that name work, and the kind each becomes. Taken from the
#: table in ``docs/control/DECISIONS.md`` and held against the core's own
#: :data:`~pz_agent_core.goals.GOAL_SPECS` by :func:`_check_goal_tables`, so a
#: kind that grows a required parameter stops this module importing rather than
#: producing goals the channel will refuse one utterance at a time.
_GOAL_KIND: Final[Mapping[VoiceGoal, GoalKind]] = MappingProxyType(
    {
        VoiceGoal.EAT: GoalKind.SATISFY_HUNGER,
        VoiceGoal.DRINK: GoalKind.SATISFY_THIRST,
        VoiceGoal.READ: GoalKind.READ_FOR_BOREDOM,
    }
)

#: The spoken goals that are control verbs over a goal already submitted. Stated
#: as a set rather than left as "whatever is missing from :data:`_GOAL_KIND`", so
#: that a new member of :class:`~.messages.VoiceGoal` is an import failure and
#: not a silently unroutable word.
_CONTROL_GOALS: Final[frozenset[VoiceGoal]] = frozenset({VoiceGoal.RESUME})

#: How many *spoken* units make one of the core's, per parameter. Speech says
#: "восемьдесят процентов" and :data:`~pz_agent_core.goals.NUMERIC_RANGES` holds
#: ``satisfy_to`` as a fraction of one, so a number has to be divided before it
#: is range-checked or an ordinary request would be refused as far too large.
#:
#: :mod:`.intent` states the same fact privately for its own resolver and
#: :mod:`.phrases` states it again to read a range out loud; neither exposes it,
#: so it is restated here and pinned against ``NUMERIC_RANGES`` at import. Three
#: statements of one scale is a drift risk worth naming rather than hiding: the
#: fix is a public converter in :mod:`.intent`, which this module does not own.
_SPOKEN_PER_CORE_UNIT: Final[Mapping[str, float]] = MappingProxyType(
    {
        "target_level": 1.0,
        "satisfy_to": 100.0,
        "pages": 1.0,
    }
)


#: The one unit conversion between :class:`~.config.VoiceConfig`, which bounds a
#: spoken command in seconds, and :class:`~pz_agent_core.goals.GoalBudget`, which
#: bounds a goal in milliseconds.
_MS_PER_SECOND: Final = 1_000


def _check_goal_tables() -> None:
    """Refuse to import tables that have drifted from the core's or the config's.

    Four separate failures, each of which would otherwise surface as one badly
    handled utterance rather than as a broken build.
    """
    unroutable = set(VoiceGoal) - set(_GOAL_KIND) - _CONTROL_GOALS
    if unroutable:
        raise RuntimeError(
            f"spoken goal(s) {sorted(goal.value for goal in unroutable)} name neither a "
            "goal kind nor a control verb, so nothing can be done with them"
        )
    both = set(_GOAL_KIND) & _CONTROL_GOALS
    if both:
        raise RuntimeError(
            f"spoken goal(s) {sorted(goal.value for goal in both)} are both a goal kind "
            "and a control verb"
        )
    for goal, kind in _GOAL_KIND.items():
        required = GOAL_SPECS[kind].required
        if required:
            raise RuntimeError(
                f"{goal.value} maps to {kind.value}, which requires {sorted(required)}; "
                "no phrasing in this package supplies them"
            )
    if set(_SPOKEN_PER_CORE_UNIT) != set(NUMERIC_RANGES):
        raise RuntimeError("_SPOKEN_PER_CORE_UNIT has drifted from NUMERIC_RANGES")
    # The budget a spoken goal carries is built from VoiceConfig, so the config's
    # own ceilings have to fit inside the channel's. They do today; a config that
    # was widened past the channel would otherwise turn every spoken goal into a
    # ValueError at submission time.
    if MAX_PLAN_STEPS > MAX_GOAL_STEPS:
        raise RuntimeError(
            f"a voice config may ask for {MAX_PLAN_STEPS} steps and the goal channel "
            f"allows {MAX_GOAL_STEPS}"
        )
    if not MIN_GOAL_WALL_MS <= _MS_PER_SECOND <= MAX_GOAL_WALL_MS:
        raise RuntimeError("the shortest configurable voice budget is not a goal budget")
    if MAX_PLAN_REAL_SECONDS * _MS_PER_SECOND > MAX_GOAL_WALL_MS:
        raise RuntimeError(
            f"a voice config may ask for {MAX_PLAN_REAL_SECONDS}s of wall clock and the "
            f"goal channel allows {MAX_GOAL_WALL_MS}ms"
        )


_check_goal_tables()


@dataclass(frozen=True, slots=True)
class _Quantities:
    """Either the typed parameters a phrase named, or why it named none.

    Exactly one of the two is meaningful, and the refusal is a member of
    :class:`~.messages.IntentRefusal` with the *name* of the parameter it is
    about — never the number the user said, which is what
    :meth:`~pz_agent_core.goals.NumericRange.check` would have quoted back.
    """

    params: GoalParams = field(default_factory=GoalParams)
    refusal: IntentRefusal | None = None
    parameter: str = ""


def _typed_params(kind: GoalKind, words: tuple[str, ...]) -> _Quantities:
    """The quantities *words* named for *kind*, in the core's units, or a refusal.

    Iterated in :data:`~pz_agent_core.goals.PARAM_NAMES` order rather than in the
    order the words arrived, so two bad quantities in one sentence always produce
    the same refusal and the companion is not a coin flip.

    A parameter the kind does not accept is refused rather than dropped: silently
    ignoring "до пятого уровня" on a request to eat would serve a goal the user
    did not ask for and say nothing about the half that was discarded.
    """
    spec = GOAL_SPECS[kind]
    accepted = spec.required | spec.optional
    spoken = extract_quantities(words)
    typed: dict[str, float] = {}
    for name in PARAM_NAMES:
        said = spoken.get(name)
        if said is None:
            continue
        if name not in accepted:
            return _Quantities(refusal=IntentRefusal.PARAMETER_NOT_ACCEPTED, parameter=name)
        value = said / _SPOKEN_PER_CORE_UNIT[name]
        try:
            NUMERIC_RANGES[name].check(value, name=name)
        except ValueError:
            # The range's own message quotes the number back, which is right for
            # a traceback and wrong for a loudspeaker. Only the fact of the
            # failure survives; the sentence comes from the closed table in
            # phrases.py, built from the range and the parameter's name.
            return _Quantities(refusal=IntentRefusal.PARAMETER_OUT_OF_RANGE, parameter=name)
        typed[name] = value
    return _Quantities(
        params=GoalParams(
            target_level=int(typed["target_level"]) if "target_level" in typed else None,
            satisfy_to=typed.get("satisfy_to"),
            pages=int(typed["pages"]) if "pages" in typed else None,
        )
    )


class VoiceSession:
    """One conversation, driven one transcript at a time."""

    def __init__(
        self,
        services: VoiceServices,
        *,
        clock: Clock,
        ids: IdFactory,
        config: VoiceConfig = DEFAULT_VOICE_CONFIG,
        events: TtsEventStream | None = None,
    ) -> None:
        self._services = services
        self._clock = clock
        self._ids = ids
        self._config = config
        self._events = events if events is not None else TtsEventStream()
        self._queue = UtteranceQueue(
            clock=clock,
            events=self._events,
            max_pending=config.max_pending_utterances,
        )
        self._state = VoiceState.IDLE
        self._woken_at_ms: int | None = None
        self._pending_goals: tuple[VoiceGoal, ...] = ()
        self._clarifications = 0
        self._plan_active = False
        self._last_plan: tuple[str, ActionStatus] | None = None
        self._last_goal: tuple[str, GoalState] | None = None
        self._takeover_reported = False
        self._revision = 0
        self._status_failure = ""

    # -- observable state --------------------------------------------------

    @property
    def queue(self) -> UtteranceQueue:
        return self._queue

    @property
    def events(self) -> TtsEventStream:
        return self._events

    @property
    def state(self) -> VoiceState:
        return self._state

    @property
    def plan_active(self) -> bool:
        """True while work the companion started has not reached a terminal state.

        Set from the goal the channel admitted — a goal is open until the channel
        says otherwise — and updated again by :meth:`report_plan` for the plans a
        sidecar runs to serve it. The name is the one the driver and the HUD
        already read; what it tracks is now the goal as well as the plan.
        """
        return self._plan_active

    @property
    def pending_question(self) -> tuple[VoiceGoal, ...]:
        """The alternatives the companion is waiting for the user to choose between."""
        return self._pending_goals

    # -- the one entry point for a transcript ------------------------------

    def handle(self, raw: VoiceInput) -> VoiceTurn:
        """Process one transcript and report what it caused."""
        if is_stop(raw):
            return self.stop(raw.at_ms)

        if not raw.final:
            return VoiceTurn(
                intent=VoiceIntent.PARTIAL,
                state=self._state,
                interrupt_speech=True,
                detail="interim transcript; only a stop acts on a guess",
            )

        self._expire_wake(raw.at_ms)
        match = classify(raw, wake_words=self._config.wake_words)

        if self._pending_goals:
            return self._answer(raw, match.goals, match.intent)

        if not self._open_session(raw, woke=match.woke):
            return VoiceTurn(
                intent=VoiceIntent.UNKNOWN,
                state=self._state,
                detail="no wake session; transcript ignored",
            )

        # Checked after the wake gate, not before it: a companion that says "не
        # расслышал" to every low-confidence fragment of a conversation it was
        # never part of is the chatter this package is built to avoid.
        if raw.confidence < self._config.min_confidence:
            return self._ask_to_repeat(raw)

        if match.intent is VoiceIntent.AMBIGUOUS:
            return self._ask_which(raw, match.goals)
        goal = match.goal
        if goal is not None:
            return self._start_goal(raw, goal)
        if match.intent is VoiceIntent.STATUS:
            return self._say_status(raw)
        if match.intent is VoiceIntent.WAKE:
            return VoiceTurn(
                intent=VoiceIntent.WAKE, state=self._state, detail="wake session opened"
            )
        if match.intent in (VoiceIntent.AFFIRM, VoiceIntent.DENY):
            # Nothing was asked, so nothing is being answered. Saying "не понял"
            # to every stray "да" in the room is the chatter the blueprint bans.
            return VoiceTurn(
                intent=match.intent, state=self._state, detail="no question is pending"
            )
        return VoiceTurn(
            intent=VoiceIntent.UNKNOWN,
            state=self._state,
            utterances=(self._say(OutputKind.QUESTION, TOPIC_CLARIFY, phrases.NOT_UNDERSTOOD),),
            detail="no intent matched",
        )

    # -- stop --------------------------------------------------------------

    def stop(self, at_ms: int | None = None) -> VoiceTurn:
        """Pull the stop lever and reset the conversation.

        Callable directly — a panic hotkey and a stop word must reach the same
        code — and callable from any state. Nothing pending survives it: the
        queue is emptied before the port is touched, so a confirmation that was
        waiting to be spoken cannot be heard *after* the stop it predates.

        The acknowledgement is spoken only once the port has answered (§ 5.17
        step 6). If the port raises, the failure is what gets said; claiming
        "остановился" without an ack would be exactly the fabricated success the
        engine's honesty rule exists to prevent.
        """
        moment = self._clock() if at_ms is None else at_ms
        self._queue.clear()
        self._state = VoiceState.IDLE
        self._woken_at_ms = None
        self._pending_goals = ()
        self._clarifications = 0
        self._plan_active = False
        self._last_plan = None
        self._last_goal = None
        self._takeover_reported = False

        try:
            report: StopReport = self._services.session.stop()
        except Exception as exc:
            utterance = self._say(OutputKind.ERROR, TOPIC_STOP, phrases.STOP_FAILED, at_ms=moment)
            return VoiceTurn(
                intent=VoiceIntent.STOP,
                state=self._state,
                utterances=(utterance,),
                interrupt_speech=True,
                detail=f"stop port raised {type(exc).__name__}: {exc}",
            )

        utterance = self._say(OutputKind.STOP, TOPIC_STOP, phrases.STOP_ACK, at_ms=moment)
        return VoiceTurn(
            intent=VoiceIntent.STOP,
            state=self._state,
            utterances=(utterance,),
            stop=report,
            interrupt_speech=True,
            detail=f"cleared={report.cleared} disarmed={report.disarmed}",
        )

    # -- reports pushed in from the sidecar --------------------------------

    def report_plan(self, record: PlanRecord) -> VoiceOutput | None:
        """Say something about *record*, but only if it is news.

        Returns None — and enqueues nothing — when the plan is in the same
        status it was last reported in. This is the whole of the "never speak
        every tick" rule: the sidecar may call this at whatever rate it observes
        plans at, and the user hears one sentence per actual transition.
        """
        signature = (record.plan_id, record.status)
        if signature == self._last_plan:
            return None
        self._last_plan = signature
        self._plan_active = not record.status.is_terminal
        if not record.status.is_terminal:
            return None
        if record.status is ActionStatus.SUCCEEDED:
            return self._say(OutputKind.CONFIRMATION, TOPIC_PLAN, phrases.PLAN_DONE)
        return self._say(OutputKind.ERROR, TOPIC_PLAN, phrases.refusal(record.stopped_reason))

    def report_goal(self, record: GoalRecord) -> VoiceOutput | None:
        """Say something about *record*, but only if it is news.

        The goal-channel counterpart of :meth:`report_plan`, and it exists
        because without it a spoken goal could be submitted and never reported.
        The companion used to submit a ``PlanRequest``, so ``report_plan`` was
        the whole progress path; goals travel on their own channel now
        (``docs/control/DECISIONS.md``) and a ``GoalRecord`` is not a
        ``PlanRecord``. Translating one into the other at the boundary was the
        other option and it is worse: two records with different terminal
        vocabularies — a goal can be ``EXPIRED`` or ``EXHAUSTED`` and a plan
        cannot — would have had to collapse onto ``ActionStatus``, and the
        distinction the user needs ("it ran out of time" versus "it failed")
        would be the one lost.

        Same "never speak every tick" rule as :meth:`report_plan`: the caller
        may poll at whatever rate it observes the channel at, and the user hears
        one sentence per actual transition.
        """
        signature = (record.goal_id, record.state)
        if signature == self._last_goal:
            return None
        self._last_goal = signature
        self._plan_active = record.is_open
        if record.is_open:
            return None
        if record.state is GoalState.SUCCEEDED:
            return self._say(OutputKind.CONFIRMATION, TOPIC_PLAN, phrases.PLAN_DONE)
        # Every other terminal state — failed, cancelled, expired, exhausted —
        # is a refusal the user can act on, and the reason code is what names
        # which. A goal that ended with no code still gets a sentence rather
        # than silence: not knowing why is not a reason to say nothing.
        return self._say(
            OutputKind.ERROR,
            TOPIC_PLAN,
            phrases.refusal(record.reason_code) if record.reason_code else phrases.PLAN_REFUSED,
        )

    def report_manual_takeover(self) -> VoiceOutput | None:
        """Announce that the player took control, once per takeover."""
        if self._takeover_reported:
            return None
        self._takeover_reported = True
        self._plan_active = False
        return self._say(OutputKind.STATUS, TOPIC_SESSION, phrases.TAKEOVER)

    def clear_manual_takeover(self) -> None:
        """Re-arm the takeover announcement after control came back."""
        self._takeover_reported = False

    # -- internals ---------------------------------------------------------

    def _expire_wake(self, at_ms: int) -> None:
        if self._woken_at_ms is None:
            return
        if at_ms - self._woken_at_ms <= self._config.wake_ttl_ms:
            return
        self._state = VoiceState.IDLE
        self._woken_at_ms = None
        self._pending_goals = ()
        self._clarifications = 0

    def _open_session(self, raw: VoiceInput, *, woke: bool) -> bool:
        """True when this transcript may be acted on.

        A wake word both opens the session and is stripped of significance: the
        rest of the same sentence is classified normally, so "агент, поешь" is
        one turn rather than two.
        """
        if self._state is not VoiceState.IDLE or not self._config.require_wake_word:
            self._state = VoiceState.LISTENING
            self._woken_at_ms = raw.at_ms
            return True
        if not woke:
            return False
        self._state = VoiceState.LISTENING
        self._woken_at_ms = raw.at_ms
        return True

    def _snapshot(self) -> SessionSnapshot | None:
        """The session's own view of itself, or None when it cannot be read.

        :meth:`~pz_agent_mcp.ports.SessionPort.status` is documented to answer
        even when the game is gone, so a port that raises here is a broken port
        rather than a disconnected game. It is caught all the same: an exception
        escaping :meth:`handle` would end the driver's transcript loop, and the
        transcript the user says next is the one that has to reach the stop.
        """
        try:
            return self._services.session.status()
        except Exception as exc:
            self._status_failure = f"{type(exc).__name__}: {exc}"
            return None

    def _refused_goal(
        self, raw: VoiceInput, goal: VoiceGoal, text: str, *, detail: str
    ) -> VoiceTurn:
        """One shape for every way a spoken goal does not become a submission.

        Always an utterance, never silence: a companion that heard a command,
        did nothing about it and said nothing about that is indistinguishable
        from one that is not listening. *text* comes from :mod:`.phrases`, which
        is a closed table, so nothing the recogniser produced can reach it.
        """
        return VoiceTurn(
            intent=VoiceIntent.GOAL,
            state=self._state,
            goal=goal,
            utterances=(self._say(OutputKind.ERROR, TOPIC_PLAN, text, at_ms=raw.at_ms),),
            detail=detail,
        )

    def _budget(self, kind: GoalKind) -> GoalBudget:
        """The bounds this companion's config puts on one spoken goal.

        Wall clock and steps are the user's own wiring — a spoken command is
        allowed to run for as long as :class:`~.config.VoiceConfig` says and no
        longer — and the pending time to live is the kind's, because speech has
        no phrasing for "how long may this wait before it starts" and inventing
        one here would be a number nobody chose. Sending all three rather than
        omitting the budget is what keeps a config that was tightened from being
        quietly ignored by the channel's defaults.
        """
        return GoalBudget(
            max_wall_ms=self._config.plan_max_real_seconds * _MS_PER_SECOND,
            max_steps=self._config.plan_max_steps,
            pending_ttl_ms=GOAL_SPECS[kind].budget.pending_ttl_ms,
        )

    def _start_goal(self, raw: VoiceInput, goal: VoiceGoal) -> VoiceTurn:
        snapshot = self._snapshot()
        if snapshot is None:
            # Not knowing the state is not the same as knowing it is fine: the
            # honest answer is the one that says nothing was started.
            return self._refused_goal(
                raw,
                goal,
                phrases.NOT_CONNECTED,
                detail=f"session port raised {self._status_failure}",
            )
        if not snapshot.connected:
            return self._refused_goal(
                raw, goal, phrases.NOT_CONNECTED, detail="the game is not connected"
            )
        if not snapshot.armed:
            return self._refused_goal(
                raw, goal, phrases.NOT_ARMED, detail="the session is disarmed"
            )

        goals = self._services.goals
        if goals is None:
            return self._refused_goal(
                raw,
                goal,
                phrases.refusal(ReasonCode.CAPABILITY_UNAVAILABLE),
                detail="this companion was assembled without a goal channel",
            )
        if goal in _CONTROL_GOALS:
            return self._resume(raw, goals)

        kind = _GOAL_KIND[goal]
        quantities = _typed_params(kind, raw.words())
        if quantities.refusal is not None:
            # Refused before the wire is touched. The number was the user's and
            # is not repeated; the sentence names the parameter and its declared
            # range, both of which this process minted.
            return self._refused_goal(
                raw,
                goal,
                phrases.intent_refusal(quantities.refusal, parameter=quantities.parameter),
                detail=(
                    f"{quantities.parameter} refused as {quantities.refusal.value}; "
                    "nothing was submitted"
                ),
            )

        try:
            admission = goals.submit(
                GoalRequest(
                    kind=kind,
                    idempotency_key=self._ids(),
                    params=quantities.params,
                    budget=self._budget(kind),
                )
            )
        except Exception as exc:
            # Bounded by the port's own deadline, so a wedged core arrives here
            # as an exception rather than as a turn that never returns.
            return self._refused_goal(
                raw,
                goal,
                phrases.PLAN_REFUSED,
                detail=f"goal channel raised {type(exc).__name__}: {exc}",
            )

        refusal = admission.refusal
        if refusal is not None:
            named = f" (active goal {refusal.active_goal_id})" if refusal.active_goal_id else ""
            return self._refused_goal(
                raw,
                goal,
                phrases.refusal(refusal.reason_code),
                detail=f"the goal channel refused with {refusal.reason_code.value}{named}",
            )
        record = admission.goal
        if record is None:
            # An admission carries exactly one of a goal and a refusal, so this
            # is unreachable. It is answered rather than asserted because an
            # exception escaping handle() would end the driver's transcript loop,
            # and the transcript the user says next is the one that has to reach
            # the stop.
            return self._refused_goal(
                raw,
                goal,
                phrases.PLAN_REFUSED,
                detail="the goal channel answered with neither a goal nor a refusal",
            )

        self._plan_active = record.is_open
        # The agent is acting again, so the next time the player takes over,
        # that is news. Without this the announcement fires once per process:
        # § 5.16 ends with the user saying "продолжай", which arrives here as
        # VoiceGoal.RESUME, and nothing else re-arms it.
        self._takeover_reported = False
        utterance = self._say(
            OutputKind.CONFIRMATION, TOPIC_PLAN, phrases.KIND_ACCEPTED[kind], at_ms=raw.at_ms
        )
        again = " (already submitted)" if admission.duplicate else ""
        return VoiceTurn(
            intent=VoiceIntent.GOAL,
            state=self._state,
            goal=goal,
            utterances=(utterance,),
            detail=f"goal {record.goal_id} is {kind.value} in state {record.state.value}{again}",
        )

    def _resume(self, raw: VoiceInput, goals: GoalPort) -> VoiceTurn:
        """Answer "продолжай" from the channel rather than by submitting anything.

        Resume names no work. It is a statement about work already submitted, so
        it is served by reading the channel: a goal that is active is already
        continuing, and a goal that is pending is one the channel's own
        activation will start. Both are things to confirm; neither is a reason to
        submit a second goal, which is what a fifth
        :class:`~pz_agent_core.goals.GoalKind` would have made this do.
        """
        try:
            status = goals.status()
        except Exception as exc:
            return self._refused_goal(
                raw,
                VoiceGoal.RESUME,
                phrases.PLAN_REFUSED,
                detail=f"goal channel raised {type(exc).__name__}: {exc}",
            )
        open_goal = status.active if status.active is not None else next(iter(status.pending), None)
        if open_goal is None:
            # There is nothing to continue, and saying "продолжаю" here would be
            # the fabricated success the engine's honesty rule exists to prevent.
            return self._refused_goal(
                raw,
                VoiceGoal.RESUME,
                phrases.PLAN_REFUSED,
                detail="the goal channel holds no open goal, so there is nothing to continue",
            )
        self._plan_active = open_goal.is_open
        self._takeover_reported = False
        utterance = self._say(
            OutputKind.CONFIRMATION,
            TOPIC_PLAN,
            phrases.GOAL_ACCEPTED[VoiceGoal.RESUME],
            at_ms=raw.at_ms,
        )
        return VoiceTurn(
            intent=VoiceIntent.GOAL,
            state=self._state,
            goal=VoiceGoal.RESUME,
            utterances=(utterance,),
            detail=(
                f"continuing goal {open_goal.goal_id}, which is {open_goal.kind.value} "
                f"in state {open_goal.state.value}"
            ),
        )

    def _ask_which(self, raw: VoiceInput, goals: tuple[VoiceGoal, ...]) -> VoiceTurn:
        """Ask which of two goals was meant instead of picking one."""
        first, second = goals[0], goals[1]
        self._pending_goals = goals
        self._clarifications = 1
        self._state = VoiceState.AWAITING_ANSWER
        utterance = self._say(
            OutputKind.QUESTION,
            TOPIC_CLARIFY,
            phrases.clarify_between(first, second),
            at_ms=raw.at_ms,
        )
        return VoiceTurn(
            intent=VoiceIntent.AMBIGUOUS,
            state=self._state,
            utterances=(utterance,),
            detail=f"ambiguous between {', '.join(goal.value for goal in goals)}",
        )

    def _ask_to_repeat(self, raw: VoiceInput) -> VoiceTurn:
        """A transcript the recogniser is unsure of is a question, not a command."""
        utterance = self._say(
            OutputKind.QUESTION, TOPIC_CLARIFY, phrases.CLARIFY_REPEAT, at_ms=raw.at_ms
        )
        return VoiceTurn(
            intent=VoiceIntent.AMBIGUOUS,
            state=self._state,
            utterances=(utterance,),
            detail=f"confidence {raw.confidence} below {self._config.min_confidence}",
        )

    def _answer(
        self, raw: VoiceInput, goals: tuple[VoiceGoal, ...], intent: VoiceIntent
    ) -> VoiceTurn:
        """Resolve an outstanding clarification, or give up on it."""
        if raw.confidence < self._config.min_confidence:
            # Having asked a question does not lower the bar for the answer. The
            # confidence gate is checked here as well as in handle() because an
            # outstanding clarification is precisely the state in which a
            # half-heard word looks most like a decision.
            return self._reask(
                raw,
                phrases.CLARIFY_REPEAT,
                detail=f"confidence {raw.confidence} below {self._config.min_confidence}",
            )

        chosen = [goal for goal in goals if goal in self._pending_goals]
        if len(chosen) == 1:
            self._forget_question(raw.at_ms)
            return self._start_goal(raw, chosen[0])

        if intent is VoiceIntent.DENY:
            self._forget_question(raw.at_ms)
            return VoiceTurn(
                intent=VoiceIntent.DENY, state=self._state, detail="clarification declined"
            )

        pending = self._pending_goals
        return self._reask(
            raw,
            phrases.clarify_between(pending[0], pending[1]),
            detail=f"clarification {self._clarifications + 1} of {self._config.max_clarifications}",
        )

    def _forget_question(self, at_ms: int) -> None:
        """Drop the outstanding clarification and stay awake for what follows."""
        self._pending_goals = ()
        self._clarifications = 0
        self._state = VoiceState.LISTENING
        self._woken_at_ms = at_ms

    def _reask(self, raw: VoiceInput, text: str, *, detail: str) -> VoiceTurn:
        """Ask *text* once more, or give the question up if the budget is spent.

        Every route back to the user runs through here, so an answer that cannot
        be used — unrecognised, misheard, or naming neither option — costs the
        same bounded number of attempts. Anything that reasked without spending
        the budget would be an unbounded loop with a microphone in it.
        """
        if self._clarifications > self._config.max_clarifications:
            self._forget_question(raw.at_ms)
            return VoiceTurn(
                intent=VoiceIntent.UNKNOWN,
                state=self._state,
                utterances=(
                    self._say(
                        OutputKind.QUESTION, TOPIC_CLARIFY, phrases.NOT_UNDERSTOOD, at_ms=raw.at_ms
                    ),
                ),
                detail="clarification budget spent; the question is dropped",
            )

        self._clarifications += 1
        utterance = self._say(OutputKind.QUESTION, TOPIC_CLARIFY, text, at_ms=raw.at_ms)
        return VoiceTurn(
            intent=VoiceIntent.AMBIGUOUS,
            state=self._state,
            utterances=(utterance,),
            detail=detail,
        )

    def _say_status(self, raw: VoiceInput) -> VoiceTurn:
        snapshot = self._snapshot()
        if snapshot is None:
            utterance = self._say(
                OutputKind.ERROR, TOPIC_SESSION, phrases.NOT_CONNECTED, at_ms=raw.at_ms
            )
            return VoiceTurn(
                intent=VoiceIntent.STATUS,
                state=self._state,
                utterances=(utterance,),
                detail=f"session port raised {self._status_failure}",
            )
        danger = snapshot.danger_level if snapshot.connected else DangerLevel.NONE
        utterance = self._say(
            OutputKind.STATUS,
            TOPIC_SESSION,
            phrases.status_line(armed=snapshot.armed, connected=snapshot.connected, danger=danger),
            at_ms=raw.at_ms,
        )
        return VoiceTurn(
            intent=VoiceIntent.STATUS,
            state=self._state,
            utterances=(utterance,),
            detail="status requested",
        )

    def _say(
        self, kind: OutputKind, topic: str, text: str, *, at_ms: int | None = None
    ) -> VoiceOutput:
        """Build an utterance, offer it to the queue, and return it.

        The revision is a session-wide counter rather than anything derived from
        the subject, so a later message about a topic always outranks an earlier
        one and the queue's staleness check can never reject fresh news.
        """
        self._revision += 1
        utterance = VoiceOutput(
            kind=kind,
            topic=topic,
            text=text,
            at_ms=self._clock() if at_ms is None else at_ms,
            revision=self._revision,
        )
        self._queue.push(utterance)
        return utterance
