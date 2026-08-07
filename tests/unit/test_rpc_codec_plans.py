"""The plan codec: round trips, the nested step list, and what messages must not say.

Round trips assert equality of the *record*, never of the JSON. A field that
both halves drop in the same way passes a shape assertion and fails this one.

Every enum on these records is a :class:`~enum.StrEnum`, and a StrEnum member
compares equal to its own value — ``ActionStatus.FAILED == "failed"`` is
``True``. So record equality alone would accept a decoder that handed back the
raw string, and every round trip here also asserts the member's *type*.

The nested ``steps`` list is where a plausible-looking codec goes wrong, so
three properties of it are pinned separately: it comes back as a ``tuple``, it
comes back in the order it went out even when the indices are not sorted and not
unique, and an empty one survives.
"""

from __future__ import annotations

import json
from dataclasses import fields
from typing import Any

import pytest

from pz_agent_core.protocol import ActionName, ActionStatus, ReasonCode, SessionMode
from pz_agent_mcp.ports import PlanRecord, PlanRequest, PlanStepRecord
from pz_agent_mcp.remote.codec import CodecError
from pz_agent_mcp.remote.codec.plans import (
    decode_plan_current,
    decode_plan_record,
    decode_plan_request,
    decode_plan_step,
    encode_plan_current,
    encode_plan_record,
    encode_plan_request,
    encode_plan_step,
)

#: Shaped like something a redactor would be sorry to have missed. It is put in
#: the *value* position of a field that is then rejected, and asserted absent
#: from the exception.
SECRET = "sk-live-51H0-PZ-token-9f8e7d6c5b4a3210"

#: The install this is built for is Russian, and the goal is the one field a
#: user types or speaks in their own words.
CYRILLIC_GOAL = "найди еду и поешь, только не рискуй"

REQUEST_REQUIRED = ("goal", "mode", "max_steps", "max_real_seconds", "idempotency_key")
REQUEST_OPTIONAL: tuple[str, ...] = ()

STEP_REQUIRED = ("index", "action", "status")
STEP_OPTIONAL = ("reason_code", "action_id")

RECORD_REQUIRED = ("plan_id", "status", "step_index", "steps")
RECORD_OPTIONAL = ("stopped_reason",)


def full_request(**overrides: Any) -> PlanRequest:
    values: dict[str, Any] = {
        "goal": "eat something safe",
        "mode": SessionMode.ASSISTED,
        "max_steps": 6,
        "max_real_seconds": 120,
        "idempotency_key": "goal-1:attempt-1",
    }
    values.update(overrides)
    return PlanRequest(**values)


def full_step(**overrides: Any) -> PlanStepRecord:
    """A step with every field, including both optional ones, populated."""
    values: dict[str, Any] = {
        "index": 2,
        "action": ActionName.MOVEMENT_MOVE_TO,
        "status": ActionStatus.SUCCEEDED,
        "reason_code": ReasonCode.POSTCONDITION_MET,
        "action_id": "a-31",
    }
    values.update(overrides)
    return PlanStepRecord(**values)


def bare_step(**overrides: Any) -> PlanStepRecord:
    """A step with both optional fields left out — one that has not run yet."""
    values: dict[str, Any] = {
        "index": 0,
        "action": ActionName.WORLD_INSPECT,
        "status": ActionStatus.ACCEPTED,
    }
    values.update(overrides)
    return PlanStepRecord(**values)


def full_record(**overrides: Any) -> PlanRecord:
    values: dict[str, Any] = {
        "plan_id": "p-7f1c9a2b",
        "status": ActionStatus.PROGRESS,
        "step_index": 1,
        "steps": (bare_step(), full_step()),
        "stopped_reason": ReasonCode.LEASE_EXPIRED,
    }
    values.update(overrides)
    return PlanRecord(**values)


def assert_request_round_trip(record: PlanRequest) -> None:
    decoded = decode_plan_request(encode_plan_request(record))
    assert decoded == record
    # A StrEnum member equals its own value, so equality above would also hold
    # for a decoder that returned the raw string.
    assert isinstance(decoded.mode, SessionMode)


