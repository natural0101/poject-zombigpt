"""Eight release-pipeline promises, re-checked by execution instead of by reading.

Eight master-plan criteria about the Windows release pipeline were marked
verified because a person read ``.github/workflows/windows.yml`` and the two
PyInstaller specs and saw the right lines. That verification was real once and
started decaying the moment it landed: nothing re-reads a file when it changes,
so the next edit can drop the PATH reduction, split the upload into a second
job, or flip ``console`` in a spec, and every one of those criteria stays green
in the plan while being false in the tree.

These tests are the re-reading, done by the suite on every run. Each one anchors
on the fragment that actually carries its promise — a step name, the
``set "PATH=`` line, the ``EXE(...)`` keyword arguments, the artefact globs —
not on cosmetic spelling, so an edit that keeps a promise passes and an edit
that breaks it fails on the commit that made it. The criterion each test pins
is named in its docstring, so retiring a criterion makes its dead test visible.
"""

from __future__ import annotations

import functools
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
WINDOWS: Final = REPO_ROOT / ".github" / "workflows" / "windows.yml"
SPECS: Final = (
    REPO_ROOT / "packaging" / "windows" / "pz-agent.spec",
    REPO_ROOT / "packaging" / "windows" / "pz-agent-mcp.spec",
)
EVIDENCE_INDEX: Final = REPO_ROOT / "docs" / "control" / "EVIDENCE_INDEX.md"
STATUS: Final = REPO_ROOT / "docs" / "control" / "STATUS.json"

#: The steps this module anchors on, by their ``name:`` in the workflow. Names
#: rather than positions or command text, because a step's name is the one part
#: of it that identifies intent: a renamed step is a changed contract and should
#: fail loudly here, while a reworded comment or a reordered flag should not.
SMOKE: Final = "Both executables answer, without Python on PATH"
TESTS: Final = "Tests"
BUILD_CLI: Final = "Build pz-agent.exe"
BUILD_MCP: Final = "Build pz-agent-mcp.exe"
GATE: Final = "Release gate"
UPLOAD: Final = "Upload the release candidate"

_STEP_ORDER: Final = (TESTS, BUILD_CLI, BUILD_MCP, GATE, UPLOAD)


