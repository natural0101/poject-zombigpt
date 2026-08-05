"""The capability model: what may be claimed, and what evidence each claim needs."""

from __future__ import annotations

import json

import pytest

from pz_agent_core.capabilities.model import (
    MAX_CAPABILITIES,
    MAX_EVIDENCE_PER_CAPABILITY,
    REASON_BUILD_CHANGED,
    REASON_NO_VERIFIED_API,
    Capability,
    CapabilityError,
    CapabilityReport,
    Evidence,
    EvidenceKind,
    utc_now_iso,
)
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    CapabilityState,
    ReasonCode,
)
from pz_agent_core.version import PROTOCOL_VERSION
from tests.fixtures.capability_trees import probe_ack

WHEN = "2026-01-02T03:04:05+00:00"
SHA = "a" * 64


def static_evidence(symbol: str = "ISEatFoodAction.new") -> Evidence:
    return Evidence.from_scan(
        symbol=symbol,
        file="client/TimedActions/ISEatFoodAction.lua",
        file_sha256=SHA,
        signature=f"{symbol}(character, item, percentage)",
        observed_at=WHEN,
    )


def runtime_evidence(symbol: str = "ISEatFoodAction.new") -> Evidence:
    return Evidence.from_ack(
        probe="eat_percentage",
        symbol=symbol,
        ack=probe_ack(ActionName.CONSUME_EAT, {"hunger_before": 0.6, "hunger_after": 0.1}),
        observed_at=WHEN,
    )


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


def test_static_evidence_requires_a_file_and_its_hash() -> None:
    with pytest.raises(CapabilityError, match="file"):
        Evidence(kind=EvidenceKind.STATIC_SCAN, symbol="X.new", observed_at=WHEN)
    with pytest.raises(CapabilityError, match="sha256"):
        Evidence(
            kind=EvidenceKind.STATIC_SCAN,
            symbol="X.new",
            observed_at=WHEN,
            file="a.lua",
            file_sha256="not-a-digest",
        )


def test_static_evidence_may_not_claim_a_probe_ran() -> None:
    with pytest.raises(CapabilityError, match="must not claim a probe ran"):
        Evidence(
            kind=EvidenceKind.STATIC_SCAN,
            symbol="X.new",
            observed_at=WHEN,
            file="a.lua",
            file_sha256=SHA,
            probe="eat_percentage",
        )


def test_runtime_evidence_requires_a_probe_name() -> None:
    with pytest.raises(CapabilityError, match="must name the probe"):
        Evidence(kind=EvidenceKind.RUNTIME_PROBE, symbol="X.new", observed_at=WHEN)


def test_evidence_rejects_a_non_iso_timestamp() -> None:
    with pytest.raises(CapabilityError, match="ISO-8601"):
        Evidence.from_scan(
            symbol="X.new",
            file="a.lua",
            file_sha256=SHA,
            signature="X.new()",
            observed_at="last tuesday",
        )


def test_from_ack_refuses_an_ack_that_did_not_succeed() -> None:
    failed = probe_ack(ActionName.CONSUME_EAT, {}, status=ActionStatus.FAILED)
    with pytest.raises(CapabilityError, match="ack status is failed"):
        Evidence.from_ack(probe="eat_percentage", symbol="X.new", ack=failed, observed_at=WHEN)


def test_from_ack_refuses_a_success_without_postcondition_evidence() -> None:
    # Built field by field: ActionResult.succeeded() would have refused this, and
    # the point is that Evidence.from_ack refuses it independently.
    hollow = ActionResult(
        session_id="s",
        seq=1,
        command_id="c",
        action=ActionName.CONSUME_EAT.value,
        status=ActionStatus.SUCCEEDED,
        reason_code=ReasonCode.POSTCONDITION_MET,
        timestamp_ms=1,
        evidence={},
    )
    with pytest.raises(CapabilityError, match="no postcondition evidence"):
        Evidence.from_ack(probe="eat_percentage", symbol="X.new", ack=hollow, observed_at=WHEN)


def test_evidence_round_trips_through_json() -> None:
    for evidence in (static_evidence(), runtime_evidence()):
        assert Evidence.from_dict(evidence.to_dict()) == evidence


def test_evidence_from_dict_rejects_an_unknown_kind() -> None:
    payload = static_evidence().to_dict()
    payload["kind"] = "hearsay"
    with pytest.raises(CapabilityError, match="unknown evidence kind"):
        Evidence.from_dict(payload)