def assert_step_round_trip(record: PlanStepRecord) -> None:
    decoded = decode_plan_step(encode_plan_step(record))
    assert decoded == record
    assert isinstance(decoded.action, ActionName)
    assert isinstance(decoded.status, ActionStatus)
    if record.reason_code is None:
        assert decoded.reason_code is None
    else:
        assert isinstance(decoded.reason_code, ReasonCode)


def assert_record_round_trip(record: PlanRecord) -> None:
    decoded = decode_plan_record(encode_plan_record(record))
    assert decoded == record
    assert isinstance(decoded.status, ActionStatus)
    assert isinstance(decoded.steps, tuple)
    for step in decoded.steps:
        assert isinstance(step, PlanStepRecord)
        assert isinstance(step.action, ActionName)
        assert isinstance(step.status, ActionStatus)
    if record.stopped_reason is None:
        assert decoded.stopped_reason is None
    else:
        assert isinstance(decoded.stopped_reason, ReasonCode)


# -- the parametrisation itself is pinned ---------------------------------


@pytest.mark.parametrize(
    ("record_type", "required", "optional"),
    [
        (PlanRequest, REQUEST_REQUIRED, REQUEST_OPTIONAL),
        (PlanStepRecord, STEP_REQUIRED, STEP_OPTIONAL),
        (PlanRecord, RECORD_REQUIRED, RECORD_OPTIONAL),
    ],
)
def test_every_field_is_classified_as_required_or_optional(
    record_type: type, required: tuple[str, ...], optional: tuple[str, ...]
) -> None:
    """A field added to a record must land in one of the lists above.

    Otherwise the strictness tests would keep passing while quietly not
    covering it.
    """
    declared = {f.name for f in fields(record_type)}
    assert declared == set(required) | set(optional)
    # Optional means "declared ``| None``", not "has a default": ``steps`` has a
    # default and is still required on the wire.
    optional_by_type = {f.name for f in fields(record_type) if "None" in str(f.type)}
    assert optional_by_type == set(optional)


def test_encode_emits_exactly_the_record_fields() -> None:
    """No field dropped on the way out, and none invented."""
    assert set(encode_plan_request(full_request())) == {f.name for f in fields(PlanRequest)}
    assert set(encode_plan_step(full_step())) == {f.name for f in fields(PlanStepRecord)}
    assert set(encode_plan_record(full_record())) == {f.name for f in fields(PlanRecord)}


def test_encode_omits_absent_optionals_rather_than_writing_null() -> None:
    assert set(encode_plan_step(bare_step())) == set(STEP_REQUIRED)
    assert set(encode_plan_record(full_record(stopped_reason=None))) == set(RECORD_REQUIRED)


# -- round trips ----------------------------------------------------------


def test_request_round_trip() -> None:
    assert_request_round_trip(full_request())


@pytest.mark.parametrize("mode", list(SessionMode))
def test_request_round_trip_covers_every_mode(mode: SessionMode) -> None:
    assert_request_round_trip(full_request(mode=mode))


@pytest.mark.parametrize(("max_steps", "max_real_seconds"), [(1, 1), (6, 120), (12, 900)])
def test_request_limits_are_not_transposed(max_steps: int, max_real_seconds: int) -> None:
    """Two integers side by side is exactly where a key swap hides."""
    record = full_request(max_steps=max_steps, max_real_seconds=max_real_seconds)
    decoded = decode_plan_request(encode_plan_request(record))
    assert decoded == record
    assert decoded.max_steps == max_steps
    assert decoded.max_real_seconds == max_real_seconds


def test_request_limits_are_distinguished_when_only_one_differs() -> None:
    """Equal limits round-trip under a codec that wrote one key twice."""
    decoded = decode_plan_request(
        encode_plan_request(full_request(max_steps=3, max_real_seconds=3))
    )
    assert decoded.max_steps == 3
    swapped = decode_plan_request(
        encode_plan_request(full_request(max_steps=3, max_real_seconds=999))
    )
    assert (swapped.max_steps, swapped.max_real_seconds) == (3, 999)


