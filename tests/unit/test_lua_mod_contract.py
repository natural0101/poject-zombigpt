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

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_core.ipc import layout
from pz_agent_core.ipc.journal import HEADER_TYPE, ROTATED_TYPE
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
from pz_agent_core.session.heartbeat import Heartbeat, Peer
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
    assert read_only == {a.value for a in READ_ONLY_ACTIONS}

    always = protocol_lua[protocol_lua.index("Protocol.ALWAYS_ALLOWED_ACTIONS") :]
    always_allowed = set(re.findall(r'"([^"]*)"', always[: always.index("\n\n")]))
    assert always_allowed == {a.value for a in ALWAYS_ALLOWED_ACTIONS}


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
    assert _scalar(ipc_lua, "Ipc.HEADER_TYPE") == HEADER_TYPE
    assert _scalar(ipc_lua, "Ipc.ROTATED_TYPE") == ROTATED_TYPE


#: Modules in the order the game loads them: shared before client.
_HEARTBEAT_MODULES: Final = (
    "shared/PZAgent/Json.lua",
    "shared/PZAgent/Protocol.lua",
    "shared/PZAgent/Ownership.lua",
    "client/PZAgent/Heartbeat.lua",
)

_HEARTBEAT_SCRIPT: Final = """
local session = {{
  session_id = "3f2b9c1e-0a4d-4c7b-9e21-8b6d5f0a1c33",
  nonce = "sidecar-nonce",
  game_nonce = "g1a-2-3",
  generation = 4,
}}
local document = PZAgent.Heartbeat.build({{
  session = session,
  safety = {{
    armed = true,
    mode = "AUTONOMOUS",
    danger_level = "medium",
    manual_takeover = false,
    sidecar_stale = false,
  }},
  action = PZAgent.Ownership.describe({{}}, session.session_id),
  seq = 17,
  now_ms = 1700000000000,
  player_present = true,
  player_alive = true,
  build = "{build}",
  build_verified = true,
}})
local text, err = PZAgent.Json.encode(document)
if text == nil then
  error(err, 0)
end
io.write(text)
"""


def _lua_interpreter() -> str | None:
    for candidate in ("lua5.4", "lua"):
        found = shutil.which(candidate)
        if found is not None:
            return found
    return None


@pytest.mark.skipif(_lua_interpreter() is None, reason="no Lua interpreter available")
def test_the_heartbeat_the_mod_writes_is_one_the_sidecar_can_read() -> None:
    """The mod's only consumer parses this file; a field it needs is an outage.

    ``HeartbeatMonitor`` is what ``SessionManager.staleness`` and ``resume``
    consult, and it refuses a document ``Heartbeat.from_dict`` cannot build. A
    missing ``peer`` or ``version`` therefore does not degrade one field — it
    makes a running game indistinguishable from a crashed one. Asserting the
    field names textually would not catch that, so the document is really built
    by the mod's own code and really parsed by the sidecar's.
    """
    interpreter = _lua_interpreter()
    assert interpreter is not None
    loads = "".join(f'dofile("{MOD_LUA_ROOT / module}")\n' for module in _HEARTBEAT_MODULES)
    completed = subprocess.run(
        [interpreter, "-e", loads + _HEARTBEAT_SCRIPT.format(build=TARGET_BUILD)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    payload: Any = json.loads(completed.stdout)

    heartbeat = Heartbeat.from_dict(payload)
    assert heartbeat.peer is Peer.GAME
    assert heartbeat.protocol_version == PROTOCOL_VERSION
    assert heartbeat.version == MOD_VERSION
    assert heartbeat.build == TARGET_BUILD
    assert heartbeat.seq == 17
    assert heartbeat.armed is True
    assert heartbeat.player_present is True
    assert heartbeat.mode is SessionMode.AUTONOMOUS
    assert heartbeat.danger_level is DangerLevel.MEDIUM
    # The mod answers with its *own* nonce (§3.3), not the sidecar's.
    assert heartbeat.nonce == "g1a-2-3"
    assert payload["sidecar_nonce"] == "sidecar-nonce"


def test_every_mod_source_is_free_of_dynamic_loading() -> None:
    """Restates the CI gate locally, where a failure is cheap to diagnose."""
    banned = ("loadstring", "dofile", "require(")
    for path in sorted(MOD_LUA_ROOT.rglob("*.lua")):
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{path.relative_to(REPO_ROOT)} uses {token}"
