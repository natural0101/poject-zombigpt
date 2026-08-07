"""The action codec: the request out, the record back, and the honesty rule.

Round trips assert equality of the *record*, never the shape of the JSON. A
field that both halves forget survives any assertion about the document and
fails an assertion about the object, which is the only reason these tests can
catch a codec pair drifting together.

The tests that matter most are the ones about ``succeeded``. A record claiming
success without the terminal result, without ``POSTCONDITION_MET``, or without
observed evidence must be refused at the boundary rather than decoded, because
the whole point of the link is that the process reporting success is not the
process that watched the world.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from pz_agent_core.actions import ActionRequest
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    CommandPolicy,
    JsonDict,
    ReasonCode,
    RiskClass,
)
from pz_agent_core.protocol.messages import MAX_LEASE_MS, MIN_LEASE_MS
from pz_agent_mcp.ports import ActionRecord
from pz_agent_mcp.remote.codec import CodecError
from pz_agent_mcp.remote.codec.actions import (
    decode_action_record,
    decode_action_request,
    decode_action_status,
    encode_action_record,
    encode_action_request,
    encode_action_status,
)

SESSION_ID = str(uuid.UUID(int=0x5E5510))
COMMAND_ID = str(uuid.UUID(int=0xC0DE))

#: Recognisable, secret-shaped, and never legitimately part of an error text.
SECRET = "sk-live-ZOMBI-9f8e7d6c5b4a3210-DO-NOT-LOG"

#: The install this is built for is Russian, so every string field on this link
#: carries Cyrillic at some point.
CYRILLIC_LABEL = "консервированные бобы"


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------


def succeeded_result(
    *,
    evidence: JsonDict | None = None,
    message: str = "",
) -> ActionResult:
    """A terminal success ack, built through the constructor that demands proof."""
    return ActionResult.succeeded(
        session_id=SESSION_ID,
        seq=7,
        command_id=COMMAND_ID,
        action=ActionName.CONSUME_EAT.value,
        timestamp_ms=1_700_000_000_000,
        evidence={"hunger_delta": -0.42} if evidence is None else evidence,
        started_at_ms=1_699_999_999_000,
        attempt=2,
        message=message,
    )


def failure_result(
    *,
    reason_code: ReasonCode = ReasonCode.POSTCONDITION_FAILED,
    status: ActionStatus = ActionStatus.FAILED,
    diagnostics: tuple[str, ...] = (),
    message: str = "",
) -> ActionResult:
    return ActionResult.failure(
        session_id=SESSION_ID,
        seq=8,
        command_id=COMMAND_ID,
        action=ActionName.CONSUME_EAT.value,
        timestamp_ms=1_700_000_000_500,
        reason_code=reason_code,
        status=status,
        message=message,
        diagnostics=diagnostics,
    )


def valid_request() -> ActionRequest:
    return ActionRequest(
        action=ActionName.CONSUME_EAT,
        session_id=SESSION_ID,
        idempotency_key="eat-1",
        args={"item_ref": "item:9001"},
        policy=CommandPolicy(allow_interrupt=False, max_retries=2, risk_permission=RiskClass.P2),
        lease_ms=20_000,
    )


def valid_record() -> ActionRecord:
    return ActionRecord(
        action_id="act-1",
        action=ActionName.CONSUME_EAT,
        status=ActionStatus.SUCCEEDED,
        idempotency_key="eat-1",
        progress=1.0,
        result=succeeded_result(),
    )


def succeeded_payload(result: JsonDict | None) -> JsonDict:
    """A record payload claiming success, with *result* attached verbatim."""
    payload: JsonDict = {
        "action_id": "act-1",
        "action": ActionName.CONSUME_EAT.value,
        "status": ActionStatus.SUCCEEDED.value,
        "idempotency_key": "eat-1",
    }
    if result is not None:
        payload["result"] = result
    return payload


REQUESTS: tuple[ActionRequest, ...] = (
    # Everything non-default, including the optional risk permission.
    valid_request(),
    # Every dataclass default taken explicitly: empty args, default policy.
    ActionRequest(
        action=ActionName.WORLD_INSPECT,
        session_id=SESSION_ID,
        idempotency_key="k",
        args={},
        policy=CommandPolicy(),
        lease_ms=15_000,
    ),
    # The bounds themselves, and a policy that forbids interrupting.
    ActionRequest(
        action=ActionName.SAFETY_STOP,
        session_id=SESSION_ID,
        idempotency_key="x" * 120,
        args={"nested": {"list": [1, 2.5, True, None, CYRILLIC_LABEL]}},
        policy=CommandPolicy(allow_interrupt=False, max_retries=0, risk_permission=RiskClass.P0),
        lease_ms=MIN_LEASE_MS,
    ),
    ActionRequest(
        action=ActionName.MOVEMENT_MOVE_TO,
        session_id=SESSION_ID,
        idempotency_key=CYRILLIC_LABEL,
        args={"цель": {"x": 1.5, "y": -2.0, "z": 0}},
        policy=CommandPolicy(allow_interrupt=True, max_retries=3, risk_permission=RiskClass.P4),
        lease_ms=MAX_LEASE_MS,
    ),
)

RECORDS: tuple[ActionRecord, ...] = (
    # Both optionals present.
    valid_record(),
    # Both optionals absent.
    ActionRecord(
        action_id="act-2",
        action=ActionName.INVENTORY_SEARCH,
        status=ActionStatus.ACCEPTED,
        idempotency_key="search-1",
    ),
    # progress present, result absent — the in-flight shape.
    ActionRecord(
        action_id="act-3",
        action=ActionName.MOVEMENT_MOVE_TO,
        status=ActionStatus.PROGRESS,
        idempotency_key="move-1",
        progress=0.0,
    ),
    ActionRecord(
        action_id="act-4",
        action=ActionName.MOVEMENT_MOVE_TO,
        status=ActionStatus.STARTED,
        idempotency_key="move-2",
        progress=0.5,
    ),
    # result present, progress absent — a terminal failure with diagnostics.
    ActionRecord(
        action_id="act-5",
        action=ActionName.CONSUME_EAT,
        status=ActionStatus.FAILED,
        idempotency_key="eat-2",
        result=failure_result(
            diagnostics=("нет безопасной еды", "inventory scanned"),
            message=CYRILLIC_LABEL,
        ),
    ),
    # A non-success terminal state that is not a failure.
    ActionRecord(
        action_id="act-6",
        action=ActionName.SURVIVAL_SLEEP,
        status=ActionStatus.CANCELLED,
        idempotency_key="sleep-1",
        progress=0.25,
        result=failure_result(
            reason_code=ReasonCode.PANIC_STOP,
            status=ActionStatus.CANCELLED,
        ),
    ),
    # Success carrying Cyrillic evidence and a Cyrillic message.
    ActionRecord(
        action_id="act-7",
        action=ActionName.LITERATURE_READ,
        status=ActionStatus.SUCCEEDED,
        idempotency_key="читать-1",
        progress=1.0,
        result=succeeded_result(
            evidence={"название": CYRILLIC_LABEL, "уровень": 3},
            message="постусловие подтверждено",
        ),
    ),
)


# --------------------------------------------------------------------------
# round trips — equality of the record
# --------------------------------------------------------------------------


@pytest.mark.parametrize("request_", REQUESTS, ids=lambda r: r.action.value)
def test_request_round_trip_preserves_the_record(request_: ActionRequest) -> None:
    assert decode_action_request(encode_action_request(request_)) == request_


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r.action_id)
def test_record_round_trip_preserves_the_record(record: ActionRecord) -> None:
    assert decode_action_record(encode_action_record(record)) == record


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r.action_id)
def test_status_round_trip_preserves_the_record(record: ActionRecord) -> None:
    assert decode_action_status(encode_action_status(record)) == record


def test_absent_record_round_trips_as_absent() -> None:
    """An id the core has never seen is "no record", not a malformed answer."""
    assert encode_action_status(None) == {}
    assert decode_action_status(encode_action_status(None)) is None


def test_request_round_trip_covers_every_field() -> None:
    """Field by field, so a dropped one cannot hide behind a passing equality."""
    original = valid_request()
    decoded = decode_action_request(encode_action_request(original))
    assert decoded.action is original.action
    assert decoded.session_id == original.session_id
    assert decoded.idempotency_key == original.idempotency_key
    assert decoded.args == original.args
    assert decoded.policy == original.policy
    assert decoded.policy.risk_permission is RiskClass.P2
    assert decoded.policy.allow_interrupt is False
    assert decoded.policy.max_retries == 2
    assert decoded.lease_ms == original.lease_ms


def test_record_round_trip_covers_every_field() -> None:
    original = valid_record()
    decoded = decode_action_record(encode_action_record(original))
    assert decoded.action_id == original.action_id
    assert decoded.action is original.action
    assert decoded.status is original.status
    assert decoded.idempotency_key == original.idempotency_key
    assert decoded.progress == original.progress
    assert decoded.result == original.result
    assert decoded.result is not None
    assert decoded.result.evidence == {"hunger_delta": -0.42}


def test_optional_fields_are_omitted_rather_than_nulled() -> None:
    encoded = encode_action_record(
        ActionRecord(
            action_id="act-2",
            action=ActionName.INVENTORY_SEARCH,
            status=ActionStatus.ACCEPTED,
            idempotency_key="search-1",
        )
    )
    assert "progress" not in encoded
    assert "result" not in encoded


def test_explicit_nulls_decode_as_absent() -> None:
    """A peer that spells "nothing" as null means the same thing as omitting it."""
    payload = encode_action_record(
        ActionRecord(
            action_id="act-2",
            action=ActionName.INVENTORY_SEARCH,
            status=ActionStatus.ACCEPTED,
            idempotency_key="search-1",
        )
    )
    payload["progress"] = None
    payload["result"] = None
    decoded = decode_action_record(payload)
    assert decoded.progress is None
    assert decoded.result is None


# --------------------------------------------------------------------------
# it has to survive a pipe
# --------------------------------------------------------------------------


@pytest.mark.parametrize("request_", REQUESTS, ids=lambda r: r.action.value)
def test_encoded_request_is_json_serialisable(request_: ActionRequest) -> None:
    encoded = encode_action_request(request_)
    assert json.loads(json.dumps(encoded)) == encoded


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r.action_id)
def test_encoded_record_is_json_serialisable(record: ActionRecord) -> None:
    encoded = encode_action_record(record)
    assert json.loads(json.dumps(encoded)) == encoded
    status_body = encode_action_status(record)
    assert json.loads(json.dumps(status_body)) == status_body


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r.action_id)
def test_record_survives_an_actual_json_hop(record: ActionRecord) -> None:
    """Encode, serialise, parse, decode — the trip the transport really makes."""
    assert decode_action_record(json.loads(json.dumps(encode_action_record(record)))) == record


@pytest.mark.parametrize("request_", REQUESTS, ids=lambda r: r.action.value)
def test_request_survives_an_actual_json_hop(request_: ActionRequest) -> None:
    assert (
        decode_action_request(json.loads(json.dumps(encode_action_request(request_)))) == request_
    )


# --------------------------------------------------------------------------
# enums travel as values
# --------------------------------------------------------------------------


def test_enums_are_encoded_by_value_not_by_name() -> None:
    request_encoded = encode_action_request(valid_request())
    assert request_encoded["action"] == "consume.eat"
    record_encoded = encode_action_record(valid_record())
    assert record_encoded["action"] == "consume.eat"
    assert record_encoded["status"] == "succeeded"
    assert "CONSUME_EAT" not in json.dumps(record_encoded)
    assert "SUCCEEDED" not in json.dumps(record_encoded)
    assert request_encoded["policy"]["risk_permission"] == "P2"


@pytest.mark.parametrize(
    ("field", "name"),
    [("action", "CONSUME_EAT"), ("status", "SUCCEEDED")],
)
def test_record_refuses_an_enum_spelled_by_name(field: str, name: str) -> None:
    payload = encode_action_record(valid_record())
    payload[field] = name
    with pytest.raises(CodecError):
        decode_action_record(payload)


def test_request_refuses_an_enum_spelled_by_name() -> None:
    payload = encode_action_request(valid_request())
    payload["action"] = "CONSUME_EAT"
    with pytest.raises(CodecError):
        decode_action_request(payload)


# --------------------------------------------------------------------------
# strictness — a missing required field is a refusal, never a default
# --------------------------------------------------------------------------


REQUEST_REQUIRED = ("action", "session_id", "idempotency_key", "args", "policy", "lease_ms")
RECORD_REQUIRED = ("action_id", "action", "status", "idempotency_key")


@pytest.mark.parametrize("field", REQUEST_REQUIRED)
def test_request_refuses_a_missing_required_field(field: str) -> None:
    payload = encode_action_request(valid_request())
    del payload[field]
    with pytest.raises(CodecError) as exc:
        decode_action_request(payload)
    assert field in str(exc.value)


@pytest.mark.parametrize("field", ("allow_interrupt", "max_retries"))
def test_request_refuses_a_policy_missing_a_required_key(field: str) -> None:
    """A defaulted policy key is a permission granted by the peer's silence."""
    payload = encode_action_request(valid_request())
    del payload["policy"][field]
    with pytest.raises(CodecError) as exc:
        decode_action_request(payload)
    assert field in str(exc.value)


