"""Arbitration: who yields to whom, and when the arbiter stops asking."""

from __future__ import annotations

import pytest

from pz_agent_core.protocol import Priority
from pz_agent_core.safety.priority import (
    AntiLoopConfig,
    Arbitration,
    Need,
    NeedArbiter,
    RunningPlan,
)

NOW = 1_700_000_000_000


def _need(
    key: str,
    priority: Priority,
    *,
    signature: str = "sig-0",
    preemptive: bool = True,
) -> Need:
    return Need(
        key=key,
        priority=priority,
        state_signature=signature,
        message=f"{key} needs attention",
        preemptive=preemptive,
    )


def _plan(
    priority: Priority = Priority.LONG_TERM_TASK, *, interruptible: bool = True
) -> RunningPlan:
    return RunningPlan(key="build-a-wall", priority=priority, interruptible=interruptible)


# --------------------------------------------------------------------------
# the ladder
# --------------------------------------------------------------------------


def test_nothing_to_do_means_continue() -> None:
    decision = NeedArbiter().arbitrate(_plan(), [], NOW)

    assert decision.outcome is Arbitration.CONTINUE
    assert decision.serve is None
    assert decision.detail == "nothing needs attention"


def test_with_no_plan_running_the_need_is_simply_served() -> None:
    need = _need("thirst", Priority.CRITICAL_THIRST)
    decision = NeedArbiter().arbitrate(None, [need], NOW)

    assert decision.outcome is Arbitration.CONTINUE
    assert decision.serve == need


@pytest.mark.parametrize(
    "priority",
    [Priority.PANIC_STOP, Priority.MANUAL_INPUT],
)
def test_the_two_top_rungs_abort_the_running_plan(priority: Priority) -> None:
    decision = NeedArbiter().arbitrate(_plan(), [_need("stop", priority)], NOW)

    assert decision.outcome is Arbitration.ABORT
    assert decision.serve is not None


@pytest.mark.parametrize(
    "priority",
    [
        Priority.LETHAL_THREAT,
        Priority.CRITICAL_BLEEDING,
        Priority.HAZARD_TILE,
        Priority.CRITICAL_THIRST,
        Priority.CRITICAL_HUNGER,
        Priority.EXHAUSTION,
        Priority.USER_COMMAND,
    ],
)
def test_an_emergency_suspends_a_long_term_task_but_does_not_destroy_it(
    priority: Priority,
) -> None:
    decision = NeedArbiter().arbitrate(_plan(), [_need("need", priority)], NOW)

    assert decision.outcome is Arbitration.SUSPEND
    assert decision.serve is not None
    assert decision.serve.priority is priority


@pytest.mark.parametrize(
    "priority",
    [Priority.LONG_TERM_TASK, Priority.BASE_MAINTENANCE, Priority.OPTIONAL_ACTIVITY],
)
def test_an_equal_or_weaker_need_waits_its_turn(priority: Priority) -> None:
    need = _need("chore", priority)
    decision = NeedArbiter().arbitrate(_plan(Priority.LONG_TERM_TASK), [need], NOW)

    assert decision.outcome is Arbitration.CONTINUE
    assert decision.serve is None
    assert decision.deferred == (need,)


def test_a_non_preemptive_need_never_interrupts() -> None:
    need = _need("boredom", Priority.CRITICAL_THIRST, preemptive=False)
    decision = NeedArbiter().arbitrate(_plan(), [need], NOW)

    assert decision.outcome is Arbitration.CONTINUE
    assert decision.deferred == (need,)


def test_a_step_that_cannot_be_paused_is_not_suspended_mid_flight() -> None:
    decision = NeedArbiter().arbitrate(
        _plan(interruptible=False), [_need("thirst", Priority.CRITICAL_THIRST)], NOW
    )

    assert decision.outcome is Arbitration.CONTINUE
    assert "cannot be interrupted" in decision.detail


def test_a_panic_stop_beats_even_an_uninterruptible_step() -> None:
    decision = NeedArbiter().arbitrate(
        _plan(interruptible=False), [_need("panic", Priority.PANIC_STOP)], NOW
    )
    assert decision.outcome is Arbitration.ABORT


def test_the_most_urgent_need_wins_and_the_rest_are_deferred() -> None:
    thirst = _need("thirst", Priority.CRITICAL_THIRST)
    hunger = _need("hunger", Priority.CRITICAL_HUNGER)
    bleeding = _need("bleeding", Priority.CRITICAL_BLEEDING)
    decision = NeedArbiter().arbitrate(_plan(), [hunger, thirst, bleeding], NOW)

    assert decision.serve == bleeding
    assert decision.deferred == (thirst, hunger)


