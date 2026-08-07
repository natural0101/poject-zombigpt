"""The Core RPC router: what it answers, what it refuses, and what it must not decide.

Three things are pinned here, and only the first is what a round-trip test would
have caught.

**The wire shapes are compared against hand-written literals.** A key swapped
consistently in both halves of a codec passes every round trip: an encoder that
writes ``{"armed": record.connected}`` paired with a decoder that reads ``armed``
out of ``connected`` reproduces every record exactly and hands the peer a
snapshot with two fields exchanged. So the params in this file are written out by
hand, as JSON, and the assertion is that a *literal* payload reaches the port as
the right argument — ``max_steps`` as the step ceiling and not as the seconds
ceiling, ``level`` as the level filter and not as the component filter.

**Every declared method is answered, and every answer comes from a port.** The
set the router routes is asserted *equal* to ``ALL_METHODS`` — a declared name
that nothing routes is a call that fails on a user's machine, and a routed name
that nothing declares is a capability nobody reviewed. Then the same thirteen
methods are called against a core whose every port raises: each one must come
back ``CORE_REFUSED``, which no route can do by answering out of a cache.

**The router decides nothing**, and that is checked against the module's syntax
tree rather than its behaviour. A capability check, an arming check, a clamped
limit or an idempotency cache cannot be written without a comparison, a boolean
operator, a branch or a loop, and ``TestTheRouterDecidesNothing`` asserts the
module contains none of them.
"""

from __future__ import annotations

import ast
import json
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pz_agent_core.actions import ActionRequest
from pz_agent_core.capabilities.model import CapabilityReport
from pz_agent_core.protocol import (
    ActionName,
    ActionStatus,
    DangerLevel,
    JsonDict,
    Observation,
    ReasonCode,
    SessionMode,
)
from pz_agent_core.rpc import (
    ErrorCode,
    RpcRequest,
    RpcResponse,
    decode_response,
    encode_response,
    issue_token,
)
from pz_agent_core.rpc.transport import Handler, RpcClient, RpcServer, new_address
from pz_agent_core.version import PROTOCOL_VERSION, TARGET_BUILD
from pz_agent_mcp.ports import (
    ActionRecord,
    CoreServices,
    DoctorCheck,
    LogRecord,
    MemoryRecord,
    PlanRecord,
    PlanRequest,
    PlanStepRecord,
    SessionSnapshot,
    StopReport,
)
from pz_agent_mcp.remote import server as server_module
from pz_agent_mcp.remote.methods import ALL_METHODS, Method
from pz_agent_mcp.remote.server import (
    ROUTED_METHODS,
    ArmParams,
    CoreRouter,
    decode_action_status_params,
    decode_arm_params,
    encode_action_status_params,
    encode_arm_params,
)
from tests.fixtures import DEFAULT_SAVE, DEFAULT_SESSION, make_observation
from tests.fixtures.mcp_doubles import HOSTILE_NAME, Doubles, make_report, succeeded_result

SERVER_SOURCE = Path(server_module.__file__)

#: What a core says when it refuses. Asserted to arrive intact: a client that
#: got "the core refused" with no reason cannot tell a missing backup from a
#: dead game, and those have different remedies.
REFUSAL = "make a backup first: run 'pz-agent backup' and confirm it"

#: Shaped like something a redactor would be sorry to have missed. Placed in the
#: *value* position of a field that is then refused, and asserted absent from
#: every message the router produces.
SECRET = "sk-live-51H0-PZ-token-9f8e7d6c5b4a3210"

#: An id the fake action port knows about.
KNOWN_ACTION_ID = "ac-7f1c9a2b"

GRACE = 5.0


# ---------------------------------------------------------------------------
# Hand-written payloads. Never built from an encoder — that is the whole point.
# ---------------------------------------------------------------------------

ARM_PARAMS: JsonDict = {"mode": "AUTONOMOUS", "confirm_backup": True}

SUBMIT_PARAMS: JsonDict = {
    "action": "consume.eat",
    "session_id": DEFAULT_SESSION,
    "idempotency_key": "eat-beans-1",
    "args": {"item": "ref:item:0007"},
    "policy": {"allow_interrupt": False, "max_retries": 0},
    "lease_ms": 30_000,
}

#: The two ceilings are deliberately different numbers, and neither is a
#: plausible value for the other: a router that passed ``max_real_seconds``
#: where ``max_steps`` belongs would be caught by the assertion, not by luck.
PLAN_PARAMS: JsonDict = {
    "goal": "find something to eat",
    "mode": "ASSISTED",
    "max_steps": 4,
    "max_real_seconds": 90,
    "idempotency_key": "plan-eat-1",
}

MEMORY_PARAMS: JsonDict = {"kinds": ["place", "container"], "limit": 3}

#: Three distinct filter values for three distinct filters, so a swap between
#: any two of them changes what the port is asked for.
TAIL_PARAMS: JsonDict = {
    "limit": 5,
    "level": "WARN",
    "component": "action-engine",
    "action_id": "ac-0004",
}

#: One valid params body per declared method. Asserted below to cover
#: ``ALL_METHODS`` exactly, so a method added to the protocol cannot be left
#: without a case here.
VALID_PARAMS: dict[str, JsonDict] = {
    Method.SESSION_STATUS: {},
    Method.SESSION_ARM: ARM_PARAMS,
    Method.SESSION_DISARM: {},
    Method.SESSION_STOP: {},
    Method.OBSERVATION_LATEST: {},
    Method.CAPABILITIES_REPORT: {},
    Method.ACTION_SUBMIT: SUBMIT_PARAMS,
    Method.ACTION_STATUS: {"action_id": KNOWN_ACTION_ID},
    Method.PLAN_EXECUTE: PLAN_PARAMS,
    Method.PLAN_CURRENT: {},
    Method.MEMORY_QUERY: MEMORY_PARAMS,
    Method.DIAGNOSTICS_DOCTOR: {},
    Method.DIAGNOSTICS_TAIL: TAIL_PARAMS,
}

#: The exact object ``session.status`` answers with, written by hand. Pinned
#: here as well as in the codec's own tests because this is the layer a peer
#: actually reads: the router is what puts the snapshot in a result body, and a
#: field it dropped on the way would be invisible to a round trip.
GOLDEN_STATUS_RESULT: JsonDict = {
    "session_id": DEFAULT_SESSION,
    "mode": "ASSISTED",
    "armed": True,
    "connected": True,
    "protocol_version": PROTOCOL_VERSION,
    "build": TARGET_BUILD,
    "save_id": DEFAULT_SAVE,
    "danger_level": "none",
    "observation_seq": 41,
    "capability_revision": 3,
    "game_heartbeat_ok": True,
    "sidecar_heartbeat_ok": True,
    "active_action_id": None,
}

