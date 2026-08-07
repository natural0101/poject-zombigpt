#!/usr/bin/env python3
"""Epics 11-15: PyInstaller and RC, security, docs, live validation, release.

E14 and E15 are ``local``: nothing in this environment can produce their
evidence, and no substitute counts. They carry the highest weights precisely
because they cannot be done from here — a model that weighted them low would let
the total climb to nearly full while the only question that matters, whether the
thing works in the game, stayed unanswered.
"""

from __future__ import annotations

from scripts.plan_epics_a import CI, RC, _tasks
from scripts.plan_model import Check, Epic, Milestone

T = "tests/unit"
C = "tests/contract"
PKG = "packaging/windows"
LIVE = "docs/LIVE_TEST_PLAYBOOK.md"

# ---------------------------------------------------------------------------
# E11 — PyInstaller and the Windows RC (30, band "packaging"/"integration")
# ---------------------------------------------------------------------------

_E11_SPEC = [
    (
        "Discover a package's submodules by reading the directory, not by importing",
        5,
        "collection executes nothing; a module that exits on import is still packaged",
        f".venv/bin/pytest {T}/test_packaging_specutil.py -q",
        f"{T}/test_packaging_specutil.py",
        f"{PKG}/specutil.py",
    ),
    (
        "Exclude mcp.cli by dotted prefix, taking its children with it",
        5,
        "mcp.cli and mcp.cli.cli are dropped; a sibling mcp.client is not",
        f".venv/bin/pytest {T}/test_packaging_specutil.py -q",
        f"{T}/test_packaging_specutil.py",
        f"{PKG}/specutil.py",
    ),
    (
        "Refuse rather than return an empty hidden-import list",
        5,
        "an absent package raises; an empty list would build an exe that fails on first use",
        f".venv/bin/pytest {T}/test_packaging_specutil.py -q",
        f"{T}/test_packaging_specutil.py",
        f"{PKG}/specutil.py",
    ),
    (
        "Skip __pycache__ and data directories when walking",
        4,
        "neither becomes a hidden import",
        f".venv/bin/pytest {T}/test_packaging_specutil.py -q",
        f"{T}/test_packaging_specutil.py",
        f"{PKG}/specutil.py",
    ),
    (
        "Generate the pz-agent entry script from the spec",
        4,
        "the entry imports the package properly",
        "read the spec",
        "",
        f"{PKG}/pz-agent.spec",
    ),
    (
        "Generate the pz-agent-mcp entry script from the spec",
        4,
        "the entry imports the package properly",
        "read the spec",
        "",
        f"{PKG}/pz-agent-mcp.spec",
    ),
]

_E11_BUILD = [
    (
        "Build pz-agent.exe on windows-latest",
        7,
        "the build step succeeds and the file exists",
        "read the windows workflow run",
        "",
        "docs/control/evidence/step-30-40/windows-suite.txt",
    ),
    (
        "Build pz-agent-mcp.exe on windows-latest",
        7,
        "the build step succeeds and the file exists",
        "read the windows workflow run",
        "",
        "docs/control/evidence/step-30-40/windows-suite.txt",
    ),
    (
        "Run pz-agent.exe --version on Windows",
        7,
        "the executable answers, not merely exists",
        "read the windows workflow run",
        "",
        "docs/control/evidence/step-30-40/windows-suite.txt",
    ),
    (
        "Run pz-agent-mcp.exe --describe on Windows",
        7,
        "the executable writes the catalogue",
        "read the windows workflow run",
        "",
        "docs/control/evidence/step-30-40/windows-suite.txt",
    ),
    (
        "Run pz-agent-mcp.exe against a running sidecar on Windows",
        8,
        "the packaged executable completes an MCP initialize over the RPC link",
        "read the windows workflow run",
        f"{C}/test_mcp_subprocess_e2e.py",
        "docs/control/evidence/step-30-40/windows-suite.txt",
    ),
    (
        "Prove neither executable needs a Python installation",
        7,
        "both run on a runner with no project venv on PATH",
        "read the windows workflow run",
        "",
        "docs/control/evidence/step-30-40/windows-suite.txt",
    ),
    (
        "Keep the console attached so a client can read stdio",
        7,
        "console=True in both specs",
        "read the specs",
        "",
        f"{PKG}/pz-agent-mcp.spec",
    ),
    (
        "Leave both executables unpacked and unsigned rather than UPX-compressed",
        7,
        "upx=False and strip=False, because a packed unsigned binary reads as malware",
        "read the specs",
        "",
        f"{PKG}/pz-agent.spec",
    ),
]

