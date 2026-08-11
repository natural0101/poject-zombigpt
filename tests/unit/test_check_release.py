"""The release gate, including the case it exists for: refusing v1.0.0 today.

The headline test is :func:`test_release_is_refused_because_there_is_no_evidence`.
It asserts that the gate fails on this repository right now, and that it says
why in a sentence naming the file that is absent. That is not a test of a bug —
it is the gate's whole purpose written down, so that anyone who later makes it
pass has to do it by producing evidence rather than by adjusting a check.

The rest are the failures a gate has to get right to be worth having: a claim in
a manifest that the artefact contradicts, a test report nobody produced, a red
suite, and an evidence file whose bytes no longer match the digest recorded for
it.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import sys
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_cli.livetest.scenarios import SCENARIO_IDS

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO_ROOT / "packaging" / "windows"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

build_rc = importlib.import_module("build_rc")
check_release = importlib.import_module("check_release")

_E2E_CLASS: Final = check_release.E2E_MODULE + ".TestTheProtocol"

# The green report carries E2E testcases because the gate now requires them by
# name: a report of green counters with the E2E suite absent is exactly the
# shape ``tests.mcp-e2e`` exists to refuse, so it cannot also be the fixture
# every passing path runs through.
_GREEN_JUNIT: Final = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" '
    'tests="812" time="41.0">'
    f'<testcase classname="{_E2E_CLASS}" name="test_initialize_answers" time="0.2"/>'
    f'<testcase classname="{_E2E_CLASS}" name="test_tools_list_matches" time="0.2"/>'
    "</testsuite></testsuites>\n"
)

_GREEN_BUT_NO_E2E_JUNIT: Final = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" '
    'tests="812" time="41.0">'
    '<testcase classname="tests.unit.test_config" name="test_defaults" time="0.1"/>'
    "</testsuite></testsuites>\n"
)

_GREEN_BUT_E2E_SKIPPED_JUNIT: Final = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="2" '
    'tests="812" time="41.0">'
    f'<testcase classname="{_E2E_CLASS}" name="test_initialize_answers" time="0.0">'
    '<skipped message="platform"/></testcase>'
    f'<testcase classname="{_E2E_CLASS}" name="test_tools_list_matches" time="0.2"/>'
    "</testsuite></testsuites>\n"
)

_RED_JUNIT: Final = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<testsuites><testsuite name="pytest" errors="1" failures="2" skipped="0" '
    'tests="812" time="41.0"/></testsuites>\n'
)

_EMPTY_JUNIT: Final = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" '
    'tests="0" time="0.0"/></testsuites>\n'
)


def _failures(findings: list[Any]) -> dict[str, str]:
    return {finding.check: finding.detail for finding in findings if not finding.ok}


# ---------------------------------------------------------------------------
# building the things the gate reads
# ---------------------------------------------------------------------------


def _repo(root: Path) -> Path:
    """The smallest tree ``build_rc`` calls complete."""
    bat_dir = root / "packaging" / "windows" / "bat"
    bat_dir.mkdir(parents=True)
    for name in build_rc.BAT_NAMES:
        (bat_dir / name).write_text(f"@echo off\nrem {name}\n", encoding="utf-8")
    mod = root / "pz-mod" / "42"
    mod.mkdir(parents=True)
    (mod / "mod.info").write_text("id=pz_agent_bridge\n", encoding="utf-8")
    (root / "configs" / "agent").mkdir(parents=True)
    (root / "configs" / "agent" / "config.example.toml").write_text("# s\n", encoding="utf-8")
    (root / "configs" / "mcp").mkdir(parents=True)
    for name in ("claude-desktop.json", "claude-code.json", "generic-stdio.json"):
        (root / "configs" / "mcp" / name).write_text("{}\n", encoding="utf-8")
    (root / "configs" / "mcp" / "README.md").write_text("# mcp\n", encoding="utf-8")
    (root / "docs").mkdir()
    for name in build_rc.DOC_NAMES:
        (root / "docs" / name).write_text(f"# {name}\n", encoding="utf-8")
    for name in build_rc.META_NAMES:
        (root / name).write_text(f"{name}\n", encoding="utf-8")
    schema = root / "evidence" / "schema"
    schema.mkdir(parents=True)
    for name in build_rc.EVIDENCE_SCHEMA_NAMES:
        (schema / name).write_text('{"type": "object"}\n', encoding="utf-8")
    return root


def _archive(tmp_path: Path, *, with_binaries: bool = True) -> Path:
    repo = _repo(tmp_path / "repo")
    binaries = repo / "bin"
    binaries.mkdir()
    if with_binaries:
        for name in build_rc.BIN_NAMES:
            (binaries / name).write_bytes(b"MZ fake executable")
    report = build_rc.build(repo_root=repo, bin_dir=binaries, output_dir=tmp_path / "out")
    archive: Path = report.archive
    return archive


def _junit(tmp_path: Path, body: str = _GREEN_JUNIT) -> Path:
    path = tmp_path / "pytest.xml"
    path.write_text(body, encoding="utf-8")
    return path


def _evidence(root: Path, *, failing: str = "", tamper: str = "") -> tuple[Path, Path]:
    """A passing evidence tree and the manifest that accounts for it.

    Returns ``(manifest path, evidence root)``. ``failing`` marks one scenario
    as FAIL; ``tamper`` rewrites one scenario's result file after its digest was
    recorded, which is the edit the digests exist to catch.
    """
    root.mkdir(parents=True, exist_ok=True)
    artefacts: list[dict[str, Any]] = []
    scenarios: list[dict[str, Any]] = []
    for scenario_id in SCENARIO_IDS:
        directory = root / scenario_id
        directory.mkdir(parents=True, exist_ok=True)
        for kind, name in (("state", "state.json"), ("result", "result.json")):
            path = directory / name
            # Bytes, and the digest below is taken from the same bytes. Written
            # as text this fixture translated its own newlines on Windows, so
            # every artefact it built was "tampered" before the gate saw it —
            # the fixture reproduced the defect it was meant to be testing past.
            body = (json.dumps({"scenario_id": scenario_id, "kind": kind}) + "\n").encode("utf-8")
            path.write_bytes(body)
            artefacts.append(
                {
                    "scenario_id": scenario_id,
                    "kind": kind,
                    "path": f"{scenario_id}/{name}",
                    "required": True,
                    "present": True,
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "size_bytes": len(body),
                    "problem": "",
                }
            )
        state = "FAIL" if scenario_id == failing else "PASS"
        scenarios.append(
            {
                "scenario_id": scenario_id,
                "state": state,
                "attempt_count": 1,
                "last_run_ms": 1_700_000_000_000,
                "result_sha256": artefacts[-1]["sha256"],
            }
        )
    if tamper:
        (root / tamper / "result.json").write_bytes(b'{"edited": true}\n')

    manifest = root.parent / "release" / "evidence-manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "format": check_release.LIVETEST_MANIFEST_FORMAT,
                "complete": True,
                "commit": "0123456789abcdef",
                "game_builds": ["42.20"],
                "product_version": build_rc.RELEASE_VERSION,
                "scenario_count": len(SCENARIO_IDS),
                "scenarios": scenarios,
                "artefacts": artefacts,
                "totals": {"artefact_count": len(artefacts), "bytes": 0},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return manifest, root


def _run(
    tmp_path: Path,
    *,
    release: bool = False,
    archive: Path | None = None,
    junit: Path | None = None,
    manifest: Path | None = None,
    evidence_dir: Path | None = None,
) -> list[Any]:
    findings: list[Any] = check_release.run(
        release=release,
        archive=archive if archive is not None else _archive(tmp_path),
        junit=junit if junit is not None else _junit(tmp_path),
        manifest=manifest if manifest is not None else tmp_path / "no-such-manifest.json",
        evidence_dir=evidence_dir if evidence_dir is not None else tmp_path / "no-evidence",
    )
    return findings


# ---------------------------------------------------------------------------
# the refusal this script exists for
# ---------------------------------------------------------------------------


def test_release_is_refused_because_there_is_no_evidence(tmp_path: Path) -> None:
    """v1.0.0 cannot be certified from this repository, and says so by name."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = check_release.main(["--release", "--junit", str(_junit(tmp_path))])
    printed = buffer.getvalue()
    assert code == check_release.EXIT_REFUSED
    assert "REFUSED v1.0.0" in printed
    assert "release/evidence-manifest.json" in printed.replace("\\", "/")
    assert "live-test finalize" in printed