def test_a_duplicated_need_key_keeps_only_its_most_urgent_form() -> None:
    mild = _need("thirst", Priority.OPTIONAL_ACTIVITY)
    severe = _need("thirst", Priority.CRITICAL_THIRST)
    decision = NeedArbiter().arbitrate(_plan(), [mild, severe], NOW)

    assert decision.serve == severe
    assert decision.deferred == ()


# --------------------------------------------------------------------------
# anti-loop
# --------------------------------------------------------------------------


def test_a_repeating_need_asks_for_an_alternate_strategy_then_asks_the_user() -> None:
    arbiter = NeedArbiter()
    need = _need("thirst", Priority.CRITICAL_THIRST, signature="thirst=0.72")

    first = arbiter.arbitrate(_plan(), [need], NOW)
    assert first.outcome is Arbitration.SUSPEND
    assert not first.alternate_strategy

    second = arbiter.arbitrate(_plan(), [need], NOW + 1_000)
    assert second.outcome is Arbitration.SUSPEND
    assert second.alternate_strategy

    third = arbiter.arbitrate(_plan(), [need], NOW + 2_000)
    assert third.outcome is Arbitration.ASK_USER
    assert third.trigger_count == 3
    assert "without its state improving" in third.detail


def test_an_escalated_need_is_suppressed_instead_of_thrashing() -> None:
    arbiter = NeedArbiter()
    need = _need("thirst", Priority.CRITICAL_THIRST, signature="thirst=0.72")
    for step in range(3):
        arbiter.arbitrate(_plan(), [need], NOW + step)

    after = arbiter.arbitrate(_plan(), [need], NOW + 10_000)

    assert after.outcome is Arbitration.CONTINUE
    assert after.suppressed == (need,)
    assert after.serve is None
    assert "suppressed after escalation" in after.detail


def test_the_cooldown_ends_and_the_need_may_be_served_again() -> None:
    config = AntiLoopConfig(cooldown_ms=5_000, window_ms=1_000)
    arbiter = NeedArbiter(config)
    need = _need("thirst", Priority.CRITICAL_THIRST, signature="thirst=0.72")
    for step in range(3):
        arbiter.arbitrate(_plan(), [need], NOW + step)

    assert arbiter.arbitrate(_plan(), [need], NOW + 1_000).outcome is Arbitration.CONTINUE
    revived = arbiter.arbitrate(_plan(), [need], NOW + 6_000)

    assert revived.outcome is Arbitration.SUSPEND
    assert revived.trigger_count == 1


def test_a_need_whose_state_keeps_improving_never_escalates() -> None:
    arbiter = NeedArbiter()
    for step, thirst in enumerate((0.72, 0.61, 0.44, 0.30, 0.21)):
        decision = arbiter.arbitrate(
            _plan(),
            [_need("thirst", Priority.CRITICAL_THIRST, signature=f"thirst={thirst}")],
            NOW + step,
        )
        assert decision.outcome is Arbitration.SUSPEND
        assert decision.trigger_count == 1
        assert not decision.alternate_strategy


def test_triggers_spread_beyond_the_window_are_not_a_loop() -> None:
    arbiter = NeedArbiter(AntiLoopConfig(window_ms=10_000))
    need = _need("thirst", Priority.CRITICAL_THIRST, signature="thirst=0.72")

    for step in range(5):
        decision = arbiter.arbitrate(_plan(), [need], NOW + step * 20_000)
        assert decision.outcome is Arbitration.SUSPEND
        assert decision.trigger_count == 1


def test_only_the_need_that_is_actually_served_advances_its_counter() -> None:
    arbiter = NeedArbiter()
    bleeding = _need("bleeding", Priority.CRITICAL_BLEEDING, signature="wounds=1")
    thirst = _need("thirst", Priority.CRITICAL_THIRST, signature="thirst=0.72")

    for step in range(3):
        assert arbiter.arbitrate(_plan(), [bleeding, thirst], NOW + step).serve == bleeding

    # Bleeding escalated and is now suppressed; thirst gets its first real turn
    # with a fresh counter, having only ever been deferred until now.
    decision = arbiter.arbitrate(_plan(), [bleeding, thirst], NOW + 4)
    assert decision.serve == thirst
    assert decision.trigger_count == 1
    assert decision.suppressed == (bleeding,)