@pytest.mark.parametrize("field", RECORD_REQUIRED)
def test_record_refuses_a_missing_required_field(field: str) -> None:
    payload = encode_action_record(valid_record())
    del payload[field]
    with pytest.raises(CodecError) as exc:
        decode_action_record(payload)
    assert field in str(exc.value)


@pytest.mark.parametrize("field", RECORD_REQUIRED)
def test_status_body_refuses_a_record_missing_a_required_field(field: str) -> None:
    body = encode_action_status(valid_record())
    del body["record"][field]
    with pytest.raises(CodecError):
        decode_action_status(body)


def test_status_body_refuses_a_record_that_is_not_an_object() -> None:
    with pytest.raises(CodecError):
        decode_action_status({"record": "act-1"})


def test_status_body_refuses_an_empty_record_object() -> None:
    """ "No such action" is a *missing* key, never a present but empty one.

    The two answers are one keystroke apart in the decoder and opposite in
    meaning: a reader that treats an empty record as absence answers "the core
    has never seen this id" when the truth is "the peer sent an answer it never
    filled in". Deleting fields one at a time never reaches this shape, so it
    needs its own case.
    """
    with pytest.raises(CodecError):
        decode_action_status({"record": {}})


# --------------------------------------------------------------------------
# type confusion
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lease_ms", "20000"),  # int given a string
        ("lease_ms", True),  # int given a bool
        ("lease_ms", 12.5),  # int given a float
        # ...and a float that is *within* the lease bounds. 12.5 truncates to
        # 12, which the request's own range check refuses anyway, so on its own
        # it cannot tell a missing type check from a passing range check. This
        # one is only refused if the field is really read as an integer.
        ("lease_ms", 20_000.5),
        ("session_id", 1234),  # string given an int
        ("idempotency_key", ["eat-1"]),  # string given an array
        ("args", "item_ref=item:9001"),  # object given a string
        ("args", [["item_ref", "item:9001"]]),  # object given an array
        ("policy", "default"),  # object given a string
        ("action", "consume.feast"),  # enum given an unknown value
        ("action", 3),  # enum given an ordinal
    ],
)
def test_request_refuses_a_field_of_the_wrong_type(field: str, value: Any) -> None:
    payload = encode_action_request(valid_request())
    payload[field] = value
    with pytest.raises(CodecError):
        decode_action_request(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allow_interrupt", 1),  # bool given an int
        ("allow_interrupt", "true"),  # bool given a string
        ("max_retries", "2"),  # int given a string
        ("max_retries", 99),  # int outside the protocol's retry ceiling
        ("risk_permission", "P9"),  # enum given an unknown value
    ],
)
def test_request_refuses_a_policy_field_of_the_wrong_type(field: str, value: Any) -> None:
    payload = encode_action_request(valid_request())
    payload["policy"][field] = value
    with pytest.raises(CodecError):
        decode_action_request(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("action_id", 1),  # string given an int
        ("idempotency_key", 1),  # string given an int
        ("status", "finished"),  # enum given an unknown value
        ("status", 5),  # enum given an ordinal
        ("action", "consume.feast"),  # enum given an unknown value
        ("progress", "0.5"),  # number given a string
        ("progress", True),  # number given a bool
        ("progress", 1.5),  # number outside the record's own range
        ("progress", -0.1),
        ("result", 5),  # object given an int
        ("result", "succeeded"),  # object given a string
        ("action_id", ""),  # present but empty: nothing to poll on
    ],
)
def test_record_refuses_a_field_of_the_wrong_type(field: str, value: Any) -> None:
    payload = encode_action_record(valid_record())
    payload[field] = value
    with pytest.raises(CodecError):
        decode_action_record(payload)


