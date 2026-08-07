#!/usr/bin/env python3
"""Epics 1-5: baseline, Windows portability, evidence, installer, Local Core RPC.

Task tuples are ``(action, weight, pass_criterion, verify_command,
regression_test, evidence)``. Dependencies chain within a milestone unless an
extra one is named, because a task that could run before its predecessor
belongs in a different milestone.
"""

from __future__ import annotations

from scripts.plan_model import Check, Epic, Milestone, Task

RC = "https://github.com/natural0101/poject-zombigpt/actions/workflows/windows.yml"
CI = "https://github.com/natural0101/poject-zombigpt/actions/workflows/ci.yml"


def _tasks(
    epic: str,
    milestone: str,
    subsystem: str,
    band: str,
    owner: str,
    *,
    rows: list[tuple[str, int, str, str, str, str]],
    first_depends_on: tuple[str, ...] = (),
) -> tuple[Task, ...]:
    built: list[Task] = []
    for index, (action, weight, criterion, verify, test, evidence) in enumerate(rows, start=1):
        identifier = f"{epic}-{milestone}-T{index:03d}"
        depends = first_depends_on if index == 1 else (built[-1].id,)
        built.append(
            Task(
                id=identifier,
                subsystem=subsystem,
                action=action,
                weight=weight,
                band=band,
                owner=owner,
                pass_criterion=criterion,
                verify_command=verify,
                regression_test=test,
                evidence=evidence,
                depends_on=depends,
            )
        )
    return tuple(built)


# ---------------------------------------------------------------------------
# E01 — baseline and audit
# ---------------------------------------------------------------------------

