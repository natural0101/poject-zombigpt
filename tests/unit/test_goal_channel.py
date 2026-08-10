"""The typed goal channel: closedness, bounds, idempotency and the stop levers.

The tests here are written against hand-written literals wherever a shape is
load-bearing. A round trip through the module's own tables would pass with the
tables consistently wrong — a goal kind mapped to the wrong planner kind, a
range table whose entries were swapped, a digest function replaced by
``str.__len__`` — so the expected values are spelled out rather than derived.

Four groups of test carry most of the weight:

* ``TestNoFreeText`` and ``TestRefusalHygiene``, which are the point of the
  package: nothing a microphone produced becomes a value the core acts on, and
  no caller-supplied byte ever reaches a message that will land in a traceback.
* ``TestTypedParameterSurface`` and ``TestEveryNumericParameterIsRanged``, which
  say the same thing *structurally*: they walk every parameter every kind
  declares, resolve its annotation to a real type, and refuse a free-form
  string, a mapping, or a number with no declared range. A test that named the
  four parameters that exist today would prove nothing about the fifth, and the
  fifth is the one that will be added by somebody in a hurry.
* ``TestWindowsCoarseClock``, which builds the shipping platform's clock
  explicitly. Windows advances its wall clock in ~15.6 ms granules, so two
  events in one granule share a timestamp and an ``>`` expiry check silently
  grants every goal an extra granule. Neither defect is visible against Linux's
  microsecond clock, so the granule is injected rather than waited for.
* ``TestTerminalStates``, which walks each of the four ways a goal ends and
  pins the reason code each one reports.
"""

from __future__ import annotations

import math
import re
import sys
from collections.abc import Callable, Iterator
from dataclasses import FrozenInstanceError, fields, replace
from enum import Enum
from types import NoneType
from typing import Any, get_args, get_type_hints

import pytest

from pz_agent_core.actions.adapters import movement
from pz_agent_core.goals import (
    GOAL_SPECS,
    MAX_DETAIL_CHARS,
    MAX_EVIDENCE_KEYS,
    MAX_GOAL_STEPS,
    MAX_GOAL_WALL_MS,
    MAX_IDEMPOTENCY_KEY_LEN,
    MAX_PARSED_TOKEN_CHARS,
    MAX_PENDING_TTL_MS,
    MAX_RENDERED_VALUE_CHARS,
    MAX_SKILL_LEVEL,
    MIN_GOAL_WALL_MS,
    NUMERIC_RANGES,
    PARAM_NAMES,
    TERMINAL_GOAL_STATES,
    GoalAdmission,
    GoalBudget,
    GoalKind,
    GoalParams,
    GoalQueue,
    GoalRecord,
    GoalRefusal,
    GoalRequest,
    GoalSpec,
    GoalState,
    GoalTransition,
    NumericRange,
    TrainableSkill,
    UnknownGoalError,
    key_digest,
    normalise_evidence_keys,
    parse_kind,
    parse_skill,
    to_planner_goal,
)
from pz_agent_core.goals import model as goal_model
from pz_agent_core.planner.plan import MAX_PLAN_STEPS
from pz_agent_core.protocol import ActionResult, ActionStatus, ReasonCode

# --------------------------------------------------------------------------
# doubles
# --------------------------------------------------------------------------


class FakeClock:
    """A wall clock the test drives by hand, including backwards."""

    def __init__(self, start: int = 0) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> int:
        self.now += ms
        return self.now


class WindowsClock:
    """The shipping platform's wall clock, at the resolution it actually has.

    ``GetSystemTimeAsFileTime`` — which is what CPython's ``time.time_ns()``
    reaches on Windows unless the process opts into the precise variant — ticks
    at the system timer interval, ~15.625 ms by default. Every value this clock
    returns is therefore a multiple of that granule, which is the shape a Linux
    test never produces on its own.
    """

    GRANULE_US: int = 15_625

    def __init__(self, start_granules: int = 0) -> None:
        self.granules = start_granules

    def __call__(self) -> int:
        return (self.granules * self.GRANULE_US) // 1_000

    def advance_granules(self, count: int) -> int:
        self.granules += count
        return self()


def make_queue(
    clock: Callable[[], int],
    *,
    max_open: int = 4,
    max_remembered: int = 32,
    armed: bool = True,
) -> GoalQueue:
    return GoalQueue(clock=clock, max_open=max_open, max_remembered=max_remembered, armed=armed)


def eat(key: str = "eat-1", **params: object) -> GoalRequest:
    return GoalRequest(
        kind=GoalKind.SATISFY_HUNGER,
        idempotency_key=key,
        params=GoalParams(**params),  # type: ignore[arg-type]
    )


def observed(evidence: dict[str, object] | None = None) -> ActionResult:
    """A success ack built the only way the protocol permits one to be built."""
    return ActionResult.succeeded(
        session_id="s-1",
        seq=7,
        command_id="c-1",
        action="consume.eat",
        timestamp_ms=1_000,
        evidence=dict(evidence if evidence is not None else {"hunger": 0.1}),
    )


def start_one(queue: GoalQueue, request: GoalRequest | None = None) -> GoalRecord:
    """Submit and activate one goal, asserting both steps were accepted."""
    admission = queue.submit(request if request is not None else eat())
    assert admission.goal is not None, admission.refusal
    started = queue.activate_next()
    assert started.goal is not None, started.refusal
    return started.goal


def params_for(kind: GoalKind) -> GoalParams:
    """The smallest parameter set *kind* will accept."""
    if kind is GoalKind.TRAIN_SKILL:
        return GoalParams(skill=TrainableSkill.COOKING)
    if kind is GoalKind.NAVIGATE_TO:
        return GoalParams(target_x=1200, target_y=3400, target_z=0)
    return GoalParams()


def a_record(**overrides: Any) -> GoalRecord:
    """A minimal, valid pending record, with *overrides* applied.

    Written out here because a dozen invariant tests each need one field of a
    record to be wrong and every other field to be right.
    """
    base: dict[str, Any] = {
        "goal_id": "g",
        "kind": GoalKind.SATISFY_HUNGER,
        "params": GoalParams(),
        "budget": GOAL_SPECS[GoalKind.SATISFY_HUNGER].budget,
        "key_digest": "0" * 16,
        "sequence": 1,
        "state": GoalState.PENDING,
        "submitted_at_ms": 0,
    }
    return GoalRecord(**{**base, **overrides})


# --------------------------------------------------------------------------
# reading the parameter surface structurally
# --------------------------------------------------------------------------

#: How many members a vocabulary this module calls "closed" may have. Not a
#: technical limit — a bound on what one reviewer can hold in their head when
#: the question is "is every one of these safe to act on".
MAX_CLOSED_VOCABULARY: int = 32

#: The string parameters that are token *lists* rather than enum members —
#: closed all the same, because the constructor resolves every comma-joined
#: token against a closed enum and refuses the rest. Each entry here must have
#: its closure proven behaviourally in :class:`TestLootParameters`; the
#: structural walk exempts exactly these names and nothing else.
CLOSED_TOKEN_LIST_PARAMS: frozenset[str] = frozenset({"categories"})


def atoms(annotation: object) -> tuple[type, ...]:
    """The concrete types in a resolved annotation, with ``None`` dropped."""
    parts = get_args(annotation) or (annotation,)
    return tuple(part for part in parts if part is not NoneType and isinstance(part, type))


def param_types() -> dict[str, tuple[type, ...]]:
    """Every :class:`GoalParams` field, resolved to real types.

    ``get_type_hints`` rather than ``field.type``: the module uses
    ``from __future__ import annotations``, so the dataclass keeps the source
    text, and a test that walked the text would be reading a string that says
    ``"int | None"`` rather than a type it can ask questions of.
    """
    return {name: atoms(hint) for name, hint in get_type_hints(GoalParams).items()}


def wants_whole_numbers(name: str) -> bool:
    return int in param_types()[name]


def edge_values(name: str) -> tuple[object, object]:
    """The two values a numeric parameter's declared range still admits."""
    span = NUMERIC_RANGES[name]
    if wants_whole_numbers(name):
        return int(span.minimum), int(span.maximum)
    return span.minimum, span.maximum


def outside_values(name: str) -> tuple[object, object]:
    """One value below the declared range and one above it."""
    span = NUMERIC_RANGES[name]
    if wants_whole_numbers(name):
        return int(span.minimum) - 1, int(span.maximum) + 1
    return span.minimum - 1.0, span.maximum + 1.0


# --------------------------------------------------------------------------
# the closed vocabulary
# --------------------------------------------------------------------------


class TestClosedVocabulary:
    def test_goal_kinds_are_exactly_these_seven(self) -> None:
        # Hand-written. Adding a kind is a reviewed change to three tables, and
        # this literal is the thing that makes the review happen.
        assert {k.value for k in GoalKind} == {
            "satisfy_hunger",
            "satisfy_thirst",
            "read_for_boredom",
            "train_skill",
            "learn_recipe",
            "navigate_to",
            "loot_area",
        }

    def test_trainable_skills_are_exactly_these_eleven(self) -> None:
        assert {s.value for s in TrainableSkill} == {
            "carpentry",
            "cooking",
            "farming",
            "electrical",
            "metalworking",
            "mechanics",
            "tailoring",
            "foraging",
            "fishing",
            "trapping",
            "first_aid",
        }

    def test_goal_states_and_the_terminal_subset(self) -> None:
        assert {s.value for s in GoalState} == {
            "pending",
            "active",
            "succeeded",
            "failed",
            "cancelled",
            "expired",
        }
        assert {s.value for s in TERMINAL_GOAL_STATES} == {
            "succeeded",
            "failed",
            "cancelled",
            "expired",
        }
        assert GoalState.PENDING.is_terminal is False
        assert GoalState.ACTIVE.is_terminal is False
        assert GoalState.EXPIRED.is_terminal is True

    @pytest.mark.parametrize(
        "unknown",
        [
            "satisfy_boredom",
            "SATISFY_HUNGER",
            "satisfy_hunger ",
            "satisfy_hunger\n",
            "",
            "eat",
        ],
    )
    def test_the_enum_refuses_an_unknown_value_instead_of_coercing_it(self, unknown: str) -> None:
        # ``GoalKind(x)`` is the other route a string could take into the enum,
        # and it has to be as closed as ``parse_kind``: near-misses in case or
        # whitespace are refusals, not members. ``parse_kind`` folds case and
        # strips deliberately; the constructor does neither.
        with pytest.raises(ValueError, match="is not a valid"):
            GoalKind(unknown)
        assert len(GoalKind) == 7

    def test_no_lookup_hook_invents_a_member(self) -> None:
        # ``_missing_`` is the one hook that can turn an unrecognised value into
        # a member — an implementation of it that fell back to a "generic" kind
        # would make every test above pass and the enum open anyway.
        assert GoalKind._missing_("satisfy_boredom") is None
        assert TrainableSkill._missing_("lockpicking") is None

    def test_an_unknown_kind_creates_no_goal(self) -> None:
        # The whole edge path for a transcript nobody's vocabulary contains.
        queue = make_queue(FakeClock())
        assert parse_kind("satisfy_boredom") is None
        with pytest.raises(ValueError, match="GoalKind"):
            GoalRequest(kind="satisfy_boredom", idempotency_key="k")  # type: ignore[arg-type]
        assert queue.open_count == 0
        assert queue.pending == ()

    def test_every_kind_has_a_spec_and_a_budget(self) -> None:
        assert set(GOAL_SPECS) == set(GoalKind)
        for kind, spec in GOAL_SPECS.items():
            assert spec.budget.max_steps >= 1, kind
            assert spec.budget.max_wall_ms >= MIN_GOAL_WALL_MS, kind

    def test_required_and_optional_parameters_per_kind(self) -> None:
        # Hand-written table. A swap between two kinds' rows round-trips through
        # the module perfectly and is caught only here.
        expected: dict[str, tuple[set[str], set[str]]] = {
            "satisfy_hunger": (set(), {"satisfy_to"}),
            "satisfy_thirst": (set(), {"satisfy_to"}),
            "read_for_boredom": (set(), {"pages"}),
            "train_skill": ({"skill"}, {"target_level", "pages"}),
            "learn_recipe": (set(), {"pages"}),
            "navigate_to": ({"target_x", "target_y", "target_z"}, set()),
            # Everything optional on purpose: the bare goal is «облутай
            # квартиру», and it means scope=room with useful_only selection.
            "loot_area": (set(), {"scope", "radius", "take_all", "categories"}),
        }
        actual = {
            kind.value: (set(spec.required), set(spec.optional))
            for kind, spec in GOAL_SPECS.items()
        }
        assert actual == expected