def test_the_refusal_survives_the_archive_being_perfect(tmp_path: Path) -> None:
    """A complete archive and a green suite still do not certify a release."""
    findings = _run(tmp_path, release=True)
    assert "evidence.manifest" in _failures(findings)
    assert [f.check for f in findings if not f.ok] == ["evidence.manifest"]


# ---------------------------------------------------------------------------
# the candidate
# ---------------------------------------------------------------------------


def test_a_complete_archive_and_a_green_suite_certify_a_candidate(tmp_path: Path) -> None:
    findings = _run(tmp_path)
    assert _failures(findings) == {}
    checks = {finding.check for finding in findings}
    assert {"archive", "archive.complete", "archive.bat", "archive.bin", "tests"} <= checks


def test_a_candidate_needs_a_test_report_rather_than_a_promise(tmp_path: Path) -> None:
    findings = check_release.run(
        release=False,
        archive=_archive(tmp_path),
        junit=None,
        manifest=tmp_path / "none.json",
        evidence_dir=tmp_path / "none",
    )
    failures = _failures(findings)
    assert set(failures) == {"tests"}
    assert "no test report" in failures["tests"]


def test_a_red_suite_is_not_a_candidate(tmp_path: Path) -> None:
    findings = _run(tmp_path, junit=_junit(tmp_path, _RED_JUNIT))
    assert "2 failure(s) and 1 error(s)" in _failures(findings)["tests"]


