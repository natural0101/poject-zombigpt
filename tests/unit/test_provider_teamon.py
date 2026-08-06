"""``provider = "teamon"``: the documented contract, exercised over a fake transport.

These tests are about *this* side of the wire and say nothing about a TeamON
server, because no TeamON server has been reached from this repository — the
module docstring says which half is confirmed and which half is a proposal, and
this file is the confirmed half. What is asserted here is that the request
carries only redacted data, that a reply is parsed and never assembled, that
every way the platform can answer badly ends as a refusal or an honest
``reachable = False``, and that nothing an endpoint sends can make the provider
raise into the session loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pz_agent_cli.config import (
    CODE_MISSING_VALUE,
    CODE_TYPE_MISMATCH,
    ConfigValidation,
    load_config,
)
from pz_agent_core.planner.provider import PlanProvider
from pz_agent_core.planner.providers.teamon import (
    CONTRACT_ID,
    DEFAULT_TEAMON_KEY_ENV,
    HEALTH_PATH,
    PLAN_PATH,
    PROVIDER_TEAMON,
    TeamONConfig,
    TeamONProvider,
)
from pz_agent_core.planner.providers.transport import (
    ConnectFailed,
    CredentialUnavailable,
    HttpResponse,
    InvalidEndpoint,
    ReadTimedOut,
    ResponseTooLarge,
)
from pz_agent_core.protocol import ActionName, ReasonCode
from tests.fixtures import make_game
from tests.fixtures.planner_worlds import (
    GOAL_ID,
    plan_payload,
    plan_request,
    planner_observation,
    step_payload,
    stocked_inventory,
)
from tests.fixtures.policy_items import food_item
from tests.unit.test_provider_transport import FakeTransport, json_response

KEY_ENV = DEFAULT_TEAMON_KEY_ENV
ENVIRON = {KEY_ENV: "test-key"}
BASE_URL = "https://teamon.example.invalid"


def config(**overrides: Any) -> TeamONConfig:
    base: dict[str, Any] = {"base_url": BASE_URL}
    base.update(overrides)
    return TeamONConfig(**base)


def provider(
    *replies: Any, environ: dict[str, str] | None = None, **config_overrides: Any
) -> tuple[TeamONProvider, FakeTransport]:
    transport = FakeTransport(*replies)
    return (
        TeamONProvider(
            config(**config_overrides),
            transport=transport,
            environ=ENVIRON if environ is None else environ,
        ),
        transport,
    )


# ---------------------------------------------------------------------------
# the plan call
# ---------------------------------------------------------------------------


def test_it_is_a_plan_provider() -> None:
    seam: PlanProvider = TeamONProvider(config(), transport=FakeTransport(json_response({})))

    assert seam.name == PROVIDER_TEAMON


def test_a_plan_in_the_reply_comes_back_as_a_proposal() -> None:
    planner, transport = provider(json_response({"plan": plan_payload()}))

    proposal = planner.propose(plan_request())

    assert not proposal.refused
    assert proposal.plan is not None
    assert [step.action for step in proposal.plan.steps] == [ActionName.CONSUME_EAT]
    assert transport.calls == 1


def test_the_request_states_the_contract_it_is_speaking() -> None:
    planner, transport = provider(json_response({"plan": plan_payload()}), assistant="survival")

    planner.propose(plan_request())

    sent = transport.requests[0]
    body = transport.sent_body()
    assert sent.url == f"{BASE_URL}{PLAN_PATH}"
    assert sent.method == "POST"
    assert sent.headers["authorization"] == "Bearer test-key"
    assert body["contract"] == CONTRACT_ID
    assert body["plan_schema_version"] == "1.0"
    assert body["assistant"] == "survival"
    assert body["request"]["goal"]["goal_id"] == GOAL_ID


def test_an_unnamed_assistant_is_left_out_rather_than_sent_empty() -> None:
    planner, transport = provider(json_response({"plan": plan_payload()}))

    planner.propose(plan_request())

    assert "assistant" not in transport.sent_body()


def test_neither_the_save_name_nor_a_path_reaches_the_platform() -> None:
    observation = planner_observation(
        game=make_game(save_id="Muldraugh, KY\\eleanor"),
        inventory=stocked_inventory(
            items=[food_item("beans", display_name="Beans from C:\\Users\\bob\\Zomboid")]
        ),
    )
    planner, transport = provider(json_response({"plan": plan_payload()}))

    planner.propose(plan_request(observation))

    body = transport.requests[0].body.decode("utf-8")
    assert "eleanor" not in body
    assert "bob" not in body
    assert "path-redacted" in body


# ---------------------------------------------------------------------------
# every way the reply can be wrong
# ---------------------------------------------------------------------------


def test_a_reply_that_is_not_json_is_refused() -> None:
    planner, _ = provider(HttpResponse(status=200, body=b"<html>hello</html>"))

    proposal = planner.propose(plan_request())

    assert proposal.refused
    assert proposal.reason_code is ReasonCode.INVALID_ARGUMENT


def test_a_reply_with_neither_a_plan_nor_a_refusal_is_refused() -> None:
    planner, _ = provider(json_response({"contract": CONTRACT_ID, "status": "thinking"}))

    proposal = planner.propose(plan_request())

    assert proposal.refused
    assert "neither a plan nor a refusal" in proposal.detail


def test_valid_json_that_is_not_a_plan_carries_the_parse_fault() -> None:
    planner, _ = provider(json_response({"plan": {"summary": "eat"}}))

    proposal = planner.propose(plan_request())

    assert proposal.refused
    assert proposal.reasons[0].startswith("MISSING_FIELD")


def test_a_plan_naming_an_unknown_action_is_refused() -> None:
    payload = plan_payload(steps=[step_payload(action="lua.exec", args={"code": "os.exit()"})])
    planner, _ = provider(json_response({"plan": payload}))

    proposal = planner.propose(plan_request())

    assert proposal.reasons[0].startswith("UNKNOWN_ACTION")


def test_a_plan_for_another_goal_is_refused() -> None:
    payload = plan_payload(goal_id="00000000-0000-4000-8000-00000000dead")
    planner, _ = provider(json_response({"plan": payload}))

    assert "different goal" in planner.propose(plan_request()).detail


def test_a_plan_longer_than_the_request_allows_is_refused() -> None:
    payload = plan_payload(steps=[step_payload(step_id="s1"), step_payload(step_id="s2")])
    planner, _ = provider(json_response({"plan": payload}))

    assert planner.propose(plan_request(max_steps=1)).reason_code is ReasonCode.POLICY_DENIED


# ---------------------------------------------------------------------------
# the platform's own refusal, which is untrusted text
# ---------------------------------------------------------------------------


def test_a_refusal_is_quoted_back_with_its_reason_code() -> None:
    planner, _ = provider(
        json_response(
            {"refusal": {"reason_code": "NO_SAFE_FOOD", "detail": "everything is rotten"}}
        )
    )

    proposal = planner.propose(plan_request())

    assert proposal.reason_code is ReasonCode.NO_SAFE_FOOD
    assert "everything is rotten" in proposal.detail


def test_a_refusal_naming_an_unknown_code_lands_on_policy_denied() -> None:
    planner, _ = provider(json_response({"refusal": {"reason_code": "SUDO", "detail": "no"}}))

    assert planner.propose(plan_request()).reason_code is ReasonCode.POLICY_DENIED


def test_a_refusal_claiming_success_cannot_make_the_provider_raise() -> None:
    """``POSTCONDITION_MET`` is the one code a refusal may not carry.

    An endpoint that sent it would otherwise reach the ``ValueError`` inside
    :meth:`PlanProposal.refusal` — a peer choosing which line of this process
    raises, which is exactly what the parsing rules exist to prevent.
    """
    planner, _ = provider(
        json_response({"refusal": {"reason_code": "POSTCONDITION_MET", "detail": "done"}})
    )

    assert planner.propose(plan_request()).reason_code is ReasonCode.POLICY_DENIED


def test_a_refusal_with_no_detail_still_explains_itself() -> None:
    planner, _ = provider(json_response({"refusal": {"reason_code": "POLICY_DENIED"}}))

    assert planner.propose(plan_request()).detail.strip()


def test_a_refusal_detail_holding_a_path_is_redacted() -> None:
    planner, _ = provider(
        json_response({"refusal": {"reason_code": "POLICY_DENIED", "detail": "see /home/bob/log"}})
    )

    assert "/home/bob" not in planner.propose(plan_request()).detail


# ---------------------------------------------------------------------------
# what the transport can say instead of a reply
# ---------------------------------------------------------------------------


def test_a_client_error_is_refused_with_what_the_platform_said() -> None:
    planner, transport = provider(json_response({"error": "unknown assistant"}, status=403))

    proposal = planner.propose(plan_request())

    assert proposal.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
    assert "403" in proposal.detail
    assert proposal.reasons == ("unknown assistant",)
    assert transport.calls == 1


def test_an_unreachable_platform_is_refused_rather_than_raised() -> None:
    planner, _ = provider(ConnectFailed(BASE_URL, attempts=3, cause="name resolution failed"))

    proposal = planner.propose(plan_request())

    assert proposal.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
    assert "3 attempt(s)" in proposal.detail


def test_a_timeout_is_refused_rather_than_raised() -> None:
    planner, _ = provider(ReadTimedOut(BASE_URL, read_timeout_s=60.0))

    assert planner.propose(plan_request()).refused


def test_a_reply_over_the_byte_ceiling_is_refused() -> None:
    planner, _ = provider(ResponseTooLarge(BASE_URL, limit=1_000_000))

    assert "1000000 byte ceiling" in planner.propose(plan_request()).detail


def test_a_missing_key_names_the_variable_and_sends_nothing() -> None:
    planner, transport = provider(json_response({"plan": plan_payload()}), environ={})

    proposal = planner.propose(plan_request())

    assert KEY_ENV in proposal.detail
    assert transport.calls == 0


def test_the_key_check_is_callable_before_a_session_starts() -> None:
    planner, _ = provider(json_response({}), environ={})

    with pytest.raises(CredentialUnavailable):
        planner.api_key()


# ---------------------------------------------------------------------------
# the health check the doctor calls
# ---------------------------------------------------------------------------


def test_health_is_reachable_only_for_a_reply_that_was_read() -> None:
    planner, transport = provider(json_response({"status": "ok", "assistant": "survival"}))

    health = planner.health()

    assert health.reachable
    assert health.http_status == 200
    assert health.reported_status == "ok"
    assert transport.requests[0].url == f"{BASE_URL}{HEALTH_PATH}"
    assert transport.requests[0].method == "GET"


def test_a_platform_that_reports_anything_but_ok_is_not_reachable() -> None:
    planner, _ = provider(json_response({"status": "degraded"}))

    health = planner.health()

    assert not health.reachable
    assert health.reported_status == "degraded"


def test_a_health_error_status_is_not_reachable() -> None:
    planner, _ = provider(json_response({"status": "ok"}, status=503))

    assert not planner.health().reachable


def test_a_health_reply_that_is_not_json_is_not_reachable() -> None:
    planner, _ = provider(HttpResponse(status=200, body=b"OK"))

    assert not planner.health().reachable


def test_health_reports_a_connection_failure_instead_of_raising() -> None:
    planner, _ = provider(ConnectFailed(BASE_URL, attempts=2, cause="refused"))

    health = planner.health()

    assert not health.reachable
    assert "could not connect" in health.detail
    assert health.http_status is None


def test_health_names_the_missing_key_and_probes_nothing() -> None:
    """The key is the prerequisite, so it is the first thing the doctor is told."""
    planner, transport = provider(json_response({"status": "ok"}), environ={})

    health = planner.health()

    assert not health.reachable
    assert KEY_ENV in health.detail
    assert transport.calls == 0


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [{"base_url": "teamon.example.invalid"}, {"base_url": "ws://x/y"}, {"api_key_env": "  "}],
)
def test_a_configuration_that_cannot_work_is_refused_at_construction(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises((ValueError, InvalidEndpoint)):
        config(**overrides)


def test_the_paths_are_appended_to_the_configured_root() -> None:
    assert config(base_url=f"{BASE_URL}/").url(PLAN_PATH) == f"{BASE_URL}{PLAN_PATH}"


def test_the_key_never_appears_in_the_configuration() -> None:
    assert config().api_key_env == KEY_ENV
    assert "test-key" not in repr(config())


# ---------------------------------------------------------------------------
# the configuration file that selects it
# ---------------------------------------------------------------------------

SELECTED = f"""\
[planner]
provider = "teamon"