class TestNoFreeText:
    """A transcript must not become a string the core acts on."""

    def test_goal_params_declares_no_free_text_field(self) -> None:
        # ``from __future__ import annotations`` keeps these as source strings,
        # which is exactly what we want to pin: a new ``note: str`` field, or a
        # ``skill: str`` widening, fails here.
        annotations = {f.name: f.type for f in fields(GoalParams)}
        assert annotations == {
            "skill": "TrainableSkill | None",
            "target_level": "int | None",
            "satisfy_to": "float | None",
            "pages": "int | None",
            "target_x": "int | None",
            "target_y": "int | None",
            "target_z": "int | None",
            "scope": "LootScope | None",
            "radius": "int | None",
            "take_all": "bool | None",
            # A string field, and deliberately not free text: the constructor
            # runs it through parse_loot_categories, whose closed-token
            # behaviour the loot-parameter tests below pin. A widening of the
            # validation would not fail this line — it fails those.
            "categories": "str | None",
        }
        assert tuple(annotations) == PARAM_NAMES

    def test_goal_record_declares_no_free_text_parameter(self) -> None:
        names = {f.name for f in fields(GoalRecord)}
        assert "idempotency_key" not in names
        assert "transcript" not in names
        assert "key_digest" in names

    @pytest.mark.parametrize(
        "transcript",
        [
            "агент поешь пожалуйста",
            "satisfy_hunger; rm -rf /",
            "satisfy hunger",
            "",
            "eat",
        ],
    )
    def test_parse_kind_refuses_anything_that_is_not_a_member(self, transcript: str) -> None:
        assert parse_kind(transcript) is None

    def test_parse_kind_accepts_exactly_the_member_values(self) -> None:
        for kind in GoalKind:
            assert parse_kind(kind.value) is kind
            assert parse_kind(f"  {kind.value.upper()}  ") is kind

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("carpentry", TrainableSkill.CARPENTRY),
            ("Carpentry", TrainableSkill.CARPENTRY),
            ("first aid", TrainableSkill.FIRST_AID),
            ("First-Aid", TrainableSkill.FIRST_AID),
        ],
    )
    def test_parse_skill_folds_onto_members(self, raw: str, expected: TrainableSkill) -> None:
        assert parse_skill(raw) is expected

    @pytest.mark.parametrize("raw", ["lockpicking", "sprinting", "", "carpentry; drop table"])
    def test_parse_skill_refuses_everything_else(self, raw: str) -> None:
        assert parse_skill(raw) is None

    def test_parse_is_bounded_and_the_bound_bites(self) -> None:
        # Truncation happens before the fold, so the work is linear in
        # MAX_PARSED_TOKEN_CHARS however long the transcript is — and a token
        # pushed past that bound by padding does not match, which is the
        # observable consequence.
        assert parse_skill("carpentry" + "x" * 100_000) is None
        assert parse_kind("satisfy_hunger" + "!" * 100_000) is None
        assert parse_kind(" " * 64 + "satisfy_hunger") is None
        assert parse_skill(" " * 64 + "carpentry") is None
        # Just inside the bound, the same padding still resolves.
        assert parse_kind(" " * 40 + "satisfy_hunger") is GoalKind.SATISFY_HUNGER

    def test_a_raw_string_cannot_be_smuggled_in_as_a_skill(self) -> None:
        with pytest.raises(ValueError, match="TrainableSkill") as caught:
            GoalParams(skill="carpentry")  # type: ignore[arg-type]
        assert "carpentry" not in str(caught.value)

    def test_a_raw_string_cannot_be_smuggled_in_as_a_kind(self) -> None:
        with pytest.raises(ValueError, match="GoalKind"):
            GoalRequest(kind="satisfy_hunger", idempotency_key="k")  # type: ignore[arg-type]


class TestTypedParameterSurface:
    """Each kind's parameters, walked rather than spot-checked.

    The tests above name parameters one at a time, which proves things about the
    parameters that exist today and nothing at all about the sixth one somebody
    adds. These walk :data:`GOAL_SPECS` and the resolved annotations of
    :class:`GoalParams`, so a new kind, or a new field on an old kind, is
    included the moment it is declared.
    """

    def test_every_declared_parameter_is_a_typed_field_of_goal_params(self) -> None:
        typed = param_types()
        for kind, spec in GOAL_SPECS.items():
            for name in sorted(spec.required | spec.optional):
                assert name in typed, f"{kind.value} declares {name}, which is not a field"
                assert typed[name], f"{kind.value}.{name} resolves to no concrete type"

    def test_no_kind_declares_an_unbounded_string_parameter(self) -> None:
        # The structural form of the promise the package exists for. A ``str``
        # field would carry a transcript verbatim into the core; a ``StrEnum``
        # field is also a ``str`` subclass, and what makes it bounded is that
        # its value set is finite, short and reviewed. Anything else has to be
        # a boolean, a number with a declared finite range, or one of the
        # named token-list exceptions whose closure is proven behaviourally in
        # :class:`TestLootParameters` — an *exception list*, so a new ``str``
        # field still fails here until it is reviewed onto it.
        typed = param_types()
        for kind, spec in GOAL_SPECS.items():
            for name in sorted(spec.required | spec.optional):
                for atom in typed[name]:
                    where = f"{kind.value}.{name}"
                    if name in CLOSED_TOKEN_LIST_PARAMS:
                        assert atom in (str, NoneType), f"{where} widened past its tokens"
                        continue
                    assert atom is not str, f"{where} is free-form text"
                    assert atom not in (bytes, object), f"{where} is unbounded"
                    if issubclass(atom, str):
                        assert issubclass(atom, Enum), f"{where} is a string that is not closed"
                        members = list(atom)
                        assert 0 < len(members) <= MAX_CLOSED_VOCABULARY, where
                        for member in members:
                            assert len(str(member.value)) <= MAX_PARSED_TOKEN_CHARS, where
                    elif atom is bool:
                        # A boolean is the smallest closed vocabulary there is.
                        continue
                    else:
                        assert atom in (int, float), f"{where} is neither a number nor an enum"
                        span = NUMERIC_RANGES[name]
                        assert math.isfinite(span.minimum), where
                        assert math.isfinite(span.maximum), where

    @pytest.mark.parametrize("name", PARAM_NAMES)
    def test_no_parameter_accepts_a_string_at_all(self, name: str) -> None:
        # The behavioural half: whatever the annotation says, a string does not
        # get through — and the refusal does not carry the string back out.
        for candidate in ("carpentry", "7", "x" * 100_000):
            with pytest.raises(ValueError) as caught:
                GoalParams(**{name: candidate})  # type: ignore[arg-type]
            assert candidate not in str(caught.value)

    @pytest.mark.parametrize("kind", list(GoalKind))
    def test_no_kind_accepts_a_string_for_a_parameter_it_declares(self, kind: GoalKind) -> None:
        spec = GOAL_SPECS[kind]
        for name in sorted(spec.required | spec.optional):
            with pytest.raises(ValueError) as caught:
                GoalRequest(
                    kind=kind,
                    idempotency_key="k",
                    params=GoalParams(**{name: "x" * 10_000}),  # type: ignore[arg-type]
                )
            assert "xxxx" not in str(caught.value)

    def test_the_parameters_are_a_typed_object_and_not_a_bag(self) -> None:
        # A free-form dict is what this design is the alternative to, so the
        # absence of one is worth an assertion: no field of the request, the
        # parameters or the record is a mapping, and an unrecognised parameter
        # name is a TypeError from the constructor rather than a key nobody
        # reads.
        for holder in (GoalParams, GoalRequest, GoalRecord):
            for name, hint in get_type_hints(holder).items():
                for atom in atoms(hint):
                    assert not issubclass(atom, dict), f"{holder.__name__}.{name} is a bag"
        with pytest.raises(TypeError, match="unexpected keyword"):
            GoalParams(note="feed me")  # type: ignore[call-arg]

    def test_each_kind_gets_its_own_parameters_and_refuses_the_others(self) -> None:
        # "Its own" is the claim: a parameter one kind declares is refused by a
        # kind that does not, so the surface is per-kind rather than shared.
        samples: dict[str, object] = {
            "skill": TrainableSkill.COOKING,
            "target_level": 3,
            "satisfy_to": 0.5,
            "pages": 10,
            "target_x": 1200,
            "target_y": 3400,
            "target_z": 0,
            # scope=radius so that the sample set stays jointly valid: radius
            # is admissible only beside that scope, by loot_area's own rule.
            "scope": goal_model.LootScope.RADIUS,
            "radius": 5,
            "take_all": True,
            "categories": "food,medical",
        }
        assert set(samples) == set(PARAM_NAMES)
        for kind, spec in GOAL_SPECS.items():
            declared = spec.required | spec.optional
            for name in sorted(set(PARAM_NAMES) - declared):
                # Everything the kind requires, plus the one it does not take:
                # the refusal has to be about the extra parameter and not about
                # a missing one.
                supplied = {n: samples[n] for n in spec.required} | {name: samples[name]}
                with pytest.raises(ValueError, match=rf"{kind.value} takes no \['{name}'\]"):
                    GoalRequest(
                        kind=kind,
                        idempotency_key="k",
                        params=GoalParams(**supplied),  # type: ignore[arg-type]
                    )
            for name in sorted(spec.required):
                # Everything required except the one under test, so the
                # refusal names exactly the parameter that is missing.
                rest = {n: samples[n] for n in spec.required if n != name}
                with pytest.raises(ValueError, match=rf"{kind.value} requires \['{name}'\]"):
                    GoalRequest(
                        kind=kind,
                        idempotency_key="k",
                        params=GoalParams(**rest),  # type: ignore[arg-type]
                    )

    def test_the_parameters_a_kind_declares_are_accepted_together(self) -> None:
        # The other side of the previous test: a kind that refuses everything
        # would pass it, so every declared parameter is also exercised as
        # accepted, at a value inside its range.
        samples: dict[str, object] = {
            "skill": TrainableSkill.COOKING,
            "target_level": 3,
            "satisfy_to": 0.5,
            "pages": 10,
            "target_x": 1200,
            "target_y": 3400,
            "target_z": 0,
            # scope=radius so that the sample set stays jointly valid: radius
            # is admissible only beside that scope, by loot_area's own rule.
            "scope": goal_model.LootScope.RADIUS,
            "radius": 5,
            "take_all": True,
            "categories": "food,medical",
        }
        for kind, spec in GOAL_SPECS.items():
            declared = spec.required | spec.optional
            request = GoalRequest(
                kind=kind,
                idempotency_key="k",
                params=GoalParams(**{n: samples[n] for n in declared}),  # type: ignore[arg-type]
            )
            assert request.params.present() == frozenset(declared), kind


class TestLootParameters:
    """The seventh kind's parameter surface: closed tokens, honest pairings.

    ``categories`` is the one ``str`` field the channel carries, exempted from
    the structural string ban above on the strength of what this class proves:
    every token resolves against :class:`~pz_agent_core.loot.LootCategory` or
    the whole value is refused, bounded first, and never echoed back.
    """

    def test_scope_tokens_resolve_only_to_members(self) -> None:
        assert goal_model.parse_scope("room") is goal_model.LootScope.ROOM
        assert goal_model.parse_scope("  Building ") is goal_model.LootScope.BUILDING
        assert goal_model.parse_scope("RADIUS") is goal_model.LootScope.RADIUS
        assert goal_model.parse_scope("everywhere") is None
        assert goal_model.parse_scope("room" + "x" * 100_000) is None

    def test_a_raw_string_cannot_be_smuggled_in_as_a_scope(self) -> None:
        with pytest.raises(ValueError, match="LootScope") as caught:
            GoalParams(scope="room")  # type: ignore[arg-type]
        assert "room" not in str(caught.value).replace("LootScope", "")

    def test_categories_accepts_exactly_the_loot_category_tokens(self) -> None:
        parsed = goal_model.parse_loot_categories("food, WATER,medical")
        assert {member.value for member in parsed} == {"FOOD", "WATER", "MEDICAL"}
        request = GoalRequest(
            kind=GoalKind.LOOT_AREA,
            idempotency_key="k",
            params=GoalParams(categories="food,water"),
        )
        # Order-independent: the value is a set of reviewed tokens, not text.
        assert request.params.loot_categories() == goal_model.parse_loot_categories("water,food")

    @pytest.mark.parametrize(
        "raw",
        ["", "food,", "food,,water", "food,ammo", "еда", "food,food", "x" * 200],
        ids=["empty", "trailing", "double-comma", "unknown", "cyrillic", "repeat", "over-long"],
    )
    def test_categories_refuses_anything_else_without_echoing(self, raw: str) -> None:
        with pytest.raises(ValueError) as caught:
            GoalParams(categories=raw)
        if raw.strip():
            assert "ammo" not in str(caught.value)
            assert "еда" not in str(caught.value)

    def test_take_all_is_a_boolean_and_nothing_else(self) -> None:
        assert GoalParams(take_all=False).present() == {"take_all"}
        with pytest.raises(ValueError, match="true or false"):
            GoalParams(take_all=1)  # type: ignore[arg-type]

    def test_radius_without_its_scope_is_refused_at_the_door(self) -> None:
        # A radius the mission would silently ignore is a caller believing
        # they bounded a sweep that is actually scoped to a room.
        with pytest.raises(ValueError, match="scope=radius"):
            GoalRequest(
                kind=GoalKind.LOOT_AREA,
                idempotency_key="k",
                params=GoalParams(radius=5),
            )
        with pytest.raises(ValueError, match="scope=radius"):
            GoalRequest(
                kind=GoalKind.LOOT_AREA,
                idempotency_key="k",
                params=GoalParams(scope=goal_model.LootScope.ROOM, radius=5),
            )
        paired = GoalRequest(
            kind=GoalKind.LOOT_AREA,
            idempotency_key="k",
            params=GoalParams(scope=goal_model.LootScope.RADIUS, radius=5),
        )
        assert paired.params.present() == {"scope", "radius"}

    def test_the_bare_goal_is_admissible_with_no_parameters_at_all(self) -> None:
        # «облутай квартиру», the founding sentence: kind and key, nothing else.
        request = GoalRequest(kind=GoalKind.LOOT_AREA, idempotency_key="k")
        assert request.params.present() == frozenset()
        assert request.effective_budget == GOAL_SPECS[GoalKind.LOOT_AREA].budget