GOLDEN_STOP_RESULT: JsonDict = {"cleared": 2, "disarmed": True, "mode": "OBSERVE"}

#: The action id the fake port mints for the first submission. Written out
#: rather than read back off the double, so that a router answering ``submit``
#: with anything other than the record that port returned is a failure here.
SUBMITTED_ACTION_ID = "00000000-0000-0000-0000-000000000ac2"

#: The plan the fixture's port is running. ``plan.execute`` answers it flat and
#: ``plan.current`` answers it under ``plan`` — the same record, two shapes, and
#: a handler that used the other one would hand the client's decoder a body it
#: refuses on every call.
GOLDEN_PLAN: JsonDict = {
    "plan_id": "plan-1",
    "status": "started",
    "step_index": 1,
    "steps": [
        {
            "index": 0,
            "action": "world.inspect",
            "status": "succeeded",
            "reason_code": "POSTCONDITION_MET",
            "action_id": "ac-0001",
        }
    ],
}

#: The observation the fixture's port holds, so that the entry below is the
#: port's own value under the router's key.
OBSERVATION: Observation = make_observation()

#: **Every** method's result body, one hand-written object each.
#:
#: The round trip through ``encode_response``/``decode_response`` proves a body
#: is JSON; it proves nothing about *which* body. A handler that called the
#: right port and then answered the wrong object — an empty report, a record
#: under the neighbouring key, a doctor with its checks dropped — passes every
#: assertion that only looks at ``ok``, and reaches the client as an answer its
#: codec refuses or, worse, as a clean bill of health nobody ran.
#:
#: Keyed by ``Method`` and asserted to cover ``ALL_METHODS`` exactly, so a
#: method added to the protocol cannot be routed without its answer being
#: written down. One entry is not a literal: an observation is a nested document
#: with its own schema and its own tests, and what this file pins about it is
#: the key it travels under and that it is the port's value.
GOLDEN_RESULTS: dict[str, JsonDict] = {
    Method.SESSION_STATUS: GOLDEN_STATUS_RESULT,
    Method.SESSION_ARM: {**GOLDEN_STATUS_RESULT, "mode": "AUTONOMOUS", "armed": True},
    Method.SESSION_DISARM: {**GOLDEN_STATUS_RESULT, "mode": "OBSERVE", "armed": False},
    Method.SESSION_STOP: GOLDEN_STOP_RESULT,
    Method.OBSERVATION_LATEST: {"observation": OBSERVATION.to_dict()},
    Method.CAPABILITIES_REPORT: {
        "report": {
            "build": TARGET_BUILD,
            "protocol_version": PROTOCOL_VERSION,
            "revision": 3,
            "capabilities": {},
        }
    },
    Method.ACTION_SUBMIT: {
        "action_id": SUBMITTED_ACTION_ID,
        "action": "consume.eat",
        "status": "accepted",
        "idempotency_key": "eat-beans-1",
    },
    Method.ACTION_STATUS: {
        "record": {
            "action_id": KNOWN_ACTION_ID,
            "action": "consume.eat",
            "status": "accepted",
            "idempotency_key": "eat-beans-1",
        }
    },
    Method.PLAN_EXECUTE: GOLDEN_PLAN,
    Method.PLAN_CURRENT: {"plan": GOLDEN_PLAN},
    Method.MEMORY_QUERY: {
        "records": [
            {
                "kind": "place",
                "key": "kitchen",
                "label": HOSTILE_NAME,
                "refs": ["ref:room:2b"],
                "data": {"visits": 3, "distance": 12.5, "safe": True, "last_seen": None},
            }
        ]
    },
    Method.DIAGNOSTICS_DOCTOR: {
        "checks": [{"code": "MOD_INSTALLED", "ok": True, "detail": "found", "remediation": ""}]
    },
    Method.DIAGNOSTICS_TAIL: {
        "records": [
            {
                "timestamp_ms": 1_700_000_000_000,
                "level": "WARN",
                "component": "action-engine",
                "message": HOSTILE_NAME,
                "code": "LEASE_EXPIRED",
                "action_id": "ac-0004",
            }
        ]
    },
}


def request(method: str, params: JsonDict | None = None) -> RpcRequest:
    """One request, with an id a failing assertion can point at."""
    return RpcRequest(id="req-0001", method=method, params=dict(params or {}))


def call(services: CoreServices, method: str, params: JsonDict | None = None) -> RpcResponse:
    return CoreRouter(services)(request(method, params))


@pytest.fixture
def doubles() -> Doubles:
    """A core whose every port answers, with one action already recorded."""
    bundle = Doubles()
    bundle.actions.records[KNOWN_ACTION_ID] = ActionRecord(
        action_id=KNOWN_ACTION_ID,
        action=ActionName.CONSUME_EAT,
        status=ActionStatus.ACCEPTED,
        idempotency_key="eat-beans-1",
    )
    bundle.observations.observation = OBSERVATION
    bundle.plans.record = PlanRecord(
        plan_id="plan-1",
        status=ActionStatus.STARTED,
        step_index=1,
        steps=(
            PlanStepRecord(
                index=0,
                action=ActionName.WORLD_INSPECT,
                status=ActionStatus.SUCCEEDED,
                reason_code=ReasonCode.POSTCONDITION_MET,
                action_id="ac-0001",
            ),
        ),
    )
    bundle.memory.records = (
        MemoryRecord(
            kind="place",
            key="kitchen",
            label=HOSTILE_NAME,
            refs=("ref:room:2b",),
            data={"visits": 3, "distance": 12.5, "safe": True, "last_seen": None},
        ),
    )
    bundle.diagnostics.checks = (
        DoctorCheck(code="MOD_INSTALLED", ok=True, detail="found", remediation=""),
    )
    bundle.diagnostics.lines = (
        LogRecord(
            timestamp_ms=1_700_000_000_000,
            level="WARN",
            component="action-engine",
            message=HOSTILE_NAME,
            code="LEASE_EXPIRED",
            action_id="ac-0004",
        ),
    )
    return bundle