def test_request_refuses_a_lease_outside_its_bounds() -> None:
    payload = encode_action_request(valid_request())
    payload["lease_ms"] = MAX_LEASE_MS + 1
    with pytest.raises(CodecError):
        decode_action_request(payload)


def test_request_refuses_an_over_long_idempotency_key() -> None:
    payload = encode_action_request(valid_request())
    payload["idempotency_key"] = "k" * 121
    with pytest.raises(CodecError):
        decode_action_request(payload)


def test_record_refuses_an_unreadable_result() -> None:
    payload = encode_action_record(valid_record())
    del payload["result"]["timestamp_ms"]
    with pytest.raises(CodecError):
        decode_action_record(payload)


# --------------------------------------------------------------------------
# the honesty rule — the reason this codec exists
# --------------------------------------------------------------------------


def test_a_succeeded_record_round_trips_with_its_proof_intact() -> None:
    record = valid_record()
    decoded = decode_action_record(encode_action_record(record))
    assert decoded.status is ActionStatus.SUCCEEDED
    assert decoded.result is not None
    assert decoded.result.reason_code is ReasonCode.POSTCONDITION_MET
    assert decoded.result.evidence == {"hunger_delta": -0.42}
    assert decoded.terminal is True


def test_succeeded_with_no_result_is_refused() -> None:
    """Queued work is "accepted". A success with nothing to show is not decoded."""
    with pytest.raises(CodecError):
        decode_action_record(succeeded_payload(None))


