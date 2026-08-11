"""The knowledge corpus vocabulary, mirrored from the schema that owns it.

``schemas/gameplay-knowledge.schema.json`` is the wire source of truth for the
corpus documents under ``knowledge/gameplay/``; this module is the loader's
typed reading of the same contract. Every closed set here — domains, statuses,
sources, need channels — is a mirror of an ``enum`` array in that schema, and
``tests/unit/test_knowledge_loader.py`` reads the schema file and compares the
arrays member-for-member so the two cannot drift apart quietly.

The honesty rule the whole corpus runs on lives in :class:`KnowledgeStatus`:
``verified_script`` is reserved for rules that restate behaviour this
repository's tested code enforces, ``verified_live`` for postconditions a real
session observed, and everything else is ``unverified`` — a hypothesis the
planner may retrieve but must not treat as ground truth. A rule dressed above
its evidence is refused by the validation here and in
:mod:`pz_agent_core.knowledge.loader`, never corrected.

Refusal messages are assembled from field names, bounds and tokens that have
already passed a closed pattern check. Free text out of a YAML file — a claim,
a detail, an unvalidated id — is never echoed into an error, because the
corpus is data and a refusal is not a place to launder it into output.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum, unique
from typing import Final, NoReturn, TypeVar

from ..goals.model import GoalKind
from ..protocol import ActionName, RiskClass

__all__ = [
    "MAX_ACTIONS",
    "MAX_GOAL_KINDS",
    "MAX_NEARBY_KINDS",
    "MAX_NEEDS",
    "MAX_NUMBERS",
    "MAX_OBSERVED_INPUTS",
    "MAX_POSTCONDITIONS",
    "MAX_PRECONDITIONS",
    "MAX_PROVEN_BY",
    "MAX_RULES_PER_DOCUMENT",
    "SCHEMA_VERSION",
    "KnowledgeDocument",
    "KnowledgeDomain",
    "KnowledgeNumber",
    "KnowledgeRefusal",
    "KnowledgeRule",
    "KnowledgeSource",
    "KnowledgeStatus",
    "KnowledgeValidationError",
    "NeedChannel",
]

#: The one document version this loader reads; the schema pins it as a const.
SCHEMA_VERSION: Final = "1.0"

#: Array bounds, each the schema's own ``maxItems`` for the matching field.
MAX_RULES_PER_DOCUMENT: Final = 64
MAX_OBSERVED_INPUTS: Final = 12
MAX_PRECONDITIONS: Final = 8
MAX_POSTCONDITIONS: Final = 8
MAX_ACTIONS: Final = 8
MAX_GOAL_KINDS: Final = 6
MAX_NEEDS: Final = 6
MAX_NEARBY_KINDS: Final = 8
MAX_PROVEN_BY: Final = 6
MAX_NUMBERS: Final = 8

#: String patterns, verbatim from the schema. Compiled once; every free-form
#: token is checked against one of these before it may appear in a refusal.
_ID_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_BUILD_PATTERN: Final = re.compile(r"^[0-9]{2}(\.[0-9]{1,3}){0,2}$")
_OBSERVED_INPUT_PATTERN: Final = re.compile(
    r"^[a-z_][a-z0-9_]*(\[\])?(\.[a-z_][a-z0-9_]*(\[\])?){0,5}$"
)
_ACTION_PATTERN: Final = re.compile(r"^[a-z_]+\.[a-z_]+$")
_GOAL_KIND_PATTERN: Final = re.compile(r"^[a-z_]{3,32}$")
_NEARBY_KIND_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_.\-]{0,31}$")
_NUMBER_NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{1,47}$")

#: Prefixes a code-sourced rule's ``source_detail`` repo path may carry: the
#: shipped packages and the mod tree are where enforceable behaviour lives.
_REPO_PATH_PREFIXES: Final = ("packages/", "pz-mod/")

#: Prefix every ``verified_script`` proof must carry: the status claims the
#: behaviour is pinned by this repository's tests, so the pointer must be one.
_TEST_PATH_PREFIX: Final = "tests/"

#: What counts as a live evidence pointer for ``verified_live``: a livetest
#: scenario id, or a path into the evidence trees.
_LIVE_SCENARIO_PATTERN: Final = re.compile(r"^S[0-9]{2}$")
_LIVE_EVIDENCE_PREFIXES: Final = ("docs/evidence/", "evidence/")

#: Scalar string bounds, from the schema's ``minLength``/``maxLength``.
_CLAIM_BOUNDS: Final = (10, 300)
_DECISION_BOUNDS: Final = (10, 400)
_ON_FAILURE_BOUNDS: Final = (5, 300)
_CONDITION_BOUNDS: Final = (5, 200)
_PROVEN_BY_BOUNDS: Final = (3, 200)
_SOURCE_DETAIL_MAX: Final = 200
_UNIT_MAX: Final = 32


@unique
class KnowledgeDomain(StrEnum):
    """The knowledge areas one document may cover. Mirror of the schema enum."""

    MOVEMENT = "movement"
    DOORS_WINDOWS = "doors_windows"
    CONTAINERS_LOOT = "containers_loot"
    INVENTORY_EQUIPMENT = "inventory_equipment"
    FOOD_WATER = "food_water"
    MEDICAL = "medical"
    REST_SLEEP = "rest_sleep"
    THREAT_COMBAT = "threat_combat"
    CLOTHING_PROTECTION = "clothing_protection"
    LITERATURE_SKILLS = "literature_skills"
    CRAFTING = "crafting"
    BUILDING = "building"
    UTILITIES_SURVIVAL = "utilities_survival"
    MEMORY_PLACES = "memory_places"


@unique
class KnowledgeStatus(StrEnum):
    """How far a claim has been verified. Mirror of the schema enum."""

    VERIFIED_LIVE = "verified_live"
    VERIFIED_SCRIPT = "verified_script"
    UNVERIFIED = "unverified"


@unique
class KnowledgeSource(StrEnum):
    """Where a claim comes from, in the priority order the schema documents."""

    CODE = "code"
    LIVE_PROBE = "live_probe"
    OFFICIAL = "official"
    WIKI = "wiki"


@unique
class NeedChannel(StrEnum):
    """Need channels a rule may speak to, for bounded retrieval."""

    HUNGER = "hunger"
    THIRST = "thirst"
    FATIGUE = "fatigue"
    ENDURANCE = "endurance"
    BLEEDING = "bleeding"
    PAIN = "pain"
    SICKNESS = "sickness"
    TEMPERATURE = "temperature"
    PANIC = "panic"
    ENCUMBRANCE = "encumbrance"
    BOREDOM = "boredom"
    WETNESS = "wetness"


@unique
class KnowledgeRefusal(StrEnum):
    """Why a document or a rule was refused. One code per distinct gate.

    ``SCHEMA_MISMATCH`` covers the structural failures — a missing field, a
    broken pattern, an exceeded bound. The honesty gates each carry their own
    code because a planner-facing tool wants to distinguish "the YAML is
    malformed" from "the YAML claims more than its evidence".
    """

    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    UNKNOWN_ACTION = "UNKNOWN_ACTION"
    UNKNOWN_GOAL_KIND = "UNKNOWN_GOAL_KIND"
    VERIFIED_SCRIPT_WITHOUT_CODE_SOURCE = "VERIFIED_SCRIPT_WITHOUT_CODE_SOURCE"
    VERIFIED_SCRIPT_WITHOUT_TESTS = "VERIFIED_SCRIPT_WITHOUT_TESTS"
    VERIFIED_LIVE_WITHOUT_POINTER = "VERIFIED_LIVE_WITHOUT_POINTER"
    SOURCE_DETAIL_NOT_REPO_PATH = "SOURCE_DETAIL_NOT_REPO_PATH"
    DEAD_PROOF_PATH = "DEAD_PROOF_PATH"
    DEAD_SOURCE_PATH = "DEAD_SOURCE_PATH"
    DUPLICATE_RULE_ID = "DUPLICATE_RULE_ID"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    TOO_MANY_DOCUMENTS = "TOO_MANY_DOCUMENTS"
    UNREADABLE_FILE = "UNREADABLE_FILE"
    PARSE_FAILED = "PARSE_FAILED"
    NOT_A_MAPPING = "NOT_A_MAPPING"
    YAML_UNAVAILABLE = "YAML_UNAVAILABLE"


class KnowledgeValidationError(ValueError):
    """One rule or document failed validation.

    ``rule_id`` is filled only when the offending rule's id itself passed the
    id pattern — an unvalidated id is free text and stays out of the message.
    """

    def __init__(
        self,
        refusal: KnowledgeRefusal,
        message: str,
        *,
        rule_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.refusal = refusal
        self.rule_id = rule_id


def _refuse(refusal: KnowledgeRefusal, message: str, *, rule_id: str | None = None) -> NoReturn:
    raise KnowledgeValidationError(refusal, message, rule_id=rule_id)


#: Bound for the enum readers below; Python 3.11 has no PEP 695 syntax yet.
E = TypeVar("E", bound=StrEnum)


def _check_str(name: str, value: str, bounds: tuple[int, int], *, rule_id: str | None) -> None:
    low, high = bounds
    if not low <= len(value) <= high:
        _refuse(
            KnowledgeRefusal.SCHEMA_MISMATCH,
            f"{name} must be {low}..{high} characters, got {len(value)}",
            rule_id=rule_id,
        )


def _check_items(
    name: str,
    values: tuple[str, ...],
    limit: int,
    *,
    rule_id: str | None,
    pattern: re.Pattern[str] | None = None,
    bounds: tuple[int, int] | None = None,
) -> None:
    if len(values) > limit:
        _refuse(
            KnowledgeRefusal.SCHEMA_MISMATCH,
            f"{name} holds {len(values)} entries, above the schema's {limit}",
            rule_id=rule_id,
        )
    for index, value in enumerate(values):
        if pattern is not None and pattern.fullmatch(value) is None:
            _refuse(
                KnowledgeRefusal.SCHEMA_MISMATCH,
                f"{name}[{index}] does not match the schema pattern",
                rule_id=rule_id,
            )
        if bounds is not None:
            _check_str(f"{name}[{index}]", value, bounds, rule_id=rule_id)


def _read_str(payload: Mapping[str, object], key: str, *, rule_id: str | None) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        _refuse(
            KnowledgeRefusal.SCHEMA_MISMATCH,
            f"{key} must be a string and is required",
            rule_id=rule_id,
        )
    return value


def _read_str_tuple(
    payload: Mapping[str, object], key: str, *, rule_id: str | None, required: bool
) -> tuple[str, ...]:
    value = payload.get(key)
    if value is None and not required:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _refuse(
            KnowledgeRefusal.SCHEMA_MISMATCH,
            f"{key} must be a list of strings",
            rule_id=rule_id,
        )
    return tuple(value)


@dataclass(frozen=True, slots=True)
class KnowledgeNumber:
    """One named numeric fact, carrying its own honesty status.

    A number's status is independent of its rule's: a ``verified_script`` rule
    may cite an ``unverified`` game-side number, and the split is the point —
    the planner may trust the refusal without trusting the folklore behind it.
    """

    name: str
    value: float
    status: KnowledgeStatus
    unit: str = ""

    def __post_init__(self) -> None:
        if _NUMBER_NAME_PATTERN.fullmatch(self.name) is None:
            _refuse(KnowledgeRefusal.SCHEMA_MISMATCH, "a number name fails the schema pattern")
        if not math.isfinite(self.value):
            _refuse(
                KnowledgeRefusal.SCHEMA_MISMATCH,
                f"number {self.name} must be finite",
            )
        if len(self.unit) > _UNIT_MAX:
            _refuse(
                KnowledgeRefusal.SCHEMA_MISMATCH,
                f"number {self.name} unit must be at most {_UNIT_MAX} characters",
            )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object], *, rule_id: str | None) -> KnowledgeNumber:
        _check_keys(payload, _NUMBER_KEYS, context="numbers[]", rule_id=rule_id)
        name = _read_str(payload, "name", rule_id=rule_id)
        raw_value = payload.get("value")
        if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
            _refuse(
                KnowledgeRefusal.SCHEMA_MISMATCH,
                "a number value must be numeric",
                rule_id=rule_id,
            )
        status = _read_enum(payload, "status", KnowledgeStatus, rule_id=rule_id)
        unit = payload.get("unit", "")
        if not isinstance(unit, str):
            _refuse(KnowledgeRefusal.SCHEMA_MISMATCH, "a number unit must be a string")
        return cls(name=name, value=float(raw_value), status=status, unit=unit)

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "value": self.value,
            "status": self.status.value,
        }
        if self.unit:
            payload["unit"] = self.unit
        return payload


#: The exact key sets the schema allows (``additionalProperties: false``).
_NUMBER_KEYS: Final = frozenset({"name", "value", "unit", "status"})
_RULE_REQUIRED_KEYS: Final = frozenset(
    {
        "id",
        "build",
        "source",
        "status",
        "claim",
        "observed_inputs",
        "preconditions",
        "decision",
        "actions",
        "postconditions",
        "on_failure",
        "risk_class",
        "proven_by",
    }
)
_RULE_KEYS: Final = _RULE_REQUIRED_KEYS | frozenset(
    {"source_detail", "goal_kinds", "needs", "nearby_kinds", "numbers"}
)
_DOCUMENT_KEYS: Final = frozenset({"schema_version", "domain", "rules"})


def _check_keys(
    payload: Mapping[str, object],
    allowed: frozenset[str],
    *,
    context: str,
    rule_id: str | None,
) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        # Key names are attacker-controlled free text; the count is not.
        _refuse(
            KnowledgeRefusal.SCHEMA_MISMATCH,
            f"{context} carries {len(unknown)} keys the schema does not allow",
            rule_id=rule_id,
        )


def _read_enum(
    payload: Mapping[str, object],
    key: str,
    enum_type: type[E],
    *,
    rule_id: str | None,
) -> E:
    value = _read_str(payload, key, rule_id=rule_id)
    try:
        return enum_type(value)
    except ValueError:
        _refuse(
            KnowledgeRefusal.SCHEMA_MISMATCH,
            f"{key} is not a member of the closed {enum_type.__name__} set",
            rule_id=rule_id,
        )


@dataclass(frozen=True, slots=True)
class KnowledgeRule:
    """One gameplay rule, exactly as the schema shapes it.

    The evidence-side gates that need no filesystem run in ``__post_init__``:
    a ``verified_script`` rule must be code-sourced with test-path proofs, a
    ``verified_live`` rule must name a live pointer. The gates that touch the
    disk — do those proof paths exist — belong to the loader, which knows the
    repository root.
    """

    id: str
    build: str
    source: KnowledgeSource
    status: KnowledgeStatus
    claim: str
    observed_inputs: tuple[str, ...]
    preconditions: tuple[str, ...]
    decision: str
    actions: tuple[ActionName, ...]
    postconditions: tuple[str, ...]
    on_failure: str
    risk_class: RiskClass
    proven_by: tuple[str, ...]
    source_detail: str = ""
    goal_kinds: tuple[GoalKind, ...] = ()
    needs: tuple[NeedChannel, ...] = ()
    nearby_kinds: tuple[str, ...] = ()
    numbers: tuple[KnowledgeNumber, ...] = ()

    def __post_init__(self) -> None:
        if _ID_PATTERN.fullmatch(self.id) is None:
            _refuse(KnowledgeRefusal.SCHEMA_MISMATCH, "a rule id fails the schema id pattern")
        rid = self.id
        if _BUILD_PATTERN.fullmatch(self.build) is None:
            _refuse(
                KnowledgeRefusal.SCHEMA_MISMATCH,
                "build does not match the schema pattern",
                rule_id=rid,
            )
        _check_str("claim", self.claim, _CLAIM_BOUNDS, rule_id=rid)
        _check_str("decision", self.decision, _DECISION_BOUNDS, rule_id=rid)
        _check_str("on_failure", self.on_failure, _ON_FAILURE_BOUNDS, rule_id=rid)
        if len(self.source_detail) > _SOURCE_DETAIL_MAX:
            _refuse(
                KnowledgeRefusal.SCHEMA_MISMATCH,
                f"source_detail must be at most {_SOURCE_DETAIL_MAX} characters",
                rule_id=rid,
            )
        _check_items(
            "observed_inputs",
            self.observed_inputs,
            MAX_OBSERVED_INPUTS,
            rule_id=rid,
            pattern=_OBSERVED_INPUT_PATTERN,
        )
        _check_items(
            "preconditions",
            self.preconditions,
            MAX_PRECONDITIONS,
            rule_id=rid,
            bounds=_CONDITION_BOUNDS,
        )
        _check_items(
            "postconditions",
            self.postconditions,
            MAX_POSTCONDITIONS,
            rule_id=rid,
            bounds=_CONDITION_BOUNDS,
        )
        _check_items(
            "nearby_kinds",
            self.nearby_kinds,
            MAX_NEARBY_KINDS,
            rule_id=rid,
            pattern=_NEARBY_KIND_PATTERN,
        )
        _check_items(
            "proven_by", self.proven_by, MAX_PROVEN_BY, rule_id=rid, bounds=_PROVEN_BY_BOUNDS
        )
        if len(self.actions) > MAX_ACTIONS:
            _refuse(
                KnowledgeRefusal.SCHEMA_MISMATCH,
                f"actions holds {len(self.actions)} entries, above the schema's {MAX_ACTIONS}",
                rule_id=rid,
            )
        if len(self.goal_kinds) > MAX_GOAL_KINDS:
            _refuse(
                KnowledgeRefusal.SCHEMA_MISMATCH,
                f"goal_kinds holds {len(self.goal_kinds)} entries, above the "
                f"schema's {MAX_GOAL_KINDS}",
                rule_id=rid,
            )
        if len(self.needs) > MAX_NEEDS:
            _refuse(
                KnowledgeRefusal.SCHEMA_MISMATCH,
                f"needs holds {len(self.needs)} entries, above the schema's {MAX_NEEDS}",
                rule_id=rid,
            )
        if len(self.numbers) > MAX_NUMBERS:
            _refuse(
                KnowledgeRefusal.SCHEMA_MISMATCH,
                f"numbers holds {len(self.numbers)} entries, above the schema's {MAX_NUMBERS}",
                rule_id=rid,
            )
        self._check_honesty(rid)

    def _check_honesty(self, rid: str) -> None:
        """The evidence gates that need no disk access."""
        if self.source is KnowledgeSource.CODE and not self.source_detail.startswith(
            _REPO_PATH_PREFIXES
        ):
            _refuse(
                KnowledgeRefusal.SOURCE_DETAIL_NOT_REPO_PATH,
                "a code-sourced rule must name a repository path "
                f"({' or '.join(_REPO_PATH_PREFIXES)}) in source_detail",
                rule_id=rid,
            )
        if self.status is KnowledgeStatus.VERIFIED_SCRIPT:
            if self.source is not KnowledgeSource.CODE:
                _refuse(
                    KnowledgeRefusal.VERIFIED_SCRIPT_WITHOUT_CODE_SOURCE,
                    f"status {self.status.value} requires source "
                    f"{KnowledgeSource.CODE.value}, got {self.source.value}",
                    rule_id=rid,
                )
            if not self.proven_by or any(
                not proof.startswith(_TEST_PATH_PREFIX) for proof in self.proven_by
            ):
                _refuse(
                    KnowledgeRefusal.VERIFIED_SCRIPT_WITHOUT_TESTS,
                    f"status {self.status.value} requires proven_by to name only "
                    f"{_TEST_PATH_PREFIX}* paths, at least one",
                    rule_id=rid,
                )
        if self.status is KnowledgeStatus.VERIFIED_LIVE and not any(
            _LIVE_SCENARIO_PATTERN.fullmatch(proof) is not None
            or proof.startswith(_LIVE_EVIDENCE_PREFIXES)
            for proof in self.proven_by
        ):
            _refuse(
                KnowledgeRefusal.VERIFIED_LIVE_WITHOUT_POINTER,
                f"status {self.status.value} requires a live pointer in proven_by: "
                "a Snn scenario id or a path under "
                f"{' or '.join(_LIVE_EVIDENCE_PREFIXES)}",
                rule_id=rid,
            )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> KnowledgeRule:
        """Read one rule out of a parsed YAML mapping, refusing what drifts."""
        raw_id = payload.get("id")
        rule_id = raw_id if isinstance(raw_id, str) and _ID_PATTERN.fullmatch(raw_id) else None
        _check_keys(payload, _RULE_KEYS, context="a rule", rule_id=rule_id)
        missing = sorted(_RULE_REQUIRED_KEYS - set(payload))
        if missing:
            _refuse(
                KnowledgeRefusal.SCHEMA_MISMATCH,
                "a rule is missing required fields: " + ", ".join(missing),
                rule_id=rule_id,
            )
        source_detail = payload.get("source_detail", "")
        if not isinstance(source_detail, str):
            _refuse(
                KnowledgeRefusal.SCHEMA_MISMATCH,
                "source_detail must be a string",
                rule_id=rule_id,
            )
        raw_numbers = payload.get("numbers", [])
        if not isinstance(raw_numbers, list) or any(
            not isinstance(entry, Mapping) for entry in raw_numbers
        ):
            _refuse(
                KnowledgeRefusal.SCHEMA_MISMATCH,
                "numbers must be a list of mappings",
                rule_id=rule_id,
            )
        return cls(
            id=_read_str(payload, "id", rule_id=rule_id),
            build=_read_str(payload, "build", rule_id=rule_id),
            source=_read_enum(payload, "source", KnowledgeSource, rule_id=rule_id),
            status=_read_enum(payload, "status", KnowledgeStatus, rule_id=rule_id),
            claim=_read_str(payload, "claim", rule_id=rule_id),
            observed_inputs=_read_str_tuple(
                payload, "observed_inputs", rule_id=rule_id, required=True
            ),
            preconditions=_read_str_tuple(payload, "preconditions", rule_id=rule_id, required=True),
            decision=_read_str(payload, "decision", rule_id=rule_id),
            actions=_read_actions(payload, rule_id=rule_id),
            postconditions=_read_str_tuple(
                payload, "postconditions", rule_id=rule_id, required=True
            ),
            on_failure=_read_str(payload, "on_failure", rule_id=rule_id),
            risk_class=_read_enum(payload, "risk_class", RiskClass, rule_id=rule_id),
            proven_by=_read_str_tuple(payload, "proven_by", rule_id=rule_id, required=True),
            source_detail=source_detail,
            goal_kinds=_read_goal_kinds(payload, rule_id=rule_id),
            needs=tuple(
                _to_enum_tuple(
                    _read_str_tuple(payload, "needs", rule_id=rule_id, required=False),
                    NeedChannel,
                    field_name="needs",
                    rule_id=rule_id,
                )
            ),
            nearby_kinds=_read_str_tuple(payload, "nearby_kinds", rule_id=rule_id, required=False),
            numbers=tuple(
                KnowledgeNumber.from_mapping(entry, rule_id=rule_id) for entry in raw_numbers
            ),
        )

    def as_dict(self) -> dict[str, object]:
        """The YAML shape back, optional keys present only when non-empty."""
        payload: dict[str, object] = {
            "id": self.id,
            "build": self.build,
            "source": self.source.value,
            "status": self.status.value,
            "claim": self.claim,
            "observed_inputs": list(self.observed_inputs),
            "preconditions": list(self.preconditions),
            "decision": self.decision,
            "actions": [action.value for action in self.actions],
            "postconditions": list(self.postconditions),
            "on_failure": self.on_failure,
            "risk_class": self.risk_class.value,
            "proven_by": list(self.proven_by),
        }
        if self.source_detail:
            payload["source_detail"] = self.source_detail
        if self.goal_kinds:
            payload["goal_kinds"] = [kind.value for kind in self.goal_kinds]
        if self.needs:
            payload["needs"] = [need.value for need in self.needs]
        if self.nearby_kinds:
            payload["nearby_kinds"] = list(self.nearby_kinds)
        if self.numbers:
            payload["numbers"] = [number.as_dict() for number in self.numbers]
        return payload


def _read_actions(payload: Mapping[str, object], *, rule_id: str | None) -> tuple[ActionName, ...]:
    values = _read_str_tuple(payload, "actions", rule_id=rule_id, required=True)
    actions: list[ActionName] = []
    for value in values:
        if _ACTION_PATTERN.fullmatch(value) is None:
            _refuse(
                KnowledgeRefusal.SCHEMA_MISMATCH,
                "an actions entry does not match the schema pattern",
                rule_id=rule_id,
            )
        try:
            actions.append(ActionName(value))
        except ValueError:
            # The token passed the closed pattern above, so echoing it is
            # bounded: lowercase words and one dot, nothing an id cannot say.
            _refuse(
                KnowledgeRefusal.UNKNOWN_ACTION,
                f"actions names {value}, which is not an ActionName the protocol carries",
                rule_id=rule_id,
            )
    return tuple(actions)


def _read_goal_kinds(payload: Mapping[str, object], *, rule_id: str | None) -> tuple[GoalKind, ...]:
    values = _read_str_tuple(payload, "goal_kinds", rule_id=rule_id, required=False)
    kinds: list[GoalKind] = []
    for value in values:
        if _GOAL_KIND_PATTERN.fullmatch(value) is None:
            _refuse(
                KnowledgeRefusal.SCHEMA_MISMATCH,
                "a goal_kinds entry does not match the schema pattern",
                rule_id=rule_id,
            )
        try:
            kinds.append(GoalKind(value))
        except ValueError:
            _refuse(
                KnowledgeRefusal.UNKNOWN_GOAL_KIND,
                f"goal_kinds names {value}, which is not a GoalKind the goal channel carries",
                rule_id=rule_id,
            )
    return tuple(kinds)


def _to_enum_tuple(
    values: tuple[str, ...],
    enum_type: type[E],
    *,
    field_name: str,
    rule_id: str | None,
) -> list[E]:
    members: list[E] = []
    for value in values:
        try:
            members.append(enum_type(value))
        except ValueError:
            _refuse(
                KnowledgeRefusal.SCHEMA_MISMATCH,
                f"a {field_name} entry is not a member of the closed {enum_type.__name__} set",
                rule_id=rule_id,
            )
    return members


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    """One ``knowledge/gameplay/*.yaml`` file, parsed and validated."""

    domain: KnowledgeDomain
    rules: tuple[KnowledgeRule, ...]
    schema_version: str = field(default=SCHEMA_VERSION)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            _refuse(
                KnowledgeRefusal.SCHEMA_MISMATCH,
                f"schema_version must be the const {SCHEMA_VERSION}",
            )
        if not 1 <= len(self.rules) <= MAX_RULES_PER_DOCUMENT:
            _refuse(
                KnowledgeRefusal.SCHEMA_MISMATCH,
                f"a document must carry 1..{MAX_RULES_PER_DOCUMENT} rules, got {len(self.rules)}",
            )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> KnowledgeDocument:
        _check_keys(payload, _DOCUMENT_KEYS, context="a document", rule_id=None)
        missing = sorted(_DOCUMENT_KEYS - set(payload))
        if missing:
            _refuse(
                KnowledgeRefusal.SCHEMA_MISMATCH,
                "a document is missing required fields: " + ", ".join(missing),
            )
        version = _read_str(payload, "schema_version", rule_id=None)
        domain = _read_enum(payload, "domain", KnowledgeDomain, rule_id=None)
        raw_rules = payload.get("rules")
        if not isinstance(raw_rules, list) or any(
            not isinstance(entry, Mapping) for entry in raw_rules
        ):
            _refuse(KnowledgeRefusal.SCHEMA_MISMATCH, "rules must be a list of mappings")
        if len(raw_rules) > MAX_RULES_PER_DOCUMENT:
            # Refused before per-rule parsing so an oversized document cannot
            # buy MAX_RULES_PER_DOCUMENT-plus validations of attacker payload.
            _refuse(
                KnowledgeRefusal.SCHEMA_MISMATCH,
                f"a document must carry 1..{MAX_RULES_PER_DOCUMENT} rules, got {len(raw_rules)}",
            )
        rules = tuple(KnowledgeRule.from_mapping(entry) for entry in raw_rules)
        return cls(domain=domain, rules=rules, schema_version=version)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "domain": self.domain.value,
            "rules": [rule.as_dict() for rule in self.rules],
        }