@pytest.fixture
def services(doubles: Doubles) -> CoreServices:
    return doubles.services


# ---------------------------------------------------------------------------
# A core that refuses everything, for the CORE_REFUSED half of the contract.
# ---------------------------------------------------------------------------


class _Refuses:
    """Every port method, and every one of them raises.

    One class rather than seven because the router must not care which port it
    is: a refusal is a refusal wherever it came from, and the point of the test
    that uses this is that all thirteen methods reach *a* port.
    """

    def status(self, action_id: str | None = None) -> Any:
        raise RuntimeError(REFUSAL)

    def arm(self, mode: SessionMode, *, confirm_backup: bool) -> SessionSnapshot:
        raise RuntimeError(REFUSAL)

    def disarm(self) -> SessionSnapshot:
        raise RuntimeError(REFUSAL)

    def stop(self) -> StopReport:
        raise RuntimeError(REFUSAL)

    def latest(self) -> Observation | None:
        raise RuntimeError(REFUSAL)

    def report(self) -> CapabilityReport:
        raise RuntimeError(REFUSAL)

    def submit(self, action_request: ActionRequest) -> ActionRecord:
        raise RuntimeError(REFUSAL)

    def execute(self, plan_request: PlanRequest) -> PlanRecord:
        raise RuntimeError(REFUSAL)

    def current(self) -> PlanRecord | None:
        raise RuntimeError(REFUSAL)

    def query(self, *, kinds: Sequence[str], limit: int) -> Sequence[MemoryRecord]:
        raise RuntimeError(REFUSAL)

    def doctor(self) -> Sequence[DoctorCheck]:
        raise RuntimeError(REFUSAL)

    def tail(
        self,
        *,
        limit: int,
        level: str | None = None,
        component: str | None = None,
        action_id: str | None = None,
    ) -> Sequence[LogRecord]:
        raise RuntimeError(REFUSAL)


def refusing_services() -> CoreServices:
    port = _Refuses()
    return CoreServices(
        session=port,
        observations=port,
        capabilities=port,
        actions=port,
        plans=port,
        memory=port,
        diagnostics=port,
    )


class _NarratingSession:
    """A session port whose every method answers a snapshot naming itself.

    The doubles answer ``status()`` with the object ``arm()`` has just stored,
    which is honest for a fake and blind to one mistake: a handler that arms and
    then reports ``status()`` rather than what ``arm()`` returned. Against a real
    core those are two different reads with a session's worth of time between
    them, and the caller would be handed a snapshot from after its own call.
    """

    def _named(self, who: str) -> SessionSnapshot:
        return SessionSnapshot(
            session_id=who,
            mode=SessionMode.OBSERVE,
            armed=False,
            connected=True,
            protocol_version=PROTOCOL_VERSION,
        )

    def status(self) -> SessionSnapshot:
        return self._named("from-status")

    def arm(self, mode: SessionMode, *, confirm_backup: bool) -> SessionSnapshot:
        return self._named("from-arm")

    def disarm(self) -> SessionSnapshot:
        return self._named("from-disarm")

    def stop(self) -> StopReport:
        return StopReport(cleared=0, disarmed=True, mode=SessionMode.OBSERVE)


class _ExplodingParams(dict[str, Any]):
    """A params object whose reader blows up in a way no codec expects.

    ``CodecError`` is the failure a codec is written to produce, and its message
    is safe to repeat. This one is everything else: it raises a plain
    ``RuntimeError`` whose text carries a secret, which is what a reader that
    quoted what it read would look like.
    """

    def __contains__(self, key: object) -> bool:
        raise RuntimeError(f"reader failed while holding {SECRET}")

    def get(self, key: str, default: Any = None) -> Any:
        raise RuntimeError(f"reader failed while holding {SECRET}")


# ---------------------------------------------------------------------------


class TestTheRoutedSet:
    def test_the_routed_set_equals_the_declared_set(self) -> None:
        """Not a subset and not a superset.

        A declared name nothing routes is a call a client makes that no answer
        ever comes back for; a routed name nothing declares is a capability that
        went through no review. Both directions are named separately so a
        failure says which one happened.
        """
        assert ROUTED_METHODS == ALL_METHODS
        assert not (ALL_METHODS - ROUTED_METHODS), "declared but not routed"
        assert not (ROUTED_METHODS - ALL_METHODS), "routed but not declared"

    def test_every_declared_method_has_a_case_in_this_file(self) -> None:
        """The table below is what makes the rest of the suite exhaustive."""
        assert set(VALID_PARAMS) == ALL_METHODS

    @pytest.mark.parametrize("method", sorted(ALL_METHODS))
    def test_every_declared_method_answers(self, services: CoreServices, method: str) -> None:
        """Routed, not merely listed: each name produces a successful answer."""
        response = call(services, method, VALID_PARAMS[method])

        assert response.ok is True, f"{method}: {response.error_code} {response.error_message}"
        assert response.error_code is None
        assert response.id == "req-0001"

    @pytest.mark.parametrize("method", sorted(ALL_METHODS))
    def test_every_method_reaches_a_port(self, method: str) -> None:
        """A core that refuses everything must make every method refuse.

        This is what a route cannot fake. A handler that answered from a
        constant, a cache or a default would come back ``ok`` here, and the
        thing it was hiding — that it never asked the core — would ship.
        """
        response = call(refusing_services(), method, VALID_PARAMS[method])

        assert response.ok is False
        assert response.error_code == ErrorCode.CORE_REFUSED
        assert REFUSAL in response.error_message

    @pytest.mark.parametrize("method", sorted(ALL_METHODS))
    def test_every_answer_survives_the_envelope(self, services: CoreServices, method: str) -> None:
        """The result body is JSON the wire can carry and read back.

        A handler that put a tuple, an enum member or a dataclass in a result
        would pass every assertion above and fail here, which is where it would
        have failed on a user's machine.
        """
        response = call(services, method, VALID_PARAMS[method])

        restored = decode_response(encode_response(response))

        assert restored.ok is True
        assert restored.id == response.id
        assert restored.result == json.loads(json.dumps(response.result))


