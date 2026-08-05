"""The Lua mod restates the wire vocabulary; this test is what keeps it honest.

``pz-mod/42/media/lua/shared/PZAgent/Protocol.lua`` duplicates the action
whitelist, the reason codes and the version constants, because the mod has to
reject an unknown command *before* dispatch and cannot ask the sidecar that sent
it whether the name is real. Duplication is the price of that check. The point
of this module is that the duplicate cannot drift: the Lua source is parsed as
text and compared against the Python definitions, so adding an ``ActionName``
without teaching the mod about it fails here rather than in a live session.

Parsing is deliberately textual and shallow. Running a Lua interpreter would
make the check depend on one being installed, and the constants are literal
tables whose shape a small regular expression reads reliably.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from pz_agent_core.ipc import layout
from pz_agent_core.protocol.enums import (
    ActionName,
    ActionOwnership,
    ActionStatus,
    CapabilityState,
    DangerLevel,
    SessionMode,
)
from pz_agent_core.protocol.reason_codes import ReasonCode
from pz_agent_core.version import (
    MOD_VERSION,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    SUPPORTED_BUILDS,
    TARGET_BUILD,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
MOD_LUA_ROOT: Final = REPO_ROOT / "pz-mod" / "42" / "media" / "lua"
PROTOCOL_LUA: Final = MOD_LUA_ROOT / "shared" / "PZAgent" / "Protocol.lua"
IPC_LUA: Final = MOD_LUA_ROOT / "client" / "PZAgent" / "Ipc.lua"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing mod source: {path}"
    return path.read_text(encoding="utf-8")


def _table_body(source: str, name: str) -> str:
    """Return the text between ``name = {`` and its closing brace.

    The tables this reads are flat literals, so counting braces is enough; a
    nested table would need a real parser and is asserted against below.
    """
    start = source.index(f"{name} = {{") + len(f"{name} = {{")
    depth = 1
    for offset in range(start, len(source)):
        char = source[offset]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:offset]
    raise AssertionError(f"unterminated Lua table {name!r}")


def _string_list(source: str, name: str) -> list[str]:
    """Values of a Lua array-of-strings literal, in source order."""
    return re.findall(r'"([^"]*)"', _table_body(source, name))


def _string_map(source: str, name: str) -> dict[str, str]:
    """Key/value pairs of a Lua ``KEY = "value"`` table literal."""
    return dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', _table_body(source, name)))


def _scalar(source: str, name: str) -> str:
    match = re.search(rf'^{re.escape(name)} = "([^"]*)"$', source, re.MULTILINE)
    assert match is not None, f"{name} is not declared as a string constant"
    return match.group(1)


@pytest.fixture(scope="module")
def protocol_lua() -> str:
    return _read(PROTOCOL_LUA)


@pytest.fixture(scope="module")
def ipc_lua() -> str:
    return _read(IPC_LUA)


def test_versions_match_python(protocol_lua: str) -> None:
    assert _scalar(protocol_lua, "Protocol.PROTOCOL_VERSION") == PROTOCOL_VERSION
    assert _scalar(protocol_lua, "Protocol.SCHEMA_VERSION") == SCHEMA_VERSION
    assert _scalar(protocol_lua, "Protocol.MOD_VERSION") == MOD_VERSION
    assert _scalar(protocol_lua, "Protocol.TARGET_BUILD") == TARGET_BUILD
    assert _string_list(protocol_lua, "Protocol.SUPPORTED_BUILDS") == list(SUPPORTED_BUILDS)


def test_mod_info_declares_the_same_mod_version() -> None:
    for info in (REPO_ROOT / "pz-mod" / "mod.info", REPO_ROOT / "pz-mod" / "42" / "mod.info"):
        declared = dict(line.split("=", 1) for line in _read(info).splitlines() if "=" in line)
        assert declared["modversion"] == MOD_VERSION
        assert declared["pzversion"] == TARGET_BUILD


def test_action_whitelist_is_complete_and_ordered(protocol_lua: str) -> None:
    """The mod must know every action, and no action the protocol does not."""
    assert _string_list(protocol_lua, "Protocol.ACTION_NAMES") == [a.value for a in ActionName]


def test_action_classes_match(protocol_lua: str) -> None:
    body = protocol_lua[protocol_lua.index("Protocol.READ_ONLY_ACTIONS") :]
    read_only = set(re.findall(r'"([^"]*)"', body[: body.index("\n\n")]))
    assert read_only == {"world.inspect", "action.wait"}

    always = protocol_lua[protocol_lua.index("Protocol.ALWAYS_ALLOWED_ACTIONS") :]
    always_allowed = set(re.findall(r'"([^"]*)"', always[: always.index("\n\n")]))
    assert always_allowed == {"safety.stop", "session.disarm", "plan.cancel"}


def test_reason_codes_match(protocol_lua: str) -> None:
    lua_reasons = _string_map(protocol_lua, "Protocol.REASON")
    assert set(lua_reasons) == {code.name for code in ReasonCode}
    # Every code's Lua key and value agree, which is what lets the mod emit the
    # key it looked up without a second table.
    assert all(key == value for key, value in lua_reasons.items())
    assert set(lua_reasons.values()) == {code.value for code in ReasonCode}


@pytest.mark.parametrize(
    ("table", "enum"),
    [
        ("Protocol.STATUS", ActionStatus),
        ("Protocol.MODE", SessionMode),
        ("Protocol.DANGER", DangerLevel),
        ("Protocol.OWNERSHIP", ActionOwnership),
        ("Protocol.CAPABILITY", CapabilityState),
    ],
)
def test_enum_vocabularies_match(
    protocol_lua: str,
    table: str,
    enum: type[ActionStatus]
    | type[SessionMode]
    | type[DangerLevel]
    | type[ActionOwnership]
    | type[CapabilityState],
) -> None:
    lua_values = _string_map(protocol_lua, table)
    assert set(lua_values.values()) == {member.value for member in enum}
    assert set(lua_values) == {member.name for member in enum}


def test_ipc_filenames_match_the_layout(ipc_lua: str) -> None:
    """A filename that differs by one character is a silent, total outage."""
    files = _string_map(ipc_lua, "Ipc.FILES")
    assert files == {
        "session": layout.SESSION_FILE,
        "capabilities": layout.CAPABILITIES_FILE,
        "snapshot_a": layout.SNAPSHOT_SLOT_A_FILE,
        "snapshot_b": layout.SNAPSHOT_SLOT_B_FILE,
        "snapshot_pointer": layout.SNAPSHOT_POINTER_FILE,
        "observation_events": layout.OBSERVATION_EVENTS_FILE,
        "command_queue": layout.COMMAND_QUEUE_FILE,
        "command_ack": layout.COMMAND_ACK_FILE,
        "game_heartbeat": layout.GAME_HEARTBEAT_FILE,
        "sidecar_heartbeat": layout.SIDECAR_HEARTBEAT_FILE,
        "panic_stop": layout.PANIC_STOP_FILE,
    }


def test_ipc_never_offers_the_sidecar_lock(ipc_lua: str) -> None:
    """The lock is the sidecar's own; the mod has no business touching it."""
    assert layout.SIDECAR_LOCK_FILE not in ipc_lua


def test_journal_record_types_match(ipc_lua: str) -> None:
    from pz_agent_core.ipc.journal import HEADER_TYPE, ROTATED_TYPE

    assert _scalar(ipc_lua, "Ipc.HEADER_TYPE") == HEADER_TYPE
    assert _scalar(ipc_lua, "Ipc.ROTATED_TYPE") == ROTATED_TYPE


def test_every_mod_source_is_free_of_dynamic_loading() -> None:
    """Restates the CI gate locally, where a failure is cheap to diagnose."""
    banned = ("loadstring", "dofile", "require(")
    for path in sorted(MOD_LUA_ROOT.rglob("*.lua")):
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{path.relative_to(REPO_ROOT)} uses {token}"
