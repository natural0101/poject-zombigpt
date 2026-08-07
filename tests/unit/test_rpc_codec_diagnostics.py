"""The diagnostics codec: round trips, strictness, and the two things it must
not do to a log line.

Both records here are read when something has already gone wrong, so the
failure mode this file is written against is not "the codec raises" — it is
"the codec answers, plausibly, with something the core did not say".

Three of the assertions look like paranoia and are the point of the file:

* ``detail`` and ``remediation`` have dataclass defaults of ``""``, so a
  decoder that fell back to the default would produce a record that compares
  **equal** to a check that genuinely had nothing to say. The strictness tests
  therefore delete those keys from a check that *does* have a remediation, and
  insist on a refusal rather than a check reported as having no known fix.
* ``bool`` is a subclass of ``int`` in Python, so ``ok`` decoded from ``1``
  would look fine. The type-confusion tests pin that.
* ``message`` is text the sidecar's own logger already redacted. This codec is
  not allowed to redact it again, or to shorten it: the caller bounds the
  number of records, not their content, and a truncated log line is a log line
  that lies about what was logged.
"""

from __future__ import annotations

import json
from dataclasses import fields
from typing import Any

import pytest

from pz_agent_mcp.ports import DoctorCheck, LogRecord
from pz_agent_mcp.remote.codec import CodecError
from pz_agent_mcp.remote.codec.diagnostics import (
    TailQuery,
    decode_doctor_check,
    decode_doctor_checks,
    decode_log_record,
    decode_log_records,
    decode_tail_query,
    encode_doctor_check,
    encode_doctor_checks,
    encode_log_record,
    encode_log_records,
    encode_tail_query,
)

#: Shaped like something a redactor would be sorry to have missed. It is put in
#: the position of a field that is then rejected, and asserted absent from the
#: exception — a CodecError is written into a traceback and a bug report before
#: any redactor sees it.
SECRET = "sk-live-51H0-PZ-token-9f8e7d6c5b4a3210"

#: The install this is built for is Russian: a doctor's detail names a path
#: under the user's profile, and a log message is written by a sidecar whose
#: operator reads Russian.
CYRILLIC_DETAIL = "Папка сохранений не найдена: проверьте установку игры"
CYRILLIC_REMEDIATION = "Запустите игру один раз, затем повторите проверку"
CYRILLIC_MESSAGE = "Действие отклонено: рефлекс-охрана, рядом четыре зомби"

#: A path is the shape this codec is most likely to be blamed for mangling,
#: and the one a well-meaning "just scrub it here too" change would eat.
PATH_MESSAGE = (
    r"observation store at C:\Users\Игрок\Zomboid\pz-agent\state\obs.jsonl is stale: "
    r"last write 42s ago, expected every 5s (см. журнал сайдкара)"
)

CHECK_REQUIRED = ("code", "ok", "detail", "remediation")
CHECK_OPTIONAL: tuple[str, ...] = ()

RECORD_REQUIRED = ("timestamp_ms", "level", "component", "message")
RECORD_OPTIONAL = ("code", "action_id")

QUERY_REQUIRED = ("limit",)
QUERY_OPTIONAL = ("level", "component", "action_id")


def full_check(**overrides: Any) -> DoctorCheck:
    """A check with every field, including the defaulted ones, populated."""
    values: dict[str, Any] = {
        "code": "steam_library",
        "ok": False,
        "detail": "the game directory named in the config does not exist",
        "remediation": "run pz-agent config set game_dir <path>",
    }
    values.update(overrides)
    return DoctorCheck(**values)


def bare_check() -> DoctorCheck:
    """A check with both defaulted fields left at ``""``."""
    return DoctorCheck(code="user_dir", ok=True)


def full_record(**overrides: Any) -> LogRecord:
    """A log record with every field, including every optional one."""
    values: dict[str, Any] = {
        "timestamp_ms": 1_719_000_000_123,
        "level": "warning",
        "component": "ipc",
        "message": "queue write retried after a locked file",
        "code": "ipc.retry",
        "action_id": "act-7f1c9a2b",
    }
    values.update(overrides)
    return LogRecord(**values)


def bare_record() -> LogRecord:
    """A log record with every optional field left out."""
    return LogRecord(timestamp_ms=0, level="info", component="session", message="armed")


