#!/usr/bin/env python3
"""Epics 6-10: RemoteCoreServices, MCP transport and E2E, goals, voice, TeamON."""

from __future__ import annotations

from scripts.plan_epics_a import CI, RC, _tasks
from scripts.plan_model import Check, Epic, Milestone

REM = "packages/pz_agent_mcp/src/pz_agent_mcp/remote"
CD = f"{REM}/codec"
MCP = "packages/pz_agent_mcp/src/pz_agent_mcp"
VOICE = "packages/pz_agent_voice/src/pz_agent_voice"
T = "tests/unit"
C = "tests/contract"

# ---------------------------------------------------------------------------
# E06 — RemoteCoreServices (35, band "integration", weights 6-8)
# ---------------------------------------------------------------------------

_E06_CODEC = [
    (
        "Define the shared decode helpers that never default a missing field",
        7,
        "require_* raises on a missing key; no helper has a default parameter",
        f".venv/bin/pytest {T}/test_rpc_codec_session.py -q",
        f"{T}/test_rpc_codec_session.py",
        f"{CD}/__init__.py",
    ),
    (
        "Refuse a bool where an int is required",
        7,
        "True is not accepted as 1",
        f".venv/bin/pytest {T}/test_rpc_codec_session.py -q",
        f"{T}/test_rpc_codec_session.py",
        f"{CD}/__init__.py",
    ),
    (
        "Decode enums by value and refuse an unknown one",
        7,
        "an unrecognised value raises rather than falling back to a member",
        f".venv/bin/pytest {T}/test_rpc_codec_session.py -q",
        f"{T}/test_rpc_codec_session.py",
        f"{CD}/__init__.py",
    ),
    (
        "Keep every codec refusal free of the value it rejected",
        7,
        "a secret-shaped payload value is absent from str(exc)",
        f".venv/bin/pytest {T}/test_rpc_codec_memory.py -q",
        f"{T}/test_rpc_codec_memory.py",
        f"{CD}/__init__.py",
    ),
    (
        "Codec the session snapshot and stop report",
        7,
        "round-trip equality over every field, both optional present and absent",
        f".venv/bin/pytest {T}/test_rpc_codec_session.py -q",
        f"{T}/test_rpc_codec_session.py",
        f"{CD}/session.py",
    ),
    (
        "Codec the action request and record, delegating the honesty invariant",
        8,
        "a payload claiming SUCCEEDED without POSTCONDITION_MET and evidence is refused",
        f".venv/bin/pytest {T}/test_rpc_codec_actions.py -q",
        f"{T}/test_rpc_codec_actions.py",
        f"{CD}/actions.py",
    ),
    (
        "Codec the plan request, step record and plan record",
        7,
        "steps decode to a tuple, order is preserved, an empty list round-trips",
        f".venv/bin/pytest {T}/test_rpc_codec_plans.py -q",
        f"{T}/test_rpc_codec_plans.py",
        f"{CD}/plans.py",
    ),
    (
        "Codec the memory record, refusing a string in the numeric data map",
        7,
        "a string, a nested object and an array in data are each refused",
        f".venv/bin/pytest {T}/test_rpc_codec_memory.py -q",
        f"{T}/test_rpc_codec_memory.py",
        f"{CD}/memory.py",
    ),
    (
        "Codec the doctor check and log record",
        7,
        "a long message and a path-shaped message survive byte for byte",
        f".venv/bin/pytest {T}/test_rpc_codec_diagnostics.py -q",
        f"{T}/test_rpc_codec_diagnostics.py",
        f"{CD}/diagnostics.py",
    ),
    (
        "Codec the observation, distinguishing absence from unreadable",
        7,
        "None encodes as a missing key; a present but bad value raises",
        f".venv/bin/pytest {T}/test_rpc_codec_world.py -q",
        f"{T}/test_rpc_codec_world.py",
        f"{CD}/observations.py",
    ),
]

_E06_CLIENT = [
    (
        "Implement the RPC client that dials, sends one request and reads one answer",
        7,
        "a call reaches a real server and returns its answer",
        f".venv/bin/pytest {T}/test_rpc_transport.py -q",
        f"{T}/test_rpc_transport.py",
        "packages/pz_agent_core/src/pz_agent_core/rpc/transport.py",
    ),
    (
        "Refuse an answer whose id does not match the request",
        7,
        "a mismatched id raises rather than being returned",
        f".venv/bin/pytest {T}/test_rpc_transport.py -q",
        f"{T}/test_rpc_transport.py",
        "packages/pz_agent_core/src/pz_agent_core/rpc/transport.py",
    ),
    (
        "Implement RemoteCoreServices.session over session.* methods",
        7,
        "status, arm, disarm and stop each reach the core and return its record",
        f".venv/bin/pytest {T}/test_remote_core_services.py -q",
        f"{T}/test_remote_core_services.py",
        f"{REM}/client.py",
    ),
    (
        "Carry a core refusal through as the core's own error, not a transport error",
        8,
        "arm refused by the core raises with the core's message, distinguishable "
        "from the link being down",
        f".venv/bin/pytest {T}/test_remote_core_services.py -q",
        f"{T}/test_remote_core_services.py",
        f"{REM}/client.py",
    ),
    (
        "Implement RemoteCoreServices.observations",
        7,
        "latest() returns None before the first observation and the record after",
        f".venv/bin/pytest {T}/test_remote_core_services.py -q",
        f"{T}/test_remote_core_services.py",
        f"{REM}/client.py",
    ),
    (
        "Implement RemoteCoreServices.capabilities",
        7,
        "report() returns the core's report with every capability state intact",
        f".venv/bin/pytest {T}/test_remote_core_services.py -q",
        f"{T}/test_remote_core_services.py",
        f"{REM}/client.py",
    ),
    (
        "Implement RemoteCoreServices.actions",
        8,
        "submit returns as soon as the action has an id; status polls it",
        f".venv/bin/pytest {T}/test_remote_core_services.py -q",
        f"{T}/test_remote_core_services.py",
        f"{REM}/client.py",
    ),
    (
        "Keep submit from blocking until the postcondition is observed",
        8,
        "a long-running action does not hold the transport, so stop stays reachable",
        f".venv/bin/pytest {T}/test_remote_core_services.py -q",
        f"{T}/test_remote_core_services.py",
        f"{REM}/client.py",
    ),
    (
        "Implement RemoteCoreServices.plans",
        7,
        "execute and current round-trip a plan with its steps",
        f".venv/bin/pytest {T}/test_remote_core_services.py -q",
        f"{T}/test_remote_core_services.py",
        f"{REM}/client.py",
    ),
    (
        "Implement RemoteCoreServices.memory",
        7,
        "query returns records with data values still typed",
        f".venv/bin/pytest {T}/test_remote_core_services.py -q",
        f"{T}/test_remote_core_services.py",
        f"{REM}/client.py",
    ),
    (
        "Implement RemoteCoreServices.diagnostics",
        7,
        "doctor and tail return the core's records with their filters applied",
        f".venv/bin/pytest {T}/test_remote_core_services.py -q",
        f"{T}/test_remote_core_services.py",
        f"{REM}/client.py",
    ),
    (
        "Satisfy the whole CoreServices protocol structurally",
        8,
        "a static check asserts RemoteCoreServices is assignable to CoreServices",
        f".venv/bin/pytest {T}/test_remote_core_services.py -q",
        f"{C}/test_remote_core_round_trip.py::test_remote_core_services_is_a_core_services",
        f"{REM}/client.py",
    ),
]