_E11_ARCHIVE = [
    (
        "Assemble the release archive on Windows",
        7,
        "the zip is produced by the workflow",
        "read the windows workflow run",
        "",
        "docs/control/evidence/step-30-40/windows-suite.txt",
    ),
    (
        "List every archive member in the manifest",
        7,
        "the index and the archive agree exactly",
        ".venv/bin/python scripts/check_release.py",
        f"{T}/test_check_release.py",
        "scripts/check_release.py",
    ),
    (
        "Verify each member's digest independently of the index",
        8,
        "the gate hashes every member rather than trusting the recorded list",
        ".venv/bin/python scripts/check_release.py",
        f"{T}/test_check_release.py",
        "scripts/check_release.py",
    ),
    (
        "Include both executables in the archive",
        7,
        "both are present and executable",
        ".venv/bin/python scripts/check_release.py",
        f"{T}/test_check_release.py",
        f"{PKG}/build_rc.py",
    ),
    (
        "Include the mod payload",
        7,
        "the mod directory is present with its mod.info",
        ".venv/bin/python scripts/check_release.py",
        f"{T}/test_check_release.py",
        f"{PKG}/build_rc.py",
    ),
    (
        "Include the installer and the launcher",
        7,
        "both are present",
        ".venv/bin/python scripts/check_release.py",
        f"{T}/test_check_release.py",
        f"{PKG}/build_rc.py",
    ),
    (
        "Include every document an operator needs, and no dangling link",
        7,
        "every relative link in a shipped document resolves inside the archive",
        f".venv/bin/pytest {C}/test_archive_documents_resolve.py -q",
        f"{C}/test_archive_documents_resolve.py",
        f"{PKG}/build_rc.py",
    ),
    (
        "Refuse to publish an archive whose gate did not pass",
        8,
        "a red gate stops the upload step",
        "read the windows workflow",
        "",
        ".github/workflows/windows.yml",
    ),
    (
        "Record the archive digest in the plan when it is built",
        7,
        "the recorded sha256 matches the uploaded artefact",
        "read the windows workflow run",
        "",
        "docs/control/MASTER_PLAN.yaml",
    ),
]

_E11_GATE = [
    (
        "Keep the release gate red until every scenario has actually run",
        8,
        "all-NOT_RUN fails the gate",
        ".venv/bin/python scripts/check_release.py",
        f"{T}/test_check_release.py",
        "scripts/check_release.py",
    ),
    (
        "Refuse a v1.0.0 tag before live validation passes",
        8,
        "the gate refuses a release whose live scenarios are not PASS",
        ".venv/bin/python scripts/check_release.py",
        f"{T}/test_check_release.py",
        "scripts/check_release.py",
    ),
    (
        "Keep the Windows workflow required for the RC",
        7,
        "the RC cannot be produced from a red Windows run",
        "read the windows workflow",
        "",
        ".github/workflows/windows.yml",
    ),
    (
        "Publish the RC as a workflow artefact with its digest",
        7,
        "the artefact and its sha256 are both recorded",
        "read the windows workflow run",
        "",
        "docs/control/MASTER_PLAN.yaml",
    ),
    (
        "Record the RC digest in the evidence index",
        7,
        "the index carries the artefact digest",
        "cat docs/control/EVIDENCE_INDEX.md",
        "",
        "docs/control/EVIDENCE_INDEX.md",
    ),
    (
        "Prove a PyInstaller spec with no built executable is not counted",
        8,
        "the plan marks the build tasks BLOCKED until a run produces the files",
        ".venv/bin/python scripts/check_master_plan.py",
        f"{T}/test_master_plan.py",
        "scripts/check_master_plan.py",
    ),
    (
        "Prove a ZIP whose contents were never checked is not counted",
        8,
        "the archive tasks require the per-member verification to have run",
        ".venv/bin/python scripts/check_master_plan.py",
        f"{T}/test_master_plan.py",
        "scripts/check_master_plan.py",
    ),
]

E11 = Epic(
    id="E11",
    title="PyInstaller and the Windows RC",
    subsystem="packaging",
    integration_scenario=(
        "A windows-latest run builds both executables, runs each of them, "
        "completes an MCP initialize through the packaged pz-agent-mcp.exe "
        "against a packaged sidecar, assembles the archive and passes the "
        "release gate over every member independently."
    ),
    required_ci=RC,
    milestones=(
        Milestone(
            id="E11-M01",
            title="The specs",
            tasks=_tasks("E11", "M01", "packaging", "packaging", "remote", rows=_E11_SPEC),
            checks=(
                Check(
                    id="E11-M01-C01",
                    statement=(
                        "Submodule collection imports nothing, proven against a package that "
                        "exits on import."
                    ),
                    command=f".venv/bin/pytest {T}/test_packaging_specutil.py -q",
                ),
            ),
        ),
        Milestone(
            id="E11-M02",
            title="Building and running the executables",
            tasks=_tasks("E11", "M02", "packaging", "integration", "remote", rows=_E11_BUILD),
            checks=(
                Check(
                    id="E11-M02-C01",
                    statement="Both executables run on a machine with no project Python.",
                    command="read the windows workflow run",
                ),
            ),
        ),
        Milestone(
            id="E11-M03",
            title="The archive",
            tasks=_tasks("E11", "M03", "packaging", "integration", "remote", rows=_E11_ARCHIVE),
            checks=(
                Check(
                    id="E11-M03-C01",
                    statement="Every archive member is verified independently of the index.",
                    command=".venv/bin/python scripts/check_release.py",
                ),
            ),
        ),
        Milestone(
            id="E11-M04",
            title="The release gate",
            tasks=_tasks("E11", "M04", "release", "integration", "remote", rows=_E11_GATE),
            checks=(
                Check(
                    id="E11-M04-C01",
                    statement="No artefact can be published from a red gate or a red Windows run.",
                    command="read .github/workflows/windows.yml",
                ),
            ),
        ),
    ),
)

