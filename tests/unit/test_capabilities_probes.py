"""Probe resolution: a static scan can never reach ``verified``, and attack never does."""

from __future__ import annotations

from pathlib import Path

import pytest

from pz_agent_core.capabilities.model import (
    REASON_NO_VERIFIED_API,
    REASON_PROBE_NOT_CONFIRMED,
    REASON_SYMBOL_MISSING,
    Capability,
    CapabilityError,
    EvidenceKind,
)
from pz_agent_core.capabilities.probes import (
    AUTONOMOUS_ATTACK,
    DRINK_WORLD_SOURCE,
    EAT_PERCENTAGE,
    INVENTORY_TRANSFER,
    MOVE_TO_SQUARE,
    PROBES,
    READ_LITERATURE,
    ProbeDefinition,
    RuntimeConfirmation,
    confirm,
    missing_symbols,
    probe_for,
    resolve_all,
    resolve_static,
)
from pz_agent_core.capabilities.scanner import SymbolIndex, scan_lua_tree
from pz_agent_core.protocol import ActionName, ActionStatus, CapabilityState
from tests.fixtures.capability_trees import (
    VANILLA_SOURCES,
    full_lua_tree,
    probe_ack,
    write_lua_tree,
)

WHEN = "2026-01-02T03:04:05+00:00"
BUILD = "42.20"

EAT_EVIDENCE: dict[str, object] = {"hunger_before": 0.6, "hunger_after": 0.1}


def full_index(tmp_path: Path) -> SymbolIndex:
    return scan_lua_tree(full_lua_tree(tmp_path)).index()


# ---------------------------------------------------------------------------
# the declared set
# ---------------------------------------------------------------------------


def test_every_capability_named_by_the_blueprint_has_a_probe() -> None:
    declared = {probe.capability for probe in PROBES}
    assert declared == {
        "move_to_square",
        "inventory_transfer",
        "eat_percentage",
        "drink_carried",
        "read_literature",
        "drink_world_source",
        "autonomous_attack",
    }


def test_probe_for_refuses_an_unknown_capability() -> None:
    with pytest.raises(CapabilityError, match="no probe declared"):
        probe_for("drive_a_car")


def test_a_probe_that_cannot_be_verified_must_say_why() -> None:
    with pytest.raises(CapabilityError, match="must say why"):
        ProbeDefinition(
            capability="whatever",
            required_symbols=(),
            confirmation=RuntimeConfirmation(
                action=ActionName.ACTION_WAIT, evidence_keys=("x",), description="d"
            ),
            ceiling=CapabilityState.UNSUPPORTED,
        )


def test_a_probe_may_not_declare_a_static_state_of_verified() -> None:
    with pytest.raises(CapabilityError, match="static scan may never yield"):
        ProbeDefinition(
            capability="whatever",
            required_symbols=(),
            confirmation=RuntimeConfirmation(
                action=ActionName.ACTION_WAIT, evidence_keys=("x",), description="d"
            ),
            static_state=CapabilityState.VERIFIED,
        )


def test_a_confirmation_must_name_the_evidence_it_requires() -> None:
    with pytest.raises(CapabilityError, match="must name the evidence"):
        RuntimeConfirmation(action=ActionName.CONSUME_EAT, evidence_keys=(), description="d")


# ---------------------------------------------------------------------------
# static resolution
# ---------------------------------------------------------------------------


def test_a_full_static_scan_verifies_nothing(tmp_path: Path) -> None:
    report = resolve_all(full_index(tmp_path), build=BUILD, observed_at=WHEN)
    assert report.build == BUILD
    assert all(c.state is not CapabilityState.VERIFIED for c in report.capabilities)
    assert not any(c.has_runtime_evidence for c in report.capabilities)
    assert report.state(EAT_PERCENTAGE) is CapabilityState.AVAILABLE_UNVERIFIED