def test_a_need_that_never_wins_arbitration_never_escalates_to_the_user() -> None:
    # The need loses to the running plan on every tick, so nothing is ever tried
    # on its behalf. Escalating it would ask the user about a situation the
    # agent never once attempted to fix.
    arbiter = NeedArbiter()
    chore = _need("boredom", Priority.OPTIONAL_ACTIVITY, signature="boredom=3")

    for step in range(10):
        decision = arbiter.arbitrate(_plan(Priority.USER_COMMAND), [chore], NOW + step)
        assert decision.outcome is Arbitration.CONTINUE
        assert decision.serve is None
        assert decision.deferred == (chore,)
        assert decision.trigger_count == 0
        assert not decision.alternate_strategy

    assert arbiter.tracked == 0

    # It escalates normally once it actually starts being served.
    for _ in range(3):
        arbiter.arbitrate(None, [chore], NOW)
    assert arbiter.arbitrate(None, [chore], NOW).outcome is Arbitration.CONTINUE


def test_an_uninterruptible_step_does_not_let_a_need_escalate_behind_its_back() -> None:
    arbiter = NeedArbiter()
    thirst = _need("thirst", Priority.CRITICAL_THIRST, signature="thirst=0.72")

    for step in range(5):
        decision = arbiter.arbitrate(_plan(interruptible=False), [thirst], NOW + step)
        assert decision.outcome is Arbitration.CONTINUE
        assert decision.trigger_count == 0

    # The step finishes; the need is served for the first time, from zero.
    served = arbiter.arbitrate(_plan(), [thirst], NOW + 5)
    assert served.outcome is Arbitration.SUSPEND
    assert served.trigger_count == 1


def test_resolving_a_need_clears_its_history() -> None:
    arbiter = NeedArbiter()
    need = _need("thirst", Priority.CRITICAL_THIRST, signature="thirst=0.72")
    arbiter.arbitrate(_plan(), [need], NOW)
    arbiter.arbitrate(_plan(), [need], NOW + 1)
    arbiter.note_resolved("thirst")

    decision = arbiter.arbitrate(_plan(), [need], NOW + 2)
    assert decision.trigger_count == 1
    assert not decision.alternate_strategy
    assert arbiter.tracked == 1


def test_a_suppressed_need_reappearing_with_new_state_is_heard_again() -> None:
    arbiter = NeedArbiter()
    stuck = _need("thirst", Priority.CRITICAL_THIRST, signature="thirst=0.72")
    for step in range(3):
        arbiter.arbitrate(_plan(), [stuck], NOW + step)

    worse = _need("thirst", Priority.CRITICAL_THIRST, signature="thirst=0.91")
    decision = arbiter.arbitrate(_plan(), [worse], NOW + 4)

    assert decision.outcome is Arbitration.SUSPEND
    assert decision.trigger_count == 1


def test_the_history_is_bounded() -> None:
    arbiter = NeedArbiter(AntiLoopConfig(max_tracked=4))
    for index in range(50):
        arbiter.arbitrate(None, [_need(f"need-{index}", Priority.CRITICAL_THIRST)], NOW + index)

    assert arbiter.tracked == 4


def test_the_evicted_entries_are_the_least_recently_seen() -> None:
    # Escalation is pushed out of the way so the counter, not the brake, is
    # what this test observes.
    arbiter = NeedArbiter(AntiLoopConfig(max_tracked=2, alternate_after=50, escalate_after=100))
    keeper = _need("keeper", Priority.CRITICAL_BLEEDING, signature="s")
    for index in range(5):
        arbiter.arbitrate(None, [keeper], NOW + index * 2)
        arbiter.arbitrate(
            None, [_need(f"churn-{index}", Priority.CRITICAL_THIRST)], NOW + index * 2 + 1
        )

    assert arbiter.tracked == 2
    # The keeper survived every eviction, so its counter is still climbing.
    assert arbiter.arbitrate(None, [keeper], NOW + 20).trigger_count == 6


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window_ms": 0},
        {"cooldown_ms": -1},
        {"alternate_after": 0},
        {"alternate_after": 9, "escalate_after": 2},
        {"max_tracked": 0},
    ],
)
def test_an_incoherent_config_is_refused(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="must be"):
        AntiLoopConfig(**kwargs)  # type: ignore[arg-type]