# ---------------------------------------------------------------------------
# E12 — security and failure recovery (30, band "security", weights 7-9)
# ---------------------------------------------------------------------------

_E12_SECRETS = [
    (
        "Keep every secret out of the repository",
        9,
        "the secret scanner finds nothing tracked",
        ".venv/bin/python scripts/check_forbidden.py",
        f"{T}/test_check_forbidden.py",
        "scripts/check_forbidden.py",
    ),
    (
        "Keep the RPC token out of every log line",
        9,
        "no log record contains the token",
        f".venv/bin/pytest {T}/test_rpc_token_and_descriptor.py -q",
        f"{T}/test_rpc_token_and_descriptor.py",
        "packages/pz_agent_core/src/pz_agent_core/rpc/token.py",
    ),
    (
        "Keep the RPC token out of every support bundle",
        9,
        "no bundle member contains it",
        f".venv/bin/pytest {T}/test_diagnostics_bundle.py -q",
        f"{T}/test_diagnostics_bundle.py",
        "packages/pz_agent_core/src/pz_agent_core/diagnostics/bundle.py",
    ),
    (
        "Keep the RPC token out of every exception message",
        9,
        "no exception text carries it",
        f".venv/bin/pytest {T}/test_rpc_token_and_descriptor.py -q",
        f"{T}/test_rpc_token_and_descriptor.py",
        "packages/pz_agent_core/src/pz_agent_core/rpc/token.py",
    ),
    (
        "Strike a credential out of a log line entirely, not merely mark it",
        9,
        "the secret is absent from the redacted output, for every shape",
        f".venv/bin/pytest {T}/test_diagnostics_redaction.py -q",
        f"{T}/test_diagnostics_redaction.py",
        "packages/pz_agent_core/src/pz_agent_core/diagnostics/redaction.py",
    ),
    (
        "Report a leak in verify_bundle when one is present",
        9,
        "findings() is non-empty for a bundle containing a credential",
        f".venv/bin/pytest {T}/test_diagnostics_bundle.py -q",
        f"{T}/test_diagnostics_bundle.py",
        "packages/pz_agent_core/src/pz_agent_core/diagnostics/bundle.py",
    ),
    (
        "Never flag a clean bundle as leaking",
        8,
        "the verifier does not flag its own placeholders",
        f".venv/bin/pytest {T}/test_diagnostics_bundle.py -q",
        f"{T}/test_diagnostics_bundle.py",
        "packages/pz_agent_core/src/pz_agent_core/diagnostics/bundle.py",
    ),
    (
        "Keep the account name out of every recorded path",
        8,
        "no manifest or evidence path carries it",
        f".venv/bin/pytest {C}/test_portable_paths.py -q",
        f"{C}/test_portable_paths.py",
        "packages/pz_agent_core/src/pz_agent_core/platform/paths.py",
    ),
]

_E12_BOUNDARY = [
    (
        "Refuse any transport that is not local",
        9,
        "AF_INET is refused in the descriptor and the client",
        f".venv/bin/pytest {T}/test_rpc_token_and_descriptor.py -q",
        f"{T}/test_rpc_token_and_descriptor.py",
        "packages/pz_agent_core/src/pz_agent_core/rpc/descriptor.py",
    ),
    (
        "Never deserialise executable content",
        9,
        "no pickle, no eval, no dynamic import from a payload",
        "grep -rn 'pickle\\|eval(\\|exec(' packages/*/src",
        f"{T}/test_rpc_wire.py",
        "packages/pz_agent_core/src/pz_agent_core/rpc/wire.py",
    ),
    (
        "Bound every read from another process",
        8,
        "every recv has a maximum length",
        f".venv/bin/pytest {T}/test_rpc_wire.py -q",
        f"{T}/test_rpc_wire.py",
        "packages/pz_agent_core/src/pz_agent_core/rpc/wire.py",
    ),
    (
        "Bound every wait on another process",
        8,
        "every poll and read has a deadline",
        f".venv/bin/pytest {T}/test_rpc_transport.py -q",
        f"{T}/test_rpc_transport.py",
        "packages/pz_agent_core/src/pz_agent_core/rpc/transport.py",
    ),
    (
        "Never modify game memory",
        9,
        "no code reads or writes another process's memory",
        "grep -rn 'ReadProcessMemory\\|WriteProcessMemory\\|ptrace' packages installer",
        "",
        "docs/SAFETY.md",
    ),
    (
        "Never inject a library into the game process",
        9,
        "no DLL injection anywhere",
        "grep -rn 'CreateRemoteThread\\|LoadLibrary' packages installer",
        "",
        "docs/SAFETY.md",
    ),
    (
        "Never attempt to bypass anti-cheat",
        9,
        "no such code path exists",
        "read docs/SAFETY.md",
        "",
        "docs/SAFETY.md",
    ),
    (
        "Keep synthetic input disabled in multiplayer",
        9,
        "the input fallback refuses when the session is multiplayer",
        f".venv/bin/pytest {T}/test_safety_input.py -q",
        f"{T}/test_safety_input.py",
        "packages/pz_agent_core/src/pz_agent_core/safety/input.py",
    ),
]