# ---------------------------------------------------------------------------
# capability
# ---------------------------------------------------------------------------


def test_verified_without_any_evidence_is_rejected() -> None:
    with pytest.raises(CapabilityError, match="requires runtime probe evidence"):
        Capability(name="eat_percentage", state=CapabilityState.VERIFIED, build="42.20")


def test_verified_with_only_static_evidence_is_rejected() -> None:
    with pytest.raises(CapabilityError, match="requires runtime probe evidence"):
        Capability.verified(
            name="eat_percentage",
            build="42.20",
            evidence=[static_evidence()],
        )


def test_verified_requires_the_build_it_was_proven_on() -> None:
    with pytest.raises(CapabilityError, match="requires the build"):
        Capability.verified(name="eat_percentage", build="", evidence=[runtime_evidence()])


def test_verified_with_runtime_evidence_is_accepted() -> None:
    capability = Capability.verified(
        name="eat_percentage",
        build="42.20",
        evidence=[static_evidence(), runtime_evidence()],
    )
    assert capability.state is CapabilityState.VERIFIED
    assert capability.has_runtime_evidence
    assert capability.usable


def test_available_unverified_requires_some_evidence() -> None:
    with pytest.raises(CapabilityError, match="at least one piece of evidence"):
        Capability.available_unverified(name="eat_percentage", build="42.20", evidence=[])


def test_refusal_states_require_a_reason() -> None:
    for state in (
        CapabilityState.UNSUPPORTED,
        CapabilityState.EXPERIMENTAL,
        CapabilityState.DISABLED_BY_POLICY,
    ):
        with pytest.raises(CapabilityError, match="requires a reason"):
            Capability(name="autonomous_attack", state=state)


def test_capability_name_must_be_lower_snake_case() -> None:
    with pytest.raises(CapabilityError, match="lower_snake_case"):
        Capability.unsupported(name="Autonomous Attack", reason=REASON_NO_VERIFIED_API)


def test_evidence_list_is_bounded() -> None:
    many = [static_evidence(f"X.sym{i}") for i in range(MAX_EVIDENCE_PER_CAPABILITY + 3)]
    capability = Capability.available_unverified(
        name="eat_percentage", build="42.20", evidence=many
    )
    assert len(capability.evidence) == MAX_EVIDENCE_PER_CAPABILITY
    with pytest.raises(CapabilityError, match="at most"):
        Capability(
            name="eat_percentage",
            state=CapabilityState.AVAILABLE_UNVERIFIED,
            build="42.20",
            evidence=tuple(many),
        )


def test_unsupported_and_experimental_are_not_publishable() -> None:
    unsupported = Capability.unsupported(name="autonomous_attack", reason=REASON_NO_VERIFIED_API)
    experimental = Capability.experimental(
        name="drink_world_source", build="42.20", reason="EXPERIMENTAL_API"
    )
    disabled = Capability.disabled_by_policy(name="drink_carried")
    assert not unsupported.usable
    assert not experimental.usable
    assert not disabled.usable


def test_downgrade_drops_runtime_evidence_but_keeps_the_static_finding() -> None:
    verified = Capability.verified(
        name="eat_percentage",
        build="42.19",
        evidence=[static_evidence(), runtime_evidence()],
    )
    downgraded = verified.downgraded(build="42.20", reason=REASON_BUILD_CHANGED)
    assert downgraded.state is CapabilityState.AVAILABLE_UNVERIFIED
    assert downgraded.build == "42.20"
    assert not downgraded.has_runtime_evidence
    assert downgraded.static_symbols() == ("ISEatFoodAction.new",)


def test_downgrade_without_static_evidence_becomes_unsupported() -> None:
    verified = Capability.verified(
        name="eat_percentage", build="42.19", evidence=[runtime_evidence()]
    )
    downgraded = verified.downgraded(build="42.20", reason=REASON_BUILD_CHANGED)
    assert downgraded.state is CapabilityState.UNSUPPORTED
    assert downgraded.evidence == ()


def test_capability_round_trips_through_json() -> None:
    capability = Capability.verified(
        name="eat_percentage",
        build="42.20",
        evidence=[static_evidence(), runtime_evidence()],
    )
    assert Capability.from_dict("eat_percentage", capability.to_dict()) == capability