_E06_ROUTER = [
    (
        "Route every declared method to a port call",
        8,
        "the router answers exactly the methods in ALL_METHODS, no more and no fewer",
        f".venv/bin/pytest {T}/test_remote_router.py -q",
        f"{T}/test_remote_router.py::TestTheRoutedSet::test_the_routed_set_equals_the_declared_set",
        f"{REM}/server.py",
    ),
    (
        "Answer UNKNOWN_METHOD for a name the router does not have",
        7,
        "an invented method returns UNKNOWN_METHOD rather than an exception",
        f".venv/bin/pytest {T}/test_remote_router.py -q",
        f"{T}/test_remote_router.py",
        f"{REM}/server.py",
    ),
    (
        "Answer MALFORMED for parameters that fail to decode",
        7,
        "a bad param object returns MALFORMED, not CORE_REFUSED",
        f".venv/bin/pytest {T}/test_remote_router.py -q",
        f"{T}/test_remote_router.py",
        f"{REM}/server.py",
    ),
    (
        "Answer CORE_REFUSED for the core's own refusal",
        7,
        "a port raising is carried through with its message",
        f".venv/bin/pytest {T}/test_remote_router.py -q",
        f"{T}/test_remote_router.py",
        f"{REM}/server.py",
    ),
    (
        "Keep the router free of policy the core owns",
        8,
        "no capability, arming or idempotency decision is made in the router",
        "read the router source",
        f"{T}/test_remote_router.py",
        f"{REM}/server.py",
    ),
    (
        "Prove client and router agree on every method name",
        8,
        "a name spelled in one and not the other fails a test",
        f".venv/bin/pytest {T}/test_remote_router.py -q",
        f"{T}/test_remote_router.py",
        f"{REM}/methods.py",
    ),
    (
        "Round-trip every port through client and router against a fake core",
        8,
        "each of the seven ports is exercised end to end over a real socket",
        f".venv/bin/pytest {C}/test_remote_core_round_trip.py -q",
        f"{C}/test_remote_core_round_trip.py",
        f"{C}/test_remote_core_round_trip.py",
    ),
    (
        "Prove the round trip preserves the honesty invariant",
        8,
        "a core reporting SUCCEEDED with proof arrives with proof; without it, refused",
        f".venv/bin/pytest {C}/test_remote_core_round_trip.py -q",
        f"{C}/test_remote_core_round_trip.py",
        f"{C}/test_remote_core_round_trip.py",
    ),
]

_E06_WIRING = [
    (
        "Serve the router from the sidecar over the RPC server",
        8,
        "a second process reaches the real core through the link",
        f".venv/bin/pytest {C}/test_remote_core_round_trip.py -q",
        f"{C}/test_remote_core_round_trip.py",
        "packages/pz_agent_cli/src/pz_agent_cli/supervisor.py",
    ),
    (
        "Build RemoteCoreServices from a state directory alone",
        8,
        "given a state dir, the client finds the descriptor, reads the token and connects",
        f".venv/bin/pytest {T}/test_remote_core_services.py -q",
        f"{T}/test_remote_core_services.py",
        f"{REM}/client.py",
    ),
    (
        "Report a stale descriptor as 'the sidecar is not running'",
        7,
        "the message names the remedy, not an internal error",
        f".venv/bin/pytest {T}/test_remote_core_services.py -q",
        f"{T}/test_remote_core_services.py",
        f"{REM}/client.py",
    ),
    (
        "Replace UnroutedPlanPort with the remote plan port",
        8,
        "no port in the shipped wiring raises 'not routed'",
        "grep -rn 'Unrouted' packages",
        f"{T}/test_remote_core_services.py",
        f"{REM}/client.py",
    ),
    (
        "Prove no CoreServices port is left unimplemented in the shipped path",
        8,
        "every Protocol member has a concrete implementation reachable from the entry point",
        f".venv/bin/pytest {T}/test_remote_core_services.py -q",
        f"{T}/test_remote_core_services.py",
        f"{REM}/client.py",
    ),
]

E06 = Epic(
    id="E06",
    title="RemoteCoreServices",
    subsystem="rpc-client",
    integration_scenario=(
        "A second process holding only a state directory reaches the real core "
        "through every one of the seven ports, and a success it reports is one "
        "the core proved."
    ),
    required_ci=CI,
    milestones=(
        Milestone(
            id="E06-M01",
            title="Record codecs",
            tasks=_tasks("E06", "M01", "rpc-codec", "integration", "remote", rows=_E06_CODEC),
            checks=(
                Check(
                    id="E06-M01-C01",
                    statement="Every codec is a matched pair; nothing encodes that cannot decode.",
                    command=f".venv/bin/pytest {T} -k rpc_codec -q",
                ),
            ),
        ),
        Milestone(
            id="E06-M02",
            title="The client ports",
            tasks=_tasks("E06", "M02", "rpc-client", "integration", "remote", rows=_E06_CLIENT),
            checks=(
                Check(
                    id="E06-M02-C01",
                    statement="RemoteCoreServices is structurally a CoreServices, checked by mypy.",
                    command=".venv/bin/mypy packages/pz_agent_mcp/src",
                ),
            ),
        ),
        Milestone(
            id="E06-M03",
            title="The router",
            tasks=_tasks("E06", "M03", "rpc-server", "integration", "remote", rows=_E06_ROUTER),
            checks=(
                Check(
                    id="E06-M03-C01",
                    statement=(
                        "Client and router implement the same method set, proven by a test over "
                        "both."
                    ),
                    command=f".venv/bin/pytest {T}/test_remote_router.py -q",
                ),
            ),
        ),
        Milestone(
            id="E06-M04",
            title="Wiring into the sidecar",
            tasks=_tasks("E06", "M04", "rpc-server", "integration", "remote", rows=_E06_WIRING),
            checks=(
                Check(
                    id="E06-M04-C01",
                    statement="No port in the shipped wiring is a stub or an Unrouted placeholder.",
                    command="grep -rn 'Unrouted\\|NotImplementedError' packages/*/src",
                ),
            ),
        ),
    ),
)