_E12_RECOVERY = [
    (
        "Recover when the sidecar dies mid-session",
        8,
        "the mod detects the dead heartbeat and stops acting",
        f".venv/bin/pytest {T}/test_session_recovery.py -q",
        f"{T}/test_session_recovery.py",
        "packages/pz_agent_core/src/pz_agent_core/session/holder.py",
    ),
    (
        "Recover when the game dies mid-action",
        8,
        "the action ends as GAME_DISCONNECTED, not as succeeded",
        f".venv/bin/pytest {T}/test_actions_engine.py -q",
        f"{T}/test_actions_engine.py",
        "packages/pz_agent_core/src/pz_agent_core/actions/engine.py",
    ),
    (
        "Recover when the RPC link drops mid-call",
        8,
        "the client reports it; the sidecar keeps serving",
        f".venv/bin/pytest {T}/test_rpc_transport.py -q",
        f"{T}/test_rpc_transport.py",
        "packages/pz_agent_core/src/pz_agent_core/rpc/transport.py",
    ),
    (
        "Recover when the state directory becomes unwritable",
        8,
        "the sidecar reports it rather than losing the session silently",
        f".venv/bin/pytest {T}/test_cli_supervisor.py -q",
        f"{T}/test_cli_supervisor.py",
        "packages/pz_agent_cli/src/pz_agent_cli/supervisor.py",
    ),
    (
        "Recover from a corrupt journal",
        8,
        "a truncated journal is detected and the session refuses to arm",
        f".venv/bin/pytest {T}/test_ipc_journal.py -q",
        f"{T}/test_ipc_journal.py",
        "packages/pz_agent_core/src/pz_agent_core/ipc/journal.py",
    ),
    (
        "Recover from a corrupt snapshot",
        8,
        "the alternate slot is used and the corruption reported",
        f".venv/bin/pytest {T}/test_ipc_snapshot.py -q",
        f"{T}/test_ipc_snapshot.py",
        "packages/pz_agent_core/src/pz_agent_core/ipc/snapshot.py",
    ),
    (
        "Keep a panic stop working when every other subsystem is down",
        9,
        "a stop is honoured with no core, no game, no queue and no link",
        f".venv/bin/pytest {T}/test_safety_stop.py -q",
        f"{T}/test_safety_stop.py",
        "packages/pz_agent_core/src/pz_agent_core/safety/stop.py",
    ),
    (
        "Never clear a queue entry the player owns",
        9,
        "a stop clears only mod-owned entries",
        f".venv/bin/pytest {T}/test_safety_stop.py -q",
        f"{T}/test_safety_stop.py",
        "packages/pz_agent_core/src/pz_agent_core/safety/stop.py",
    ),
    (
        "Refuse to act after a manual takeover, before dispatch",
        9,
        "no command is sent once the player has taken control",
        f".venv/bin/pytest {T}/test_actions_engine.py -q",
        f"{T}/test_actions_engine.py::test_manual_takeover_before_the_send_refuses_rather_than_cancels",
        "packages/pz_agent_core/src/pz_agent_core/actions/engine.py",
    ),
    (
        "Cancel in flight when a takeover arrives mid-action",
        9,
        "the command is cancelled with USER_TAKEOVER",
        f".venv/bin/pytest {T}/test_actions_engine.py -q",
        f"{T}/test_actions_engine.py",
        "packages/pz_agent_core/src/pz_agent_core/actions/engine.py",
    ),
    (
        "Require a confirmed backup before arming",
        9,
        "arming without a backup is refused",
        f".venv/bin/pytest {T}/test_session_arming.py -q",
        f"{T}/test_session_arming.py",
        "packages/pz_agent_core/src/pz_agent_core/session/holder.py",
    ),
    (
        "Bind the backup to the save the mod reports",
        9,
        "a backup for another save does not satisfy the arming gate",
        f".venv/bin/pytest {T}/test_session_arming.py -q",
        f"{T}/test_session_arming.py",
        "packages/pz_agent_core/src/pz_agent_core/platform/backup.py",
    ),
    (
        "Refuse to act on a save that changed under us",
        9,
        "a different save id ends the session rather than continuing",
        f".venv/bin/pytest {T}/test_actions_engine.py -q",
        f"{T}/test_actions_engine.py",
        "packages/pz_agent_core/src/pz_agent_core/actions/engine.py",
    ),
    (
        "Document every refusal an operator can hit",
        7,
        "each has a code, a cause and a remedy",
        f".venv/bin/pytest {C}/test_documented_commands_parse.py -q",
        f"{C}/test_documented_commands_parse.py",
        "docs/TROUBLESHOOTING.md",
    ),
]