# --------------------------------------------------------------------------
# bounded parameters
# --------------------------------------------------------------------------


class TestNumericRanges:
    def test_declared_ranges_are_exactly_these(self) -> None:
        # Hand-written bounds. ``pages`` tracks the literature adapter's own
        # ceiling; if that moves, this fails and the two are reconciled on
        # purpose rather than by accident.
        assert {name: (r.minimum, r.maximum) for name, r in NUMERIC_RANGES.items()} == {
            "target_level": (1, 10),
            "satisfy_to": (0.0, 1.0),
            "pages": (1, 200),
            "target_x": (0, 32_000),
            "target_y": (0, 32_000),
            "target_z": (-32, 31),
            # The loot sweep. The ceiling deliberately equals the mod's own
            # single-move distance (MAX_MOVE_DISTANCE_SQUARES), pinned below.
            "radius": (1, 30),
        }

    def test_the_loot_radius_ceiling_is_the_single_move_distance(self) -> None:
        # Pinned by value rather than imported in the model, so the goal
        # channel keeps zero dependencies on the action layer's modules; this
        # is the reconciliation point when either number moves.
        assert goal_model.MAX_LOOT_RADIUS == movement.MAX_MOVE_DISTANCE_SQUARES
        assert goal_model.MIN_LOOT_RADIUS == 1

    def test_every_declared_numeric_parameter_has_a_range(self) -> None:
        declared: set[str] = set()
        for spec in GOAL_SPECS.values():
            declared |= spec.required | spec.optional
        # The non-numeric exemptions are each closed by their own check: the
        # two enums, the boolean, and the token-list string proven above.
        closed_otherwise = {"skill", "scope", "take_all"} | CLOSED_TOKEN_LIST_PARAMS
        assert declared - closed_otherwise <= set(NUMERIC_RANGES)

    @pytest.mark.parametrize("pages", [1, 2, 199, 200])
    def test_pages_inside_the_range_is_accepted(self, pages: int) -> None:
        assert GoalParams(pages=pages).pages == pages

    @pytest.mark.parametrize("pages", [0, -1, 201, 100_000])
    def test_pages_outside_the_range_is_refused(self, pages: int) -> None:
        with pytest.raises(ValueError, match=r"pages must be within 1(\.0)?\.\.200"):
            GoalParams(pages=pages)

    @pytest.mark.parametrize("level", [1, 5, 10])
    def test_target_level_inside_the_range(self, level: int) -> None:
        assert GoalParams(target_level=level).target_level == level

    @pytest.mark.parametrize("level", [0, -3, 11, 99])
    def test_target_level_outside_the_range(self, level: int) -> None:
        with pytest.raises(ValueError, match="target_level must be within"):
            GoalParams(target_level=level)

    @pytest.mark.parametrize("value", [0.0, 0.5, 1.0, 0])
    def test_satisfy_to_inside_the_range(self, value: float) -> None:
        assert GoalParams(satisfy_to=value).satisfy_to == value

    @pytest.mark.parametrize("value", [-0.000_1, 1.000_1, 2.0, -1])
    def test_satisfy_to_outside_the_range(self, value: float) -> None:
        with pytest.raises(ValueError, match="satisfy_to must be within"):
            GoalParams(satisfy_to=value)

    @pytest.mark.parametrize("field_name", ["pages", "target_level"])
    def test_a_boolean_is_not_a_number(self, field_name: str) -> None:
        # ``bool`` is an ``int`` subclass, so ``True`` would otherwise pass a
        # ``1..10`` range check and become "train to level True".
        with pytest.raises(ValueError, match="whole number"):
            GoalParams(**{field_name: True})  # type: ignore[arg-type]

    def test_a_boolean_is_not_a_fraction(self) -> None:
        with pytest.raises(ValueError, match="satisfy_to must be a number"):
            GoalParams(satisfy_to=True)

    @pytest.mark.parametrize("value", [3.5, "7"])
    def test_pages_must_be_whole(self, value: object) -> None:
        with pytest.raises(ValueError, match="pages must be a whole number"):
            GoalParams(pages=value)  # type: ignore[arg-type]

    def test_an_empty_range_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            NumericRange(5, 1)

    def test_range_boundaries_are_inclusive(self) -> None:
        span = NumericRange(1, 3)
        span.check(1, name="x")
        span.check(3, name="x")
        with pytest.raises(ValueError, match=r"x must be within 1\.\.3, got 4"):
            span.check(4, name="x")


class TestEveryNumericParameterIsRanged:
    """The range table, walked in both directions.

    :class:`GoalParams` checks its fields one ``if`` at a time, so a fifth field
    can be added, declared by a kind, and never checked — the module would
    import, the spec table would be consistent, and the value would go through.
    These tests are what makes that fail.
    """

    def test_every_numeric_field_has_a_declared_range_and_no_range_is_orphaned(self) -> None:
        numeric = {
            name
            for name, types in param_types().items()
            if any(atom in (int, float) for atom in types)
        }
        # Equality, not containment: a numeric field with no range is unbounded,
        # and a range for a field that no longer exists is a check nobody runs.
        assert numeric == set(NUMERIC_RANGES)

    def test_the_ranges_are_finite(self) -> None:
        for name, span in NUMERIC_RANGES.items():
            assert math.isfinite(span.minimum), name
            assert math.isfinite(span.maximum), name
            assert span.minimum <= span.maximum, name

    @pytest.mark.parametrize("name", sorted(NUMERIC_RANGES))
    def test_both_edges_of_every_range_are_accepted(self, name: str) -> None:
        for value in edge_values(name):
            params = GoalParams(**{name: value})  # type: ignore[arg-type]
            assert getattr(params, name) == value

    @pytest.mark.parametrize("name", sorted(NUMERIC_RANGES))
    def test_a_value_outside_every_range_is_refused(self, name: str) -> None:
        for value in outside_values(name):
            with pytest.raises(ValueError, match=f"{name} must be within"):
                GoalParams(**{name: value})  # type: ignore[arg-type]

    def test_an_out_of_range_parameter_never_becomes_a_goal(self) -> None:
        # Per kind rather than per field: the refusal has to happen on the way
        # to a request, so the queue never sees the submission at all.
        queue = make_queue(FakeClock())
        checked = 0
        for kind, spec in GOAL_SPECS.items():
            declared = (spec.required | spec.optional) & set(NUMERIC_RANGES)
            for name in sorted(declared):
                _, too_high = outside_values(name)
                supplied = {n: TrainableSkill.COOKING for n in spec.required if n == "skill"}
                with pytest.raises(ValueError, match=f"{name} must be within"):
                    GoalRequest(
                        kind=kind,
                        idempotency_key="k",
                        params=GoalParams(**{**supplied, name: too_high}),  # type: ignore[arg-type]
                    )
                checked += 1
        assert checked >= 5, "the walk found almost no numeric parameters; it proves little"
        assert queue.open_count == 0


class TestParametersAreCheckedAgainstTheKind:
    def test_train_skill_requires_a_skill(self) -> None:
        with pytest.raises(ValueError, match=r"train_skill requires \['skill'\]"):
            GoalRequest(kind=GoalKind.TRAIN_SKILL, idempotency_key="k")

    def test_train_skill_accepts_its_optional_parameters(self) -> None:
        request = GoalRequest(
            kind=GoalKind.TRAIN_SKILL,
            idempotency_key="k",
            params=GoalParams(skill=TrainableSkill.CARPENTRY, target_level=4, pages=30),
        )
        assert request.params.skill is TrainableSkill.CARPENTRY

    def test_a_kind_refuses_a_parameter_it_does_not_declare(self) -> None:
        with pytest.raises(ValueError, match=r"satisfy_hunger takes no \['skill'\]"):
            GoalRequest(
                kind=GoalKind.SATISFY_HUNGER,
                idempotency_key="k",
                params=GoalParams(skill=TrainableSkill.COOKING),
            )

    def test_satisfy_to_belongs_to_the_consumption_kinds_only(self) -> None:
        GoalRequest(
            kind=GoalKind.SATISFY_THIRST,
            idempotency_key="k",
            params=GoalParams(satisfy_to=0.2),
        )
        with pytest.raises(ValueError, match=r"read_for_boredom takes no \['satisfy_to'\]"):
            GoalRequest(
                kind=GoalKind.READ_FOR_BOREDOM,
                idempotency_key="k",
                params=GoalParams(satisfy_to=0.2),
            )

    def test_the_default_budget_comes_from_the_kind(self) -> None:
        request = eat()
        assert request.effective_budget == GOAL_SPECS[GoalKind.SATISFY_HUNGER].budget

    def test_a_submitted_budget_wins(self) -> None:
        budget = GoalBudget(max_wall_ms=5_000, max_steps=2, pending_ttl_ms=5_000)
        request = GoalRequest(kind=GoalKind.SATISFY_HUNGER, idempotency_key="k", budget=budget)
        assert request.effective_budget is budget


class TestBudgetBounds:
    @pytest.mark.parametrize(
        ("wall", "steps", "ttl", "message"),
        [
            (MIN_GOAL_WALL_MS - 1, 1, 1_000, "max_wall_ms"),
            (MAX_GOAL_WALL_MS + 1, 1, 1_000, "max_wall_ms"),
            (5_000, 0, 1_000, "max_steps"),
            (5_000, MAX_GOAL_STEPS + 1, 1_000, "max_steps"),
            (5_000, 1, 0, "pending_ttl_ms"),
            (5_000, 1, 10_000_000, "pending_ttl_ms"),
        ],
    )
    def test_out_of_range_budgets_are_refused(
        self, wall: int, steps: int, ttl: int, message: str
    ) -> None:
        with pytest.raises(ValueError, match=message):
            GoalBudget(max_wall_ms=wall, max_steps=steps, pending_ttl_ms=ttl)

    def test_the_edges_are_accepted(self) -> None:
        GoalBudget(max_wall_ms=MIN_GOAL_WALL_MS, max_steps=1, pending_ttl_ms=1)
        GoalBudget(
            max_wall_ms=MAX_GOAL_WALL_MS,
            max_steps=MAX_GOAL_STEPS,
            pending_ttl_ms=goal_model.MAX_PENDING_TTL_MS,
        )

    def test_a_spec_cannot_make_a_parameter_both_required_and_optional(self) -> None:
        with pytest.raises(ValueError, match="either required or optional"):
            GoalSpec(
                required=frozenset({"pages"}),
                optional=frozenset({"pages"}),
                budget=GoalBudget(max_wall_ms=5_000, max_steps=1, pending_ttl_ms=1_000),
            )


