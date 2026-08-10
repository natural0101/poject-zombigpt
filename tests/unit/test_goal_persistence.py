"""Goals on disk: the round trip, the honest restore, and every refusal.

The property under test is the directive's own sentence: a goal survives a
sidecar restart and is either honestly restored (a pending goal, under its
original id, digest and submission time) or honestly finished (the previously
active goal, as ``FAILED`` / ``SESSION_TERMINATED`` with a detail saying the
sidecar restarted under it — the closed :class:`GoalState` gains no ``LOST``
member for this, and a test pins that it never silently does).

Everything here drives the queue and the store through their public surface:
records come from real submissions against a hand-driven clock, documents from
:func:`snapshot_to_document`, files from :class:`GoalStore`. The corrupt-file
tests write the broken bytes by hand, because that is exactly how a broken
file arrives in production — from outside the writer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pz_agent_core.goals import (
    GOALS_FILE_NAME,
    GOALS_QUARANTINE_NAME,
    GOALS_SCHEMA_VERSION,
    MAX_GOAL_FILE_BYTES,
    MAX_PERSISTED_TERMINAL,
    RESTART_LOST_DETAIL,
    GoalDocumentError,
    GoalKind,
    GoalParams,
    GoalQueue,
    GoalRecord,
    GoalRequest,
    GoalState,
    GoalStore,
    GoalStoreError,
    LootScope,
    TrainableSkill,
    snapshot_from_document,
    snapshot_to_document,
)
from pz_agent_core.protocol import ActionResult, ReasonCode


class FakeClock:
    """A wall clock the test drives by hand."""

    def __init__(self, start: int = 0) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> int:
        self.now += ms
        return self.now


def eat(key: str = "eat-1") -> GoalRequest:
    return GoalRequest(kind=GoalKind.SATISFY_HUNGER, idempotency_key=key)


def observed() -> ActionResult:
    return ActionResult.succeeded(
        session_id="s-1",
        seq=7,
        command_id="c-1",
        action="consume.eat",
        timestamp_ms=1_000,
        evidence={"hunger": 0.1},
    )


def submitted(queue: GoalQueue, request: GoalRequest) -> GoalRecord:
    admission = queue.submit(request)
    assert admission.goal is not None, admission.refusal
    return admission.goal


def activated(queue: GoalQueue) -> GoalRecord:
    started = queue.activate_next()
    assert started.goal is not None, started.refusal
    return started.goal


def a_lived_in_queue(clock: FakeClock) -> tuple[GoalQueue, dict[str, GoalRecord]]:
    """A queue holding one of everything the file must carry.

    One succeeded goal, one failed goal, one active goal (with a spent step
    and typed params) and one pending goal — the four shapes a restore has to
    answer for.
    """
    queue = GoalQueue(clock=clock)
    won = submitted(queue, eat("won-key"))
    activated(queue)
    assert queue.succeed(won.goal_id, observed()) is not None
    lost = submitted(queue, eat("lost-key"))
    activated(queue)
    assert queue.fail(lost.goal_id, ReasonCode.PATH_NOT_FOUND, "no route") is not None
    running = submitted(
        queue,
        GoalRequest(
            kind=GoalKind.LOOT_AREA,
            idempotency_key="running-key",
            params=GoalParams(scope=LootScope.RADIUS, radius=5, take_all=True),
        ),
    )
    activated(queue)
    assert queue.note_step(running.goal_id) is None
    clock.advance(500)
    waiting = submitted(
        queue,
        GoalRequest(
            kind=GoalKind.TRAIN_SKILL,
            idempotency_key="waiting-key",
            params=GoalParams(skill=TrainableSkill.FIRST_AID, target_level=3),
        ),
    )
    return queue, {"won": won, "lost": lost, "running": running, "waiting": waiting}


# --------------------------------------------------------------------------
# the round trip
# --------------------------------------------------------------------------


class TestRoundTrip:
    def test_open_and_terminal_records_survive_the_file_whole(self, tmp_path: Path) -> None:
        clock = FakeClock(10_000)
        queue, goals = a_lived_in_queue(clock)
        store = GoalStore(tmp_path)

        store.save_snapshot(queue.snapshot())
        loaded = store.load()

        assert loaded is not None
        by_id = {record.goal_id: record for record in loaded.records}
        for name in ("won", "lost", "running", "waiting"):
            assert goals[name].goal_id in by_id, f"the {name} goal did not survive the file"
        # Field-for-field: the file adds nothing and loses nothing. The live
        # records compare equal to the loaded ones because both are the same
        # frozen dataclass built from the same values.
        assert by_id[goals["waiting"].goal_id] == queue.record(goals["waiting"].goal_id)
        assert by_id[goals["running"].goal_id] == queue.record(goals["running"].goal_id)
        succeeded = by_id[goals["won"].goal_id]
        assert succeeded.state is GoalState.SUCCEEDED
        assert succeeded.evidence_keys == ("hunger",)
        failed = by_id[goals["lost"].goal_id]
        assert failed.reason_code is ReasonCode.PATH_NOT_FOUND
        assert failed.detail == "no route"

    def test_the_file_is_valid_json_at_the_declared_schema(self, tmp_path: Path) -> None:
        clock = FakeClock(10_000)
        queue, _ = a_lived_in_queue(clock)
        store = GoalStore(tmp_path)

        store.save_snapshot(queue.snapshot())

        document = json.loads((tmp_path / GOALS_FILE_NAME).read_text(encoding="utf-8"))
        assert document["schema_version"] == GOALS_SCHEMA_VERSION
        assert [entry["state"] for entry in document["open"]] == ["active", "pending"]
        assert len(document["terminal"]) == 2

    def test_terminal_history_is_capped_newest_kept(self) -> None:
        clock = FakeClock(1_000)
        queue = GoalQueue(clock=clock, max_remembered=64)
        for index in range(MAX_PERSISTED_TERMINAL + 4):
            record = submitted(queue, eat(f"cap-key-{index}"))
            activated(queue)
            queue.fail(record.goal_id, ReasonCode.PATH_NOT_FOUND, f"loss {index}")
        newest = queue.snapshot().records[-1]

        document = snapshot_to_document(queue.snapshot())

        assert len(document["terminal"]) == MAX_PERSISTED_TERMINAL
        assert document["terminal"][-1]["goal_id"] == newest.goal_id


# --------------------------------------------------------------------------
# the honest restore
# --------------------------------------------------------------------------


class TestRestore:
    def test_the_previous_active_goal_ends_failed_session_terminated(self, tmp_path: Path) -> None:
        clock = FakeClock(10_000)
        queue, goals = a_lived_in_queue(clock)
        store = GoalStore(tmp_path)
        store.save_snapshot(queue.snapshot())

        fresh = GoalQueue(clock=clock, armed=False)
        loaded = store.load()
        assert loaded is not None
        lost = fresh.restore(loaded, now_ms=clock.advance(60_000))

        assert [record.goal_id for record in lost] == [goals["running"].goal_id]
        ended = fresh.record(goals["running"].goal_id)
        assert ended is not None
        assert ended.state is GoalState.FAILED
        assert ended.reason_code is ReasonCode.SESSION_TERMINATED
        assert ended.detail == "the sidecar restarted while this goal was active"
        assert ended.detail == RESTART_LOST_DETAIL
        assert ended.steps_used == 1, "the step it spent is still the truth about it"
        assert fresh.active is None

    def test_goal_state_stays_closed_with_no_lost_member(self) -> None:
        """Pinned: the honest 'lost' is FAILED/SESSION_TERMINATED, not a new state."""
        assert {state.value for state in GoalState} == {
            "pending",
            "active",
            "succeeded",
            "failed",
            "cancelled",
            "expired",
        }

    def test_a_pending_goal_comes_back_pending_under_its_original_identity(
        self, tmp_path: Path
    ) -> None:
        clock = FakeClock(10_000)
        queue, goals = a_lived_in_queue(clock)
        store = GoalStore(tmp_path)
        store.save_snapshot(queue.snapshot())

        fresh = GoalQueue(clock=clock, armed=False)
        loaded = store.load()
        assert loaded is not None
        fresh.restore(loaded, now_ms=clock.now)

        restored = fresh.record(goals["waiting"].goal_id)
        assert restored is not None
        assert restored.state is GoalState.PENDING
        assert restored.submitted_at_ms == goals["waiting"].submitted_at_ms
        assert restored.key_digest == goals["waiting"].key_digest
        assert [record.goal_id for record in fresh.pending] == [goals["waiting"].goal_id]

    def test_a_resubmitted_key_resolves_to_the_restored_goal_not_a_second_one(
        self, tmp_path: Path
    ) -> None:
        """The directive's recovery: the same key is the same goal across restart."""
        clock = FakeClock(10_000)
        queue, goals = a_lived_in_queue(clock)
        store = GoalStore(tmp_path)
        store.save_snapshot(queue.snapshot())

        fresh = GoalQueue(clock=clock, armed=False)
        loaded = store.load()
        assert loaded is not None
        fresh.restore(loaded, now_ms=clock.now)
        again = fresh.submit(
            GoalRequest(
                kind=GoalKind.TRAIN_SKILL,
                idempotency_key="waiting-key",
                params=GoalParams(skill=TrainableSkill.FIRST_AID, target_level=3),
            )
        )

        assert again.duplicate is True
        assert again.goal is not None
        assert again.goal.goal_id == goals["waiting"].goal_id

    def test_pending_ttl_counts_from_the_original_submission_and_expires_on_first_tick(
        self, tmp_path: Path
    ) -> None:
        """A goal whose TTL ran out while the sidecar was down gets no second life."""
        clock = FakeClock(10_000)
        queue = GoalQueue(clock=clock)
        record = submitted(queue, eat("ttl-key"))
        store = GoalStore(tmp_path)
        store.save_snapshot(queue.snapshot())

        clock.advance(record.budget.pending_ttl_ms + 1)
        fresh = GoalQueue(clock=clock, armed=False)
        loaded = store.load()
        assert loaded is not None
        fresh.restore(loaded, now_ms=clock.now)
        transitions = fresh.tick()

        assert [(t.goal_id, t.state, t.reason_code) for t in transitions] == [
            (record.goal_id, GoalState.EXPIRED, ReasonCode.ACTION_TIMEOUT)
        ]

    def test_a_wall_clock_that_stepped_back_cannot_finish_a_goal_before_it_began(
        self, tmp_path: Path
    ) -> None:
        clock = FakeClock(100_000)
        queue = GoalQueue(clock=clock)
        record = submitted(queue, eat("clock-key"))
        activated(queue)
        store = GoalStore(tmp_path)
        store.save_snapshot(queue.snapshot())

        fresh = GoalQueue(clock=FakeClock(5), armed=False)
        loaded = store.load()
        assert loaded is not None
        fresh.restore(loaded, now_ms=5)

        ended = fresh.record(record.goal_id)
        assert ended is not None and ended.finished_at_ms is not None
        assert ended.finished_at_ms >= ended.submitted_at_ms

    def test_restore_refuses_a_queue_that_already_holds_goals(self, tmp_path: Path) -> None:
        clock = FakeClock(10_000)
        queue, _ = a_lived_in_queue(clock)
        store = GoalStore(tmp_path)
        store.save_snapshot(queue.snapshot())
        loaded = store.load()
        assert loaded is not None

        used = GoalQueue(clock=clock)
        submitted(used, eat("occupied-key"))

        with pytest.raises(ValueError, match="fresh queue"):
            used.restore(loaded, now_ms=clock.now)

    def test_restore_refuses_more_pending_goals_than_the_channel_admits(self) -> None:
        clock = FakeClock(1_000)
        wide = GoalQueue(clock=clock, max_open=3)
        for index in range(3):
            submitted(wide, eat(f"backlog-{index}"))
        snapshot = wide.snapshot()

        narrow = GoalQueue(clock=clock, max_open=1, max_remembered=8)

        with pytest.raises(ValueError, match="refusing to guess"):
            narrow.restore(snapshot, now_ms=clock.now)
        assert narrow.snapshot().records == (), "a refused restore leaves the queue fresh"