E12 = Epic(
    id="E12",
    title="Security and failure recovery",
    subsystem="safety",
    integration_scenario=(
        "With the sidecar killed, the link dropped, the journal truncated and the "
        "state directory read-only, a panic stop is still honoured, no secret has "
        "reached a log or a bundle, and nothing reports a success it cannot prove."
    ),
    required_ci=RC,
    milestones=(
        Milestone(
            id="E12-M01",
            title="Secrets",
            tasks=_tasks("E12", "M01", "safety", "security", "remote", rows=_E12_SECRETS),
            checks=(
                Check(
                    id="E12-M01-C01",
                    statement=(
                        "A bundle built from a real session contains no credential, path or "
                        "account name."
                    ),
                    command=f".venv/bin/pytest {T}/test_diagnostics_bundle.py -q",
                ),
            ),
        ),
        Milestone(
            id="E12-M02",
            title="The trust boundary",
            tasks=_tasks("E12", "M02", "safety", "security", "remote", rows=_E12_BOUNDARY),
            checks=(
                Check(
                    id="E12-M02-C01",
                    statement=(
                        "Nothing in the shipped tree reads memory, injects code or deserialises "
                        "executables."
                    ),
                    command=(
                        "grep -rn 'pickle\\|CreateRemoteThread\\|ReadProcessMemory' packages "
                        "installer"
                    ),
                ),
            ),
        ),
        Milestone(
            id="E12-M03",
            title="Failure recovery",
            tasks=_tasks("E12", "M03", "safety", "security", "remote", rows=_E12_RECOVERY),
            checks=(
                Check(
                    id="E12-M03-C01",
                    statement="A panic stop is honoured under every failure here.",
                    command=f".venv/bin/pytest {T}/test_safety_stop.py -q",
                ),
            ),
        ),
    ),
)

# ---------------------------------------------------------------------------
# E13 — documentation and handoff (20, band "doc", weights 1-2)
# ---------------------------------------------------------------------------

_E13 = [
    (
        "Describe the Core RPC link",
        2,
        "docs/CORE_RPC.md matches the implementation",
        "read docs/CORE_RPC.md",
        f"{C}/test_archive_documents_resolve.py",
        "docs/CORE_RPC.md",
    ),
    (
        "Describe the MCP surface",
        2,
        "docs/MCP_TOOLS.md matches --describe",
        f".venv/bin/pytest {C}/test_mcp_docs_match_catalogue.py -q",
        f"{C}/test_mcp_docs_match_catalogue.py",
        "docs/MCP_TOOLS.md",
    ),
    (
        "Describe the voice surface and its limits",
        2,
        "docs/VOICE.md matches the runtime",
        f".venv/bin/pytest {C}/test_documented_commands_parse.py -q",
        f"{C}/test_documented_commands_parse.py",
        "docs/VOICE.md",
    ),
    (
        "Keep QUICKSTART honest about what works",
        2,
        "no documented command is refused by the build",
        f".venv/bin/pytest {C}/test_documented_commands_parse.py -q",
        f"{C}/test_documented_commands_parse.py",
        "docs/QUICKSTART.md",
    ),
    (
        "Keep every relative link in a shipped document resolvable",
        2,
        "the archive link check passes",
        f".venv/bin/pytest {C}/test_archive_documents_resolve.py -q",
        f"{C}/test_archive_documents_resolve.py",
        "docs/README.md",
    ),
    (
        "Record every limitation the build actually has",
        2,
        "docs/LIMITATIONS.md matches the code",
        "read docs/LIMITATIONS.md",
        "",
        "docs/LIMITATIONS.md",
    ),
    (
        "Record the safety rules and their enforcement points",
        2,
        "each rule names where it is enforced",
        "read docs/SAFETY.md",
        "",
        "docs/SAFETY.md",
    ),
    (
        "Record the troubleshooting codes",
        2,
        "each code has a cause and a remedy",
        "read docs/TROUBLESHOOTING.md",
        "",
        "docs/TROUBLESHOOTING.md",
    ),
    (
        "Write the live-test playbook for the local agent",
        2,
        "S01-S20 are each described with steps",
        ".venv/bin/python scripts/generate_playbook.py --check",
        "",
        LIVE,
    ),
    (
        "Write the handoff naming what needs the game",
        2,
        "every task this environment cannot do is listed",
        "read docs/LOCAL_GAME_HANDOFF.md",
        "",
        "docs/LOCAL_GAME_HANDOFF.md",
    ),
    (
        "Write the local agent prompt",
        1,
        "the prompt is self-contained",
        "read docs/LOCAL_AGENT_PROMPT.md",
        "",
        "docs/LOCAL_AGENT_PROMPT.md",
    ),
    (
        "Write the debug map for live symptoms",
        1,
        "each symptom names the file to read",
        "read docs/LOCAL_DEBUG_MAP.md",
        "",
        "docs/LOCAL_DEBUG_MAP.md",
    ),
    (
        "Keep the protocol document current",
        2,
        "docs/PROTOCOL.md matches the schemas",
        "read docs/PROTOCOL.md",
        "",
        "docs/PROTOCOL.md",
    ),
    (
        "Keep the architecture document current",
        2,
        "docs/ARCHITECTURE.md matches the packages",
        "read docs/ARCHITECTURE.md",
        "",
        "docs/ARCHITECTURE.md",
    ),
    (
        "Keep the compatibility document current",
        2,
        "docs/COMPATIBILITY.md matches the capability model",
        "read docs/COMPATIBILITY.md",
        "",
        "docs/COMPATIBILITY.md",
    ),
    (
        "Point PROGRESS.md at the plan of record",
        1,
        "the handover doc names docs/control/",
        "read docs/PROGRESS.md",
        "",
        "docs/PROGRESS.md",
    ),
    (
        "Keep CHANGELOG entries matched to real changes",
        1,
        "each entry names a change that happened",
        ".venv/bin/python scripts/check_versions.py",
        "",
        "CHANGELOG.md",
    ),
    (
        "Record every decision that shaped the RC",
        2,
        "docs/control/DECISIONS.md is current",
        "read docs/control/DECISIONS.md",
        "",
        "docs/control/DECISIONS.md",
    ),
    (
        "Keep the evidence index complete",
        2,
        "every evidence path in the plan appears in the index",
        ".venv/bin/python scripts/check_master_plan.py",
        f"{T}/test_master_plan.py",
        "docs/control/EVIDENCE_INDEX.md",
    ),
    (
        "Write the final implementation report",
        2,
        "the report meets docs/RELEASE.md and lists what physically needs the game",
        "read FINAL_IMPLEMENTATION_REPORT.md",
        "",
        "FINAL_IMPLEMENTATION_REPORT.md",
    ),
]