class TestTheDeclaredBoundsThemselves:
    """The ceilings, against hand-written numbers.

    Every other bounds test in this file is written *relative* to the constant it
    checks — ``GoalParams(pages=MAX + 1)`` is refused, a ``detail`` of
    ``MAX_DETAIL_CHARS + 1`` is refused — which proves a check exists and proves
    nothing whatever about the size of the number. Raising ``MAX_DETAIL_CHARS``
    to a million, ``MAX_GOAL_WALL_MS`` to eleven days, ``MAX_EVIDENCE_KEYS`` to
    forty or ``MIN_GOAL_WALL_MS`` to one millisecond leaves every one of those
    tests green, because each moves with the constant it is derived from.
    "Bounded" is a claim about how large the bound is, so the sizes are spelled
    out here and loosening one is a reviewed change rather than a silent one.
    """

    def test_the_declared_bounds_are_exactly_these(self) -> None:
        assert MAX_IDEMPOTENCY_KEY_LEN == 64
        assert MAX_PARSED_TOKEN_CHARS == 64
        assert MAX_DETAIL_CHARS == 200
        assert MAX_RENDERED_VALUE_CHARS == 26
        assert MAX_EVIDENCE_KEYS == 8
        assert MAX_SKILL_LEVEL == 10
        assert MAX_GOAL_STEPS == 12
        assert MIN_GOAL_WALL_MS == 1_000
        assert MAX_GOAL_WALL_MS == 900_000
        assert MAX_PENDING_TTL_MS == 600_000

    def test_a_goal_may_outlast_one_plan_but_not_many(self) -> None:
        # The comment on MAX_GOAL_STEPS claimed for a while that it was *equal*
        # to the planner's own ceiling, and it is 12 against the planner's 5.
        # The relationship it really has — room for a goal to be re-planned, and
        # not an open number of steps — is checked here so that the comment
        # cannot quietly become wrong again.
        assert MAX_PLAN_STEPS == 5
        assert MAX_GOAL_STEPS > MAX_PLAN_STEPS
        assert MAX_GOAL_STEPS <= 3 * MAX_PLAN_STEPS

    def test_the_longest_detail_admitted_is_a_line_and_not_a_page(self) -> None:
        # The behavioural half, with the literal written out rather than derived:
        # 200 characters is a diagnostic somebody reads in one log line.
        a_record(detail="x" * 200)
        with pytest.raises(ValueError, match="at most"):
            a_record(detail="x" * 201)

    def test_the_wall_clock_ceiling_is_minutes_and_not_days(self) -> None:
        # Fifteen minutes, with the arithmetic spelled out. A budget long enough
        # that a user would have forgotten setting it is not a bound on anything.
        assert MAX_GOAL_WALL_MS == 15 * 60 * 1_000
        GoalBudget(max_wall_ms=15 * 60 * 1_000, max_steps=1, pending_ttl_ms=1_000)
        with pytest.raises(ValueError, match="max_wall_ms"):
            GoalBudget(max_wall_ms=15 * 60 * 1_000 + 1, max_steps=1, pending_ttl_ms=1_000)

    def test_the_pending_time_to_live_ceiling_is_ten_minutes(self) -> None:
        assert MAX_PENDING_TTL_MS == 10 * 60 * 1_000
        GoalBudget(max_wall_ms=5_000, max_steps=1, pending_ttl_ms=10 * 60 * 1_000)
        with pytest.raises(ValueError, match="pending_ttl_ms"):
            GoalBudget(max_wall_ms=5_000, max_steps=1, pending_ttl_ms=10 * 60 * 1_000 + 1)


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------


class TestIdempotency:
    @pytest.mark.parametrize(
        ("key", "digest"),
        [
            # Hand-computed with blake2s(digest_size=8). A digest function
            # replaced by anything else — including something that happens to be
            # collision-free — fails here.
            ("goal-eat-1", "83db24d17ba18774"),
            ("eat", "edfc8c7751487050"),
            ("a" * 64, "50ebb69474dcd48c"),
        ],
    )
    def test_digest_is_pinned(self, key: str, digest: str) -> None:
        assert key_digest(key) == digest
        assert len(key_digest(key)) == 16

    def test_a_resubmission_is_not_a_second_goal(self) -> None:
        queue = make_queue(FakeClock())
        first = queue.submit(eat("order-42"))
        second = queue.submit(eat("order-42"))
        assert first.goal is not None
        assert second.goal is not None
        assert second.duplicate is True
        assert first.duplicate is False
        assert second.goal.goal_id == first.goal.goal_id
        assert queue.open_count == 1
        assert len(queue.pending) == 1

    def test_a_resubmission_after_the_goal_finished_returns_that_goal(self) -> None:
        queue = make_queue(FakeClock())
        started = start_one(queue, eat("order-42"))
        queue.fail(started.goal_id, ReasonCode.NO_SAFE_FOOD)
        again = queue.submit(eat("order-42"))
        assert again.duplicate is True
        assert again.goal is not None
        assert again.goal.goal_id == started.goal_id
        assert again.goal.state is GoalState.FAILED
        assert queue.open_count == 0

    def test_the_same_key_with_different_parameters_is_refused(self) -> None:
        queue = make_queue(FakeClock())
        assert queue.submit(eat("order-42", satisfy_to=0.2)).accepted
        clash = queue.submit(eat("order-42", satisfy_to=0.9))
        assert clash.goal is None
        assert clash.refusal is not None
        assert clash.refusal.reason_code is ReasonCode.INVALID_ARGUMENT
        assert "mint a new key" in clash.refusal.message
        assert queue.open_count == 1

    def test_the_same_key_for_a_different_kind_is_refused(self) -> None:
        queue = make_queue(FakeClock())
        assert queue.submit(eat("order-42")).accepted
        clash = queue.submit(GoalRequest(kind=GoalKind.SATISFY_THIRST, idempotency_key="order-42"))
        assert clash.refusal is not None
        assert clash.refusal.reason_code is ReasonCode.INVALID_ARGUMENT

    def test_distinct_keys_are_distinct_goals(self) -> None:
        queue = make_queue(FakeClock())
        one = queue.submit(eat("order-1"))
        two = queue.submit(eat("order-2"))
        assert one.goal is not None
        assert two.goal is not None
        assert one.goal.goal_id != two.goal.goal_id
        assert queue.open_count == 2

    def test_the_goal_id_is_minted_here_not_supplied(self) -> None:
        queue = make_queue(FakeClock())
        record = queue.submit(eat("order-1")).goal
        assert record is not None
        assert re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            record.goal_id,
        )

    @pytest.mark.parametrize(
        "key",
        ["", " ", "-leading", ".leading", "has space", "a" * 65, "C:/Users/bob/token", "ключ"],
    )
    def test_badly_shaped_keys_are_refused(self, key: str) -> None:
        with pytest.raises(ValueError, match="idempotency_key must be"):
            eat(key)

    @pytest.mark.parametrize("key", ["a", "0", "order-42", "a.b:c_d-e", "a" * 64])
    def test_well_shaped_keys_are_accepted(self, key: str) -> None:
        assert eat(key).idempotency_key == key

    @pytest.mark.parametrize(
        "key", ["order-42\n", "order-42\r\n", "order\n42", "a" * 64 + "\n", "order-42\t"]
    )
    def test_a_key_containing_a_line_break_is_refused(self, key: str) -> None:
        # ``$`` matches immediately before a trailing newline in Python, so a
        # ``$``-anchored shape check accepts "order-42\n" — which hashes to a
        # different digest than "order-42" and therefore becomes a *second*
        # goal from what the caller sent twice. The digests below are what makes
        # that consequence concrete rather than stylistic.
        assert key_digest("order-42\n") != key_digest("order-42")
        with pytest.raises(ValueError, match="idempotency_key must be"):
            eat(key)


class TestEveryGoalCarriesAnIdempotencyKey:
    """The key is not optional, and it is the whole of what makes a retry safe."""

    @pytest.mark.parametrize("kind", list(GoalKind))
    def test_no_kind_can_be_requested_without_a_key(self, kind: GoalKind) -> None:
        with pytest.raises(TypeError, match="idempotency_key"):
            GoalRequest(kind=kind, params=params_for(kind))  # type: ignore[call-arg]

    @pytest.mark.parametrize(
        "digest", ["", "not hex at all", "0" * 15, "0" * 17, "G" * 16, "0123456789abcdeF"]
    )
    def test_a_record_without_a_well_formed_digest_is_refused(self, digest: str) -> None:
        # A record whose digest is missing or malformed is a record that no
        # resubmission of its key can resolve to, so the goal it stands for
        # could be created a second time by the retry the key exists to absorb.
        with pytest.raises(ValueError, match="digest of its idempotency key"):
            a_record(key_digest=digest)

    def test_a_record_without_an_id_is_refused(self) -> None:
        with pytest.raises(ValueError, match="the id this process minted"):
            a_record(goal_id="")

    @pytest.mark.parametrize("kind", list(GoalKind))
    def test_resubmitting_the_same_key_does_not_create_a_second_goal(self, kind: GoalKind) -> None:
        queue = make_queue(FakeClock())
        first = queue.submit(
            GoalRequest(kind=kind, idempotency_key="order-42", params=params_for(kind))
        )
        assert first.goal is not None, first.refusal
        assert first.duplicate is False
        for _ in range(3):
            # A distinct but equal request each time: what a retried submission
            # after a dropped acknowledgement actually looks like.
            again = queue.submit(
                GoalRequest(kind=kind, idempotency_key="order-42", params=params_for(kind))
            )
            assert again.goal is not None, again.refusal
            assert again.duplicate is True
            assert again.goal.goal_id == first.goal.goal_id
            assert again.goal.sequence == first.goal.sequence
        assert queue.open_count == 1
        assert len(queue.pending) == 1
        assert queue.pending[0].goal_id == first.goal.goal_id

    def test_a_duplicate_consumes_no_sequence_number(self) -> None:
        # The sequence is the queue's own counter and orders the backlog, so it
        # is the one number that says whether a second record was minted at all
        # — a duplicate implementation that created and discarded a record would
        # leave a gap here.
        queue = make_queue(FakeClock())
        first = queue.submit(eat("order-1")).goal
        assert first is not None
        for _ in range(4):
            assert queue.submit(eat("order-1")).duplicate is True
        second = queue.submit(eat("order-2")).goal
        assert second is not None
        assert first.sequence == 1
        assert second.sequence == 2

    def test_every_record_carries_the_digest_of_the_key_that_made_it(self) -> None:
        queue = make_queue(FakeClock(), max_open=len(GoalKind))
        for index, kind in enumerate(GoalKind):
            key = f"order-{index}"
            admitted = queue.submit(
                GoalRequest(kind=kind, idempotency_key=key, params=params_for(kind))
            )
            assert admitted.goal is not None
            assert admitted.goal.key_digest == key_digest(key)
            assert key not in repr(admitted.goal)

    def test_a_resubmission_survives_the_goal_being_activated(self) -> None:
        # The window that matters in practice: the acknowledgement went missing
        # while the goal was starting, so the retry arrives against an *active*
        # goal and must still not start a second one.
        queue = make_queue(FakeClock())
        started = start_one(queue, eat("order-42"))
        again = queue.submit(eat("order-42"))
        assert again.duplicate is True
        assert again.goal is not None
        assert again.goal.goal_id == started.goal_id
        assert again.goal.state is GoalState.ACTIVE
        assert queue.open_count == 1
        assert queue.pending == ()


# --------------------------------------------------------------------------
# the bound on the queue itself
# --------------------------------------------------------------------------


class TestBoundedQueue:
    def test_submitting_past_the_cap_is_refused_not_dropped(self) -> None:
        queue = make_queue(FakeClock(), max_open=2)
        assert queue.submit(eat("a")).accepted
        assert queue.submit(eat("b")).accepted
        over = queue.submit(eat("c"))
        assert over.accepted is False
        assert over.goal is None
        assert over.refusal is not None
        assert over.refusal.reason_code is ReasonCode.QUEUE_REJECTED
        assert "2 open goal(s)" in over.refusal.message
        assert "cancel one or wait" in over.refusal.message
        # Refused, not dropped: the two that were admitted are still there and
        # the third created nothing.
        assert queue.open_count == 2
        assert len(queue.pending) == 2

    def test_the_cap_counts_the_active_goal_too(self) -> None:
        queue = make_queue(FakeClock(), max_open=2)
        start_one(queue, eat("a"))
        assert queue.submit(eat("b")).accepted
        over = queue.submit(eat("c"))
        assert over.refusal is not None
        assert queue.open_count == 2

    def test_a_finished_goal_frees_its_slot(self) -> None:
        queue = make_queue(FakeClock(), max_open=1)
        started = start_one(queue, eat("a"))
        assert queue.submit(eat("b")).refusal is not None
        queue.fail(started.goal_id, ReasonCode.NO_SAFE_FOOD)
        assert queue.open_count == 0
        assert queue.submit(eat("b")).accepted

    def test_the_history_is_bounded_and_never_evicts_an_open_goal(self) -> None:
        # The churn has to be *admitted* and then finished, not refused. An
        # earlier version of this test asserted that every churn submission hit
        # the cap, which meant no record beyond the two open ones was ever
        # created, the history never exceeded ``max_remembered``, and the
        # eviction loop this test is named after never ran once: deleting the
        # "skip open goals" guard from ``_evict`` left it green.
        queue = make_queue(FakeClock(), max_open=2, max_remembered=3)
        keep = queue.submit(eat("keep")).goal
        assert keep is not None
        churn: list[str] = []
        for index in range(10):
            admitted = queue.submit(eat(f"churn-{index}")).goal
            assert admitted is not None, f"churn-{index} needed the free slot beside keep"
            churn.append(admitted.goal_id)
            # Ended without activating it, so "keep" never loses its place at
            # the front of the history — which is exactly where an eviction
            # that ignored ``is_open`` would take its victim from.
            assert queue.request_cancel(admitted.goal_id) is True
            assert [t.state for t in queue.tick()] == [GoalState.CANCELLED]

        # Bounded: the oldest finished goals really were dropped.
        assert queue.record(churn[0]) is None
        assert queue.record(churn[-1]) is not None
        # And the open goal survived every one of those evictions.
        survivor = queue.record(keep.goal_id)
        assert survivor is not None
        assert survivor.state is GoalState.PENDING
        assert queue.open_count == 1

    def test_a_still_open_goal_keeps_its_place_in_the_resubmission_index(self) -> None:
        # ``_trim_digests`` bounds the digest index the same way ``_evict``
        # bounds the history, and it has the same rule: only a digest whose goal
        # has finished may be dropped. Losing an *open* goal's digest is the one
        # failure an idempotency key exists to prevent — the retry that follows
        # would be admitted as a second goal for work already running.
        queue = make_queue(FakeClock(), max_open=2, max_remembered=3)
        keep = queue.submit(eat("keep")).goal
        assert keep is not None
        for index in range(10):
            admitted = queue.submit(eat(f"churn-{index}")).goal
            assert admitted is not None
            queue.request_cancel(admitted.goal_id)
            queue.tick()

        # Eleven digests have passed through an index capped at three.
        again = queue.submit(eat("keep"))
        assert again.duplicate is True, "the open goal's key was trimmed and would run twice"
        assert again.goal is not None
        assert again.goal.goal_id == keep.goal_id
        assert queue.open_count == 1

    def test_the_history_forgets_the_oldest_finished_goal(self) -> None:
        queue = make_queue(FakeClock(), max_open=1, max_remembered=2)
        finished: list[str] = []
        for index in range(4):
            started = start_one(queue, eat(f"g-{index}"))
            queue.fail(started.goal_id, ReasonCode.NO_SAFE_FOOD)
            finished.append(started.goal_id)
        assert queue.record(finished[-1]) is not None
        assert queue.record(finished[0]) is None
        # A key whose goal has been forgotten starts over rather than raising.
        fresh = queue.submit(eat("g-0"))
        assert fresh.accepted
        assert fresh.duplicate is False

    def test_the_queue_refuses_a_history_smaller_than_its_cap(self) -> None:
        with pytest.raises(ValueError, match="max_remembered must be at least"):
            GoalQueue(clock=FakeClock(), max_open=4, max_remembered=3)

    def test_the_queue_refuses_a_zero_cap(self) -> None:
        with pytest.raises(ValueError, match="max_open must be positive"):
            GoalQueue(clock=FakeClock(), max_open=0)


