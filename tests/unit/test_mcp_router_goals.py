"""``pz_goal_status``'s three additive tails, as the router publishes them.

The port half of this feature is ``test_cli_core_services.py``'s subject (the
real loop and wrapper behind :class:`~pz_agent_cli.core_services.LoopGoalPort`)
and the producer half ``test_goal_progress.py``'s. Here the port is a double,
so what is under test is exactly the *translation*: how a
:class:`~pz_agent_mcp.ports.GoalProgress`, a
:class:`~pz_agent_mcp.ports.PausedGoalRecord` and a mission report leave this
boundary — additively beside the payload the tool has always answered, null
when the port had nothing to say, tokens and numbers in the open, every free
string under the same ``untrusted_text`` quarantine as any other game-adjacent
text.

The additive census matters most: the four keys the tool answered before this
feature keep their names and shapes, and the three new ones are *always
present* — a client discovers them as ``null``, never as a key that appears
only when populated, which is the difference between an additive field and a
payload whose shape depends on the world.
"""

from __future__ import annotations

import json

from pz_agent_core.goals import GoalState
from pz_agent_core.observation.compact import CONTENT_MARKER, UNTRUSTED_TEXT_KEY
from pz_agent_mcp.ports import GoalChannelStatus, GoalProgress, PausedGoalRecord
from tests.fixtures.mcp_doubles import HOSTILE_NAME
from tests.unit.test_mcp_catalog_goals import (
    EMPTY_CHANNEL,
    GOAL_ID,
    OTHER_ID,
    FakeGoalPort,
    make_goal,
    wired,
)

#: What one ``pz_goal_status`` answer carries, spelled out. A new key is a
#: deliberate edit here; a renamed or vanished one is a break this census
#: exists to catch.
STATUS_PAYLOAD_KEYS = frozenset(
    {"goal", "active", "pending", "pending_truncated", "progress", "paused", "report"}
)


# --- the additive census ----------------------------------------------------


def test_the_status_payload_is_the_censused_shape_with_the_tails_null() -> None:
    """An empty channel answers every key, the three new ones as null."""
    router, _, _ = wired(FakeGoalPort(channel=EMPTY_CHANNEL))

    data = router.call("pz_goal_status", {})["data"]

    assert set(data) == STATUS_PAYLOAD_KEYS
    assert data["progress"] is None, "no deterministic drive answered; null is the honest word"
    assert data["paused"] is None
    assert data["report"] is None


def test_a_goal_with_no_deterministic_drive_still_answers_null_progress() -> None:
    """An LLM-served active goal has no phase, and the payload says exactly that."""
    active = make_goal(goal_id=GOAL_ID, state=GoalState.ACTIVE)
    router, _, _ = wired(FakeGoalPort(channel=GoalChannelStatus(active=active)))

    data = router.call("pz_goal_status", {})["data"]

    assert data["active"]["goal_id"] == GOAL_ID
    assert data["progress"] is None
    assert data["report"] is None


# --- progress ---------------------------------------------------------------


def test_progress_crosses_as_the_drives_phase_and_counters() -> None:
    channel = GoalChannelStatus(
        active=make_goal(goal_id=GOAL_ID, state=GoalState.ACTIVE),
        progress=GoalProgress(phase="moving", counters={"legs_used": 3}),
    )
    router, _, _ = wired(FakeGoalPort(channel=channel))

    data = router.call("pz_goal_status", {})["data"]

    assert data["progress"] == {"phase": "moving", "counters": {"legs_used": 3}}


def test_progress_rides_beside_the_named_goal_too() -> None:
    channel = GoalChannelStatus(
        named=make_goal(goal_id=OTHER_ID, state=GoalState.ACTIVE),
        progress=GoalProgress(
            phase="transfer",
            counters={"containers_inspected": 2, "containers_skipped": 1},
        ),
    )
    router, goals, _ = wired(FakeGoalPort(channel=channel))

    payload = router.call("pz_goal_status", {"goal_id": OTHER_ID})

    assert goals.status_calls == [OTHER_ID]
    assert payload["data"]["goal"]["goal_id"] == OTHER_ID
    assert payload["data"]["progress"]["phase"] == "transfer"
    assert payload["data"]["progress"]["counters"] == {
        "containers_inspected": 2,
        "containers_skipped": 1,
    }