E13 = Epic(
    id="E13",
    title="Documentation and handoff",
    subsystem="docs",
    integration_scenario=(
        "Every command a shipped document tells a user to run is accepted by the "
        "build, and every relative link in the archive resolves inside it."
    ),
    required_ci=CI,
    milestones=(
        Milestone(
            id="E13-M01",
            title="Documents that match the runtime",
            tasks=_tasks("E13", "M01", "docs", "doc", "remote", rows=_E13),
            checks=(
                Check(
                    id="E13-M01-C01",
                    statement="No document asserts something the code does not do.",
                    command=f".venv/bin/pytest {C}/test_documented_commands_parse.py -q",
                ),
            ),
        ),
    ),
)

# ---------------------------------------------------------------------------
# E14 — Project Zomboid live validation (60, band "live", owner local)
# ---------------------------------------------------------------------------

_SCENARIOS = [
    ("S01", "handshake and heartbeat"),
    ("S02", "arm refuses without a backup"),
    ("S03", "arm succeeds with a confirmed backup"),
    ("S04", "walk to a nearby square"),
    ("S05", "open a container"),
    ("S06", "transfer an item"),
    ("S07", "nested inventory observation"),
    ("S08", "eat a known-safe food"),
    ("S09", "drink from a carried container"),
    ("S10", "equip a weapon"),
    ("S11", "read a skill book"),
    ("S12", "apply a bandage"),
    ("S13", "sleep in a bed"),
    ("S14", "refuse an unverified capability"),
    ("S15", "panic stop mid-action"),
    ("S16", "manual takeover mid-action"),
    ("S17", "danger interrupts an interruptible action"),
    ("S18", "idempotent replay"),
    ("S19", "session survives a game save"),
    ("S20", "session ends on character death"),
]

_E14_SETUP = [
    (
        "Create a dedicated test save, never the user's own",
        10,
        "a new save exists and is named as the test save",
        "follow docs/LIVE_TEST_PLAYBOOK.md",
        "",
        "evidence/live/setup.json",
    ),
    (
        "Take a backup of that save before any run",
        10,
        "the backup exists and its digest is recorded",
        ".venv/bin/pz-agent backup",
        "",
        "evidence/live/setup.json",
    ),
    (
        "Confirm the game build is 42.20",
        10,
        "the reported build matches",
        ".venv/bin/pz-agent doctor",
        "",
        "evidence/live/doctor.json",
    ),
    (
        "Install the mod through the shipped path",
        10,
        "the mod loads and the handshake completes",
        "install.bat",
        "",
        "evidence/live/setup.json",
    ),
    (
        "Confirm the sidecar and the game agree on the protocol version",
        10,
        "the handshake reports a matching major",
        ".venv/bin/pz-agent status",
        "",
        "evidence/live/setup.json",
    ),
    (
        "Run the capability scan against the real build",
        10,
        "the report lists each capability with its observed state",
        ".venv/bin/pz-agent capabilities",
        "",
        "evidence/live/capabilities.json",
    ),
    (
        "Confirm no capability is verified without a live acknowledgement",
        10,
        "every verified capability has a confirm() record",
        ".venv/bin/pz-agent capabilities",
        "",
        "evidence/live/capabilities.json",
    ),
    (
        "Record the machine and its Windows version",
        9,
        "the environment is recorded with the evidence",
        "follow docs/LIVE_TEST_PLAYBOOK.md",
        "",
        "evidence/live/setup.json",
    ),
]

_E14_API = [
    (
        f"Verify the real Build 42.20 API for {name}",
        10,
        "the symbol exists on the real build and its signature matches the probe",
        ".venv/bin/pz-agent capabilities --verify",
        "",
        f"evidence/live/api/{code}.json",
    )
    for code, name in _SCENARIOS
]

_E14_RUN = [
    (
        f"Run live scenario {code} — {name}",
        10,
        "the scenario reaches POSTCONDITION_MET with observed evidence, or records why not",
        f".venv/bin/pz-agent livetest run {code}",
        "",
        f"evidence/live/{code}/result.json",
    )
    for code, name in _SCENARIOS
]

