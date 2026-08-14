"""The protocol vocabularies, compared as the mod resolves them.

``tests/unit/test_lua_mod_contract.py`` already holds most of ``Protocol.lua``
against the Python enums, and it does it by matching ``KEY = "value"`` pairs in
the file's text. That is the right tool for the tables written as literals and
the wrong one for the tables that are not. Four are built from other tables —

    Protocol.ACTIONS           = toSet(Protocol.ACTION_NAMES)
    Protocol.TERMINAL_STATUSES = toSet({ Protocol.STATUS.SUCCEEDED, ... })
    Protocol.MUTATING_MODES    = toSet({ Protocol.MODE.ASSISTED, ... })
    Protocol.DANGER_RANK       = { [Protocol.DANGER.NONE] = 0, ... }

— and a pattern looking for quoted pairs finds nothing in any of them, so they
were left unchecked. Three carry decisions that have to hold on both sides:

* **``TERMINAL_STATUSES`` decides when the mod stops tracking an action.** NEVER
  TERMINAL is one of the three defect families this project names. A status the
  sidecar retires and the mod does not is an action the mod nurses forever,
  reported as running to a user watching a thing that finished.
* **``MUTATING_MODES`` decides which session modes accept a world-changing
  command at all.** A mode that mutates on one side and not the other is a
  safety question.
* **``DANGER_RANK`` is the order the reflex guard compares against**, so a rank
  that disagrees is a threshold firing at the wrong level.

They agree today. This file exists so that stays true by construction rather
than by nobody having edited one side, and it is deliberately built the way the
other three seam checks are built — by running the producer and reading what it
resolved. That is the lesson of the retraction: a ``kind = "square"`` producer
written through a constant was invisible to a regex, two documents were retracted
on the strength of what the regex could not see, and the fix was to stop asking
what the source looks like. ``dump_protocol_tables.lua`` loads the module the way
the game loads it and prints the tables it ends up holding.

The literal tables are re-checked here too, against the same dump. That is not
redundant with the unit test: it checks the *file*, this checks the *value*, and
the interesting failure — a table edited into a form the pattern cannot see —
passes the first and fails this one.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_core.protocol import MUTATING_MODES, TERMINAL_STATUSES
from pz_agent_core.protocol.enums import (
    ALWAYS_ALLOWED_ACTIONS,
    READ_ONLY_ACTIONS,
    ActionName,
    ActionOwnership,
    ActionStatus,
    CapabilityState,
    DangerLevel,
    SessionMode,
)
from pz_agent_core.protocol.reason_codes import ReasonCode
from pz_agent_core.session.heartbeat import Peer
from pz_agent_core.version import (
    MOD_VERSION,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    SUPPORTED_BUILDS,
    TARGET_BUILD,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DUMPER: Final = REPO_ROOT / "tests" / "lua" / "support" / "dump_protocol_tables.lua"

_INTERPRETERS: Final = ("lua5.4", "lua")


def _interpreter() -> str:
    for name in _INTERPRETERS:
        found = shutil.which(name)
        if found is not None:
            return found
    pytest.skip("no Lua interpreter is installed; the mod's own suite needs one too")


@pytest.fixture(scope="module")
def tables() -> dict[str, Any]:
    """``Protocol.lua`` as the mod holds it after loading."""
    assert DUMPER.is_file(), f"missing dumper: {DUMPER}"
    completed = subprocess.run(
        [_interpreter(), str(DUMPER)],
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


def test_the_dump_carries_every_table_this_file_checks(tables: dict[str, Any]) -> None:
    """A dumper that quietly stopped emitting one would make its test vacuous."""
    for name in (
        "versions",
        "supported_builds",
        "action_names",
        "actions",
        "read_only_actions",
        "always_allowed_actions",
        "terminal_statuses",
        "mutating_modes",
        "danger_rank",
        "status",
        "mode",
        "danger",
        "ownership",
        "capability",
        "peer",
        "reason",
    ):
        assert name in tables, f"the dump has no {name}"
        assert tables[name], f"{name} is empty in the dump"


# ---------------------------------------------------------------------------
# the four the pattern cannot see
# ---------------------------------------------------------------------------


def test_the_two_sides_retire_an_action_on_the_same_statuses(tables: dict[str, Any]) -> None:
    """NEVER TERMINAL, in the one place it would be silent.

    Compared against ``ActionStatus.is_terminal`` as well as the exported set,
    because those are two Python statements of the same rule and a status added
    to one and not the other is the same defect one layer up.
    """
    lua = set(tables["terminal_statuses"])

    assert lua == {status.value for status in TERMINAL_STATUSES}
    assert lua == {status.value for status in ActionStatus if status.is_terminal}


def test_the_two_sides_agree_on_which_modes_may_change_the_world(
    tables: dict[str, Any],
) -> None:
    assert set(tables["mutating_modes"]) == {mode.value for mode in MUTATING_MODES}


def test_the_danger_order_is_the_same_order(tables: dict[str, Any]) -> None:
    """Ranks, not just names: the guard compares them with ``<``."""
    assert tables["danger_rank"] == {level.value: level.rank for level in DangerLevel}


def test_the_action_set_is_the_action_list(tables: dict[str, Any]) -> None:
    """``ACTIONS`` is derived from ``ACTION_NAMES``; nothing checked the result.

    A duplicate in the list would collapse in the set and leave the two
    disagreeing in a way the list's own ordering test cannot see.
    """
    names = tables["action_names"]

    assert names == [action.value for action in ActionName]
    assert len(set(names)) == len(names), "ACTION_NAMES repeats an entry"
    assert set(tables["actions"]) == set(names)


# ---------------------------------------------------------------------------
# the ones the pattern can see, checked as values rather than as text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "enum"),
    [
        ("status", ActionStatus),
        ("mode", SessionMode),
        ("danger", DangerLevel),
        ("ownership", ActionOwnership),
        ("capability", CapabilityState),
        ("peer", Peer),
        ("reason", ReasonCode),
    ],
)
def test_each_enum_vocabulary_resolves_to_the_python_one(
    tables: dict[str, Any], key: str, enum: type[ActionStatus]
) -> None:
    resolved = tables[key]

    assert set(resolved) == {member.name for member in enum}
    assert set(resolved.values()) == {member.value for member in enum}
    assert resolved == {member.name: member.value for member in enum}


def test_the_action_classes_resolve_to_the_python_ones(tables: dict[str, Any]) -> None:
    assert set(tables["read_only_actions"]) == {action.value for action in READ_ONLY_ACTIONS}
    assert set(tables["always_allowed_actions"]) == {
        action.value for action in ALWAYS_ALLOWED_ACTIONS
    }


def test_the_versions_the_mod_holds_are_the_versions_python_holds(
    tables: dict[str, Any],
) -> None:
    assert tables["versions"] == {
        "protocol": PROTOCOL_VERSION,
        "schema": SCHEMA_VERSION,
        "mod": MOD_VERSION,
        "target_build": TARGET_BUILD,
    }
    assert tables["supported_builds"] == list(SUPPORTED_BUILDS)