# ---------------------------------------------------------------------------
# E07 — MCP transport and E2E (35, band "integration", weights 7-8)
# ---------------------------------------------------------------------------

_E07_CLI = [
    (
        "Accept --state-dir on pz-agent-mcp",
        7,
        "the flag is parsed and used to find the descriptor",
        f".venv/bin/pytest {T}/test_mcp_entry.py -q",
        f"{T}/test_mcp_entry.py",
        f"{MCP}/__main__.py",
    ),
    (
        "Accept --zomboid-dir on pz-agent-mcp",
        7,
        "the flag is parsed and derives the state dir",
        f".venv/bin/pytest {T}/test_mcp_entry.py -q",
        f"{T}/test_mcp_entry.py",
        f"{MCP}/__main__.py",
    ),
    (
        "Keep --describe answerable with no core and no SDK",
        7,
        "--describe writes the catalogue and exits zero without a sidecar",
        f".venv/bin/pytest {T}/test_mcp_entry.py -q",
        f"{T}/test_mcp_entry.py",
        f"{MCP}/__main__.py",
    ),
    (
        "Keep --version answerable with no core",
        7,
        "--version prints and exits zero",
        f".venv/bin/pytest {T}/test_mcp_entry.py -q",
        f"{T}/test_mcp_entry.py",
        f"{MCP}/__main__.py",
    ),
    (
        "Default the state directory when no flag is given",
        7,
        "the default matches the CLI's own state directory",
        f".venv/bin/pytest {T}/test_mcp_entry.py -q",
        f"{T}/test_mcp_entry.py",
        f"{MCP}/__main__.py",
    ),
    (
        "Refuse an unreadable state directory with a named reason",
        7,
        "the message says which path and why, without a traceback",
        f".venv/bin/pytest {T}/test_mcp_entry.py -q",
        f"{T}/test_mcp_entry.py",
        f"{MCP}/__main__.py",
    ),
    (
        "Replace NO_SERVICES_MESSAGE with a real connection attempt",
        8,
        "serve no longer refuses by construction; it connects or says why it cannot",
        f".venv/bin/pytest {T}/test_mcp_entry.py -q",
        f"{T}/test_mcp_entry.py",
        f"{MCP}/__main__.py",
    ),
    (
        "Print the MCP client entry with the state dir the process will use",
        7,
        "pz-agent start's snippet and the executable agree on the directory",
        f".venv/bin/pytest {C}/test_mcp_snippet_is_json.py -q",
        f"{C}/test_mcp_snippet_is_json.py",
        "packages/pz_agent_cli/src/pz_agent_cli/app.py",
    ),
]

_E07_DISCOVERY = [
    (
        "Find the descriptor under the given state directory",
        7,
        "the standard path is used",
        f".venv/bin/pytest {T}/test_mcp_entry.py -q",
        f"{T}/test_mcp_entry.py",
        f"{MCP}/__main__.py",
    ),
    (
        "Report 'the sidecar is not running' when there is no descriptor",
        7,
        "the exit code and message distinguish this from a crash",
        f".venv/bin/pytest {T}/test_mcp_entry.py -q",
        f"{T}/test_mcp_entry.py",
        f"{MCP}/__main__.py",
    ),
    (
        "Report a stale descriptor distinctly from a malformed one",
        7,
        "two different messages and remedies",
        f".venv/bin/pytest {T}/test_mcp_entry.py -q",
        f"{T}/test_mcp_entry.py",
        f"{MCP}/__main__.py",
    ),
    (
        "Report a protocol mismatch as two installs, not as a bug",
        7,
        "the message names the cause",
        f".venv/bin/pytest {T}/test_mcp_entry.py -q",
        f"{T}/test_mcp_entry.py",
        f"{MCP}/__main__.py",
    ),
    (
        "Read the token from beside the descriptor",
        7,
        "the token file is found by name",
        f".venv/bin/pytest {T}/test_mcp_entry.py -q",
        f"{T}/test_mcp_entry.py",
        f"{MCP}/__main__.py",
    ),
    (
        "Never write the token or the address to stdout",
        8,
        "stdout carries JSON-RPC only; neither secret appears anywhere on it",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py",
        f"{MCP}/__main__.py",
    ),
]

_E07_LAUNCH = [
    (
        "Launch pz-agent-mcp as a real subprocess in the test suite",
        8,
        "the test starts the actual entry point, not an in-process double",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py",
        f"{C}/test_mcp_subprocess_e2e.py",
    ),
    (
        "Keep stdout free of anything that is not JSON-RPC",
        8,
        "no log line, banner or warning reaches stdout",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py",
        f"{MCP}/__main__.py",
    ),
    (
        "Send diagnostics to stderr instead",
        7,
        "stderr carries the messages stdout must not",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py",
        f"{MCP}/__main__.py",
    ),
    (
        "Exit cleanly when its stdin closes",
        7,
        "the process ends rather than hanging",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py",
        f"{MCP}/__main__.py",
    ),
    (
        "Survive the sidecar going away mid-session",
        8,
        "tool calls report the link is down; the process does not crash",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py",
        f"{MCP}/__main__.py",
    ),
    (
        "Bound the time any single tool call may take",
        8,
        "a stalled core produces an error rather than an unbounded wait",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py",
        f"{MCP}/server.py",
    ),
    (
        "Run the subprocess E2E on windows-latest",
        8,
        "the E2E suite passes in a windows workflow run",
        "read the windows workflow run",
        f"{C}/test_mcp_subprocess_e2e.py",
        "docs/control/evidence/step-30-40/windows-suite.txt",
    ),
]