_E14_EVIDENCE = [
    (
        "Collect the evidence tree for every scenario",
        10,
        "each scenario has its artefacts and digests",
        ".venv/bin/pz-agent livetest collect",
        "",
        "evidence/live/manifest.json",
    ),
    (
        "Verify every evidence digest after collection",
        10,
        "the release gate passes over the tree",
        ".venv/bin/python scripts/check_release.py",
        "",
        "evidence/live/manifest.json",
    ),
    (
        "Record every scenario that did not run, and why",
        10,
        "NOT_RUN and BLOCKED are distinguished from FAIL",
        ".venv/bin/pz-agent livetest report",
        "",
        "evidence/live/manifest.json",
    ),
    (
        "Record the game incompatibilities the run found",
        10,
        "each is named with its symbol and build",
        ".venv/bin/pz-agent livetest report",
        "",
        "evidence/live/incompatibilities.json",
    ),
    (
        "Fix each confirmed game incompatibility",
        10,
        "each fix has a test and a re-run scenario",
        ".venv/bin/pz-agent livetest run",
        "",
        "evidence/live/manifest.json",
    ),
    (
        "Re-run every scenario a fix touched",
        10,
        "the affected scenarios pass after the fix",
        ".venv/bin/pz-agent livetest run",
        "",
        "evidence/live/manifest.json",
    ),
    (
        "Confirm the panic stop works from the keyboard in game",
        10,
        "the hotkey stops the agent within one tick",
        "follow docs/LIVE_TEST_PLAYBOOK.md",
        "",
        "evidence/live/S15/result.json",
    ),
    (
        "Confirm no save file was corrupted by any run",
        10,
        "the save loads and the backup still matches",
        "follow docs/LIVE_TEST_PLAYBOOK.md",
        "",
        "evidence/live/manifest.json",
    ),
    (
        "Confirm the MCP server works against the live sidecar",
        10,
        "a real MCP client completes a tool call against the running game",
        "follow docs/LIVE_TEST_PLAYBOOK.md",
        "",
        "evidence/live/mcp.json",
    ),
    (
        "Confirm voice control works against the live sidecar",
        10,
        "a spoken goal reaches the core and the character acts",
        "follow docs/LIVE_TEST_PLAYBOOK.md",
        "",
        "evidence/live/voice.json",
    ),
    (
        "Confirm a spoken stop halts the character",
        10,
        "the character stops within one tick",
        "follow docs/LIVE_TEST_PLAYBOOK.md",
        "",
        "evidence/live/voice.json",
    ),
    (
        "Record the whole run as a support bundle and verify it is clean",
        10,
        "verify_bundle reports no findings on the live bundle",
        ".venv/bin/pz-agent logs --bundle --verify",
        "",
        "evidence/live/bundle.json",
    ),
]

E14 = Epic(
    id="E14",
    title="Project Zomboid live validation",
    subsystem="live",
    integration_scenario=(
        "On a Windows machine with Steam and Project Zomboid Build 42.20, against "
        "a dedicated test save with a verified backup, all twenty scenarios run to "
        "a terminal state, every evidence digest verifies, the panic stop works "
        "from the keyboard, and no save is corrupted."
    ),
    required_ci=None,
    milestones=(
        Milestone(
            id="E14-M01",
            title="Environment and setup",
            tasks=_tasks("E14", "M01", "live", "live", "local", rows=_E14_SETUP),
            checks=(
                Check(
                    id="E14-M01-C01",
                    statement=(
                        "The run used a dedicated test save with a verified backup, never the "
                        "user's own."
                    ),
                    command="read evidence/live/setup.json",
                ),
            ),
        ),
        Milestone(
            id="E14-M02",
            title="Real API verification",
            tasks=_tasks("E14", "M02", "live", "live", "local", rows=_E14_API),
            checks=(
                Check(
                    id="E14-M02-C01",
                    statement=(
                        "No capability is verified without a live acknowledgement from the "
                        "running game."
                    ),
                    command="read evidence/live/capabilities.json",
                ),
            ),
        ),
        Milestone(
            id="E14-M03",
            title="The twenty scenarios",
            tasks=_tasks("E14", "M03", "live", "live", "local", rows=_E14_RUN),
            checks=(
                Check(
                    id="E14-M03-C01",
                    statement="Every scenario reached a terminal state; none is NOT_RUN.",
                    command="read evidence/live/manifest.json",
                ),
            ),
        ),
        Milestone(
            id="E14-M04",
            title="Evidence and incompatibilities",
            tasks=_tasks("E14", "M04", "live", "live", "local", rows=_E14_EVIDENCE),
            checks=(
                Check(
                    id="E14-M04-C01",
                    statement="The live evidence passes the gate from a clean checkout.",
                    command=".venv/bin/python scripts/check_release.py",
                ),
            ),
        ),
    ),
)

# ---------------------------------------------------------------------------
# E15 — endurance and final release (20, band "live", owner local)
# ---------------------------------------------------------------------------

