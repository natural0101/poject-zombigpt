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

What leaves this module towards the planner is a member of
:class:`~.messages.VoiceGoal` inside a :class:`~pz_agent_mcp.ports.PlanRequest`.
The transcript itself is read by the matcher in :mod:`.intent` and then dropped;
it is never stored, never forwarded and never spoken back.
"""

from __future__ import annotations

from typing import Final

from pz_agent_core.ipc.clocks import Clock
from pz_agent_core.protocol import ActionStatus, DangerLevel

from . import phrases
from .config import DEFAULT_VOICE_CONFIG, VoiceConfig
from .events import TtsEventStream
from .intent import classify, is_stop
from .messages import OutputKind, VoiceGoal, VoiceInput, VoiceIntent, VoiceOutput
from .ports import IdFactory, PlanRecord, PlanRequest, StopReport, VoiceServices
from .queue import UtteranceQueue
from .state import VoiceState, VoiceTurn

__all__ = ["TOPIC_CLARIFY", "TOPIC_PLAN", "TOPIC_SESSION", "TOPIC_STOP", "VoiceSession"]

#: One subject per topic. Two messages about the plan collapse; a message about
#: the plan never collapses a message about the stop.
TOPIC_STOP: Final = "stop"
TOPIC_PLAN: Final = "plan"
TOPIC_CLARIFY: Final = "clarify"
TOPIC_SESSION: Final = "session"


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
        self._takeover_reported = False
        self._revision = 0

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
        """True while a submitted plan has not reported a terminal status."""
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

    def _start_goal(self, raw: VoiceInput, goal: VoiceGoal) -> VoiceTurn:
        snapshot = self._services.session.status()
        if not snapshot.connected:
            return VoiceTurn(
                intent=VoiceIntent.GOAL,
                state=self._state,
                goal=goal,
                utterances=(
                    self._say(OutputKind.ERROR, TOPIC_PLAN, phrases.NOT_CONNECTED, at_ms=raw.at_ms),
                ),
                detail="the game is not connected",
            )
        if not snapshot.armed:
            return VoiceTurn(
                intent=VoiceIntent.GOAL,
                state=self._state,
                goal=goal,
                utterances=(
                    self._say(OutputKind.ERROR, TOPIC_PLAN, phrases.NOT_ARMED, at_ms=raw.at_ms),
                ),
                detail="the session is disarmed",
            )

        request = PlanRequest(
            goal=goal.value,
            mode=snapshot.mode,
            max_steps=self._config.plan_max_steps,
            max_real_seconds=self._config.plan_max_real_seconds,
            idempotency_key=self._ids(),
        )
        try:
            record = self._services.plans.execute(request)
        except Exception as exc:
            return VoiceTurn(
                intent=VoiceIntent.GOAL,
                state=self._state,
                goal=goal,
                utterances=(
                    self._say(OutputKind.ERROR, TOPIC_PLAN, phrases.PLAN_REFUSED, at_ms=raw.at_ms),
                ),
                detail=f"plan port raised {type(exc).__name__}: {exc}",
            )

        self._plan_active = not record.status.is_terminal
        self._last_plan = (record.plan_id, record.status)
        utterance = self._say(
            OutputKind.CONFIRMATION, TOPIC_PLAN, phrases.GOAL_ACCEPTED[goal], at_ms=raw.at_ms
        )
        return VoiceTurn(
            intent=VoiceIntent.GOAL,
            state=self._state,
            goal=goal,
            plan=record,
            utterances=(utterance,),
            detail=f"submitted goal {goal.value}",
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
        chosen = [goal for goal in goals if goal in self._pending_goals]
        if len(chosen) == 1:
            self._pending_goals = ()
            self._clarifications = 0
            self._state = VoiceState.LISTENING
            self._woken_at_ms = raw.at_ms
            return self._start_goal(raw, chosen[0])

        if intent is VoiceIntent.DENY:
            self._pending_goals = ()
            self._clarifications = 0
            self._state = VoiceState.LISTENING
            self._woken_at_ms = raw.at_ms
            return VoiceTurn(
                intent=VoiceIntent.DENY, state=self._state, detail="clarification declined"
            )

        if self._clarifications > self._config.max_clarifications:
            self._pending_goals = ()
            self._clarifications = 0
            self._state = VoiceState.LISTENING
            self._woken_at_ms = raw.at_ms
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
        pending = self._pending_goals
        utterance = self._say(
            OutputKind.QUESTION,
            TOPIC_CLARIFY,
            phrases.clarify_between(pending[0], pending[1]),
            at_ms=raw.at_ms,
        )
        return VoiceTurn(
            intent=VoiceIntent.AMBIGUOUS,
            state=self._state,
            utterances=(utterance,),
            detail=f"clarification {self._clarifications} of {self._config.max_clarifications}",
        )

    def _say_status(self, raw: VoiceInput) -> VoiceTurn:
        snapshot = self._services.session.status()
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
