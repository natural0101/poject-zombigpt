"""The conversation state machine.

Half of this file is about stop, because stop is the behaviour the package
exists to get right: it is exercised from every state the session can be in, and
from a transcript the session would otherwise have refused to act on at all.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from pz_agent_core.protocol import ActionStatus, DangerLevel, ReasonCode, SessionMode
from pz_agent_mcp.ports import PlanRecord
from pz_agent_voice import phrases
from pz_agent_voice.config import VoiceConfig
from pz_agent_voice.messages import OutputKind, VoiceGoal, VoiceInput, VoiceIntent
from pz_agent_voice.ports import VoiceServices
from pz_agent_voice.session import TOPIC_CLARIFY, TOPIC_PLAN, TOPIC_STOP, VoiceSession
from pz_agent_voice.state import VoiceState
from tests.fixtures.ipc_builders import BASE_TIME_MS
from tests.fixtures.mcp_doubles import Doubles
from tests.fixtures.voice_doubles import (
    HOSTILE_TRANSCRIPT,
    BlindSessionPort,
    RaisingPlanPort,
    RaisingSessionPort,
    make_session,
)


def said(
    text: str, *, at_ms: int = BASE_TIME_MS, final: bool = True, confidence: float = 1.0
) -> VoiceInput:
    return VoiceInput(transcript=text, at_ms=at_ms, final=final, confidence=confidence)


def texts(session: VoiceSession) -> list[str]:
    return [message.text for message in session.queue.pending]


# --------------------------------------------------------------------------
# stop, from every state
# --------------------------------------------------------------------------


def test_stop_works_from_idle_without_a_wake_word() -> None:
    session, doubles, _ = make_session()
    assert session.state is VoiceState.IDLE

    turn = session.handle(said("стоп"))

    assert turn.intent is VoiceIntent.STOP
    assert doubles.session.stops == 1
    assert texts(session) == [phrases.STOP_ACK]
    assert turn.interrupt_speech is True


def test_stop_works_while_listening() -> None:
    session, doubles, _ = make_session()
    session.handle(said("агент"))
    opened = session.state
    assert opened is VoiceState.LISTENING

    session.handle(said("стоп"))

    assert doubles.session.stops == 1
    assert session.state is VoiceState.IDLE


def test_stop_works_while_a_plan_is_running() -> None:
    session, doubles, _ = make_session()
    session.handle(said("агент, поешь"))
    running = session.plan_active
    assert running is True

    turn = session.handle(said("стоп"))

    assert doubles.session.stops == 1
    assert session.plan_active is False
    assert turn.stop is not None
    assert turn.stop.cleared == 2


def test_stop_works_while_a_clarification_is_outstanding() -> None:
    session, doubles, _ = make_session()
    session.handle(said("агент, поешь и попей"))
    asked = session.state
    assert asked is VoiceState.AWAITING_ANSWER

    session.handle(said("стоп"))

    assert doubles.session.stops == 1
    assert session.pending_question == ()
    assert session.state is VoiceState.IDLE


def test_stop_works_from_an_interim_low_confidence_transcript() -> None:
    # Neither gate applies to it: an interim transcript may not start work and a
    # low-confidence one may not be guessed at, but both may stop the agent.
    session, doubles, _ = make_session()

    turn = session.handle(said("стоп", final=False, confidence=0.05))

    assert turn.intent is VoiceIntent.STOP
    assert doubles.session.stops == 1


def test_stop_works_after_the_wake_session_has_expired() -> None:
    session, doubles, _ = make_session()
    session.handle(said("агент", at_ms=BASE_TIME_MS))

    session.handle(said("стоп", at_ms=BASE_TIME_MS + 10 * 60 * 1_000))

    assert doubles.session.stops == 1


def test_stop_discards_everything_that_was_waiting_to_be_said() -> None:
    session, _, _ = make_session()
    session.handle(said("агент, поешь"))
    assert texts(session) == [phrases.GOAL_ACCEPTED[VoiceGoal.EAT]]

    session.handle(said("стоп"))

    # A confirmation queued before the stop would otherwise be heard after it,
    # describing work that is no longer happening.
    assert texts(session) == [phrases.STOP_ACK]


def test_stop_is_confirmed_only_after_the_port_acknowledges() -> None:
    doubles = Doubles()
    broken = RaisingSessionPort(doubles.session)
    session, _, _ = make_session(
        doubles, services=VoiceServices(session=broken, plans=doubles.plans)
    )

    turn = session.handle(said("стоп"))

    assert broken.stops == 1
    assert texts(session) == [phrases.STOP_FAILED]
    assert phrases.STOP_ACK not in texts(session)
    assert turn.stop is None
    assert turn.utterances[0].kind is OutputKind.ERROR


def test_the_stop_utterance_outranks_everything_else_in_the_queue() -> None:
    session, _, _ = make_session()
    turn = session.stop()

    assert turn.utterances[0].kind is OutputKind.STOP
    assert turn.utterances[0].topic == TOPIC_STOP


def test_stop_is_callable_directly_for_the_panic_hotkey() -> None:
    session, doubles, _ = make_session()

    turn = session.stop()

    assert doubles.session.stops == 1
    assert turn.intent is VoiceIntent.STOP


# --------------------------------------------------------------------------
# a transcript is data, never an instruction
# --------------------------------------------------------------------------


def test_the_planner_receives_a_goal_token_never_the_transcript() -> None:
    session, doubles, _ = make_session()

    turn = session.handle(said(HOSTILE_TRANSCRIPT))

    assert turn.goal is VoiceGoal.EAT
    request = doubles.plans.requests[0]
    assert request.goal == VoiceGoal.EAT.value
    submitted = f"{request.goal}{request.mode.value}{request.idempotency_key}"
    for fragment in ("SYSTEM", "ignore previous", "secrets.txt", "AUTONOMOUS"):
        assert fragment not in submitted


def test_a_hostile_transcript_is_never_spoken_back() -> None:
    session, _, _ = make_session()

    session.handle(said(HOSTILE_TRANSCRIPT))

    for message in session.queue.pending:
        assert message.text in phrases.GOAL_ACCEPTED.values()


def test_an_injected_second_command_word_produces_a_question_not_two_plans() -> None:
    session, doubles, _ = make_session()

    # Smuggling an extra command word into the transcript buys an ambiguity, not
    # an action: the classifier reports both goals and the session asks.
    turn = session.handle(said(f"{HOSTILE_TRANSCRIPT} and read a book"))

    assert turn.intent is VoiceIntent.AMBIGUOUS
    assert doubles.plans.requests == []


def test_the_plan_request_carries_the_configured_limits_not_the_transcripts() -> None:
    session, doubles, _ = make_session(
        config=VoiceConfig(plan_max_steps=3, plan_max_real_seconds=9)
    )

    session.handle(said("агент, почитай"))

    request = doubles.plans.requests[0]
    assert request.max_steps == 3
    assert request.max_real_seconds == 9
    assert request.mode is SessionMode.ASSISTED


def test_two_goals_produce_two_distinct_idempotency_keys() -> None:
    session, doubles, _ = make_session()

    session.handle(said("агент, поешь"))
    session.handle(said("почитай"))

    keys = [request.idempotency_key for request in doubles.plans.requests]
    assert len(set(keys)) == 2


# --------------------------------------------------------------------------
# the wake session
# --------------------------------------------------------------------------


def test_a_command_without_a_wake_word_is_ignored_in_silence() -> None:
    session, doubles, _ = make_session()

    turn = session.handle(said("поешь"))

    assert doubles.plans.requests == []
    assert turn.utterances == ()
    assert session.queue.pending == ()


def test_the_wake_word_and_the_command_may_arrive_together() -> None:
    session, doubles, _ = make_session()

    turn = session.handle(said("агент, поешь"))

    assert turn.goal is VoiceGoal.EAT
    assert len(doubles.plans.requests) == 1


def test_a_wake_session_expires_and_stops_accepting_commands() -> None:
    session, doubles, _ = make_session(config=VoiceConfig(wake_ttl_ms=5_000))
    session.handle(said("агент", at_ms=BASE_TIME_MS))

    session.handle(said("поешь", at_ms=BASE_TIME_MS + 5_001))

    assert doubles.plans.requests == []
    assert session.state is VoiceState.IDLE


def test_a_wake_session_stays_open_within_its_window() -> None:
    session, doubles, _ = make_session(config=VoiceConfig(wake_ttl_ms=5_000))
    session.handle(said("агент", at_ms=BASE_TIME_MS))

    session.handle(said("поешь", at_ms=BASE_TIME_MS + 4_999))

    assert len(doubles.plans.requests) == 1


def test_wake_words_can_be_turned_off_entirely() -> None:
    session, doubles, _ = make_session(config=VoiceConfig(require_wake_word=False))

    session.handle(said("поешь"))

    assert len(doubles.plans.requests) == 1


# --------------------------------------------------------------------------
# clarification instead of guessing
# --------------------------------------------------------------------------


def test_two_goals_produce_a_question_and_no_plan() -> None:
    session, doubles, _ = make_session()

    turn = session.handle(said("агент, поешь и попей"))

    assert turn.intent is VoiceIntent.AMBIGUOUS
    assert doubles.plans.requests == []
    assert session.state is VoiceState.AWAITING_ANSWER
    assert texts(session) == [phrases.clarify_between(VoiceGoal.EAT, VoiceGoal.DRINK)]


def test_the_answer_to_a_clarification_starts_that_goal() -> None:
    session, doubles, _ = make_session()
    session.handle(said("агент, поешь и попей"))

    turn = session.handle(said("попей"))

    assert turn.goal is VoiceGoal.DRINK
    assert doubles.plans.requests[0].goal == VoiceGoal.DRINK.value
    assert session.pending_question == ()
    assert session.state is VoiceState.LISTENING


def test_answering_a_clarification_needs_no_second_wake_word() -> None:
    session, doubles, _ = make_session()
    session.handle(said("агент, поешь и почитай"))

    session.handle(said("почитай"))

    assert len(doubles.plans.requests) == 1


def test_declining_a_clarification_drops_it_without_starting_anything() -> None:
    session, doubles, _ = make_session()
    session.handle(said("агент, поешь и попей"))
    session.queue.clear()

    turn = session.handle(said("отмена"))

    assert turn.intent is VoiceIntent.DENY
    assert doubles.plans.requests == []
    assert session.pending_question == ()
    assert session.queue.pending == ()


def test_the_clarification_budget_is_bounded() -> None:
    session, doubles, _ = make_session(config=VoiceConfig(max_clarifications=1))
    session.handle(said("агент, поешь и попей"))

    first = session.handle(said("что-то невнятное"))
    second = session.handle(said("опять невнятное"))

    assert first.intent is VoiceIntent.AMBIGUOUS
    assert second.intent is VoiceIntent.UNKNOWN
    assert session.pending_question == ()
    assert doubles.plans.requests == []


def test_a_misheard_answer_to_a_clarification_is_not_treated_as_an_answer() -> None:
    # The question was asked from a confident transcript; the answer to it gets
    # no discount. Acting on a half-heard "попей" would make asking a question
    # the way around the confidence gate.
    session, doubles, _ = make_session(config=VoiceConfig(min_confidence=0.6))
    session.handle(said("агент, поешь и попей"))
    session.queue.clear()

    turn = session.handle(said("попей", confidence=0.2))

    assert turn.intent is VoiceIntent.AMBIGUOUS
    assert doubles.plans.requests == []
    assert texts(session) == [phrases.CLARIFY_REPEAT]
    assert session.pending_question == (VoiceGoal.EAT, VoiceGoal.DRINK)
    assert session.state is VoiceState.AWAITING_ANSWER


def test_misheard_answers_spend_the_same_bounded_budget() -> None:
    # Otherwise the loop is unbounded: every low-confidence answer would buy
    # another question, and the recogniser is the one supplying them.
    session, doubles, _ = make_session(config=VoiceConfig(min_confidence=0.6, max_clarifications=1))
    session.handle(said("агент, поешь и попей"))

    first = session.handle(said("попей", confidence=0.2))
    second = session.handle(said("попей", confidence=0.2))

    assert first.intent is VoiceIntent.AMBIGUOUS
    assert second.intent is VoiceIntent.UNKNOWN
    assert second.detail == "clarification budget spent; the question is dropped"
    assert session.pending_question == ()
    assert doubles.plans.requests == []


def test_a_confident_answer_after_a_misheard_one_still_works() -> None:
    session, doubles, _ = make_session(config=VoiceConfig(min_confidence=0.6))
    session.handle(said("агент, поешь и попей"))
    session.handle(said("попей", confidence=0.2))

    turn = session.handle(said("попей", confidence=0.9))

    assert turn.goal is VoiceGoal.DRINK
    assert doubles.plans.requests[0].goal == VoiceGoal.DRINK.value


def test_a_clarification_that_names_neither_option_does_not_pick_one() -> None:
    session, doubles, _ = make_session()
    session.handle(said("агент, поешь и попей"))

    session.handle(said("почитай"))

    # "Почитай" is a goal, but not one of the two on offer; guessing that the
    # user meant the nearest option is exactly what must not happen.
    assert doubles.plans.requests == []


def test_a_low_confidence_transcript_asks_to_repeat_instead_of_acting() -> None:
    session, doubles, _ = make_session(config=VoiceConfig(min_confidence=0.6))
    session.handle(said("агент"))

    turn = session.handle(said("поешь", confidence=0.4))

    assert turn.intent is VoiceIntent.AMBIGUOUS
    assert doubles.plans.requests == []
    assert texts(session) == [phrases.CLARIFY_REPEAT]


def test_low_confidence_noise_before_the_wake_word_is_silent() -> None:
    session, _, _ = make_session()

    turn = session.handle(said("бормотание", confidence=0.1))

    assert turn.utterances == ()
    assert session.queue.pending == ()


def test_speech_that_matches_nothing_gets_one_short_reply() -> None:
    session, _, _ = make_session()
    session.handle(said("агент"))

    session.handle(said("расскажи анекдот"))
    session.handle(said("ну и погода"))

    assert texts(session) == [phrases.NOT_UNDERSTOOD]


def test_a_stray_yes_with_no_question_pending_is_ignored() -> None:
    session, _, _ = make_session()
    session.handle(said("агент"))

    turn = session.handle(said("да"))

    assert turn.intent is VoiceIntent.AFFIRM
    assert session.queue.pending == ()


# --------------------------------------------------------------------------
# refusals and errors worth reporting
# --------------------------------------------------------------------------


def test_a_disconnected_game_is_reported_and_nothing_is_submitted() -> None:
    session, doubles, _ = make_session()
    doubles.session.snapshot = replace(doubles.session.snapshot, connected=False)

    session.handle(said("агент, поешь"))

    assert doubles.plans.requests == []
    assert texts(session) == [phrases.NOT_CONNECTED]


def test_a_disarmed_session_is_reported_and_nothing_is_submitted() -> None:
    session, doubles, _ = make_session()
    doubles.session.disarm()

    session.handle(said("агент, поешь"))

    assert doubles.plans.requests == []
    assert texts(session) == [phrases.NOT_ARMED]


def test_a_planner_that_raises_is_reported_not_swallowed() -> None:
    doubles = Doubles()
    plans = RaisingPlanPort()
    session, _, _ = make_session(
        doubles, services=VoiceServices(session=doubles.session, plans=plans)
    )

    turn = session.handle(said("агент, поешь"))

    assert plans.requests
    assert texts(session) == [phrases.PLAN_REFUSED]
    assert turn.plan is None
    assert "RuntimeError" in turn.detail


def test_a_session_port_that_cannot_answer_does_not_end_the_conversation() -> None:
    doubles = Doubles()
    blind = BlindSessionPort(doubles.session)
    session, _, _ = make_session(
        doubles, services=VoiceServices(session=blind, plans=doubles.plans)
    )

    turn = session.handle(said("агент, поешь"))

    # Nothing was submitted, the failure is named in the turn, and — the point
    # of catching it — the session is still able to take the next transcript.
    assert doubles.plans.requests == []
    assert texts(session) == [phrases.NOT_CONNECTED]
    assert turn.utterances[0].kind is OutputKind.ERROR
    assert "RuntimeError" in turn.detail
    assert session.handle(said("стоп")).intent is VoiceIntent.STOP
    assert doubles.session.stops == 1


def test_a_status_request_a_broken_port_cannot_answer_says_so() -> None:
    doubles = Doubles()
    blind = BlindSessionPort(doubles.session)
    session, _, _ = make_session(
        doubles, services=VoiceServices(session=blind, plans=doubles.plans)
    )

    turn = session.handle(said("агент, статус"))

    assert texts(session) == [phrases.NOT_CONNECTED]
    assert turn.utterances[0].kind is OutputKind.ERROR
    assert "RuntimeError" in turn.detail


def test_status_is_answered_from_the_session_snapshot() -> None:
    session, doubles, _ = make_session()
    doubles.session.snapshot = replace(doubles.session.snapshot, danger_level=DangerLevel.HIGH)

    session.handle(said("агент, статус"))

    assert texts(session) == [
        phrases.status_line(armed=True, connected=True, danger=DangerLevel.HIGH)
    ]


# --------------------------------------------------------------------------
# reports pushed in from the sidecar: news only
# --------------------------------------------------------------------------


def running(step: int = 0) -> PlanRecord:
    return PlanRecord(plan_id="plan-1", status=ActionStatus.PROGRESS, step_index=step)


def test_an_unchanged_plan_status_says_nothing_however_often_it_is_reported() -> None:
    session, _, _ = make_session()

    spoken = [session.report_plan(running()) for _ in range(200)]

    assert spoken.count(None) == 200
    assert session.queue.pending == ()


def test_a_terminal_plan_is_reported_exactly_once() -> None:
    session, _, _ = make_session()
    done = PlanRecord(plan_id="plan-1", status=ActionStatus.SUCCEEDED, step_index=2)

    first = session.report_plan(done)
    second = session.report_plan(done)

    assert first is not None
    assert first.text == phrases.PLAN_DONE
    assert second is None
    assert len(session.queue) == 1


def test_a_failed_plan_names_the_reason_the_user_can_act_on() -> None:
    session, _, _ = make_session()
    failed = PlanRecord(
        plan_id="plan-1",
        status=ActionStatus.FAILED,
        step_index=1,
        stopped_reason=ReasonCode.NO_SAFE_FOOD,
    )

    message = session.report_plan(failed)

    assert message is not None
    assert message.kind is OutputKind.ERROR
    assert message.text == phrases.refusal(ReasonCode.NO_SAFE_FOOD)


def test_a_terminal_plan_supersedes_the_confirmation_that_started_it() -> None:
    session, _, _ = make_session()
    session.handle(said("агент, поешь"))
    record = PlanRecord(plan_id="plan-1", status=ActionStatus.SUCCEEDED, step_index=1)

    session.report_plan(record)

    assert texts(session) == [phrases.PLAN_DONE]
    assert all(message.topic == TOPIC_PLAN for message in session.queue.pending)


def test_manual_takeover_is_announced_once_per_takeover() -> None:
    session, _, _ = make_session()

    first = session.report_manual_takeover()
    second = session.report_manual_takeover()
    session.clear_manual_takeover()
    third = session.report_manual_takeover()

    assert first is not None
    assert first.text == phrases.TAKEOVER
    assert second is None
    assert third is not None


def test_a_second_takeover_after_the_agent_resumed_is_announced_again() -> None:
    # § 5.16 ends with the user saying "продолжай". Once the agent is working
    # again the next takeover is news, and an announcement that only ever fires
    # once per process would leave the user wondering why it stopped.
    session, _, _ = make_session()
    session.handle(said("агент, поешь"))
    first = session.report_manual_takeover()

    session.handle(said("продолжай"))
    second = session.report_manual_takeover()

    assert first is not None
    assert second is not None
    assert second.text == phrases.TAKEOVER


def test_a_takeover_the_agent_did_not_resume_from_is_still_announced_once() -> None:
    session, _, _ = make_session()
    session.handle(said("агент, поешь"))

    first = session.report_manual_takeover()
    second = session.report_manual_takeover()

    assert first is not None
    assert second is None


def test_a_takeover_stops_the_session_believing_a_plan_is_running() -> None:
    session, _, _ = make_session()
    session.handle(said("агент, поешь"))

    session.report_manual_takeover()

    assert session.plan_active is False


# --------------------------------------------------------------------------
# configuration bounds
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"wake_ttl_ms": 0}, "wake_ttl_ms must be positive"),
        ({"min_confidence": 1.5}, "min_confidence must be within"),
        ({"max_pending_utterances": 0}, "max_pending_utterances must be positive"),
        ({"max_clarifications": -1}, "max_clarifications must be non-negative"),
        ({"plan_max_steps": 99}, "plan_max_steps must be within"),
        ({"plan_max_real_seconds": 0}, "plan_max_real_seconds must be within"),
        ({"wake_words": frozenset()}, "at least one wake word"),
        ({"wake_words": frozenset({"Агент"})}, "case-folded"),
    ],
)
def test_a_misconfigured_voice_loop_fails_at_construction(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        VoiceConfig(**kwargs)  # type: ignore[arg-type]


def test_the_queue_bound_comes_from_the_config() -> None:
    session, _, _ = make_session(config=VoiceConfig(max_pending_utterances=1))

    assert session.queue.max_pending == 1


def test_the_clarification_topic_is_stable_so_repeats_collapse() -> None:
    session, _, _ = make_session()
    session.handle(said("агент"))

    session.handle(said("бла бла"))
    session.handle(said("бла бла бла"))

    assert [message.topic for message in session.queue.pending] == [TOPIC_CLARIFY]
