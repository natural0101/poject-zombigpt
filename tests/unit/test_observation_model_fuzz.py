"""Seeded fuzz over ``Observation.from_dict`` and the diff / compact readers.

:mod:`tests.unit.test_protocol_messages` pins the hand-picked observation cases
(the round trip, the honest-success rule, the safe stat defaults) and
:mod:`tests.unit.test_observation_diff` pins the diff invariant on a curated set
of snapshot pairs. Neither walks the *neighbourhood* of a valid observation
systematically, and the neighbourhood is where a boundary parser crashes: one
field's type swapped, one required key removed, one number past what the
interpreter will convert, one string full of control bytes.

An observation reaches the sidecar from the mod, over the journal, already
``json.loads``-parsed — so the hostile input is not bytes but a *dict* with
every field wrong. The contract every reader here documents is total: a payload
either builds the typed model or raises the reader's own named error —
:class:`ProtocolError` for :meth:`Observation.from_dict`, :class:`DiffError` for
:meth:`ObservationDiff.from_dict`. Nothing else may escape. A bare ``KeyError``
from a missing field, a ``TypeError`` from a wrong-typed one, an
``OverflowError`` from an absurd number or a ``RecursionError`` from deep nesting
is a denial of service one observation wide: the callers in
:mod:`pz_agent_core.diagnostics` catch only the named error and let anything else
through raw.

Every generator is driven by ``random.Random`` seeded from a fixed literal —
never the global ``random`` module, never the clock — so each run replays the
same corpus and a failure names the seed and index that rebuild it. Per-index
labels vary by index so refs stay unique and each mutation is distinguishable.

Writing this file found three real escapes. One is fixed here, in this task's
edit scope: :meth:`ObservationDiff.from_dict` recursed once per nesting level and
overflowed the stack with a ``RecursionError`` — now bounded before the descent
by :data:`~pz_agent_core.observation.diff.MAX_DELTA_DEPTH`, refused as a typed
:class:`DiffError`, and pinned by
:func:`test_a_deeply_nested_diff_is_refused_not_a_recursion_error`. The other two
live in ``packages/.../protocol/`` — outside this task's edit scope — so they are
*reported*, not fixed: they are the ``finds_``-prefixed reproducers at the bottom
of this file (excluded from collection because they do not start with ``test``),
each naming the exact input that triggers it. The broad corpus deliberately
steers under both of those boundaries — bounded integers, and non-finite floats
kept out of the compact reader — so its property covers the general shape while
those two reproducers pin the exact edges.

The corpus is sized to finish in a second or two; the repository-wide 300-second
pytest cap is the enforcement, not an assertion here.
"""

from __future__ import annotations

import copy
import random
import uuid
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Final

import pytest

from pz_agent_core.observation.compact import compact_for_planner
from pz_agent_core.observation.diff import (
    MAX_DELTA_DEPTH,
    DiffError,
    ObservationDiff,
    apply_diff,
    diff_observations,
)
from pz_agent_core.protocol import (
    ActionOwnership,
    ActionState,
    CapabilityState,
    ContainerKind,
    ContainerView,
    DangerLevel,
    GameState,
    Hands,
    InventoryView,
    ItemView,
    JsonDict,
    NearbyObject,
    NearbyView,
    NearbyZombie,
    Observation,
    PlayerState,
    Position,
    ProtocolError,
    SafetyState,
    SessionMode,
    Wound,
)
from tests.fixtures import make_observation

#: One fixed literal per property. A failure quotes the seed and the case index,
#: which together rebuild the exact input on any machine.
MUTATION_SEED: Final = 0x0B5E0B
ROUND_TRIP_SEED: Final = 0x0FF5E7
STRUCTURAL_SEED: Final = 0x5CA1AB1E
DIFF_SEED: Final = 0xD1FFED
COMPACT_SEED: Final = 0xC0FFEE

#: Below the edge where ``float(int)`` overflows a C double: the broad corpus
#: stays under it so the property holds, while the exact overflow is pinned by
#: :func:`finds_a_huge_integer_in_a_float_field_overflows_observation_from_dict`.
_SAFE_INT_BOUND: Final = 2**53

#: Word material, one script per alphabet on purpose: the project ships to a
#: Russian-language install, so Cyrillic and astral characters must survive the
#: boundary as data, and the control-laden alphabet drives strings that a naive
#: reader might try to interpret rather than carry.
_ALPHABETS: Final = (
    "abcdefghijklmnopqrstuvwxyz0123456789._-",
    "выживаниезомби",
    "🧟🌲🔧🩸",
    "\x00\x07\x1f\x7f\t\n ",
)


