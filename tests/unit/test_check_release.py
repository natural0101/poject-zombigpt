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

import ast
import hashlib
import importlib
import io
import json
import re
import sys
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_cli.livetest.runner import UNOBSERVED_BUILD
from pz_agent_cli.livetest.scenarios import SCENARIO_IDS
from pz_agent_core.version import MOD_VERSION, SCHEMA_VERSION

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


def _failures_full(findings: list[Any]) -> dict[str, str]:
    """Detail and remediation together, for the assertions about what to do next."""
    return {
        finding.check: f"{finding.detail} {finding.remediation}"
        for finding in findings
        if not finding.ok
    }


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


#: The commit a well-formed manifest and every scenario in it agree on.
_MANIFEST_COMMIT: Final = "0123456789abcdef0123456789abcdef01234567"

#: A different one, for the scenario that passed against other code.
_OTHER_COMMIT: Final = "fedcba9876543210fedcba9876543210fedcba98"


def _evidence(
    root: Path,
    *,
    failing: str = "",
    tamper: str = "",
    commit_elsewhere: str = "",
    game_builds: list[str] | None = None,
) -> tuple[Path, Path]:
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
                # The commit the scenario passed at. Well-formed evidence agrees
                # with the manifest's own commit; ``commit_elsewhere`` names a
                # scenario that passed against different code, which is the case
                # ``evidence.commit`` exists to refuse.
                "commit": _OTHER_COMMIT if scenario_id == commit_elsewhere else _MANIFEST_COMMIT,
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
                "commit": _MANIFEST_COMMIT,
                "game_builds": ["42.20"] if game_builds is None else game_builds,
                "product_version": build_rc.RELEASE_VERSION,
                "mod_version": MOD_VERSION,
                "schema_version": SCHEMA_VERSION,
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


def test_an_archive_claiming_another_release_is_refused(tmp_path: Path) -> None:
    """The headline was the one claim derived from neither the artefact nor a check.

    ``CERTIFIED v1.0.0-rc1`` is built from ``build_rc.RELEASE_VERSION``, the
    *checkout's* constant, in a file whose rule is *"A claim is checked against
    the artefact, never accepted from it."* The archive records its own
    ``release_version`` and nothing read it.

    Scope, stated rather than dressed up: ``DECISIONS.md`` D-012 records that the
    gate runs in the workflow that built, so in the real release path both
    constants are the same object moments apart and this cannot fire. It is a
    tightening — an otherwise-valid archive examined outside that workflow is now
    refused instead of relabelled — not a reachable false success.

    Built by rewriting the manifest of a real, complete archive: the hand-made
    one used while investigating was refused for incompleteness first, which
    would have made this assertion pass for the wrong reason.
    """
    archive = _archive(tmp_path)
    relabelled = tmp_path / "relabelled.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(relabelled, "w") as target:
        for item in source.infolist():
            payload = source.read(item.filename)
            if item.filename == build_rc.MANIFEST_NAME:
                document = json.loads(payload.decode("utf-8"))
                document["release_version"] = "0.4.2"
                payload = json.dumps(document).encode("utf-8")
            target.writestr(item, payload)

    findings = _run(tmp_path, archive=relabelled)

    detail = _failures(findings)["archive.release"]
    assert "0.4.2" in detail
    assert build_rc.RELEASE_VERSION in detail
    # The other archive checks still passed, so the refusal is about the label
    # and not about a broken archive.
    assert "archive.complete" not in _failures(findings)