def test_step_round_trip_with_every_field_present() -> None:
    assert_step_round_trip(full_step())


def test_step_round_trip_with_every_optional_field_absent() -> None:
    assert_step_round_trip(bare_step())


@pytest.mark.parametrize("field_name", STEP_OPTIONAL)
def test_step_round_trip_with_one_optional_field_absent(field_name: str) -> None:
    """Each optional field, alone, absent — the rest populated."""
    assert_step_round_trip(full_step(**{field_name: None}))


@pytest.mark.parametrize("field_name", STEP_OPTIONAL)
def test_step_decodes_when_an_optional_key_is_omitted_entirely(field_name: str) -> None:
    record = full_step(**{field_name: None})
    payload = encode_plan_step(record)
    payload.pop(field_name, None)
    assert decode_plan_step(payload) == record


@pytest.mark.parametrize("field_name", STEP_OPTIONAL)
def test_step_decodes_when_an_optional_key_is_an_explicit_null(field_name: str) -> None:
    """A peer that writes the null rather than omitting it says the same thing."""
    record = full_step(**{field_name: None})
    payload = encode_plan_step(full_step())
    payload[field_name] = None
    assert decode_plan_step(payload) == record


@pytest.mark.parametrize("status", list(ActionStatus))
def test_step_round_trip_covers_every_status(status: ActionStatus) -> None:
    assert_step_round_trip(full_step(status=status))


@pytest.mark.parametrize("action", list(ActionName))
def test_step_round_trip_covers_every_action_name(action: ActionName) -> None:
    assert_step_round_trip(full_step(action=action))


@pytest.mark.parametrize("reason", list(ReasonCode))
def test_step_round_trip_covers_every_reason_code(reason: ReasonCode) -> None:
    assert_step_round_trip(full_step(reason_code=reason))


def test_record_round_trip_with_every_field_present() -> None:
    assert_record_round_trip(full_record())


def test_record_round_trip_with_the_optional_field_absent() -> None:
    assert_record_round_trip(full_record(stopped_reason=None))


def test_record_decodes_when_the_optional_key_is_omitted_entirely() -> None:
    record = full_record(stopped_reason=None)
    payload = encode_plan_record(full_record())
    del payload["stopped_reason"]
    assert decode_plan_record(payload) == record


@pytest.mark.parametrize("status", list(ActionStatus))
def test_record_round_trip_covers_every_status(status: ActionStatus) -> None:
    assert_record_round_trip(full_record(status=status))


@pytest.mark.parametrize("reason", list(ReasonCode))
def test_record_round_trip_covers_every_stopped_reason(reason: ReasonCode) -> None:
    assert_record_round_trip(full_record(stopped_reason=reason))


@pytest.mark.parametrize("step_index", [0, 1, 5, 41])
def test_record_round_trip_covers_step_index(step_index: int) -> None:
    assert_record_round_trip(full_record(step_index=step_index))


# -- the nested step list -------------------------------------------------


def test_empty_steps_round_trips() -> None:
    """A plan refused before its first step has no steps; that is an answer."""
    record = full_record(steps=())
    payload = encode_plan_record(record)
    assert payload["steps"] == []
    decoded = decode_plan_record(payload)
    assert decoded == record
    assert decoded.steps == ()
    assert isinstance(decoded.steps, tuple)


def test_steps_decode_to_a_tuple_not_a_list() -> None:
    """The record declares a tuple, and equality would not notice a list.

    ``(a, b) == [a, b]`` is ``False`` for a dataclass field, but the record is
    frozen and a list in that slot would make it mutable through its own field,
    so the type is asserted rather than inferred.
    """
    decoded = decode_plan_record(encode_plan_record(full_record()))
    assert type(decoded.steps) is tuple


