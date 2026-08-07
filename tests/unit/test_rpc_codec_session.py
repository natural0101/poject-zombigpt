"""The session codec: round trips, strictness, and what the messages must not say.

Round trips assert equality of the *record*, never of the JSON. A field that
both halves drop in the same way passes a shape assertion and fails this one.

Two of the record's fields are :class:`~enum.StrEnum` members, and a StrEnum
member compares equal to its own value — ``SessionMode.OBSERVE == "OBSERVE"``
is ``True``. So record equality alone would accept a decoder that handed back
the raw string, and every round trip here also asserts the member's *type*.
"""

from __future__ import annotations

import json
from dataclasses import fields
from typing import Any

import pytest

from pz_agent_core.observation.compact import save_scope
from pz_agent_core.protocol import DangerLevel, SessionMode
from pz_agent_mcp.ports import SessionSnapshot, StopReport
from pz_agent_mcp.remote.codec import CodecError
from pz_agent_mcp.remote.codec.session import (
    decode_session_snapshot,
    decode_stop_report,
    encode_session_snapshot,
    encode_stop_report,
)

#: Shaped like something a redactor would be sorry to have missed. It is put in
#: the *value* position of a field that is then rejected, and asserted absent
#: from the exception.
SECRET = "sk-live-51H0-PZ-token-9f8e7d6c5b4a3210"

#: The install this is built for is Russian; the save id is a directory
#: fragment and the build string comes off the game, so both can be Cyrillic.
CYRILLIC_SAVE = "Песочница/Малдро-Игрок"
CYRILLIC_BUILD = "42.20 сборка"

SNAPSHOT_REQUIRED = (
    "session_id",
    "mode",
    "armed",
    "connected",
    "protocol_version",
    "danger_level",
    "capability_revision",
    "game_heartbeat_ok",
    "sidecar_heartbeat_ok",
)
SNAPSHOT_OPTIONAL = ("build", "save_id", "observation_seq", "active_action_id")

SNAPSHOT_BOOLEANS = ("armed", "connected", "game_heartbeat_ok", "sidecar_heartbeat_ok")

STOP_REQUIRED = ("cleared", "disarmed", "mode")

#: The exact object this codec puts on the wire, written out by hand.
#:
#: A round trip cannot see a key that *both* halves moved the same way.
#: ``{"armed": record.connected}`` in the encoder, paired with
#: ``armed=require_bool(payload, "connected")`` in the decoder, round-trips
#: every record perfectly — and hands the peer a snapshot with two fields
#: exchanged. The peer is a second implementation reading these names off the
#: wire, so which key carries which field is part of the contract, and pinning
#: it means comparing against a literal rather than against the encoder's own
#: output.
GOLDEN_SNAPSHOT_JSON: dict[str, Any] = {
    "session_id": "s-7f1c9a2b",
    "mode": "ASSISTED",
    "armed": True,
    "connected": False,
    "protocol_version": "1.4",
    "build": "42.20",
    "save_id": "Sandbox/Muldraugh, KY",
    "danger_level": "high",
    "observation_seq": 4211,
    "capability_revision": 9,
    "game_heartbeat_ok": False,
    "sidecar_heartbeat_ok": True,
    "active_action_id": "a-31",
}

GOLDEN_SNAPSHOT = SessionSnapshot(
    session_id="s-7f1c9a2b",
    mode=SessionMode.ASSISTED,
    armed=True,
    connected=False,
    protocol_version="1.4",
    build="42.20",
    save_id="Sandbox/Muldraugh, KY",
    danger_level=DangerLevel.HIGH,
    observation_seq=4211,
    capability_revision=9,
    game_heartbeat_ok=False,
    sidecar_heartbeat_ok=True,
    active_action_id="a-31",
)

GOLDEN_STOP_JSON: dict[str, Any] = {"cleared": 3, "disarmed": True, "mode": "OBSERVE"}

GOLDEN_STOP = StopReport(cleared=3, disarmed=True, mode=SessionMode.OBSERVE)


