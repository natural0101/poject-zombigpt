"""The goal codec: the request out, the channel back, and the closed kind set.

Round trips assert equality of the *record*, never the shape of the JSON. A
field both halves forget survives any assertion about the document and fails an
assertion about the object, which is the only reason a codec pair drifting
together can be caught here at all. Where the shape itself matters — the key a
value travels under, the spelling of an enum — it is written out by hand in this
file rather than read back from the module under test, because a constant
compared against itself is a test that cannot fail.

Two properties get most of the file.

**The kind set is closed on the wire.** ``GoalKind`` is the whole width of the
opening between a language model or a microphone and the action engine, and it
is an enum in the process and a string on the pipe. A decoder that resolved an
unknown token to a member — by falling back, by prefix matching, by taking the
first of the five — would turn "train me in swordsmanship" into "eat something",
and the caller would be told its goal was accepted. So an invented kind must
raise, and the raising must not quote the token: that string came from outside,
and an exception is written before any redactor runs.

**Success cannot be claimed by a peer.** ``GoalRecord`` refuses to exist as
``SUCCEEDED`` without ``POSTCONDITION_MET`` and observed evidence keys, and the
codec does not re-implement that check — it builds the real record. The tests
below drive payloads that claim success without the evidence, because the whole
point of a two-process design is that the process reporting success is not the
one that watched the world.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from pz_agent_core.goals import (
    GOAL_SPECS,
    GoalAdmission,
    GoalBudget,
    GoalKind,
    GoalParams,
    GoalRecord,
    GoalRefusal,
    GoalRequest,
    GoalState,
    TrainableSkill,
    parse_kind,
)
from pz_agent_core.protocol import JsonDict, ReasonCode
from pz_agent_mcp.ports import GoalCancellation, GoalChannelStatus
from pz_agent_mcp.remote.codec import CodecError
from pz_agent_mcp.remote.codec.goals import (
    decode_goal_admission,
    decode_goal_cancellation,
    decode_goal_channel_status,
    decode_goal_id,
    decode_goal_query,
    decode_goal_record,
    decode_goal_refusal,
    decode_goal_request,
    encode_goal_admission,
    encode_goal_cancellation,
    encode_goal_channel_status,
    encode_goal_id,
    encode_goal_query,
    encode_goal_record,
    encode_goal_refusal,
    encode_goal_request,
)

#: Recognisable, secret-shaped, and never legitimately part of an error text.
#: Placed in a field that is then refused, and asserted absent from the message.
SECRET = "sk-live-ZOMBI-goal-9f8e7d6c5b4a3210-DO-NOT-LOG"

#: The install this is built for is Russian, so a caller's bytes are as likely
#: to be Cyrillic as not.
CYRILLIC = "покорми меня чем-нибудь"

#: A budget written out by hand and deliberately unequal to every entry in
#: ``GOAL_SPECS``, so "the budget crossed" cannot pass by the decoder happening
#: to resolve the kind's default.
CHOSEN_BUDGET = GoalBudget(max_wall_ms=45_000, max_steps=3, pending_ttl_ms=9_000)

#: The parameters each kind requires, so the round trip below can be driven off
#: ``GoalKind`` itself and still build a request every kind accepts.
REQUIRED_PARAMS: dict[GoalKind, GoalParams] = {
    GoalKind.SATISFY_HUNGER: GoalParams(),
    GoalKind.SATISFY_THIRST: GoalParams(),
    GoalKind.READ_FOR_BOREDOM: GoalParams(),
    GoalKind.TRAIN_SKILL: GoalParams(skill=TrainableSkill.CARPENTRY),
    GoalKind.LEARN_RECIPE: GoalParams(),
}

#: Tokens that are not goals. None of them resolves through ``parse_kind`` —
#: asserted below rather than assumed — and each is a different way of being
#: wrong: an invention, a prefix of a real one, a real one with a suffix, an
#: empty string, a secret-shaped string, and free Cyrillic text.
INVENTED_KINDS = (
    "eat_the_dog",
    "satisfy",
    "satisfy_hunger_now",
    "",
    SECRET,
    CYRILLIC,
    "x" * 500,
)


# ---------------------------------------------------------------------------
# builders and the crossing itself
# ---------------------------------------------------------------------------


def crossing(payload: JsonDict) -> JsonDict:
    """What the pipe does to a body: JSON out, JSON in, and nothing else.

    Every round trip in this file goes through here rather than handing the
    encoder's own dict straight to the decoder. Serialising is what turns a
    tuple into a list, an enum member into whatever it serialises as, and a
    non-string key into a string — and a decoder tested against the encoder's
    in-memory object never meets any of that.
    """
    restored = json.loads(json.dumps(payload))
    assert isinstance(restored, dict)
    return restored


def assert_plain_json(value: object) -> None:
    """Every leaf is exactly a JSON type — not a subclass of one.

    ``GoalKind`` and ``ReasonCode`` are ``StrEnum`` members, so they *are*
    strings: an encoder that wrote ``record.kind`` instead of
    ``record.kind.value`` serialises identically and passes ``json.dumps``
    without complaint. ``type(...) is str`` is what tells the two apart, and it
    is the whole reason this check does not use ``isinstance``.
    """
    if value is None or type(value) in (bool, int, float, str):
        return
    if type(value) is list:
        for item in value:
            assert_plain_json(item)
        return
    assert type(value) is dict, f"{value!r} is a {type(value).__name__}, not a JSON value"
    for key, item in value.items():
        assert type(key) is str, f"{key!r} is a {type(key).__name__}, not a JSON key"
        assert_plain_json(item)


class WatchedKeys(list[str]):
    """A JSON array that records whether anything walked it.

    A bound whose only job is to refuse *before* the work is done cannot be
    observed through the outcome: ``GoalRecord`` rejects an over-long evidence
    tuple as well, so removing the codec's own length check leaves every
    assertion about the refusal intact and only changes how much of a hostile
    array was materialised first. This subclass makes that difference visible —
    ``walked`` counts the times the array was iterated, and a check that runs
    after ``len`` leaves it at zero.
    """

    def __init__(self, size: int) -> None:
        super().__init__(f"k{index}" for index in range(size))
        self.walked = 0

    def __iter__(self) -> Iterator[str]:
        self.walked += 1
        return super().__iter__()


def a_request(
    *,
    kind: GoalKind = GoalKind.TRAIN_SKILL,
    params: GoalParams | None = None,
    budget: GoalBudget | None = None,
    idempotency_key: str = "goal-1:attempt-1",
) -> GoalRequest:
    return GoalRequest(
        kind=kind,
        idempotency_key=idempotency_key,
        params=params if params is not None else REQUIRED_PARAMS[kind],
        budget=budget,
    )


def a_record(
    *,
    kind: GoalKind = GoalKind.TRAIN_SKILL,
    params: GoalParams | None = None,
    state: GoalState = GoalState.PENDING,
    sequence: int = 3,
    goal_id: str = "3f1b0c92-0000-4000-8000-00000000000a",
    steps_used: int = 0,
    started_at_ms: int | None = None,
    finished_at_ms: int | None = None,
    reason_code: ReasonCode | None = None,
    evidence_keys: tuple[str, ...] = (),
    detail: str = "",
) -> GoalRecord:
    return GoalRecord(
        goal_id=goal_id,
        kind=kind,
        params=params if params is not None else REQUIRED_PARAMS[kind],
        budget=CHOSEN_BUDGET,
        key_digest="0123456789abcdef",
        sequence=sequence,
        state=state,
        submitted_at_ms=1_000,
        started_at_ms=started_at_ms,
        finished_at_ms=finished_at_ms,
        steps_used=steps_used,
        reason_code=reason_code,
        evidence_keys=evidence_keys,
        detail=detail,
    )


def a_succeeded_record() -> GoalRecord:
    return a_record(
        state=GoalState.SUCCEEDED,
        started_at_ms=1_200,
        finished_at_ms=4_500,
        steps_used=2,
        reason_code=ReasonCode.POSTCONDITION_MET,
        evidence_keys=("nutrition", "item_ref"),
        detail="the postcondition was observed",
    )


#: A whole record body, written out by hand. Never produced by the encoder —
#: that is the point: a key swapped consistently in both halves of the codec
#: reproduces every record exactly and hands the peer a goal with two fields
#: exchanged.
HAND_WRITTEN_RECORD: JsonDict = {
    "goal_id": "3f1b0c92-0000-4000-8000-00000000000a",
    "kind": "read_for_boredom",
    "params": {"pages": 6},
    "budget": {"max_wall_ms": 45000, "max_steps": 3, "pending_ttl_ms": 9000},
    "key_digest": "0123456789abcdef",
    "sequence": 3,
    "state": "active",
    "submitted_at_ms": 1000,
    "started_at_ms": 1200,
    "finished_at_ms": None,
    "steps_used": 1,
    "reason_code": None,
    "evidence_keys": [],
    "detail": "",
}


# ---------------------------------------------------------------------------
# T004 — the request crosses, kind and budget included
# ---------------------------------------------------------------------------


class TestARequestSurvivesTheCrossing:
    @pytest.mark.parametrize("kind", sorted(GoalKind))
    def test_every_declared_kind_round_trips(self, kind: GoalKind) -> None:
        """Driven off the enum, so a sixth member cannot be added untested."""
        request = a_request(kind=kind, budget=CHOSEN_BUDGET)

        assert decode_goal_request(crossing(encode_goal_request(request))) == request

    def test_the_kind_travels_as_its_value(self) -> None:
        """The value, not the member name and not an ordinal.

        ``train_skill`` is written out by hand: an ordinal moves when a member
        is inserted, a name moves when one is renamed, and only the value is
        what the channel already pins.
        """
        body = encode_goal_request(a_request(kind=GoalKind.TRAIN_SKILL))

        assert body["kind"] == "train_skill"

    def test_a_chosen_budget_crosses_and_is_not_the_kind_default(self) -> None:
        """The three bounds arrive as the three bounds the caller chose.

        Asserted against hand-written numbers *and* against the kind's default
        being a different object, because a decoder that ignored the field and
        resolved ``GOAL_SPECS`` would satisfy a round trip that only compared
        behaviour.
        """
        request = a_request(kind=GoalKind.READ_FOR_BOREDOM, budget=CHOSEN_BUDGET)

        body = encode_goal_request(request)
        restored = decode_goal_request(crossing(body))

        assert body["budget"] == {"max_wall_ms": 45000, "max_steps": 3, "pending_ttl_ms": 9000}
        assert restored.budget == CHOSEN_BUDGET
        assert restored.budget != GOAL_SPECS[GoalKind.READ_FOR_BOREDOM].budget
        assert restored.effective_budget == CHOSEN_BUDGET

    def test_an_unstated_budget_stays_unstated(self) -> None:
        """Absent means "use the kind's default", and it is not resolved here.

        A decoder that materialised the default would answer a question the
        caller left open, and the request it produced would stop being equal to
        the one that was sent the moment ``GOAL_SPECS`` moved.
        """
        request = a_request(kind=GoalKind.SATISFY_HUNGER, budget=None)

        body = encode_goal_request(request)
        restored = decode_goal_request(crossing(body))

        assert "budget" not in body
        assert restored.budget is None
        assert restored == request
        assert restored.effective_budget == GOAL_SPECS[GoalKind.SATISFY_HUNGER].budget

    def test_a_satisfy_to_of_zero_is_carried(self) -> None:
        """``0.0`` is a parameter the caller chose, not an absent one.

        An encoder testing truthiness drops it, the request decodes with no
        ``satisfy_to``, and the goal quietly widens from "stop at nothing" to
        "stop wherever the policy stops".
        """
        request = a_request(kind=GoalKind.SATISFY_THIRST, params=GoalParams(satisfy_to=0.0))

        body = encode_goal_request(request)

        assert body["params"] == {"satisfy_to": 0.0}
        assert decode_goal_request(crossing(body)) == request

    def test_the_skill_and_its_numbers_cross_as_written(self) -> None:
        request = a_request(
            params=GoalParams(skill=TrainableSkill.FIRST_AID, target_level=7, pages=12)
        )

        body = encode_goal_request(request)

        assert body["params"] == {"skill": "first_aid", "target_level": 7, "pages": 12}
        assert decode_goal_request(crossing(body)) == request

    def test_a_hand_written_body_reaches_the_fields_it_names(self) -> None:
        """Each key lands where it was addressed, not merely somewhere.

        A codec pair that swapped ``target_level`` and ``pages`` in both halves
        round-trips perfectly and asks the core to read seven pages to reach
        level twelve.
        """
        restored = decode_goal_request(
            {
                "kind": "train_skill",
                "idempotency_key": "goal-9:attempt-2",
                "params": {"skill": "cooking", "target_level": 4, "pages": 11},
                "budget": {"max_wall_ms": 45000, "max_steps": 3, "pending_ttl_ms": 9000},
            }
        )

        assert restored.kind is GoalKind.TRAIN_SKILL
        assert restored.idempotency_key == "goal-9:attempt-2"
        assert restored.params.skill is TrainableSkill.COOKING
        assert restored.params.target_level == 4
        assert restored.params.pages == 11
        assert restored.params.satisfy_to is None
        assert restored.budget == GoalBudget(max_wall_ms=45000, max_steps=3, pending_ttl_ms=9000)

    def test_only_plain_json_leaves_the_encoder(self) -> None:
        """No enum member, no tuple, no dataclass — and never a pickle.

        ``StrEnum`` members serialise as strings, so this is the one check that
        separates ``kind.value`` from ``kind`` itself.
        """
        assert_plain_json(encode_goal_request(a_request(budget=CHOSEN_BUDGET)))
        assert_plain_json(encode_goal_record(a_succeeded_record()))
        assert_plain_json(
            encode_goal_channel_status(
                GoalChannelStatus(active=a_record(state=GoalState.ACTIVE, started_at_ms=1_200))
            )
        )

    def test_a_missing_field_is_refused_rather_than_defaulted(self) -> None:
        body = encode_goal_request(a_request())
        del body["idempotency_key"]

        with pytest.raises(CodecError):
            decode_goal_request(crossing(body))

    def test_an_idempotency_key_the_channel_refuses_is_not_quoted_back(self) -> None:
        """The key is a caller's bytes; a refusal must not carry them.

        A space is not in the key's shape, so the request type rejects it — and
        its own message would have quoted the string it rejected.
        """
        with pytest.raises(CodecError) as refused:
            decode_goal_request(
                {
                    "kind": "satisfy_hunger",
                    "idempotency_key": f"{SECRET} {CYRILLIC}",
                    "params": {},
                }
            )

        assert SECRET not in str(refused.value)
        assert CYRILLIC not in str(refused.value)

    def test_parameters_a_kind_does_not_take_are_refused(self) -> None:
        """The spec table decides, and it is not restated in the codec."""
        with pytest.raises(CodecError):
            decode_goal_request(
                {
                    "kind": "satisfy_hunger",
                    "idempotency_key": "goal-1:attempt-1",
                    "params": {"skill": "carpentry"},
                }
            )

    def test_a_number_outside_its_range_is_refused(self) -> None:
        with pytest.raises(CodecError):
            decode_goal_request(
                {
                    "kind": "train_skill",
                    "idempotency_key": "goal-1:attempt-1",
                    "params": {"skill": "carpentry", "target_level": 99},
                }
            )

    def test_an_out_of_range_optional_parameter_is_refused_not_emptied(self) -> None:
        """The case the required-parameter one above cannot see.

        ``train_skill`` needs a skill, so a decoder that answered a rejected
        parameter object with an *empty* one would still be refused a line
        later — by the request, for the wrong reason — and would look correct.
        ``pages`` is optional on ``read_for_boredom``, so the same emptying
        decodes clean and admits a goal that reads whatever the policy picks
        instead of the 999 pages the caller asked for. 999 is written out
        against a parameter whose declared ceiling is 200.
        """
        with pytest.raises(CodecError):
            decode_goal_request(
                {
                    "kind": "read_for_boredom",
                    "idempotency_key": "goal-1:attempt-1",
                    "params": {"pages": 999},
                }
            )

    def test_a_budget_outside_the_channel_s_bounds_is_refused(self) -> None:
        with pytest.raises(CodecError):
            decode_goal_request(
                {
                    "kind": "satisfy_hunger",
                    "idempotency_key": "goal-1:attempt-1",
                    "params": {},
                    "budget": {"max_wall_ms": 1, "max_steps": 3, "pending_ttl_ms": 9000},
                }
            )


# ---------------------------------------------------------------------------
# T005 — an invented kind is refused, never defaulted
# ---------------------------------------------------------------------------


class TestAnInventedKindIsRefused:
    @pytest.mark.parametrize("token", INVENTED_KINDS)
    def test_the_token_is_not_a_goal_to_begin_with(self, token: str) -> None:
        """The premise of every test below, stated rather than assumed.

        If one of these ever *did* resolve, the refusal tests would still pass —
        they would just be asserting nothing.
        """
        assert parse_kind(token) is None

    @pytest.mark.parametrize("token", INVENTED_KINDS)
    def test_an_invented_kind_in_a_request_is_refused(self, token: str) -> None:
        """Refused, not resolved to the nearest member and not to the first.

        This is the whole width of the opening: a decoder that fell back would
        turn a token nobody defined into ``satisfy_hunger``, and the caller
        would be told its goal was accepted while the agent went to eat.
        """
        with pytest.raises(CodecError):
            decode_goal_request(
                {"kind": token, "idempotency_key": "goal-1:attempt-1", "params": {}}
            )

    @pytest.mark.parametrize("token", INVENTED_KINDS)
    def test_an_invented_kind_in_a_record_is_refused(self, token: str) -> None:
        """The same door, in the answering direction."""
        with pytest.raises(CodecError):
            decode_goal_record({**HAND_WRITTEN_RECORD, "kind": token})

    @pytest.mark.parametrize("token", INVENTED_KINDS)
    def test_the_refusal_does_not_quote_the_token(self, token: str) -> None:
        """The kind is what a model or a transcript proposed.

        ``require_enum`` names the value it rejected, which is right for a
        protocol identifier and wrong for this field: the message reaches a
        traceback and a bug report before any redactor sees it.
        """
        with pytest.raises(CodecError) as refused:
            decode_goal_request(
                {"kind": token, "idempotency_key": "goal-1:attempt-1", "params": {}}
            )

        assert token not in str(refused.value) or token == ""
        assert "kind" in str(refused.value)

    @pytest.mark.parametrize("wrong", [42, None, ["satisfy_hunger"], {"kind": "satisfy_hunger"}])
    def test_a_kind_that_is_not_a_string_is_refused(self, wrong: object) -> None:
        with pytest.raises(CodecError):
            decode_goal_request(
                {"kind": wrong, "idempotency_key": "goal-1:attempt-1", "params": {}}
            )

    def test_a_missing_kind_is_refused(self) -> None:
        with pytest.raises(CodecError):
            decode_goal_request({"idempotency_key": "goal-1:attempt-1", "params": {}})

    def test_an_invented_skill_is_refused_and_not_dropped(self) -> None:
        """Dropping it would be worse than refusing.

        ``train_skill`` requires a skill, so a decoder that silently discarded
        an unknown one would refuse this request for the wrong reason — and on a
        kind where the parameter is optional it would admit a goal the caller
        never asked for.
        """
        with pytest.raises(CodecError) as refused:
            decode_goal_request(
                {
                    "kind": "train_skill",
                    "idempotency_key": "goal-1:attempt-1",
                    "params": {"skill": "swordsmanship"},
                }
            )

        assert "swordsmanship" not in str(refused.value)
        assert "skill" in str(refused.value)

    def test_an_invented_state_is_refused(self) -> None:
        with pytest.raises(CodecError):
            decode_goal_record({**HAND_WRITTEN_RECORD, "state": "nearly_done"})

    def test_an_invented_reason_code_is_refused(self) -> None:
        with pytest.raises(CodecError):
            decode_goal_record(
                {
                    **HAND_WRITTEN_RECORD,
                    "state": "failed",
                    "finished_at_ms": 4000,
                    "reason_code": "BECAUSE_I_SAID_SO",
                }
            )

    def test_an_invented_reason_code_on_an_open_goal_is_refused_not_dropped(self) -> None:
        """The case the terminal one above cannot see.

        A record that says ``failed`` is refused by the record itself the
        moment its reason code goes missing, so a decoder that quietly turned
        an unknown code into ``None`` would still raise there and look correct.
        On an *open* goal ``None`` is the legal value, so a dropped code decodes
        clean and the peer's stated reason is gone without a word. This body is
        ``active``, which is exactly that case.
        """
        with pytest.raises(CodecError):
            decode_goal_record({**HAND_WRITTEN_RECORD, "reason_code": "BECAUSE_I_SAID_SO"})


# ---------------------------------------------------------------------------
# the record, and the honesty rule it carries
# ---------------------------------------------------------------------------


class TestTheRecordCrossesWithItsInvariant:
    @pytest.mark.parametrize(
        "record",
        [
            a_record(),
            a_record(state=GoalState.ACTIVE, started_at_ms=1_200, steps_used=1),
            a_succeeded_record(),
            a_record(
                state=GoalState.CANCELLED,
                started_at_ms=1_200,
                finished_at_ms=2_000,
                reason_code=ReasonCode.CANCELLED_BY_REQUEST,
                detail="cancelled at the user's request",
            ),
            a_record(
                kind=GoalKind.READ_FOR_BOREDOM,
                params=GoalParams(pages=6),
                state=GoalState.EXPIRED,
                finished_at_ms=9_000,
                reason_code=ReasonCode.ACTION_TIMEOUT,
            ),
        ],
        ids=["pending", "active", "succeeded", "cancelled", "expired"],
    )
    def test_a_record_round_trips(self, record: GoalRecord) -> None:
        assert decode_goal_record(crossing(encode_goal_record(record))) == record

    def test_the_hand_written_body_decodes_to_the_fields_it_names(self) -> None:
        restored = decode_goal_record(HAND_WRITTEN_RECORD)

        assert restored.goal_id == "3f1b0c92-0000-4000-8000-00000000000a"
        assert restored.kind is GoalKind.READ_FOR_BOREDOM
        assert restored.params.pages == 6
        assert restored.budget == GoalBudget(max_wall_ms=45000, max_steps=3, pending_ttl_ms=9000)
        assert restored.key_digest == "0123456789abcdef"
        assert restored.sequence == 3
        assert restored.state is GoalState.ACTIVE
        assert restored.submitted_at_ms == 1000
        assert restored.started_at_ms == 1200
        assert restored.finished_at_ms is None
        assert restored.steps_used == 1
        assert restored.reason_code is None
        assert restored.evidence_keys == ()

    def test_evidence_keys_arrive_as_a_tuple_in_order(self) -> None:
        """A list would make an ostensibly frozen record mutable through a field.

        Order is the channel's; a codec that sorted would silently repair a peer
        that had it wrong, indistinguishably from the peer being right.
        """
        restored = decode_goal_record(crossing(encode_goal_record(a_succeeded_record())))

        assert restored.evidence_keys == ("nutrition", "item_ref")
        assert type(restored.evidence_keys) is tuple

    def test_a_success_without_evidence_is_refused(self) -> None:
        """The rule the whole link exists for.

        The process claiming the postcondition is not the process that watched
        the world, so a claim with nothing behind it fails to decode rather than
        being believed.
        """
        with pytest.raises(CodecError):
            decode_goal_record(
                {
                    **HAND_WRITTEN_RECORD,
                    "state": "succeeded",
                    "finished_at_ms": 4000,
                    "reason_code": "POSTCONDITION_MET",
                    "evidence_keys": [],
                }
            )

    def test_a_success_carrying_another_reason_is_refused(self) -> None:
        with pytest.raises(CodecError):
            decode_goal_record(
                {
                    **HAND_WRITTEN_RECORD,
                    "state": "succeeded",
                    "finished_at_ms": 4000,
                    "reason_code": "NO_PROGRESS",
                    "evidence_keys": ["nutrition"],
                }
            )

    def test_a_terminal_goal_with_no_reason_is_refused(self) -> None:
        with pytest.raises(CodecError):
            decode_goal_record({**HAND_WRITTEN_RECORD, "state": "failed", "finished_at_ms": 4000})

    def test_more_evidence_keys_than_a_goal_keeps_is_refused(self) -> None:
        """Nine, written out, against a channel that keeps eight."""
        with pytest.raises(CodecError):
            decode_goal_record(
                {
                    **HAND_WRITTEN_RECORD,
                    "evidence_keys": [f"k{index}" for index in range(9)],
                }
            )

    def test_an_over_long_evidence_array_is_refused_before_it_is_walked(self) -> None:
        """The bound is about the work, and the outcome cannot show it.

        ``GoalRecord`` refuses nine keys too, so the test above passes whether
        or not the codec looks at the length first — and a peer answering with a
        million-element array would then be copied element by element before
        anything objected. What this asserts is the part that only the bound
        provides: ``len`` was consulted and the array was never iterated.
        """
        keys = WatchedKeys(9)

        with pytest.raises(CodecError):
            decode_goal_record({**HAND_WRITTEN_RECORD, "evidence_keys": keys})

        assert keys.walked == 0

    def test_steps_beyond_the_budget_are_refused(self) -> None:
        """The budget on the record is the budget the steps are checked against."""
        with pytest.raises(CodecError):
            decode_goal_record({**HAND_WRITTEN_RECORD, "steps_used": 4})

    @pytest.mark.parametrize("field", ["state", "steps_used", "sequence", "budget", "key_digest"])
    def test_a_dropped_field_is_refused_rather_than_defaulted(self, field: str) -> None:
        """Missing means missing.

        Defaulting ``steps_used`` reports a goal that spent its budget as one
        that has just started; defaulting ``state`` reports a cancelled goal as
        pending. Both are confident, wrong, harmless-looking answers where the
        honest one is that the payload could not be read.
        """
        body = dict(HAND_WRITTEN_RECORD)
        del body[field]

        with pytest.raises(CodecError):
            decode_goal_record(body)

    def test_no_encoding_of_a_record_carries_the_raw_idempotency_key(self) -> None:
        """The record does not hold it, and the wire must not reintroduce it."""
        document = json.dumps(encode_goal_record(a_succeeded_record()))

        assert "0123456789abcdef" in document
        assert "goal-1:attempt-1" not in document

    def test_a_record_that_finished_before_it_started_is_refused(self) -> None:
        """A wall clock stepped backwards is a loud failure, not a record."""
        with pytest.raises(CodecError):
            decode_goal_record(
                {
                    **HAND_WRITTEN_RECORD,
                    "state": "failed",
                    "started_at_ms": 5000,
                    "finished_at_ms": 4000,
                    "reason_code": "NO_PROGRESS",
                }
            )


# ---------------------------------------------------------------------------
# the three answers
# ---------------------------------------------------------------------------


class TestTheAdmission:
    def test_an_admitted_goal_round_trips(self) -> None:
        admission = GoalAdmission(goal=a_record())

        restored = decode_goal_admission(crossing(encode_goal_admission(admission)))

        assert restored == admission
        assert restored.duplicate is False

    def test_a_duplicate_round_trips_as_a_duplicate(self) -> None:
        """The difference between "admitted" and "you already sent this one".

        A caller that could not tell them apart would count a retried
        submission — the ordinary consequence of a dropped acknowledgement — as
        a second goal.
        """
        admission = GoalAdmission(goal=a_record(), duplicate=True)

        body = encode_goal_admission(admission)

        assert body["duplicate"] is True
        assert decode_goal_admission(crossing(body)) == admission

    def test_a_refusal_round_trips(self) -> None:
        refusal = GoalRefusal(
            reason_code=ReasonCode.QUEUE_REJECTED,
            message="the goal channel already holds 4 open goal(s), which is its cap.",
            active_goal_id="3f1b0c92-0000-4000-8000-00000000000a",
        )
        admission = GoalAdmission(refusal=refusal)

        restored = decode_goal_admission(crossing(encode_goal_admission(admission)))

        assert restored == admission
        assert restored.accepted is False

    def test_the_refusal_keys_are_the_ones_written_here(self) -> None:
        refusal = GoalRefusal(
            reason_code=ReasonCode.NOT_ARMED,
            message="the goal channel is disarmed; arm the session first.",
        )

        assert encode_goal_refusal(refusal) == {
            "reason_code": "NOT_ARMED",
            "message": "the goal channel is disarmed; arm the session first.",
            "active_goal_id": "",
        }

    def test_a_dropped_active_goal_id_is_refused_rather_than_emptied(self) -> None:
        """ "No goal is active" is the answer that stops a user unblocking theirs."""
        with pytest.raises(CodecError):
            decode_goal_refusal({"reason_code": "QUEUE_REJECTED", "message": "full."})

    def test_an_admission_carrying_both_is_refused(self) -> None:
        with pytest.raises(CodecError):
            decode_goal_admission(
                {
                    "duplicate": False,
                    "goal": HAND_WRITTEN_RECORD,
                    "refusal": {
                        "reason_code": "NOT_ARMED",
                        "message": "disarmed.",
                        "active_goal_id": "",
                    },
                }
            )

    def test_an_admission_carrying_neither_is_refused(self) -> None:
        with pytest.raises(CodecError):
            decode_goal_admission({"duplicate": False})

    def test_a_dropped_duplicate_flag_is_refused(self) -> None:
        with pytest.raises(CodecError):
            decode_goal_admission({"goal": HAND_WRITTEN_RECORD})


class TestTheChannelStatus:
    def test_an_empty_channel_round_trips(self) -> None:
        status = GoalChannelStatus()

        body = encode_goal_channel_status(status)

        assert body == {"active": None, "pending": [], "named": None}
        assert decode_goal_channel_status(crossing(body)) == status

    def test_the_three_slots_stay_in_their_own_slots(self) -> None:
        """``active``, ``named`` and the backlog are three different answers.

        A codec pair that put ``named`` under ``active`` in both halves would
        report the goal a caller asked about as the one the agent is serving.
        """
        active = a_record(goal_id="active-goal", state=GoalState.ACTIVE, started_at_ms=1_200)
        named = a_record(goal_id="named-goal", state=GoalState.PENDING)
        waiting = a_record(goal_id="waiting-goal", state=GoalState.PENDING, sequence=7)
        status = GoalChannelStatus(active=active, pending=(waiting,), named=named)

        restored = decode_goal_channel_status(crossing(encode_goal_channel_status(status)))

        assert restored.active is not None
        assert restored.active.goal_id == "active-goal"
        assert restored.named is not None
        assert restored.named.goal_id == "named-goal"
        assert [record.goal_id for record in restored.pending] == ["waiting-goal"]

    def test_each_slot_is_written_under_the_key_named_here(self) -> None:
        """The key names themselves, by hand, against the encoder's output.

        The round trip above cannot see this one: a pair that spelled ``active``
        as ``named`` and ``named`` as ``active`` in *both* halves reproduces
        every status exactly and still hands a peer built from the other
        spelling the goal it asked about as the one the agent is serving.
        """
        status = GoalChannelStatus(
            active=a_record(goal_id="active-goal", state=GoalState.ACTIVE, started_at_ms=1_200),
            pending=(a_record(goal_id="waiting-goal"),),
            named=a_record(goal_id="named-goal"),
        )

        body = encode_goal_channel_status(status)

        assert sorted(body) == ["active", "named", "pending"]
        assert body["active"]["goal_id"] == "active-goal"
        assert body["named"]["goal_id"] == "named-goal"
        assert [record["goal_id"] for record in body["pending"]] == ["waiting-goal"]

    def test_the_backlog_keeps_its_order_and_its_type(self) -> None:
        """Oldest first by admission sequence, and never re-sorted here."""
        backlog = tuple(
            a_record(goal_id=f"g-{index}", sequence=sequence)
            for index, sequence in enumerate((9, 4, 6))
        )
        status = GoalChannelStatus(pending=backlog)

        restored = decode_goal_channel_status(crossing(encode_goal_channel_status(status)))

        assert [record.goal_id for record in restored.pending] == ["g-0", "g-1", "g-2"]
        assert type(restored.pending) is tuple

    def test_a_backlog_longer_than_the_channel_remembers_is_refused(self) -> None:
        """Thirty-three, written out, against a channel that remembers 32.

        Refused rather than truncated: losing part of a backlog without saying
        so is how a user is told a goal is not queued when it is.
        """
        with pytest.raises(CodecError):
            decode_goal_channel_status(
                {
                    "active": None,
                    "named": None,
                    "pending": [HAND_WRITTEN_RECORD for _ in range(33)],
                }
            )

    @pytest.mark.parametrize(
        "entry", ["g-1", 7, None, ["g-1"]], ids=["string", "int", "null", "array"]
    )
    def test_a_backlog_entry_that_is_not_a_record_is_refused(self, entry: object) -> None:
        """A ``CodecError``, for every shape a JSON array can hold.

        A string and an array both answer ``in`` and so reach a readable
        refusal even without a type check in front of them; a number and a null
        do not, and a decoder that walked straight into them would raise a
        ``TypeError`` the router reports as an unreadable payload with no field
        named. The four together are what pin the check rather than the luck of
        the container.
        """
        with pytest.raises(CodecError):
            decode_goal_channel_status({"active": None, "named": None, "pending": [entry]})

    def test_a_dropped_backlog_is_refused_rather_than_emptied(self) -> None:
        with pytest.raises(CodecError):
            decode_goal_channel_status({"active": None, "named": None})


class TestTheCancellation:
    @pytest.mark.parametrize(
        "cancellation",
        [
            GoalCancellation(
                goal=a_record(state=GoalState.ACTIVE, started_at_ms=1_200), requested=True
            ),
            GoalCancellation(goal=a_succeeded_record(), requested=False),
            GoalCancellation(),
        ],
        ids=["accepted", "already-ended", "unknown-id"],
    )
    def test_a_cancellation_round_trips(self, cancellation: GoalCancellation) -> None:
        restored = decode_goal_cancellation(crossing(encode_goal_cancellation(cancellation)))

        assert restored == cancellation

    def test_requested_is_carried_and_is_not_the_goal_s_state(self) -> None:
        """The channel applies a cancellation on its next tick.

        So an accepted request is routinely reported against a goal that is
        still ``active``, and both facts have to survive the crossing separately.
        """
        running = a_record(state=GoalState.ACTIVE, started_at_ms=1_200)

        body = encode_goal_cancellation(GoalCancellation(goal=running, requested=True))
        restored = decode_goal_cancellation(crossing(body))

        assert body["requested"] is True
        assert restored.requested is True
        assert restored.goal is not None
        assert restored.goal.state is GoalState.ACTIVE

    def test_a_dropped_requested_is_refused_rather_than_false(self) -> None:
        """``False`` is the safe-looking default and the wrong one.

        Told about a goal that is still running, "there was nothing to cancel"
        is the opposite of the truth.
        """
        with pytest.raises(CodecError):
            decode_goal_cancellation({"goal": HAND_WRITTEN_RECORD})

    def test_an_accepted_cancellation_naming_no_goal_is_refused(self) -> None:
        with pytest.raises(CodecError):
            decode_goal_cancellation({"requested": True, "goal": None})


class TestTheParamsBodies:
    def test_a_status_query_may_carry_no_id(self) -> None:
        """Asking with none asks about the channel, and it travels as null.

        An empty string would ask about the goal whose id is ``""``.
        """
        assert encode_goal_query(None) == {"goal_id": None}
        assert decode_goal_query(crossing(encode_goal_query(None))) is None

    def test_a_status_query_carries_the_id_it_was_given(self) -> None:
        assert encode_goal_query("g-1") == {"goal_id": "g-1"}
        assert decode_goal_query(crossing(encode_goal_query("g-1"))) == "g-1"

    def test_an_absent_key_is_the_same_as_a_null_one(self) -> None:
        assert decode_goal_query({}) is None

    def test_a_status_query_that_is_not_a_string_is_refused(self) -> None:
        with pytest.raises(CodecError):
            decode_goal_query({"goal_id": 7})

    def test_a_cancel_body_carries_the_id_under_the_same_key(self) -> None:
        assert encode_goal_id("g-1") == {"goal_id": "g-1"}
        assert decode_goal_id(crossing(encode_goal_id("g-1"))) == "g-1"

    def test_a_cancel_body_without_an_id_is_refused(self) -> None:
        """Unlike a status, a cancel has nothing to mean without one."""
        with pytest.raises(CodecError):
            decode_goal_id({})


class TestNoMessageCarriesAValue:
    @pytest.mark.parametrize(
        "body",
        [
            {"kind": SECRET, "idempotency_key": "goal-1:attempt-1", "params": {}},
            {
                "kind": "train_skill",
                "idempotency_key": "goal-1:attempt-1",
                "params": {"skill": SECRET},
            },
            # A space is outside the key's declared shape, so this one is
            # refused *by the request type*, whose own message quotes what it
            # rejected. The codec has to replace that text rather than chain it.
            {
                "kind": "satisfy_hunger",
                "idempotency_key": f"{SECRET} {CYRILLIC}",
                "params": {},
            },
        ],
        ids=["kind", "skill", "idempotency-key"],
    )
    def test_a_refusal_never_quotes_the_field_it_refused(self, body: dict[str, Any]) -> None:
        """Every string on this link that came from a caller, in turn.

        The message reaches a log line, a traceback and a support bundle before
        any redactor sees it, so the rule is that it names the field and the
        reason and never the value.
        """
        with pytest.raises(CodecError) as refused:
            decode_goal_request(body)

        assert SECRET not in str(refused.value)

    def test_a_refusal_says_which_field_failed(self) -> None:
        """Naming nothing would be as useless as naming the value."""
        with pytest.raises(CodecError) as refused:
            decode_goal_record({**HAND_WRITTEN_RECORD, "key_digest": 7})

        assert "key_digest" in str(refused.value)