def test_a_report_with_no_tests_in_it_proves_nothing(tmp_path: Path) -> None:
    findings = _run(tmp_path, junit=_junit(tmp_path, _EMPTY_JUNIT))
    assert "no tests at all" in _failures(findings)["tests"]


class TestTheE2ESuiteIsObserved:
    """The gate requires ``tests/contract/test_mcp_subprocess_e2e.py`` by name.

    Green aggregate counters cannot distinguish a full run from one where the
    E2E suite was deselected or skipped wholesale — and that suite is the one
    proof that a second process reaches a served core on the platform the
    report came from. Both directions are pinned: absence and skips refuse,
    and the ordinary green fixture (which carries ran E2E cases) passes.
    """

    def test_a_green_report_without_the_e2e_suite_is_refused(self, tmp_path: Path) -> None:
        findings = _run(tmp_path, junit=_junit(tmp_path, _GREEN_BUT_NO_E2E_JUNIT))
        failures = _failures(findings)
        assert set(failures) == {"tests.mcp-e2e"}
        assert check_release.E2E_MODULE in failures["tests.mcp-e2e"]
        remediation = next(f.remediation for f in findings if f.check == "tests.mcp-e2e")
        assert "test_mcp_subprocess_e2e" in remediation

    def test_a_skipped_e2e_case_is_refused(self, tmp_path: Path) -> None:
        findings = _run(tmp_path, junit=_junit(tmp_path, _GREEN_BUT_E2E_SKIPPED_JUNIT))
        assert "1 of 2" in _failures(findings)["tests.mcp-e2e"]

    def test_a_report_with_ran_e2e_cases_satisfies_the_check(self, tmp_path: Path) -> None:
        findings = _run(tmp_path)
        observed = next(f for f in findings if f.check == "tests.mcp-e2e")
        assert observed.ok
        assert "2 testcase(s)" in observed.detail

    def test_the_real_suite_matches_the_name_the_gate_requires(self) -> None:
        """The constant names a module that exists and yields that classname.

        If the E2E suite is ever moved or renamed, the gate would refuse every
        honest report while this constant kept pointing at nothing. The tie is
        pinned from the gate's side: the path derived from ``E2E_MODULE`` must
        be a real test file in this repository.
        """
        parts = check_release.E2E_MODULE.split(".")
        assert (REPO_ROOT.joinpath(*parts[:-1]) / f"{parts[-1]}.py").is_file()


