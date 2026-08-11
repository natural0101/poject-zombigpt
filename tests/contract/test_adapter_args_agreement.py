"""What the sidecar sends must be what the mod agreed to receive.

The two halves of an action are written on opposite sides of a file-based wire.
The sidecar's ``build_args`` produces the argument table; the mod's
``PZAgent.CommandDispatcher`` rebuilds one *from the adapter's own declaration*
and hands that to the adapter. Nothing compared the two, and neither side's
tests could: a Python adapter test asserts the payload it meant to build, and a
Lua adapter test asserts what the adapter does with a payload the test wrote by
hand. Both pass while the two disagree.

The disagreement is not benign. An undeclared key is a refusal, not an ignored
field — deliberately, because a silently dropped ``radius`` runs the action with
a default nobody asked for. So a key the sidecar emits and the adapter never
declared costs *every command of that action*, in a build where both suites are
green.

That is exactly what happened: ``inventory.transfer`` and
``inventory.ensure_main`` emitted an ``origin`` object that no adapter declared
and no scalar declaration could ever have accepted. Every transfer the agent
tried would have come back ``INVALID_ARGUMENT``.

It then happened again, one blind spot over. This suite used to build its
registry from the game adapters alone while the shipped app builds
``register_game_adapters(register_builtins(...))``, and the Lua dumper walked
only the published ``PZAgent.Adapters.ALL`` — never ActionRuntime's control
adapters. ``action.wait`` and ``plan.cancel`` live exactly in that gap, and
both disagreed: the sidecar sent ``game_seconds`` where the mod demanded
``duration_ms`` (in a different unit, against a different clock), and the
targeted cancel's ``command_id`` was a key the mod's adapter never declared.
The registry here is now built the way ``pz_agent_cli.app`` builds it, the
dumper reports both adapter families, and the coverage test closes with a
two-way census so no family can slip out again.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_core.actions.adapter import AdapterRegistry
from pz_agent_core.actions.adapters import register_game_adapters
from pz_agent_core.actions.builtin import register_builtins
from pz_agent_core.protocol import (
    ActionName,
    Command,
    NearbyView,
    NearbyZombie,
    Observation,
    Position,
)
from pz_agent_core.protocol.refs import RefKind, ref_kind
from tests.fixtures import DEFAULT_SESSION
from tests.fixtures.adapter_worlds import (
    BAG_REF,
    CRATE_REF,
    MAIN_REF,
    a_command,
    a_world,
    a_world_object,
    an_item,
    bag_container,
    crate_container,
    main_container,
    square_ref,
)

#: A door reference in the mod's ``object:`` shape — square coordinates plus
#: the index of the door in that square's object list.
DOOR_REF: Final = f"object:{DEFAULT_SESSION}:1203:3400:0:2"

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DUMPER: Final = REPO_ROOT / "tests" / "lua" / "support" / "dump_adapter_args.lua"

_INTERPRETERS: Final = ("lua5.4", "lua")

#: The mod's default string-argument bound and token alphabet, mirrored from
#: ``CommandDispatcher.checkString``.
_MOD_MAX_STRING_BYTES: Final = 64
_TOKEN_PATTERN: Final = re.compile(r"[A-Za-z0-9_.\-]+")

#: A Lua declaration type, and what a JSON value must be to satisfy it. The one
#: structured value the mod accepts is a declared ``list`` — a dense, bounded
#: array of refs or plain strings, checked element-wise below; everything else
#: table-valued is refused outright, so every other accepted value is scalar.
_PYTHON_TYPE_OF: Final[dict[str, tuple[type, ...]]] = {
    "number": (int, float),
    "string": (str,),
    "boolean": (bool,),
    "enum": (str,),
    "ref": (str,),
}

#: Element kinds a declared list may carry, and the ceiling no ``max_items``
#: may raise — both mirrored from ``CommandDispatcher.checkArgSpec``.
_LIST_ELEMENT_KINDS: Final = frozenset({"ref", "string"})
_MOD_MAX_LIST_ITEMS: Final = 8


def _interpreter() -> str:
    for name in _INTERPRETERS:
        found = shutil.which(name)
        if found is not None:
            return found
    pytest.skip("no Lua interpreter is installed; the mod's own suite needs one too")


@pytest.fixture(scope="module")
def declarations() -> dict[str, dict[str, Any]]:
    """Every adapter's argument declaration, straight from the shipped mod."""
    assert DUMPER.is_file(), f"missing declaration dumper: {DUMPER}"
    completed = subprocess.run(
        [_interpreter(), str(DUMPER.relative_to(REPO_ROOT))],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert isinstance(document, dict)
    return document


#: The actions the mod serves that have no adapter in the sidecar's registry:
#: the control plane the CLI drives through its own session machinery rather
#: than through ``build_args``. Named exactly, so a Lua adapter for anything
#: else that appears without a Python half fails the census below.
_CONTROL_PLANE_ONLY: Final = frozenset({"session.arm", "session.disarm", "safety.stop"})


def _registry() -> AdapterRegistry:
    """The registry built exactly as ``pz_agent_cli.app`` builds it.

    Builtins first, then the game adapters, imported from the same modules. A
    test registry assembled any other way checks a sidecar that does not ship:
    the builtin ``action.wait`` and ``plan.cancel`` were invisible here for
    precisely that reason, while both disagreed with the mod.
    """
    return register_game_adapters(register_builtins(AdapterRegistry()))


#: One realistic command per action the sidecar can build arguments for, with
#: the world that makes its happy path hold. Hand-built rather than generated:
#: the point is to exercise the *real* ``build_args``, and a generated payload
#: would only prove the generator agrees with itself.
def _cases() -> list[tuple[ActionName, Command, Observation]]:
    apple = an_item(runtime_id="1001", container_ref=MAIN_REF, full_type="Base.Apple")
    water = an_item(runtime_id="1002", container_ref=MAIN_REF, full_type="Base.WaterBottle")
    book = an_item(runtime_id="1003", container_ref=MAIN_REF, full_type="Base.Book")
    bandage = an_item(runtime_id="1004", container_ref=MAIN_REF, full_type="Base.Bandage")
    shirt = an_item(runtime_id="1005", container_ref=MAIN_REF, full_type="Base.Tshirt")
    stashed = an_item(runtime_id="1006", container_ref=BAG_REF, full_type="Base.Biscuit")

    # A container reference, because that is what the mod mints for a nearby
    # thing that holds one — it never mints an `object:` reference at all.
    crate_object = a_world_object(CRATE_REF)
    # A sink is not a container, so the mod mints a square reference for it and
    # marks it with the semantic ``consume.drink_source`` gates on.
    sink = a_world_object(square_ref(1204, 3400), kind="sink", semantics=["water_source"])
    # A door is the one thing the mod addresses with an ``object:`` reference.
    # Closed and unlocked, so every door adapter's arguments build cleanly.
    door = a_world_object(
        DOOR_REF,
        x=1203,
        y=3400,
        kind="door",
        semantics=["door", "obstacle"],
        open=False,
        locked=False,
        barricaded=False,
        orientation="north",
    )
    world = a_world(
        items=[apple, water, book, bandage, shirt, stashed],
        containers=[main_container(), bag_container(), crate_container()],
        objects=[crate_object, sink, door],
    )
    # A zombie at contact range, for the two combat actions that name one and
    # the retreat that needs one observed. ``build_args`` for combat does not
    # read the world — the policy gates live in ``validate`` — but the case
    # table's contract is a world the happy path holds in, so it is here.
    zombie = NearbyZombie(
        ref=f"zombie:{DEFAULT_SESSION}:9001:0",
        distance=1.5,
        visible=True,
        chasing=False,
        position=Position(x=1201.5, y=3400.0, z=0),
        state="standing",
    )
    combat_world = a_world(
        items=[apple],
        containers=[main_container()],
        nearby=NearbyView(zombies=[zombie]),
    )

    return [
        (
            ActionName.INVENTORY_TRANSFER,
            a_command(
                ActionName.INVENTORY_TRANSFER,
                {"item_ref": stashed.ref, "destination_container_ref": MAIN_REF},
            ),
            world,
        ),
        # Three items from two source containers into the crate underfoot: the
        # one list-typed argument on the wire, exercised with more than one
        # element so an element-shape disagreement cannot hide behind a
        # singleton.
        (
            ActionName.INVENTORY_TRANSFER_BATCH,
            a_command(
                ActionName.INVENTORY_TRANSFER_BATCH,
                {
                    "item_refs": [apple.ref, bandage.ref, stashed.ref],
                    "destination_container_ref": CRATE_REF,
                },
            ),
            world,
        ),
        (
            ActionName.INVENTORY_ENSURE_MAIN,
            a_command(ActionName.INVENTORY_ENSURE_MAIN, {"item_ref": stashed.ref}),
            world,
        ),
        (
            ActionName.CONSUME_EAT,
            a_command(ActionName.CONSUME_EAT, {"item_ref": apple.ref, "fraction": 1.0}),
            world,
        ),
        (
            ActionName.CONSUME_DRINK,
            a_command(ActionName.CONSUME_DRINK, {"item_ref": water.ref, "fraction": 1.0}),
            world,
        ),
        (
            ActionName.CONSUME_DRINK_SOURCE,
            a_command(
                ActionName.CONSUME_DRINK_SOURCE,
                {"item_ref": water.ref, "fraction": 1.0, "source_ref": sink.ref},
            ),
            world,
        ),
        (
            ActionName.LITERATURE_READ,
            a_command(ActionName.LITERATURE_READ, {"item_ref": book.ref, "pages": 10}),
            world,
        ),
        (
            ActionName.MOVEMENT_MOVE_TO,
            a_command(
                ActionName.MOVEMENT_MOVE_TO,
                {"target": {"x": 1201, "y": 3401, "z": 0}, "radius": 1.0},
            ),
            world,
        ),
        (
            ActionName.MOVEMENT_MOVE_NEAR,
            a_command(ActionName.MOVEMENT_MOVE_NEAR, {"object_ref": crate_object.ref}),
            world,
        ),
        (
            ActionName.WORLD_INSPECT,
            a_command(ActionName.WORLD_INSPECT, {"ref": square_ref(1201, 3401), "radius": 1}),
            world,
        ),
        (
            ActionName.CONTAINER_INSPECT,
            a_command(ActionName.CONTAINER_INSPECT, {"container_ref": CRATE_REF}),
            world,
        ),
        (
            ActionName.CONTAINER_OPEN_NEARBY,
            a_command(ActionName.CONTAINER_OPEN_NEARBY, {"container_ref": CRATE_REF}),
            world,
        ),
        (
            ActionName.INVENTORY_SEARCH,
            a_command(ActionName.INVENTORY_SEARCH, {"full_type": "Base.Apple"}),
            world,
        ),
        (
            ActionName.EQUIPMENT_EQUIP,
            a_command(ActionName.EQUIPMENT_EQUIP, {"item_ref": shirt.ref, "hand": "primary"}),
            world,
        ),
        (
            ActionName.EQUIPMENT_UNEQUIP,
            a_command(ActionName.EQUIPMENT_UNEQUIP, {"item_ref": shirt.ref}),
            world,
        ),
        (
            ActionName.MEDICAL_BANDAGE,
            a_command(
                ActionName.MEDICAL_BANDAGE,
                {"body_part": "Hand_L", "item_ref": bandage.ref},
            ),
            world,
        ),
        # The defaults are the payload for the two survival actions: an empty
        # args table still ships target, posture and wait bounds, and each of
        # those defaults must sit inside the mod's declared range.
        (
            ActionName.SURVIVAL_REST,
            a_command(ActionName.SURVIVAL_REST, {}),
            world,
        ),
        (
            ActionName.SURVIVAL_SLEEP,
            a_command(ActionName.SURVIVAL_SLEEP, {}),
            world,
        ),
        # The two builtins the shipped registry carries. ``action.wait`` is the
        # agreed wire shape itself — game seconds against the world clock — and
        # the targeted cancel exercises the one argument ``plan.cancel`` may
        # send; the untargeted form sends no arguments at all.
        (
            ActionName.ACTION_WAIT,
            a_command(ActionName.ACTION_WAIT, {"game_seconds": 30}),
            world,
        ),
        (
            ActionName.PLAN_CANCEL,
            a_command(
                ActionName.PLAN_CANCEL,
                {"command_id": "0f0e0d0c-0b0a-4009-8807-060504030201"},
            ),
            world,
        ),
        # The three door actions share one argument shape: the door's object
        # reference plus an optional approach radius. One case each, so a key
        # either side renames is caught for all three.
        (
            ActionName.DOOR_OPEN,
            a_command(ActionName.DOOR_OPEN, {"door_ref": door.ref, "radius": 1.0}),
            world,
        ),
        (
            ActionName.DOOR_CLOSE,
            a_command(ActionName.DOOR_CLOSE, {"door_ref": door.ref}),
            world,
        ),
        (
            ActionName.DOOR_UNLOCK,
            a_command(ActionName.DOOR_UNLOCK, {"door_ref": door.ref}),
            world,
        ),
        # The four combat actions. equip_best and retreat ship no arguments
        # at all — the mod's declarations own their defaults — and the two
        # targeted actions ship exactly the one zombie reference; a key
        # either side renames, or a kind the Lua declaration does not
        # accept, is caught here for all four.
        (
            ActionName.COMBAT_EQUIP_BEST,
            a_command(ActionName.COMBAT_EQUIP_BEST, {}),
            combat_world,
        ),
        (
            ActionName.COMBAT_SHOVE,
            a_command(ActionName.COMBAT_SHOVE, {"target_ref": zombie.ref}),
            combat_world,
        ),
        (
            ActionName.COMBAT_ENGAGE,
            a_command(ActionName.COMBAT_ENGAGE, {"target_ref": zombie.ref}),
            combat_world,
        ),
        (
            ActionName.COMBAT_RETREAT,
            a_command(ActionName.COMBAT_RETREAT, {}),
            combat_world,
        ),
        # The two crafting actions. Both ship a recipe name — a bounded game
        # identifier rather than a ref, which is a shape this table had not
        # carried before — and the craft ships the count as well. The name goes
        # to the mod exactly as the observation reported it, so a key either
        # side renames is caught here, and so is a mod declaration that stopped
        # accepting a string where the sidecar still sends one.
        (
            ActionName.CRAFTING_INSPECT,
            a_command(ActionName.CRAFTING_INSPECT, {"recipe": "MakeCrate"}),
            world,
        ),
        (
            ActionName.CRAFTING_CRAFT,
            a_command(ActionName.CRAFTING_CRAFT, {"recipe": "MakeCrate", "count": 1}),
            world,
        ),
        # The two building actions. Both carry a square reference, which is the
        # argument shape this table has checked since movement — but here it
        # names where a permanent object would go rather than where a character
        # would walk, so a rename on either side is worth catching twice over.
        (
            ActionName.BUILDING_INSPECT,
            a_command(ActionName.BUILDING_INSPECT, {"square": square_ref(1201, 3400, 0)}),
            world,
        ),
        (
            ActionName.BUILDING_BUILD,
            a_command(
                ActionName.BUILDING_BUILD,
                {"blueprint": "WoodenWall", "square": square_ref(1201, 3400, 0)},
            ),
            world,
        ),
    ]


def _check_list(
    action: ActionName,
    key: str,
    value: Any,
    spec: dict[str, Any],
) -> list[str]:
    """Every way one list-valued argument would be refused, as plain sentences.

    Mirrors ``CommandDispatcher.checkList``: a dense array of 1..``max_items``
    elements, no duplicates, each element passing the same check its kind gets
    when it stands alone — the ref-kind gate for ``of == "ref"``, the token
    alphabet and byte bound for ``of == "string"``.
    """
    problems: list[str] = []
    if not isinstance(value, list):
        return [
            f"{action.value} sends {key}={value!r} ({type(value).__name__}), "
            f"but the adapter declares it as a list"
        ]
    max_items = int(spec["max_items"])
    if not value:
        problems.append(f"{action.value} sends an empty {key}, which the mod's list check refuses")
    if len(value) > max_items:
        problems.append(
            f"{action.value} sends {len(value)} elements in {key}, "
            f"above the declared max_items {max_items}"
        )
    element_kind = str(spec["of"])
    assert element_kind in _LIST_ELEMENT_KINDS, (
        f"{action.value}.{key} declares list elements of {element_kind!r}, "
        f"which the dispatcher does not accept"
    )
    seen: dict[Any, int] = {}
    for index, element in enumerate(value):
        if not isinstance(element, str) or not element:
            problems.append(
                f"{action.value} sends {key}[{index}]={element!r}, "
                f"which is not the non-empty string a {element_kind} element must be"
            )
            continue
        if element_kind == "ref":
            kind = ref_kind(element)
            allowed = {str(k) for k in spec.get("kinds", [])}
            if kind is None or kind.value not in allowed:
                problems.append(
                    f"{action.value} sends {key}[{index}] as a "
                    f"{kind.value if kind else 'unparseable'} reference, "
                    f"but the adapter accepts {sorted(allowed)}"
                )
        else:
            limit = int(spec.get("max_bytes", _MOD_MAX_STRING_BYTES))
            if len(element.encode()) > limit:
                problems.append(
                    f"{action.value} sends {key}[{index}]={element!r}, "
                    f"outside the 1..{limit} byte bound"
                )
            elif _TOKEN_PATTERN.fullmatch(element) is None:
                problems.append(
                    f"{action.value} sends {key}[{index}]={element!r}, which is not a "
                    f"plain token the mod's string check accepts"
                )
        earlier = seen.get(element)
        if earlier is not None:
            problems.append(
                f"{action.value} sends {key}[{index}] repeating {key}[{earlier}], "
                f"which the mod's list check refuses as one object asked to move twice"
            )
        seen.setdefault(element, index)
    return problems


def _check_payload(
    action: ActionName,
    payload: dict[str, Any],
    declared: dict[str, Any],
) -> list[str]:
    """Every way *payload* would be refused by the mod, as plain sentences."""
    problems: list[str] = []
    for key, value in payload.items():
        spec = declared.get(key)
        if spec is None:
            problems.append(
                f"{action.value} sends {key!r}, which the adapter does not declare; "
                f"the dispatcher refuses the whole command"
            )
            continue
        if spec["type"] == "list":
            problems.extend(_check_list(action, key, value, spec))
            continue
        expected = _PYTHON_TYPE_OF.get(str(spec["type"]))
        assert expected is not None, f"unknown declared type {spec['type']!r} for {key}"
        # bool is a subclass of int, so a boolean would satisfy "number" without
        # this. The mod's checkNumber would not.
        wrong_bool = isinstance(value, bool) and spec["type"] != "boolean"
        if wrong_bool or not isinstance(value, expected):
            problems.append(
                f"{action.value} sends {key}={value!r} ({type(value).__name__}), "
                f"but the adapter declares it as {spec['type']}"
            )
            continue
        if spec["type"] == "string":
            # The mod's checkString: a plain token, bounded in bytes. A value
            # that fits the declared type but not the token alphabet is refused
            # just as hard as an undeclared key.
            assert isinstance(value, str)
            limit = int(spec.get("max_bytes", _MOD_MAX_STRING_BYTES))
            if not value or len(value.encode()) > limit:
                problems.append(
                    f"{action.value} sends {key}={value!r}, outside the 1..{limit} byte bound"
                )
            elif _TOKEN_PATTERN.fullmatch(value) is None:
                problems.append(
                    f"{action.value} sends {key}={value!r}, which is not a plain token "
                    f"the mod's string check accepts"
                )
        if spec["type"] == "enum" and value not in spec.get("values", []):
            problems.append(
                f"{action.value} sends {key}={value!r}, outside the declared values "
                f"{sorted(spec.get('values', []))}"
            )
        if spec["type"] == "ref":
            kind = ref_kind(str(value))
            allowed = {str(k) for k in spec.get("kinds", [])}
            if kind is None or kind.value not in allowed:
                problems.append(
                    f"{action.value} sends {key} as a "
                    f"{kind.value if kind else 'unparseable'} reference, "
                    f"but the adapter accepts {sorted(allowed)}"
                )
        if spec["type"] == "number":
            assert isinstance(value, int | float)
            if spec.get("integer") and float(value) != int(value):
                problems.append(f"{action.value} sends a fractional {key}={value!r}")
            if "min" in spec and value < spec["min"]:
                problems.append(f"{action.value} sends {key}={value!r} below {spec['min']}")
            if "max" in spec and value > spec["max"]:
                problems.append(f"{action.value} sends {key}={value!r} above {spec['max']}")
    for key, spec in declared.items():
        if spec.get("required") and key not in payload:
            problems.append(f"{action.value} omits {key!r}, which the adapter requires")
    return problems


def test_every_adapter_declares_the_arguments_its_python_half_sends(
    declarations: dict[str, dict[str, Any]],
) -> None:
    registry = _registry()
    problems: list[str] = []
    checked: set[ActionName] = set()

    for action, command, observation in _cases():
        adapter = registry.get(action)
        declared = declarations.get(action.value)
        assert declared is not None, f"no Lua adapter is published for {action.value}"
        payload = adapter.build_args(command, observation)
        problems.extend(_check_payload(action, payload, declared))
        checked.add(action)

    assert not problems, "\n".join(problems)
    assert checked, "the case table is empty, so this test proved nothing"


def test_the_case_table_covers_every_action_that_builds_arguments(
    declarations: dict[str, dict[str, Any]],
) -> None:
    """A case table that quietly stops covering an action proves less each release.

    There is no exemption left: every action the shipped registry publishes —
    game adapter or builtin — has a case, so the payload check above sees each
    one's real ``build_args``. The set used to carry ten names, excused as
    read-only or as lacking a world, and ``plan.cancel`` sat among them while
    its targeted form was refused by the mod; a happy-path world turned out to
    be enough for every one of them, so none gets to stand aside again.

    The census is two-way, so a whole adapter *family* cannot slip out the way
    the builtins and the control adapters once did: every Python-published
    action must be declared by the dumped Lua registry, and a Lua action with
    no Python adapter must be one of the named control-plane commands the CLI
    drives without ``build_args``.
    """
    registry = _registry()
    covered = {action for action, _, _ in _cases()}
    published = set(registry.names())

    missing = published - covered
    assert not missing, (
        f"these actions build arguments nothing checks: {sorted(a.value for a in missing)}"
    )

    undeclared = {action.value for action in published} - set(declarations)
    assert not undeclared, (
        f"the sidecar publishes adapters the mod declares nothing for — either the "
        f"dumper lost an adapter family or the mod lost an action: {sorted(undeclared)}"
    )

    lua_only = set(declarations) - {action.value for action in published}
    assert lua_only == set(_CONTROL_PLANE_ONLY), (
        f"the mod declares actions this registry does not publish, beyond the named "
        f"control plane: {sorted(lua_only - _CONTROL_PLANE_ONLY)}"
    )


def test_no_declaration_accepts_an_unchecked_structured_value(
    declarations: dict[str, dict[str, Any]],
) -> None:
    """The wire carries scalars and bounded lists of them, and nothing else.

    ``CommandDispatcher.checkArgs`` refuses any other table-valued argument
    outright, so an adapter that declared one would be declaring something no
    command could satisfy. The one structured shape it earned is the declared
    ``list``: elements limited to refs and plain strings, ``max_items`` capped
    by the dispatcher's own ceiling, and a ref-list naming its accepted kinds.
    This catches the mistake at the declaration rather than at the first
    refused command.
    """
    for action, declared in declarations.items():
        for key, spec in declared.items():
            if spec["type"] == "list":
                assert spec.get("of") in _LIST_ELEMENT_KINDS, (
                    f"{action} declares {key} as a list of {spec.get('of')!r}, "
                    f"which the dispatcher refuses to register"
                )
                max_items = spec.get("max_items")
                assert isinstance(max_items, int) and 1 <= max_items <= _MOD_MAX_LIST_ITEMS, (
                    f"{action}.{key} declares max_items {max_items!r} "
                    f"outside 1..{_MOD_MAX_LIST_ITEMS}"
                )
                if spec.get("of") == "ref":
                    assert spec.get("kinds"), f"{action}.{key} is a ref list with no accepted kinds"
                continue
            assert spec["type"] in _PYTHON_TYPE_OF, (
                f"{action} declares {key} as {spec['type']!r}, which is not a scalar kind"
            )


def test_every_ref_declaration_names_kinds_that_exist(
    declarations: dict[str, dict[str, Any]],
) -> None:
    known = {kind.value for kind in RefKind}
    for action, declared in declarations.items():
        for key, spec in declared.items():
            # A ref list carries the same `kinds` table a lone ref does, and a
            # kind that does not exist is just as unsatisfiable there.
            if spec["type"] != "ref" and not (spec["type"] == "list" and spec.get("of") == "ref"):
                continue
            kinds = set(spec.get("kinds", []))
            assert kinds, f"{action} declares {key} as a ref with no accepted kinds"
            unknown = kinds - known
            assert not unknown, (
                f"{action}.{key} accepts reference kinds that do not exist: {unknown}"
            )