def _word(rng: random.Random, cap: int) -> str:
    alphabet = rng.choice(_ALPHABETS)
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(1, cap)))


def _label(rng: random.Random, index: int) -> str:
    """A free-text label that varies by index in both alphabet and suffix."""
    alphabet = _ALPHABETS[index % len(_ALPHABETS)]
    body = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 12)))
    return f"{body}-{index}"


def _hostile_pool(rng: random.Random) -> tuple[object, ...]:
    """Wrong-typed and edge values. Integers stay bounded so the pool never
    trips the ``float(int)`` overflow the protocol layer owns; the non-finite
    floats are included on purpose — ``from_dict`` must *tolerate* them (a float
    field accepts them, an int field refuses them), never crash on them.
    """
    return (
        None,
        True,
        False,
        0,
        1,
        -1,
        rng.randint(-_SAFE_INT_BOUND, _SAFE_INT_BOUND),
        0.0,
        1.5,
        -3.25,
        rng.uniform(-1e6, 1e6),
        float("inf"),
        float("-inf"),
        float("nan"),
        "",
        "x",
        _word(rng, 8),
        [],
        [1, 2],
        ["a"],
        {},
        {"k": "v"},
        {"ref": "r"},
    )


# --------------------------------------------------------------------------
# a rich, valid observation — the baseline every mutator walks outward from
# --------------------------------------------------------------------------


def _rich_observation(
    rng: random.Random,
    index: int,
    *,
    session: str | None = None,
    seq: int | None = None,
) -> Observation:
    """A valid observation with every optional sub-structure populated.

    Built from the typed dataclasses so it is valid by construction; the wire
    form (``to_dict``) is what the mutators corrupt. Refs and labels carry the
    index so successive observations differ where a diff can see it.
    """
    session_id = session if session is not None else str(uuid.UUID(int=0x5E5510 + index))
    main = f"container:{session_id}:player-main"
    back = f"container:{session_id}:worn:Back:{index}"

    def iref(n: int) -> str:
        return f"item:{session_id}:player-main:{index}-{n}:0"

    game = GameState(
        build="42.20",
        save_id=f"save-{index:04d}",
        paused=bool(index % 2),
        speed=round(rng.uniform(0.0, 3.0), 3),
        world_time="1993-07-09T14:20:00",
        multiplayer=False,
    )
    player = PlayerState(
        present=True,
        alive=True,
        position=Position(
            x=1200.0 + index,
            y=3400.0 - index,
            z=index % 3,
            direction=rng.choice(("N", "S", "E", "W")),
        ),
        stats={
            "health": round(rng.random(), 3),
            "hunger": round(rng.random(), 3),
            "cooked": bool(index % 2),
            "unknown_build42": None,
        },
        moodles={f"moodle_{index}_{i}": rng.randint(1, 4) for i in range(index % 3)},
        wounds=[
            Wound(
                ref=f"wound:{session_id}:{index}-{w}",
                kind="laceration",
                severity=round(rng.random(), 3),
                bleeding=bool(w % 2),
            )
            for w in range(index % 2 + 1)
        ],
        hands=Hands(primary=iref(0) if index % 2 else None),
    )
    safety = SafetyState(
        armed=bool(index % 2),
        mode=SessionMode.ASSISTED,
        danger_level=DangerLevel.NONE,
        manual_takeover=False,
        sidecar_stale=False,
    )
    action = ActionState(
        ownership=ActionOwnership.MOD,
        busy=bool(index % 2),
        action_id=f"act-{index}" if index % 2 else None,
        type="reading" if index % 2 else None,
        progress=round(rng.random(), 3) if index % 2 else None,
    )
    containers = [
        ContainerView(
            ref=main,
            kind=ContainerKind.PLAYER_MAIN,
            name="Inventory",
            parent_ref=None,
            capacity=20.0,
            used_capacity=round(rng.uniform(0.0, 10.0), 2),
        ),
        ContainerView(
            ref=back,
            kind=ContainerKind.WORN,
            name=_label(rng, index),
            parent_ref=main,
        ),
    ]
    items = [
        ItemView(
            ref=iref(n),
            container_ref=main,
            full_type="Base.TinnedBeans",
            display_name=_label(rng, index * 10 + n),
            category="Food",
            weight=round(rng.uniform(0.0, 3.0), 3),
            favorite=bool(n % 2),
            equipped=bool((n + 1) % 2),
            tags=[f"tag{index}_{n}"],
            food={"hunger_change": -0.3, "calories": 120} if n % 2 == 0 else None,
            literature={"title": _label(rng, n), "already_read": False} if n % 3 == 0 else None,
            extra={f"extra_{index}": n} if n % 2 else {},
        )
        for n in range(index % 4)
    ]
    inventory = InventoryView(containers=containers, items=items)
    zombies = [
        NearbyZombie(
            ref=f"zombie:{session_id}:{index}-{n}:0",
            distance=round(rng.uniform(0.0, 50.0), 1),
            chasing=bool(n % 2),
            position=Position(x=float(n), y=float(n), z=0) if n % 2 else None,
        )
        for n in range(index % 3)
    ]
    objects = [
        NearbyObject(
            ref=f"object:{session_id}:{index}-{n}:0",
            kind="tree",
            distance=round(rng.uniform(0.0, 50.0), 1),
            semantics=[f"sem{index}_{n}"],
        )
        for n in range(index % 3)
    ]
    nearby = NearbyView(objects=objects, zombies=zombies)
    return Observation(
        session_id=session_id,
        seq=seq if seq is not None else index + 1,
        timestamp_ms=1_700_000_000_000 + index,
        game=game,
        player=player,
        safety=safety,
        action=action,
        full=bool(index % 2 == 0),
        inventory=inventory,
        nearby=nearby,
        capability_revision=index,
        active_goal_id=f"goal-{index}" if index % 2 else None,
    )