def test_succeeded_with_a_null_result_is_refused() -> None:
    payload = succeeded_payload(None)
    payload["result"] = None
    with pytest.raises(CodecError):
        decode_action_record(payload)


def test_succeeded_with_a_different_reason_code_is_refused() -> None:
    """The ack says the postcondition failed; the record says it succeeded."""
    payload = succeeded_payload(failure_result().to_dict())
    with pytest.raises(CodecError):
        decode_action_record(payload)


def test_succeeded_with_empty_evidence_is_refused() -> None:
    """POSTCONDITION_MET with nothing observed is a claim, not a postcondition."""
    result = succeeded_result().to_dict()
    del result["evidence"]
    assert result["reason_code"] == ReasonCode.POSTCONDITION_MET.value
    with pytest.raises(CodecError):
        decode_action_record(succeeded_payload(result))


def test_succeeded_with_an_evidence_object_that_is_empty_is_refused() -> None:
    result = succeeded_result().to_dict()
    result["evidence"] = {}
    with pytest.raises(CodecError):
        decode_action_record(succeeded_payload(result))


def test_a_success_claim_cannot_be_smuggled_in_through_the_status_body() -> None:
    """The wrapper delegates; it does not get a laxer path to the same record."""
    with pytest.raises(CodecError):
        decode_action_status({"record": succeeded_payload(None)})