def test_an_archive_recording_no_release_is_refused(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    stripped = tmp_path / "stripped.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(stripped, "w") as target:
        for item in source.infolist():
            payload = source.read(item.filename)
            if item.filename == build_rc.MANIFEST_NAME:
                document = json.loads(payload.decode("utf-8"))
                del document["release_version"]
                payload = json.dumps(document).encode("utf-8")
            target.writestr(item, payload)

    findings = _run(tmp_path, archive=stripped)

    assert "records no release_version" in _failures(findings)["archive.release"]


def test_the_archive_this_checkout_builds_is_accepted(tmp_path: Path) -> None:
    """The other direction: refusing every archive would pass the two above."""
    findings = _run(tmp_path, archive=_archive(tmp_path))

    assert "archive.release" not in _failures(findings)
    passing = {f.check: f.detail for f in findings if f.ok}
    assert build_rc.RELEASE_VERSION in passing["archive.release"]


def test_evidence_from_another_mod_version_is_refused(tmp_path: Path) -> None:
    """The mod is the code that ran inside the game and did the observing.

    This repository's changelog opens with *"Five versions move independently —
    product, protocol, schema, mod and the supported build range."* The manifest
    records three of them and the gate compared exactly one. ``mod_version`` and
    ``schema_version`` were written into the evidence and read by nobody; found
    by enumerating every key ``finalize`` writes against every key the gate
    reads, rather than one field at a time.
    """
    manifest, evidence = _evidence(tmp_path / "tree")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["mod_version"] = "0.0.9"
    manifest.write_text(json.dumps(document, indent=2), encoding="utf-8")

    findings = _run(tmp_path, release=True, manifest=manifest, evidence_dir=evidence)

    detail = _failures(findings)["evidence.components"]
    assert "0.0.9" in detail
    assert MOD_VERSION in detail


def test_evidence_from_another_schema_version_is_refused(tmp_path: Path) -> None:
    """A schema that moved can move a field out from under a check still finding one."""
    manifest, evidence = _evidence(tmp_path / "tree")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["schema_version"] = "0.9"
    manifest.write_text(json.dumps(document, indent=2), encoding="utf-8")

    findings = _run(tmp_path, release=True, manifest=manifest, evidence_dir=evidence)

    assert "schema_version 0.9" in _failures(findings)["evidence.components"]


def test_a_manifest_missing_a_component_version_is_refused(tmp_path: Path) -> None:
    manifest, evidence = _evidence(tmp_path / "tree")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    del document["mod_version"]
    manifest.write_text(json.dumps(document, indent=2), encoding="utf-8")

    findings = _run(tmp_path, release=True, manifest=manifest, evidence_dir=evidence)

    assert "records no mod_version" in _failures(findings)["evidence.components"]


def test_evidence_from_this_checkouts_components_is_accepted(tmp_path: Path) -> None:
    """The other direction: refusing every manifest would pass the three above."""
    manifest, evidence = _evidence(tmp_path / "tree")

    findings = _run(tmp_path, release=True, manifest=manifest, evidence_dir=evidence)

    assert "evidence.components" not in _failures(findings)
    passing = {f.check: f.detail for f in findings if f.ok}
    assert MOD_VERSION in passing["evidence.components"]
    assert SCHEMA_VERSION in passing["evidence.components"]


def test_every_version_the_manifest_records_is_checked_by_something() -> None:
    """The enumeration itself, so the next field added is not silently ignored.

    ``finalize`` writes the versions; each must be read by some check. This is
    what turned up ``mod_version`` and ``schema_version``, and it is kept so the
    same gap cannot reopen one field at a time.
    """
    runner_source = (
        REPO_ROOT / "packages" / "pz_agent_cli" / "src" / "pz_agent_cli" / "livetest" / "runner.py"
    ).read_text(encoding="utf-8")
    block = runner_source.split("document: JsonDict = {", 1)[1].split("\n    }", 1)[0]
    recorded = {
        name
        for name in re.findall(r'^\s*"([a-z_]+)":', block, re.MULTILINE)
        if name.endswith("_version") or name == "game_builds"
    }
    assert recorded == {"product_version", "mod_version", "schema_version", "game_builds"}, (
        f"the manifest now records {sorted(recorded)}; give the new one a check"
    )

    # Every string constant in the gate's code, not only the literal arguments of
    # ``.get``. The first version of this guard looked for ``manifest.get("x")``
    # and reported ``mod_version`` and ``schema_version`` as unread — they are
    # read through a loop over a dict whose *keys* are those strings, so the
    # pattern could not see the idiom the code actually uses. That is the
    # retraction lesson in miniature: a checker blind to the producer's spelling
    # reports a false absence, and the fix is to widen the checker rather than to
    # bend the code into the shape the checker expected.
    gate_source = (REPO_ROOT / "scripts" / "check_release.py").read_text(encoding="utf-8")
    tree = ast.parse(gate_source)
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    mentioned = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in docstrings
    }

    assert recorded <= mentioned, f"the gate never names {sorted(recorded - mentioned)}"


def test_evidence_that_never_named_a_game_build_is_refused(tmp_path: Path) -> None:
    """The runner's own rule, which nothing enforced.

    ``UNOBSERVED_BUILD`` carries it on the constant: *"Not a guess at the
    supported build: evidence that cannot name the game it ran against closes
    nothing."* Twenty-one of the twenty-two scenarios declare no postcondition
    about the build, so they reach PASS with it unset and the result records
    ``(not observed)`` — which used to reach ``CERTIFIED v1.0.0`` unremarked.
    """

    manifest, evidence = _evidence(tmp_path / "tree", game_builds=[UNOBSERVED_BUILD])

    findings = _run(tmp_path, release=True, manifest=manifest, evidence_dir=evidence)

    detail = _failures(findings)["evidence.game_build"]
    assert UNOBSERVED_BUILD in detail
    assert "nobody looked" in _failures_full(findings)["evidence.game_build"]