def test_static_resolution_records_the_file_and_hash_it_found_the_symbol_in(
    tmp_path: Path,
) -> None:
    capability = resolve_static(
        probe_for(EAT_PERCENTAGE), full_index(tmp_path), build=BUILD, observed_at=WHEN
    )
    assert capability.state is CapabilityState.AVAILABLE_UNVERIFIED
    assert all(e.kind is EvidenceKind.STATIC_SCAN for e in capability.evidence)
    found = next(e for e in capability.evidence if e.symbol.endswith("new"))
    assert found.file == "client/TimedActions/ISEatFoodAction.lua"
    assert len(found.file_sha256) == 64
    assert found.detail.startswith("ISEatFoodAction:new(")


def test_a_missing_symbol_makes_the_capability_unsupported_and_names_it() -> None:
    capability = resolve_static(
        probe_for(READ_LITERATURE), SymbolIndex.from_records(()), build=BUILD, observed_at=WHEN
    )
    assert capability.state is CapabilityState.UNSUPPORTED
    assert capability.reason.startswith(REASON_SYMBOL_MISSING)
    assert "ISReadABook" in capability.reason
    assert not capability.usable


def test_the_world_water_source_is_experimental_even_when_present(tmp_path: Path) -> None:
    capability = resolve_static(
        probe_for(DRINK_WORLD_SOURCE), full_index(tmp_path), build=BUILD, observed_at=WHEN
    )
    assert capability.state is CapabilityState.EXPERIMENTAL
    assert not capability.usable
    assert capability.evidence


def test_an_install_without_the_water_action_reports_it_unsupported(tmp_path: Path) -> None:
    index = scan_lua_tree(write_lua_tree(tmp_path, VANILLA_SOURCES)).index()
    report = resolve_all(index, build=BUILD, observed_at=WHEN)
    assert report.state(DRINK_WORLD_SOURCE) is CapabilityState.UNSUPPORTED
    assert missing_symbols(index)[DRINK_WORLD_SOURCE] == (
        "ISTakeWaterAction",
        "ISTakeWaterAction.new",
    )


def test_missing_symbols_is_empty_for_a_complete_install(tmp_path: Path) -> None:
    assert missing_symbols(full_index(tmp_path)) == {}


# ---------------------------------------------------------------------------
# autonomous_attack stays refused
# ---------------------------------------------------------------------------


def test_autonomous_attack_is_unsupported_after_a_full_scan(tmp_path: Path) -> None:
    report = resolve_all(full_index(tmp_path), build=BUILD, observed_at=WHEN)
    attack = report.get(AUTONOMOUS_ATTACK)
    assert attack is not None
    assert attack.state is CapabilityState.UNSUPPORTED
    assert attack.reason == REASON_NO_VERIFIED_API
    assert not report.usable(AUTONOMOUS_ATTACK)


def test_autonomous_attack_stays_unsupported_even_with_a_succeeded_ack(tmp_path: Path) -> None:
    probe = probe_for(AUTONOMOUS_ATTACK)
    static = resolve_static(probe, full_index(tmp_path), build=BUILD, observed_at=WHEN)
    ack = probe_ack(ActionName.ACTION_WAIT, {"never": True, "killed": 3})
    confirmed = confirm(probe, static, ack, build=BUILD, observed_at=WHEN)
    assert confirmed.state is CapabilityState.UNSUPPORTED
    assert confirmed.reason == REASON_NO_VERIFIED_API
    assert not confirmed.has_runtime_evidence


# ---------------------------------------------------------------------------
# runtime confirmation
# ---------------------------------------------------------------------------


def test_confirmation_upgrades_a_static_capability_to_verified(tmp_path: Path) -> None:
    probe = probe_for(EAT_PERCENTAGE)
    static = resolve_static(probe, full_index(tmp_path), build=BUILD, observed_at=WHEN)
    confirmed = confirm(
        probe,
        static,
        probe_ack(ActionName.CONSUME_EAT, EAT_EVIDENCE),
        build=BUILD,
        observed_at=WHEN,
    )
    assert confirmed.state is CapabilityState.VERIFIED
    assert confirmed.has_runtime_evidence
    assert confirmed.usable
    runtime = [e for e in confirmed.evidence if e.kind is EvidenceKind.RUNTIME_PROBE]
    assert runtime[0].probe == EAT_PERCENTAGE