class TestTheThreeFailures:
    """``UNKNOWN_METHOD``, ``MALFORMED`` and ``CORE_REFUSED`` are three answers.

    A client acts differently on each: reinstall, fix the call, read the core's
    reason. A router that collapsed any two of them would leave the client with
    a refusal it cannot act on.
    """

    @pytest.mark.parametrize(
        "method",
        [
            "session.statu",
            "session.status ",
            "SESSION.STATUS",
            "safety.stop",
            "observation.snapshot",
            "x",
        ],
    )
    def test_an_unknown_method_is_refused_and_never_raises(
        self, services: CoreServices, method: str
    ) -> None:
        """Including names that are one character from a real one.

        ``safety.stop`` is in the mod's own action vocabulary and is not a
        method on this link, which is exactly the confusion a client author
        makes; it gets the same clear refusal as a typo.
        """
        response = call(services, method)

        assert response.ok is False
        assert response.error_code == ErrorCode.UNKNOWN_METHOD
        assert response.id == "req-0001"

    def test_an_unknown_method_touches_no_port(self, doubles: Doubles) -> None:
        call(doubles.services, "session.statu")

        assert doubles.session.arms == []
        assert doubles.session.disarms == 0
        assert doubles.session.stops == 0
        assert doubles.actions.submitted == []
        assert doubles.memory.calls == []
        assert doubles.diagnostics.tail_calls == []

    @pytest.mark.parametrize(
        ("method", "params"),
        [
            (Method.SESSION_ARM, {}),
            (Method.SESSION_ARM, {"mode": "AUTONOMOUS"}),
            (Method.SESSION_ARM, {"confirm_backup": True}),
            (Method.SESSION_ARM, {"mode": "SUPERUSER", "confirm_backup": True}),
            (Method.SESSION_ARM, {"mode": "AUTONOMOUS", "confirm_backup": "yes"}),
            (Method.ACTION_STATUS, {}),
            (Method.ACTION_STATUS, {"action_id": 7}),
            (Method.ACTION_SUBMIT, {**SUBMIT_PARAMS, "action": "consume.inhale"}),
            (Method.ACTION_SUBMIT, {**SUBMIT_PARAMS, "policy": {"max_retries": 0}}),
            (Method.ACTION_SUBMIT, {**SUBMIT_PARAMS, "lease_ms": 99}),
            (Method.PLAN_EXECUTE, {k: v for k, v in PLAN_PARAMS.items() if k != "max_steps"}),
            (Method.PLAN_EXECUTE, {**PLAN_PARAMS, "mode": "GODMODE"}),
            (Method.MEMORY_QUERY, {"kinds": ["place"]}),
            (Method.MEMORY_QUERY, {"kinds": "place", "limit": 3}),
            (Method.MEMORY_QUERY, {"kinds": ["place"], "limit": True}),
            (Method.DIAGNOSTICS_TAIL, {"level": "WARN"}),
            (Method.DIAGNOSTICS_TAIL, {"limit": 5, "level": 3}),
        ],
    )
    def test_parameters_that_do_not_decode_are_malformed(
        self, doubles: Doubles, method: str, params: JsonDict
    ) -> None:
        """The caller's bug, and never the core's decision.

        ``MALFORMED`` says "fix the call" and ``CORE_REFUSED`` says "read what
        the core said". A client handed the second for the first would show the
        user a refusal that has nothing to do with their game.
        """
        response = call(doubles.services, method, params)

        assert response.ok is False
        assert response.error_code == ErrorCode.MALFORMED
        assert method in response.error_message

    @pytest.mark.parametrize(
        ("method", "params"),
        [
            (Method.SESSION_ARM, {"mode": "AUTONOMOUS", "confirm_backup": "yes"}),
            (Method.ACTION_SUBMIT, {**SUBMIT_PARAMS, "lease_ms": 99}),
            (Method.MEMORY_QUERY, {"kinds": ["place"]}),
            (Method.DIAGNOSTICS_TAIL, {"level": "WARN"}),
        ],
    )
    def test_a_malformed_call_never_reaches_the_core(
        self, doubles: Doubles, method: str, params: JsonDict
    ) -> None:
        """Refused before the port, so a bad call cannot half-happen."""
        call(doubles.services, method, params)

        assert doubles.session.arms == []
        assert doubles.actions.submitted == []
        assert doubles.memory.calls == []
        assert doubles.diagnostics.tail_calls == []

    def test_a_malformed_call_is_malformed_even_when_the_core_would_refuse(self) -> None:
        """The two failures are decided in order, not by whichever fires.

        A router that called the port first, or that caught both in one handler,
        would report a core refusal for parameters the core never saw.
        """
        response = call(refusing_services(), Method.SESSION_ARM, {"mode": "AUTONOMOUS"})

        assert response.error_code == ErrorCode.MALFORMED

    def test_a_port_that_raises_becomes_core_refused_with_its_message(
        self, doubles: Doubles
    ) -> None:
        """Carried, not summarised. The reason is the actionable part."""
        doubles.session.arm_error = RuntimeError(REFUSAL)

        response = call(doubles.services, Method.SESSION_ARM, ARM_PARAMS)

        assert response.ok is False
        assert response.error_code == ErrorCode.CORE_REFUSED
        assert REFUSAL in response.error_message
        assert "RuntimeError" in response.error_message

    def test_a_port_raising_a_value_error_is_still_core_refused(self, doubles: Doubles) -> None:
        """Not confused with a decode failure, which is also a ``ValueError``.

        :class:`~pz_agent_mcp.remote.codec.CodecError` is a ``ValueError``, so a
        router that told the two apart by exception type instead of by phase
        would report this as the caller's bug.
        """
        doubles.actions.submit_error = ValueError(REFUSAL)

        response = call(doubles.services, Method.ACTION_SUBMIT, SUBMIT_PARAMS)

        assert response.error_code == ErrorCode.CORE_REFUSED
        assert REFUSAL in response.error_message

    def test_the_router_never_raises_whatever_the_core_does(self, doubles: Doubles) -> None:
        """Including the exceptions a handler is not supposed to see.

        The transport turns a raised handler into ``CORE_REFUSED`` itself, which
        is right for a port and wrong for everything else; answering here keeps
        the classification with the layer that knows which phase failed.
        """
        doubles.actions.submit_error = KeyError("no such adapter")

        response = call(doubles.services, Method.ACTION_SUBMIT, SUBMIT_PARAMS)

        assert response.error_code == ErrorCode.CORE_REFUSED

    @pytest.mark.parametrize(
        ("method", "params", "code"),
        [
            ("session.statu", {}, ErrorCode.UNKNOWN_METHOD),
            (Method.SESSION_ARM, {"mode": "AUTONOMOUS"}, ErrorCode.MALFORMED),
            (Method.SESSION_ARM, ARM_PARAMS, ErrorCode.CORE_REFUSED),
        ],
    )
    def test_every_refusal_is_addressed_to_the_request_it_answers(
        self, method: str, params: JsonDict, code: str
    ) -> None:
        """All three failures carry the caller's own id back.

        One request per connection, so :class:`~pz_agent_core.rpc.RpcClient`
        reads a mismatched id as an answer belonging to something else and
        refuses it. A refusal that lost the id would therefore not reach the
        caller as a refusal at all: it would arrive as "the answer is not one
        this build can read", which is the third failure kind wearing the
        clothes of the other two, and the remedy it points at is a reinstall
        rather than the backup the core actually asked for.
        """
        response = call(refusing_services(), method, params)

        assert response.error_code == code
        assert response.id == "req-0001"

    @pytest.mark.parametrize(
        ("method", "params", "field"),
        [
            (Method.SESSION_ARM, {"mode": "AUTONOMOUS"}, "confirm_backup"),
            (Method.ACTION_STATUS, {}, "action_id"),
            (Method.MEMORY_QUERY, {"kinds": ["place"]}, "limit"),
            (Method.DIAGNOSTICS_TAIL, {"level": "WARN"}, "limit"),
            (Method.PLAN_EXECUTE, {k: v for k, v in PLAN_PARAMS.items() if k != "goal"}, "goal"),
        ],
    )
    def test_a_malformed_message_keeps_the_field_the_codec_named(
        self, services: CoreServices, method: str, params: JsonDict, field: str
    ) -> None:
        """ "Fix the call" is only actionable if it says which part.

        A :class:`~pz_agent_mcp.remote.codec.CodecError` is written to name the
        field and the reason and never the value, which is exactly why this
        module repeats it. Repeating the method alone would be safe and useless:
        the caller would know which call to fix and nothing about what is wrong
        with it, and ``session.arm`` has five ways to be malformed.
        """
        response = call(services, method, params)

        assert response.error_code == ErrorCode.MALFORMED
        assert field in response.error_message

    def test_a_reader_that_fails_unexpectedly_is_malformed_and_says_nothing(self) -> None:
        """Nothing was called, so it is not the core's refusal.

        And the message is the method and the phase only: this exception is not
        a ``CodecError``, so nothing promises its text is free of what it read.
        """
        response = CoreRouter(refusing_services())(
            RpcRequest(id="req-0001", method=Method.ACTION_STATUS, params=_ExplodingParams())
        )

        assert response.error_code == ErrorCode.MALFORMED
        assert SECRET not in response.error_message