def full_snapshot(**overrides: Any) -> SessionSnapshot:
    """A snapshot with every field, including every optional one, populated."""
    values: dict[str, Any] = {
        "session_id": "s-7f1c9a2b",
        "mode": SessionMode.ASSISTED,
        "armed": True,
        "connected": True,
        "protocol_version": "1.4",
        "build": "42.20",
        "save_id": "Sandbox/Muldraugh, KY",
        "danger_level": DangerLevel.HIGH,
        "observation_seq": 4211,
        "capability_revision": 9,
        "game_heartbeat_ok": True,
        "sidecar_heartbeat_ok": False,
        "active_action_id": "a-31",
    }
    values.update(overrides)
    return SessionSnapshot(**values)


def bare_snapshot() -> SessionSnapshot:
    """A snapshot with every optional field left out — the pre-game state."""
    return SessionSnapshot(
        session_id="s-0",
        mode=SessionMode.OFF,
        armed=False,
        connected=False,
        protocol_version="1.4",
        danger_level=DangerLevel.NONE,
        capability_revision=0,
        game_heartbeat_ok=False,
        sidecar_heartbeat_ok=False,
    )


def assert_snapshot_round_trip(record: SessionSnapshot) -> None:
    decoded = decode_session_snapshot(encode_session_snapshot(record))
    assert decoded == record
    # A StrEnum member equals its own value, so equality above would also hold
    # for a decoder that returned the raw string.
    assert isinstance(decoded.mode, SessionMode)
    assert isinstance(decoded.danger_level, DangerLevel)


# -- the parametrisation itself is pinned ---------------------------------


def test_every_snapshot_field_is_classified_as_required_or_optional() -> None:
    """A field added to the record must land in one of the lists below.

    Otherwise the strictness tests would keep passing while quietly not
    covering it.
    """
    declared = {f.name for f in fields(SessionSnapshot)}
    assert declared == set(SNAPSHOT_REQUIRED) | set(SNAPSHOT_OPTIONAL)
    # Optional means "declared ``| None``", not "has a default": four of the
    # required fields have defaults and are still required on the wire.
    optional_by_type = {f.name for f in fields(SessionSnapshot) if "None" in str(f.type)}
    assert optional_by_type == set(SNAPSHOT_OPTIONAL)


def test_every_stop_field_is_required() -> None:
    assert {f.name for f in fields(StopReport)} == set(STOP_REQUIRED)


def test_every_boolean_field_is_covered_by_the_one_at_a_time_tests() -> None:
    """A fifth boolean added to the record must join the list below.

    The key-mapping tests are the only thing standing between a transposed pair
    of booleans and a green suite, and they are driven off this tuple.
    """
    assert {f.name for f in fields(SessionSnapshot) if str(f.type) == "bool"} == set(
        SNAPSHOT_BOOLEANS
    )


def test_encode_emits_exactly_the_record_fields() -> None:
    """No field dropped on the way out, and none invented."""
    assert set(encode_session_snapshot(full_snapshot())) == {
        f.name for f in fields(SessionSnapshot)
    }
    assert set(
        encode_stop_report(StopReport(cleared=2, disarmed=True, mode=SessionMode.OBSERVE))
    ) == {f.name for f in fields(StopReport)}


# -- round trips ----------------------------------------------------------


def test_snapshot_round_trip_with_every_field_present() -> None:
    assert_snapshot_round_trip(full_snapshot())


def test_snapshot_round_trip_with_every_optional_field_absent() -> None:
    assert_snapshot_round_trip(bare_snapshot())


@pytest.mark.parametrize("field_name", SNAPSHOT_OPTIONAL)
def test_snapshot_round_trip_with_one_optional_field_absent(field_name: str) -> None:
    """Each optional field, alone, absent — the rest populated."""
    assert_snapshot_round_trip(full_snapshot(**{field_name: None}))


@pytest.mark.parametrize("field_name", SNAPSHOT_OPTIONAL)
def test_snapshot_decodes_when_an_optional_key_is_omitted_entirely(field_name: str) -> None:
    """A peer that omits a null rather than sending it says the same thing."""
    record = full_snapshot(**{field_name: None})
    payload = encode_session_snapshot(record)
    del payload[field_name]
    assert decode_session_snapshot(payload) == record