_E07_PROTOCOL = [
    (
        "Complete an MCP initialize handshake",
        8,
        "the server answers initialize with its capabilities",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py::TestTheProtocol::test_initialize_answers_with_the_server_capabilities",
        f"{MCP}/server.py",
    ),
    (
        "Answer tools/list with the published set",
        8,
        "the list matches the capability-filtered catalogue",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py::TestTheProtocol::test_tools_list_matches_the_published_set",
        f"{MCP}/server.py",
    ),
    (
        "Withhold a tool whose capability is not usable",
        8,
        "an unusable tool is neither listed nor callable, and calling it says why",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py::TestTheProtocol::test_an_unusable_tool_is_neither_listed_nor_callable",
        f"{MCP}/router.py",
    ),
    (
        "Answer a real tools/call that reaches the core",
        8,
        "the call travels MCP -> RPC -> core and the answer comes back",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py::TestTheProtocol::test_a_tool_call_reaches_the_core_and_the_answer_returns",
        f"{MCP}/server.py",
    ),
    (
        "Answer a read-only tool call with the core's observation",
        8,
        "the observation is the core's, compacted, not synthesised at the boundary",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py::TestTheProtocol::test_a_read_only_call_answers_with_the_cores_observation",
        f"{MCP}/router.py",
    ),
    (
        "Refuse a mutating tool call while the session is disarmed",
        8,
        "the refusal carries NOT_ARMED from the core",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py::TestTheProtocol::test_a_mutating_call_is_refused_while_disarmed",
        f"{MCP}/router.py",
    ),
    (
        "Answer a replayed idempotency key with the original result",
        8,
        "the second call does not act; it replays",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py::TestTheProtocol::test_a_replayed_key_answers_with_the_original_result",
        f"{MCP}/router.py",
    ),
    (
        "Never report succeeded without the core's observed postcondition",
        8,
        "a tool answer saying succeeded carries evidence from the core",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py::TestTheProtocol::test_no_answer_says_succeeded_without_the_cores_evidence",
        f"{MCP}/router.py",
    ),
    (
        "Answer resources/list with the published resources",
        7,
        "the list matches the catalogue",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py::TestTheProtocol::test_resources_list_matches_the_published_set",
        f"{MCP}/server.py",
    ),
    (
        "Answer a real resources/read from the core",
        8,
        "the content comes from the core over RPC",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py::TestTheProtocol::test_a_resource_read_answers_from_the_core",
        f"{MCP}/server.py",
    ),
    (
        "Quarantine game-authored text in every answer",
        8,
        "no unmarked game string reaches a client",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py::TestTheProtocol::test_no_game_authored_text_reaches_a_client_unmarked",
        f"{MCP}/scrub.py",
    ),
    (
        "Publish save_scope rather than the raw save id",
        8,
        "the raw id never leaves the boundary",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py::TestTheProtocol::test_the_raw_save_id_never_leaves_the_boundary",
        f"{MCP}/router.py",
    ),
    (
        "Keep --describe agreeing with what a running server publishes",
        7,
        "the catalogue and the live tools/list agree on names and schemas",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py::TestTheProtocol::test_describe_agrees_with_a_running_servers_tools_list",
        f"{MCP}/catalog.py",
    ),
    (
        "Prove --describe alone is not treated as evidence of a working server",
        8,
        "the E2E suite fails if the transport is broken even when --describe succeeds",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py::TestTheProtocol::test_describe_succeeding_does_not_imply_a_working_transport",
        f"{C}/test_mcp_subprocess_e2e.py",
    ),
]

E07 = Epic(
    id="E07",
    title="MCP transport and end to end",
    subsystem="mcp",
    integration_scenario=(
        "A real MCP client process speaks initialize, tools/list, tools/call and "
        "resources/read to a launched pz-agent-mcp subprocess, which reaches a "
        "running sidecar over the RPC link and answers from the real core."
    ),
    required_ci=RC,
    milestones=(
        Milestone(
            id="E07-M01",
            title="The command surface",
            tasks=_tasks("E07", "M01", "mcp", "integration", "remote", rows=_E07_CLI),
            checks=(
                Check(
                    id="E07-M01-C01",
                    statement="No invocation of pz-agent-mcp refuses by construction any more.",
                    command=f".venv/bin/pytest {T}/test_mcp_entry.py -q",
                ),
            ),
        ),
        Milestone(
            id="E07-M02",
            title="Discovery",
            tasks=_tasks("E07", "M02", "mcp", "integration", "remote", rows=_E07_DISCOVERY),
            checks=(
                Check(
                    id="E07-M02-C01",
                    statement="Every discovery failure has its own message and remedy.",
                    command=f".venv/bin/pytest {T}/test_mcp_entry.py -q",
                ),
            ),
        ),
        Milestone(
            id="E07-M03",
            title="Launching a real subprocess",
            tasks=_tasks("E07", "M03", "mcp", "integration", "remote", rows=_E07_LAUNCH),
            checks=(
                Check(
                    id="E07-M03-C01",
                    statement="stdout carries JSON-RPC and nothing else, on both platforms.",
                    command="read the windows workflow run",
                ),
            ),
        ),
        Milestone(
            id="E07-M04",
            title="The protocol end to end",
            tasks=_tasks("E07", "M04", "mcp", "integration", "remote", rows=_E07_PROTOCOL),
            checks=(
                Check(
                    id="E07-M04-C01",
                    statement=(
                        "A tool call travels client -> MCP -> RPC -> core and back, "
                        "and a reported success carries the core's evidence."
                    ),
                    command=f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
                ),
            ),
        ),
    ),
)

# ---------------------------------------------------------------------------
# E08 — typed goal channel (25, band "integration", weights 6-8)
# ---------------------------------------------------------------------------

_E08_SCHEMA = [
    (
        "Define a closed GoalKind enumeration",
        7,
        "the set is fixed; an unknown kind is refused",
        f".venv/bin/pytest {T}/test_goal_channel.py -q",
        f"{T}/test_goal_channel.py",
        "packages/pz_agent_core/src/pz_agent_core/goals/model.py",
    ),
    (
        "Give each GoalKind its typed parameters",
        7,
        "parameters are per-kind, not a free dict",
        f".venv/bin/pytest {T}/test_goal_channel.py -q",
        f"{T}/test_goal_channel.py",
        "packages/pz_agent_core/src/pz_agent_core/goals/model.py",
    ),
    (
        "Refuse a goal carrying free text as a parameter",
        8,
        "no GoalKind accepts an unbounded string",
        f".venv/bin/pytest {T}/test_goal_channel.py -q",
        f"{T}/test_goal_channel.py",
        "packages/pz_agent_core/src/pz_agent_core/goals/model.py",
    ),
    (
        "Publish the goal JSON schema",
        7,
        "schemas/goal.schema.json compiles and pins the closed set",
        ".venv/bin/python scripts/check_schemas.py",
        f"{C}/test_goal_schema_conformance.py",
        "schemas/goal.schema.json",
    ),
    (
        "Bound every numeric goal parameter",
        7,
        "each has a declared range and is checked",
        f".venv/bin/pytest {T}/test_goal_channel.py -q",
        f"{T}/test_goal_channel.py",
        "packages/pz_agent_core/src/pz_agent_core/goals/model.py",
    ),
    (
        "Give each goal an idempotency key",
        7,
        "a resubmitted goal is not a second goal",
        f".venv/bin/pytest {T}/test_goal_channel.py -q",
        f"{T}/test_goal_channel.py",
        "packages/pz_agent_core/src/pz_agent_core/goals/model.py",
    ),
]