def test_step_order_is_preserved_and_nothing_is_sorted_or_deduplicated() -> None:
    """Deliberately non-sorted indices, with one repeated.

    A codec that sorted by ``index``, or that de-duplicated, would pass every
    single-step test above and fail exactly here.
    """
    steps = (
        full_step(index=3, action=ActionName.WORLD_INSPECT, status=ActionStatus.FAILED),
        bare_step(index=0),
        full_step(index=2, action=ActionName.MOVEMENT_MOVE_NEAR),
        bare_step(index=1, status=ActionStatus.STARTED),
        full_step(index=2, action=ActionName.MOVEMENT_MOVE_NEAR),
    )
    record = full_record(steps=steps, step_index=4)
    decoded = decode_plan_record(encode_plan_record(record))
    assert decoded == record
    assert decoded.steps == steps
    assert [step.index for step in decoded.steps] == [3, 0, 2, 1, 2]
    assert len(decoded.steps) == 5


def test_a_long_step_list_keeps_every_element_distinct() -> None:
    actions = list(ActionName)
    steps = tuple(
        full_step(index=position, action=actions[position % len(actions)])
        for position in range(len(actions))
    )
    record = full_record(steps=steps, step_index=0)
    decoded = decode_plan_record(encode_plan_record(record))
    assert decoded.steps == steps


def test_steps_are_encoded_as_a_json_array_of_objects() -> None:
    payload = encode_plan_record(full_record())
    assert isinstance(payload["steps"], list)
    assert all(isinstance(step, dict) for step in payload["steps"])


def test_a_step_inside_a_record_is_as_strict_as_a_lone_step() -> None:
    """The nested path must not be a second, laxer decoder."""
    payload = encode_plan_record(full_record())
    del payload["steps"][1]["action"]
    with pytest.raises(CodecError) as excinfo:
        decode_plan_record(payload)
    assert "action" in str(excinfo.value)
    # The position is structural — derived from the array, not from the payload
    # — so naming it leaks nothing and tells a reader which step failed.
    assert "steps[1]" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["", "steps", 3, 3.5, True, None, {"index": 0}])
def test_steps_that_are_not_an_array_are_refused(bad: Any) -> None:
    payload = encode_plan_record(full_record())
    payload["steps"] = bad
    with pytest.raises(CodecError) as excinfo:
        decode_plan_record(payload)
    assert "steps" in str(excinfo.value)


@pytest.mark.parametrize("bad", [3, "step", None, ["index"], True])
def test_a_step_that_is_not_an_object_is_refused(bad: Any) -> None:
    payload = encode_plan_record(full_record())
    payload["steps"] = [encode_plan_step(bare_step()), bad]
    with pytest.raises(CodecError) as excinfo:
        decode_plan_record(payload)
    assert "steps[1]" in str(excinfo.value)


def test_missing_steps_is_refused_not_defaulted_to_empty() -> None:
    """``steps`` defaults to ``()`` on the dataclass and is still required here.

    A decoder that defaulted it would publish ``step_count: 0`` for a plan that
    had run every step it had.
    """
    payload = encode_plan_record(full_record())
    del payload["steps"]
    with pytest.raises(CodecError) as excinfo:
        decode_plan_record(payload)
    assert "steps" in str(excinfo.value)


# -- enums travel as values -----------------------------------------------


def test_enums_are_encoded_as_their_value_not_their_name_or_ordinal() -> None:
    step = encode_plan_step(full_step(status=ActionStatus.SUCCEEDED))
    assert step["status"] == "succeeded"
    assert step["status"] != "SUCCEEDED"
    assert step["action"] == "movement.move_to"
    assert step["reason_code"] == "POSTCONDITION_MET"
    assert not isinstance(step["status"], int)
    assert not isinstance(step["action"], int)

    record = encode_plan_record(full_record())
    assert record["status"] == "progress"
    assert record["stopped_reason"] == "LEASE_EXPIRED"
    assert encode_plan_request(full_request())["mode"] == "ASSISTED"


def test_decoding_a_status_by_name_is_refused() -> None:
    """``ActionStatus.SUCCEEDED.name`` is ``"SUCCEEDED"``; its value is ``"succeeded"``.

    A decoder reaching for the name would accept this payload; one reading the
    value refuses it.
    """
    payload = encode_plan_step(full_step())
    payload["status"] = "SUCCEEDED"
    with pytest.raises(CodecError):
        decode_plan_step(payload)

    record = encode_plan_record(full_record())
    record["status"] = "PROGRESS"
    with pytest.raises(CodecError):
        decode_plan_record(record)