def test_a_non_success_record_may_carry_a_result_without_evidence() -> None:
    """The rule constrains success only; a failure needs no postcondition."""
    record = ActionRecord(
        action_id="act-5",
        action=ActionName.CONSUME_EAT,
        status=ActionStatus.FAILED,
        idempotency_key="eat-2",
        result=failure_result(),
    )
    decoded = decode_action_record(encode_action_record(record))
    assert decoded == record
    assert decoded.result is not None
    assert decoded.result.evidence == {}


# --------------------------------------------------------------------------
# an error message names the field and the reason, never the value
# --------------------------------------------------------------------------


def secret_request_payload() -> JsonDict:
    payload = encode_action_request(valid_request())
    payload["session_id"] = SECRET
    payload["args"] = {"item_ref": SECRET, SECRET: SECRET}
    return payload


def secret_record_payload() -> JsonDict:
    payload = encode_action_record(valid_record())
    payload["action_id"] = SECRET
    payload["idempotency_key"] = SECRET
    payload["result"]["evidence"] = {"note": SECRET}
    payload["result"]["message"] = SECRET
    return payload


#: Built inside the tests rather than at import: a payload table assembled at
#: collection time turns a codec bug into a collection error, which reports as
#: one line and hides every other result in the file.
REQUEST_LEAK_CASES = (
    "missing lease_ms",
    "lease_ms is a string",
    "lease_ms out of range",
    "idempotency_key too long",
    "args is a string",
    "policy.allow_interrupt is a string",
    "policy is missing max_retries",
    "session_id is an array",
)