# --- paused -----------------------------------------------------------------


def test_the_paused_marker_crosses_with_its_reason_quarantined() -> None:
    """The one sentence the marker carries leaves marked, not on trust.

    The loop writes a constant today, but the field is a string and the
    boundary treats it as one: a hostile reason arrives under the quarantine
    key, redacted, and never verbatim anywhere in the payload.
    """
    parked = PausedGoalRecord(
        goal_id=GOAL_ID,
        kind="loot_area",
        reason=HOSTILE_NAME,
        paused_at_ms=1_234,
    )
    router, _, _ = wired(FakeGoalPort(channel=GoalChannelStatus(paused=parked)))

    data = router.call("pz_goal_status", {})["data"]

    paused = data["paused"]
    assert paused["goal_id"] == GOAL_ID
    assert paused["kind"] == "loot_area"
    assert paused["paused_at_ms"] == 1_234
    assert "reason" not in paused, "the sentence lives only under the quarantine key"
    assert paused[UNTRUSTED_TEXT_KEY]["reason"]
    assert paused["content_marker"] == CONTENT_MARKER
    assert HOSTILE_NAME not in json.dumps(data)
    assert "hostile" not in json.dumps(data), "the OS username must not cross this boundary"


# --- report -----------------------------------------------------------------


def test_the_missions_report_crosses_scrubbed_not_verbatim() -> None:
    """The ledger document is redacted at source and quarantined again here.

    Counts and references stay in the open; the skip reasons — assembled from
    world facts like a door reference, but strings all the same — leave under
    ``untrusted_text``, exactly as every other free string does.
    """
    crate = f"container:{GOAL_ID}:world:1232:3400:0:1:0"
    report = {
        "scope": {"scope": "room", "room": "kitchen"},
        "containers_inspected": [crate],
        "containers_skipped": [{"ref": crate, "reason": HOSTILE_NAME}],
        "items_taken": {"food": 3},
        "ended": "complete",
        "candidates_truncated": False,
    }
    channel = GoalChannelStatus(
        named=make_goal(goal_id=GOAL_ID, state=GoalState.ACTIVE), report=report
    )
    router, _, _ = wired(FakeGoalPort(channel=channel))

    data = router.call("pz_goal_status", {"goal_id": GOAL_ID})["data"]

    published = data["report"]
    assert published["containers_inspected"] == [crate], "a reference survives in the open"
    assert published["items_taken"] == {"food": 3}
    assert published["candidates_truncated"] is False
    skipped = published["containers_skipped"][0]
    assert skipped["ref"] == crate
    assert "reason" not in skipped, "a skip reason is a string and leaves quarantined"
    assert skipped[UNTRUSTED_TEXT_KEY]["reason"]
    assert HOSTILE_NAME not in json.dumps(data)
    assert "hostile" not in json.dumps(data), "the OS username must not cross this boundary"


def test_a_live_reports_ending_word_is_a_string_and_treated_as_one() -> None:
    """Even the module-minted ``ended`` token is quarantined, not trusted.

    The scrubber's rule is membership of a closed protocol vocabulary, and the
    mission's report vocabulary is deliberately not in it: "who wrote it" is
    the argument the boundary refuses, so the wrapper's own constants ride the
    quarantine like everything else rather than widening the trusted set.
    """
    channel = GoalChannelStatus(
        named=make_goal(goal_id=GOAL_ID, state=GoalState.ACTIVE),
        report={"ended": "in_progress", "waypoints_visited": 4},
    )
    router, _, _ = wired(FakeGoalPort(channel=channel))

    published = router.call("pz_goal_status", {"goal_id": GOAL_ID})["data"]["report"]

    assert published["waypoints_visited"] == 4
    assert published[UNTRUSTED_TEXT_KEY]["ended"] == "in_progress"
    assert published["content_marker"] == CONTENT_MARKER
