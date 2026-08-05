"""The seven resources: what they project, and that they are not a way around a refusal."""

from __future__ import annotations

from dataclasses import replace

import pytest

from pz_agent_core.capabilities.probes import EAT_PERCENTAGE, MOVE_TO_SQUARE
from pz_agent_core.observation.compact import UNTRUSTED_TEXT_KEY
from pz_agent_core.protocol import ActionStatus, ReasonCode, SessionMode
from pz_agent_mcp import resources as resources_module
from pz_agent_mcp.catalog import RESOURCES, ResourceSpec
from pz_agent_mcp.envelope import ToolFailure
from pz_agent_mcp.ports import PlanRecord
from pz_agent_mcp.resources import ResourceReader
from pz_agent_mcp.router import ToolRouter
from tests.fixtures import make_observation
from tests.fixtures.mcp_doubles import CountingIds, Doubles, make_report


def reader_for(doubles: Doubles) -> ResourceReader:
    return ResourceReader(ToolRouter(doubles.services, request_ids=CountingIds()))


def armed() -> Doubles:
    doubles = Doubles()
    doubles.capabilities.report_value = make_report(
        usable=[MOVE_TO_SQUARE], unsupported=[EAT_PERCENTAGE]
    )
    return doubles


def test_every_listed_resource_has_a_reader() -> None:
    reader = reader_for(armed())

    for spec in RESOURCES:
        assert reader.read(spec.uri) is not None


def test_the_listing_matches_the_catalogue_and_says_it_is_subscribable() -> None:
    listing = reader_for(armed()).list()

    assert {entry["uri"] for entry in listing} == {spec.uri for spec in RESOURCES}
    assert all(entry["subscribable"] for entry in listing)
    assert all(entry["mimeType"] == "application/json" for entry in listing)


def test_an_unknown_uri_is_refused() -> None:
    with pytest.raises(ToolFailure) as caught:
        reader_for(armed()).read("pz://anything/else")

    assert caught.value.reason_code is ReasonCode.INVALID_ARGUMENT


def test_the_observation_resource_is_the_full_compacted_view_not_a_raw_snapshot() -> None:
    document = reader_for(armed()).read("pz://observation/latest")

    assert document["view"] == "compact_observation"
    assert document["detail"] == "full"
    assert "inventory" in document
    assert "save_id" not in document["game"]


def test_a_resource_read_carries_the_observation_seq_a_client_uses_as_an_etag() -> None:
    doubles = armed()
    doubles.observations.observation = make_observation(seq=77)
    reader = reader_for(doubles)

    assert reader.read("pz://observation/latest")["seq"] == 77
    assert reader.read("pz://inventory/current")["seq"] == 77


def test_a_resource_read_is_not_a_privileged_path_around_a_refusal() -> None:
    # The same GAME_DISCONNECTED the tool would answer with, not a partial read.
    doubles = armed()
    doubles.observations.observation = None

    with pytest.raises(ToolFailure) as caught:
        reader_for(doubles).read("pz://inventory/current")

    assert caught.value.reason_code is ReasonCode.GAME_DISCONNECTED


def test_the_session_resource_reports_mode_armed_state_and_protocol_version() -> None:
    document = reader_for(armed()).read("pz://session/current")

    assert document["mode"] == SessionMode.ASSISTED.value
    assert document["armed"] is True
    assert document["protocol_version"]


def test_the_safety_resource_carries_danger_takeover_and_heartbeat_health() -> None:
    doubles = armed()
    doubles.observations.observation = make_observation()

    document = reader_for(doubles).read("pz://safety/status")

    assert document["heartbeat"] == {"game_ok": True, "sidecar_ok": True}
    assert document["observed"]["manual_takeover"] is False
    assert document["observed"]["danger_level"]


def test_the_safety_resource_still_answers_with_no_observation_at_all() -> None:
    # A safety view that vanished when the game went quiet would hide exactly
    # the situation a client is subscribed to hear about.
    doubles = armed()
    doubles.observations.observation = None
    doubles.session.snapshot = replace(doubles.session.snapshot, connected=False)

    document = reader_for(doubles).read("pz://safety/status")

    assert document["connected"] is False
    assert document["observed"] is None


def test_the_capability_resource_names_what_is_published_and_what_is_withheld() -> None:
    document = reader_for(armed()).read("pz://capabilities")

    assert "pz_action_move_to" in document["published_tools"]
    assert "pz_action_eat" not in document["published_tools"]
    assert document["withheld_tools"]["pz_action_eat"] == "NO_VERIFIED_API"
    assert document["capabilities"][MOVE_TO_SQUARE]["usable"] is True
    assert document["capabilities"][EAT_PERCENTAGE]["usable"] is False


def test_capability_evidence_is_summarised_without_the_file_it_was_found_in() -> None:
    # Evidence.file is an absolute path into the game install; §3.13 does not
    # let one cross this boundary even inside a diagnostic.
    document = reader_for(armed()).read("pz://capabilities")

    evidence = document["capabilities"][MOVE_TO_SQUARE]["evidence"]
    assert evidence
    for entry in evidence:
        assert set(entry) == {"kind", "symbol", "probe", "observed_at"}


def test_the_plan_resource_reports_the_active_plan() -> None:
    doubles = armed()
    doubles.plans.record = PlanRecord(plan_id="plan-3", status=ActionStatus.STARTED, step_index=1)

    document = reader_for(doubles).read("pz://plan/current")

    assert document["plan_id"] == "plan-3"
    assert document["active"] is True


def test_the_diagnostics_resource_returns_a_bounded_redacted_tail() -> None:
    document = reader_for(armed()).read("pz://diagnostics/recent")

    assert document["records"] == []
    assert document["limit"] == 20


def test_the_inventory_resource_quarantines_the_names_it_carries() -> None:
    doubles = armed()

    document = reader_for(doubles).read("pz://inventory/current")

    assert document["scope"] == "all"
    assert document["include_nested"] is True
    for container in document["containers"]:
        assert UNTRUSTED_TEXT_KEY in container


def test_a_reader_refuses_to_start_if_a_listed_resource_has_no_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extra = ResourceSpec(uri="pz://unwired", name="unwired", summary="")
    monkeypatch.setattr(resources_module, "RESOURCES", (*RESOURCES, extra))

    with pytest.raises(ValueError, match="without a reader"):
        reader_for(armed())