def full_query(**overrides: Any) -> TailQuery:
    values: dict[str, Any] = {
        "limit": 50,
        "level": "warning",
        "component": "actions",
        "action_id": "act-7f1c9a2b",
    }
    values.update(overrides)
    return TailQuery(**values)


# -- the parametrisation itself is pinned ---------------------------------


def test_every_check_field_is_classified_as_required_or_optional() -> None:
    """A field added to the record must land in one of the lists above.

    Otherwise the strictness tests keep passing while quietly not covering it.
    """
    assert {f.name for f in fields(DoctorCheck)} == set(CHECK_REQUIRED) | set(CHECK_OPTIONAL)
    # "Optional" means declared ``| None``. ``detail`` and ``remediation`` have
    # *defaults*, which is a different thing and is why they are required here:
    # a default is for a caller building a check, not for a payload that
    # arrived without the field.
    optional_by_type = {f.name for f in fields(DoctorCheck) if str(f.type).endswith("| None")}
    assert optional_by_type == set(CHECK_OPTIONAL) == set()


def test_every_log_field_is_classified_as_required_or_optional() -> None:
    assert {f.name for f in fields(LogRecord)} == set(RECORD_REQUIRED) | set(RECORD_OPTIONAL)
    optional_by_type = {f.name for f in fields(LogRecord) if str(f.type).endswith("| None")}
    assert optional_by_type == set(RECORD_OPTIONAL)


def test_every_query_field_is_classified_as_required_or_optional() -> None:
    assert {f.name for f in fields(TailQuery)} == set(QUERY_REQUIRED) | set(QUERY_OPTIONAL)
    optional_by_type = {f.name for f in fields(TailQuery) if str(f.type).endswith("| None")}
    assert optional_by_type == set(QUERY_OPTIONAL)


def test_encode_emits_exactly_the_check_fields() -> None:
    """No field dropped on the way out, and none invented."""
    assert set(encode_doctor_check(full_check())) == {f.name for f in fields(DoctorCheck)}
    assert set(encode_doctor_check(bare_check())) == {f.name for f in fields(DoctorCheck)}


def test_encode_emits_exactly_the_log_fields() -> None:
    assert set(encode_log_record(full_record())) == {f.name for f in fields(LogRecord)}
    assert set(encode_log_record(bare_record())) == {f.name for f in fields(LogRecord)}


def test_encode_emits_exactly_the_query_fields() -> None:
    assert set(encode_tail_query(full_query())) == {f.name for f in fields(TailQuery)}
    assert set(encode_tail_query(TailQuery(limit=1))) == {f.name for f in fields(TailQuery)}


# -- round trips: equality of the record ----------------------------------


def test_check_round_trip_with_every_field_present() -> None:
    check = full_check()
    assert decode_doctor_check(encode_doctor_check(check)) == check


def test_check_round_trip_with_the_defaulted_fields_empty() -> None:
    check = bare_check()
    decoded = decode_doctor_check(encode_doctor_check(check))
    assert decoded == check
    assert decoded.detail == ""
    assert decoded.remediation == ""


@pytest.mark.parametrize("ok", [True, False])
def test_check_round_trip_for_both_verdicts(ok: bool) -> None:
    check = full_check(ok=ok)
    decoded = decode_doctor_check(encode_doctor_check(check))
    assert decoded == check
    # `True == 1`, so equality alone would not catch a verdict turned into a
    # number on the way through.
    assert decoded.ok is ok


def test_check_round_trip_with_only_a_detail_and_no_remediation() -> None:
    check = full_check(remediation="")
    decoded = decode_doctor_check(encode_doctor_check(check))
    assert decoded == check
    assert decoded.detail != ""


def test_check_round_trip_with_only_a_remediation_and_no_detail() -> None:
    check = full_check(detail="")
    assert decode_doctor_check(encode_doctor_check(check)) == check


def test_log_round_trip_with_every_field_present() -> None:
    record = full_record()
    assert decode_log_record(encode_log_record(record)) == record


def test_log_round_trip_with_every_optional_field_absent() -> None:
    record = bare_record()
    decoded = decode_log_record(encode_log_record(record))
    assert decoded == record
    assert decoded.code is None
    assert decoded.action_id is None


