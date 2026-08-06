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
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_core.actions.adapter import AdapterRegistry
from pz_agent_core.actions.adapters import register_game_adapters
from pz_agent_core.protocol import ActionName, Command, Observation
from pz_agent_core.protocol.refs import RefKind, ref_kind
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
)

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DUMPER: Final = REPO_ROOT / "tests" / "lua" / "support" / "dump_adapter_args.lua"

_INTERPRETERS: Final = ("lua5.4", "lua")

#: A Lua declaration type, and what a JSON value must be to satisfy it. The mod
#: refuses a table-valued argument outright, so every accepted value is scalar.
_PYTHON_TYPE_OF: Final[dict[str, tuple[type, ...]]] = {
    "number": (int, float),
    "string": (str,),
    "boolean": (bool,),
    "enum": (str,),
    "ref": (str,),
}


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


def _registry() -> AdapterRegistry:
    return register_game_adapters(AdapterRegistry())


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
    world = a_world(
        items=[apple, water, book, bandage, shirt, stashed],
        containers=[main_container(), bag_container(), crate_container()],
        objects=[crate_object],
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
    ]


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


def test_the_case_table_covers_every_action_that_builds_arguments() -> None:
    """A case table that quietly stops covering an action proves less each release.

    The exemptions are named rather than inferred: an action whose adapter takes
    no arguments has nothing to disagree about, and a read-only action's payload
    is checked by its own contract test.
    """
    registry = _registry()
    covered = {action for action, _, _ in _cases()}
    published = set(registry.names())

    #: Read-only and no-argument actions, and the ones whose Python adapter
    #: builds its payload from a reference the case table has no world for.
    exempt = {
        ActionName.WORLD_INSPECT,
        ActionName.CONTAINER_INSPECT,
        ActionName.CONTAINER_OPEN_NEARBY,
        ActionName.INVENTORY_SEARCH,
        ActionName.EQUIPMENT_EQUIP,
        ActionName.EQUIPMENT_UNEQUIP,
        ActionName.MEDICAL_BANDAGE,
        ActionName.SURVIVAL_REST,
        ActionName.SURVIVAL_SLEEP,
        ActionName.PLAN_CANCEL,
    }

    missing = published - covered - exempt
    assert not missing, (
        f"these actions build arguments nothing checks: {sorted(a.value for a in missing)}"
    )


def test_no_declaration_accepts_a_structured_value(
    declarations: dict[str, dict[str, Any]],
) -> None:
    """The wire carries scalars, and that is a property worth pinning.

    ``CommandDispatcher.checkArgs`` refuses a table-valued argument outright, so
    an adapter that declared one would be declaring something no command could
    satisfy. This catches the mistake at the declaration rather than at the
    first refused command.
    """
    for action, declared in declarations.items():
        for key, spec in declared.items():
            assert spec["type"] in _PYTHON_TYPE_OF, (
                f"{action} declares {key} as {spec['type']!r}, which is not a scalar kind"
            )


def test_every_ref_declaration_names_kinds_that_exist(
    declarations: dict[str, dict[str, Any]],
) -> None:
    known = {kind.value for kind in RefKind}
    for action, declared in declarations.items():
        for key, spec in declared.items():
            if spec["type"] != "ref":
                continue
            kinds = set(spec.get("kinds", []))
            assert kinds, f"{action} declares {key} as a ref with no accepted kinds"
            unknown = kinds - known
            assert not unknown, (
                f"{action}.{key} accepts reference kinds that do not exist: {unknown}"
            )