@pytest.mark.parametrize("mode", list(SessionMode))
@pytest.mark.parametrize("danger", list(DangerLevel))
def test_snapshot_round_trip_covers_every_enum_member(
    mode: SessionMode, danger: DangerLevel
) -> None:
    assert_snapshot_round_trip(full_snapshot(mode=mode, danger_level=danger))


@pytest.mark.parametrize("cleared", [0, 1, 47])
@pytest.mark.parametrize("disarmed", [True, False])
@pytest.mark.parametrize("mode", list(SessionMode))
def test_stop_report_round_trip(cleared: int, disarmed: bool, mode: SessionMode) -> None:
    record = StopReport(cleared=cleared, disarmed=disarmed, mode=mode)
    decoded = decode_stop_report(encode_stop_report(record))
    assert decoded == record
    assert isinstance(decoded.mode, SessionMode)


def test_snapshot_booleans_are_not_transposed() -> None:
    """Every boolean distinguished from every other, one at a time.

    Four booleans that all round-trip when all four are True proves nothing
    about which key carries which. This still only pins the two halves against
    *each other* — the wire names are pinned by the section below.
    """
    for name in SNAPSHOT_BOOLEANS:
        record = full_snapshot(**{other: other == name for other in SNAPSHOT_BOOLEANS})
        decoded = decode_session_snapshot(encode_session_snapshot(record))
        assert decoded == record
        assert getattr(decoded, name) is True


# -- which key carries which field ----------------------------------------
#
# Everything above this line is a round trip, and a round trip is blind to a
# key swapped consistently in both halves: encode ``record.connected`` under
# ``"armed"``, decode ``armed`` from ``payload["armed"]``'s neighbour, and every
# record still survives the trip while the peer reads a session as armed when
# it is merely connected. The peer is the other implementation of this format,
# so the mapping is pinned here against hand-written literals.


def test_encode_produces_the_hand_written_wire_object() -> None:
    """Byte-for-byte against a literal, not against the decoder."""
    assert encode_session_snapshot(GOLDEN_SNAPSHOT) == GOLDEN_SNAPSHOT_JSON
    assert encode_stop_report(GOLDEN_STOP) == GOLDEN_STOP_JSON


def test_decode_reads_the_hand_written_wire_object() -> None:
    """And the decoder against the same literal, not against the encoder."""
    assert decode_session_snapshot(dict(GOLDEN_SNAPSHOT_JSON)) == GOLDEN_SNAPSHOT
    assert decode_stop_report(dict(GOLDEN_STOP_JSON)) == GOLDEN_STOP


@pytest.mark.parametrize("field_name", SNAPSHOT_BOOLEANS)
def test_each_boolean_field_is_written_under_its_own_key(field_name: str) -> None:
    """One boolean true at a time, asserted on the payload.

    A single literal payload cannot separate four booleans: there are two
    values to go round four fields, so some pair always agrees and a swap of
    that pair survives. Each field gets its own vector instead. ``is True`` and
    ``is False`` rather than ``==``, because ``1 == True``.
    """
    record = full_snapshot(**{name: name == field_name for name in SNAPSHOT_BOOLEANS})
    payload = encode_session_snapshot(record)
    assert payload[field_name] is True
    for other in SNAPSHOT_BOOLEANS:
        if other != field_name:
            assert payload[other] is False


@pytest.mark.parametrize("field_name", SNAPSHOT_BOOLEANS)
def test_each_boolean_field_is_read_from_its_own_key(field_name: str) -> None:
    """The same one-hot vector, driven into the decoder from a literal."""
    payload = dict(GOLDEN_SNAPSHOT_JSON)
    payload.update({name: name == field_name for name in SNAPSHOT_BOOLEANS})
    decoded = decode_session_snapshot(payload)
    assert getattr(decoded, field_name) is True
    for other in SNAPSHOT_BOOLEANS:
        if other != field_name:
            assert getattr(decoded, other) is False


