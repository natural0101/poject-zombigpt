"""Persistence of the generated compatibility report, and the cross-build downgrade."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pz_agent_core.capabilities import report_io
from pz_agent_core.capabilities.model import (
    REASON_NO_VERIFIED_API,
    Capability,
    CapabilityReport,
    Evidence,
)
from pz_agent_core.capabilities.probes import EAT_PERCENTAGE, confirm, probe_for, resolve_all
from pz_agent_core.capabilities.report_io import (
    DEFAULT_REPORT_PATH,
    MAX_REPORT_BYTES,
    ReportIOError,
    load_or_empty,
    load_report,
    save_report,
)
from pz_agent_core.capabilities.scanner import WriteRefused, scan_lua_tree
from pz_agent_core.protocol import ActionName, CapabilityState
from tests.fixtures.capability_trees import full_lua_tree, probe_ack, tree_fingerprint

WHEN = "2026-01-02T03:04:05+00:00"
SHA = "b" * 64


def verified_report(build: str, tmp_path: Path) -> CapabilityReport:
    """A report in which ``eat_percentage`` really was confirmed on *build*."""
    index = scan_lua_tree(full_lua_tree(tmp_path / "game")).index()
    report = resolve_all(index, build=build, observed_at=WHEN)
    probe = probe_for(EAT_PERCENTAGE)
    static = report.get(EAT_PERCENTAGE)
    assert static is not None
    confirmed = confirm(
        probe,
        static,
        probe_ack(ActionName.CONSUME_EAT, {"hunger_before": 0.6, "hunger_after": 0.1}),
        build=build,
        observed_at=WHEN,
    )
    return report.with_capabilities([confirmed])


def test_default_path_is_the_gitignored_compat_file() -> None:
    assert Path("compat") / "generated_api_report.json" == DEFAULT_REPORT_PATH


def test_report_round_trips_through_the_file(tmp_path: Path) -> None:
    report = verified_report("42.20", tmp_path)
    path = tmp_path / "state" / DEFAULT_REPORT_PATH
    written = save_report(path, report)

    assert written == len(path.read_bytes())
    loaded = load_report(path, expected_build="42.20")
    assert loaded.report == report
    assert not loaded.downgraded
    assert loaded.build_matches
    assert loaded.report.state(EAT_PERCENTAGE) is CapabilityState.VERIFIED


def test_saving_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    path = tmp_path / "compat" / "generated_api_report.json"
    save_report(path, CapabilityReport(build="42.20"))
    assert sorted(p.name for p in path.parent.iterdir()) == ["generated_api_report.json"]


def test_loading_a_report_from_another_build_downgrades_every_verified_capability(
    tmp_path: Path,
) -> None:
    stale = verified_report("42.19", tmp_path)
    assert stale.state(EAT_PERCENTAGE) is CapabilityState.VERIFIED

    path = tmp_path / "compat" / "generated_api_report.json"
    save_report(path, stale)
    loaded = load_report(path, expected_build="42.20")

    assert loaded.downgraded
    assert loaded.stored_build == "42.19"
    assert loaded.report.build == "42.20"
    assert loaded.report.state(EAT_PERCENTAGE) is CapabilityState.AVAILABLE_UNVERIFIED
    assert not any(c.has_runtime_evidence for c in loaded.report.capabilities)
    assert loaded.report.revision == stale.revision + 1
    assert any("42.19" in note for note in loaded.notes)


def test_a_downgraded_report_still_refuses_the_capabilities_that_were_refused(
    tmp_path: Path,
) -> None:
    path = tmp_path / "compat" / "generated_api_report.json"
    save_report(path, verified_report("42.19", tmp_path))
    loaded = load_report(path, expected_build="42.20")
    assert loaded.report.state("autonomous_attack") is CapabilityState.UNSUPPORTED
    assert not loaded.report.usable("autonomous_attack")
    assert not loaded.report.usable("drink_world_source")


def test_saving_into_the_game_directory_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    game_dir = tmp_path / "ProjectZomboid"
    full_lua_tree(game_dir)
    before = tree_fingerprint(game_dir)

    with pytest.raises(WriteRefused):
        save_report(
            game_dir / "media" / "lua" / "generated_api_report.json",
            CapabilityReport(build="42.20"),
            protected_roots=[game_dir],
        )

    assert tree_fingerprint(game_dir) == before


def test_load_requires_an_expected_build(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    save_report(path, CapabilityReport(build="42.20"))
    with pytest.raises(ReportIOError, match="expected_build must be given"):
        load_report(path, expected_build="")


def test_load_reports_a_missing_file_rather_than_inventing_one(tmp_path: Path) -> None:
    with pytest.raises(ReportIOError, match="cannot stat"):
        load_report(tmp_path / "nope.json", expected_build="42.20")


def test_load_or_empty_treats_a_first_run_as_empty_but_not_as_capable(tmp_path: Path) -> None:
    loaded = load_or_empty(tmp_path / "nope.json", expected_build="42.20")
    assert loaded.report.capabilities == ()
    assert not loaded.report.usable(EAT_PERCENTAGE)
    assert loaded.stored_build == ""
    assert loaded.notes


def test_load_or_empty_reports_a_path_it_cannot_even_inspect(tmp_path: Path) -> None:
    # "I could not look" is not "there is nothing there". Returning an empty
    # report here would tell the caller the ledger is empty on the strength of an
    # error it never saw.
    blocked = tmp_path / "a-file-not-a-directory"
    blocked.write_text("{}", encoding="utf-8")
    with pytest.raises(ReportIOError, match="cannot stat"):
        load_or_empty(blocked / "generated_api_report.json", expected_build="42.20")


def test_load_or_empty_still_fails_on_a_corrupt_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ReportIOError, match="not a valid JSON document"):
        load_or_empty(path, expected_build="42.20")


def test_load_refuses_a_json_array(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ReportIOError, match="expected a JSON object"):
        load_report(path, expected_build="42.20")


def test_load_refuses_a_report_whose_verified_claim_has_no_probe_evidence(tmp_path: Path) -> None:
    forged = {
        "build": "42.20",
        "protocol_version": "1.0",
        "revision": 1,
        "capabilities": {"autonomous_attack": {"state": "verified", "build": "42.20"}},
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(ReportIOError, match="requires runtime probe evidence"):
        load_report(path, expected_build="42.20")


def test_load_refuses_a_report_whose_verified_claim_cites_only_a_static_scan(
    tmp_path: Path,
) -> None:
    static = Evidence.from_scan(
        symbol="ISEatFoodAction.new",
        file="client/ISEatFoodAction.lua",
        file_sha256=SHA,
        signature="ISEatFoodAction:new(character, item, percentage)",
        observed_at=WHEN,
    )
    forged = {
        "build": "42.20",
        "capabilities": {
            "eat_percentage": {
                "state": "verified",
                "build": "42.20",
                "evidence": [static.to_dict()],
            }
        },
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(ReportIOError, match="requires runtime probe evidence"):
        load_report(path, expected_build="42.20")


def test_load_refuses_a_report_that_smuggles_in_another_builds_claim(tmp_path: Path) -> None:
    forged = {
        "build": "42.20",
        "capabilities": {
            "eat_percentage": {
                "state": "available_unverified",
                "build": "42.19",
                "evidence": [
                    Evidence.from_scan(
                        symbol="ISEatFoodAction.new",
                        file="client/ISEatFoodAction.lua",
                        file_sha256=SHA,
                        signature="ISEatFoodAction:new(character, item, percentage)",
                        observed_at=WHEN,
                    ).to_dict()
                ],
            }
        },
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(ReportIOError, match=r"proven on build 42\.19"):
        load_report(path, expected_build="42.20")


def test_load_refuses_a_file_over_the_size_cap(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_bytes(b" " * (MAX_REPORT_BYTES + 1))
    with pytest.raises(ReportIOError, match="exceeds"):
        load_report(path, expected_build="42.20")


def test_load_refuses_a_file_that_is_not_utf8(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_bytes(b"\xff\xfe{}")
    with pytest.raises(ReportIOError, match="not a valid JSON document"):
        load_report(path, expected_build="42.20")


def test_save_refuses_an_oversized_document_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    padded = CapabilityReport(
        build="42.20",
        capabilities=tuple(
            Capability.unsupported(name=f"cap_{i}", reason=REASON_NO_VERIFIED_API + "x" * 150)
            for i in range(60)
        ),
    )
    path = tmp_path / "report.json"
    monkeypatch.setattr(report_io, "MAX_REPORT_BYTES", 512)
    with pytest.raises(ReportIOError, match="exceeds"):
        save_report(path, padded)
    assert not path.exists()


def test_a_failed_write_leaves_no_partial_file_behind(tmp_path: Path) -> None:
    # A directory sitting where the report should go makes the final rename fail
    # after the temporary file has already been written and fsynced.
    path = tmp_path / "compat" / "generated_api_report.json"
    (path / "occupied").mkdir(parents=True)

    with pytest.raises(ReportIOError, match="cannot write report"):
        save_report(path, CapabilityReport(build="42.20"))

    # The half-written temporary must be gone: a stray file next to the report
    # reads as a report to anyone inspecting the directory.
    assert sorted(p.name for p in path.parent.iterdir()) == ["generated_api_report.json"]
    assert path.is_dir()


def test_loading_a_cross_build_report_whose_only_support_was_the_run_refuses_it(
    tmp_path: Path,
) -> None:
    # 'available_unverified' backed by nothing but a probe run on another build
    # has nothing left once that run is discarded. The load must refuse the
    # capability, not fail — and above all not keep claiming availability.
    runtime = Evidence.from_ack(
        probe=EAT_PERCENTAGE,
        symbol="ISEatFoodAction.new",
        ack=probe_ack(ActionName.CONSUME_EAT, {"hunger_before": 0.6, "hunger_after": 0.1}),
        observed_at=WHEN,
    )
    stale = CapabilityReport(
        build="42.19",
        capabilities=(
            Capability.available_unverified(name=EAT_PERCENTAGE, build="42.19", evidence=[runtime]),
        ),
    )
    path = tmp_path / "report.json"
    save_report(path, stale)

    loaded = load_report(path, expected_build="42.20")
    assert loaded.downgraded
    assert loaded.report.state(EAT_PERCENTAGE) is CapabilityState.UNSUPPORTED
    assert not loaded.report.usable(EAT_PERCENTAGE)


def test_a_full_report_stays_well_inside_the_size_cap(tmp_path: Path) -> None:
    padded = CapabilityReport(
        build="42.20",
        capabilities=tuple(
            Capability.unsupported(name=f"cap_{i}", reason=REASON_NO_VERIFIED_API + "x" * 150)
            for i in range(60)
        ),
    )
    path = tmp_path / "report.json"
    assert save_report(path, padded) < MAX_REPORT_BYTES
    assert load_report(path, expected_build="42.20").report == padded