def test_decoding_an_action_name_by_member_name_is_refused() -> None:
    payload = encode_plan_step(full_step())
    payload["action"] = "MOVEMENT_MOVE_TO"
    with pytest.raises(CodecError):
        decode_plan_step(payload)


@pytest.mark.parametrize("field_name", ["status", "action"])
def test_decoding_a_step_enum_by_ordinal_is_refused(field_name: str) -> None:
    payload = encode_plan_step(full_step())
    payload[field_name] = 1
    with pytest.raises(CodecError):
        decode_plan_step(payload)


def test_unknown_enum_value_is_refused_not_defaulted() -> None:
    for field_name, bogus in (
        ("status", "half_done"),
        ("action", "shell.exec"),
        ("reason_code", "VIBES_OFF"),
    ):
        payload = encode_plan_step(full_step())
        payload[field_name] = bogus
        with pytest.raises(CodecError):
            decode_plan_step(payload)


def test_an_unknown_optional_enum_is_refused_rather_than_read_as_absent() -> None:
    """The dangerous failure for an optional enum is ``None``, not an exception.

    A decoder that swallowed an unreadable ``reason_code`` would report a step
    that failed for no stated reason, and a plan that stopped for none.
    """
    payload = encode_plan_step(full_step())
    payload["reason_code"] = "NOT_A_REASON"
    with pytest.raises(CodecError):
        decode_plan_step(payload)

    record = encode_plan_record(full_record())
    record["stopped_reason"] = "NOT_A_REASON"
    with pytest.raises(CodecError):
        decode_plan_record(record)


def test_an_unknown_mode_is_refused() -> None:
    payload = encode_plan_request(full_request())
    payload["mode"] = "GOD_MODE"
    with pytest.raises(CodecError):
        decode_plan_request(payload)


# -- it has to survive a process boundary ---------------------------------


def test_encoded_request_is_json_serialisable() -> None:
    payload = encode_plan_request(full_request())
    assert json.loads(json.dumps(payload)) == payload


@pytest.mark.parametrize("record", [full_step(), bare_step()])
def test_encoded_step_is_json_serialisable(record: PlanStepRecord) -> None:
    payload = encode_plan_step(record)
    assert json.loads(json.dumps(payload)) == payload


@pytest.mark.parametrize(
    "record", [full_record(), full_record(steps=()), full_record(stopped_reason=None)]
)
def test_encoded_record_is_json_serialisable(record: PlanRecord) -> None:
    payload = encode_plan_record(record)
    assert json.loads(json.dumps(payload)) == payload


def test_encoded_current_bodies_are_json_serialisable() -> None:
    for body in (encode_plan_current(None), encode_plan_current(full_record())):
        assert json.loads(json.dumps(body)) == body


def test_records_survive_an_actual_json_round_trip() -> None:
    request = full_request()
    assert decode_plan_request(json.loads(json.dumps(encode_plan_request(request)))) == request
    record = full_record()
    decoded = decode_plan_record(json.loads(json.dumps(encode_plan_record(record))))
    assert decoded == record
    assert isinstance(decoded.steps, tuple)


# -- Cyrillic --------------------------------------------------------------


def test_cyrillic_text_survives_the_round_trip() -> None:
    request = full_request(goal=CYRILLIC_GOAL, idempotency_key="цель-1:попытка-2")
    payload = encode_plan_request(request)
    assert payload["goal"] == CYRILLIC_GOAL
    assert decode_plan_request(json.loads(json.dumps(payload))) == request

    record = full_record(
        plan_id="план-7",
        steps=(full_step(action_id="действие-3"),),
    )
    decoded = decode_plan_record(json.loads(json.dumps(encode_plan_record(record))))
    assert decoded == record
    assert decoded.steps[0].action_id == "действие-3"