_E08_QUEUE = [
    (
        "Bound the goal queue",
        7,
        "submitting past the cap is refused, not dropped silently",
        f".venv/bin/pytest {T}/test_goal_channel.py -q",
        f"{T}/test_goal_channel.py",
        "packages/pz_agent_core/src/pz_agent_core/goals/queue.py",
    ),
    (
        "Refuse a second goal while one is active",
        7,
        "the refusal names the active goal",
        f".venv/bin/pytest {T}/test_goal_channel.py -q",
        f"{T}/test_goal_channel.py",
        "packages/pz_agent_core/src/pz_agent_core/goals/queue.py",
    ),
    (
        "Give every goal a terminal state",
        7,
        "no goal can remain in flight indefinitely",
        f".venv/bin/pytest {T}/test_goal_channel.py -q",
        f"{T}/test_goal_channel.py",
        "packages/pz_agent_core/src/pz_agent_core/goals/queue.py",
    ),
    (
        "Bound a goal by wall-clock time",
        7,
        "a goal past its deadline ends as expired",
        f".venv/bin/pytest {T}/test_goal_channel.py -q",
        f"{T}/test_goal_channel.py",
        "packages/pz_agent_core/src/pz_agent_core/goals/queue.py",
    ),
    (
        "Bound a goal by step count",
        7,
        "a goal past its step budget ends as exhausted",
        f".venv/bin/pytest {T}/test_goal_channel.py -q",
        f"{T}/test_goal_channel.py",
        "packages/pz_agent_core/src/pz_agent_core/goals/queue.py",
    ),
    (
        "Cancel a goal without waiting for its current step",
        8,
        "cancel is observed within one tick",
        f".venv/bin/pytest {T}/test_goal_channel.py -q",
        f"{T}/test_goal_channel.py",
        "packages/pz_agent_core/src/pz_agent_core/goals/queue.py",
    ),
    (
        "End every goal when the session disarms",
        8,
        "disarm terminates the active goal",
        f".venv/bin/pytest {T}/test_goal_channel.py -q",
        f"{T}/test_goal_channel.py",
        "packages/pz_agent_core/src/pz_agent_core/goals/queue.py",
    ),
    (
        "End every goal on a panic stop",
        8,
        "a stop leaves no goal in flight",
        f".venv/bin/pytest {T}/test_goal_channel.py -q",
        f"{T}/test_goal_channel.py",
        "packages/pz_agent_core/src/pz_agent_core/goals/queue.py",
    ),
]

_E08_RPC = [
    (
        "Add goal.submit to the RPC method set",
        7,
        "the method is declared, routed and answered",
        f".venv/bin/pytest {T}/test_remote_router.py -q",
        f"{T}/test_remote_router.py",
        f"{REM}/methods.py",
    ),
    (
        "Add goal.status",
        7,
        "the method returns the goal's state and progress",
        f".venv/bin/pytest {T}/test_remote_router.py -q",
        f"{T}/test_remote_router.py",
        f"{REM}/methods.py",
    ),
    (
        "Add goal.cancel",
        7,
        "the method cancels and reports what it cancelled",
        f".venv/bin/pytest {T}/test_remote_router.py -q",
        f"{T}/test_remote_router.py",
        f"{REM}/methods.py",
    ),
    (
        "Codec the goal records",
        7,
        "round-trip equality including the closed kind",
        f".venv/bin/pytest {T}/test_rpc_codec_goals.py -q",
        f"{T}/test_rpc_codec_goals.py",
        f"{CD}/goals.py",
    ),
    (
        "Refuse an unknown GoalKind on the wire",
        8,
        "an invented kind is refused, not defaulted",
        f".venv/bin/pytest {T}/test_rpc_codec_goals.py -q",
        f"{T}/test_rpc_codec_goals.py",
        f"{CD}/goals.py",
    ),
    (
        "Publish pz_goal_submit as an MCP tool",
        8,
        "the tool is listed and callable",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py",
        f"{MCP}/catalog.py",
    ),
    (
        "Publish pz_goal_status",
        7,
        "the tool is listed and callable",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py",
        f"{MCP}/catalog.py",
    ),
    (
        "Publish pz_goal_cancel",
        7,
        "the tool is listed and callable",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py",
        f"{MCP}/catalog.py",
    ),
    (
        "Gate the goal tools on arming like any other mutating tool",
        8,
        "a goal cannot be submitted while disarmed",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py",
        f"{MCP}/router.py",
    ),
    (
        "Submit a goal end to end from an MCP client",
        8,
        "a real client submits, polls and cancels a goal against a running core",
        f".venv/bin/pytest {C}/test_mcp_subprocess_e2e.py -q",
        f"{C}/test_mcp_subprocess_e2e.py",
        f"{C}/test_mcp_subprocess_e2e.py",
    ),
    (
        "Prove a submitted goal reaches the planner",
        8,
        "the plan the core runs is the one the goal asked for",
        f".venv/bin/pytest {C}/test_goal_reaches_the_planner.py -q",
        f"{C}/test_goal_reaches_the_planner.py",
        f"{C}/test_goal_reaches_the_planner.py",
    ),
]

E08 = Epic(
    id="E08",
    title="Typed goal channel",
    subsystem="goals",
    integration_scenario=(
        "An MCP client submits a typed goal, the core plans and acts on it, the "
        "client observes progress, and a cancel stops it within one tick."
    ),
    required_ci=CI,
    milestones=(
        Milestone(
            id="E08-M01",
            title="The closed goal schema",
            tasks=_tasks("E08", "M01", "goals", "integration", "remote", rows=_E08_SCHEMA),
            checks=(
                Check(
                    id="E08-M01-C01",
                    statement="No goal can carry free text into the core.",
                    command=f".venv/bin/pytest {T}/test_goal_channel.py -q",
                ),
            ),
        ),
        Milestone(
            id="E08-M02",
            title="The bounded queue",
            tasks=_tasks("E08", "M02", "goals", "integration", "remote", rows=_E08_QUEUE),
            checks=(
                Check(
                    id="E08-M02-C01",
                    statement="Disarm and panic stop both leave no goal in flight.",
                    command=f".venv/bin/pytest {T}/test_goal_channel.py -q",
                ),
            ),
        ),
        Milestone(
            id="E08-M03",
            title="Goals over RPC and MCP",
            tasks=_tasks("E08", "M03", "goals", "integration", "remote", rows=_E08_RPC),
            checks=(
                Check(
                    id="E08-M03-C01",
                    statement="A goal submitted by a client is the plan the core runs.",
                    command=f".venv/bin/pytest {C}/test_goal_reaches_the_planner.py -q",
                ),
            ),
        ),
    ),
)