@pytest.mark.parametrize(
    ("field_name", "distinct"),
    [
        ("session_id", "s-DISTINCT"),
        ("protocol_version", "9.99-DISTINCT"),
        ("build", "42.20-DISTINCT"),
        ("save_id", "Sandbox/DISTINCT"),
        ("active_action_id", "a-DISTINCT"),
        ("observation_seq", 314159),
        ("capability_revision", 271828),
    ],
)
def test_each_non_boolean_field_is_written_under_its_own_key(
    field_name: str, distinct: object
) -> None:
    """One field at a time given a value no other field could be holding.

    The two integers and the five strings are otherwise interchangeable on the
    wire, and the golden object alone would not catch a pair of them swapped in
    both halves if their values happened to be equal.
    """
    payload = encode_session_snapshot(full_snapshot(**{field_name: distinct}))
    assert payload[field_name] == distinct
    assert [key for key, value in payload.items() if value == distinct] == [field_name]


# -- enums travel as values -----------------------------------------------


def test_enums_are_encoded_as_their_value_not_their_name_or_ordinal() -> None:
    payload = encode_session_snapshot(full_snapshot(danger_level=DangerLevel.NONE))
    assert payload["danger_level"] == "none"
    assert payload["danger_level"] != "NONE"
    assert payload["mode"] == "ASSISTED"
    assert not isinstance(payload["mode"], int)
    assert not isinstance(payload["danger_level"], int)


def test_decoding_an_enum_by_name_is_refused() -> None:
    """``DangerLevel.NONE.name`` is ``"NONE"`` and its value is ``"none"``.

    A decoder reaching for the name would accept this payload; one reading the
    value refuses it.
    """
    payload = encode_session_snapshot(full_snapshot())
    payload["danger_level"] = "NONE"
    with pytest.raises(CodecError):
        decode_session_snapshot(payload)


def test_decoding_an_enum_by_ordinal_is_refused() -> None:
    payload = encode_session_snapshot(full_snapshot())
    payload["mode"] = 1
    with pytest.raises(CodecError):
        decode_session_snapshot(payload)


def test_unknown_enum_value_is_refused_not_defaulted() -> None:
    for field_name, bogus in (("mode", "SUPERVISOR"), ("danger_level", "apocalyptic")):
        payload = encode_session_snapshot(full_snapshot())
        payload[field_name] = bogus
        with pytest.raises(CodecError):
            decode_session_snapshot(payload)


# -- it has to survive a process boundary ---------------------------------


def test_encoded_snapshot_is_json_serialisable() -> None:
    payload = encode_session_snapshot(full_snapshot())
    assert json.loads(json.dumps(payload)) == payload


def test_encoded_bare_snapshot_is_json_serialisable() -> None:
    payload = encode_session_snapshot(bare_snapshot())
    assert json.loads(json.dumps(payload)) == payload


def test_encoded_stop_report_is_json_serialisable() -> None:
    payload = encode_stop_report(StopReport(cleared=3, disarmed=True, mode=SessionMode.OBSERVE))
    assert json.loads(json.dumps(payload)) == payload


def test_snapshot_survives_an_actual_json_round_trip() -> None:
    record = full_snapshot()
    assert (
        decode_session_snapshot(json.loads(json.dumps(encode_session_snapshot(record)))) == record
    )


# -- Cyrillic --------------------------------------------------------------


def test_cyrillic_text_survives_the_round_trip() -> None:
    record = full_snapshot(
        session_id="сессия-7",
        build=CYRILLIC_BUILD,
        save_id=CYRILLIC_SAVE,
        active_action_id="действие-3",
    )
    payload = encode_session_snapshot(record)
    assert payload["save_id"] == CYRILLIC_SAVE
    assert decode_session_snapshot(json.loads(json.dumps(payload))) == record


def test_cyrillic_survives_ascii_escaped_json() -> None:
    """An encoder elsewhere in the stack may set ``ensure_ascii``; escaping is
    not allowed to change the string."""
    record = full_snapshot(save_id=CYRILLIC_SAVE)
    text = json.dumps(encode_session_snapshot(record), ensure_ascii=True)
    assert decode_session_snapshot(json.loads(text)).save_id == CYRILLIC_SAVE


# -- strictness: nothing is defaulted -------------------------------------


@pytest.mark.parametrize("field_name", SNAPSHOT_REQUIRED)
def test_snapshot_missing_required_field_is_refused(field_name: str) -> None:
    payload = encode_session_snapshot(full_snapshot())
    del payload[field_name]
    with pytest.raises(CodecError) as excinfo:
        decode_session_snapshot(payload)
    assert field_name in str(excinfo.value)