class TestOneAtATime:
    def test_a_second_activation_is_refused_and_names_the_active_goal(self) -> None:
        queue = make_queue(FakeClock())
        first = start_one(queue, eat("a"))
        assert queue.submit(eat("b")).accepted
        second = queue.activate_next()
        assert second.goal is None
        assert second.refusal is not None
        assert second.refusal.reason_code is ReasonCode.QUEUE_REJECTED
        assert second.refusal.active_goal_id == first.goal_id
        assert first.goal_id in second.refusal.message
        assert "satisfy_hunger" in second.refusal.message
        assert queue.active is not None
        assert queue.active.goal_id == first.goal_id

    def test_the_backlog_survives_a_refused_activation(self) -> None:
        queue = make_queue(FakeClock())
        start_one(queue, eat("a"))
        assert queue.submit(eat("b")).accepted
        queue.activate_next()
        assert len(queue.pending) == 1

    def test_activation_with_nothing_waiting_is_refused(self) -> None:
        queue = make_queue(FakeClock())
        empty = queue.activate_next()
        assert empty.refusal is not None
        assert empty.refusal.reason_code is ReasonCode.PRECONDITION_FAILED

    def test_the_next_goal_starts_once_the_active_one_ends(self) -> None:
        queue = make_queue(FakeClock())
        first = start_one(queue, eat("a"))
        assert queue.submit(eat("b")).accepted
        queue.fail(first.goal_id, ReasonCode.NO_SAFE_FOOD)
        second = queue.activate_next()
        assert second.goal is not None
        assert second.goal.state is GoalState.ACTIVE
        assert queue.active is not None


# --------------------------------------------------------------------------
# Windows: the platform whose clock is coarse
# --------------------------------------------------------------------------


class TestWindowsCoarseClock:
    def test_the_double_reproduces_the_granule(self) -> None:
        clock = WindowsClock()
        assert clock() == 0
        assert clock.advance_granules(1) == 15
        assert clock.advance_granules(1) == 31
        assert clock.advance_granules(62) == 1_000

    def test_goals_submitted_in_one_granule_share_a_timestamp_but_keep_order(self) -> None:
        # This is the defect a Linux clock hides: on Windows three submissions
        # inside 15 ms carry the *same* millisecond, so ordering the backlog by
        # timestamp makes it arbitrary. The queue orders by admission sequence.
        queue = make_queue(WindowsClock(), max_open=4)
        ids = []
        for key in ("first", "second", "third"):
            admitted = queue.submit(eat(key))
            assert admitted.goal is not None
            ids.append(admitted.goal.goal_id)
        stamps = {record.submitted_at_ms for record in queue.pending}
        assert stamps == {0}, "the granule should have collapsed all three timestamps"
        assert [record.goal_id for record in queue.pending] == ids
        assert [record.sequence for record in queue.pending] == [1, 2, 3]

    def test_activation_order_survives_the_shared_timestamp(self) -> None:
        # Three goals, one granule, one timestamp: whichever was submitted first
        # is served first, and the two that follow keep their order behind it.
        queue = make_queue(WindowsClock(), max_open=4)
        submitted = []
        for key in ("first", "second", "third"):
            admitted = queue.submit(eat(key)).goal
            assert admitted is not None
            submitted.append(admitted.goal_id)
        assert {r.submitted_at_ms for r in queue.pending} == {0}

        served = []
        while queue.pending:
            started = queue.activate_next().goal
            assert started is not None
            served.append(started.goal_id)
            queue.fail(started.goal_id, ReasonCode.NO_SAFE_FOOD)
        assert served == submitted

    def test_a_goal_does_not_survive_a_whole_extra_granule(self) -> None:
        # ``now > deadline`` instead of ``now >= deadline`` costs one granule of
        # overrun on every goal, and against a microsecond clock the difference
        # never shows: some later millisecond always satisfies both. Here the
        # deadline lands exactly on a granule boundary, which is the case a
        # coarse clock produces constantly.
        clock = WindowsClock()
        queue = make_queue(clock, max_open=2)
        budget = GoalBudget(max_wall_ms=1_000, max_steps=4, pending_ttl_ms=600_000)
        started = start_one(
            queue, GoalRequest(kind=GoalKind.SATISFY_HUNGER, idempotency_key="k", budget=budget)
        )
        assert started.deadline_ms == 1_000
        clock.advance_granules(63)  # 984 ms — still inside the budget
        assert queue.tick() == ()
        assert clock.advance_granules(1) == 1_000  # exactly on the deadline
        transitions = queue.tick()
        assert [t.state for t in transitions] == [GoalState.EXPIRED]
        assert transitions[0].reason_code is ReasonCode.ACTION_TIMEOUT

    def test_the_pending_time_to_live_lands_on_the_boundary_too(self) -> None:
        clock = WindowsClock()
        queue = make_queue(clock, max_open=2)
        budget = GoalBudget(max_wall_ms=5_000, max_steps=4, pending_ttl_ms=1_000)
        admitted = queue.submit(
            GoalRequest(kind=GoalKind.SATISFY_HUNGER, idempotency_key="k", budget=budget)
        )
        assert admitted.goal is not None
        assert admitted.goal.pending_expiry_ms == 1_000
        clock.advance_granules(63)
        assert queue.tick() == ()
        clock.advance_granules(1)
        transitions = queue.tick()
        assert [t.state for t in transitions] == [GoalState.EXPIRED]
        assert queue.open_count == 0


class TestClockMonotonicity:
    def test_a_backward_clock_step_does_not_produce_a_goal_that_ends_before_it_starts(
        self,
    ) -> None:
        # Wall clocks are shared between the sidecar and the game, so this
        # subsystem uses one; the price is that a time sync can step it back.
        clock = FakeClock(start=10_000)
        queue = make_queue(clock)
        started = start_one(queue)
        assert started.started_at_ms == 10_000
        clock.now = 400  # NTP stepped the wall clock backwards
        transition = queue.fail(started.goal_id, ReasonCode.NO_SAFE_FOOD)
        assert transition is not None
        assert transition.at_ms == 10_000
        ended = queue.record(started.goal_id)
        assert ended is not None
        assert ended.finished_at_ms == 10_000

    def test_a_backward_clock_step_does_not_extend_a_deadline(self) -> None:
        clock = FakeClock(start=0)
        queue = make_queue(clock)
        budget = GoalBudget(max_wall_ms=1_000, max_steps=4, pending_ttl_ms=600_000)
        started = start_one(
            queue, GoalRequest(kind=GoalKind.SATISFY_HUNGER, idempotency_key="k", budget=budget)
        )
        clock.now = 1_500
        clock.now = 200  # a step back, arriving before the queue next looks
        assert queue.tick() == ()
        clock.now = 1_000
        assert [t.state for t in queue.tick()] == [GoalState.EXPIRED]
        ended = queue.record(started.goal_id)
        assert ended is not None
        assert ended.finished_at_ms == 1_000


# --------------------------------------------------------------------------
# every goal reaches a terminal state
# --------------------------------------------------------------------------


class TestTerminalStates:
    def test_the_wall_clock_ends_a_goal(self) -> None:
        clock = FakeClock()
        queue = make_queue(clock)
        budget = GoalBudget(max_wall_ms=2_000, max_steps=8, pending_ttl_ms=600_000)
        started = start_one(
            queue, GoalRequest(kind=GoalKind.SATISFY_HUNGER, idempotency_key="k", budget=budget)
        )
        clock.now = 1_999
        assert queue.tick() == ()
        assert queue.active is not None
        clock.now = 2_000
        transitions = queue.tick()
        assert len(transitions) == 1
        assert transitions[0].previous is GoalState.ACTIVE
        assert transitions[0].state is GoalState.EXPIRED
        assert transitions[0].reason_code is ReasonCode.ACTION_TIMEOUT
        assert "2000 ms" in transitions[0].detail
        assert queue.open_count == 0
        ended = queue.record(started.goal_id)
        assert ended is not None
        assert ended.state.is_terminal

    def test_the_step_budget_ends_a_goal(self) -> None:
        queue = make_queue(FakeClock())
        budget = GoalBudget(max_wall_ms=600_000, max_steps=2, pending_ttl_ms=600_000)
        started = start_one(
            queue, GoalRequest(kind=GoalKind.SATISFY_HUNGER, idempotency_key="k", budget=budget)
        )
        assert queue.note_step(started.goal_id) is None
        first = queue.record(started.goal_id)
        assert first is not None
        assert first.steps_used == 1
        assert first.steps_left == 1
        exhausted = queue.note_step(started.goal_id)
        assert exhausted is not None
        assert exhausted.state is GoalState.EXPIRED
        assert exhausted.reason_code is ReasonCode.NO_PROGRESS
        assert queue.active is None
        assert queue.open_count == 0

    def test_a_step_cannot_be_charged_to_a_goal_that_is_not_active(self) -> None:
        queue = make_queue(FakeClock())
        admitted = queue.submit(eat())
        assert admitted.goal is not None
        with pytest.raises(ValueError, match="only an active goal spends steps"):
            queue.note_step(admitted.goal.goal_id)

    def test_a_step_landing_after_the_goal_ended_changes_nothing(self) -> None:
        queue = make_queue(FakeClock())
        started = start_one(queue)
        queue.fail(started.goal_id, ReasonCode.NO_SAFE_FOOD)
        assert queue.note_step(started.goal_id) is None
        ended = queue.record(started.goal_id)
        assert ended is not None
        assert ended.state is GoalState.FAILED

    def test_a_pending_goal_expires_on_its_time_to_live(self) -> None:
        clock = FakeClock()
        queue = make_queue(clock)
        budget = GoalBudget(max_wall_ms=5_000, max_steps=4, pending_ttl_ms=3_000)
        admitted = queue.submit(
            GoalRequest(kind=GoalKind.SATISFY_HUNGER, idempotency_key="k", budget=budget)
        )
        assert admitted.goal is not None
        clock.now = 2_999
        assert queue.tick() == ()
        clock.now = 3_000
        transitions = queue.tick()
        assert [(t.previous, t.state) for t in transitions] == [
            (GoalState.PENDING, GoalState.EXPIRED)
        ]
        assert transitions[0].reason_code is ReasonCode.ACTION_TIMEOUT
        assert queue.open_count == 0

    def test_an_activated_goal_no_longer_expires_on_the_pending_clock(self) -> None:
        clock = FakeClock()
        queue = make_queue(clock)
        budget = GoalBudget(max_wall_ms=600_000, max_steps=4, pending_ttl_ms=1_000)
        start_one(
            queue, GoalRequest(kind=GoalKind.SATISFY_HUNGER, idempotency_key="k", budget=budget)
        )
        clock.now = 500_000
        assert queue.tick() == ()
        assert queue.active is not None

    def test_a_failure_carries_the_reason_it_was_given(self) -> None:
        queue = make_queue(FakeClock())
        started = start_one(queue)
        transition = queue.fail(started.goal_id, ReasonCode.NO_SAFE_FOOD, "nothing edible")
        assert transition is not None
        assert transition.state is GoalState.FAILED
        assert transition.reason_code is ReasonCode.NO_SAFE_FOOD
        assert transition.detail == "nothing edible"

    def test_a_failure_may_not_claim_the_postcondition_was_met(self) -> None:
        queue = make_queue(FakeClock())
        started = start_one(queue)
        with pytest.raises(ValueError, match="use succeed"):
            queue.fail(started.goal_id, ReasonCode.POSTCONDITION_MET)

    def test_an_unknown_goal_id_is_a_lookup_error(self) -> None:
        queue = make_queue(FakeClock())
        with pytest.raises(UnknownGoalError) as caught:
            queue.fail("../../etc/passwd", ReasonCode.NO_SAFE_FOOD)
        assert "passwd" not in str(caught.value)