def test_a_missing_archive_says_how_to_build_one(tmp_path: Path) -> None:
    findings = _run(tmp_path, archive=tmp_path / "nothing.zip")
    failures = _failures(findings)
    assert "does not exist" in failures["archive"]
    remediation = next(f.remediation for f in findings if f.check == "archive")
    assert "build_rc.py" in remediation


def test_an_archive_without_the_executables_is_not_a_candidate(tmp_path: Path) -> None:
    findings = _run(tmp_path, archive=_archive(tmp_path, with_binaries=False))
    failures = _failures(findings)
    assert "bin/pz-agent.exe" in failures["archive.complete"]
    assert "pz-agent.exe" in failures["archive.bin"]


def test_an_edited_archive_contradicts_its_own_manifest(tmp_path: Path) -> None:
    """The manifest is a claim; the gate hashes the bytes instead of believing it."""
    original = _archive(tmp_path)
    edited = tmp_path / "edited.zip"
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(edited, "w") as destination:
        for info in source.infolist():
            body = source.read(info.filename)
            if info.filename == "install.bat":
                body = b"@echo off\r\nrem something else entirely\r\n"
            destination.writestr(info, body)
    findings = _run(tmp_path, archive=edited)
    assert "install.bat" in _failures(findings)["archive.digests"]


def test_a_same_size_edit_of_a_member_is_caught_by_its_digest(tmp_path: Path) -> None:
    """An edit that keeps a member's size is caught by re-hashing its bytes.

    The size comparison cannot see this one — the tampered body is exactly as
    long as the recorded one — so the only thing standing between the edit and
    a certification is the ``_sha256_bytes(...) != recorded`` comparison in
    ``_recorded_digests``. Deleting that comparison must fail this test.
    """
    original = _archive(tmp_path)
    edited = tmp_path / "edited-same-size.zip"
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(edited, "w") as destination:
        for info in source.infolist():
            body = source.read(info.filename)
            if info.filename == "install.bat":
                # Flip the last byte: different content, identical length.
                body = body[:-1] + bytes([body[-1] ^ 0xFF])
                assert len(body) == info.file_size
            destination.writestr(info, body)
    findings = _run(tmp_path, archive=edited)
    detail = _failures(findings)["archive.digests"]
    assert "install.bat: content does not match the recorded digest" in detail


def test_a_member_the_index_never_recorded_is_refused(tmp_path: Path) -> None:
    """The other direction of the digest check: a file smuggled in beside them.

    Hashing what the manifest claims proves the claims and nothing more — the
    per-entry loop cannot see a member the index never mentions, so the gate
    used to certify an archive carrying an extra file. The sweep over
    ``names - recorded`` is what this test observes; deleting it must fail
    here while every recorded member still verifies.
    """
    original = _archive(tmp_path)
    padded = tmp_path / "padded.zip"
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(padded, "w") as destination:
        for info in source.infolist():
            destination.writestr(info, source.read(info.filename))
        destination.writestr("extra.bin", b"unrecorded")
    findings = _run(tmp_path, archive=padded)
    detail = _failures(findings)["archive.digests"]
    assert "extra.bin: in the archive but recorded in no manifest entry" in detail


# ---------------------------------------------------------------------------
# the evidence, when there is some
# ---------------------------------------------------------------------------


def test_well_formed_evidence_leaves_only_the_version_to_answer_for(tmp_path: Path) -> None:
    """Twenty passes with hashed artefacts satisfy every evidence check but one.

    The version check is the one still failing, and legitimately: this checkout
    declares a product version that is not the release being certified. It is
    the last thing bumped before a tag, so a test that hid it would be hiding
    the step it is there to enforce.
    """
    manifest, evidence = _evidence(tmp_path / "tree")
    findings = _run(tmp_path, release=True, manifest=manifest, evidence_dir=evidence)
    assert list(_failures(findings)) == ["evidence.version"]
    artefacts = next(f for f in findings if f.check == "evidence.artefacts")
    assert "re-hashed" in artefacts.detail
    assert next(f for f in findings if f.check == "evidence.scenarios").ok