@pytest.mark.parametrize("field_name", RECORD_OPTIONAL)
def test_log_round_trip_with_each_optional_field_alone(field_name: str) -> None:
    """Each optional field present while the other is absent."""
    record = full_record(**{name: None for name in RECORD_OPTIONAL if name != field_name})
    decoded = decode_log_record(encode_log_record(record))
    assert decoded == record
    assert getattr(decoded, field_name) is not None


@pytest.mark.parametrize("field_name", RECORD_OPTIONAL)
def test_log_decodes_when_an_optional_key_is_omitted_entirely(field_name: str) -> None:
    """A peer that omits a null rather than sending it says the same thing."""
    record = full_record(**{field_name: None})
    payload = encode_log_record(record)
    del payload[field_name]
    assert decode_log_record(payload) == record


@pytest.mark.parametrize("timestamp_ms", [0, 1, 1_719_000_000_123, 2**53])
def test_log_round_trip_for_a_range_of_timestamps(timestamp_ms: int) -> None:
    record = full_record(timestamp_ms=timestamp_ms)
    decoded = decode_log_record(encode_log_record(record))
    assert decoded == record
    assert decoded.timestamp_ms == timestamp_ms


def test_query_round_trip_with_every_field_present() -> None:
    query = full_query()
    assert decode_tail_query(encode_tail_query(query)) == query


def test_query_round_trip_with_every_optional_field_absent() -> None:
    query = TailQuery(limit=20)
    decoded = decode_tail_query(encode_tail_query(query))
    assert decoded == query
    assert decoded.level is None
    assert decoded.component is None
    assert decoded.action_id is None


@pytest.mark.parametrize("field_name", QUERY_OPTIONAL)
def test_query_round_trip_with_each_filter_alone(field_name: str) -> None:
    query = full_query(**{name: None for name in QUERY_OPTIONAL if name != field_name})
    decoded = decode_tail_query(encode_tail_query(query))
    assert decoded == query
    assert getattr(decoded, field_name) is not None


@pytest.mark.parametrize("field_name", QUERY_OPTIONAL)
def test_query_decodes_when_an_optional_key_is_omitted_entirely(field_name: str) -> None:
    query = full_query(**{field_name: None})
    payload = encode_tail_query(query)
    del payload[field_name]
    assert decode_tail_query(payload) == query


def test_a_zero_limit_is_carried_and_is_not_a_missing_one() -> None:
    """``0`` is falsy; a codec that tested truthiness would drop the bound."""
    query = TailQuery(limit=0)
    assert encode_tail_query(query)["limit"] == 0
    assert decode_tail_query(encode_tail_query(query)) == query


# -- the list bodies -------------------------------------------------------


def test_checks_body_round_trip() -> None:
    checks = (full_check(), bare_check(), full_check(code="build_supported", ok=True))
    decoded = decode_doctor_checks(encode_doctor_checks(checks))
    assert decoded == checks
    assert isinstance(decoded, tuple)


def test_records_body_round_trip_keeps_order() -> None:
    records = tuple(full_record(timestamp_ms=n, message=f"line {n}") for n in range(5))
    decoded = decode_log_records(encode_log_records(records))
    assert decoded == records
    assert [r.message for r in decoded] == [f"line {n}" for n in range(5)]


def test_an_empty_doctor_is_not_a_missing_key() -> None:
    """ "Nothing to report" and "unreadable" must not have the same spelling."""
    body = encode_doctor_checks(())
    assert body == {"checks": []}
    assert decode_doctor_checks(body) == ()


def test_an_empty_tail_is_not_a_missing_key() -> None:
    body = encode_log_records(())
    assert body == {"records": []}
    assert decode_log_records(body) == ()


def test_a_missing_checks_key_is_refused() -> None:
    with pytest.raises(CodecError) as excinfo:
        decode_doctor_checks({})
    assert "checks" in str(excinfo.value)


def test_a_missing_records_key_is_refused() -> None:
    with pytest.raises(CodecError) as excinfo:
        decode_log_records({})
    assert "records" in str(excinfo.value)


@pytest.mark.parametrize("value", [{}, "none", 0, None, True])
def test_a_checks_key_that_is_not_an_array_is_refused(value: Any) -> None:
    with pytest.raises(CodecError):
        decode_doctor_checks({"checks": value})


@pytest.mark.parametrize("value", [{}, "none", 0, None, True])
def test_a_records_key_that_is_not_an_array_is_refused(value: Any) -> None:
    with pytest.raises(CodecError):
        decode_log_records({"records": value})


