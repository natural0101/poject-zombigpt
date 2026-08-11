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

#: Kinds the core carries that the *remote wire* deliberately does not yet.
#: ``navigate_to`` is served locally by the sidecar's deterministic route
#: executor; putting it on the remote link is a protocol change — a
#: ``schemas/goal.schema.json`` edit plus a ``version.py`` bump — that this
#: epic does not make. The gap is pinned both ways below: the schema must not
#: quietly grow the kind, and a payload naming it must *fail* validation, so a
#: remote submission is a loud refusal rather than a silently dropped goal.
#: ``return_home`` and ``explore_area`` join the carve-out on the same terms:
#: both are served locally by the sidecar's deterministic servers, and putting
#: either on the remote link is the same protocol change. The care wave's
#: three kinds — ``treat_wounds``, ``rest_until``, ``sleep_until_rested`` —
#: join on those terms again: each is served locally by the deterministic
#: care missions, and each rides the same loud-refusal rule below.
#: ``avoid_threat`` joins on those terms once more: the retreat is served
#: locally by the deterministic avoid mission, and the same loud-refusal
#: rule below covers it until the protocol change puts it on the link.
#: ``engage_single_zombie`` joins on those terms and one more besides: the
#: combat kind is served locally by the deterministic combat mission, and a
#: remote link that could carry a kill order is a protocol change nobody
#: should make by accident — the loud refusal below is the pin.
NOT_YET_ON_THE_WIRE: Final[frozenset[str]] = frozenset(
    {
        "navigate_to",
        "loot_area",
        "return_home",
        "explore_area",
        "treat_wounds",
        "rest_until",
        "sleep_until_rested",
        "avoid_threat",
        "engage_single_zombie",
    }
)

#: The *numeric* parameters only those kinds carry, absent from the wire
#: schema for the same reason and pinned the same way. (Every name here must
#: have a row in NUMERIC_RANGES; the loot goal's non-numeric parameters are
#: pinned separately below.)
LOCAL_ONLY_PARAMS: Final[frozenset[str]] = frozenset(
    {"target_x", "target_y", "target_z", "radius", "target_endurance", "hours"}
)

#: The loot goal's closed-token parameters: local to the sidecar, not on the
#: wire, and with no numeric range to appear in the loop above — so their
#: absence from the schema is pinned by its own assertion.
LOCAL_ONLY_TOKEN_PARAMS: Final[frozenset[str]] = frozenset({"scope", "take_all", "categories"})


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

    def test_a_suspended_record_validates_with_its_bookkeeping(
        self, record_schema: Draft202012Validator
    ) -> None:
        """The four suspension keys are declared, not smuggled past a closed set.

        They are written sparsely — an ordinary goal's document carries none of
        them — and that is exactly what makes this test necessary rather than
        redundant. A sparse encoding under ``additionalProperties: false`` keeps
        the gate green for every goal that was never parked, so a schema that
        never learned the four would fail only on the day a needs preemption
        actually happened, on a user's machine, over a link that had passed
        every test here.
        """
        record = GoalRecord(
            goal_id=mint_goal_id(),
            kind=GoalKind.SATISFY_HUNGER,
            params=GoalParams(satisfy_to=0.7),
            budget=GoalBudget(max_wall_ms=600_000, max_steps=12, pending_ttl_ms=60_000),
            key_digest=key_digest("k-3"),
            sequence=2,
            state=GoalState.PENDING,
            submitted_at_ms=1_000,
            suspended_by=mint_goal_id(),
            suspensions=1,
            active_ms_before_suspend=120_000,
            front_rank=-1,
        )

        encoded = encode_goal_record(record)

        assert "suspended_by" in encoded, "the marker must survive the wire to be worth declaring"
        record_schema.validate(encoded)


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
        published = set(_document()["$defs"]["kind"]["enum"])
        assert published == {m.value for m in GoalKind} - NOT_YET_ON_THE_WIRE
        # Both directions of the carve-out: the schema must not have grown the
        # kind without the protocol change, and the core must still carry it.
        assert published & NOT_YET_ON_THE_WIRE == set()
        assert {m.value for m in GoalKind} >= NOT_YET_ON_THE_WIRE

    def test_a_local_only_kind_fails_wire_validation_loudly(
        self, schema: Draft202012Validator
    ) -> None:
        """The gap is a refusal at the wire, never a silently dropped goal."""
        local_params: dict[str, dict[str, Any]] = {
            "navigate_to": {"target_x": 1200, "target_y": 3400, "target_z": 0},
            "loot_area": {"scope": "room"},
            "return_home": {},
            "explore_area": {"scope": "radius", "radius": 8},
            "treat_wounds": {},
            "rest_until": {"target_endurance": 0.8},
            "sleep_until_rested": {"hours": 8},
            "avoid_threat": {},
            "engage_single_zombie": {},
        }
        assert set(local_params) == set(NOT_YET_ON_THE_WIRE)
        for kind, params in sorted(local_params.items()):
            payload = {
                "kind": kind,
                "idempotency_key": "client:goal.8",
                "params": params,
            }
            assert not schema.is_valid(payload), kind
            # Even a bare submission of the kind is refused: the enum is the
            # gate, not the parameters that happened to ride along.
            assert not schema.is_valid(
                {"kind": kind, "idempotency_key": "client:goal.9", "params": {}}
            ), kind

    def test_the_skills_are_the_trainable_skill_enum(self) -> None:
        skills = _document()["$defs"]["params"]["properties"]["skill"]["enum"]
        assert set(skills) == {m.value for m in TrainableSkill}

    def test_the_states_are_the_goal_state_enum(self) -> None:
        states = _document()["$defs"]["record"]["properties"]["state"]["enum"]
        assert set(states) == {m.value for m in GoalState}

    def test_the_numeric_ranges_are_the_models_ranges(self) -> None:
        params = _document()["$defs"]["params"]["properties"]
        for name, bound in NUMERIC_RANGES.items():
            if name in LOCAL_ONLY_PARAMS:
                # Not on the wire yet, and pinned absent rather than skipped:
                # a schema that grew the property without the protocol change
                # would put half of navigate_to on the link.
                assert name not in params, name
                continue
            assert params[name]["minimum"] == bound.minimum, name
            assert params[name]["maximum"] == bound.maximum, name
        assert set(NUMERIC_RANGES) >= LOCAL_ONLY_PARAMS

    def test_the_local_only_token_parameters_are_not_on_the_wire(self) -> None:
        """The other half of the loot carve-out: scope/take_all/categories.

        Not numeric, so the range loop above never visits them; their absence
        is pinned here so a schema that grew one without the protocol change
        would put half of loot_area on the link.
        """
        params = _document()["$defs"]["params"]["properties"]
        for name in sorted(LOCAL_ONLY_TOKEN_PARAMS):
            assert name not in params, name
        # And the census stays honest against the model's own declaration.
        loot_spec = GOAL_SPECS[GoalKind("loot_area")]
        assert loot_spec.required == frozenset()
        assert loot_spec.optional == LOCAL_ONLY_TOKEN_PARAMS | {"radius"}

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