[planner.teamon]
base_url = "{BASE_URL}"
assistant = "survival"
connect_timeout_ms = 2000
"""


def validated(document: str, tmp_path: Path) -> ConfigValidation:
    path = tmp_path / "config.toml"
    path.write_text(document, encoding="utf-8")
    return load_config(path)


def test_the_documented_section_produces_the_provider_config(tmp_path: Path) -> None:
    validation = validated(SELECTED, tmp_path)

    assert validation.ok, [problem.render() for problem in validation.errors]
    assert validation.config is not None
    selected = validation.config.planner_provider
    assert isinstance(selected, TeamONConfig)
    assert selected.assistant == "survival"
    assert selected.transport.connect_timeout_s == 2.0
    assert selected.api_key_env == KEY_ENV


def test_selecting_the_platform_without_an_endpoint_is_an_error(tmp_path: Path) -> None:
    validation = validated('[planner]\nprovider = "teamon"\n', tmp_path)

    assert not validation.ok
    assert validation.errors[0].path == "planner.teamon.base_url"
    assert validation.errors[0].code == CODE_MISSING_VALUE


def test_a_section_written_as_a_value_is_reported_as_a_table(tmp_path: Path) -> None:
    validation = validated('[planner]\nteamon = "on"\n', tmp_path)

    assert validation.errors[0].path == "planner.teamon"
    assert validation.errors[0].code == CODE_TYPE_MISMATCH


def test_a_bound_the_transport_refuses_is_reported_against_the_section(
    tmp_path: Path,
) -> None:
    """The provider owns the bound; this only moves when the user hears about it."""
    validation = validated(SELECTED + "max_attempts = 99\n", tmp_path)

    assert not validation.ok
    assert validation.errors[0].path == "planner.teamon.max_attempts"