def test_evidence_from_an_unsupported_build_is_refused(tmp_path: Path) -> None:
    """A build string that was read, and is the wrong game.

    ``S01_INSTALL`` is the only scenario that looks at the build at all and its
    check is ``observed`` — measured, it passes on ``"41.78"`` and on
    ``"banana"``. So "a build was recorded" was never the same claim as "a
    supported build was recorded".
    """
    manifest, evidence = _evidence(tmp_path / "tree", game_builds=["41.78"])

    findings = _run(tmp_path, release=True, manifest=manifest, evidence_dir=evidence)

    detail = _failures(findings)["evidence.game_build"]
    assert "41.78" in detail
    assert "42.20" in detail


def test_a_manifest_naming_no_build_at_all_is_refused(tmp_path: Path) -> None:
    manifest, evidence = _evidence(tmp_path / "tree", game_builds=[])

    findings = _run(tmp_path, release=True, manifest=manifest, evidence_dir=evidence)

    assert "names no game build" in _failures(findings)["evidence.game_build"]


def test_evidence_from_the_supported_build_is_accepted(tmp_path: Path) -> None:
    """The other direction: refusing every manifest would pass the three above."""
    manifest, evidence = _evidence(tmp_path / "tree")

    findings = _run(tmp_path, release=True, manifest=manifest, evidence_dir=evidence)

    assert "evidence.game_build" not in _failures(findings)
    passing = {f.check: f.detail for f in findings if f.ok}
    assert "42.20" in passing["evidence.game_build"]


def test_the_gate_reads_the_unobserved_marker_from_the_runner(tmp_path: Path) -> None:
    """A second spelling of that constant would make the refusal miss it.

    The gate imports ``UNOBSERVED_BUILD``; this pins that it is still the string
    the runner actually records, so the two cannot drift into a checker that
    agrees only with itself.
    """

    source = (REPO_ROOT / "scripts" / "check_release.py").read_text(encoding="utf-8")
    assert "from pz_agent_cli.livetest.runner import UNOBSERVED_BUILD" in source

    # Docstrings quote the marker on purpose — that is the explanation. What must
    # not exist is a second *value*, so the tree is walked and docstrings are
    # skipped rather than the text being grepped.
    tree = ast.parse(source)
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    literals = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and node.value == UNOBSERVED_BUILD
        and node not in docstrings
    ]

    assert literals == [], (
        f"{UNOBSERVED_BUILD!r} is spelled as a value at line(s) "
        f"{[n.lineno for n in literals]}; import it instead"
    )


def test_a_scenario_that_passed_against_other_code_is_refused(tmp_path: Path) -> None:
    """The gap this check closes, and it is at the last gate before a tag.

    ``_manifest_version`` states the principle — *"evidence from a different
    build is evidence about that build"* — and enforces it on the version
    string, a single literal that does not move for hundreds of commits. The
    commit distinguishes them, and nothing compared it: the manifest's own was
    printed in a detail line and the per-scenario ones were not in the document
    at all.

    It is reachable by the plan's own design. The ledger derives *PASS if any
    attempt passed*, so a scenario keeps its verdict when the code moves, and
    ``E14-M04`` is "fix each incompatibility, re-run every scenario a fix
    touches" — a week of live testing naturally ends with passes spread across
    commits.
    """
    manifest, evidence = _evidence(tmp_path / "tree", commit_elsewhere=SCENARIO_IDS[4])

    findings = _run(tmp_path, release=True, manifest=manifest, evidence_dir=evidence)

    detail = _failures(findings)["evidence.commit"]
    assert SCENARIO_IDS[4] in detail
    assert _OTHER_COMMIT[:8] in detail
    assert _MANIFEST_COMMIT[:8] in detail


def test_a_manifest_that_records_no_commit_cannot_answer_the_question(tmp_path: Path) -> None:
    """A manifest written before the field existed must refuse, not pass silently."""
    manifest, evidence = _evidence(tmp_path / "tree")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    for entry in document["scenarios"]:
        entry.pop("commit")
    manifest.write_text(json.dumps(document, indent=2), encoding="utf-8")

    findings = _run(tmp_path, release=True, manifest=manifest, evidence_dir=evidence)

    detail = _failures(findings)["evidence.commit"]
    assert "no commit is recorded" in detail
    assert SCENARIO_IDS[0] in detail


def test_evidence_all_taken_against_one_commit_is_accepted(tmp_path: Path) -> None:
    """The other direction: a check that refused every manifest would pass the two above."""
    manifest, evidence = _evidence(tmp_path / "tree")

    findings = _run(tmp_path, release=True, manifest=manifest, evidence_dir=evidence)

    assert "evidence.commit" not in _failures(findings)
    passing = {f.check: f.detail for f in findings if f.ok}
    assert _MANIFEST_COMMIT[:8] in passing["evidence.commit"]


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