def test_capability_from_dict_rejects_a_forged_verified_claim() -> None:
    forged = {"state": "verified", "build": "42.20"}
    with pytest.raises(CapabilityError, match="requires runtime probe evidence"):
        Capability.from_dict("eat_percentage", forged)


def test_capability_from_dict_rejects_an_unknown_state() -> None:
    with pytest.raises(CapabilityError, match="unknown capability state"):
        Capability.from_dict("eat_percentage", {"state": "probably_fine"})


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def make_report(build: str = "42.20") -> CapabilityReport:
    return CapabilityReport(
        build=build,
        capabilities=(
            Capability.verified(
                name="eat_percentage", build=build, evidence=[static_evidence(), runtime_evidence()]
            ),
            Capability.available_unverified(
                name="move_to_square",
                build=build,
                evidence=[static_evidence("ISWalkToTimedAction")],
            ),
            Capability.experimental(
                name="drink_world_source", build=build, reason="EXPERIMENTAL_API"
            ),
            Capability.unsupported(name="autonomous_attack", reason=REASON_NO_VERIFIED_API),
        ),
        revision=3,
        generated_at=WHEN,
    )


def test_usable_gates_tool_publication() -> None:
    report = make_report()
    assert report.usable("eat_percentage")
    assert report.usable("move_to_square")
    assert not report.usable("drink_world_source")
    assert not report.usable("autonomous_attack")
    assert not report.usable("teleport")
    assert report.usable_names() == ("eat_percentage", "move_to_square")


def test_an_unknown_capability_reports_as_unsupported() -> None:
    assert make_report().state("teleport") is CapabilityState.UNSUPPORTED
    assert make_report().get("teleport") is None


def test_unusable_reasons_explain_every_refusal() -> None:
    reasons = make_report().unusable_reasons()
    assert reasons["autonomous_attack"] == REASON_NO_VERIFIED_API
    assert set(reasons) == {"autonomous_attack", "drink_world_source"}


def test_report_rejects_duplicates_and_a_negative_revision() -> None:
    duplicate = Capability.unsupported(name="autonomous_attack", reason=REASON_NO_VERIFIED_API)
    with pytest.raises(CapabilityError, match="same capability twice"):
        CapabilityReport(build="42.20", capabilities=(duplicate, duplicate))
    with pytest.raises(CapabilityError, match="revision must be non-negative"):
        CapabilityReport(build="42.20", revision=-1)
    with pytest.raises(CapabilityError, match="build must not be empty"):
        CapabilityReport(build="")


def test_report_capability_count_is_bounded() -> None:
    too_many = tuple(
        Capability.unsupported(name=f"cap_{i}", reason="X") for i in range(MAX_CAPABILITIES + 1)
    )
    with pytest.raises(CapabilityError, match="at most"):
        CapabilityReport(build="42.20", capabilities=too_many)


def test_revision_advances_only_when_the_set_actually_changes() -> None:
    report = make_report()
    unchanged = report.with_capabilities([report.capabilities[0]])
    assert unchanged.revision == report.revision

    changed = report.with_capabilities(
        [Capability.disabled_by_policy(name="move_to_square", build="42.20")]
    )
    assert changed.revision == report.revision + 1
    assert not changed.usable("move_to_square")


def test_report_round_trips_through_json() -> None:
    report = make_report()
    restored = CapabilityReport.from_dict(json.loads(json.dumps(report.to_dict())))
    assert restored == report
    assert restored.protocol_version == PROTOCOL_VERSION


def test_for_build_downgrades_every_verified_capability() -> None:
    report = make_report("42.19")
    rebased = report.for_build("42.20")
    assert rebased.build == "42.20"
    assert rebased.state("eat_percentage") is CapabilityState.AVAILABLE_UNVERIFIED
    assert not any(c.has_runtime_evidence for c in rebased.capabilities)
    assert rebased.revision == report.revision + 1
    assert any("42.19" in note for note in rebased.notes)


def test_for_build_is_a_no_op_for_the_same_build() -> None:
    report = make_report()
    assert report.for_build("42.20") is report


def test_utc_now_iso_is_parseable_evidence_timestamp() -> None:
    evidence = Evidence.from_scan(
        symbol="X.new",
        file="a.lua",
        file_sha256=SHA,
        signature="X.new()",
        observed_at=utc_now_iso(),
    )
    assert evidence.observed_at.endswith("+00:00")
