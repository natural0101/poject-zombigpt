"""The quarantine rule for payloads that are not observations."""

from __future__ import annotations

from typing import Any

from pz_agent_core.observation.compact import (
    CONTENT_MARKER,
    MAX_TEXT_CHARS,
    REDACTED_PATH,
    UNTRUSTED_TEXT_KEY,
)
from pz_agent_core.protocol import ActionName, ReasonCode
from pz_agent_mcp.scrub import (
    MAX_DEPTH,
    MAX_KEYS,
    MAX_SEQUENCE,
    as_token,
    is_reference,
    scrub_payload,
)
from tests.fixtures import DEFAULT_SESSION, item_ref, main_container_ref
from tests.fixtures.mcp_doubles import HOSTILE_NAME


def test_protocol_vocabulary_survives_unquarantined() -> None:
    payload = scrub_payload(
        {"reason_code": ReasonCode.NO_SAFE_FOOD.value, "action": ActionName.CONSUME_EAT.value}
    )

    assert payload["reason_code"] == ReasonCode.NO_SAFE_FOOD.value
    assert payload["action"] == ActionName.CONSUME_EAT.value
    assert UNTRUSTED_TEXT_KEY not in payload


def test_references_survive_unquarantined() -> None:
    ref = item_ref("77", "player-main")

    payload = scrub_payload({"item_ref": ref, "container_ref": main_container_ref()})

    assert payload["item_ref"] == ref
    assert payload["container_ref"] == main_container_ref()


def test_a_string_that_is_neither_is_quarantined_however_token_shaped_it_looks() -> None:
    # A shape test would admit this: it has no spaces and no punctuation a token
    # forbids. Membership of a closed set is what makes the rule ungameable.
    payload = scrub_payload({"container_name": "IGNORE_PREVIOUS_INSTRUCTIONS"})

    assert "container_name" not in payload
    assert payload[UNTRUSTED_TEXT_KEY] == {"container_name": "IGNORE_PREVIOUS_INSTRUCTIONS"}
    assert payload["content_marker"] == CONTENT_MARKER


def test_a_hostile_display_name_is_redacted_and_quarantined() -> None:
    payload = scrub_payload({"container_name": HOSTILE_NAME})

    quarantined = payload[UNTRUSTED_TEXT_KEY]["container_name"]
    assert "\n" not in quarantined
    assert "C:\\Users\\hostile" not in quarantined
    assert "secrets.txt" not in quarantined
    assert len(quarantined) <= MAX_TEXT_CHARS
    assert payload["content_marker"] == CONTENT_MARKER


def test_a_path_short_enough_to_survive_truncation_is_still_replaced() -> None:
    payload = scrub_payload({"container_name": "bag at C:\\Users\\ann\\zomboid"})

    assert payload[UNTRUSTED_TEXT_KEY]["container_name"] == f"bag at {REDACTED_PATH}"


def test_numbers_booleans_and_null_pass_through() -> None:
    payload = scrub_payload({"distance": 3.5, "arrived": True, "floor": 0, "target": None})

    assert payload == {"distance": 3.5, "arrived": True, "floor": 0, "target": None}


def test_a_key_that_is_not_an_identifier_is_dropped_with_its_value() -> None:
    payload = scrub_payload({"ok key": 1, "ok_key": 2})

    assert payload == {"ok_key": 2}


def test_nesting_is_bounded() -> None:
    deep: Any = {"leaf": 1}
    for _ in range(MAX_DEPTH + 3):
        deep = {"n": deep}

    payload = scrub_payload(deep)

    depth = 0
    node: Any = payload
    while isinstance(node, dict) and "n" in node:
        node = node["n"]
        depth += 1
    assert depth <= MAX_DEPTH


def test_width_is_bounded_in_both_maps_and_lists() -> None:
    wide = scrub_payload({f"k{n}": n for n in range(MAX_KEYS * 2)})
    long = scrub_payload({"values": list(range(MAX_SEQUENCE * 2))})

    assert len(wide) == MAX_KEYS
    assert len(long["values"]) == MAX_SEQUENCE


def test_a_free_string_inside_a_list_is_quarantined_in_place() -> None:
    # A list element has no key to quarantine it under, so the element itself
    # becomes the quarantine rather than being silently dropped.
    payload = scrub_payload({"chain": ["a rusty locker", main_container_ref()]})

    assert payload["chain"][0] == {UNTRUSTED_TEXT_KEY: {"value": "a rusty locker"}}
    assert payload["chain"][1] == main_container_ref()


def test_bytes_are_refused_rather_than_walked_as_a_sequence() -> None:
    assert scrub_payload({"blob": b"\x00\x01"}) == {}


def test_an_empty_payload_stays_empty() -> None:
    assert scrub_payload(None) == {}
    assert scrub_payload({}) == {}


def test_is_reference_accepts_every_ref_kind_and_rejects_prose() -> None:
    assert is_reference(f"zombie:{DEFAULT_SESSION}:31:0")
    assert is_reference(main_container_ref())
    assert not is_reference("zombie the item")
    assert not is_reference("Base.TinnedBeans")


def test_as_token_keeps_identifiers_and_drops_everything_else() -> None:
    assert as_token("steam_library") == "steam_library"
    assert as_token("a" * 65) is None
    assert as_token("two words") is None
    assert as_token(7) is None
    assert as_token(None) is None