class TestMessagesQuoteNothing:
    """A refusal names the field and the reason. This text reaches a bug report."""

    @pytest.mark.parametrize(
        ("method", "params"),
        [
            (Method.ACTION_STATUS, {"action_id": {"token": SECRET}}),
            (Method.MEMORY_QUERY, {"kinds": SECRET, "limit": 3}),
            (Method.MEMORY_QUERY, {"kinds": ["place"], "limit": SECRET}),
            (Method.DIAGNOSTICS_TAIL, {"limit": 5, "level": {"held": SECRET}}),
            (Method.SESSION_ARM, {"mode": "AUTONOMOUS", "confirm_backup": SECRET}),
            (Method.PLAN_EXECUTE, {**PLAN_PARAMS, "max_steps": SECRET}),
        ],
    )
    def test_a_rejected_value_is_never_in_the_message(
        self, services: CoreServices, method: str, params: JsonDict
    ) -> None:
        response = call(services, method, params)

        assert response.error_code == ErrorCode.MALFORMED
        assert SECRET not in response.error_message

    def test_an_unknown_method_message_quotes_no_parameters(self, services: CoreServices) -> None:
        """The name is named; what came with it is not.

        Nothing decoded these parameters — the name was refused before any
        reader saw them — so the router knows nothing about what is in them, and
        a message that quoted them would put an unread payload into a log line
        and a bug report. The method name is the one thing this message needs,
        and the envelope caps it at 64 characters before the router sees it.
        """
        response = call(services, "session.statu", {"token": SECRET, "args": {"item": SECRET}})

        assert response.error_code == ErrorCode.UNKNOWN_METHOD
        assert SECRET not in response.error_message

    def test_an_unknown_method_message_says_what_to_do(self, services: CoreServices) -> None:
        """A refusal that only says "no" leaves the user with nothing to try."""
        response = call(services, "session.statu")

        assert "reinstall" in response.error_message