_E15_ENDURANCE = [
    (
        "Run autonomously for thirty minutes",
        10,
        "the run completes with no unsafe action and no crash",
        "follow docs/LIVE_TEST_PLAYBOOK.md",
        "",
        "evidence/live/endurance-30m.json",
    ),
    (
        "Record every action taken during the thirty-minute run",
        10,
        "the trace is complete and replayable",
        ".venv/bin/pz-agent logs --trace",
        "",
        "evidence/live/endurance-30m.json",
    ),
    (
        "Confirm memory and handle counts are stable over thirty minutes",
        10,
        "no unbounded growth",
        "follow docs/LIVE_TEST_PLAYBOOK.md",
        "",
        "evidence/live/endurance-30m.json",
    ),
    (
        "Run autonomously for two hours",
        10,
        "the run completes with no unsafe action and no crash",
        "follow docs/LIVE_TEST_PLAYBOOK.md",
        "",
        "evidence/live/endurance-2h.json",
    ),
    (
        "Record every action taken during the two-hour run",
        10,
        "the trace is complete and replayable",
        ".venv/bin/pz-agent logs --trace",
        "",
        "evidence/live/endurance-2h.json",
    ),
    (
        "Confirm the journals rotated without losing an observation",
        10,
        "rotation is observed and no diff is orphaned",
        ".venv/bin/pz-agent logs --trace",
        "",
        "evidence/live/endurance-2h.json",
    ),
    (
        "Confirm the character survived or died for an explainable reason",
        10,
        "the outcome is accounted for in the trace",
        ".venv/bin/pz-agent logs --trace",
        "",
        "evidence/live/endurance-2h.json",
    ),
    (
        "Confirm no save corruption after the endurance runs",
        10,
        "the save loads and verifies",
        "follow docs/LIVE_TEST_PLAYBOOK.md",
        "",
        "evidence/live/endurance-2h.json",
    ),
    (
        "Fix everything the endurance runs found",
        10,
        "each fix has a test and a re-run",
        "bash scripts/check.sh",
        "",
        "evidence/live/endurance-2h.json",
    ),
    (
        "Re-run the affected scenarios after those fixes",
        10,
        "the affected scenarios pass",
        ".venv/bin/pz-agent livetest run",
        "",
        "evidence/live/manifest.json",
    ),
]

_E15_RELEASE = [
    (
        "Merge the RC branch into dev with every check green",
        10,
        "the merge lands with CI and the Windows workflow green",
        "read the workflow runs for dev",
        "",
        "docs/control/EVIDENCE_INDEX.md",
    ),
    (
        "Re-run both workflows on dev after the merge",
        10,
        "both are green on the merged head",
        "read the workflow runs for dev",
        "",
        "docs/control/EVIDENCE_INDEX.md",
    ),
    (
        "Open the dev to main pull request",
        10,
        "the PR exists with the required checks configured",
        "read the pull request",
        "",
        "docs/control/EVIDENCE_INDEX.md",
    ),
    (
        "Require every check green on that pull request",
        10,
        "no check is red or skipped",
        "read the pull request checks",
        "",
        "docs/control/EVIDENCE_INDEX.md",
    ),
    (
        "Merge to main",
        10,
        "main carries the release commit",
        "git log --oneline -1 origin/main",
        "",
        "docs/control/EVIDENCE_INDEX.md",
    ),
    (
        "Build the final archive from main",
        10,
        "the archive is built from the released commit",
        "read the windows workflow run for main",
        "",
        "docs/control/EVIDENCE_INDEX.md",
    ),
    (
        "Verify the final archive against its manifest",
        10,
        "every member verifies independently",
        ".venv/bin/python scripts/check_release.py",
        "",
        "docs/control/EVIDENCE_INDEX.md",
    ),
    (
        "Write the final implementation report from the live evidence",
        10,
        "the report cites the live run, not a plan",
        "read FINAL_IMPLEMENTATION_REPORT.md",
        "",
        "FINAL_IMPLEMENTATION_REPORT.md",
    ),
    (
        "Tag v1.0.0",
        10,
        "the tag points at the merged release commit",
        "git tag --points-at origin/main",
        "",
        "docs/control/EVIDENCE_INDEX.md",
    ),
    (
        "Publish the GitHub Release with the archive and its digest",
        10,
        "the release exists, carries the archive, and records its sha256",
        "read the GitHub release",
        "",
        "docs/control/EVIDENCE_INDEX.md",
    ),
]

E15 = Epic(
    id="E15",
    title="Endurance and final release",
    subsystem="release",
    integration_scenario=(
        "After a two-hour autonomous run with no unsafe action and no save "
        "corruption, the branch merges to main with every check green, the "
        "archive is rebuilt from main and verified member by member, and v1.0.0 "
        "is published with its digest."
    ),
    required_ci=None,
    milestones=(
        Milestone(
            id="E15-M01",
            title="Endurance",
            tasks=_tasks("E15", "M01", "live", "live", "local", rows=_E15_ENDURANCE),
            checks=(
                Check(
                    id="E15-M01-C01",
                    statement=(
                        "Two hours of autonomy with no unsafe action, no crash and no save "
                        "corruption."
                    ),
                    command="read evidence/live/endurance-2h.json",
                ),
            ),
        ),
        Milestone(
            id="E15-M02",
            title="Release",
            tasks=_tasks("E15", "M02", "release", "live", "local", rows=_E15_RELEASE),
            checks=(
                Check(
                    id="E15-M02-C01",
                    statement="v1.0.0 is a Release carrying an archive that verifies.",
                    command="read the GitHub release",
                ),
            ),
        ),
    ),
)