# --------------------------------------------------------------------------
# succeeded means observed
# --------------------------------------------------------------------------


class TestSucceededRequiresObservation:
    def test_a_goal_succeeds_on_an_observed_postcondition(self) -> None:
        queue = make_queue(FakeClock())
        started = start_one(queue)
        transition = queue.succeed(started.goal_id, observed({"hunger": 0.1, "ate_ref": "x"}))
        assert transition is not None
        assert transition.state is GoalState.SUCCEEDED
        assert transition.reason_code is ReasonCode.POSTCONDITION_MET
        ended = queue.record(started.goal_id)
        assert ended is not None
        assert ended.evidence_keys == ("ate_ref", "hunger")

    def test_a_success_ack_with_no_evidence_is_refused(self) -> None:
        # ActionResult.succeeded() will not build one, but the raw constructor
        # will — and this queue is a second consumer of that type.
        hollow = ActionResult(
            session_id="s",
            seq=1,
            command_id="c",
            action="consume.eat",
            status=ActionStatus.SUCCEEDED,
            reason_code=ReasonCode.POSTCONDITION_MET,
            timestamp_ms=5,
        )
        queue = make_queue(FakeClock())
        started = start_one(queue)
        # Matched in full on purpose: GoalRecord has its own invariant with a
        # near-identical message, and a loose pattern would pass whether or not
        # the queue checked anything at its own boundary.
        with pytest.raises(
            ValueError, match=r"^a goal succeeds only on observed postcondition evidence$"
        ):
            queue.succeed(started.goal_id, hollow)
        still_running = queue.record(started.goal_id)
        assert still_running is not None
        assert still_running.state is GoalState.ACTIVE

    @pytest.mark.parametrize(
        "status", [ActionStatus.FAILED, ActionStatus.CANCELLED, ActionStatus.ACCEPTED]
    )
    def test_a_non_success_ack_cannot_end_a_goal_successfully(self, status: ActionStatus) -> None:
        ack = ActionResult(
            session_id="s",
            seq=1,
            command_id="c",
            action="consume.eat",
            status=status,
            reason_code=ReasonCode.NO_SAFE_FOOD,
            timestamp_ms=5,
            evidence={"hunger": 0.9},
        )
        queue = make_queue(FakeClock())
        started = start_one(queue)
        with pytest.raises(ValueError, match="succeeded action result"):
            queue.succeed(started.goal_id, ack)

    def test_the_record_itself_refuses_an_unevidenced_success(self) -> None:
        base = GoalRecord(
            goal_id="g",
            kind=GoalKind.SATISFY_HUNGER,
            params=GoalParams(),
            budget=GOAL_SPECS[GoalKind.SATISFY_HUNGER].budget,
            key_digest="0" * 16,
            sequence=1,
            state=GoalState.PENDING,
            submitted_at_ms=0,
        )
        with pytest.raises(ValueError, match="observed postcondition evidence"):
            replace(
                base,
                state=GoalState.SUCCEEDED,
                reason_code=ReasonCode.POSTCONDITION_MET,
                finished_at_ms=1,
            )

    def test_only_a_succeeded_goal_may_carry_postcondition_met(self) -> None:
        with pytest.raises(ValueError, match="only a succeeded goal"):
            GoalRecord(
                goal_id="g",
                kind=GoalKind.SATISFY_HUNGER,
                params=GoalParams(),
                budget=GOAL_SPECS[GoalKind.SATISFY_HUNGER].budget,
                key_digest="0" * 16,
                sequence=1,
                state=GoalState.FAILED,
                submitted_at_ms=0,
                finished_at_ms=1,
                reason_code=ReasonCode.POSTCONDITION_MET,
            )

    def test_a_succeeded_goal_must_carry_postcondition_met(self) -> None:
        with pytest.raises(ValueError, match="must carry POSTCONDITION_MET"):
            GoalRecord(
                goal_id="g",
                kind=GoalKind.SATISFY_HUNGER,
                params=GoalParams(),
                budget=GOAL_SPECS[GoalKind.SATISFY_HUNGER].budget,
                key_digest="0" * 16,
                sequence=1,
                state=GoalState.SUCCEEDED,
                submitted_at_ms=0,
                finished_at_ms=1,
                reason_code=ReasonCode.NO_SAFE_FOOD,
                evidence_keys=("hunger",),
            )

    def test_a_terminal_goal_must_say_why_and_when(self) -> None:
        common = {
            "goal_id": "g",
            "kind": GoalKind.SATISFY_HUNGER,
            "params": GoalParams(),
            "budget": GOAL_SPECS[GoalKind.SATISFY_HUNGER].budget,
            "key_digest": "0" * 16,
            "sequence": 1,
            "submitted_at_ms": 0,
        }
        with pytest.raises(ValueError, match="must say why"):
            GoalRecord(state=GoalState.CANCELLED, finished_at_ms=1, **common)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="must record when it ended"):
            GoalRecord(
                state=GoalState.CANCELLED,
                reason_code=ReasonCode.PANIC_STOP,
                **common,  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError, match="must not carry a reason code"):
            GoalRecord(
                state=GoalState.PENDING,
                reason_code=ReasonCode.PANIC_STOP,
                **common,  # type: ignore[arg-type]
            )

    def test_an_active_goal_must_record_when_it_started(self) -> None:
        with pytest.raises(ValueError, match="when it started"):
            GoalRecord(
                goal_id="g",
                kind=GoalKind.SATISFY_HUNGER,
                params=GoalParams(),
                budget=GOAL_SPECS[GoalKind.SATISFY_HUNGER].budget,
                key_digest="0" * 16,
                sequence=1,
                state=GoalState.ACTIVE,
                submitted_at_ms=0,
            )

    def test_a_success_after_a_cancel_does_not_reopen_the_goal(self) -> None:
        queue = make_queue(FakeClock())
        started = start_one(queue)
        assert queue.request_cancel(started.goal_id) is True
        queue.tick()
        assert queue.succeed(started.goal_id, observed()) is None
        ended = queue.record(started.goal_id)
        assert ended is not None
        assert ended.state is GoalState.CANCELLED


class TestEvidenceKeys:
    def test_keys_are_kept_and_values_are_not(self) -> None:
        assert normalise_evidence_keys({"hunger": 0.1, "item_ref": "item:abc"}) == (
            "hunger",
            "item_ref",
        )

    def test_a_game_authored_key_is_not_carried_through(self) -> None:
        # Evidence is forwarded from the mod, which forwards it from the game,
        # and game-authored text is untrusted data.
        assert normalise_evidence_keys({"Мясо (свежее)": 1}) == ("unnamed",)
        assert normalise_evidence_keys({"C:\\Users\\bob": 1}) == ("unnamed",)

    @pytest.mark.parametrize("key", ["hunger\n", "hunger\nWARN fake line", "hunger\r", "hunger\t"])
    def test_a_key_carrying_a_line_break_is_not_carried_through(self, key: str) -> None:
        # A ``$``-anchored shape check accepts a trailing newline, and this key
        # is forwarded from the game: accepting it would let game-authored text
        # choose where one of our log lines ends and invent the next one.
        assert normalise_evidence_keys({key: 1}) == ("unnamed",)

    def test_the_key_list_is_capped(self) -> None:
        many = {f"k{index}": index for index in range(50)}
        kept = normalise_evidence_keys(many)
        assert len(kept) == MAX_EVIDENCE_KEYS
        assert kept == tuple(sorted(many)[:MAX_EVIDENCE_KEYS])

    def test_a_record_refuses_more_keys_than_the_cap(self) -> None:
        with pytest.raises(ValueError, match="evidence keys are kept"):
            GoalRecord(
                goal_id="g",
                kind=GoalKind.SATISFY_HUNGER,
                params=GoalParams(),
                budget=GOAL_SPECS[GoalKind.SATISFY_HUNGER].budget,
                key_digest="0" * 16,
                sequence=1,
                state=GoalState.SUCCEEDED,
                submitted_at_ms=0,
                finished_at_ms=1,
                reason_code=ReasonCode.POSTCONDITION_MET,
                evidence_keys=tuple(f"k{i}" for i in range(MAX_EVIDENCE_KEYS + 1)),
            )


# --------------------------------------------------------------------------
# the stop levers
# --------------------------------------------------------------------------


class TestCancel:
    def test_a_cancel_is_observed_within_one_tick(self) -> None:
        queue = make_queue(FakeClock())
        started = start_one(queue)
        assert queue.request_cancel(started.goal_id) is True
        # Not applied eagerly — the tick is the observation point.
        assert queue.active is not None
        transitions = queue.tick()
        assert [(t.goal_id, t.state) for t in transitions] == [
            (started.goal_id, GoalState.CANCELLED)
        ]
        assert transitions[0].reason_code is ReasonCode.CANCELLED_BY_REQUEST
        assert queue.open_count == 0
        assert queue.pending == ()

    def test_a_second_tick_reports_nothing_further(self) -> None:
        queue = make_queue(FakeClock())
        started = start_one(queue)
        queue.request_cancel(started.goal_id)
        assert len(queue.tick()) == 1
        assert queue.tick() == ()

    def test_a_pending_goal_can_be_cancelled_too(self) -> None:
        queue = make_queue(FakeClock())
        admitted = queue.submit(eat())
        assert admitted.goal is not None
        assert queue.request_cancel(admitted.goal.goal_id) is True
        transitions = queue.tick()
        assert [(t.previous, t.state) for t in transitions] == [
            (GoalState.PENDING, GoalState.CANCELLED)
        ]

    def test_cancelling_an_already_finished_goal_reports_nothing_to_do(self) -> None:
        queue = make_queue(FakeClock())
        started = start_one(queue)
        queue.fail(started.goal_id, ReasonCode.NO_SAFE_FOOD)
        assert queue.request_cancel(started.goal_id) is False
        assert queue.tick() == ()

    def test_a_cancel_outranks_an_expiry_in_the_same_tick(self) -> None:
        # User input wins: a goal that was cancelled in the same window it ran
        # out of time is reported as cancelled, once.
        clock = FakeClock()
        queue = make_queue(clock)
        budget = GoalBudget(max_wall_ms=1_000, max_steps=4, pending_ttl_ms=600_000)
        started = start_one(
            queue, GoalRequest(kind=GoalKind.SATISFY_HUNGER, idempotency_key="k", budget=budget)
        )
        queue.request_cancel(started.goal_id)
        clock.now = 9_999
        transitions = queue.tick()
        assert len(transitions) == 1
        assert transitions[0].state is GoalState.CANCELLED
        assert transitions[0].reason_code is ReasonCode.CANCELLED_BY_REQUEST

    def test_a_cancelled_goal_is_not_started_while_the_cancel_waits_for_a_tick(self) -> None:
        # ``activate_next`` skips a goal whose cancellation is outstanding.
        # "Applied on the next tick" is a bound on when the goal *ends*, not a
        # licence to start it in the meantime: promoting a withdrawn goal is how
        # the executor comes to dispatch a step for work the user already
        # stopped. Nothing asserted this, and deleting the skip stayed green.
        queue = make_queue(FakeClock())
        first = queue.submit(eat("a")).goal
        second = queue.submit(eat("b")).goal
        assert first is not None
        assert second is not None
        assert queue.request_cancel(first.goal_id) is True

        started = queue.activate_next()
        assert started.goal is not None, started.refusal
        assert started.goal.goal_id == second.goal_id, "the cancelled goal was started anyway"
        withdrawn = queue.record(first.goal_id)
        assert withdrawn is not None
        assert withdrawn.state is GoalState.PENDING
        assert withdrawn.started_at_ms is None

        assert [(t.goal_id, t.state) for t in queue.tick()] == [
            (first.goal_id, GoalState.CANCELLED)
        ]

    def test_activation_is_refused_when_every_waiting_goal_has_been_cancelled(self) -> None:
        # The other side of the skip: with nothing startable left, the refusal
        # has to say so rather than report the backlog as empty, because the
        # backlog is not empty and the caller can see that.
        queue = make_queue(FakeClock())
        admitted = queue.submit(eat("a")).goal
        assert admitted is not None
        assert queue.request_cancel(admitted.goal_id) is True

        blocked = queue.activate_next()
        assert blocked.goal is None
        assert blocked.refusal is not None
        assert blocked.refusal.reason_code is ReasonCode.CANCELLED_BY_REQUEST
        assert "submit a new goal" in blocked.refusal.message.lower()
        assert queue.active is None
        assert len(queue.pending) == 1
        assert [t.state for t in queue.tick()] == [GoalState.CANCELLED]

    def test_a_step_landing_against_a_cancelled_goal_ends_it_as_cancelled(self) -> None:
        # The step is charged, because it was spent — but the goal ends as
        # cancelled rather than as whatever its budget would have said. Naming
        # budget exhaustion here would report the wrong cause to the one person
        # who knows better, and would swallow the cancellation without any tick
        # ever reporting it.
        queue = make_queue(FakeClock())
        budget = GoalBudget(max_wall_ms=600_000, max_steps=4, pending_ttl_ms=600_000)
        started = start_one(
            queue, GoalRequest(kind=GoalKind.SATISFY_HUNGER, idempotency_key="k", budget=budget)
        )
        assert queue.request_cancel(started.goal_id) is True

        transition = queue.note_step(started.goal_id)
        assert transition is not None, "the step budget swallowed the cancellation"
        assert transition.previous is GoalState.ACTIVE
        assert transition.state is GoalState.CANCELLED
        assert transition.reason_code is ReasonCode.CANCELLED_BY_REQUEST

        ended = queue.record(started.goal_id)
        assert ended is not None
        assert ended.steps_used == 1, "the step was spent and has to be charged"
        assert ended.reason_code is ReasonCode.CANCELLED_BY_REQUEST
        # Consumed, not left over: the tick has nothing further to report.
        assert queue.tick() == ()
        assert queue.open_count == 0

    def test_cancelling_an_unknown_goal_does_not_echo_the_id(self) -> None:
        queue = make_queue(FakeClock())
        poison = "C:/Users/bob/AppData/token"
        with pytest.raises(UnknownGoalError) as caught:
            queue.request_cancel(poison)
        assert poison not in str(caught.value)
        assert "Users" not in str(caught.value)