class TestParametersReachThePortsUnchanged:
    """Hand-written payload in, right argument out.

    Every payload in this class is a literal. Comparing against the encoder's
    own output would agree with any key the two halves swapped together.
    """

    def test_arm_params_carry_the_mode_and_the_confirmation(self, doubles: Doubles) -> None:
        call(doubles.services, Method.SESSION_ARM, {"mode": "AUTONOMOUS", "confirm_backup": True})

        assert doubles.session.arms == [(SessionMode.AUTONOMOUS, True)]

    def test_a_refused_backup_stays_refused(self, doubles: Doubles) -> None:
        """``False`` is carried, not treated as absent.

        The router has no opinion about whether a session may arm without a
        backup — the core decides — but it must hand over the answer the user
        actually gave.
        """
        call(doubles.services, Method.SESSION_ARM, {"mode": "ASSISTED", "confirm_backup": False})

        assert doubles.session.arms == [(SessionMode.ASSISTED, False)]

    def test_the_arm_params_pair_agrees_with_the_literal(self) -> None:
        """The encoder the client half imports writes exactly this object."""
        assert encode_arm_params(ArmParams(mode=SessionMode.AUTONOMOUS, confirm_backup=True)) == {
            "mode": "AUTONOMOUS",
            "confirm_backup": True,
        }
        assert decode_arm_params(ARM_PARAMS) == ArmParams(
            mode=SessionMode.AUTONOMOUS, confirm_backup=True
        )

    def test_the_arm_mode_comes_back_as_a_member_and_not_a_string(self) -> None:
        """``SessionMode`` is a ``StrEnum``, so equality alone proves nothing.

        ``SessionMode.AUTONOMOUS == "AUTONOMOUS"`` is ``True``, and a decoder
        that handed back the raw string would satisfy every assertion above
        while giving the port something whose ``.value`` does not exist.
        """
        assert type(decode_arm_params(ARM_PARAMS).mode) is SessionMode

    def test_the_action_id_pair_agrees_with_the_literal(self) -> None:
        assert encode_action_status_params(KNOWN_ACTION_ID) == {"action_id": KNOWN_ACTION_ID}
        assert decode_action_status_params({"action_id": KNOWN_ACTION_ID}) == KNOWN_ACTION_ID

    def test_the_action_id_reaches_the_port(self, doubles: Doubles) -> None:
        """Under ``action_id`` and no other key."""
        response = call(doubles.services, Method.ACTION_STATUS, {"action_id": KNOWN_ACTION_ID})

        assert response.result["record"]["action_id"] == KNOWN_ACTION_ID

    def test_a_submitted_request_arrives_field_for_field(self, doubles: Doubles) -> None:
        call(doubles.services, Method.ACTION_SUBMIT, SUBMIT_PARAMS)

        submitted = doubles.actions.submitted[0]
        assert submitted.action is ActionName.CONSUME_EAT
        assert submitted.session_id == DEFAULT_SESSION
        assert submitted.idempotency_key == "eat-beans-1"
        assert submitted.args == {"item": "ref:item:0007"}
        assert submitted.lease_ms == 30_000
        # The policy is the field a silence would grant: `from_dict` defaults an
        # absent `allow_interrupt` to True and an absent `max_retries` to 1, so
        # a dropped key here would hand the core an interrupt and a retry that
        # this payload explicitly withheld.
        assert submitted.policy.allow_interrupt is False
        assert submitted.policy.max_retries == 0

    def test_the_two_plan_ceilings_do_not_change_places(self, doubles: Doubles) -> None:
        """Four steps and ninety seconds, in that order and not the other one."""
        call(doubles.services, Method.PLAN_EXECUTE, PLAN_PARAMS)

        planned = doubles.plans.requests[0]
        assert planned.max_steps == 4
        assert planned.max_real_seconds == 90
        assert planned.goal == "find something to eat"
        assert planned.mode is SessionMode.ASSISTED
        assert planned.idempotency_key == "plan-eat-1"

    def test_the_memory_query_carries_its_kinds_and_its_limit(self, doubles: Doubles) -> None:
        call(doubles.services, Method.MEMORY_QUERY, MEMORY_PARAMS)

        assert doubles.memory.calls == [(("place", "container"), 3)]

    def test_an_empty_kinds_list_stays_empty(self, doubles: Doubles) -> None:
        """Empty means "every kind" to the port, and the router does not fill it in."""
        call(doubles.services, Method.MEMORY_QUERY, {"kinds": [], "limit": 2})

        assert doubles.memory.calls == [((), 2)]

    def test_the_three_tail_filters_do_not_change_places(self, doubles: Doubles) -> None:
        call(doubles.services, Method.DIAGNOSTICS_TAIL, TAIL_PARAMS)

        assert doubles.diagnostics.tail_calls == [
            {
                "limit": 5,
                "level": "WARN",
                "component": "action-engine",
                "action_id": "ac-0004",
            }
        ]

    def test_an_absent_tail_filter_arrives_as_none(self, doubles: Doubles) -> None:
        """``None`` is "do not filter", and an empty string is a filter that matches nothing."""
        call(doubles.services, Method.DIAGNOSTICS_TAIL, {"limit": 2})

        assert doubles.diagnostics.tail_calls == [
            {"limit": 2, "level": None, "component": None, "action_id": None}
        ]