RECORD_LEAK_CASES = (
    "missing idempotency_key",
    "progress is a string",
    "progress out of range",
    "action_id is empty",
    "result is missing a field",
    "result carries a non-UUID session id",
    "succeeded with the wrong reason code",
    "succeeded with no result",
)


def leaky_request_payload(case: str) -> JsonDict:
    payload = secret_request_payload()
    if case == "missing lease_ms":
        del payload["lease_ms"]
    elif case == "lease_ms is a string":
        payload["lease_ms"] = SECRET
    elif case == "lease_ms out of range":
        payload["lease_ms"] = MAX_LEASE_MS + 1
    elif case == "idempotency_key too long":
        payload["idempotency_key"] = SECRET * 10
    elif case == "args is a string":
        payload["args"] = SECRET
    elif case == "policy.allow_interrupt is a string":
        payload["policy"] = {"allow_interrupt": SECRET, "max_retries": 1}
    elif case == "policy is missing max_retries":
        payload["policy"].pop("max_retries")
    elif case == "session_id is an array":
        payload["session_id"] = [SECRET]
    else:  # pragma: no cover - the table and this function are checked below
        raise AssertionError(case)
    return payload


def leaky_record_payload(case: str) -> JsonDict:
    payload = secret_record_payload()
    if case == "missing idempotency_key":
        del payload["idempotency_key"]
    elif case == "progress is a string":
        payload["progress"] = SECRET
    elif case == "progress out of range":
        payload["progress"] = 7.5
    elif case == "action_id is empty":
        payload["action_id"] = ""
    elif case == "result is missing a field":
        payload["result"].pop("session_id")
    elif case == "result carries a non-UUID session id":
        # The protocol's own UUID reader quotes what it rejected. Chaining its
        # text into the codec's message would carry that value straight into a
        # bug report, so this case exists to make that mistake fail here.
        payload["result"]["session_id"] = SECRET
    elif case == "succeeded with the wrong reason code":
        payload["result"] = failure_result(message=SECRET).to_dict()
        payload["result"]["evidence"] = {"note": SECRET}
    elif case == "succeeded with no result":
        del payload["result"]
    else:  # pragma: no cover - the table and this function are checked below
        raise AssertionError(case)
    return payload


@pytest.mark.parametrize("case", REQUEST_LEAK_CASES)
def test_request_errors_never_quote_the_value(case: str) -> None:
    with pytest.raises(CodecError) as exc:
        decode_action_request(leaky_request_payload(case))
    assert SECRET not in str(exc.value)


@pytest.mark.parametrize("case", RECORD_LEAK_CASES)
def test_record_errors_never_quote_the_value(case: str) -> None:
    with pytest.raises(CodecError) as exc:
        decode_action_record(leaky_record_payload(case))
    assert SECRET not in str(exc.value)


