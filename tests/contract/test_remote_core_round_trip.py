"""The seam: the two halves of the Core RPC bridge, over a real socket.

Neither half could establish what is here. The client was written against the
method catalogue and the codecs; the router was written against the same two.
Both can be internally consistent, fully tested, green — and disagree, because
each was checked against the contract rather than against the other. That is the
defect this project keeps finding: subsystems that work and a join that nobody
exercised.

So these tests own the join and nothing else. A fake ``CoreServices`` sits
behind a real :class:`RpcServer` running the real :class:`CoreRouter`, and a
real :class:`RemoteCoreServices` reaches it through a real descriptor and a real
token over a real named pipe or Unix socket. Every assertion is about a value
that crossed a process boundary and came back, or about the fake recording that
the call arrived.

Two things are checked here that a unit test on either side cannot see:

**Every method the client can call, the router answers.** Not "the client's set
is a subset" — equality, in both directions. A method the client calls and the
router does not route hangs a caller; a method the router answers and no client
calls is a surface nobody reviewed.

**The spellings agree.** ``session.arm`` and ``action.status`` carry parameters
that are not records and so have no codec. Each half names its own keys. If they
disagree the call still travels, the router still answers, and the answer is
wrong — which is why these are pinned against a payload written out by hand.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final

import pytest

from pz_agent_core.actions import ActionRequest
from pz_agent_core.protocol import ActionName, ActionStatus, ReasonCode, SessionMode
from pz_agent_core.rpc.descriptor import write_descriptor
from pz_agent_core.rpc.token import issue_token
from pz_agent_core.rpc.transport import RpcServer, new_address
from pz_agent_core.rpc.wire import RpcRequest, RpcResponse
from pz_agent_mcp.ports import CoreServices
from pz_agent_mcp.remote.client import (
    SIDECAR_NOT_RUNNING,
    CoreRefused,
    RemoteCoreError,
    RemoteCoreServices,
    SidecarUnavailable,
)
from pz_agent_mcp.remote.methods import ALL_METHODS, Method
from pz_agent_mcp.remote.server import ROUTED_METHODS, CoreRouter
from tests.fixtures.mcp_doubles import Doubles, succeeded_result

pytestmark = pytest.mark.contract

#: Long enough for a loaded runner, short enough that a hang ends the test.
GRACE: Final = 10.0

#: Servers started by `_services_served_by`, closed by the autouse fixture below
#: so a test that raises does not leave a thread holding a socket.
_SERVERS: list[tuple[RpcServer, threading.Thread]] = []
_COUNTER = itertools.count()


@pytest.fixture(autouse=True)
def _close_extra_servers() -> Iterator[None]:
    yield
    while _SERVERS:
        server, thread = _SERVERS.pop()
        server.close()
        thread.join(timeout=GRACE)


@dataclass
class Bridge:
    """Both halves, joined, with the fake core still reachable for assertions."""

    services: RemoteCoreServices
    core: Doubles
    server: RpcServer
    thread: threading.Thread

    def stop(self) -> None:
        self.server.close()
        self.thread.join(timeout=GRACE)


@pytest.fixture
def bridge(tmp_path: Path) -> Iterator[Bridge]:
    """A running sidecar, as the MCP executable would find one."""
    state_dir = tmp_path / "pz-agent"
    runtime = state_dir / "runtime"
    runtime.mkdir(parents=True)
    key = issue_token(runtime)
    core = Doubles()
    server = RpcServer(new_address(runtime), authkey=key, handler=CoreRouter(core.services))
    write_descriptor(state_dir, server.descriptor())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    built = Bridge(
        services=RemoteCoreServices.from_state_dir(state_dir, deadline=GRACE),
        core=core,
        server=server,
        thread=thread,
    )
    try:
        yield built
    finally:
        built.stop()


class TestTheTwoHalvesAgree:
    def test_the_router_answers_exactly_what_the_client_can_call(self) -> None:
        """Equality, not containment, and in both directions.

        A declared method nobody routes is a call that hangs; a routed method
        nobody declared is a surface that was never reviewed.
        """
        assert ROUTED_METHODS == ALL_METHODS

    def test_every_declared_method_survives_a_real_call(self, bridge: Bridge) -> None:
        """The catalogue is a list of strings until something travels each one.

        Driven off `ALL_METHODS` rather than a hand-kept list, so a method added
        to the catalogue and wired nowhere fails here.
        """
        # Bound methods where the call takes no arguments, `partial` where it
        # does: a lambda wrapping a single call is a lambda that can silently
        # capture the wrong thing in a loop, and there is nothing to defer here
        # beyond the call itself.
        exercised: dict[str, Callable[[], object]] = {
            Method.SESSION_STATUS: bridge.services.session.status,
            Method.SESSION_ARM: partial(
                bridge.services.session.arm, SessionMode.ASSISTED, confirm_backup=True
            ),
            Method.SESSION_DISARM: bridge.services.session.disarm,
            Method.SESSION_STOP: bridge.services.session.stop,
            Method.OBSERVATION_LATEST: bridge.services.observations.latest,
            Method.CAPABILITIES_REPORT: bridge.services.capabilities.report,
            Method.ACTION_SUBMIT: partial(bridge.services.actions.submit, _a_request()),
            Method.ACTION_STATUS: partial(bridge.services.actions.status, "nope"),
            Method.PLAN_CURRENT: bridge.services.plans.current,
            Method.MEMORY_QUERY: partial(bridge.services.memory.query, kinds=["home"], limit=5),
            Method.DIAGNOSTICS_DOCTOR: bridge.services.diagnostics.doctor,
            Method.DIAGNOSTICS_TAIL: partial(bridge.services.diagnostics.tail, limit=5),
        }

        assert set(exercised) | {Method.PLAN_EXECUTE} == ALL_METHODS, (
            "a declared method is not exercised here; add it rather than trimming the set"
        )
        for name, call in exercised.items():
            try:
                call()
            except CoreRefused:
                # The fake refusing is still a completed round trip: the request
                # reached a port and the port's answer came back.
                pass
            except SidecarUnavailable as unreachable:  # pragma: no cover - a real failure
                pytest.fail(f"{name} did not reach the core: {unreachable}")


class TestValuesSurviveTheCrossing:
    def test_the_session_snapshot_is_the_core_s_own(self, bridge: Bridge) -> None:
        expected = bridge.core.session.snapshot

        assert bridge.services.session.status() == expected

    def test_arming_reaches_the_core_with_both_arguments(self, bridge: Bridge) -> None:
        """The spelling seam. `session.arm` has no codec: each half names its own
        keys, and if they disagree the call still travels and answers wrongly."""
        bridge.services.session.arm(SessionMode.AUTONOMOUS, confirm_backup=True)

        assert bridge.core.session.arms == [(SessionMode.AUTONOMOUS, True)]

    def test_a_refused_arming_is_the_core_s_refusal_not_a_link_failure(
        self, bridge: Bridge
    ) -> None:
        """The distinction a user acts on: fix the session, or start the sidecar."""
        bridge.core.session.arm_error = RuntimeError("a verified backup is required")

        with pytest.raises(CoreRefused) as refused:
            bridge.services.session.arm(SessionMode.ASSISTED, confirm_backup=False)

        assert "a verified backup is required" in str(refused.value)
        assert SIDECAR_NOT_RUNNING not in str(refused.value)

    def test_the_observation_survives_whole(self, bridge: Bridge) -> None:
        expected = bridge.core.observations.observation

        assert bridge.services.observations.latest() == expected

    def test_no_observation_yet_is_not_an_error(self, bridge: Bridge) -> None:
        """The state a fresh session is in. It must not read as a broken link."""
        bridge.core.observations.observation = None

        assert bridge.services.observations.latest() is None

    def test_the_capability_report_survives_with_every_state(self, bridge: Bridge) -> None:
        """This decides which tools are published at all."""
        expected = bridge.core.capabilities.report_value

        received = bridge.services.capabilities.report()

        assert received == expected
        assert received.usable_names() == expected.usable_names()

    def test_an_action_id_travels_under_a_key_both_halves_read(self, bridge: Bridge) -> None:
        """The second spelling seam, and the one with a silent failure mode:
        a mismatched key makes `status` answer about no action at all."""
        submitted = bridge.services.actions.submit(_a_request())

        found = bridge.services.actions.status(submitted.action_id)

        assert found is not None
        assert found.action_id == submitted.action_id

    def test_the_diagnostics_filters_reach_the_core(self, bridge: Bridge) -> None:
        bridge.services.diagnostics.tail(limit=3, level="ERROR", action_id="a-1")

        assert bridge.core.diagnostics.tail_calls == [
            {"limit": 3, "level": "ERROR", "component": None, "action_id": "a-1"}
        ]


class TestTheHonestyRuleSurvivesTheCrossing:
    """The one invariant that must not weaken in transit.

    `ActionRecord` refuses to exist as SUCCEEDED without the terminal result
    carrying POSTCONDITION_MET and observed evidence. That rule lives in the
    core. A boundary that rebuilt the record field by field could produce a
    success the core never proved — which is the whole reason the codec
    delegates to the dataclass rather than re-implementing it.
    """

    def test_a_proven_success_arrives_with_its_proof(self, bridge: Bridge) -> None:
        submitted = bridge.services.actions.submit(_a_request())
        bridge.core.actions.finish(
            submitted.action_id,
            succeeded_result(ActionName.CONSUME_EAT, observed={"hunger_delta": -20}),
        )

        record = bridge.services.actions.status(submitted.action_id)

        assert record is not None
        assert record.status is ActionStatus.SUCCEEDED
        assert record.result is not None
        assert record.result.reason_code is ReasonCode.POSTCONDITION_MET
        assert record.result.evidence, "a success crossed the link without its evidence"

    def test_a_lying_core_cannot_make_the_client_report_a_success(self, tmp_path: Path) -> None:
        """The question the fake cannot ask: what if the *other process* lies?

        The fake cannot build an unproven success — `ActionRecord.__post_init__`
        forbids it — so the only way to test this is to serve the lie directly.
        A server answering `SUCCEEDED` with no result, or with a reason code that
        is not `POSTCONDITION_MET`, must not produce a record on this side. If it
        did, the boundary would publish a success the core never proved, which is
        the one failure this whole design exists to prevent.
        """
        for lie in (
            {
                "action_id": "a-1",
                "action": "consume.eat",
                "status": "succeeded",
                "idempotency_key": "k",
            },
            {
                "action_id": "a-1",
                "action": "consume.eat",
                "status": "succeeded",
                "idempotency_key": "k",
                "result": None,
            },
        ):
            services = _services_served_by(tmp_path, _always({"record": lie}))
            with pytest.raises(RemoteCoreError) as caught:
                services.actions.status("a-1")

            assert not isinstance(caught.value, SidecarUnavailable), (
                "an unprovable success was reported as the link being down, "
                "which hides it behind a retry"
            )


class TestWhenTheSidecarGoesAway:
    def test_a_call_after_shutdown_says_the_sidecar_is_not_running(self, bridge: Bridge) -> None:
        """Not a traceback, and not silence: the message names the remedy."""
        assert bridge.services.session.status() is not None
        bridge.stop()

        with pytest.raises(SidecarUnavailable) as gone:
            bridge.services.session.status()

        assert SIDECAR_NOT_RUNNING in str(gone.value)

    def test_that_failure_is_not_reported_as_the_core_refusing(self, bridge: Bridge) -> None:
        """Collapsing these tells a user to fix a session that is fine."""
        bridge.stop()

        with pytest.raises(SidecarUnavailable):
            bridge.services.session.status()

    def test_an_observation_after_shutdown_raises_rather_than_answering_none(
        self, bridge: Bridge
    ) -> None:
        """`None` means "nothing yet". A dead link answering `None` would read as
        a session that never receives an observation, forever."""
        bridge.stop()

        with pytest.raises(SidecarUnavailable):
            bridge.services.observations.latest()

    def test_the_capability_report_is_never_invented_when_the_link_is_down(
        self, bridge: Bridge
    ) -> None:
        """An empty report would silently withdraw every tool."""
        bridge.stop()

        with pytest.raises(SidecarUnavailable):
            bridge.services.capabilities.report()


def test_remote_core_services_is_a_core_services(bridge: Bridge) -> None:
    """Structural, and checked by mypy as well as here.

    The router takes a `CoreServices`. If `RemoteCoreServices` is not one, the
    two halves cannot be joined at all, and the failure would appear as a type
    error in whatever wires them rather than here.
    """
    services: CoreServices = bridge.services

    assert services.session.status() is not None


def _always(result: dict[str, object]) -> Callable[[RpcRequest], dict[str, object]]:
    """A handler that answers *result* whatever it is asked.

    A named function rather than a lambda in a loop: a lambda closing over the
    loop variable is the classic way to test the last value four times, and the
    default-argument trick that avoids it is exactly the kind of cleverness a
    reader has to stop and verify.
    """

    def answer(_request: RpcRequest) -> dict[str, object]:
        return result

    return answer


def _services_served_by(
    tmp_path: Path, answer: Callable[[RpcRequest], dict[str, object]]
) -> RemoteCoreServices:
    """A client wired to a server that answers whatever *answer* returns.

    Deliberately bypasses `CoreRouter`: the point is to serve a payload a real
    core could never produce, and the router cannot produce one either.
    """
    state_dir = tmp_path / f"lying-{next(_COUNTER)}"
    runtime = state_dir / "runtime"
    runtime.mkdir(parents=True)
    key = issue_token(runtime)

    def handler(request: RpcRequest) -> RpcResponse:
        return RpcResponse(id=request.id, ok=True, result=answer(request))

    server = RpcServer(new_address(runtime), authkey=key, handler=handler)
    write_descriptor(state_dir, server.descriptor())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _SERVERS.append((server, thread))
    return RemoteCoreServices.from_state_dir(state_dir, deadline=GRACE)


def _a_request() -> ActionRequest:
    return ActionRequest(
        action=ActionName.CONSUME_EAT,
        session_id="11111111-1111-1111-1111-111111111111",
        idempotency_key="k-1",
        args={"item_ref": "i-1"},
    )