class TestAnswersMatchTheLiteralShapes:
    """The result bodies, pinned against objects written out by hand."""

    def test_every_declared_method_has_a_pinned_body(self) -> None:
        """Thirteen methods, thirteen answers written down.

        Without this the table below can be *nearly* complete and the gap is
        invisible: the method still answers, still round trips, still refuses
        when the core refuses, and only its body goes unread.
        """
        assert set(GOLDEN_RESULTS) == ALL_METHODS

    @pytest.mark.parametrize("method", sorted(ALL_METHODS))
    def test_every_answer_is_the_body_written_down_here(
        self, services: CoreServices, method: str
    ) -> None:
        """Not merely ``ok``, and not merely JSON: this exact object.

        The failures this catches are the ones every other assertion in the file
        passes over — a handler that calls its port and then answers an empty
        object, the checks it dropped, or the neighbouring codec's shape
        (``action.submit`` answers a record flat and ``action.status`` answers
        one under ``record``; ``plan.execute`` and ``plan.current`` are the same
        pair). Each of those reaches a user as an answer the client's decoder
        refuses on every single call, or — for the doctor — as an environment
        with nothing wrong with it.
        """
        response = call(services, method, VALID_PARAMS[method])

        assert response.result == GOLDEN_RESULTS[method]

    def test_the_capability_report_carries_the_capabilities_the_port_reported(
        self, doubles: Doubles
    ) -> None:
        """The one body that decides which tools exist.

        The table above pins the empty report, and an empty report is also what
        a handler that dropped the port's answer would produce. So this one is
        driven with capabilities in it: the states the core granted are the
        states that cross, since a tool published on a capability the game never
        confirmed is the failure the whole capability model exists to prevent.
        """
        doubles.capabilities.report_value = make_report(
            usable=["consume_eat"], unsupported=["vehicle_drive"]
        )

        response = call(doubles.services, Method.CAPABILITIES_REPORT)

        published = response.result["report"]["capabilities"]
        assert set(published) == {"consume_eat", "vehicle_drive"}
        assert published["consume_eat"]["state"] == "available_unverified"
        assert published["vehicle_drive"]["state"] == "unsupported"

    @pytest.mark.parametrize(
        ("method", "params", "expected"),
        [
            (Method.SESSION_STATUS, {}, "from-status"),
            (Method.SESSION_ARM, ARM_PARAMS, "from-arm"),
            (Method.SESSION_DISARM, {}, "from-disarm"),
        ],
    )
    def test_each_session_method_answers_what_its_own_call_returned(
        self, doubles: Doubles, method: str, params: JsonDict, expected: str
    ) -> None:
        """Arming answers the arm, not a second look at the session."""
        services = replace(doubles.services, session=_NarratingSession())

        response = call(services, method, params)

        assert response.result["session_id"] == expected

    def test_session_status_answers_the_golden_object(self, services: CoreServices) -> None:
        response = call(services, Method.SESSION_STATUS)

        assert response.result == GOLDEN_STATUS_RESULT

    def test_session_stop_answers_the_golden_object(self, services: CoreServices) -> None:
        response = call(services, Method.SESSION_STOP)

        assert response.result == GOLDEN_STOP_RESULT

    def test_the_raw_save_id_crosses_the_link(self, services: CoreServices) -> None:
        """Not digested here.

        ``save_scope`` is applied by :mod:`pz_agent_mcp.router`, at the boundary
        that faces the tool caller. A router that digested it on this side would
        move a security decision out of the module that owns it and leave the
        other half unable to compute the same digest.
        """
        result = call(services, Method.SESSION_STATUS).result

        assert result["save_id"] == DEFAULT_SAVE
        assert "save_scope" not in result

    def test_an_absent_observation_is_a_missing_key_and_not_an_empty_one(
        self, doubles: Doubles
    ) -> None:
        """ "Nothing yet" and "unreadable" must not have the same spelling."""
        doubles.observations.observation = None

        response = call(doubles.services, Method.OBSERVATION_LATEST)

        assert response.ok is True
        assert response.result == {}

    def test_an_observation_travels_under_its_own_key(self, doubles: Doubles) -> None:
        observation: Observation = make_observation()
        doubles.observations.observation = observation

        response = call(doubles.services, Method.OBSERVATION_LATEST)

        assert response.result == {"observation": observation.to_dict()}

    def test_an_unknown_action_id_is_a_successful_empty_answer(self, doubles: Doubles) -> None:
        """ "The core has no such action" is an answer, not a failure."""
        response = call(doubles.services, Method.ACTION_STATUS, {"action_id": "ac-nothing"})

        assert response.ok is True
        assert response.result == {}

    def test_no_plan_is_a_successful_empty_answer(self, doubles: Doubles) -> None:
        doubles.plans.record = None

        response = call(doubles.services, Method.PLAN_CURRENT)

        assert response.ok is True
        assert response.result == {}

    def test_an_empty_doctor_still_carries_its_key(self, doubles: Doubles) -> None:
        """A clean bill of health is an empty array, never a missing one."""
        doubles.diagnostics.checks = ()

        response = call(doubles.services, Method.DIAGNOSTICS_DOCTOR)

        assert response.result == {"checks": []}

    def test_an_empty_tail_still_carries_its_key(self, doubles: Doubles) -> None:
        doubles.diagnostics.lines = ()

        response = call(doubles.services, Method.DIAGNOSTICS_TAIL, {"limit": 5})

        assert response.result == {"records": []}

    def test_an_empty_memory_result_still_carries_its_key(self, doubles: Doubles) -> None:
        doubles.memory.records = ()

        response = call(doubles.services, Method.MEMORY_QUERY, MEMORY_PARAMS)

        assert response.result == {"records": []}

    def test_a_succeeded_action_carries_its_evidence(self, doubles: Doubles) -> None:
        """The honesty rule is the record's, and the router does not soften it.

        ``ActionRecord`` refuses to exist as ``SUCCEEDED`` without
        ``POSTCONDITION_MET`` and observed evidence, so a success on this link
        always arrives with the proof attached.
        """
        doubles.actions.finish(KNOWN_ACTION_ID, succeeded_result(ActionName.CONSUME_EAT))

        response = call(doubles.services, Method.ACTION_STATUS, {"action_id": KNOWN_ACTION_ID})

        record = response.result["record"]
        assert record["status"] == "succeeded"
        assert record["result"]["reason_code"] == ReasonCode.POSTCONDITION_MET.value
        assert record["result"]["evidence"]["observed"]

    def test_a_plan_answers_with_its_steps_in_order(self, doubles: Doubles) -> None:
        response = call(doubles.services, Method.PLAN_CURRENT)

        plan = response.result["plan"]
        assert plan["plan_id"] == "plan-1"
        assert plan["step_index"] == 1
        assert [step["index"] for step in plan["steps"]] == [0]
        assert plan["steps"][0]["action"] == ActionName.WORLD_INSPECT.value


class TestGameTextIsCarriedAndNotQuarantinedHere:
    """The quarantine is the outward boundary's, and doing it twice loses the text.

    Both ends of this link are ours, on one machine. The MCP boundary that faces
    a tool caller — :mod:`pz_agent_mcp.router`, with :mod:`pz_agent_mcp.scrub` —
    is what marks game text as untrusted, and it runs in the process on the
    other end of this pipe. Scrubbing here would scrub text a redactor has
    already seen, and then neither half could say what the game actually wrote.
    """

    def test_a_hostile_memory_label_crosses_verbatim(self, services: CoreServices) -> None:
        response = call(services, Method.MEMORY_QUERY, MEMORY_PARAMS)

        assert response.result["records"][0]["label"] == HOSTILE_NAME

    def test_a_hostile_log_message_crosses_verbatim(self, services: CoreServices) -> None:
        response = call(services, Method.DIAGNOSTICS_TAIL, TAIL_PARAMS)

        assert response.result["records"][0]["message"] == HOSTILE_NAME

    def test_the_router_imports_no_redactor(self) -> None:
        """Stated against the source, because the absence is the guarantee."""
        imported = _imported_modules(_module_tree())

        assert not [name for name in imported if "scrub" in name]
        assert not [name for name in imported if "compact" in name]