# ---------------------------------------------------------------------------
# E09 — voice goal routing (25, band "transport", weights 5-7)
# ---------------------------------------------------------------------------

_E09_PORT = [
    (
        "Implement a working PlanPort over Core RPC for the voice companion",
        6,
        "the companion's plan port reaches the core rather than refusing",
        f".venv/bin/pytest {T}/test_voice_plan_port.py -q",
        f"{T}/test_voice_plan_port.py",
        f"{VOICE}/ports.py",
    ),
    (
        "Remove the placeholder that refused every plan",
        6,
        "no code path answers 'not routed'",
        "grep -rn 'Unrouted' packages",
        f"{T}/test_voice_plan_port.py",
        f"{VOICE}/ports.py",
    ),
    (
        "Keep arm, disarm and stop on their existing short path",
        6,
        "the stop path does not depend on the goal channel being up",
        f".venv/bin/pytest {T}/test_voice_plan_port.py -q",
        f"{T}/test_voice_plan_port.py",
        f"{VOICE}/ports.py",
    ),
    (
        "Prove a stop still works when the RPC link is down",
        6,
        "a stop is honoured with the core unreachable",
        f".venv/bin/pytest {T}/test_voice_plan_port.py -q",
        f"{T}/test_voice_plan_port.py",
        f"{VOICE}/ports.py",
    ),
    (
        "Bound how long a voice command waits for the core",
        6,
        "a stalled core produces a spoken failure, not silence",
        f".venv/bin/pytest {T}/test_voice_plan_port.py -q",
        f"{T}/test_voice_plan_port.py",
        f"{VOICE}/ports.py",
    ),
]

_E09_INTENT = [
    (
        "Map each supported Russian intent to a GoalKind",
        6,
        "every intent in the grammar resolves to a kind or is refused explicitly",
        f".venv/bin/pytest {T}/test_voice_intents.py -q",
        f"{T}/test_voice_intents.py",
        f"{VOICE}/intent.py",
    ),
    (
        "Refuse an intent with no GoalKind rather than inventing one",
        6,
        "an unmapped phrase produces a named refusal",
        f".venv/bin/pytest {T}/test_voice_intents.py -q",
        f"{T}/test_voice_intents.py",
        f"{VOICE}/intent.py",
    ),
    (
        "Extract typed parameters from the phrase",
        6,
        "quantities and targets become typed fields",
        f".venv/bin/pytest {T}/test_voice_intents.py -q",
        f"{T}/test_voice_intents.py",
        f"{VOICE}/intent.py",
    ),
    (
        "Refuse a parameter outside its declared range",
        6,
        "an out-of-range quantity is refused",
        f".venv/bin/pytest {T}/test_voice_intents.py -q",
        f"{T}/test_voice_intents.py",
        f"{VOICE}/intent.py",
    ),
    (
        "Cover the Russian phrasings for each intent",
        5,
        "each intent has several attested phrasings",
        f".venv/bin/pytest {T}/test_voice_intents.py -q",
        f"{T}/test_voice_intents.py",
        f"{VOICE}/intent.py",
    ),
    (
        "Keep the transcript out of the goal",
        6,
        "no transcript text is carried into a GoalKind",
        f".venv/bin/pytest {T}/test_voice_intents.py -q",
        f"{T}/test_voice_intents.py",
        f"{VOICE}/intent.py",
    ),
    (
        "Keep the transcript out of the sidecar entirely",
        6,
        "no RPC call from the voice package carries transcript text",
        f".venv/bin/pytest {T}/test_voice_privacy.py -q",
        f"{T}/test_voice_privacy.py",
        f"{VOICE}/driver.py",
    ),
    (
        "Keep the transcript out of the logs",
        6,
        "voice logs record intents and outcomes only",
        f".venv/bin/pytest {T}/test_voice_privacy.py -q",
        f"{T}/test_voice_privacy.py",
        f"{VOICE}/driver.py",
    ),
    (
        "Keep the transcript out of the support bundle",
        6,
        "no bundle member contains a transcript",
        f".venv/bin/pytest {T}/test_voice_privacy.py -q",
        f"{T}/test_voice_privacy.py",
        f"{VOICE}/driver.py",
    ),
    (
        "Speak a refusal the user can act on",
        5,
        "each refusal has a spoken form naming the cause",
        f".venv/bin/pytest {T}/test_voice_intents.py -q",
        f"{T}/test_voice_intents.py",
        f"{VOICE}/phrases.py",
    ),
    (
        "Recognise the stop phrase before any other intent",
        6,
        "the stop grammar is matched first, so a stop is never mistaken for a goal",
        f".venv/bin/pytest {T}/test_voice_intents.py -q",
        f"{T}/test_voice_intents.py",
        f"{VOICE}/intent.py",
    ),
    (
        "Bound how much text one utterance may carry",
        6,
        "an overlong transcript is truncated before matching, not buffered",
        f".venv/bin/pytest {T}/test_voice_intents.py -q",
        f"{T}/test_voice_intents.py",
        f"{VOICE}/intent.py",
    ),
    (
        "Refuse an intent whose GoalKind is not usable on this build",
        6,
        "an intent for an unverified capability is refused with the capability named",
        f".venv/bin/pytest {T}/test_voice_intents.py -q",
        f"{T}/test_voice_intents.py",
        f"{VOICE}/intent.py",
    ),
    (
        "Keep the intent grammar and the GoalKind set in step",
        6,
        "an intent naming a kind that does not exist fails a test",
        f".venv/bin/pytest {T}/test_voice_intents.py -q",
        f"{T}/test_voice_intents.py",
        f"{VOICE}/intent.py",
    ),
]