@pytest.mark.parametrize("item", [None, 3, "check", ["code"]])
def test_a_checks_entry_that_is_not_an_object_is_refused(item: Any) -> None:
    with pytest.raises(CodecError):
        decode_doctor_checks({"checks": [encode_doctor_check(full_check()), item]})


@pytest.mark.parametrize("item", [None, 3, "record", ["level"]])
def test_a_records_entry_that_is_not_an_object_is_refused(item: Any) -> None:
    with pytest.raises(CodecError):
        decode_log_records({"records": [encode_log_record(full_record()), item]})


def test_a_checks_entry_that_is_a_bad_check_refuses_the_whole_body() -> None:
    """A partial doctor that did not say it was partial reads as a healthy one."""
    broken = encode_doctor_check(full_check())
    del broken["remediation"]
    with pytest.raises(CodecError):
        decode_doctor_checks({"checks": [encode_doctor_check(bare_check()), broken]})


def test_a_records_entry_that_is_a_bad_record_refuses_the_whole_body() -> None:
    broken = encode_log_record(full_record())
    del broken["message"]
    with pytest.raises(CodecError):
        decode_log_records({"records": [encode_log_record(bare_record()), broken]})


# -- it has to survive a process boundary ---------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        encode_doctor_check(full_check()),
        encode_doctor_check(bare_check()),
        encode_log_record(full_record()),
        encode_log_record(bare_record()),
        encode_doctor_checks((full_check(), bare_check())),
        encode_log_records((full_record(), bare_record())),
        encode_tail_query(full_query()),
        encode_tail_query(TailQuery(limit=1)),
    ],
)
def test_the_encoded_form_is_json_serialisable(payload: dict[str, Any]) -> None:
    assert json.loads(json.dumps(payload)) == payload


def test_check_survives_an_actual_json_round_trip() -> None:
    check = full_check()
    assert decode_doctor_check(json.loads(json.dumps(encode_doctor_check(check)))) == check


def test_log_record_survives_an_actual_json_round_trip() -> None:
    record = full_record()
    assert decode_log_record(json.loads(json.dumps(encode_log_record(record)))) == record


def test_bodies_survive_an_actual_json_round_trip() -> None:
    checks = (full_check(), bare_check())
    records = (full_record(), bare_record())
    assert decode_doctor_checks(json.loads(json.dumps(encode_doctor_checks(checks)))) == checks
    assert decode_log_records(json.loads(json.dumps(encode_log_records(records)))) == records


def test_query_survives_an_actual_json_round_trip() -> None:
    query = full_query()
    assert decode_tail_query(json.loads(json.dumps(encode_tail_query(query)))) == query


# -- Cyrillic, paths, and length: the message is carried, not treated ------


def test_cyrillic_text_survives_the_check_round_trip() -> None:
    check = full_check(detail=CYRILLIC_DETAIL, remediation=CYRILLIC_REMEDIATION)
    decoded = decode_doctor_check(json.loads(json.dumps(encode_doctor_check(check))))
    assert decoded == check
    assert decoded.detail == CYRILLIC_DETAIL
    assert decoded.remediation == CYRILLIC_REMEDIATION


def test_cyrillic_text_survives_the_log_round_trip() -> None:
    record = full_record(message=CYRILLIC_MESSAGE, component="действия")
    decoded = decode_log_record(json.loads(json.dumps(encode_log_record(record))))
    assert decoded == record
    assert decoded.message == CYRILLIC_MESSAGE


def test_cyrillic_survives_ascii_escaped_json() -> None:
    """An encoder elsewhere in the stack may set ``ensure_ascii``; escaping is
    not allowed to change the strings."""
    record = full_record(message=CYRILLIC_MESSAGE)
    check = full_check(detail=CYRILLIC_DETAIL)
    record_text = json.dumps(encode_log_record(record), ensure_ascii=True)
    check_text = json.dumps(encode_doctor_check(check), ensure_ascii=True)
    # The escaping really happened, so this is testing what it claims to.
    assert CYRILLIC_MESSAGE not in record_text
    assert decode_log_record(json.loads(record_text)) == record
    assert decode_doctor_check(json.loads(check_text)) == check