# --------------------------------------------------------------------------
# structural walkers over the wire form
# --------------------------------------------------------------------------


def _paths(node: Any, prefix: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    """Every addressable position in *node*: dict keys and list indices, at
    every depth. Intermediate containers are yielded too, so a mutator can
    replace a whole object with a scalar (which the strict readers must refuse).
    """
    if isinstance(node, dict):
        for key, value in node.items():
            path = (*prefix, key)
            yield path
            yield from _paths(value, path)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            path = (*prefix, i)
            yield path
            yield from _paths(value, path)


def _with_value(doc: Any, path: tuple[Any, ...], value: object) -> Any:
    out = copy.deepcopy(doc)
    node = out
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value
    return out


def _without(doc: Any, path: tuple[Any, ...]) -> Any:
    out = copy.deepcopy(doc)
    node = out
    for step in path[:-1]:
        node = node[step]
    last = path[-1]
    if isinstance(node, list):
        del node[last]
    else:
        node.pop(last, None)
    return out


def _typed_or_parsed(
    parse: Callable[[Mapping[str, Any]], object],
    payload: Mapping[str, Any],
    promised: type[Exception],
    label: str,
) -> None:
    """The property every hostile payload is held to: *parse* either returns a
    value or raises the exact error it documents. Any other exception is the
    escape this file exists to catch, and the label rebuilds the case.
    """
    try:
        parse(payload)
    except promised:
        return
    except Exception as escape:
        pytest.fail(
            f"{label}: {type(escape).__name__} escaped a parser that promises "
            f"{promised.__name__} ({escape})"
        )


# --------------------------------------------------------------------------
# Observation.from_dict
# --------------------------------------------------------------------------


class TestObservationFromDictYieldsProtocolErrorOrAModel:
    """Property: :meth:`Observation.from_dict` returns an ``Observation`` or
    raises :class:`ProtocolError`, on every hostile dict — never a bare
    exception.

    Real defect class each method catches — the bug that would make it fail:

    * wrong type: ``KeyError``/``TypeError``/``AttributeError`` from a reader
      that indexes or calls a method on a value before checking its type;
    * missing required: a bare ``KeyError`` from ``payload[key]`` where a
      ``_require`` was skipped;
    * wrong nesting: a scalar reached where an object or array was assumed,
      crashing ``.items()``/iteration instead of refusing;
    * unicode/control: a reader that validates a string by decoding or matching
      it and raises rather than carrying it as opaque data;
    * absurd (bounded) numbers: an off-by-one bound check that raises instead of
      returning the typed refusal.
    """

    def test_every_field_replaced_by_every_wrong_typed_value(self) -> None:
        rng = random.Random(MUTATION_SEED)
        baseline = _rich_observation(rng, 7).to_dict()
        pool = _hostile_pool(rng)
        for path in _paths(baseline):
            for wrong in pool:
                label = f"seed={MUTATION_SEED:#x} path={path} wrong={wrong!r}"
                _typed_or_parsed(
                    Observation.from_dict, _with_value(baseline, path, wrong), ProtocolError, label
                )

    def test_removing_each_field_is_refused_or_defaulted_never_a_crash(self) -> None:
        rng = random.Random(MUTATION_SEED + 1)
        baseline = _rich_observation(rng, 5).to_dict()
        for path in _paths(baseline):
            label = f"seed={MUTATION_SEED + 1:#x} deleted={path}"
            _typed_or_parsed(Observation.from_dict, _without(baseline, path), ProtocolError, label)

    def test_a_bare_and_an_empty_document_are_refused(self) -> None:
        for payload in ({}, {"schema_version": "1.0"}, {"session_id": "not-a-uuid"}):
            with pytest.raises(ProtocolError):
                Observation.from_dict(payload)

    def test_absurd_but_bounded_numbers_never_overflow_the_parser(self) -> None:
        """Numbers large enough to matter yet under the ``float(int)`` edge the
        protocol layer owns: the parser must round-trip or refuse them, not
        raise ``OverflowError``. The edge itself is pinned by the ``finds_``
        reproducer.
        """
        rng = random.Random(MUTATION_SEED + 2)
        baseline = _rich_observation(rng, 3).to_dict()
        numeric_edges = (
            _SAFE_INT_BOUND,
            -_SAFE_INT_BOUND,
            0,
            10**15,
            -(10**15),
            1e300,
            -1e300,
            2.2250738585072014e-308,
        )
        numeric_paths = (
            ("game", "speed"),
            ("player", "position", "x"),
            ("player", "position", "y"),
            ("timestamp_ms",),
            ("seq",),
            ("capability_revision",),
        )
        for path in numeric_paths:
            for value in numeric_edges:
                label = f"seed={MUTATION_SEED + 2:#x} numeric path={path} value={value!r}"
                _typed_or_parsed(
                    Observation.from_dict, _with_value(baseline, path, value), ProtocolError, label
                )

    def test_seeded_multi_edit_mutations_stay_within_the_contract(self) -> None:
        """The generator most likely to reach a crash no structured mutator
        imagined: one to four random edits per case over fresh valid baselines.
        """
        rng = random.Random(STRUCTURAL_SEED)
        for case in range(300):
            document = _rich_observation(rng, case % 12).to_dict()
            pool = _hostile_pool(rng)
            for _ in range(rng.randint(1, 4)):
                # Re-derive paths every edit: replacing a container with a scalar
                # invalidates the paths beneath it just as a deletion does, so a
                # stale path from a previous round could name a node that is gone.
                paths = list(_paths(document))
                if not paths:
                    break
                path = rng.choice(paths)
                if rng.random() < 0.2:
                    document = _without(document, path)
                else:
                    document = _with_value(document, path, rng.choice(pool))
            label = f"seed={STRUCTURAL_SEED:#x} multi-edit case={case}"
            _typed_or_parsed(Observation.from_dict, document, ProtocolError, label)


class TestValidObservationsRoundTrip:
    """Property: a valid observation survives ``to_dict``/``from_dict`` intact.

    Real defect class: a field dropped or renamed in serialisation, a decoder
    default silently substituting for a value that was present, or a numeric
    type collapsing (bool to int, int to float) anywhere in the nested tree —
    none of which the hostile-input property can see, because they produce a
    *wrong* value, not a crash.
    """

    def test_a_generated_observation_survives_the_wire(self) -> None:
        rng = random.Random(ROUND_TRIP_SEED)
        for index in range(120):
            observation = _rich_observation(rng, index)
            rebuilt = Observation.from_dict(observation.to_dict())
            assert rebuilt == observation, f"seed={ROUND_TRIP_SEED:#x} index={index}"


# --------------------------------------------------------------------------
# ObservationDiff.from_dict and the diff round trip
# --------------------------------------------------------------------------


class TestObservationDiffFromDictYieldsDiffErrorOrADelta:
    """Property: :meth:`ObservationDiff.from_dict` returns an ``ObservationDiff``
    or raises :class:`DiffError`, on every hostile dict.

    Real defect class each method catches:

    * wrong type / nesting: a strict ``_object``/``_string_list`` reader skipped,
      letting a scalar reach ``.items()`` or a non-string reach a ref position;
    * unbounded recursion: ``RecursionError`` from a delta nested past the stack,
      where the reader promises ``DiffError`` — the escape this file fixed.
    """

    def test_every_field_of_a_serialised_diff_wrong_typed(self) -> None:
        rng = random.Random(DIFF_SEED)
        session = str(uuid.UUID(int=0x5E5510))
        previous = _rich_observation(rng, 4, session=session, seq=10)
        current = _rich_observation(rng, 5, session=session, seq=11)
        wire = diff_observations(previous, current).to_dict()
        pool = _hostile_pool(rng)
        for path in _paths(wire):
            for wrong in pool:
                label = f"seed={DIFF_SEED:#x} path={path} wrong={wrong!r}"
                _typed_or_parsed(
                    ObservationDiff.from_dict, _with_value(wire, path, wrong), DiffError, label
                )
            _typed_or_parsed(
                ObservationDiff.from_dict,
                _without(wire, path),
                DiffError,
                f"seed={DIFF_SEED:#x} deleted={path}",
            )

    def test_nesting_up_to_the_bound_is_a_delta_never_a_crash(self) -> None:
        """Depth from one to the accepted bound, as chains of ``nested`` and of
        ``lists.changed`` — the parser must build a delta or refuse, never
        recurse off the stack.
        """
        for levels in range(1, MAX_DELTA_DEPTH + 1):
            for wrapper in ("nested", "changed"):
                delta: JsonDict = {}
                cursor = delta
                for _ in range(levels):
                    nxt: JsonDict = {}
                    if wrapper == "nested":
                        cursor["nested"] = {"k": nxt}
                    else:
                        cursor["lists"] = {"items": {"order": [], "changed": {"r": nxt}}}
                    cursor = nxt
                document = {
                    "session_id": "s",
                    "from_seq": 1,
                    "to_seq": 2,
                    "timestamp_ms": 0,
                    "delta": delta,
                }
                label = f"depth={levels} wrapper={wrapper}"
                _typed_or_parsed(ObservationDiff.from_dict, document, DiffError, label)


class TestDiffRoundTripRebuildsTheNextSnapshot:
    """Property: ``apply_diff(prev, from_dict(diff(prev, cur).to_dict())) == cur``
    over generated snapshot pairs.

    Real defect class: a serialised delta that reconstructs an *equivalent*
    snapshot rather than the exact one — a ref-keyed array reordered, an optional
    field that went to None encoded as a removal it cannot replay, a scalar type
    lost across the wire. The curated diff test covers a dozen shapes; this walks
    the space of them.
    """

    def test_generated_pairs_survive_serialisation_and_apply(self) -> None:
        rng = random.Random(DIFF_SEED + 1)
        for index in range(80):
            session = str(uuid.UUID(int=0xA11CE + index))
            previous = _rich_observation(rng, index, session=session, seq=100)
            current = _rich_observation(
                rng, index + 1, session=session, seq=100 + rng.randint(0, 3)
            )
            wire = diff_observations(previous, current).to_dict()
            rebuilt = apply_diff(previous, ObservationDiff.from_dict(wire))
            assert rebuilt == current, f"seed={DIFF_SEED + 1:#x} index={index}"


# --------------------------------------------------------------------------
# the compact planner view
# --------------------------------------------------------------------------


class TestCompactReaderNeverCrashesOnAValidObservation:
    """Property: :func:`compact_for_planner` returns a plain-JSON view for any
    valid, finite observation — it never raises.

    Real defect class: a reader that assumes a field shape and crashes on a
    valid-but-unusual value — a moodle whose name is not a token, a stat that is
    a bool or None, an empty inventory, a coordinate at the far end of the map.
    The one crash this reader *can* still take — a non-finite coordinate through
    ``int()`` — is rooted in the protocol layer and pinned by the ``finds_``
    reproducer; the corpus here keeps every float finite so the property holds.
    """

    def test_bounded_observations_compact_without_raising(self) -> None:
        rng = random.Random(COMPACT_SEED)
        states = list(CapabilityState)
        for index in range(120):
            observation = _rich_observation(rng, index)
            capabilities = {
                name: states[(index + i) % len(states)]
                for i, name in enumerate(
                    ("move_to_square", "drink_world_source", f"cap_{index}", "autonomous_attack")
                )
            }
            view = compact_for_planner(observation, capabilities)
            assert isinstance(view, dict), f"seed={COMPACT_SEED:#x} index={index}"
            assert view["view"] == "compact_observation"


# --------------------------------------------------------------------------
# the escape this file's one production fix closed
# --------------------------------------------------------------------------


def test_a_deeply_nested_diff_is_refused_not_a_recursion_error() -> None:
    """The escape the fuzzer found, now closed.

    Found by the diff-nesting generator: :meth:`MappingDelta.from_dict` recursed
    once per nesting level, so a delta of a few hundred nested ``nested`` objects
    — a few kilobytes — overflowed the interpreter with a ``RecursionError``
    where :meth:`ObservationDiff.from_dict` promises :class:`DiffError`.
    :data:`MAX_DELTA_DEPTH` now bounds the descent before it starts, so the
    reader answers with its own typed refusal instead of a bare interpreter
    error. Real defect class: unbounded recursion in a boundary parser.
    """
    delta: JsonDict = {}
    cursor = delta
    for _ in range(MAX_DELTA_DEPTH * 4):
        nxt: JsonDict = {}
        cursor["nested"] = {"k": nxt}
        cursor = nxt
    document = {
        "session_id": "s",
        "from_seq": 1,
        "to_seq": 2,
        "timestamp_ms": 0,
        "delta": delta,
    }
    with pytest.raises(DiffError, match="nests deeper"):
        ObservationDiff.from_dict(document)


def test_a_diff_within_the_depth_bound_still_parses() -> None:
    """The other side of the bound: nesting just under it is accepted, so the
    guard refuses only the pathological document, not an honest deep one.
    """
    delta: JsonDict = {}
    cursor = delta
    for _ in range(MAX_DELTA_DEPTH - 4):
        nxt: JsonDict = {}
        cursor["nested"] = {"k": nxt}
        cursor = nxt
    document = {
        "session_id": "s",
        "from_seq": 1,
        "to_seq": 2,
        "timestamp_ms": 0,
        "delta": delta,
    }
    parsed = ObservationDiff.from_dict(document)
    assert parsed.from_seq == 1


# --------------------------------------------------------------------------
# Real escapes the fuzz found in packages/.../protocol/, which this task may not
# edit. Deliberately NOT collected (no `test_` prefix): each reproduces a bare
# exception escaping a boundary that promises a typed error, with the exact
# input. Rename to `test_` and invert the expectation once `_as_float` is
# hardened (reject a non-float-convertible integer, and reject non-finite
# numbers) in the protocol layer.
# --------------------------------------------------------------------------


def finds_a_huge_integer_in_a_float_field_overflows_observation_from_dict() -> None:
    """REAL escape, reported not fixed (owner: ``protocol/messages._as_float``).

    ``_as_float`` does ``float(value)`` with no ``OverflowError`` guard, so a
    JSON integer of ~309 or more digits in any float field — ``game.speed``,
    ``player.position.x``/``y``, ``wound.severity``, ``item.weight``,
    ``*.distance``, ``progress`` — raises a bare ``OverflowError`` where
    :meth:`Observation.from_dict` promises :class:`ProtocolError`. Reachable end
    to end: the journal loader's ``json.loads`` accepts a 400-digit integer (well
    under CPython's 4300-digit conversion ceiling and the record-depth bound), so
    a mod-written line delivers it straight to the parser. Fixed by mirroring the
    existing type guard: ``try: return float(value) except OverflowError: raise
    ProtocolError(...)``. Exact input below, no seed needed.
    """
    payload = make_observation().to_dict()
    payload["game"]["speed"] = 10**400
    with pytest.raises(OverflowError):
        Observation.from_dict(payload)


def finds_a_non_finite_coordinate_crashes_the_compact_reader() -> None:
    """REAL escape, reported not fixed (owner: ``protocol/messages._as_float``).

    ``json.loads`` accepts ``Infinity``/``NaN`` by default, ``_as_float`` accepts
    them (they are ``float`` instances), and :func:`compact._compact_player` then
    does ``int(player.position.x)`` — ``OverflowError`` on infinity,
    ``ValueError`` on NaN — where :func:`compact_for_planner` names no such
    failure, crashing the one view an LLM is ever given. The clean fix rejects
    non-finite numbers at the source (``math.isfinite`` in ``_as_float``); a
    compact-level fallback would have to invent a coordinate, so it is reported
    here rather than patched downstream.
    """
    payload = make_observation().to_dict()
    payload["player"]["position"]["x"] = float("inf")
    observation = Observation.from_dict(payload)  # from_dict tolerates it
    with pytest.raises(OverflowError):
        compact_for_planner(observation, {"cap": CapabilityState.VERIFIED})