_E09_E2E = [
    (
        "Deliver a goal from a spoken phrase to the core",
        6,
        "an intent produces a goal the core receives",
        f".venv/bin/pytest {C}/test_voice_goal_e2e.py -q",
        f"{C}/test_voice_goal_e2e.py",
        f"{C}/test_voice_goal_e2e.py",
    ),
    (
        "Report goal progress back through the companion",
        6,
        "the user is told when a goal ends",
        f".venv/bin/pytest {C}/test_voice_goal_e2e.py -q",
        f"{C}/test_voice_goal_e2e.py",
        f"{C}/test_voice_goal_e2e.py",
    ),
    (
        "Cancel a goal by voice",
        6,
        "a spoken cancel ends the active goal",
        f".venv/bin/pytest {C}/test_voice_goal_e2e.py -q",
        f"{C}/test_voice_goal_e2e.py",
        f"{C}/test_voice_goal_e2e.py",
    ),
    (
        "Write voice diagnostics to logs/",
        5,
        "an intent and its outcome reach a file, with no transcript",
        f".venv/bin/pytest {T}/test_voice_privacy.py -q",
        f"{T}/test_voice_privacy.py",
        f"{VOICE}/driver.py",
    ),
    (
        "Correct QUICKSTART to match what voice can actually do",
        5,
        "the document and the runtime agree on which commands work",
        f".venv/bin/pytest {C}/test_documented_commands_parse.py -q",
        f"{C}/test_documented_commands_parse.py",
        "docs/QUICKSTART.md",
    ),
    (
        "Prove a fake voice adapter is not counted as a real integration",
        6,
        "the E2E suite fails if the goal never reaches the core, even with the adapter green",
        f".venv/bin/pytest {C}/test_voice_goal_e2e.py -q",
        f"{C}/test_voice_goal_e2e.py",
        f"{C}/test_voice_goal_e2e.py",
    ),
]

E09 = Epic(
    id="E09",
    title="Voice goal routing",
    subsystem="voice",
    integration_scenario=(
        "A Russian phrase becomes a typed goal, the core acts on it, the companion "
        "reports the outcome, and no transcript text reaches the sidecar, the logs "
        "or a support bundle."
    ),
    required_ci=CI,
    milestones=(
        Milestone(
            id="E09-M01",
            title="A working plan port",
            tasks=_tasks("E09", "M01", "voice", "transport", "remote", rows=_E09_PORT),
            checks=(
                Check(
                    id="E09-M01-C01",
                    statement="A stop is honoured with the RPC link down.",
                    command=f".venv/bin/pytest {T}/test_voice_plan_port.py -q",
                ),
            ),
        ),
        Milestone(
            id="E09-M02",
            title="Intents to goals",
            tasks=_tasks("E09", "M02", "voice", "transport", "remote", rows=_E09_INTENT),
            checks=(
                Check(
                    id="E09-M02-C01",
                    statement="No transcript text leaves the voice process.",
                    command=f".venv/bin/pytest {T}/test_voice_privacy.py -q",
                ),
            ),
        ),
        Milestone(
            id="E09-M03",
            title="Voice end to end",
            tasks=_tasks("E09", "M03", "voice", "transport", "remote", rows=_E09_E2E),
            checks=(
                Check(
                    id="E09-M03-C01",
                    statement="A spoken goal is one the core actually ran.",
                    command=f".venv/bin/pytest {C}/test_voice_goal_e2e.py -q",
                ),
            ),
        ),
    ),
)

# ---------------------------------------------------------------------------
# E10 — TeamON bridge (30, band "transport", weights 5-7)
# ---------------------------------------------------------------------------

_E10_CLIENT = [
    (
        "Implement TeamONBridgeClient as a concrete JSONL subprocess bridge",
        6,
        "the class launches a process and exchanges JSONL, with no abstract method left",
        f".venv/bin/pytest {T}/test_teamon_bridge.py -q",
        f"{T}/test_teamon_bridge.py",
        f"{VOICE}/teamon.py",
    ),
    (
        "Frame each message as one JSON object per line",
        6,
        "a message never spans two lines",
        f".venv/bin/pytest {T}/test_teamon_bridge.py -q",
        f"{T}/test_teamon_bridge.py",
        f"{VOICE}/teamon.py",
    ),
    (
        "Bound the length of any single line read",
        6,
        "an overlong line is refused, not buffered",
        f".venv/bin/pytest {T}/test_teamon_bridge.py -q",
        f"{T}/test_teamon_bridge.py",
        f"{VOICE}/teamon.py",
    ),
    (
        "Refuse a line that is not JSON",
        6,
        "a malformed line is reported and skipped",
        f".venv/bin/pytest {T}/test_teamon_bridge.py -q",
        f"{T}/test_teamon_bridge.py",
        f"{VOICE}/teamon.py",
    ),
    (
        "Bound the time spent waiting for a reply",
        6,
        "a silent bridge times out",
        f".venv/bin/pytest {T}/test_teamon_bridge.py -q",
        f"{T}/test_teamon_bridge.py",
        f"{VOICE}/teamon.py",
    ),
    (
        "Never block the stop path on the bridge",
        6,
        "a stop works with the bridge hung",
        f".venv/bin/pytest {T}/test_teamon_bridge.py -q",
        f"{T}/test_teamon_bridge.py",
        f"{VOICE}/teamon.py",
    ),
    (
        "Restart the bridge process after it exits",
        6,
        "a crashed bridge is restarted, bounded",
        f".venv/bin/pytest {T}/test_teamon_bridge.py -q",
        f"{T}/test_teamon_bridge.py",
        f"{VOICE}/teamon.py",
    ),
    (
        "Bound the number of restarts",
        6,
        "restarting stops after a declared limit",
        f".venv/bin/pytest {T}/test_teamon_bridge.py -q",
        f"{T}/test_teamon_bridge.py",
        f"{VOICE}/teamon.py",
    ),
    (
        "Report a dead bridge to the user rather than failing silently",
        6,
        "the companion says the bridge is down",
        f".venv/bin/pytest {T}/test_teamon_bridge.py -q",
        f"{T}/test_teamon_bridge.py",
        f"{VOICE}/teamon.py",
    ),
    (
        "Keep no credential in the bridge configuration",
        6,
        "no key is read from or written to config",
        "grep -rn 'api_key\\|token' packages/pz_agent_voice/src",
        f"{T}/test_teamon_bridge.py",
        f"{VOICE}/teamon.py",
    ),
]