def test_a_path_shaped_message_with_cyrillic_survives_byte_for_byte() -> None:
    """This codec does not redact and does not mangle.

    Redaction happens at write time, upstream: the line handed to the encoder
    has already been through it. A second pass here would eat a backslash path
    or a profile name that the writer deliberately kept, and neither half of
    the link could then say what the log actually said.
    """
    record = full_record(message=PATH_MESSAGE)
    encoded = encode_log_record(record)
    assert encoded["message"] == PATH_MESSAGE
    decoded = decode_log_record(json.loads(json.dumps(encoded)))
    assert decoded.message == PATH_MESSAGE
    assert decoded == record
    # Byte for byte, through the encoding the transport actually uses.
    assert decoded.message.encode("utf-8") == PATH_MESSAGE.encode("utf-8")


def test_a_very_long_message_round_trips_unchanged() -> None:
    """The caller bounds the number of records, not their content.

    A codec that truncated here would produce a log line that lies about what
    was logged — and it would lie plausibly, since a long line is exactly the
    kind that gets elided in a UI anyway.
    """
    message = ("зомби подошёл слишком близко; " * 4000) + "END"
    assert len(message) > 100_000
    record = full_record(message=message)
    decoded = decode_log_record(json.loads(json.dumps(encode_log_record(record))))
    assert decoded == record
    assert decoded.message == message
    assert len(decoded.message) == len(message)
    assert decoded.message.endswith("END")


def test_whitespace_and_newlines_in_a_message_are_preserved() -> None:
    """A traceback arrives as one record with newlines in it."""
    message = "action refused\n  reflex guard: 4 zombies\n\ttrailing tab\t "
    record = full_record(message=message)
    assert decode_log_record(json.loads(json.dumps(encode_log_record(record)))).message == message


def test_an_empty_message_is_carried_as_an_empty_message() -> None:
    record = full_record(message="")
    assert decode_log_record(encode_log_record(record)) == record


# -- strictness: nothing is defaulted -------------------------------------


@pytest.mark.parametrize("field_name", CHECK_REQUIRED)
def test_check_missing_required_field_is_refused(field_name: str) -> None:
    payload = encode_doctor_check(full_check())
    del payload[field_name]
    with pytest.raises(CodecError) as excinfo:
        decode_doctor_check(payload)
    assert field_name in str(excinfo.value)


@pytest.mark.parametrize("field_name", RECORD_REQUIRED)
def test_log_missing_required_field_is_refused(field_name: str) -> None:
    payload = encode_log_record(full_record())
    del payload[field_name]
    with pytest.raises(CodecError) as excinfo:
        decode_log_record(payload)
    assert field_name in str(excinfo.value)


@pytest.mark.parametrize("field_name", QUERY_REQUIRED)
def test_query_missing_required_field_is_refused(field_name: str) -> None:
    payload = encode_tail_query(full_query())
    del payload[field_name]
    with pytest.raises(CodecError) as excinfo:
        decode_tail_query(payload)
    assert field_name in str(excinfo.value)


@pytest.mark.parametrize("field_name", CHECK_REQUIRED)
def test_check_required_field_explicitly_null_is_refused(field_name: str) -> None:
    """``null`` is not "absent, so use the dataclass default" either."""
    payload = encode_doctor_check(full_check())
    payload[field_name] = None
    with pytest.raises(CodecError):
        decode_doctor_check(payload)


@pytest.mark.parametrize("field_name", RECORD_REQUIRED)
def test_log_required_field_explicitly_null_is_refused(field_name: str) -> None:
    payload = encode_log_record(full_record())
    payload[field_name] = None
    with pytest.raises(CodecError):
        decode_log_record(payload)


def test_query_limit_explicitly_null_is_refused() -> None:
    payload = encode_tail_query(full_query())
    payload["limit"] = None
    with pytest.raises(CodecError):
        decode_tail_query(payload)


@pytest.mark.parametrize("field_name", ["detail", "remediation"])
def test_the_defaulted_check_fields_are_still_required(field_name: str) -> None:
    """``detail`` and ``remediation`` default to ``""``; the wire has no default.

    A decoder that fell back would turn a renamed or dropped key into a check
    that is missing precisely the sentence a stuck user needs — and would do it
    silently, because ``""`` is what a check with nothing to say looks like.
    """
    check = full_check()
    assert getattr(check, field_name) != ""
    payload = encode_doctor_check(check)
    del payload[field_name]
    with pytest.raises(CodecError):
        decode_doctor_check(payload)