class TestDisarm:
    def test_disarm_ends_the_active_goal(self) -> None:
        queue = make_queue(FakeClock())
        started = start_one(queue)
        transitions = queue.disarm()
        assert [(t.goal_id, t.state) for t in transitions] == [
            (started.goal_id, GoalState.CANCELLED)
        ]
        assert transitions[0].reason_code is ReasonCode.NOT_ARMED
        assert queue.active is None
        assert queue.armed is False

    def test_disarm_keeps_the_backlog_and_blocks_activation(self) -> None:
        queue = make_queue(FakeClock())
        start_one(queue, eat("a"))
        assert queue.submit(eat("b")).accepted
        queue.disarm()
        assert len(queue.pending) == 1
        blocked = queue.activate_next()
        assert blocked.refusal is not None
        assert blocked.refusal.reason_code is ReasonCode.NOT_ARMED
        assert queue.active is None

    def test_disarm_with_nothing_running_is_quiet(self) -> None:
        queue = make_queue(FakeClock())
        assert queue.disarm() == ()
        assert queue.armed is False

    def test_arming_again_lets_the_backlog_resume(self) -> None:
        queue = make_queue(FakeClock())
        start_one(queue, eat("a"))
        assert queue.submit(eat("b")).accepted
        queue.disarm()
        queue.arm()
        resumed = queue.activate_next()
        assert resumed.goal is not None
        assert resumed.goal.state is GoalState.ACTIVE

    def test_a_pending_goal_left_by_a_disarm_still_expires(self) -> None:
        # "Every goal reaches a terminal state" has to survive a session that
        # never comes back.
        clock = FakeClock()
        queue = make_queue(clock)
        budget = GoalBudget(max_wall_ms=5_000, max_steps=4, pending_ttl_ms=2_000)
        assert queue.submit(
            GoalRequest(kind=GoalKind.SATISFY_HUNGER, idempotency_key="k", budget=budget)
        ).accepted
        queue.disarm()
        clock.now = 2_000
        assert [t.state for t in queue.tick()] == [GoalState.EXPIRED]
        assert queue.open_count == 0


class TestPanicStop:
    def test_a_panic_stop_leaves_no_goal_in_flight(self) -> None:
        queue = make_queue(FakeClock(), max_open=4)
        active = start_one(queue, eat("a"))
        assert queue.submit(eat("b")).accepted
        assert queue.submit(eat("c")).accepted
        transitions = queue.panic_stop()
        assert len(transitions) == 3
        assert {t.state for t in transitions} == {GoalState.CANCELLED}
        assert {t.reason_code for t in transitions} == {ReasonCode.PANIC_STOP}
        assert transitions[0].goal_id == active.goal_id
        assert queue.active is None
        assert queue.pending == ()
        assert queue.open_count == 0
        assert queue.armed is False

    def test_a_panic_stop_discards_a_cancel_that_had_not_been_applied(self) -> None:
        queue = make_queue(FakeClock())
        started = start_one(queue)
        queue.request_cancel(started.goal_id)
        assert len(queue.panic_stop()) == 1
        # Nothing left over to fire on the next tick.
        assert queue.tick() == ()
        ended = queue.record(started.goal_id)
        assert ended is not None
        assert ended.reason_code is ReasonCode.PANIC_STOP

    def test_a_panic_stop_on_an_empty_channel_is_quiet(self) -> None:
        queue = make_queue(FakeClock())
        assert queue.panic_stop() == ()
        assert queue.armed is False

    def test_nothing_starts_again_until_the_channel_is_armed(self) -> None:
        queue = make_queue(FakeClock())
        start_one(queue)
        queue.panic_stop()
        assert queue.submit(eat("later")).accepted
        blocked = queue.activate_next()
        assert blocked.refusal is not None
        assert blocked.refusal.reason_code is ReasonCode.NOT_ARMED


# --------------------------------------------------------------------------
# refusals say what failed, and quote nothing
# --------------------------------------------------------------------------

POISON: tuple[str, ...] = (
    "sk-ant-not-a-real-key",
    "C:/Users/bob/AppData",
    "/home/bob/.ssh/id_rsa",
    "агент поешь",
)


def refusal_messages(poison: str) -> Iterator[str]:
    """Every refusal and error string a poisoned input can produce."""
    clock = FakeClock()
    queue = make_queue(clock, max_open=1)

    try:
        eat(poison)
    except ValueError as exc:
        yield str(exc)

    safe_key = "safe-key"
    start_one(queue, eat(safe_key))
    over = queue.submit(eat("another-safe-key"))
    assert over.refusal is not None
    yield over.refusal.message

    clash = queue.submit(eat(safe_key, satisfy_to=0.5))
    assert clash.refusal is not None
    yield clash.refusal.message

    for call in (
        lambda: queue.request_cancel(poison),
        lambda: queue.fail(poison, ReasonCode.NO_SAFE_FOOD),
        lambda: queue.succeed(poison, observed()),
        lambda: queue.note_step(poison),
    ):
        try:
            call()
        except (UnknownGoalError, ValueError) as exc:
            yield str(exc)

    try:
        GoalParams(skill=poison)  # type: ignore[arg-type]
    except ValueError as exc:
        yield str(exc)


class TestRefusalHygiene:
    @pytest.mark.parametrize("poison", POISON)
    def test_no_refusal_quotes_the_value_it_refused(self, poison: str) -> None:
        produced = list(refusal_messages(poison))
        assert len(produced) >= 6, "the battery stopped early; it is not proving much"
        for message in produced:
            assert poison not in message
            for fragment in ("sk-ant", "AppData", "id_rsa", "Users", "поешь"):
                assert fragment not in message

    @pytest.mark.parametrize("poison", POISON)
    def test_no_refusal_carries_a_path_separator(self, poison: str) -> None:
        for message in refusal_messages(poison):
            assert "\\" not in message
            assert "://" not in message

    def test_every_refusal_says_what_to_do(self) -> None:
        queue = make_queue(FakeClock(), max_open=1)
        start_one(queue, eat("a"))
        messages = [
            queue.submit(eat("b")).refusal,
            queue.activate_next().refusal,
        ]
        for refusal in messages:
            assert refusal is not None
            # An imperative the caller can act on, not just a diagnosis.
            assert any(
                verb in refusal.message for verb in ("cancel", "wait", "mint", "submit", "arm")
            )

    def test_an_empty_refusal_message_is_itself_refused(self) -> None:
        with pytest.raises(ValueError, match="what failed and what to do"):
            GoalRefusal(reason_code=ReasonCode.QUEUE_REJECTED, message="   ")

    def test_a_refusal_cannot_claim_the_postcondition_was_met(self) -> None:
        with pytest.raises(ValueError, match="not a refusal"):
            GoalRefusal(reason_code=ReasonCode.POSTCONDITION_MET, message="nope")

    @pytest.mark.parametrize(
        # Exponents rather than the numbers themselves: pytest builds a
        # parameter id with ``str(val)``, which hits the very 4300-digit limit
        # this test is about.
        ("field_name", "digits", "sign"),
        [
            ("target_level", 4_000, 1),
            ("target_level", 5_000, -1),
            ("pages", 4_301, 1),
            ("satisfy_to", 4_000, 1),
        ],
    )
    def test_a_range_refusal_does_not_quote_a_number_the_caller_sized(
        self, field_name: str, digits: int, sign: int
    ) -> None:
        value = sign * 10**digits
        # A Python ``int`` has no width, so ``f"{value}"`` inside a refusal is a
        # message whose length the *caller* picks. Past CPython's 4300-digit
        # conversion limit it stops being this module's message at all:
        # ``str(int)`` raises, and what comes back is the interpreter advising
        # the caller to raise ``sys.set_int_max_str_digits`` — a refusal that no
        # longer says what failed, what to do, or which parameter it was about.
        #
        # A ``ValueError`` specifically, and that is the second half: converting
        # such an int to a float for the ``satisfy_to`` check raises
        # ``OverflowError``, which is not a ``ValueError``, so a caller catching
        # the refusal this module documents would not have caught that one at all.
        with pytest.raises(ValueError) as caught:
            GoalParams(**{field_name: value})
        message = str(caught.value)
        assert field_name in message
        assert "set_int_max_str_digits" not in message
        assert len(message) <= MAX_DETAIL_CHARS, len(message)

    def test_a_budget_refusal_is_bounded_the_same_way(self) -> None:
        huge = 10**4_400
        for kwargs in (
            {"max_wall_ms": huge, "max_steps": 1, "pending_ttl_ms": 1_000},
            {"max_wall_ms": 5_000, "max_steps": huge, "pending_ttl_ms": 1_000},
            {"max_wall_ms": 5_000, "max_steps": 1, "pending_ttl_ms": huge},
        ):
            with pytest.raises(ValueError) as caught:
                GoalBudget(**kwargs)
            message = str(caught.value)
            assert "must be within" in message
            assert "set_int_max_str_digits" not in message
            assert len(message) <= MAX_DETAIL_CHARS

    def test_the_rendering_bound_holds_at_every_extreme_a_value_can_reach(self) -> None:
        # ``MAX_RENDERED_VALUE_CHARS`` is held by construction rather than by a
        # truncation, so the claim is that *no admissible value* renders longer
        # — which is a claim about the extremes, and they are walked here.
        extremes: list[float] = [
            0,
            -1,
            10**18 - 1,
            -(10**18) + 1,
            10**18,
            -(10**18),
            10**4_400,
            sys.float_info.max,
            -sys.float_info.max,
            sys.float_info.min,
            -sys.float_info.min,
            5e-324,
            math.inf,
            -math.inf,
            math.nan,
            -0.0,
        ]
        for value in extremes:
            rendered = goal_model._render_value(value)
            assert len(rendered) <= MAX_RENDERED_VALUE_CHARS, (len(rendered), rendered)
            assert "\n" not in rendered

    def test_a_value_small_enough_to_read_is_still_quoted(self) -> None:
        # The cap must not cost the diagnostic its point: a range refusal that
        # named no value would send the caller back to guess which of their
        # numbers was wrong.
        with pytest.raises(ValueError, match=r"target_level must be within 1\.\.10, got 11$"):
            GoalParams(target_level=11)
        with pytest.raises(ValueError, match=r"pages must be within 1\.\.200, got 100000$"):
            GoalParams(pages=100_000)

    def test_a_record_repr_cannot_leak_the_idempotency_key(self) -> None:
        queue = make_queue(FakeClock())
        secret = "sk-ant-not-a-real-key"
        admitted = queue.submit(eat(secret))
        assert admitted.goal is not None
        assert secret not in repr(admitted.goal)
        assert admitted.goal.key_digest == key_digest(secret)


# --------------------------------------------------------------------------
# value objects
# --------------------------------------------------------------------------