_E10_PROTOCOL = [
    (
        "Define the bridge message set",
        6,
        "a closed set of message types, each documented",
        f".venv/bin/pytest {T}/test_teamon_bridge.py -q",
        f"{T}/test_teamon_bridge.py",
        "docs/VOICE.md",
    ),
    (
        "Refuse an unknown message type",
        6,
        "an unrecognised type is refused, not ignored",
        f".venv/bin/pytest {T}/test_teamon_bridge.py -q",
        f"{T}/test_teamon_bridge.py",
        f"{VOICE}/teamon.py",
    ),
    (
        "Carry an intent to the bridge without the transcript",
        6,
        "no message contains transcript text",
        f".venv/bin/pytest {T}/test_voice_privacy.py -q",
        f"{T}/test_voice_privacy.py",
        f"{VOICE}/teamon.py",
    ),
    (
        "Carry an outcome back from the bridge",
        6,
        "the companion learns whether the goal ended",
        f".venv/bin/pytest {T}/test_teamon_bridge.py -q",
        f"{T}/test_teamon_bridge.py",
        f"{VOICE}/teamon.py",
    ),
    (
        "Version the bridge protocol",
        6,
        "a major mismatch is refused",
        f".venv/bin/pytest {T}/test_teamon_bridge.py -q",
        f"{T}/test_teamon_bridge.py",
        f"{VOICE}/teamon.py",
    ),
    (
        "Publish the bridge JSON schema",
        5,
        "schemas/teamon_bridge.schema.json compiles",
        ".venv/bin/python scripts/check_schemas.py",
        f"{C}/test_teamon_schema_conformance.py",
        "schemas/teamon_bridge.schema.json",
    ),
    (
        "Keep bridge errors out of the spoken output verbatim",
        6,
        "an error from the bridge is summarised, not read aloud",
        f".venv/bin/pytest {T}/test_teamon_bridge.py -q",
        f"{T}/test_teamon_bridge.py",
        f"{VOICE}/teamon.py",
    ),
    (
        "Bound the size of any bridge message",
        6,
        "an oversized message is refused",
        f".venv/bin/pytest {T}/test_teamon_bridge.py -q",
        f"{T}/test_teamon_bridge.py",
        f"{VOICE}/teamon.py",
    ),
    (
        "Log bridge lifecycle without logging its content",
        6,
        "start, exit and restart are logged; payloads are not",
        f".venv/bin/pytest {T}/test_voice_privacy.py -q",
        f"{T}/test_voice_privacy.py",
        f"{VOICE}/teamon.py",
    ),
    (
        "Document the bridge contract for an implementer",
        5,
        "docs/VOICE.md describes the process, the framing and the message set",
        "read docs/VOICE.md",
        f"{C}/test_archive_documents_resolve.py",
        "docs/VOICE.md",
    ),
]

_E10_E2E = [
    (
        "Run the bridge against a fake subprocess implementing the contract",
        6,
        "a real subprocess speaks JSONL over a pipe and the exchange completes",
        f".venv/bin/pytest {C}/test_teamon_bridge_e2e.py -q",
        f"{C}/test_teamon_bridge_e2e.py",
        f"{C}/test_teamon_bridge_e2e.py",
    ),
    (
        "Deliver an intent through the bridge to a goal",
        6,
        "the goal the core receives is the one asked for",
        f".venv/bin/pytest {C}/test_teamon_bridge_e2e.py -q",
        f"{C}/test_teamon_bridge_e2e.py",
        f"{C}/test_teamon_bridge_e2e.py",
    ),
    (
        "Survive the bridge exiting mid-exchange",
        6,
        "the companion reports it and stays up",
        f".venv/bin/pytest {C}/test_teamon_bridge_e2e.py -q",
        f"{C}/test_teamon_bridge_e2e.py",
        f"{C}/test_teamon_bridge_e2e.py",
    ),
    (
        "Survive the bridge writing garbage",
        6,
        "malformed output does not crash the companion",
        f".venv/bin/pytest {C}/test_teamon_bridge_e2e.py -q",
        f"{C}/test_teamon_bridge_e2e.py",
        f"{C}/test_teamon_bridge_e2e.py",
    ),
    (
        "Prove the fake bridge is not counted as a TeamON integration",
        6,
        "the plan and the documents both record this as a contract test, not a live one",
        "read docs/VOICE.md",
        f"{C}/test_teamon_bridge_e2e.py",
        "docs/VOICE.md",
    ),
    (
        "Leave no bridge process behind after shutdown",
        6,
        "no child survives the companion",
        f".venv/bin/pytest {C}/test_teamon_bridge_e2e.py -q",
        f"{C}/test_teamon_bridge_e2e.py",
        f"{C}/test_teamon_bridge_e2e.py",
    ),
    (
        "Run the bridge E2E on windows-latest",
        6,
        "the suite passes in a windows workflow run",
        "read the windows workflow run",
        f"{C}/test_teamon_bridge_e2e.py",
        "docs/control/evidence/step-30-40/windows-suite.txt",
    ),
    (
        "Keep the bridge optional so the sidecar runs without it",
        6,
        "a missing bridge does not stop pz-agent start",
        f".venv/bin/pytest {T}/test_teamon_bridge.py -q",
        f"{T}/test_teamon_bridge.py",
        f"{VOICE}/teamon.py",
    ),
    (
        "Report bridge absence at voice check rather than at first use",
        6,
        "voice check names the missing bridge",
        f".venv/bin/pytest {T}/test_teamon_bridge.py -q",
        f"{T}/test_teamon_bridge.py",
        f"{VOICE}/driver.py",
    ),
    (
        "Prove no transcript reaches the bridge under any path",
        6,
        "an adversarial phrase containing a path and a credential is not forwarded",
        f".venv/bin/pytest {T}/test_voice_privacy.py -q",
        f"{T}/test_voice_privacy.py",
        f"{T}/test_voice_privacy.py",
    ),
]

E10 = Epic(
    id="E10",
    title="TeamON bridge",
    subsystem="voice-bridge",
    integration_scenario=(
        "A real subprocess speaking the documented JSONL contract carries an intent "
        "to a goal and an outcome back, survives being killed mid-exchange, and "
        "never receives a transcript."
    ),
    required_ci=RC,
    milestones=(
        Milestone(
            id="E10-M01",
            title="The bridge client",
            tasks=_tasks("E10", "M01", "voice-bridge", "transport", "remote", rows=_E10_CLIENT),
            checks=(
                Check(
                    id="E10-M01-C01",
                    statement="Nothing about the bridge can block a panic stop.",
                    command=f".venv/bin/pytest {T}/test_teamon_bridge.py -q",
                ),
            ),
        ),
        Milestone(
            id="E10-M02",
            title="The bridge protocol",
            tasks=_tasks("E10", "M02", "voice-bridge", "transport", "remote", rows=_E10_PROTOCOL),
            checks=(
                Check(
                    id="E10-M02-C01",
                    statement="The documented contract and the implementation agree.",
                    command=f".venv/bin/pytest {C}/test_teamon_schema_conformance.py -q",
                ),
            ),
        ),
        Milestone(
            id="E10-M03",
            title="Bridge end to end",
            tasks=_tasks("E10", "M03", "voice-bridge", "transport", "remote", rows=_E10_E2E),
            checks=(
                Check(
                    id="E10-M03-C01",
                    statement=(
                        "The bridge is exercised by a real subprocess, and the plan "
                        "records that this is a contract test rather than a live "
                        "TeamON integration."
                    ),
                    command=f".venv/bin/pytest {C}/test_teamon_bridge_e2e.py -q",
                ),
            ),
        ),
    ),
)