def test_a_dropped_remediation_does_not_decode_as_an_empty_one() -> None:
    """Stated as the outcome rather than the mechanism, because the outcome is
    the bug: a failing check reported with no known fix."""
    payload = encode_doctor_check(full_check())
    del payload["remediation"]
    with pytest.raises(CodecError):
        decode_doctor_check(payload)
    # And the check that legitimately has none still round-trips, so the rule
    # above is not paid for by refusing a valid record.
    assert decode_doctor_check(encode_doctor_check(full_check(remediation=""))).remediation == ""


def test_an_unrelated_extra_key_does_not_break_a_decode() -> None:
    """A peer on a newer minor version adds fields; the major is what is pinned.

    ``docs/CORE_RPC.md``: a minor bump only adds. Refusing here would make a
    one-sided upgrade unreadable rather than merely incomplete.
    """
    payload = encode_log_record(full_record())
    payload["thread"] = "worker-2"
    assert decode_log_record(payload) == full_record()


# -- type confusion --------------------------------------------------------


@pytest.mark.parametrize("value", [1, 0, "true", "False", None, [], {}])
def test_a_verdict_that_is_not_a_boolean_is_refused(value: Any) -> None:
    """``ok`` given an int is the dangerous one: ``True == 1``, and a doctor
    that reported ``1`` as a pass would be right by accident until it was not."""
    payload = encode_doctor_check(full_check())
    payload["ok"] = value
    with pytest.raises(CodecError) as excinfo:
        decode_doctor_check(payload)
    assert "ok" in str(excinfo.value)


@pytest.mark.parametrize("field_name", ["code", "detail", "remediation"])
@pytest.mark.parametrize("value", [1, True, 2.5, [], {}, ["text"]])
def test_a_check_string_field_given_a_non_string_is_refused(field_name: str, value: Any) -> None:
    payload = encode_doctor_check(full_check())
    payload[field_name] = value
    with pytest.raises(CodecError) as excinfo:
        decode_doctor_check(payload)
    assert field_name in str(excinfo.value)


@pytest.mark.parametrize("value", ["1719000000123", "", 12.5, [], {}, True, False])
def test_a_timestamp_that_is_not_an_integer_is_refused(value: Any) -> None:
    """``True`` is in this list deliberately: ``bool`` is an ``int`` in Python,
    so a timestamp of ``1`` from a boolean would decode as a plausible date."""
    payload = encode_log_record(full_record())
    payload["timestamp_ms"] = value
    with pytest.raises(CodecError) as excinfo:
        decode_log_record(payload)
    assert "timestamp_ms" in str(excinfo.value)


@pytest.mark.parametrize("field_name", ["level", "component", "message"])
@pytest.mark.parametrize("value", [1, True, 2.5, [], {}, ["text"]])
def test_a_log_string_field_given_a_non_string_is_refused(field_name: str, value: Any) -> None:
    payload = encode_log_record(full_record())
    payload[field_name] = value
    with pytest.raises(CodecError) as excinfo:
        decode_log_record(payload)
    assert field_name in str(excinfo.value)


@pytest.mark.parametrize("field_name", RECORD_OPTIONAL)
@pytest.mark.parametrize("value", [1, True, 2.5, [], {}])
def test_an_optional_log_field_given_a_non_string_is_refused(field_name: str, value: Any) -> None:
    """Optional means "may be absent", not "may be anything"."""
    payload = encode_log_record(full_record())
    payload[field_name] = value
    with pytest.raises(CodecError) as excinfo:
        decode_log_record(payload)
    assert field_name in str(excinfo.value)


@pytest.mark.parametrize("value", ["50", 50.0, True, [], {}])
def test_a_limit_that_is_not_an_integer_is_refused(value: Any) -> None:
    payload = encode_tail_query(full_query())
    payload["limit"] = value
    with pytest.raises(CodecError) as excinfo:
        decode_tail_query(payload)
    assert "limit" in str(excinfo.value)


@pytest.mark.parametrize("field_name", QUERY_OPTIONAL)
@pytest.mark.parametrize("value", [1, True, 2.5, [], {}])
def test_a_query_filter_given_a_non_string_is_refused(field_name: str, value: Any) -> None:
    payload = encode_tail_query(full_query())
    payload[field_name] = value
    with pytest.raises(CodecError) as excinfo:
        decode_tail_query(payload)
    assert field_name in str(excinfo.value)