def test_one_scenario_short_of_the_full_set_is_named(tmp_path: Path) -> None:
    manifest, evidence = _evidence(tmp_path / "tree", failing=SCENARIO_IDS[6])
    findings = _run(tmp_path, release=True, manifest=manifest, evidence_dir=evidence)
    detail = _failures(findings)["evidence.scenarios"]
    assert f"{SCENARIO_IDS[6]} is FAIL" in detail
    assert f"1 of {len(SCENARIO_IDS)}" in detail


def test_a_manifest_of_scenarios_that_never_ran_certifies_nothing(tmp_path: Path) -> None:
    """Twenty literal NOT_RUN verdicts refuse the gate, each named as NOT_RUN.

    ``finalize`` refuses to write such a manifest, but the gate accepts an
    arbitrary ``--manifest`` path, so a hand-built all-NOT_RUN manifest is a
    reachable input. It must be treated as what it is — no scenario passed —
    not special-cased as a benign "has not run yet". If ``_scenario_verdicts``
    ever accepted NOT_RUN, the evidence.scenarios finding would come back ok
    and this test would fail on the missing refusal.
    """
    manifest, evidence = _evidence(tmp_path / "tree")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    for entry in document["scenarios"]:
        entry["state"] = "NOT_RUN"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    findings = _run(tmp_path, release=True, manifest=manifest, evidence_dir=evidence)
    detail = _failures(findings)["evidence.scenarios"]
    assert f"{len(SCENARIO_IDS)} of {len(SCENARIO_IDS)} scenario(s) did not pass" in detail
    for scenario_id in SCENARIO_IDS:
        assert f"{scenario_id} is NOT_RUN" in detail


def test_an_edited_result_is_caught_by_its_digest(tmp_path: Path) -> None:
    manifest, evidence = _evidence(tmp_path / "tree", tamper=SCENARIO_IDS[0])
    findings = _run(tmp_path, release=True, manifest=manifest, evidence_dir=evidence)
    detail = _failures(findings)["evidence.artefacts"]
    assert "modified after it was written" in detail
    assert SCENARIO_IDS[0] in detail


def test_a_manifest_missing_a_scenario_entirely_is_refused(tmp_path: Path) -> None:
    manifest, evidence = _evidence(tmp_path / "tree")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["scenarios"] = document["scenarios"][:-1]
    document["artefacts"] = [
        entry for entry in document["artefacts"] if entry["scenario_id"] != SCENARIO_IDS[-1]
    ]
    manifest.write_text(json.dumps(document), encoding="utf-8")
    findings = _run(tmp_path, release=True, manifest=manifest, evidence_dir=evidence)
    failures = _failures(findings)
    assert "absent from the manifest" in failures["evidence.scenarios"]
    assert SCENARIO_IDS[-1] in failures["evidence.artefacts"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("sha256", ""), ("sha256", "not-a-digest"), ("present", False), ("size_bytes", 0)],
)
def test_an_artefact_without_a_usable_digest_is_refused(
    tmp_path: Path, field: str, value: object
) -> None:
    manifest, evidence = _evidence(tmp_path / "tree")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["artefacts"][0][field] = value
    manifest.write_text(json.dumps(document), encoding="utf-8")
    findings = _run(tmp_path, release=True, manifest=manifest, evidence_dir=evidence)
    assert "evidence.artefacts" in _failures(findings)


def test_a_manifest_from_another_format_is_not_read(tmp_path: Path) -> None:
    manifest, evidence = _evidence(tmp_path / "tree")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["format"] = "something/else/2"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    findings = _run(tmp_path, release=True, manifest=manifest, evidence_dir=evidence)
    assert "not 'pz-agent/livetest-manifest/1'" in _failures(findings)["evidence.manifest"]


def test_digests_are_reported_as_unverified_when_the_tree_is_elsewhere(tmp_path: Path) -> None:
    """A manifest checked on another machine says so, rather than implying a check."""
    manifest, _ = _evidence(tmp_path / "tree")
    findings = _run(tmp_path, release=True, manifest=manifest, evidence_dir=tmp_path / "not-here")
    artefacts = next(f for f in findings if f.check == "evidence.artefacts")
    assert artefacts.ok
    assert "not re-hashed" in artefacts.detail