def test_confirmation_is_refused_when_the_symbols_were_never_found() -> None:
    probe = probe_for(EAT_PERCENTAGE)
    static = resolve_static(probe, SymbolIndex.from_records(()), build=BUILD, observed_at=WHEN)
    confirmed = confirm(
        probe,
        static,
        probe_ack(ActionName.CONSUME_EAT, EAT_EVIDENCE),
        build=BUILD,
        observed_at=WHEN,
    )
    assert confirmed.state is CapabilityState.UNSUPPORTED
    assert confirmed is static


def test_confirmation_is_refused_for_an_ack_for_a_different_action(tmp_path: Path) -> None:
    probe = probe_for(EAT_PERCENTAGE)
    static = resolve_static(probe, full_index(tmp_path), build=BUILD, observed_at=WHEN)
    confirmed = confirm(
        probe,
        static,
        probe_ack(ActionName.CONSUME_DRINK, EAT_EVIDENCE),
        build=BUILD,
        observed_at=WHEN,
    )
    assert confirmed.state is CapabilityState.AVAILABLE_UNVERIFIED
    assert confirmed.reason.startswith(REASON_PROBE_NOT_CONFIRMED)
    assert not confirmed.has_runtime_evidence


def test_confirmation_is_refused_when_the_ack_lacks_the_declared_postcondition(
    tmp_path: Path,
) -> None:
    probe = probe_for(INVENTORY_TRANSFER)
    static = resolve_static(probe, full_index(tmp_path), build=BUILD, observed_at=WHEN)
    partial = probe_ack(ActionName.INVENTORY_TRANSFER, {"item_ref": "item:1"})
    confirmed = confirm(probe, static, partial, build=BUILD, observed_at=WHEN)
    assert confirmed.state is CapabilityState.AVAILABLE_UNVERIFIED
    assert "container_ref" in confirmed.reason


def test_confirmation_is_refused_for_an_ack_that_did_not_succeed(tmp_path: Path) -> None:
    probe = probe_for(MOVE_TO_SQUARE)
    static = resolve_static(probe, full_index(tmp_path), build=BUILD, observed_at=WHEN)
    failed = probe_ack(
        ActionName.MOVEMENT_MOVE_TO, {"position": {"x": 1}}, status=ActionStatus.FAILED
    )
    confirmed = confirm(probe, static, failed, build=BUILD, observed_at=WHEN)
    assert confirmed.state is CapabilityState.AVAILABLE_UNVERIFIED
    assert confirmed.reason.startswith(REASON_PROBE_NOT_CONFIRMED)


def test_confirmation_refuses_a_capability_from_a_different_probe(tmp_path: Path) -> None:
    probe = probe_for(EAT_PERCENTAGE)
    other = resolve_static(probe_for(MOVE_TO_SQUARE), full_index(tmp_path), build=BUILD)
    with pytest.raises(CapabilityError, match="cannot be confirmed by probe"):
        confirm(
            probe,
            other,
            probe_ack(ActionName.CONSUME_EAT, EAT_EVIDENCE),
            build=BUILD,
            observed_at=WHEN,
        )


def test_a_hand_written_capability_cannot_smuggle_itself_past_confirmation() -> None:
    # A caller that fabricates the "static" input still cannot reach verified
    # without an ack, because verified is gated on runtime evidence, not on this
    # function's cooperation.
    forged = Capability.unsupported(name=EAT_PERCENTAGE, reason="made up", build=BUILD)
    assert not forged.usable
    with pytest.raises(CapabilityError, match="requires runtime probe evidence"):
        Capability.verified(name=EAT_PERCENTAGE, build=BUILD, evidence=forged.evidence)


def test_resolve_all_produces_one_entry_per_probe_and_a_revision(tmp_path: Path) -> None:
    report = resolve_all(full_index(tmp_path), build=BUILD, observed_at=WHEN, revision=4)
    assert len(report.capabilities) == len(PROBES)
    assert report.revision == 4
    assert report.generated_at == WHEN
    assert set(report.usable_names()) == {
        "drink_carried",
        "eat_percentage",
        "inventory_transfer",
        "move_to_square",
        "read_literature",
    }