# -- level is a string on purpose, and it stays one ------------------------


@pytest.mark.parametrize(
    "level",
    ["debug", "info", "warning", "error", "TRACE", "уровень", "bad level", ""],
)
def test_an_unrecognised_level_is_carried_verbatim_rather_than_refused(level: str) -> None:
    """``level`` is deliberately not decoded as an enum.

    The port declares it ``str`` and the router already drops — and *counts* —
    a record whose level it does not recognise, so the tool can say "one record
    omitted". Enum-ising it here would move that decision and widen its blast
    radius: one unknown level would make the entire ``diagnostics.tail`` answer
    unreadable, and a log tail that refuses to load is the worst outcome for
    the one tool whose job is to explain a failure.
    """
    record = full_record(level=level)
    decoded = decode_log_record(json.loads(json.dumps(encode_log_record(record))))
    assert decoded == record
    assert decoded.level == level
    assert isinstance(decoded.level, str)


def test_the_level_on_the_wire_is_the_string_the_record_holds() -> None:
    """Not a name, not an ordinal — the value the port already pins."""
    assert encode_log_record(full_record(level="warning"))["level"] == "warning"
    assert encode_log_record(full_record(level="warning"))["level"] != "WARNING"


# -- an error names the field and the reason, never the value -------------


def _messages_rejecting(secret: str) -> list[str]:
    """Every refusal this codec can be made to produce with *secret* in it."""
    out: list[str] = []

    def collect(call: Any, payload: Any) -> None:
        with pytest.raises(CodecError) as excinfo:
            call(payload)
        out.append(str(excinfo.value))

    for field_name in ("code", "detail", "remediation"):
        payload = encode_doctor_check(full_check())
        payload[field_name] = [secret]
        collect(decode_doctor_check, payload)

    bad_ok = encode_doctor_check(full_check())
    bad_ok["ok"] = secret
    collect(decode_doctor_check, bad_ok)

    for field_name in ("level", "component", "message", "code", "action_id"):
        payload = encode_log_record(full_record())
        payload[field_name] = {"value": secret}
        collect(decode_log_record, payload)

    bad_ts = encode_log_record(full_record())
    bad_ts["timestamp_ms"] = secret
    collect(decode_log_record, bad_ts)

    bad_limit = encode_tail_query(full_query())
    bad_limit["limit"] = secret
    collect(decode_tail_query, bad_limit)

    for field_name in QUERY_OPTIONAL:
        payload = encode_tail_query(full_query())
        payload[field_name] = [secret]
        collect(decode_tail_query, payload)

    collect(decode_doctor_checks, {"checks": secret})
    collect(decode_doctor_checks, {"checks": [secret]})
    collect(decode_log_records, {"records": secret})
    collect(decode_log_records, {"records": [secret]})

    return out


def test_no_refusal_repeats_the_value_it_rejected() -> None:
    """A CodecError reaches a traceback and a bug report before any redactor.

    So the message names the field and the reason and stops there — a doctor
    detail carries a path through the user's profile, and a log message carries
    whatever the sidecar logged.
    """
    messages = _messages_rejecting(SECRET)
    assert messages
    for message in messages:
        assert SECRET not in message
        # Nor a fragment of it long enough to be the interesting part.
        assert "9f8e7d6c5b4a3210" not in message


def test_a_refusal_still_names_the_field_it_rejected() -> None:
    """The rule is "not the value", not "no information"."""
    payload = encode_log_record(full_record())
    payload["message"] = {"leak": SECRET}
    with pytest.raises(CodecError) as excinfo:
        decode_log_record(payload)
    text = str(excinfo.value)
    assert "message" in text
    assert SECRET not in text


def test_a_cyrillic_value_is_not_repeated_in_a_refusal_either() -> None:
    """The detail of a failing check is Russian text naming a real path."""
    payload = encode_doctor_check(full_check())
    payload["detail"] = [CYRILLIC_DETAIL]
    with pytest.raises(CodecError) as excinfo:
        decode_doctor_check(payload)
    assert CYRILLIC_DETAIL not in str(excinfo.value)
    assert "detail" in str(excinfo.value)