@pytest.mark.parametrize("field_name", STOP_REQUIRED)
def test_stop_report_missing_required_field_is_refused(field_name: str) -> None:
    payload = encode_stop_report(StopReport(cleared=1, disarmed=True, mode=SessionMode.OBSERVE))
    del payload[field_name]
    with pytest.raises(CodecError) as excinfo:
        decode_stop_report(payload)
    assert field_name in str(excinfo.value)


@pytest.mark.parametrize("field_name", SNAPSHOT_REQUIRED)
def test_snapshot_required_field_explicitly_null_is_refused(field_name: str) -> None:
    """``null`` is not "absent, so use the default" either."""
    payload = encode_session_snapshot(full_snapshot())
    payload[field_name] = None
    with pytest.raises(CodecError):
        decode_session_snapshot(payload)


def test_the_defaulted_fields_are_still_required() -> None:
    """The four fields the dataclass gives a default are the dangerous ones.

    A decoder that fell back to the dataclass default would report a healthy
    session as heartbeat-dead and a dangerous one as ``NONE`` — confident,
    wrong, and safe-looking.
    """
    for field_name in (
        "danger_level",
        "capability_revision",
        "game_heartbeat_ok",
        "sidecar_heartbeat_ok",
    ):
        payload = encode_session_snapshot(full_snapshot())
        del payload[field_name]
        with pytest.raises(CodecError):
            decode_session_snapshot(payload)


def test_an_empty_payload_is_refused() -> None:
    with pytest.raises(CodecError):
        decode_session_snapshot({})
    with pytest.raises(CodecError):
        decode_stop_report({})


# -- strictness: types are not coerced ------------------------------------


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("session_id", 7),
        ("protocol_version", 1.4),
        ("capability_revision", "9"),
        ("capability_revision", 9.0),
        ("capability_revision", True),
        ("armed", 1),
        ("armed", "true"),
        ("connected", 0),
        ("game_heartbeat_ok", 1),
        ("sidecar_heartbeat_ok", "false"),
        ("observation_seq", "4211"),
        ("observation_seq", True),
        ("build", 42),
        ("save_id", ["Sandbox"]),
        ("active_action_id", 31),
        ("mode", True),
        ("danger_level", 3),
    ],
)
def test_snapshot_type_confusion_is_refused(field_name: str, value: Any) -> None:
    payload = encode_session_snapshot(full_snapshot())
    payload[field_name] = value
    with pytest.raises(CodecError):
        decode_session_snapshot(payload)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("cleared", "3"),
        ("cleared", 3.0),
        ("cleared", True),
        ("disarmed", 1),
        ("disarmed", "yes"),
        ("mode", 2),
        ("mode", "PARANOID"),
    ],
)
def test_stop_report_type_confusion_is_refused(field_name: str, value: Any) -> None:
    payload = encode_stop_report(StopReport(cleared=1, disarmed=True, mode=SessionMode.OBSERVE))
    payload[field_name] = value
    with pytest.raises(CodecError):
        decode_stop_report(payload)


# -- the record's own invariant survives decoding -------------------------


def test_a_negative_cleared_does_not_produce_a_record() -> None:
    """``StopReport`` refuses a negative count; decoding must not route around it."""
    payload = encode_stop_report(StopReport(cleared=0, disarmed=True, mode=SessionMode.OBSERVE))
    payload["cleared"] = -1
    with pytest.raises(CodecError) as excinfo:
        decode_stop_report(payload)
    assert "cleared" in str(excinfo.value)
    # The record's own message quotes the number ("got -1"); this one must not,
    # and chaining it would have put it in the traceback anyway.
    assert "-1" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_a_negative_cleared_is_refused_by_the_codec_error_type() -> None:
    """A bare ``ValueError`` escaping here would not be a decode failure the
    router can turn into ``MALFORMED``."""
    payload = encode_stop_report(StopReport(cleared=0, disarmed=False, mode=SessionMode.OFF))
    payload["cleared"] = -99
    with pytest.raises(CodecError):
        decode_stop_report(payload)


def test_zero_cleared_is_a_real_answer_not_a_refusal() -> None:
    """Nothing to clear is a stop that worked, not a stop that failed."""
    record = StopReport(cleared=0, disarmed=True, mode=SessionMode.OBSERVE)
    assert decode_stop_report(encode_stop_report(record)) == record


