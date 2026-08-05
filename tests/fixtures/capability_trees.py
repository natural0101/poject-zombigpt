"""Fake Project Zomboid Lua trees for the capability scanner tests.

The scanner's contract is about what it does *not* do — copy files, write into
the game directory, embed source text — and none of that can be tested against a
real installation. These builders produce a tree with the declaration forms the
extractor must recognise, plus the shapes it must ignore (``local`` functions,
block comments), so a test can assert on behaviour instead of on scaffolding.

``VANILLA_SOURCES`` deliberately contains no line of real game code: every file
here is written for this test suite.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from pathlib import Path

from pz_agent_core.protocol import ActionName, ActionResult, ActionStatus, ReasonCode

#: One file per declaration form the extractor claims to handle.
VANILLA_SOURCES: Mapping[str, str] = {
    "shared/TimedActions/ISBaseTimedAction.lua": (
        "ISBaseTimedAction = ISBaseTimedAction or {}\n"
        "ISTimedActionQueue = ISTimedActionQueue or {}\n"
        "\n"
        "function ISTimedActionQueue.add(action)\n"
        "    local queue = ISTimedActionQueue.getTimedActionQueue(action.character)\n"
        "    queue:addToQueue(action)\n"
        "    return action\n"
        "end\n"
        "\n"
        "function ISBaseTimedAction:isValid()\n"
        "    return self.character ~= nil\n"
        "end\n"
    ),
    "client/TimedActions/ISEatFoodAction.lua": (
        "--[[\n"
        "    Block comments must not produce symbols:\n"
        "    function ISGhostAction:new(character)\n"
        "]]\n"
        'ISEatFoodAction = ISBaseTimedAction:derive("ISEatFoodAction")\n'
        "\n"
        "function ISEatFoodAction:new(character, item, percentage)\n"
        "    local o = ISBaseTimedAction.new(self, character)\n"
        "    o.item = item\n"
        "    o.percentage = percentage\n"
        "    return o\n"
        "end\n"
        "\n"
        "function ISEatFoodAction.isEdible(item)\n"
        "    return item:getHungerChange() < 0\n"
        "end\n"
        "\n"
        "-- a line comment that mentions function ISCommentedAction:new(x)\n"
        "local function privateHelper(character)\n"
        "    return character:getStats()\n"
        "end\n"
    ),
    "client/TimedActions/ISWalkToTimedAction.lua": (
        'ISWalkToTimedAction = ISBaseTimedAction:derive("ISWalkToTimedAction")\n'
        "\n"
        "function ISWalkToTimedAction:new(character, location)\n"
        "    local o = ISBaseTimedAction.new(self, character)\n"
        "    o.location = location\n"
        "    return o\n"
        "end\n"
        "\n"
        "luautils = luautils or {}\n"
        "luautils.walkAdj = function(character, square, ignoreWalls)\n"
        "    return character:getPathFindBehavior()\n"
        "end\n"
        "\n"
        "function globalWalkHelper(character,square)\n"
        "    return true\n"
        "end\n"
    ),
    "client/TimedActions/ISInventoryTransferAction.lua": (
        'ISInventoryTransferAction = ISBaseTimedAction:derive("ISInventoryTransferAction")\n'
        "\n"
        "function ISInventoryTransferAction:new(character, item, srcContainer, destContainer)\n"
        "    local o = ISBaseTimedAction.new(self, character)\n"
        "    o.item = item\n"
        "    return o\n"
        "end\n"
    ),
    "client/TimedActions/ISDrinkFromBottle.lua": (
        'ISDrinkFromBottle = ISBaseTimedAction:derive("ISDrinkFromBottle")\n'
        "\n"
        "function ISDrinkFromBottle:new(character, item, percentage)\n"
        "    local o = ISBaseTimedAction.new(self, character)\n"
        "    return o\n"
        "end\n"
    ),
    "client/TimedActions/ISReadABook.lua": (
        'ISReadABook = ISBaseTimedAction:derive("ISReadABook")\n'
        "\n"
        "function ISReadABook:new(character, item, time)\n"
        "    local o = ISBaseTimedAction.new(self, character)\n"
        "    return o\n"
        "end\n"
    ),
    "server/NotLua.txt": "this file is not Lua and must be ignored\n",
}

#: The world water source action, kept separate so a test can build an install
#: that lacks it — which is what the blueprint expects of an unproven API.
WORLD_WATER_SOURCE = {
    "client/TimedActions/ISTakeWaterAction.lua": (
        'ISTakeWaterAction = ISBaseTimedAction:derive("ISTakeWaterAction")\n'
        "\n"
        "function ISTakeWaterAction:new(character, waterObject, item)\n"
        "    local o = ISBaseTimedAction.new(self, character)\n"
        "    return o\n"
        "end\n"
    ),
}


def write_lua_tree(base: Path, sources: Mapping[str, str] = VANILLA_SOURCES) -> Path:
    """Create a ``media/lua`` tree under *base* and return the Lua root."""
    root = base / "media" / "lua"
    for relative, text in sources.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def full_lua_tree(base: Path) -> Path:
    """A tree carrying every symbol the declared probes require."""
    return write_lua_tree(base, {**VANILLA_SOURCES, **WORLD_WATER_SOURCE})


def source_lines(
    sources: Mapping[str, str] = VANILLA_SOURCES, *, min_length: int = 10
) -> list[str]:
    """Every distinctive line of the fake sources.

    Short lines (``end``, ``o.item = item``) are filtered out: they are common
    substrings that would make an "is any source text in the report" assertion
    fire on a coincidence rather than on a leak.
    """
    lines: list[str] = []
    for text in sources.values():
        for raw in text.splitlines():
            stripped = raw.strip()
            if len(stripped) >= min_length:
                lines.append(stripped)
    return lines


def tree_fingerprint(root: Path) -> dict[str, tuple[int, bytes]]:
    """Size and contents of every file under *root*, for before/after comparison."""
    fingerprint: dict[str, tuple[int, bytes]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            raw = path.read_bytes()
            fingerprint[str(path.relative_to(root))] = (len(raw), raw)
    return fingerprint


def probe_ack(
    action: ActionName,
    evidence: dict[str, object],
    *,
    status: ActionStatus = ActionStatus.SUCCEEDED,
) -> ActionResult:
    """An ack shaped like the one a probe command would come back with."""
    session_id = str(uuid.UUID(int=0x5E5510))
    command_id = str(uuid.UUID(int=0xC0FFEE))
    if status is ActionStatus.SUCCEEDED:
        return ActionResult.succeeded(
            session_id=session_id,
            seq=7,
            command_id=command_id,
            action=action.value,
            timestamp_ms=1_700_000_000_000,
            evidence=dict(evidence),
        )
    return ActionResult.failure(
        session_id=session_id,
        seq=7,
        command_id=command_id,
        action=action.value,
        timestamp_ms=1_700_000_000_000,
        reason_code=ReasonCode.POSTCONDITION_FAILED,
        status=status,
        evidence=dict(evidence),
    )