def test_cyrillic_survives_ascii_escaped_json() -> None:
    """An encoder elsewhere in the stack may set ``ensure_ascii``; escaping is
    not allowed to change the string."""
    request = full_request(goal=CYRILLIC_GOAL)
    text = json.dumps(encode_plan_request(request), ensure_ascii=True)
    assert decode_plan_request(json.loads(text)).goal == CYRILLIC_GOAL


# -- strictness: nothing is defaulted -------------------------------------


@pytest.mark.parametrize("field_name", REQUEST_REQUIRED)
def test_request_missing_required_field_is_refused(field_name: str) -> None:
    payload = encode_plan_request(full_request())
    del payload[field_name]
    with pytest.raises(CodecError) as excinfo:
        decode_plan_request(payload)
    assert field_name in str(excinfo.value)


@pytest.mark.parametrize("field_name", STEP_REQUIRED)
def test_step_missing_required_field_is_refused(field_name: str) -> None:
    payload = encode_plan_step(full_step())
    del payload[field_name]
    with pytest.raises(CodecError) as excinfo:
        decode_plan_step(payload)
    assert field_name in str(excinfo.value)


@pytest.mark.parametrize("field_name", RECORD_REQUIRED)
def test_record_missing_required_field_is_refused(field_name: str) -> None:
    payload = encode_plan_record(full_record())
    del payload[field_name]
    with pytest.raises(CodecError) as excinfo:
        decode_plan_record(payload)
    assert field_name in str(excinfo.value)


@pytest.mark.parametrize("field_name", REQUEST_REQUIRED)
def test_request_required_field_explicitly_null_is_refused(field_name: str) -> None:
    """``null`` is not "absent, so use the default" either."""
    payload = encode_plan_request(full_request())
    payload[field_name] = None
    with pytest.raises(CodecError):
        decode_plan_request(payload)


@pytest.mark.parametrize("field_name", STEP_REQUIRED)
def test_step_required_field_explicitly_null_is_refused(field_name: str) -> None:
    payload = encode_plan_step(full_step())
    payload[field_name] = None
    with pytest.raises(CodecError):
        decode_plan_step(payload)


@pytest.mark.parametrize("field_name", RECORD_REQUIRED)
def test_record_required_field_explicitly_null_is_refused(field_name: str) -> None:
    payload = encode_plan_record(full_record())
    payload[field_name] = None
    with pytest.raises(CodecError):
        decode_plan_record(payload)


def test_an_empty_payload_is_refused() -> None:
    with pytest.raises(CodecError):
        decode_plan_request({})
    with pytest.raises(CodecError):
        decode_plan_step({})
    with pytest.raises(CodecError):
        decode_plan_record({})


# -- strictness: types are not coerced ------------------------------------


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("goal", 7),
        ("goal", ["eat"]),
        ("goal", True),
        ("mode", 1),
        ("mode", True),
        ("max_steps", "6"),
        ("max_steps", 6.0),
        # `bool` is an `int` in Python, and a required integer given `True`
        # would otherwise decode as a one-step plan.
        ("max_steps", True),
        ("max_real_seconds", "120"),
        ("max_real_seconds", 120.5),
        ("max_real_seconds", False),
        ("idempotency_key", 1),
        ("idempotency_key", {"key": "goal-1"}),
    ],
)
def test_request_type_confusion_is_refused(field_name: str, value: Any) -> None:
    payload = encode_plan_request(full_request())
    payload[field_name] = value
    with pytest.raises(CodecError) as excinfo:
        decode_plan_request(payload)
    assert field_name in str(excinfo.value)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("index", "2"),
        ("index", 2.0),
        ("index", True),
        ("action", 4),
        ("status", 5),
        ("reason_code", 7),
        ("action_id", 31),
        ("action_id", ["a-31"]),
    ],
)
def test_step_type_confusion_is_refused(field_name: str, value: Any) -> None:
    payload = encode_plan_step(full_step())
    payload[field_name] = value
    with pytest.raises(CodecError):
        decode_plan_step(payload)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("plan_id", 7),
        ("plan_id", ["p-1"]),
        ("plan_id", True),
        ("status", 3),
        ("step_index", "1"),
        ("step_index", 1.0),
        ("step_index", True),
        ("stopped_reason", 9),
    ],
)
def test_record_type_confusion_is_refused(field_name: str, value: Any) -> None:
    payload = encode_plan_record(full_record())
    payload[field_name] = value
    with pytest.raises(CodecError):
        decode_plan_record(payload)