class TestTheRouterDecidesNothing:
    """No capability check, no arming check, no idempotency, no clamp.

    All four exist already — in the core, and in :mod:`pz_agent_mcp.router` on
    the far side of this link. A copy here would be a second implementation of a
    safety rule, and the copy that drifts is never the one anybody is watching.

    The checks below read the module's syntax tree rather than its behaviour,
    because behaviour can only show that a rule is absent for the inputs a test
    happened to try.
    """

    def test_the_module_contains_no_decision(self) -> None:
        """No branch, no loop, no comparison, no boolean operator.

        A policy check cannot be written without one of them, so this is the
        cheapest true statement about what the module does not do. Dispatch is a
        dictionary lookup and each handler is a straight line, so the module
        needs none of these to do its job.
        """
        forbidden = (
            ast.If,
            ast.IfExp,
            ast.While,
            ast.For,
            ast.AsyncFor,
            ast.Compare,
            ast.BoolOp,
            ast.Match,
        )
        found = sorted(
            {
                type(node).__name__
                for node in ast.walk(_module_tree())
                if isinstance(node, forbidden)
            }
        )

        assert found == [], f"the router grew a decision: {found}"

    @pytest.mark.parametrize(
        "name",
        [
            "armed",
            "is_armed",
            "danger_level",
            "MUTATING_MODES",
            "SELF_DIRECTED_MODES",
            "READ_ONLY_ACTIONS",
            "ALWAYS_ALLOWED_ACTIONS",
            "CapabilityState",
            "RiskClass",
            "usable",
            "withheld_tools",
            "scrub_text",
            "scrub_payload",
            "save_scope",
            "redact_text",
            "IdempotencyCache",
            "MAX_MEMORY_RESULTS",
            "MAX_LOG_RECORDS",
            "min",
            "max",
        ],
    )
    def test_the_module_names_nothing_that_belongs_to_policy(self, name: str) -> None:
        """Identifiers, not substrings.

        Matched against ``Name`` ids and attribute names so that
        ``services.capabilities.report()`` — a port call — is not mistaken for a
        capability *check*.
        """
        tree = _module_tree()
        used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        used |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

        assert name not in used

    def test_the_module_imports_no_policy(self) -> None:
        """The catalogue, the validator and the idempotency cache stay outside.

        Each of them is a decision the boundary makes about what a caller may
        do, and each already runs in the MCP process. Importing one here would
        move it to a side that cannot see the tool call it belongs to.
        """
        imported = _imported_modules(_module_tree())
        banned = (
            "pz_agent_mcp.catalog",
            "pz_agent_mcp.validation",
            "pz_agent_mcp.idempotency",
            "pz_agent_mcp.router",
            "pz_agent_mcp.envelope",
            "pz_agent_mcp.scrub",
        )

        assert [name for name in imported if name.startswith(banned)] == []

    def test_every_method_name_comes_from_the_methods_module(self) -> None:
        """No name is written twice.

        A method spelled here as well as in ``methods.py`` is a name that can
        drift, and the drift shows up as ``UNKNOWN_METHOD`` on a user's machine
        rather than as a failure in CI.
        """
        tree = _module_tree()
        from_constants = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }

        assert not (from_constants & ALL_METHODS)

    def test_the_dispatch_table_is_keyed_by_the_methods_module(self) -> None:
        """And by all thirteen of its constants."""
        tree = _module_tree()
        referenced = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "Method"
        }

        assert {getattr(Method, name) for name in referenced} == ALL_METHODS

    def test_the_router_holds_no_state(self) -> None:
        """Two identical calls reach the port twice.

        Deduplicating them is the idempotency rule, the core owns it, and a
        cache here would answer the second call with the first one's result even
        after the core had moved on.
        """
        bundle = Doubles()

        call(bundle.services, Method.ACTION_SUBMIT, SUBMIT_PARAMS)
        call(bundle.services, Method.ACTION_SUBMIT, SUBMIT_PARAMS)

        assert len(bundle.actions.submitted) == 2

    def test_the_answer_is_not_cached_between_calls(self, doubles: Doubles) -> None:
        router = CoreRouter(doubles.services)

        first = router(request(Method.SESSION_STATUS)).result["mode"]
        doubles.session.snapshot = replace(doubles.session.snapshot, mode=SessionMode.OBSERVE)
        second = router(request(Method.SESSION_STATUS)).result["mode"]

        assert (first, second) == ("ASSISTED", "OBSERVE")

    def test_an_unarmed_session_does_not_stop_a_submit(self, doubles: Doubles) -> None:
        """The arming rule is the core's, and it is the core that must apply it.

        A copy here would be a second gate: one of them would be updated when
        the rule changed and the other would not, and the failure would be a
        submit accepted or refused for a reason nobody wrote down.
        """
        doubles.session.snapshot = replace(
            doubles.session.snapshot,
            mode=SessionMode.OBSERVE,
            armed=False,
            danger_level=DangerLevel.CRITICAL,
        )

        response = call(doubles.services, Method.ACTION_SUBMIT, SUBMIT_PARAMS)

        assert response.ok is True
        assert len(doubles.actions.submitted) == 1

    def test_an_unsupported_capability_does_not_stop_a_submit(self, doubles: Doubles) -> None:
        """Publishing a tool is the catalogue's decision, made in the MCP process."""
        doubles.capabilities.report_value = make_report(unsupported=["consume_eat"])

        response = call(doubles.services, Method.ACTION_SUBMIT, SUBMIT_PARAMS)

        assert response.ok is True

    def test_a_limit_above_the_published_ceiling_is_carried_not_clamped(
        self, doubles: Doubles
    ) -> None:
        """The ceiling is applied before the call, by the layer that published it."""
        call(doubles.services, Method.MEMORY_QUERY, {"kinds": [], "limit": 10_000})

        assert doubles.memory.calls == [((), 10_000)]


class TestItIsAServerHandler:
    """Usable as-is by :class:`~pz_agent_core.rpc.transport.RpcServer`."""

    def test_the_router_satisfies_the_handler_type(self, services: CoreServices) -> None:
        """Checked by mypy through the annotation, and by pytest through the call."""
        handler: Handler = CoreRouter(services)

        assert handler(request(Method.SESSION_STATUS)).ok is True

    def test_a_real_call_over_a_real_socket_reaches_the_ports(
        self, services: CoreServices, tmp_path: Path
    ) -> None:
        """End to end: two processes' worth of machinery, one thread apart.

        Every wait is bounded — the client has its own deadline and the join
        below has one too — so a wedged server fails this test instead of
        hanging the suite.
        """
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        key = issue_token(runtime)
        server = RpcServer(new_address(runtime), authkey=key, handler=CoreRouter(services))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = RpcClient(server.descriptor(), authkey=key, deadline=GRACE)

            answered = client.call(Method.SESSION_STATUS)
            refused = client.call("session.statu")

            assert answered.ok is True
            assert answered.result == GOLDEN_STATUS_RESULT
            assert refused.error_code == ErrorCode.UNKNOWN_METHOD
        finally:
            server.close()
            thread.join(timeout=GRACE)

        assert not thread.is_alive()


# ---------------------------------------------------------------------------
# Source-reading helpers. The module is parsed once per call and that is cheap
# enough; caching it would make a mutation test read a stale tree.
# ---------------------------------------------------------------------------


def _module_tree() -> ast.Module:
    return ast.parse(SERVER_SOURCE.read_text(encoding="utf-8"))


def _imported_modules(tree: ast.Module) -> list[str]:
    """Every module name the file imports, in either spelling."""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
    return names