def test_every_leak_case_is_a_payload_the_codec_really_refuses() -> None:
    """Each case must be distinct and must actually reach a refusal.

    Without this, a case whose payload stopped being invalid would silently
    stop testing anything: ``pytest.raises`` above would fail, but a case that
    was never built at all would not.
    """
    request_payloads = [leaky_request_payload(case) for case in REQUEST_LEAK_CASES]
    record_payloads = [leaky_record_payload(case) for case in RECORD_LEAK_CASES]
    assert len(set(REQUEST_LEAK_CASES)) == len(REQUEST_LEAK_CASES)
    assert len(set(RECORD_LEAK_CASES)) == len(RECORD_LEAK_CASES)
    for payload in request_payloads:
        assert payload != secret_request_payload()
    for payload in record_payloads:
        assert payload != secret_record_payload()


def test_a_rejected_number_is_not_quoted_either() -> None:
    """ "Never the value" is not only about strings.

    The record's own ``ValueError`` for an out-of-range progress quotes the
    number. A codec that chained that text through would be quoting a value the
    payload chose, which is the habit this rule exists to break — the next
    field it is applied to is one that carries game text.
    """
    payload = encode_action_record(valid_record())
    payload["progress"] = 133.7
    with pytest.raises(CodecError) as exc:
        decode_action_record(payload)
    assert "133.7" not in str(exc.value)
    assert "progress" in str(exc.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [("lease_ms", MAX_LEASE_MS + 1), ("idempotency_key", "k" * 121)],
)
def test_the_request_bounds_refusal_still_names_what_it_checked(field: str, value: Any) -> None:
    """The request's own bounds get the same treatment as the record's.

    Both refusals above assert only that *something* was raised, which a
    message reduced to a bare "malformed" would still satisfy. The rule is
    field and reason but never the value, and it applies on this path too.
    """
    payload = encode_action_request(valid_request())
    payload[field] = value
    with pytest.raises(CodecError) as exc:
        decode_action_request(payload)
    message = str(exc.value)
    assert "action.request" in message
    assert field in message
    assert str(value) not in message


def test_error_messages_still_name_the_field() -> None:
    """Not quoting the value is only useful if the message stays diagnostic."""
    payload = secret_record_payload()
    del payload["status"]
    with pytest.raises(CodecError) as exc:
        decode_action_record(payload)
    message = str(exc.value)
    assert "status" in message
    assert "action.record" in message
    assert SECRET not in message


# --------------------------------------------------------------------------
# Cyrillic — this ships to a Russian install
# --------------------------------------------------------------------------


def test_cyrillic_survives_the_request_round_trip() -> None:
    request_ = ActionRequest(
        action=ActionName.INVENTORY_TRANSFER,
        session_id=SESSION_ID,
        idempotency_key="перенести-1",
        args={"предмет": CYRILLIC_LABEL, "контейнер": "рюкзак", "ёмкость": 12},
        policy=CommandPolicy(allow_interrupt=False, max_retries=1),
        lease_ms=1_000,
    )
    encoded = encode_action_request(request_)
    assert decode_action_request(json.loads(json.dumps(encoded))) == request_
    assert decode_action_request(json.loads(json.dumps(encoded, ensure_ascii=False))) == request_


def test_cyrillic_survives_the_record_round_trip() -> None:
    record = ActionRecord(
        action_id="действие-7",
        action=ActionName.LITERATURE_READ,
        status=ActionStatus.SUCCEEDED,
        idempotency_key="читать-1",
        progress=1.0,
        result=succeeded_result(
            evidence={"название": CYRILLIC_LABEL, "страниц": 42},
            message="постусловие подтверждено",
        ),
    )
    encoded = encode_action_record(record)
    decoded = decode_action_record(json.loads(json.dumps(encoded)))
    assert decoded == record
    assert decoded.result is not None
    assert decoded.result.evidence["название"] == CYRILLIC_LABEL
    assert decoded.result.message == "постусловие подтверждено"
    assert decode_action_record(json.loads(json.dumps(encoded, ensure_ascii=False))) == record
