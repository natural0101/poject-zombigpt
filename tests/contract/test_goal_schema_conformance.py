"""The goal schema describes the objects the codec actually puts on the wire.

Same contract as the other two conformance files: what
:mod:`pz_agent_mcp.remote.codec.goals` emits must validate against
``schemas/goal.schema.json``, and what the schema permits must decode. The
request is the object that matters most — it is the only message on the link
whose content originated with a language model or a microphone — so its closed
sets (the kinds, the trainable skills) and its numeric ranges are compared
value-for-value against :mod:`pz_agent_core.goals`, which is where they are
enforced. The record is validated against ``$defs/record``: it travels inside
RPC results, never in the request's slot.

``reason_code`` is deliberately a shape here, not an enum: the set belongs to
:class:`~pz_agent_core.protocol.ReasonCode` and is validated at decode time.
What this file pins instead is that every member of that enum *fits* the
schema's shape, so the pattern can never quietly exclude a code the core
actually reports.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

import pytest
from jsonschema import Draft202012Validator

from pz_agent_core.goals import (
    GOAL_SPECS,
    MAX_DETAIL_CHARS,
    MAX_EVIDENCE_KEYS,
    MAX_GOAL_STEPS,
    MAX_GOAL_WALL_MS,
    MAX_PENDING_TTL_MS,
    MIN_GOAL_WALL_MS,
    NUMERIC_RANGES,
    GoalBudget,
    GoalKind,
    GoalParams,
    GoalRecord,
    GoalRequest,
    GoalState,
    TrainableSkill,
    key_digest,
    mint_goal_id,
)
from pz_agent_core.protocol import ReasonCode
from pz_agent_mcp.remote.codec.goals import (
    decode_goal_request,
    encode_goal_record,
    encode_goal_request,
)

pytestmark = pytest.mark.contract

SCHEMA_PATH: Final = Path(__file__).resolve().parents[2] / "schemas" / "goal.schema.json"


def _document() -> dict[str, Any]:
    loaded: Any = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture(scope="module")
def schema() -> Draft202012Validator:
    document = _document()
    Draft202012Validator.check_schema(document)
    return Draft202012Validator(document)


@pytest.fixture(scope="module")
def record_schema() -> Draft202012Validator:
    """The record fragment, validated as its own root.

    ``$defs`` references inside it still resolve because the fragment is
    embedded back under the document that defines them.
    """
    document = _document()
    fragment = dict(document["$defs"]["record"])
    fragment["$defs"] = document["$defs"]
    Draft202012Validator.check_schema(fragment)
    return Draft202012Validator(fragment)


def _requests() -> list[GoalRequest]:
    return [
        GoalRequest(
            kind=GoalKind.SATISFY_HUNGER,
            idempotency_key="voice:eat.0001",
            params=GoalParams(satisfy_to=0.7),
        ),
        GoalRequest(
            kind=GoalKind.TRAIN_SKILL,
            idempotency_key="k-" + "a" * 62,
            params=GoalParams(skill=TrainableSkill.CARPENTRY, target_level=3),
            budget=GoalBudget(max_wall_ms=60_000, max_steps=10, pending_ttl_ms=30_000),
        ),
        GoalRequest(
            kind=GoalKind.READ_FOR_BOREDOM,
            idempotency_key="mcp.read:42",
            params=GoalParams(pages=5),
        ),
    ]


class TestOutbound:
    @pytest.mark.parametrize("request_", _requests(), ids=lambda r: r.kind.value)
    def test_every_encoded_request_validates(
        self, schema: Draft202012Validator, request_: GoalRequest
    ) -> None:
        schema.validate(encode_goal_request(request_))

    def test_a_pending_record_validates(self, record_schema: Draft202012Validator) -> None:
        record = GoalRecord(
            goal_id=mint_goal_id(),
            kind=GoalKind.SATISFY_THIRST,
            params=GoalParams(satisfy_to=0.5),
            budget=GoalBudget(max_wall_ms=30_000, max_steps=5, pending_ttl_ms=10_000),
            key_digest=key_digest("k-1"),
            sequence=0,
            state=GoalState.PENDING,
            submitted_at_ms=1_000,
        )
        record_schema.validate(encode_goal_record(record))

    def test_a_succeeded_record_validates_with_its_evidence(
        self, record_schema: Draft202012Validator
    ) -> None:
        record = GoalRecord(
            goal_id=mint_goal_id(),
            kind=GoalKind.SATISFY_HUNGER,
            params=GoalParams(satisfy_to=0.7),
            budget=GoalBudget(max_wall_ms=30_000, max_steps=5, pending_ttl_ms=10_000),
            key_digest=key_digest("k-2"),
            sequence=1,
            state=GoalState.SUCCEEDED,
            submitted_at_ms=1_000,
            started_at_ms=1_200,
            finished_at_ms=9_000,
            steps_used=3,
            reason_code=ReasonCode.POSTCONDITION_MET,
            evidence_keys=("hunger_before", "hunger_after"),
        )
        record_schema.validate(encode_goal_record(record))


class TestInbound:
    def test_a_schema_valid_request_decodes(self, schema: Draft202012Validator) -> None:
        payload = {
            "kind": "satisfy_hunger",
            "idempotency_key": "client:goal.7",
            "params": {"satisfy_to": 0.8},
        }
        schema.validate(payload)
        request = decode_goal_request(payload)
        assert request.kind is GoalKind.SATISFY_HUNGER
        assert request.params.satisfy_to == 0.8


class TestTheClosedSetsAgree:
    def test_the_kinds_are_the_goal_kind_enum(self) -> None:
        assert set(_document()["$defs"]["kind"]["enum"]) == {m.value for m in GoalKind}

    def test_the_skills_are_the_trainable_skill_enum(self) -> None:
        skills = _document()["$defs"]["params"]["properties"]["skill"]["enum"]
        assert set(skills) == {m.value for m in TrainableSkill}

    def test_the_states_are_the_goal_state_enum(self) -> None:
        states = _document()["$defs"]["record"]["properties"]["state"]["enum"]
        assert set(states) == {m.value for m in GoalState}

    def test_the_numeric_ranges_are_the_models_ranges(self) -> None:
        params = _document()["$defs"]["params"]["properties"]
        for name, bound in NUMERIC_RANGES.items():
            assert params[name]["minimum"] == bound.minimum, name
            assert params[name]["maximum"] == bound.maximum, name

    def test_the_budget_bounds_are_the_models_bounds(self) -> None:
        budget = _document()["$defs"]["budget"]["properties"]
        assert budget["max_wall_ms"]["minimum"] == MIN_GOAL_WALL_MS
        assert budget["max_wall_ms"]["maximum"] == MAX_GOAL_WALL_MS
        assert budget["max_steps"]["maximum"] == MAX_GOAL_STEPS
        assert budget["pending_ttl_ms"]["maximum"] == MAX_PENDING_TTL_MS

    def test_the_record_bounds_are_the_models_bounds(self) -> None:
        record = _document()["$defs"]["record"]["properties"]
        assert record["evidence_keys"]["maxItems"] == MAX_EVIDENCE_KEYS
        assert record["detail"]["maxLength"] == MAX_DETAIL_CHARS

    def test_every_kind_the_specs_know_is_in_the_schema(self) -> None:
        """GOAL_SPECS and the schema agree, through the enum both draw from."""
        assert set(GOAL_SPECS) == set(GoalKind)

    def test_every_reason_code_fits_the_records_shape(self) -> None:
        pattern = re.compile(_document()["$defs"]["record"]["properties"]["reason_code"]["pattern"])
        unfitting = [m.value for m in ReasonCode if not pattern.match(m.value)]
        assert unfitting == [], "the shape would refuse codes the core reports"


class TestTheSchemaRefusesWhatTheModelRefuses:
    @pytest.mark.parametrize(
        "payload",
        [
            {"kind": "eat", "idempotency_key": "k-1", "params": {}},
            {"kind": "satisfy_hunger", "params": {}},
            {"kind": "satisfy_hunger", "idempotency_key": "k-1", "params": {"satisfy_to": 70}},
            {"kind": "satisfy_hunger", "idempotency_key": "k-1", "params": {"goal": "иди есть"}},
            {"kind": "train_skill", "idempotency_key": "k-1", "params": {"skill": "sprinting"}},
            {
                "kind": "satisfy_hunger",
                "idempotency_key": "k-1",
                "params": {},
                "budget": {"max_wall_ms": 100, "max_steps": 1, "pending_ttl_ms": 1},
            },
        ],
        ids=[
            "voice-token-not-a-kind",
            "no-idempotency-key",
            "percentage-not-a-fraction",
            "free-text-smuggled-into-params",
            "untrainable-skill",
            "budget-below-the-floor",
        ],
    )
    def test_it_does_not_validate(
        self, schema: Draft202012Validator, payload: dict[str, Any]
    ) -> None:
        assert not schema.is_valid(payload)