class TestBoundedDetail:
    """The diagnostic a goal carries is the last string on this surface.

    It is assembled by the channel from constants and numbers, and a caller may
    add one through :meth:`GoalQueue.fail`. That makes it the one field on a
    record that could carry a transcript into a log line, so it is bounded and
    single-line, and both are checked where the record is built rather than
    where it is printed.
    """

    def test_a_detail_past_the_bound_is_refused(self) -> None:
        a_record(detail="x" * MAX_DETAIL_CHARS)
        with pytest.raises(ValueError, match=f"at most {MAX_DETAIL_CHARS} characters"):
            a_record(detail="x" * (MAX_DETAIL_CHARS + 1))

    @pytest.mark.parametrize("detail", ["two\nlines", "a\rb", "a\tb", "a\x00b", "a\x7fb"])
    def test_a_detail_that_is_not_one_printable_line_is_refused(self, detail: str) -> None:
        with pytest.raises(ValueError, match="single line of printable text"):
            a_record(detail=detail)

    def test_a_transition_is_bounded_the_same_way(self) -> None:
        with pytest.raises(ValueError, match="single line of printable text"):
            GoalTransition(
                goal_id="g",
                kind=GoalKind.SATISFY_HUNGER,
                previous=GoalState.ACTIVE,
                state=GoalState.FAILED,
                reason_code=ReasonCode.NO_SAFE_FOOD,
                at_ms=0,
                detail="line\ninjected",
            )

    def test_a_caller_supplied_detail_is_refused_and_the_goal_still_ends(self) -> None:
        # Refusing rather than truncating costs something, and this is the cost:
        # the failure report is rejected and the goal stays active. It still
        # reaches a terminal state, because the wall-clock budget is what
        # guarantees that and it is not on this path.
        clock = FakeClock()
        queue = make_queue(clock)
        budget = GoalBudget(max_wall_ms=1_000, max_steps=4, pending_ttl_ms=600_000)
        started = start_one(
            queue, GoalRequest(kind=GoalKind.SATISFY_HUNGER, idempotency_key="k", budget=budget)
        )
        with pytest.raises(ValueError, match="at most"):
            queue.fail(started.goal_id, ReasonCode.NO_SAFE_FOOD, "x" * (MAX_DETAIL_CHARS + 1))
        still_running = queue.record(started.goal_id)
        assert still_running is not None
        assert still_running.state is GoalState.ACTIVE
        clock.now = 1_000
        assert [t.state for t in queue.tick()] == [GoalState.EXPIRED]

    def test_every_detail_the_channel_writes_itself_fits_the_bound(self) -> None:
        # The bound is only useful if the channel's own longest message is
        # comfortably inside it; a constant that the code it guards already
        # exceeds is a constant that gets raised rather than obeyed.
        clock = FakeClock()
        queue = make_queue(clock, max_open=4)
        budget = GoalBudget(
            max_wall_ms=MAX_GOAL_WALL_MS, max_steps=1, pending_ttl_ms=goal_model.MAX_PENDING_TTL_MS
        )
        produced: list[str] = []

        stepped = start_one(
            queue, GoalRequest(kind=GoalKind.SATISFY_HUNGER, idempotency_key="steps", budget=budget)
        )
        exhausted = queue.note_step(stepped.goal_id)
        assert exhausted is not None
        produced.append(exhausted.detail)

        timed_out = start_one(
            queue, GoalRequest(kind=GoalKind.SATISFY_HUNGER, idempotency_key="wall", budget=budget)
        )
        clock.now = timed_out.submitted_at_ms + MAX_GOAL_WALL_MS
        produced.extend(t.detail for t in queue.tick())

        assert queue.submit(
            GoalRequest(kind=GoalKind.SATISFY_HUNGER, idempotency_key="ttl", budget=budget)
        ).accepted
        clock.now += goal_model.MAX_PENDING_TTL_MS
        produced.extend(t.detail for t in queue.tick())

        queue.arm()
        cancelled = start_one(queue, eat("cancel"))
        assert queue.request_cancel(cancelled.goal_id) is True
        produced.extend(t.detail for t in queue.tick())

        queue.arm()
        start_one(queue, eat("disarm"))
        produced.extend(t.detail for t in queue.disarm())

        queue.arm()
        start_one(queue, eat("panic"))
        produced.extend(t.detail for t in queue.panic_stop())
        produced.append(observed_detail(queue))

        assert len(produced) == 7, "the walk missed one of the terminal paths"
        for detail in produced:
            assert detail.strip(), "a terminal transition with no detail says nothing"
            assert len(detail) <= MAX_DETAIL_CHARS, detail


def observed_detail(queue: GoalQueue) -> str:
    """The detail the success path writes, on a freshly armed queue."""
    queue.arm()
    started = start_one(queue, eat("observed"))
    transition = queue.succeed(started.goal_id, observed())
    assert transition is not None
    return transition.detail


class TestValueObjects:
    def test_an_admission_carries_exactly_one_answer(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            GoalAdmission()
        record = GoalRecord(
            goal_id="g",
            kind=GoalKind.SATISFY_HUNGER,
            params=GoalParams(),
            budget=GOAL_SPECS[GoalKind.SATISFY_HUNGER].budget,
            key_digest="0" * 16,
            sequence=1,
            state=GoalState.PENDING,
            submitted_at_ms=0,
        )
        refusal = GoalRefusal(reason_code=ReasonCode.QUEUE_REJECTED, message="full; wait.")
        with pytest.raises(ValueError, match="exactly one"):
            GoalAdmission(goal=record, refusal=refusal)
        with pytest.raises(ValueError, match="cannot be a duplicate"):
            GoalAdmission(refusal=refusal, duplicate=True)

    def test_a_transition_must_change_something(self) -> None:
        with pytest.raises(ValueError, match="must change the state"):
            GoalTransition(
                goal_id="g",
                kind=GoalKind.SATISFY_HUNGER,
                previous=GoalState.ACTIVE,
                state=GoalState.ACTIVE,
                reason_code=None,
                at_ms=0,
            )

    def test_a_terminal_transition_must_say_why(self) -> None:
        with pytest.raises(ValueError, match="must say why"):
            GoalTransition(
                goal_id="g",
                kind=GoalKind.SATISFY_HUNGER,
                previous=GoalState.ACTIVE,
                state=GoalState.FAILED,
                reason_code=None,
                at_ms=0,
            )

    def test_records_are_immutable(self) -> None:
        queue = make_queue(FakeClock())
        record = queue.submit(eat()).goal
        assert record is not None
        with pytest.raises(FrozenInstanceError):
            record.state = GoalState.ACTIVE  # type: ignore[misc]

    def test_steps_used_cannot_exceed_the_budget(self) -> None:
        budget = GoalBudget(max_wall_ms=5_000, max_steps=2, pending_ttl_ms=1_000)
        with pytest.raises(ValueError, match=r"steps_used must be within 0\.\.2"):
            GoalRecord(
                goal_id="g",
                kind=GoalKind.SATISFY_HUNGER,
                params=GoalParams(),
                budget=budget,
                key_digest="0" * 16,
                sequence=1,
                state=GoalState.PENDING,
                submitted_at_ms=0,
                steps_used=3,
            )

    def test_a_goal_cannot_finish_before_it_started(self) -> None:
        with pytest.raises(ValueError, match="cannot finish before it started"):
            GoalRecord(
                goal_id="g",
                kind=GoalKind.SATISFY_HUNGER,
                params=GoalParams(),
                budget=GOAL_SPECS[GoalKind.SATISFY_HUNGER].budget,
                key_digest="0" * 16,
                sequence=1,
                state=GoalState.FAILED,
                submitted_at_ms=0,
                started_at_ms=500,
                finished_at_ms=400,
                reason_code=ReasonCode.NO_SAFE_FOOD,
            )

    def test_a_goal_cannot_finish_before_it_was_submitted(self) -> None:
        # The third of the three timestamp invariants, and the only one that
        # covers a goal terminated while it was still pending — it has no
        # ``started_at_ms``, so neither of the other two checks looks at it.
        # Nothing exercised this one, and replacing it with ``if False`` was
        # green.
        with pytest.raises(ValueError, match="cannot finish before it was submitted"):
            GoalRecord(
                goal_id="g",
                kind=GoalKind.SATISFY_HUNGER,
                params=GoalParams(),
                budget=GOAL_SPECS[GoalKind.SATISFY_HUNGER].budget,
                key_digest="0" * 16,
                sequence=1,
                state=GoalState.CANCELLED,
                submitted_at_ms=900,
                finished_at_ms=100,
                reason_code=ReasonCode.CANCELLED_BY_REQUEST,
            )

    def test_a_goal_cannot_start_before_it_was_submitted(self) -> None:
        with pytest.raises(ValueError, match="cannot start before it was submitted"):
            GoalRecord(
                goal_id="g",
                kind=GoalKind.SATISFY_HUNGER,
                params=GoalParams(),
                budget=GOAL_SPECS[GoalKind.SATISFY_HUNGER].budget,
                key_digest="0" * 16,
                sequence=1,
                state=GoalState.ACTIVE,
                submitted_at_ms=900,
                started_at_ms=100,
            )


# --------------------------------------------------------------------------
# the bridge to the deterministic planner
# --------------------------------------------------------------------------


class TestPlannerBridge:
    @pytest.mark.parametrize(
        ("kind", "expected_planner_value"),
        [
            # Hand-written pairs. A mapping that swapped hunger and thirst would
            # round-trip through the module without complaint.
            (GoalKind.SATISFY_HUNGER, "satisfy_hunger"),
            (GoalKind.SATISFY_THIRST, "satisfy_thirst"),
            (GoalKind.READ_FOR_BOREDOM, "read_for_boredom"),
            (GoalKind.LEARN_RECIPE, "learn_recipe"),
        ],
    )
    def test_each_kind_maps_to_its_planner_goal(
        self, kind: GoalKind, expected_planner_value: str
    ) -> None:
        queue = make_queue(FakeClock())
        admitted = queue.submit(GoalRequest(kind=kind, idempotency_key="k"))
        assert admitted.goal is not None
        planner_goal = to_planner_goal(admitted.goal)
        assert planner_goal.kind.value == expected_planner_value
        assert planner_goal.goal_id == admitted.goal.goal_id
        assert planner_goal.skill == ""

    def test_train_skill_widens_the_enum_back_to_the_planner_string(self) -> None:
        queue = make_queue(FakeClock())
        admitted = queue.submit(
            GoalRequest(
                kind=GoalKind.TRAIN_SKILL,
                idempotency_key="k",
                params=GoalParams(skill=TrainableSkill.CARPENTRY),
            )
        )
        assert admitted.goal is not None
        planner_goal = to_planner_goal(admitted.goal)
        assert planner_goal.kind.value == "train_skill"
        assert planner_goal.skill == "carpentry"

    def test_the_mapping_is_total(self) -> None:
        queue = make_queue(FakeClock(), max_open=len(GoalKind))
        for index, kind in enumerate(GoalKind):
            admitted = queue.submit(
                GoalRequest(kind=kind, idempotency_key=f"k-{index}", params=params_for(kind))
            )
            assert admitted.goal is not None
            to_planner_goal(admitted.goal)


class TestImportTimeTables:
    """The consistency checks that run at import, exercised on purpose.

    They cannot fire in a healthy tree, which is exactly why they need a test:
    a guard nobody has ever seen fail is a guard nobody knows still works.
    """

    def test_a_missing_spec_row_is_caught(self, monkeypatch: pytest.MonkeyPatch) -> None:
        trimmed = {k: v for k, v in GOAL_SPECS.items() if k is not GoalKind.LEARN_RECIPE}
        monkeypatch.setattr(goal_model, "GOAL_SPECS", trimmed)
        with pytest.raises(RuntimeError, match="missing a row for"):
            goal_model._check_tables()

    def test_a_parameter_with_no_declared_range_is_caught(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broken = dict(GOAL_SPECS)
        broken[GoalKind.LEARN_RECIPE] = GoalSpec(
            required=frozenset(),
            optional=frozenset({"pages", "target_level"}),
            budget=GOAL_SPECS[GoalKind.LEARN_RECIPE].budget,
        )
        monkeypatch.setattr(goal_model, "GOAL_SPECS", broken)
        monkeypatch.setattr(goal_model, "NUMERIC_RANGES", {"pages": NUMERIC_RANGES["pages"]})
        with pytest.raises(RuntimeError, match="no range in NUMERIC_RANGES"):
            goal_model._check_tables()

    def test_an_unknown_parameter_name_is_caught(self, monkeypatch: pytest.MonkeyPatch) -> None:
        broken = dict(GOAL_SPECS)
        broken[GoalKind.LEARN_RECIPE] = GoalSpec(
            required=frozenset(),
            optional=frozenset({"transcript"}),
            budget=GOAL_SPECS[GoalKind.LEARN_RECIPE].budget,
        )
        monkeypatch.setattr(goal_model, "GOAL_SPECS", broken)
        with pytest.raises(RuntimeError, match="unknown parameter"):
            goal_model._check_tables()

    def test_param_names_drift_is_caught(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(goal_model, "PARAM_NAMES", ("skill", "pages"))
        with pytest.raises(RuntimeError, match="PARAM_NAMES has drifted"):
            goal_model._check_tables()

    def test_a_kind_the_planner_cannot_serve_is_caught(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trimmed = {
            k: v for k, v in goal_model._PLANNER_KIND.items() if k is not GoalKind.TRAIN_SKILL
        }
        monkeypatch.setattr(goal_model, "_PLANNER_KIND", trimmed)
        with pytest.raises(RuntimeError, match="no planner goal for"):
            goal_model._check_planner_mapping()

    def test_the_healthy_tree_passes_both(self) -> None:
        goal_model._check_tables()
        goal_model._check_planner_mapping()