# -- messages name the field and the reason, never the value --------------


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        # A string field is rejected by wrapping the secret in a list — the
        # secret itself is a perfectly valid string and would be accepted.
        ("session_id", [SECRET]),
        ("protocol_version", [SECRET]),
        ("build", [SECRET]),
        ("save_id", [SECRET]),
        ("active_action_id", [SECRET]),
        # A non-string field is rejected by the secret itself.
        ("capability_revision", SECRET),
        ("armed", SECRET),
        ("connected", SECRET),
        ("game_heartbeat_ok", SECRET),
        ("sidecar_heartbeat_ok", SECRET),
        ("observation_seq", SECRET),
    ],
)
def test_no_error_message_repeats_the_value_it_rejected(field_name: str, value: Any) -> None:
    """The rejected value is put in the payload as a secret-shaped string.

    These strings reach a traceback, a log line and a bug report before any
    redactor sees them.
    """
    payload = encode_session_snapshot(full_snapshot())
    payload[field_name] = value
    with pytest.raises(CodecError) as excinfo:
        decode_session_snapshot(payload)
    assert SECRET not in str(excinfo.value)
    assert SECRET not in repr(excinfo.value)
    assert field_name in str(excinfo.value)


def test_stop_report_errors_do_not_repeat_the_value() -> None:
    for field_name in ("cleared", "disarmed"):
        payload = encode_stop_report(StopReport(cleared=1, disarmed=True, mode=SessionMode.OBSERVE))
        payload[field_name] = SECRET
        with pytest.raises(CodecError) as excinfo:
            decode_stop_report(payload)
        assert SECRET not in str(excinfo.value)
        assert field_name in str(excinfo.value)


def test_a_valid_payload_carrying_a_secret_shaped_string_is_not_an_error_at_all() -> None:
    """The rule is about *messages*, not about refusing odd-looking data.

    A save directory named like a token is still a save directory.
    """
    record = full_snapshot(save_id=SECRET)
    assert decode_session_snapshot(encode_session_snapshot(record)).save_id == SECRET


def test_the_enum_carve_out_is_the_only_place_a_value_is_named() -> None:
    """``require_enum`` names the value it rejected, deliberately.

    Its members are closed protocol identifiers, not user data. This test pins
    that the carve-out exists so nobody routes a field carrying user data
    through ``require_enum`` on the assumption that nothing is echoed.
    """
    payload = encode_session_snapshot(full_snapshot())
    payload["mode"] = "NOT_A_MODE"
    with pytest.raises(CodecError) as excinfo:
        decode_session_snapshot(payload)
    assert "mode" in str(excinfo.value)
    assert "NOT_A_MODE" in str(excinfo.value)


# -- save_id ---------------------------------------------------------------


def test_save_id_survives_verbatim_and_is_not_digested_here() -> None:
    """The raw save id crosses this link; the digest is the router's job.

    ``SessionSnapshot`` says ``save_id`` "never leaves this process", and the
    process it means is the boundary that faces the tool caller — the router
    publishes ``save_scope()`` of it (§3.13). This link is between the sidecar
    and the MCP server, both ours, over a local pipe or socket with no network
    address. The router computes the digest itself, so a codec that dropped the
    field or pre-digested it would leave every real session published as having
    no save.
    """
    raw = "Sandbox/Мулдро - Дмитрий"
    payload = encode_session_snapshot(full_snapshot(save_id=raw))
    assert payload["save_id"] == raw
    # Digesting here would move a security decision out of the module that owns
    # it, and would leave the router nothing to digest.
    assert "save_scope" not in payload
    assert decode_session_snapshot(payload).save_id == raw


def test_the_router_can_still_compute_a_scope_from_what_this_codec_delivers() -> None:
    """The consumer of the decoded field, stated as a test rather than a claim."""
    raw = "Sandbox/Мулдро - Дмитрий"
    delivered = decode_session_snapshot(encode_session_snapshot(full_snapshot(save_id=raw)))
    assert delivered.save_id is not None
    assert save_scope(delivered.save_id) == save_scope(raw)