def test_a_bool_is_never_read_as_an_integer() -> None:
    """No record in this group carries a boolean, which is itself the guard.

    The confusion that can happen here runs the other way: ``True`` arriving in
    an integer field. Read as ``1`` it would be a plan one step long, or a plan
    sitting on step one — both plausible enough to survive a review.
    """
    request_payload = encode_plan_request(full_request())
    request_payload["max_steps"] = True
    with pytest.raises(CodecError):
        decode_plan_request(request_payload)

    step_payload = encode_plan_step(full_step())
    step_payload["index"] = True
    with pytest.raises(CodecError):
        decode_plan_step(step_payload)

    record_payload = encode_plan_record(full_record())
    record_payload["step_index"] = True
    with pytest.raises(CodecError):
        decode_plan_record(record_payload)

    # And inside the nested list, where the guard is easiest to lose.
    nested = encode_plan_record(full_record())
    nested["steps"][0]["index"] = True
    with pytest.raises(CodecError):
        decode_plan_record(nested)


# -- the record's own invariant survives decoding -------------------------


def test_an_empty_plan_id_does_not_produce_a_record() -> None:
    """``PlanRecord`` refuses an empty id; decoding must not route around it."""
    payload = encode_plan_record(full_record())
    payload["plan_id"] = ""
    with pytest.raises(CodecError) as excinfo:
        decode_plan_record(payload)
    assert "plan_id" in str(excinfo.value)


@pytest.mark.parametrize("step_index", [-1, -99])
def test_a_negative_step_index_does_not_produce_a_record(step_index: int) -> None:
    payload = encode_plan_record(full_record())
    payload["step_index"] = step_index
    with pytest.raises(CodecError) as excinfo:
        decode_plan_record(payload)
    assert "step_index" in str(excinfo.value)
    # The record's own message quotes the number ("got -1"); this one must not,
    # and chaining it would have put it in the traceback anyway.
    assert str(step_index) not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_a_zero_step_index_is_a_real_answer_not_a_refusal() -> None:
    """A plan that has not passed its first step is a plan, not a failure."""
    record = full_record(step_index=0)
    assert decode_plan_record(encode_plan_record(record)) == record


def test_a_negative_step_index_is_refused_by_the_codec_error_type() -> None:
    """A bare ``ValueError`` escaping here would not be a decode failure the
    router can turn into ``MALFORMED``."""
    payload = encode_plan_record(full_record())
    payload["step_index"] = -4
    with pytest.raises(CodecError):
        decode_plan_record(payload)


def test_a_negative_step_index_on_a_nested_step_is_permitted_by_the_record() -> None:
    """``PlanStepRecord`` has no invariant, and this codec does not invent one.

    Pinned so that adding a bound here would be a deliberate change to the
    record rather than a quiet one to its codec.
    """
    record = full_record(steps=(full_step(index=-1),), step_index=0)
    assert decode_plan_record(encode_plan_record(record)) == record


# -- plan.current: absence is an answer -----------------------------------


def test_no_plan_encodes_to_an_empty_body_and_decodes_back_to_none() -> None:
    body = encode_plan_current(None)
    assert body == {}
    assert decode_plan_current(body) is None


def test_a_plan_round_trips_through_the_current_body() -> None:
    record = full_record()
    decoded = decode_plan_current(encode_plan_current(record))
    assert decoded == record
    assert decoded is not None
    assert isinstance(decoded.steps, tuple)


def test_a_malformed_plan_in_the_current_body_is_an_error_not_an_absence() -> None:
    body = encode_plan_current(full_record())
    del body["plan"]["status"]
    with pytest.raises(CodecError):
        decode_plan_current(body)


def test_a_plan_key_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(CodecError) as excinfo:
        decode_plan_current({"plan": ["p-1"]})
    assert "plan" in str(excinfo.value)


