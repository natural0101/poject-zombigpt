"""The attempt ledger.

Four invariants, and each of them is here because the alternative lets a release
claim something nobody saw: NOT_RUN is where a scenario starts, a PASS is never
overwritten, a FAIL keeps both attempts when it is retried, and an edited ledger
is detected rather than believed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pz_agent_cli.livetest.state import (
    MAX_ATTEMPTS,
    Attempt,
    LedgerError,
    LiveState,
    ScenarioState,
    StateStore,
)

SCENARIO = "S07_NESTED_INVENTORY"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path)


def record(
    store: StateStore,
    status: LiveState,
    *,
    scenario: str = SCENARIO,
    at: int = 1_000,
    digest: str = DIGEST_A,
    failure_code: str = "",
) -> ScenarioState:
    return store.record(
        scenario,
        status=status,
        started_at_ms=at,
        finished_at_ms=at + 10,
        result_sha256=digest,
        failure_code=failure_code,
    )


class TestNotRun:
    def test_a_scenario_with_no_ledger_is_not_run(self, store: StateStore) -> None:
        assert store.read(SCENARIO).state is LiveState.NOT_RUN

    def test_an_empty_ledger_is_not_run(self) -> None:
        assert ScenarioState(scenario_id=SCENARIO).state is LiveState.NOT_RUN

    def test_a_blocked_attempt_does_not_reach_pass(self, store: StateStore) -> None:
        """Blocked means somebody tried and saw nothing. It is not a pass."""
        state = record(store, LiveState.BLOCKED, failure_code="NOT_OBSERVED")

        assert state.state is LiveState.BLOCKED
        assert state.passing_attempt is None

    def test_initialise_creates_ledgers_that_read_as_not_run(self, store: StateStore) -> None:
        created = store.initialise([SCENARIO, "S04_MOVE"])

        assert set(created) == {SCENARIO, "S04_MOVE"}
        assert store.read("S04_MOVE").state is LiveState.NOT_RUN

    def test_initialise_never_clobbers_an_existing_ledger(self, store: StateStore) -> None:
        """prepare is re-run after a failed session; erasing the failures would
        remove exactly the record the operator needs."""
        record(store, LiveState.FAIL, failure_code="POSTCONDITION_FAILED")

        created = store.initialise([SCENARIO])

        assert created == ()
        assert store.read(SCENARIO).attempt_count == 1


class TestPassIsNeverOverwritten:
    def test_a_failing_rerun_leaves_the_verdict_at_pass(self, store: StateStore) -> None:
        record(store, LiveState.PASS, at=1_000, digest=DIGEST_A)

        state = record(store, LiveState.FAIL, at=2_000, digest=DIGEST_B)

        assert state.state is LiveState.PASS
        assert state.attempt_count == 2
        assert [a.status for a in state.attempts] == [LiveState.PASS, LiveState.FAIL]

    def test_the_passing_attempt_is_the_one_reported(self, store: StateStore) -> None:
        record(store, LiveState.FAIL, at=1_000, digest=DIGEST_A)
        record(store, LiveState.PASS, at=2_000, digest=DIGEST_B)
        record(store, LiveState.BLOCKED, at=3_000, digest=DIGEST_A)

        state = store.read(SCENARIO)

        assert state.state is LiveState.PASS
        passing = state.passing_attempt
        assert passing is not None
        assert passing.number == 2
        assert passing.result_sha256 == DIGEST_B

    def test_a_rerun_is_visible_as_an_extra_attempt(self, store: StateStore) -> None:
        record(store, LiveState.PASS, at=1_000)

        state = record(store, LiveState.PASS, at=5_000)

        assert state.attempt_count == 2
        assert state.last_run_ms == 5_010


class TestFailIsRetryable:
    def test_both_attempts_stay_on_the_record(self, store: StateStore) -> None:
        record(store, LiveState.FAIL, at=1_000, failure_code="PATH_NOT_FOUND")

        state = record(store, LiveState.PASS, at=2_000)

        assert [a.status for a in state.attempts] == [LiveState.FAIL, LiveState.PASS]
        assert state.attempts[0].failure_code == "PATH_NOT_FOUND"
        assert state.state is LiveState.PASS

    def test_the_ledger_is_bounded(self, store: StateStore) -> None:
        for index in range(MAX_ATTEMPTS):
            record(store, LiveState.FAIL, at=index)

        with pytest.raises(LedgerError, match="already recorded"):
            record(store, LiveState.FAIL, at=MAX_ATTEMPTS)


class TestChainDetectsEditing:
    def test_a_clean_ledger_round_trips(self, store: StateStore) -> None:
        record(store, LiveState.FAIL, at=1_000)
        record(store, LiveState.PASS, at=2_000)

        reread = store.read(SCENARIO)

        assert reread.attempt_count == 2
        reread.verify_chain()

    def test_flipping_a_status_in_the_file_is_caught(self, store: StateStore) -> None:
        record(store, LiveState.FAIL, at=1_000, failure_code="POSTCONDITION_FAILED")
        path = store.path_for(SCENARIO)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["attempts"][0]["status"] = "PASS"
        path.write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(LedgerError, match="does not verify"):
            store.read(SCENARIO)

    def test_editing_an_early_attempt_invalidates_the_later_ones(self, store: StateStore) -> None:
        record(store, LiveState.FAIL, at=1_000)
        record(store, LiveState.FAIL, at=2_000)
        record(store, LiveState.FAIL, at=3_000)
        path = store.path_for(SCENARIO)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["attempts"][0]["result_sha256"] = DIGEST_B
        path.write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(LedgerError, match="attempt 1"):
            store.read(SCENARIO)

    def test_deleting_an_attempt_is_caught_by_the_numbering(self, store: StateStore) -> None:
        record(store, LiveState.FAIL, at=1_000)
        record(store, LiveState.PASS, at=2_000)
        path = store.path_for(SCENARIO)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["attempts"] = document["attempts"][1:]
        path.write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(LedgerError, match="removed or reordered"):
            store.read(SCENARIO)

    def test_an_entry_lifted_from_another_scenario_does_not_verify(self, store: StateStore) -> None:
        """The chain is seeded from the scenario id, so entries are not portable."""
        record(store, LiveState.PASS, scenario="S04_MOVE", at=1_000)
        stolen = json.loads(store.path_for("S04_MOVE").read_text(encoding="utf-8"))
        store.initialise([SCENARIO])
        target = store.path_for(SCENARIO)
        document = json.loads(target.read_text(encoding="utf-8"))
        document["attempts"] = stolen["attempts"]
        target.write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(LedgerError, match="does not verify"):
            store.read(SCENARIO)


class TestMalformedLedgers:
    def test_a_ledger_from_another_format_is_refused(self, store: StateStore) -> None:
        store.initialise([SCENARIO])
        path = store.path_for(SCENARIO)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["format"] = "pz-agent/livetest-state/99"
        path.write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(LedgerError, match="format"):
            store.read(SCENARIO)

    def test_a_ledger_naming_another_scenario_is_refused(self, store: StateStore) -> None:
        store.initialise([SCENARIO])
        path = store.path_for(SCENARIO)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["scenario_id"] = "S04_MOVE"
        path.write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(LedgerError, match="belongs to"):
            store.read(SCENARIO)

    def test_broken_json_is_a_ledger_error_not_a_traceback(self, store: StateStore) -> None:
        store.initialise([SCENARIO])
        store.path_for(SCENARIO).write_text("{not json", encoding="utf-8")

        with pytest.raises(LedgerError, match="not valid JSON"):
            store.read(SCENARIO)

    def test_an_attempt_with_an_unknown_status_is_refused(self, store: StateStore) -> None:
        store.initialise([SCENARIO])
        path = store.path_for(SCENARIO)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["attempts"] = [
            {
                "number": 1,
                "status": "DEFINITELY_FINE",
                "started_at_ms": 1,
                "finished_at_ms": 2,
                "result_sha256": DIGEST_A,
                "chain": "0" * 64,
            }
        ]
        path.write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(LedgerError, match="status"):
            store.read(SCENARIO)

    def test_writing_a_hand_built_state_with_a_bad_chain_is_refused(
        self, store: StateStore
    ) -> None:
        """There is no path that persists a ledger without verifying it first."""
        forged = ScenarioState(
            scenario_id=SCENARIO,
            attempts=(
                Attempt(
                    number=1,
                    status=LiveState.PASS,
                    started_at_ms=1,
                    finished_at_ms=2,
                    result_sha256=DIGEST_A,
                    chain="0" * 64,
                ),
            ),
        )

        with pytest.raises(LedgerError, match="does not verify"):
            store.write(forged)


def test_the_serialised_state_carries_the_derived_verdict(store: StateStore) -> None:
    record(store, LiveState.PASS, at=1_000)
    record(store, LiveState.FAIL, at=2_000)

    document = store.read(SCENARIO).to_dict()

    assert document["state"] == "PASS"
    assert document["attempt_count"] == 2
    assert document["last_run_ms"] == 2_010


def test_read_all_preserves_the_order_it_was_asked_for(store: StateStore) -> None:
    ids = ["S04_MOVE", SCENARIO, "S01_INSTALL"]

    assert [state.scenario_id for state in store.read_all(ids)] == ids