E01 = Epic(
    id="E01",
    title="Baseline and audit",
    subsystem="control",
    integration_scenario=(
        "scripts/check_master_plan.py runs against a deliberately falsified plan "
        "— a PASS with no evidence, a weighted percent that disagrees with the "
        "sum, a task passing on an unfinished dependency — and refuses each one."
    ),
    required_ci=CI,
    milestones=(
        Milestone(
            id="E01-M01",
            title="Repository and branch baseline",
            tasks=_tasks(
                "E01",
                "M01",
                "control",
                "audit",
                "remote",
                rows=[
                    (
                        "Record every remote branch and its head SHA",
                        1,
                        "branches.txt lists each branch with a 40-character SHA",
                        "git branch -r --format='%(refname:short) %(objectname)'",
                        "tests/unit/test_control_baseline_evidence.py::test_each_branch_line_names_a_branch_and_a_full_sha",
                        "docs/control/evidence/step-01-10/branches.txt",
                    ),
                    (
                        "Record the SHA the RC work branched from",
                        1,
                        "the branch-point SHA is recorded and resolvable with git cat-file",
                        "git merge-base dev fix/windows-mcp-voice-runtime",
                        "tests/unit/test_control_baseline_evidence.py::test_the_branch_point_is_recorded_and_reachable",
                        "docs/control/evidence/step-01-10/branches.txt",
                    ),
                    (
                        "Record the Linux suite result at the branch point",
                        1,
                        "a pytest summary line with counts, from a run at that SHA",
                        "bash scripts/check.sh",
                        "tests/unit/test_control_baseline_evidence.py::test_the_linux_baseline_records_a_count_and_the_command_that_produced_it",
                        "docs/control/evidence/step-01-10/linux-baseline.txt",
                    ),
                    (
                        "Record the Windows suite result at the branch point",
                        2,
                        "a pytest summary line from a windows-latest run, with the run id",
                        "read the windows workflow run for the branch-point SHA",
                        "tests/unit/test_control_baseline_evidence.py::test_the_windows_baseline_lists_the_failures_it_counted",
                        "docs/control/evidence/step-01-10/windows-failures.txt",
                    ),
                    (
                        "Record every CI workflow and the events that trigger it",
                        1,
                        "each workflow file is listed with its on: triggers",
                        "cat .github/workflows/*.yml",
                        "tests/unit/test_control_baseline_evidence.py::test_the_workflow_evidence_names_both_workflows",
                        "docs/control/evidence/step-01-10/windows-workflow-runs.txt",
                    ),
                    (
                        "Confirm both workflows trigger on the RC branch pattern",
                        2,
                        "a push to fix/** produces a run in both workflows",
                        "read the runs list filtered to the branch",
                        "tests/unit/test_control_baseline_evidence.py::test_the_workflow_evidence_names_both_workflows",
                        "docs/control/evidence/step-01-10/windows-workflow-runs.txt",
                    ),
                ],
            ),
            checks=(
                Check(
                    id="E01-M01-C01",
                    statement=(
                        "Every recorded SHA resolves in this repository, so the baseline "
                        "describes a state that existed rather than one that was typed."
                    ),
                    command="git cat-file -e <each recorded SHA>",
                ),
            ),
        ),
        Milestone(
            id="E01-M02",
            title="Defect inventory",
            tasks=_tasks(
                "E01",
                "M02",
                "control",
                "audit",
                "remote",
                rows=[
                    (
                        "Split the Windows failures by root cause",
                        2,
                        "every failure maps to exactly one named cause; the causes are disjoint",
                        "read the windows job log and group the failures",
                        "",
                        "docs/control/DECISIONS.md",
                    ),
                    (
                        "Record a reproduction command for each root cause",
                        2,
                        "each cause carries a command that produces the failure",
                        "run each recorded command",
                        "",
                        "docs/control/BLOCKERS.md",
                    ),
                    (
                        "Inventory every evidence writer that opens a file in text mode",
                        3,
                        "the list is complete: a grep for write_text and open(..,'w') over "
                        "the evidence packages returns nothing outside it",
                        "grep -rn \"write_text\\|open(.*'w')\" packages/*/src",
                        "tests/contract/test_evidence_bytes_are_portable.py",
                        "docs/control/DECISIONS.md",
                    ),
                    (
                        "Inventory every manifest field recorded with a native separator",
                        3,
                        "each site is named with file and line",
                        "grep -rn 'relative_to' packages installer",
                        "tests/contract/test_portable_paths.py",
                        "docs/control/DECISIONS.md",
                    ),
                    (
                        "Inventory POSIX-only process calls on Windows-executable paths",
                        3,
                        "os.geteuid, signal.SIGKILL and os.kill(pid, 0) are each located",
                        "grep -rn 'geteuid\\|SIGKILL\\|os.kill' packages tests",
                        "tests/unit/test_cli_supervisor.py",
                        "docs/control/DECISIONS.md",
                    ),
                    (
                        "Inventory chmod-based permission tests",
                        3,
                        "every test that makes a file unreadable by chmod is located",
                        "grep -rn 'chmod' tests",
                        "tests/unit/test_capabilities_scanner.py",
                        "docs/control/DECISIONS.md",
                    ),
                    (
                        "Inventory JSON assembled from f-strings rather than json.dumps",
                        2,
                        "every site that builds JSON by string concatenation is located",
                        "grep -rn 'f\\\"{{' packages/*/src",
                        "tests/contract/test_mcp_snippet_is_json.py",
                        "docs/control/DECISIONS.md",
                    ),
                    (
                        "Inventory ports declared with no concrete implementation",
                        3,
                        "each Protocol in ports.py is matched to its implementations, "
                        "and the ones with none are named",
                        "grep -rn 'class .*Port' packages/pz_agent_mcp/src",
                        "",
                        "docs/control/DECISIONS.md",
                    ),
                ],
            ),
            checks=(
                Check(
                    id="E01-M02-C01",
                    statement=(
                        "The inventory accounts for every Windows failure in the baseline "
                        "run: the count of failures mapped equals the count observed."
                    ),
                    command="compare the grouped causes against the baseline failure list",
                ),
            ),
        ),
        Milestone(
            id="E01-M03",
            title="Control instrumentation",
            tasks=_tasks(
                "E01",
                "M03",
                "control",
                "audit",
                "remote",
                rows=[
                    (
                        "Create docs/control/ with the plan, blockers, decisions and evidence "
                        "index",
                        1,
                        "each named file exists and is non-empty",
                        "ls docs/control/",
                        "",
                        "docs/control/PLAN.md",
                    ),
                    (
                        "Define the five-level plan model with weight bands",
                        2,
                        "EPIC/MILESTONE/TASK/CHECK/EVIDENCE are types, and a weight "
                        "outside its band is rejected",
                        ".venv/bin/pytest tests/unit/test_master_plan.py -q",
                        "tests/unit/test_master_plan.py",
                        "scripts/plan_model.py",
                    ),
                    (
                        "Emit docs/control/MASTER_PLAN.yaml from the task definitions",
                        2,
                        "the emitted YAML parses and every task carries all fourteen fields",
                        ".venv/bin/python scripts/build_master_plan.py --check",
                        "tests/unit/test_master_plan.py",
                        "docs/control/MASTER_PLAN.yaml",
                    ),
                    (
                        "Implement the weighted progress calculator",
                        2,
                        "progress equals the summed weight of PASS tasks over the summed "
                        "weight of all tasks, and is computed rather than stored",
                        ".venv/bin/python scripts/master_report.py --json",
                        "tests/unit/test_master_plan.py",
                        "scripts/master_report.py",
                    ),
                    (
                        "Implement the plan gate that refuses an unsupported claim",
                        3,
                        "the gate exits non-zero on a PASS with no evidence, no commit, "
                        "an unmet dependency, a missing evidence path, or a live task "
                        "claimed from this environment",
                        ".venv/bin/python scripts/check_master_plan.py",
                        "tests/unit/test_master_plan.py",
                        "scripts/check_master_plan.py",
                    ),
                    (
                        "Prove each refusal by planting the violation it is meant to catch",
                        3,
                        "every refusal in the gate has a test that fails without it",
                        ".venv/bin/pytest tests/unit/test_master_plan.py -q",
                        "tests/unit/test_master_plan.py",
                        "tests/unit/test_master_plan.py",
                    ),
                ],
            ),
            checks=(
                Check(
                    id="E01-M03-C01",
                    statement=(
                        "The weighted percent cannot be raised by adding tasks: inserting "
                        "a NOT_STARTED task lowers it, and inserting a PASS one raises it "
                        "by exactly its weight share."
                    ),
                    command=".venv/bin/pytest tests/unit/test_master_plan.py -k weight -q",
                ),
                Check(
                    id="E01-M03-C02",
                    statement=(
                        "No metric can be reported above zero while every epic behind it "
                        "is untouched."
                    ),
                    command=".venv/bin/pytest tests/unit/test_master_plan.py -k metric -q",
                ),
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# E02 — Windows portability
# ---------------------------------------------------------------------------

_E02_PATHS = [
    (
        "Introduce portable_relative_path() as the one system-to-recorded conversion",
        4,
        "the function returns a POSIX-separated path relative to a root, and "
        "raises rather than falling back to an absolute path",
        ".venv/bin/pytest tests/contract/test_portable_paths.py -q",
        "tests/contract/test_portable_paths.py",
        "packages/pz_agent_core/src/pz_agent_core/platform/paths.py",
    ),
    (
        "Preserve the path flavour rather than coercing to the running platform's",
        5,
        "portable_relative_path(PureWindowsPath, PureWindowsPath) returns POSIX "
        "separators on Linux, and dropping as_posix() fails a test there",
        ".venv/bin/pytest tests/contract/test_portable_paths.py -q",
        "tests/contract/test_portable_paths.py::test_a_windows_path_is_recorded_with_posix_separators",
        "packages/pz_agent_core/src/pz_agent_core/platform/paths.py",
    ),
    (
        "Raise PathNotUnderRoot instead of recording an absolute path",
        4,
        "a path outside the root raises; no code path returns the absolute form",
        ".venv/bin/pytest tests/contract/test_portable_paths.py -q",
        "tests/contract/test_portable_paths.py::test_a_path_outside_the_root_is_refused_rather_than_recorded_absolute",
        "packages/pz_agent_core/src/pz_agent_core/platform/paths.py",
    ),
    (
        "Fix portable_posix so a drive does not keep a trailing backslash",
        4,
        "portable_posix(PureWindowsPath(r'C:\\Users\\x')) == 'C:/Users/x'",
        ".venv/bin/pytest tests/contract/test_portable_paths.py -q",
        "tests/contract/test_portable_paths.py::test_portable_posix_keeps_the_drive_because_it_is_a_rendering",
        "packages/pz_agent_core/src/pz_agent_core/platform/paths.py",
    ),
    (
        "Move installer manifest entries onto the POSIX separator",
        4,
        "no manifest path contains a backslash, a drive or a leading separator",
        ".venv/bin/pytest tests/unit/test_installer_windows.py -q",
        "tests/unit/test_installer_windows.py::test_every_manifest_path_is_portable_rather_than_native",
        "installer/pz_agent_installer.py",
    ),
    (
        "Make the manifest independent of where the install happened",
        4,
        "two installs into different roots produce identical path lists",
        ".venv/bin/pytest tests/unit/test_installer_windows.py -q",
        "tests/unit/test_installer_windows.py::test_the_manifest_does_not_depend_on_where_it_was_installed",
        "installer/pz_agent_installer.py",
    ),
    (
        "Correct the document-link checks for Windows separators",
        3,
        "the archive link check passes on a native-separator filesystem",
        ".venv/bin/pytest tests/contract/test_archive_documents_resolve.py -q",
        "tests/contract/test_archive_documents_resolve.py",
        "tests/contract/test_archive_documents_resolve.py",
    ),
    (
        "Give the test helper that walks a tree a portable naming function",
        3,
        "_tree() names files the way the manifest does on both platforms",
        ".venv/bin/pytest tests/unit/test_installer_windows.py -q",
        "tests/unit/test_installer_windows.py::test_the_round_trip_leaves_no_residue_but_the_config",
        "tests/unit/test_installer_windows.py",
    ),
]

_E02_REDACTION = [
    (
        "Emit a stable separator after every placeholder",
        4,
        "<ZOMBOID>/logs on every platform, never <ZOMBOID>\\logs",
        ".venv/bin/pytest tests/contract/test_windows_path_shapes.py -q",
        "tests/contract/test_windows_path_shapes.py",
        "packages/pz_agent_core/src/pz_agent_core/diagnostics/redaction.py",
    ),
    (
        "Order the redaction rules longest-literal-first",
        4,
        "a path inside the Zomboid directory reports user_dir, not home_dir",
        ".venv/bin/pytest tests/contract/test_windows_path_shapes.py -q",
        "tests/contract/test_windows_path_shapes.py",
        "packages/pz_agent_core/src/pz_agent_core/diagnostics/redaction.py",
    ),
    (
        "Match a percent-encoded separator (%5C, %2F) in a path tail",
        4,
        "quote()d Windows paths redact to <ZOMBOID>/x rather than <ZOMBOID>%5Cx",
        ".venv/bin/pytest tests/contract/test_windows_path_shapes.py -q",
        "tests/contract/test_windows_path_shapes.py::test_a_percent_encoded_separator_is_normalised_like_any_other",
        "packages/pz_agent_core/src/pz_agent_core/diagnostics/redaction.py",
    ),
    (
        "Match a path that mixes separators by matching each position independently",
        5,
        r"C:\Users\Иван/Zomboid reports user_dir; enumerated spellings cannot cover this",
        ".venv/bin/pytest tests/contract/test_windows_path_shapes.py -q",
        "tests/contract/test_windows_path_shapes.py::test_a_path_that_mixes_separators_still_finds_the_longest_directory",
        "packages/pz_agent_core/src/pz_agent_core/diagnostics/redaction.py",
    ),
    (
        "Apply percent-encoding per segment rather than to the whole literal",
        4,
        "a partly-encoded path matches as readily as a wholly encoded one",
        ".venv/bin/pytest tests/contract/test_windows_path_shapes.py -q",
        "tests/contract/test_windows_path_shapes.py",
        "packages/pz_agent_core/src/pz_agent_core/diagnostics/redaction.py",
    ),
    (
        "Accept PurePath and PureWindowsPath in build_redactor",
        3,
        "a Windows shape can be redacted from a Linux test without a real filesystem",
        ".venv/bin/pytest tests/contract/test_windows_path_shapes.py -q",
        "tests/contract/test_windows_path_shapes.py",
        "packages/pz_agent_core/src/pz_agent_core/diagnostics/redaction.py",
    ),
    (
        "Strike out a profile containing a space as one unit",
        5,
        r"C:\Users\John Smith\Zomboid redacts with no surname left in the output",
        ".venv/bin/pytest tests/contract/test_windows_path_shapes.py -q",
        "tests/contract/test_windows_path_shapes.py::test_a_profile_with_a_space_in_it_is_struck_out_whole",
        "packages/pz_agent_core/src/pz_agent_core/diagnostics/redaction.py",
    ),
    (
        "Reduce an unknown absolute Windows path to its basename",
        4,
        "D:\\Games\\...\\x.txt loses its directories and keeps x.txt",
        ".venv/bin/pytest tests/contract/test_windows_path_shapes.py -q",
        "tests/contract/test_windows_path_shapes.py::test_an_unknown_absolute_windows_path_still_loses_its_directories",
        "packages/pz_agent_core/src/pz_agent_core/diagnostics/redaction.py",
    ),
    (
        "Assert the credential rules remove the secret, not merely insert a placeholder",
        5,
        "for every credential shape, the secret is absent from the output",
        ".venv/bin/pytest tests/unit/test_diagnostics_redaction.py -q",
        "tests/unit/test_diagnostics_redaction.py::test_credential_shapes_are_struck_out",
        "tests/unit/test_diagnostics_redaction.py",
    ),
    (
        "Cover a credential value of any length, so a one-character value rule fails",
        5,
        "api_key=<200 chars> redacts to api_key=<REDACTED> exactly",
        ".venv/bin/pytest tests/unit/test_diagnostics_redaction.py -q",
        "tests/unit/test_diagnostics_redaction.py::test_a_credential_value_is_struck_out_whole_whatever_its_length",
        "tests/unit/test_diagnostics_redaction.py",
    ),
]

_E02_PROCESS = [
    (
        "Remove os.geteuid() from any path a Windows test reaches",
        4,
        "no test skips or branches on geteuid",
        "grep -rn 'geteuid' tests packages",
        "tests/unit/test_capabilities_scanner.py",
        "tests/unit/test_capabilities_scanner.py",
    ),
    (
        "Replace chmod-based unreadable-file tests with an injected PermissionError",
        4,
        "the unreadable case is exercised without changing a file mode",
        ".venv/bin/pytest tests/unit/test_capabilities_scanner.py -q",
        "tests/unit/test_capabilities_scanner.py",
        "tests/unit/test_capabilities_scanner.py",
    ),
    (
        "Replace the second chmod-based test in the capability report writer",
        4,
        "the same, for the report I/O path",
        ".venv/bin/pytest tests/unit/test_capabilities_report_io.py -q",
        "tests/unit/test_capabilities_report_io.py",
        "tests/unit/test_capabilities_report_io.py",
    ),
    (
        "Remove signal.SIGKILL from any path a Windows test reaches",
        4,
        "no test sends SIGKILL",
        "grep -rn 'SIGKILL' tests packages",
        "tests/unit/test_cli_supervisor.py",
        "tests/unit/test_cli_supervisor.py",
    ),
    (
        "Introduce SpawnedProcess with pid, poll, terminate, wait and stop",
        4,
        "a caller can observe and end a child without a signal number",
        ".venv/bin/pytest tests/unit/test_cli_supervisor.py -q",
        "tests/unit/test_cli_supervisor.py",
        "packages/pz_agent_cli/src/pz_agent_cli/supervisor.py",
    ),
    (
        "Expose spawn_detached() returning that handle",
        4,
        "the supervisor's liveness test holds a handle rather than a bare pid",
        ".venv/bin/pytest tests/unit/test_cli_supervisor.py -q",
        "tests/unit/test_cli_supervisor.py",
        "packages/pz_agent_cli/src/pz_agent_cli/supervisor.py",
    ),
    (
        "Replace os.kill(pid, 0) liveness with a portable probe",
        5,
        "liveness uses OpenProcess+GetExitCodeProcess on Windows; os.kill(pid,0) "
        "there terminates rather than probes",
        ".venv/bin/pytest tests/unit/test_rpc_token_and_descriptor.py -q",
        "tests/unit/test_rpc_token_and_descriptor.py::TestFindingAServer",
        "packages/pz_agent_core/src/pz_agent_core/rpc/descriptor.py",
    ),
    (
        "Prove a dead pid is detected using a pid that was really spawned and reaped",
        4,
        "the test does not guess at a free pid number",
        ".venv/bin/pytest tests/unit/test_rpc_token_and_descriptor.py -q",
        "tests/unit/test_rpc_token_and_descriptor.py::TestFindingAServer::test_a_descriptor_naming_a_dead_process_is_refused",
        "tests/unit/test_rpc_token_and_descriptor.py",
    ),
]

_E02_TEXTMODE = [
    (
        "Write every hashed JSON document in binary mode",
        5,
        "no evidence writer calls write_text or open(..,'w')",
        "grep -rn \"write_text\\|open(.*'w')\" packages/pz_agent_cli/src/pz_agent_cli/livetest",
        "tests/contract/test_evidence_bytes_are_portable.py",
        "packages/pz_agent_cli/src/pz_agent_cli/livetest/evidence.py",
    ),
    (
        "Stop test fixtures writing hashed documents in text mode",
        5,
        "the release-gate and evidence fixtures write bytes and hash the same bytes",
        ".venv/bin/pytest tests/unit/test_check_release.py tests/unit/test_livetest_evidence.py -q",
        "tests/unit/test_check_release.py",
        "tests/unit/test_check_release.py",
    ),
    (
        "Read hashed documents back as bytes rather than through the locale encoding",
        4,
        "no round-trip assertion decodes with read_text()",
        "grep -rn 'read_text()' tests/unit/test_livetest_evidence.py",
        "tests/unit/test_livetest_evidence.py",
        "tests/unit/test_livetest_evidence.py",
    ),
    (
        "Set a UTF-8 codepage in the BAT before any non-ASCII byte",
        4,
        "chcp 65001 appears before the first non-ASCII character in the file",
        ".venv/bin/pytest tests/unit/test_installer_windows.py -q",
        "tests/unit/test_installer_windows.py::test_the_launcher_sets_a_utf8_codepage_before_any_non_ascii_path",
        "installer/pz_agent_installer.py",
    ),
    (
        "Build the MCP client entry as a dict and serialise with json.dumps",
        4,
        "an interpreter path containing a quote still yields parseable JSON",
        ".venv/bin/pytest tests/contract/test_mcp_snippet_is_json.py -q",
        "tests/contract/test_mcp_snippet_is_json.py",
        "packages/pz_agent_cli/src/pz_agent_cli/app.py",
    ),
    (
        "Cover Cyrillic, spaces and Program Files in the MCP snippet",
        4,
        "five interpreter shapes round-trip through json.loads",
        ".venv/bin/pytest tests/contract/test_mcp_snippet_is_json.py -q",
        "tests/contract/test_mcp_snippet_is_json.py",
        "tests/contract/test_mcp_snippet_is_json.py",
    ),
]

_E02_GREEN = [
    (
        "Add the RC branch pattern to both workflows so the branch has a route to evidence",
        3,
        "a push to fix/** produces a run in ci.yml and windows.yml",
        "read the runs list for the branch",
        "",
        ".github/workflows/windows.yml",
    ),
    (
        "Reproduce the full Windows suite through GitHub Actions",
        3,
        "a run exists at the branch-point SHA with its failure count recorded",
        "read the windows workflow run",
        "",
        "docs/control/evidence/step-30-40/windows-suite.txt",
    ),
    (
        "Drive the Windows failure count to zero",
        5,
        "a windows-latest run reports 0 failed",
        "read the windows workflow run",
        "",
        "docs/control/evidence/step-30-40/windows-suite.txt",
    ),
    (
        "Keep every assertion that was there at the branch point",
        5,
        "no test was deleted, skipped or weakened to reach zero; the test count rose",
        "git diff --stat <branch-point>..HEAD -- tests/",
        "",
        "docs/control/evidence/step-30-40/windows-suite.txt",
    ),
    (
        "Keep the Windows workflow running every check it ran at the branch point",
        4,
        "no step was removed from windows.yml",
        "git diff <branch-point>..HEAD -- .github/workflows/windows.yml",
        "",
        ".github/workflows/windows.yml",
    ),
    (
        "Make the POSIX-address test independent of where pytest puts its temp dir",
        3,
        "the test uses a fixed short root and cannot fail for a length reason",
        ".venv/bin/pytest tests/unit/test_rpc_transport.py -q",
        "tests/unit/test_rpc_transport.py::TestAddresses::test_a_posix_address_is_inside_the_runtime_directory",
        "tests/unit/test_rpc_transport.py",
    ),
    (
        "Confirm the RPC suite itself passes on windows-latest",
        5,
        "a run whose test stage includes tests/unit/test_rpc_*.py reports 0 failed",
        "read the windows workflow run",
        "",
        "docs/control/evidence/step-30-40/windows-suite.txt",
    ),
    (
        "Record the failure count per run so the trend is auditable, not asserted",
        3,
        "each run id is recorded with its counts",
        "cat docs/control/evidence/step-30-40/windows-suite.txt",
        "",
        "docs/control/evidence/step-30-40/windows-suite.txt",
    ),
]

E02 = Epic(
    id="E02",
    title="Windows portability",
    subsystem="platform",
    integration_scenario=(
        "The full test suite runs on windows-latest with zero failures, on a run "
        "that also builds both executables and assembles the archive — so the "
        "portability work is exercised by the packaging path and not only by "
        "unit tests."
    ),
    required_ci=RC,
    milestones=(
        Milestone(
            id="E02-M01",
            title="The path model",
            tasks=_tasks("E02", "M01", "platform", "portability", "remote", rows=_E02_PATHS),
            checks=(
                Check(
                    id="E02-M01-C01",
                    statement=(
                        "A manifest written on one platform and read on the other "
                        "compares equal, end to end through a real install."
                    ),
                    command=".venv/bin/pytest tests/unit/test_installer_windows.py -q",
                ),
            ),
        ),
        Milestone(
            id="E02-M02",
            title="Redaction on Windows shapes",
            tasks=_tasks("E02", "M02", "diagnostics", "portability", "remote", rows=_E02_REDACTION),
            checks=(
                Check(
                    id="E02-M02-C01",
                    statement=(
                        "A support bundle built from a Cyrillic Windows profile "
                        "contains no account name, no absolute path and no credential, "
                        "and verify_bundle agrees."
                    ),
                    command=".venv/bin/pytest tests/unit/test_diagnostics_bundle.py -q",
                ),
            ),
        ),
        Milestone(
            id="E02-M03",
            title="Process and permission primitives",
            tasks=_tasks("E02", "M03", "platform", "portability", "remote", rows=_E02_PROCESS),
            checks=(
                Check(
                    id="E02-M03-C01",
                    statement=(
                        "A child process is started, observed alive, and stopped, "
                        "using no POSIX-only call."
                    ),
                    command=".venv/bin/pytest tests/unit/test_cli_supervisor.py -q",
                ),
            ),
        ),
        Milestone(
            id="E02-M04",
            title="Text mode and newline translation",
            tasks=_tasks("E02", "M04", "platform", "portability", "remote", rows=_E02_TEXTMODE),
            checks=(
                Check(
                    id="E02-M04-C01",
                    statement=(
                        "A digest taken before a write equals the digest of the bytes "
                        "on disk, on a platform that translates newlines."
                    ),
                    command="pytest tests/contract/test_evidence_bytes_are_portable.py -q",
                ),
            ),
        ),
        Milestone(
            id="E02-M05",
            title="The Windows suite green",
            tasks=_tasks("E02", "M05", "ci", "portability", "remote", rows=_E02_GREEN),
            checks=(
                Check(
                    id="E02-M05-C01",
                    statement=(
                        "Zero Windows failures was reached by fixing causes, not by "
                        "removing assertions: the test count at HEAD exceeds the count "
                        "at the branch point."
                    ),
                    command="compare collected test counts between the two runs",
                ),
            ),
        ),
    ),
)