@functools.cache
def _workflow() -> dict[str, Any]:
    """The parsed workflow, read once for the whole module.

    Parsed rather than grepped, because half of what this module asserts is
    structural — how many jobs, which step precedes which, what a step's
    ``with:`` block holds — and structure regexed out of YAML is a guess.
    """
    assert WINDOWS.is_file(), f"{WINDOWS} is missing, so nothing below can hold"
    loaded = yaml.safe_load(WINDOWS.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{WINDOWS.name} did not parse to a mapping"
    return loaded


def _jobs() -> dict[str, Any]:
    jobs = _workflow().get("jobs")
    assert isinstance(jobs, dict) and jobs, f"{WINDOWS.name} defines no jobs"
    return jobs


def _steps() -> list[dict[str, Any]]:
    """Every step of the single packaging job, in the order they run."""
    jobs = _jobs()
    assert len(jobs) == 1, (
        f"{WINDOWS.name} now has {len(jobs)} jobs ({sorted(jobs)}). This module reads "
        f"'the' job's steps; re-derive which job certifies the RC before widening it."
    )
    job = next(iter(jobs.values()))
    steps = job.get("steps")
    assert isinstance(steps, list) and steps, f"{WINDOWS.name}: the job has no steps"
    return steps


def _step(name: str) -> dict[str, Any]:
    """The one step with this name — one, because a duplicate would make every
    order assertion below ambiguous about which copy it proved something for."""
    found = [step for step in _steps() if step.get("name") == name]
    assert len(found) == 1, (
        f"{WINDOWS.name}: expected exactly one step named {name!r}, found {len(found)}. "
        f"If the step was renamed, the promise it carried needs re-pinning, not deleting."
    )
    return found[0]


def _step_index(name: str) -> int:
    steps = _steps()
    return steps.index(_step(name))


def _run_lines(step: dict[str, Any]) -> list[str]:
    run = step.get("run")
    assert isinstance(run, str) and run.strip(), (
        f"step {step.get('name')!r} has no run: script to inspect"
    )
    return [line.strip() for line in run.splitlines() if line.strip()]


def _asserts_failure_cannot_pass(step: dict[str, Any]) -> None:
    """A step on the certification path must be able to stop the job.

    ``continue-on-error`` makes the job green over a red step, and an ``if:``
    built on ``always()``/``failure()``/``cancelled()`` makes the step run after
    an earlier red one — either would let a failed suite still produce an RC.
    A plain ``if:`` that narrows *when* the step runs is left alone; only the
    forms that survive failure are the defect.
    """
    name = step.get("name")
    assert not step.get("continue-on-error"), (
        f"step {name!r} carries continue-on-error, so its failure no longer stops the job"
    )
    condition = step.get("if", "")
    assert isinstance(condition, str)
    survives = [token for token in ("always(", "failure(", "cancelled(") if token in condition]
    assert not survives, (
        f"step {name!r} has if: {condition!r}, which makes it run even after an earlier "
        f"step failed — a red suite could still reach it"
    )


def _exe_call(spec: Path) -> str:
    """The text of the spec's one ``EXE(...)`` call, extracted by balancing parens.

    Text-level on purpose: a spec is executed by PyInstaller with injected
    globals, so importing it here would mean building, and ``ast`` would tie
    this test to the spec being a call expression rather than to what the call
    says. Exactly one call, because a second EXE would ship a second binary the
    assertions below never looked at.
    """
    text = spec.read_text(encoding="utf-8")
    opened = list(re.finditer(r"\bEXE\s*\(", text))
    assert len(opened) == 1, f"{spec.name}: expected exactly one EXE(...) call, found {len(opened)}"
    depth = 1
    for position in range(opened[0].end(), len(text)):
        if text[position] == "(":
            depth += 1
        elif text[position] == ")":
            depth -= 1
            if depth == 0:
                return text[opened[0].start() : position + 1]
    raise AssertionError(f"{spec.name}: the EXE(...) call never closes")


def _assert_exe_field(spec: Path, field: str, value: str, why: str) -> None:
    call = _exe_call(spec)
    assert re.search(rf"\b{field}\s*=\s*{value}\b", call), (
        f"{spec.name}: the EXE(...) call no longer sets {field}={value}. {why}"
    )


def test_the_anchors_this_module_stands_on_exist() -> None:
    """Guard the guard: every step name and file the tests below anchor on.

    Each helper asserts its own anchor too, but this test fails with the whole
    list at once, so a rename shows up as 'these anchors moved' rather than as
    eight separate failures each reporting one missing piece.
    """
    present = {step.get("name") for step in _steps()}
    missing = [name for name in (*_STEP_ORDER, SMOKE) if name not in present]
    assert not missing, (
        f"{WINDOWS.name} no longer has steps named {missing}. Renaming is fine, but the "
        f"promises those steps carried must be re-pinned here under the new names."
    )
    for path in (*SPECS, EVIDENCE_INDEX, STATUS):
        assert path.is_file(), f"{path} is missing"


def test_the_smoke_step_hides_python_before_running_both_executables() -> None:
    """E11-M02-T006: both executables run on a runner with no project venv on PATH.

    The point of the smoke step is that the bundles need no Python installation,
    and that is only proven if PATH is reduced *before* the binaries run — a
    ``set`` after them, or a PATH that still holds the hosted tool cache, turns
    the step into 'the binaries run where Python happens to be'. And the
    reduction only happens at all under cmd, where ``set "PATH=..."`` is an
    assignment rather than a stray command.
    """
    step = _step(SMOKE)
    assert step.get("shell") == "cmd", (
        f'the {SMOKE!r} step no longer runs under cmd, so its set "PATH=..." line '
        f"does not actually reduce PATH and the proof is gone"
    )
    _asserts_failure_cannot_pass(step)

    lines = _run_lines(step)
    reductions = [
        (index, match)
        for index, line in enumerate(lines)
        if (match := re.match(r'set\s+"PATH=([^"]*)"', line, re.IGNORECASE))
    ]
    assert len(reductions) == 1, (
        f'the {SMOKE!r} step no longer has exactly one set "PATH=..." line; '
        f"found {len(reductions)} in {lines}"
    )
    set_index, set_match = reductions[0]
    entries = [entry for entry in set_match.group(1).split(";") if entry]
    strays = [entry for entry in entries if not entry.lower().startswith("c:\\windows")]
    assert entries and not strays, (
        f"the reduced PATH is supposed to hold only the system directories, but it now "
        f"holds {strays} — anything beyond C:\\Windows can smuggle a Python back in"
    )

    cli_runs = [i for i, line in enumerate(lines) if "pz-agent.exe" in line and "--version" in line]
    mcp_runs = [
        i for i, line in enumerate(lines) if "pz-agent-mcp.exe" in line and "--describe" in line
    ]
    assert cli_runs, f"the {SMOKE!r} step no longer runs pz-agent.exe --version"
    assert mcp_runs, f"the {SMOKE!r} step no longer runs pz-agent-mcp.exe --describe"
    late = [i for i in (*cli_runs, *mcp_runs) if i < set_index]
    assert not late, (
        f"an executable runs on line {min(late)} but PATH is only reduced on line "
        f"{set_index}, so that invocation still saw the runner's full PATH"
    )


@pytest.mark.parametrize("spec", SPECS, ids=lambda path: path.name)
def test_the_spec_builds_a_console_executable(spec: Path) -> None:
    """E11-M02-T007: ``console=True`` in both specs.

    Both binaries live or die on their standard streams — pz-agent prints for a
    person, pz-agent-mcp speaks JSON-RPC over stdio. A windowed build detaches
    those streams on Windows, and the failure would look like a client that
    hangs on initialize, nowhere near this line.
    """
    _assert_exe_field(
        spec,
        "console",
        "True",
        "A windowed binary has no usable stdio on Windows, and both of these "
        "programs are nothing but their stdio.",
    )


@pytest.mark.parametrize("spec", SPECS, ids=lambda path: path.name)
def test_the_spec_neither_strips_nor_packs_the_binary(spec: Path) -> None:
    """E11-M02-T008: ``upx=False`` and ``strip=False`` in both specs.

    These binaries ship unsigned, and a packed or stripped unsigned executable
    is the exact shape SmartScreen and antivirus engines flag. The spec says so
    in a comment beside the fields; this test is the comment made enforceable,
    because a comment survives the edit that contradicts it.
    """
    why = (
        "A packed or stripped unsigned executable reads as malware to SmartScreen "
        "and most antivirus engines, and the user it happens to is not the one "
        "who flipped the flag."
    )
    _assert_exe_field(spec, "upx", "False", why)
    _assert_exe_field(spec, "strip", "False", why)


def test_a_red_gate_stands_between_the_suite_and_the_upload() -> None:
    """E11-M03-T008: a red gate stops the upload step.

    The gate only gates if it runs before the upload and if failure anywhere on
    that path actually stops the job. ``continue-on-error`` on the tests, the
    gate, or the upload — or an ``if:`` that runs on failure — would let a red
    run publish an RC anyway. The diagnostics upload keeps its ``if: always()``:
    it ships the test report, which is most needed precisely when the run is
    red, and it never touches the RC.
    """
    assert _step_index(GATE) < _step_index(UPLOAD), (
        f"the {GATE!r} step no longer precedes {UPLOAD!r}, so the artefact is "
        f"published before anything has judged the run"
    )
    for name in (TESTS, GATE, UPLOAD):
        _asserts_failure_cannot_pass(_step(name))


def test_the_upload_shares_one_job_with_the_suite_and_the_gate() -> None:
    """E11-M04-T003: the RC cannot be produced from a red Windows run.

    Within one job a failed step stops everything after it, so 'tests, then
    build, then gate, then upload' in that order is the whole proof. Split the
    upload into a second job and that guarantee moves into ``needs:`` edges and
    result expressions that each have failure modes of their own — so the pin
    is: one job, and the steps in dependency order inside it.
    """
    jobs = _jobs()
    assert len(jobs) == 1, (
        f"{WINDOWS.name} now has jobs {sorted(jobs)}. With more than one job the "
        f"'a red step stops the upload' argument no longer follows from step order "
        f"alone and has to be re-established over the needs: graph."
    )
    job = next(iter(jobs.values()))
    assert "needs" not in job, (
        "the packaging job now depends on another job, so part of the certification "
        "path lives outside the step order this module verifies"
    )
    order = [(name, _step_index(name)) for name in _STEP_ORDER]
    indices = [index for _, index in order]
    assert indices == sorted(indices), (
        f"the certification steps run out of order: {order}. "
        f"Each one is only meaningful downstream of the ones before it."
    )


def test_the_upload_records_the_archive_and_its_digest() -> None:
    """E11-M04-T004: the artefact and its sha256 are both recorded.

    The digest is the archive's identity — EVIDENCE_INDEX.md and STATUS.json
    both name the RC by it — so an upload that ships the ZIP without its
    ``.sha256`` publishes an artefact nothing downstream can verify. And with
    ``if-no-files-found`` at anything but ``error``, a build that produced
    neither file uploads an empty artefact and stays green.
    """
    step = _step(UPLOAD)
    with_block = step.get("with")
    assert isinstance(with_block, dict), f"the {UPLOAD!r} step has no with: block"
    path = with_block.get("path")
    assert isinstance(path, str), f"the {UPLOAD!r} step uploads no path"
    entries = {line.strip() for line in path.splitlines() if line.strip()}
    for required in ("dist/pz-agent-windows-*.zip", "dist/pz-agent-windows-*.zip.sha256"):
        assert required in entries, (
            f"the {UPLOAD!r} step no longer uploads {required}; it ships "
            f"{sorted(entries)}, which is the artefact without its identity or the "
            f"identity without its artefact"
        )
    assert with_block.get("if-no-files-found") == "error", (
        f"the {UPLOAD!r} step tolerates missing files, so a build that produced "
        f"nothing still uploads a green, empty artefact"
    )


def test_the_evidence_index_carries_the_digest_status_derives() -> None:
    """E11-M04-T005: the index carries the artefact digest.

    STATUS.json is generated and EVIDENCE_INDEX.md is written by hand, which is
    exactly how they drift: the workflow rebuilds the RC, the derived record
    picks up the new digest, and the prose keeps naming an archive that no
    longer exists. The index row must hold the same 64-hex digest as
    ``release_candidate.archive_sha256``, or the index is describing the wrong
    artefact.
    """
    status = json.loads(STATUS.read_text(encoding="utf-8"))
    assert isinstance(status, dict)
    derived = status.get("release_candidate", {}).get("archive_sha256")
    assert isinstance(derived, str) and re.fullmatch(r"[0-9a-f]{64}", derived), (
        f"STATUS.json's release_candidate.archive_sha256 is not a sha256: {derived!r}"
    )

    index_text = EVIDENCE_INDEX.read_text(encoding="utf-8")
    rows = re.findall(r"archive sha256[^\n]*?([0-9a-fA-F]{64})", index_text)
    assert len(rows) == 1, (
        f"{EVIDENCE_INDEX.name}: expected exactly one 'archive sha256' row carrying a "
        f"64-hex digest, found {len(rows)} — the RC's identity must be stated once"
    )
    assert rows[0].lower() == derived, (
        f"{EVIDENCE_INDEX.name} names archive {rows[0]} but STATUS.json derives "
        f"{derived}. The index has drifted from the record it is supposed to summarise; "
        f"one of the two is describing an RC that is not the current one."
    )


def test_the_evidence_index_names_the_same_commit_and_run_as_the_record() -> None:
    """The index states the rule and the check above only enforced a third of it.

    ``EVIDENCE_INDEX.md`` opens its release-candidate table with the standard it
    holds itself to:

        The digest is the identity: an RC is *this* archive, from *this* commit,
        by *this* run, and a claim about "the RC" that names none of the three is
        a claim about nothing.

    Three things — and only the digest was compared against ``STATUS.json``. So
    the index could carry the right sha256 beside the *wrong* source commit and
    the *wrong* workflow run and the whole suite stayed green; demonstrated by
    planting both, one at a time, against the real files. That is the STALE
    IDENTITY family in the one document whose subject is identity, and it is a
    hand-written table beside a generated record, which is exactly the pair that
    drifts. Five consecutive rebuilds updated it by hand.

    The source commit is also required to resolve here and to be an ancestor of
    HEAD: a 40-hex string that is not in this history names an RC built from
    another branch, which no amount of matching STATUS.json would make true.

    What is *not* checked, said plainly rather than implied: the artefact id in
    the ``archive`` row. ``STATUS.json`` records no artefact id, so there is
    nothing here to hold it against, and inventing a second source for it would
    be a check agreeing with itself.
    """
    status = json.loads(STATUS.read_text(encoding="utf-8"))
    recorded = status.get("release_candidate", {})
    index_text = EVIDENCE_INDEX.read_text(encoding="utf-8")

    commits = re.findall(r"source commit[^\n]*?([0-9a-fA-F]{40})", index_text)
    runs = re.findall(r"workflow run[^\n]*?/actions/runs/(\d+)", index_text)

    assert len(commits) == 1, (
        f"{EVIDENCE_INDEX.name}: expected exactly one 'source commit' row carrying a "
        f"40-hex sha, found {len(commits)} — the RC's identity must be stated once"
    )
    assert len(runs) == 1, (
        f"{EVIDENCE_INDEX.name}: expected exactly one 'workflow run' row carrying a "
        f"run id, found {len(runs)} — the RC's identity must be stated once"
    )

    assert commits[0].lower() == str(recorded.get("source_commit", "")).lower(), (
        f"{EVIDENCE_INDEX.name} says the RC was built from {commits[0][:8]} and "
        f"STATUS.json records {str(recorded.get('source_commit'))[:8]}; one of them "
        f"describes an archive that was never built"
    )
    assert runs[0] == str(recorded.get("workflow_run", "")), (
        f"{EVIDENCE_INDEX.name} credits run {runs[0]} and STATUS.json records "
        f"{recorded.get('workflow_run')}; the archive cannot have come from both"
    )

    resolved = subprocess.run(
        ["git", "cat-file", "-e", f"{commits[0]}^{{commit}}"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert resolved.returncode == 0, (
        f"{EVIDENCE_INDEX.name} names source commit {commits[0][:8]}, which does not "
        f"resolve in this clone"
    )
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commits[0], "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert ancestor.returncode == 0, (
        f"{EVIDENCE_INDEX.name} names source commit {commits[0][:8]}, which is not an "
        f"ancestor of HEAD; the RC it describes was built from another history"
    )


def test_the_gate_reads_the_report_the_suite_wrote() -> None:
    """The gate's verdict is about the run only if both name the same report file.

    The tests write a JUnit report and the gate judges it; rename either side
    alone and the gate reads a stale file from a previous run, or nothing, and
    its verdict is about the wrong evidence. The two filenames are extracted
    from the real commands and compared, so the pin survives an agreed rename
    and fails only on the split.
    """
    tests_run = "\n".join(_run_lines(_step(TESTS)))
    written = re.search(r"--junitxml(?:=|\s+)(\S+)", tests_run)
    assert written, (
        f"the {TESTS!r} step no longer writes a JUnit report, so the gate has only "
        f"the step's colour to go on, and a colour is not an artefact"
    )

    gate_run = "\n".join(_run_lines(_step(GATE)))
    read = re.search(r"--junit(?:=|\s+)(\S+)", gate_run)
    assert read, f"the {GATE!r} step no longer passes a JUnit report to the gate script"
    assert written.group(1) == read.group(1), (
        f"the suite writes {written.group(1)} but the gate reads {read.group(1)}; the "
        f"gate is judging a file the suite never produced on this run"
    )