# -- messages name the field and the reason, never the value --------------


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        # A string field is rejected by wrapping the secret in a list — the
        # secret itself is a perfectly valid string and would be accepted.
        ("goal", [SECRET]),
        ("idempotency_key", [SECRET]),
        # A non-string field is rejected by the secret itself.
        ("max_steps", SECRET),
        ("max_real_seconds", SECRET),
    ],
)
def test_no_request_error_message_repeats_the_value_it_rejected(
    field_name: str, value: Any
) -> None:
    """The rejected value is put in the payload as a secret-shaped string.

    These strings reach a traceback, a log line and a bug report before any
    redactor sees them.
    """
    payload = encode_plan_request(full_request())
    payload[field_name] = value
    with pytest.raises(CodecError) as excinfo:
        decode_plan_request(payload)
    assert SECRET not in str(excinfo.value)
    assert SECRET not in repr(excinfo.value)
    assert field_name in str(excinfo.value)


@pytest.mark.parametrize(("field_name", "value"), [("index", SECRET), ("action_id", [SECRET])])
def test_no_step_error_message_repeats_the_value_it_rejected(field_name: str, value: Any) -> None:
    payload = encode_plan_step(full_step())
    payload[field_name] = value
    with pytest.raises(CodecError) as excinfo:
        decode_plan_step(payload)
    assert SECRET not in str(excinfo.value)
    assert SECRET not in repr(excinfo.value)
    assert field_name in str(excinfo.value)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [("plan_id", [SECRET]), ("step_index", SECRET), ("steps", SECRET)],
)
def test_no_record_error_message_repeats_the_value_it_rejected(field_name: str, value: Any) -> None:
    payload = encode_plan_record(full_record())
    payload[field_name] = value
    with pytest.raises(CodecError) as excinfo:
        decode_plan_record(payload)
    assert SECRET not in str(excinfo.value)
    assert SECRET not in repr(excinfo.value)
    assert field_name in str(excinfo.value)


def test_a_secret_shaped_goal_reaches_no_message_even_when_a_sibling_fails() -> None:
    """The goal is free text and the likeliest thing in a payload worth hiding.

    Here it is perfectly valid; the failure is elsewhere, and the message must
    still not quote the payload it was reading.
    """
    payload = encode_plan_request(full_request(goal=SECRET))
    del payload["max_steps"]
    with pytest.raises(CodecError) as excinfo:
        decode_plan_request(payload)
    assert SECRET not in str(excinfo.value)


def test_a_valid_payload_carrying_a_secret_shaped_string_is_not_an_error_at_all() -> None:
    """The rule is about *messages*, not about refusing odd-looking data.

    A user who says something token-shaped has still stated a goal.
    """
    request = full_request(goal=SECRET)
    assert decode_plan_request(encode_plan_request(request)).goal == SECRET


def test_the_enum_carve_out_is_the_only_place_a_value_is_named() -> None:
    """``require_enum`` names the value it rejected, deliberately.

    Its members are closed protocol identifiers, not user data. This test pins
    that the carve-out exists so nobody routes a field carrying user data
    through ``require_enum`` on the assumption that nothing is echoed.
    """
    payload = encode_plan_step(full_step())
    payload["status"] = "NOT_A_STATUS"
    with pytest.raises(CodecError) as excinfo:
        decode_plan_step(payload)
    assert "status" in str(excinfo.value)
    assert "NOT_A_STATUS" in str(excinfo.value)


# -- the goal is carried, not capped, and not scrubbed --------------------


def test_the_goal_crosses_verbatim_and_is_not_truncated_or_scrubbed_here() -> None:
    """The cap lives at the tool boundary that admitted the text (§ module doc).

    Pinned so that adding one here would be a deliberate protocol change rather
    than a quiet difference between the two halves of the link.
    """
    long_goal = "поешь " * 400
    request = full_request(goal=long_goal)
    payload = encode_plan_request(request)
    assert payload["goal"] == long_goal
    assert decode_plan_request(payload).goal == long_goal