# --------------------------------------------------------------------------
# refusing what cannot be trusted
# --------------------------------------------------------------------------


def valid_document(clock: FakeClock) -> dict[str, Any]:
    queue, _ = a_lived_in_queue(clock)
    document = snapshot_to_document(queue.snapshot())
    round_tripped: dict[str, Any] = json.loads(json.dumps(document))
    return round_tripped


class TestTheFileIsUntrusted:
    def test_a_missing_file_is_the_ordinary_first_run(self, tmp_path: Path) -> None:
        assert GoalStore(tmp_path).load() is None

    def test_an_over_cap_file_is_refused_before_the_parse(self, tmp_path: Path) -> None:
        path = tmp_path / GOALS_FILE_NAME
        path.write_bytes(b"[" + b" " * MAX_GOAL_FILE_BYTES + b"]")

        with pytest.raises(GoalStoreError, match=f"exceeds {MAX_GOAL_FILE_BYTES}"):
            GoalStore(tmp_path).load()

    def test_malformed_json_is_a_store_error(self, tmp_path: Path) -> None:
        (tmp_path / GOALS_FILE_NAME).write_text("{not json", encoding="utf-8")

        with pytest.raises(GoalStoreError, match="malformed JSON"):
            GoalStore(tmp_path).load()

    def test_an_undeclared_schema_version_is_refused_not_guessed(self) -> None:
        with pytest.raises(GoalDocumentError, match="refusing to guess"):
            snapshot_from_document({"open": [], "terminal": []})

    def test_a_future_schema_is_refused_rather_than_partially_read(self) -> None:
        with pytest.raises(GoalDocumentError, match=f"at most {GOALS_SCHEMA_VERSION}"):
            snapshot_from_document(
                {"schema_version": GOALS_SCHEMA_VERSION + 1, "open": [], "terminal": []}
            )

    @pytest.mark.parametrize(
        ("corruption", "complaint"),
        [
            (lambda d: d["open"][0].__setitem__("kind", "conquer_kentucky"), "kind"),
            (lambda d: d["open"][0].__setitem__("state", "lost"), "state"),
            (lambda d: d["terminal"][0].__setitem__("reason_code", "BECAUSE"), "reason"),
            (lambda d: d["open"][0].__setitem__("key_digest", "not-hex!"), "digest"),
            (lambda d: d["open"][0]["params"].__setitem__("smuggled", "text"), "unknown name"),
            (lambda d: d["open"][0].__setitem__("steps_used", 99), "steps_used"),
            (lambda d: d["terminal"][0].__setitem__("reason_code", None), "why it ended"),
        ],
    )
    def test_a_document_that_lies_is_refused_with_a_typed_diagnostic(
        self, corruption: Any, complaint: str
    ) -> None:
        document = valid_document(FakeClock(10_000))
        corruption(document)

        with pytest.raises(GoalDocumentError, match=complaint):
            snapshot_from_document(document)

    def test_the_refusal_never_echoes_the_files_own_bytes(self) -> None:
        """The no-echo rule at this door: a planted marker must not surface."""
        marker = "EVIL_MARKER_9000"
        document = valid_document(FakeClock(10_000))
        document["open"][0]["kind"] = marker

        with pytest.raises(GoalDocumentError) as caught:
            snapshot_from_document(document)

        assert marker not in str(caught.value)

    def test_a_terminal_entry_claiming_to_be_open_is_refused(self) -> None:
        document = valid_document(FakeClock(10_000))
        document["open"], document["terminal"] = document["terminal"], document["open"]

        with pytest.raises(GoalDocumentError, match="claims to be"):
            snapshot_from_document(document)

    def test_quarantine_sets_the_file_aside_and_keeps_the_newest_corpse(
        self, tmp_path: Path
    ) -> None:
        store = GoalStore(tmp_path)
        store.quarantine_path.write_text("older corpse", encoding="utf-8")
        (tmp_path / GOALS_FILE_NAME).write_text("newer corpse", encoding="utf-8")

        aside = store.quarantine()

        assert aside == tmp_path / GOALS_QUARANTINE_NAME
        assert aside.read_text(encoding="utf-8") == "newer corpse"
        assert not (tmp_path / GOALS_FILE_NAME).exists()

    def test_quarantining_nothing_is_a_store_error_not_a_shrug(self, tmp_path: Path) -> None:
        with pytest.raises(GoalStoreError, match="set the unreadable goals file aside"):
            GoalStore(tmp_path).quarantine()


# --------------------------------------------------------------------------
# the revision counter the write cadence hangs off
# --------------------------------------------------------------------------


class TestRevision:
    def test_a_tick_that_moves_nothing_leaves_the_revision_alone(self) -> None:
        clock = FakeClock(1_000)
        queue = GoalQueue(clock=clock)
        submitted(queue, eat("still-key"))
        before = queue.revision

        clock.advance(10)
        queue.tick()

        assert queue.revision == before

    def test_every_kind_of_transition_moves_it(self) -> None:
        clock = FakeClock(1_000)
        queue = GoalQueue(clock=clock)
        marks = [queue.revision]
        record = submitted(queue, eat("moving-key"))
        marks.append(queue.revision)
        activated(queue)
        marks.append(queue.revision)
        queue.note_step(record.goal_id)
        marks.append(queue.revision)

        assert marks == sorted(set(marks)), "submit, activate and a step each move the revision"
