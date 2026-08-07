#!/usr/bin/env python3
"""Epics 3-8: evidence, installer, Local Core RPC, RemoteCoreServices, MCP, goals."""

from __future__ import annotations

from scripts.plan_epics_a import CI, RC, _tasks
from scripts.plan_model import Check, Epic, Milestone

EV = "packages/pz_agent_cli/src/pz_agent_cli/livetest/evidence.py"
EVT = "tests/contract/test_evidence_bytes_are_portable.py"
INS = "installer/pz_agent_installer.py"
INST = "tests/unit/test_installer_windows.py"
RPC = "packages/pz_agent_core/src/pz_agent_core/rpc"
DOC = "docs/CORE_RPC.md"
REM = "packages/pz_agent_mcp/src/pz_agent_mcp/remote"

# ---------------------------------------------------------------------------
# E03 — evidence and hashing (30, band "evidence", weights 4-6)
# ---------------------------------------------------------------------------

_E03_CANON = [
    (
        "Define canonical_json_bytes() returning bytes with one trailing newline",
        5,
        "the return is bytes, ends in a single \\n, and contains no \\r\\n",
        f".venv/bin/pytest {EVT} -q",
        f"{EVT}::test_the_canonical_form_is_bytes_and_ends_in_one_newline",
        EV,
    ),
    (
        "Sort keys so two orderings of one document hash alike",
        5,
        "canonical_json_bytes({'b':1,'a':2}) == canonical_json_bytes({'a':2,'b':1})",
        f".venv/bin/pytest {EVT} -q",
        EVT,
        EV,
    ),
    (
        "Emit non-ASCII as UTF-8 rather than as \\u escapes",
        4,
        "Cyrillic and emoji appear as their own bytes",
        f".venv/bin/pytest {EVT} -q",
        EVT,
        EV,
    ),
    (
        "Keep canonical_json() as a decode of the bytes, not a second serialiser",
        4,
        "canonical_json(d) == canonical_json_bytes(d).decode('utf-8')",
        f".venv/bin/pytest {EVT} -q",
        EVT,
        EV,
    ),
    (
        "Make the canonical form reproducible across machines",
        5,
        "the same document yields the same digest under a different locale and TZ",
        f".venv/bin/pytest {EVT} -q",
        f"{EVT}::test_the_digest_does_not_depend_on_how_the_document_was_built",
        EV,
    ),
    (
        "Route every evidence document through the canonical encoder",
        5,
        "no evidence writer calls json.dumps directly",
        "grep -rn 'json.dumps' packages/pz_agent_cli/src/pz_agent_cli/livetest",
        EVT,
        EV,
    ),
    (
        "Cap the size of any single artefact while reading it",
        4,
        "sha256_file raises above the cap rather than reading the whole file",
        ".venv/bin/pytest tests/unit/test_livetest_evidence.py -q",
        "tests/unit/test_livetest_evidence.py",
        EV,
    ),
    (
        "Refuse an unreadable artefact as a named error, not an OSError",
        4,
        "the refusal names the file and the reason",
        ".venv/bin/pytest tests/unit/test_livetest_evidence.py -q",
        "tests/unit/test_livetest_evidence.py",
        EV,
    ),
]

_E03_ATOMIC = [
    (
        "Write every hashed document through a temporary file and os.replace",
        5,
        "no reader ever observes a partially written document",
        f".venv/bin/pytest {EVT} -q",
        f"{EVT}::test_writing_is_all_or_nothing",
        EV,
    ),
    (
        "Remove the temporary file when the write fails",
        5,
        "a failed write leaves no .tmp beside the target",
        f".venv/bin/pytest {EVT} -q",
        EVT,
        EV,
    ),
    (
        "Name the temporary file per process so two writers cannot collide",
        4,
        "the temporary name contains the pid",
        f".venv/bin/pytest {EVT} -q",
        EVT,
        EV,
    ),
    (
        "Create the parent directory before writing",
        4,
        "a document under a missing directory is written, not refused",
        f".venv/bin/pytest {EVT} -q",
        EVT,
        EV,
    ),
    (
        "Publish a result file by copying bytes rather than re-serialising",
        5,
        "the published copy is byte-identical to the attempt's file",
        ".venv/bin/pytest tests/unit/test_livetest_runner.py -q",
        "tests/unit/test_livetest_runner.py",
        "packages/pz_agent_cli/src/pz_agent_cli/livetest/runner.py",
    ),
    (
        "Refuse to publish when the source bytes no longer match the recorded digest",
        6,
        "a tampered attempt file is refused at publish time",
        ".venv/bin/pytest tests/unit/test_livetest_runner.py -q",
        "tests/unit/test_livetest_runner.py",
        "packages/pz_agent_cli/src/pz_agent_cli/livetest/runner.py",
    ),
]

_E03_DIGEST = [
    (
        "Take the SHA-256 over the bytes on disk, never over the pre-write string",
        6,
        "sha256_file(path).sha256 equals the digest returned by write_document",
        f".venv/bin/pytest {EVT} -q",
        f"{EVT}::test_the_returned_digest_is_the_digest_of_the_file",
        EV,
    ),
    (
        "Read the file back and refuse if the bytes changed during the write",
        6,
        "an injected translating writer is caught rather than hashed",
        f".venv/bin/pytest {EVT} -q",
        f"{EVT}::test_a_write_that_changed_the_bytes_is_caught_rather_than_hashed",
        EV,
    ),
    (
        "Record size_bytes from the file, never from len(text)",
        5,
        "size_bytes equals path.stat().st_size",
        f".venv/bin/pytest {EVT} -q",
        EVT,
        EV,
    ),
    (
        "Prove the read-back guard has a failing mutation on every platform",
        6,
        "reverting the guard fails a test on Linux as well as Windows",
        f".venv/bin/pytest {EVT} -q",
        EVT,
        EV,
    ),
    (
        "Refuse a CRLF-edited document at verification time",
        5,
        "a document whose newlines were translated fails its digest",
        f".venv/bin/pytest {EVT} -q",
        EVT,
        EV,
    ),
    (
        "Refuse an in-place edited document at verification time",
        5,
        "any byte change fails the digest",
        f".venv/bin/pytest {EVT} -q",
        EVT,
        EV,
    ),
    (
        "Record the artefact path relative to the evidence root",
        4,
        "no artefact path in a manifest is absolute",
        ".venv/bin/pytest tests/unit/test_livetest_evidence.py -q",
        "tests/unit/test_livetest_evidence.py",
        EV,
    ),
    (
        "Validate every document against its schema before it is hashed",
        5,
        "an invalid document is refused rather than written and hashed",
        ".venv/bin/pytest tests/unit/test_livetest_evidence.py -q",
        "tests/unit/test_livetest_evidence.py",
        EV,
    ),
]

_E03_MANIFEST = [
    (
        "Record each evidence file with its digest, size and portable path",
        5,
        "every manifest row carries all three",
        ".venv/bin/pytest tests/unit/test_check_release.py -q",
        "tests/unit/test_check_release.py",
        "scripts/check_release.py",
    ),
    (
        "Refuse a manifest row whose file is missing",
        5,
        "the gate exits non-zero naming the file",
        ".venv/bin/python scripts/check_release.py",
        "tests/unit/test_check_release.py",
        "scripts/check_release.py",
    ),
    (
        "Refuse a manifest row whose digest no longer matches",
        6,
        "a tampered evidence file fails the gate",
        ".venv/bin/python scripts/check_release.py",
        "tests/unit/test_check_release.py",
        "scripts/check_release.py",
    ),
    (
        "Refuse a manifest row whose recorded size disagrees with the file",
        5,
        "size and digest are checked independently",
        ".venv/bin/python scripts/check_release.py",
        "tests/unit/test_check_release.py",
        "scripts/check_release.py",
    ),
    (
        "Verify every archive member independently rather than trusting the index",
        6,
        "the release gate hashes each member it lists",
        ".venv/bin/python scripts/check_release.py",
        "tests/unit/test_check_release.py",
        "scripts/check_release.py",
    ),
    (
        "Refuse a scenario recorded PASS with no observed postcondition",
        6,
        "succeeded requires POSTCONDITION_MET and non-empty evidence",
        ".venv/bin/python scripts/check_release.py",
        "tests/unit/test_check_release.py",
        "scripts/check_release.py",
    ),
    (
        "Distinguish NOT_RUN and BLOCKED from FAIL in the release gate",
        6,
        "a scenario that never ran does not read as a failure or as a pass",
        ".venv/bin/python scripts/check_release.py",
        "tests/unit/test_check_release.py",
        "scripts/check_release.py",
    ),
    (
        "Refuse a release whose scenarios are all NOT_RUN",
        6,
        "the gate exits non-zero when nothing has been run",
        ".venv/bin/python scripts/check_release.py",
        "tests/unit/test_check_release.py",
        "scripts/check_release.py",
    ),
]

E03 = Epic(
    id="E03",
    title="Evidence and hashing",
    subsystem="evidence",
    integration_scenario=(
        "A live-test run writes an evidence tree, the release gate verifies every "
        "digest in it from a different process, and a single flipped byte anywhere "
        "in the tree fails that gate."
    ),
    required_ci=CI,
    milestones=(
        Milestone(
            id="E03-M01",
            title="Canonical bytes",
            tasks=_tasks("E03", "M01", "evidence", "evidence", "remote", rows=_E03_CANON),
            checks=(
                Check(
                    id="E03-M01-C01",
                    statement="Two machines encoding one document produce identical bytes.",
                    command=f".venv/bin/pytest {EVT} -q",
                ),
            ),
        ),
        Milestone(
            id="E03-M02",
            title="Atomic writes",
            tasks=_tasks("E03", "M02", "evidence", "evidence", "remote", rows=_E03_ATOMIC),
            checks=(
                Check(
                    id="E03-M02-C01",
                    statement="No reader can observe a half-written evidence document.",
                    command=f".venv/bin/pytest {EVT} -q",
                ),
            ),
        ),
        Milestone(
            id="E03-M03",
            title="Digest over the bytes on disk",
            tasks=_tasks("E03", "M03", "evidence", "evidence", "remote", rows=_E03_DIGEST),
            checks=(
                Check(
                    id="E03-M03-C01",
                    statement=(
                        "A digest recorded at write time equals one taken later "
                        "by a different process on a platform that translates newlines."
                    ),
                    command="read the Windows workflow run for the evidence suites",
                ),
            ),
        ),
        Milestone(
            id="E03-M04",
            title="Manifest and verification",
            tasks=_tasks("E03", "M04", "release", "evidence", "remote", rows=_E03_MANIFEST),
            checks=(
                Check(
                    id="E03-M04-C01",
                    statement=(
                        "The release gate refuses a tampered evidence tree, and "
                        "refuses one where nothing was ever run."
                    ),
                    command=".venv/bin/pytest tests/unit/test_check_release.py -q",
                ),
            ),
        ),
    ),
)

# ---------------------------------------------------------------------------
# E04 — installer and BAT (30, band "packaging", weights 3-5)
# ---------------------------------------------------------------------------

_E04_BAT = [
    (
        "Quote every path expansion in the launcher",
        4,
        "no unquoted %VAR% or drive-qualified path in any line",
        f".venv/bin/pytest {INST} -q",
        f"{INST}::test_no_path_in_the_launcher_is_left_for_cmd_to_split_on_a_space",
        INS,
    ),
    (
        "Model cmd.exe quoting over the whole file rather than named expansions",
        4,
        "a path added later under a new name fails the same test",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Balance quotes on every line",
        3,
        "each line has an even number of double quotes",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Handle a profile path containing spaces",
        4,
        "C:/Users/John Smith works end to end",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Handle a Cyrillic profile path",
        4,
        "C:/Users/Иван works end to end",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Handle a Cyrillic profile path containing a space",
        4,
        "C:/Users/Иван Петров works end to end",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Handle a Program Files path",
        3,
        "C:/Program Files/... works end to end",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Write the config path in its native spelling for cmd.exe",
        4,
        "the BAT carries str(config_path), not a POSIX rendering",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
]

_E04_INSTALL = [
    (
        "Refuse to overwrite a file the installer did not write",
        5,
        "a foreign mod.info aborts the install and nothing is written",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Refuse to overwrite its own file that has since been edited",
        5,
        "an edited file aborts rather than being replaced",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Allow reinstalling over its own untouched files",
        4,
        "an unchanged tree reinstalls cleanly",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Preserve an existing config byte for byte",
        5,
        "the file is unchanged and the manifest records its real digest and size",
        f".venv/bin/pytest {INST} -q",
        f"{INST}::test_an_existing_config_is_left_exactly_as_the_user_wrote_it",
        INS,
    ),
    (
        "Record the preserved config's size from the file, not from a string",
        4,
        "recorded.size == config.stat().st_size",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Refuse a manifest path that could escape the target directory",
        5,
        "a traversing path is rejected before any write",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Copy only mod-shaped files from the payload",
        4,
        "unrelated files in the payload are not installed",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Never touch the game installation directory",
        5,
        "no write occurs outside the user directory",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
]

_E04_UNINSTALL = [
    (
        "Keep a mod file the user edited and name it in the report",
        5,
        "the file survives and appears in kept_modified",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Leave saves untouched",
        5,
        "every save file is byte-identical after an uninstall",
        f".venv/bin/pytest {INST} -q",
        f"{INST}::test_uninstall_leaves_saves_alone",
        INS,
    ),
    (
        "Leave the config in place and report it as kept",
        5,
        "the config exists and appears in preserved, not removed",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Leave backups and logs untouched",
        4,
        "both directories survive with their contents",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Report a file that was already gone without failing",
        4,
        "the missing file is listed, the uninstall succeeds",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Refuse to uninstall with no manifest to consult",
        5,
        "nothing is removed and the refusal says why",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Refuse a manifest in an unknown format rather than half-reading it",
        5,
        "an unrecognised format aborts before any removal",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Remove only directories the installer created, never depth-1 user directories",
        5,
        "mods/ and pz-agent/ survive an uninstall",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
]

_E04_ROUNDTRIP = [
    (
        "Run the whole installer suite on windows-latest",
        4,
        "the installer tests pass in a windows workflow run",
        "read the windows workflow run",
        INST,
        "docs/control/evidence/step-30-40/windows-suite.txt",
    ),
    (
        "Leave no residue but the config after a round trip",
        4,
        "the tree difference is exactly the state dir and the config",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Confirm the generated config passes the validator the program runs",
        4,
        "load_config accepts the generated file",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Confirm the installer and the CLI agree on the state directory name",
        3,
        "both constants are the same string",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Take the expected build from the payload rather than a literal",
        3,
        "the generated config's build matches mod.info",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
    (
        "Report a refusal on the command line without a traceback",
        3,
        "a refused install prints one line and exits non-zero",
        f".venv/bin/pytest {INST} -q",
        INST,
        INS,
    ),
]

E04 = Epic(
    id="E04",
    title="Installer and BAT",
    subsystem="installer",
    integration_scenario=(
        "A synthetic install and uninstall runs on windows-latest against a "
        "Cyrillic profile with a save, a user-edited mod file and a pre-existing "
        "config; the config survives byte for byte and the save is untouched."
    ),
    required_ci=RC,
    milestones=(
        Milestone(
            id="E04-M01",
            title="BAT rendering",
            tasks=_tasks("E04", "M01", "installer", "packaging", "remote", rows=_E04_BAT),
            checks=(
                Check(
                    id="E04-M01-C01",
                    statement=(
                        "The rendered launcher is valid for cmd.exe under every profile shape "
                        "tested."
                    ),
                    command=f".venv/bin/pytest {INST} -q",
                ),
            ),
        ),
        Milestone(
            id="E04-M02",
            title="Install",
            tasks=_tasks("E04", "M02", "installer", "packaging", "remote", rows=_E04_INSTALL),
            checks=(
                Check(
                    id="E04-M02-C01",
                    statement="An install that refuses writes nothing at all.",
                    command=f".venv/bin/pytest {INST} -q",
                ),
            ),
        ),
        Milestone(
            id="E04-M03",
            title="Uninstall",
            tasks=_tasks("E04", "M03", "installer", "packaging", "remote", rows=_E04_UNINSTALL),
            checks=(
                Check(
                    id="E04-M03-C01",
                    statement="An uninstall removes only what the manifest says it placed.",
                    command=f".venv/bin/pytest {INST} -q",
                ),
            ),
        ),
        Milestone(
            id="E04-M04",
            title="Round trip on Windows",
            tasks=_tasks("E04", "M04", "installer", "packaging", "remote", rows=_E04_ROUNDTRIP),
            checks=(
                Check(
                    id="E04-M04-C01",
                    statement="The round trip is observed on windows-latest, not inferred.",
                    command="read the windows workflow run",
                ),
            ),
        ),
    ),
)

# ---------------------------------------------------------------------------
# E05 — Local Core RPC (50, band "transport", weights 5-7)
# ---------------------------------------------------------------------------

_E05_DOC = [
    (
        "Document the envelope, its fields and their meanings",
        5,
        "docs/CORE_RPC.md describes format, protocol, id, method, params, ok, result, error",
        "read docs/CORE_RPC.md",
        "tests/contract/test_archive_documents_resolve.py",
        DOC,
    ),
    (
        "Document why the link is local-only and refuses AF_INET",
        5,
        "the document states the reason, not only the rule",
        "read docs/CORE_RPC.md",
        "",
        DOC,
    ),
    (
        "Document the size caps and why each side differs",
        5,
        "both caps appear with their rationale",
        "read docs/CORE_RPC.md",
        "",
        DOC,
    ),
    (
        "Document the token rules",
        6,
        "all six rules appear: length, per-run, own file, mode, revocation, never logged",
        "read docs/CORE_RPC.md",
        "",
        DOC,
    ),
    (
        "Publish the request JSON schema",
        5,
        "schemas/core_rpc_request.schema.json compiles as a legal JSON Schema",
        ".venv/bin/python scripts/check_schemas.py",
        "tests/contract/test_core_rpc_schema_conformance.py",
        "schemas/core_rpc_request.schema.json",
    ),
    (
        "Publish the response JSON schema",
        5,
        "schemas/core_rpc_response.schema.json compiles, and requires result on ok and error on "
        "not-ok",
        ".venv/bin/python scripts/check_schemas.py",
        "tests/contract/test_core_rpc_schema_conformance.py",
        "schemas/core_rpc_response.schema.json",
    ),
]

_E05_ENVELOPE = [
    (
        "Encode a request as compact UTF-8 JSON",
        5,
        "encode_request produces bytes that json.loads accepts",
        ".venv/bin/pytest tests/unit/test_rpc_wire.py -q",
        "tests/unit/test_rpc_wire.py",
        f"{RPC}/wire.py",
    ),
    (
        "Decode a request strictly, refusing anything that is not an object",
        5,
        "an array, a scalar and empty bytes are each refused",
        ".venv/bin/pytest tests/unit/test_rpc_wire.py -q",
        "tests/unit/test_rpc_wire.py",
        f"{RPC}/wire.py",
    ),
    (
        "Require a format marker on both directions",
        5,
        "a foreign envelope is refused even when otherwise well formed",
        "pytest tests/contract/test_core_rpc_schema_conformance.py -q",
        "tests/contract/test_core_rpc_schema_conformance.py",
        f"{RPC}/wire.py",
    ),
    (
        "Refuse a pickle stream in either direction",
        7,
        "pickle.dumps of any payload raises rather than loading",
        ".venv/bin/pytest tests/unit/test_rpc_wire.py -q",
        "tests/unit/test_rpc_wire.py::TestPickleIsNotAcceptedAnywhere",
        f"{RPC}/wire.py",
    ),
    (
        "Refuse a pickle whose __reduce__ would execute on load",
        7,
        "the adversarial payload is refused, not merely a benign one",
        ".venv/bin/pytest tests/unit/test_rpc_wire.py -q",
        "tests/unit/test_rpc_wire.py::TestPickleIsNotAcceptedAnywhere::test_a_pickle_that_would_execute_on_load_is_refused",
        f"{RPC}/wire.py",
    ),
    (
        "Cap the request at 64 KiB before parsing",
        6,
        "an oversized request raises TooLarge without json.loads running",
        ".venv/bin/pytest tests/unit/test_rpc_wire.py -q",
        "tests/unit/test_rpc_wire.py::TestCaps",
        f"{RPC}/wire.py",
    ),
    (
        "Cap the response at 4 MiB before parsing",
        6,
        "an oversized response raises TooLarge on the read side",
        ".venv/bin/pytest tests/unit/test_rpc_wire.py -q",
        "tests/unit/test_rpc_wire.py::TestCaps",
        f"{RPC}/wire.py",
    ),
    (
        "Substitute an error for an oversized answer rather than sending nothing",
        6,
        "the client receives TOO_LARGE with the request id, not a timeout",
        ".venv/bin/pytest tests/unit/test_rpc_wire.py -q",
        "tests/unit/test_rpc_wire.py::TestCaps::test_an_oversized_answer_becomes_an_error_rather_than_silence",
        f"{RPC}/wire.py",
    ),
    (
        "Bound the id and method string lengths",
        5,
        "a 5000-character id is refused",
        ".venv/bin/pytest tests/unit/test_rpc_wire.py -q",
        "tests/unit/test_rpc_wire.py",
        f"{RPC}/wire.py",
    ),
    (
        "Refuse a peer whose protocol major differs",
        6,
        "protocol 2.0 raises ProtocolMismatch; 1.7 is accepted",
        ".venv/bin/pytest tests/unit/test_rpc_wire.py -q",
        "tests/unit/test_rpc_wire.py::TestProtocolVersion",
        f"{RPC}/wire.py",
    ),
    (
        "Require the ok flag rather than inferring it from an absent error",
        6,
        "a response without ok is refused, not defaulted either way",
        ".venv/bin/pytest tests/unit/test_rpc_wire.py -q",
        "tests/unit/test_rpc_wire.py::TestMalformed",
        f"{RPC}/wire.py",
    ),
    (
        "Keep every refusal message free of the payload it rejected",
        7,
        "a secret-shaped value in a malformed payload is absent from str(exc)",
        ".venv/bin/pytest tests/unit/test_rpc_wire.py -q",
        "tests/unit/test_rpc_wire.py::TestARefusalNeverQuotesThePayload",
        f"{RPC}/wire.py",
    ),
]

_E05_TOKEN = [
    (
        "Generate 32 bytes from the CSPRNG",
        7,
        "len(token) >= 32 and 32 successive tokens are distinct",
        ".venv/bin/pytest tests/unit/test_rpc_token_and_descriptor.py -q",
        "tests/unit/test_rpc_token_and_descriptor.py::TestTheToken",
        f"{RPC}/token.py",
    ),
    (
        "Issue a new token on every run",
        7,
        "two issues produce different tokens and the second is the stored one",
        ".venv/bin/pytest tests/unit/test_rpc_token_and_descriptor.py -q",
        "tests/unit/test_rpc_token_and_descriptor.py::TestTheToken::test_every_run_gets_a_new_one",
        f"{RPC}/token.py",
    ),
    (
        "Store the token in its own file, never in the descriptor",
        7,
        "the descriptor bytes contain neither the token nor its hex",
        ".venv/bin/pytest tests/unit/test_rpc_token_and_descriptor.py -q",
        "tests/unit/test_rpc_token_and_descriptor.py::TestTheToken::test_the_token_lives_apart_from_the_descriptor",
        f"{RPC}/token.py",
    ),
    (
        "Create the file at mode 0600 by descriptor rather than chmod after",
        7,
        "the mode is set at creation; there is no world-readable window",
        ".venv/bin/pytest tests/unit/test_rpc_token_and_descriptor.py -q",
        "tests/unit/test_rpc_token_and_descriptor.py::TestTheToken::test_the_file_is_not_readable_by_anyone_else",
        f"{RPC}/token.py",
    ),
    (
        "Revoke the token on a clean shutdown",
        7,
        "revoke removes the file and reports whether there was one",
        ".venv/bin/pytest tests/unit/test_rpc_token_and_descriptor.py -q",
        "tests/unit/test_rpc_token_and_descriptor.py::TestTheToken::test_a_clean_shutdown_removes_it",
        f"{RPC}/token.py",
    ),
    (
        "Never raise while revoking, because it runs during shutdown",
        7,
        "revoking a missing file or an unreachable directory returns False",
        ".venv/bin/pytest tests/unit/test_rpc_token_and_descriptor.py -q",
        "tests/unit/test_rpc_token_and_descriptor.py",
        f"{RPC}/token.py",
    ),
    (
        "Refuse a token file shorter than 32 bytes",
        8,
        "a truncated file raises rather than authenticating",
        ".venv/bin/pytest tests/unit/test_rpc_token_and_descriptor.py -q",
        "tests/unit/test_rpc_token_and_descriptor.py",
        f"{RPC}/token.py",
    ),
    (
        "Keep the token out of every exception message",
        7,
        "no TokenError contains the token or any prefix of it",
        ".venv/bin/pytest tests/unit/test_rpc_token_and_descriptor.py -q",
        "tests/unit/test_rpc_token_and_descriptor.py::TestTheToken::test_no_error_message_contains_the_token",
        f"{RPC}/token.py",
    ),
]

_E05_DESCRIPTOR = [
    (
        "Write the descriptor to <state-dir>/runtime/core-rpc.json",
        5,
        "the path is exactly that, and is where the MCP process looks",
        ".venv/bin/pytest tests/unit/test_rpc_token_and_descriptor.py -q",
        "tests/unit/test_rpc_token_and_descriptor.py::TestWriting",
        f"{RPC}/descriptor.py",
    ),
    (
        "Write it atomically so a reader never sees half of it",
        5,
        "no .tmp is left and the file always parses",
        ".venv/bin/pytest tests/unit/test_rpc_token_and_descriptor.py -q",
        "tests/unit/test_rpc_token_and_descriptor.py::TestWriting",
        f"{RPC}/descriptor.py",
    ),
    (
        "Name the token file rather than its path",
        6,
        "the descriptor carries a bare filename with no separator",
        ".venv/bin/pytest tests/unit/test_rpc_token_and_descriptor.py -q",
        "tests/unit/test_rpc_token_and_descriptor.py::TestWriting",
        f"{RPC}/descriptor.py",
    ),
    (
        "Refuse a descriptor whose format marker is not ours",
        5,
        "a foreign file is refused rather than parsed",
        ".venv/bin/pytest tests/unit/test_rpc_token_and_descriptor.py -q",
        "tests/unit/test_rpc_token_and_descriptor.py::TestFindingAServer",
        f"{RPC}/descriptor.py",
    ),
    (
        "Refuse a descriptor whose protocol major differs",
        6,
        "the refusal explains that two installs are present",
        ".venv/bin/pytest tests/unit/test_rpc_token_and_descriptor.py -q",
        "tests/unit/test_rpc_token_and_descriptor.py::TestFindingAServer",
        f"{RPC}/descriptor.py",
    ),
    (
        "Refuse a descriptor naming a process that is gone",
        7,
        "a reaped pid raises StaleDescriptor rather than being dialled",
        ".venv/bin/pytest tests/unit/test_rpc_token_and_descriptor.py -q",
        "tests/unit/test_rpc_token_and_descriptor.py::TestFindingAServer::test_a_descriptor_naming_a_dead_process_is_refused",
        f"{RPC}/descriptor.py",
    ),
    (
        "Refuse a descriptor whose token file has gone",
        6,
        "the shutdown window is not connectable",
        ".venv/bin/pytest tests/unit/test_rpc_token_and_descriptor.py -q",
        "tests/unit/test_rpc_token_and_descriptor.py::TestFindingAServer",
        f"{RPC}/descriptor.py",
    ),
    (
        "Refuse an AF_INET family outright",
        7,
        "a network address in a descriptor is never dialled",
        ".venv/bin/pytest tests/unit/test_rpc_token_and_descriptor.py -q",
        "tests/unit/test_rpc_token_and_descriptor.py::TestFindingAServer::test_an_af_inet_address_is_refused_because_this_link_is_local",
        f"{RPC}/descriptor.py",
    ),
]

_E05_TRANSPORT = [
    (
        "Bind AF_PIPE on Windows",
        7,
        "a server binds and answers on a named pipe in a windows-latest run",
        "read the windows workflow run",
        "tests/unit/test_rpc_transport.py",
        f"{RPC}/transport.py",
    ),
    (
        "Bind AF_UNIX elsewhere",
        6,
        "a server binds and answers on a Unix socket on Linux",
        ".venv/bin/pytest tests/unit/test_rpc_transport.py -q",
        "tests/unit/test_rpc_transport.py::TestTheLinkWorks",
        f"{RPC}/transport.py",
    ),
    (
        "Give a Windows address a random component so two accounts cannot collide",
        6,
        "two calls to new_address on Windows differ",
        ".venv/bin/pytest tests/unit/test_rpc_transport.py -q",
        "tests/unit/test_rpc_transport.py::TestAddresses",
        f"{RPC}/transport.py",
    ),
    (
        "Refuse a POSIX address that exceeds sun_path",
        5,
        "the refusal names the length and the remedy, never the path",
        ".venv/bin/pytest tests/unit/test_rpc_transport.py -q",
        "tests/unit/test_rpc_transport.py::TestAddresses::test_a_posix_address_that_will_not_bind_says_so_before_it_tries",
        f"{RPC}/transport.py",
    ),
    (
        "Use send_bytes and recv_bytes only, never send and recv",
        7,
        "poisoning Connection.send and Connection.recv does not break a real call",
        ".venv/bin/pytest tests/unit/test_rpc_transport.py -q",
        "tests/unit/test_rpc_transport.py::TestNothingIsPickled",
        f"{RPC}/transport.py",
    ),
    (
        "Refuse a connection presenting the wrong token",
        7,
        "the call raises RpcUnavailable and the server keeps serving",
        ".venv/bin/pytest tests/unit/test_rpc_transport.py -q",
        "tests/unit/test_rpc_transport.py::TestAuthentication",
        f"{RPC}/transport.py",
    ),
    (
        "Keep the key out of the refusal message",
        7,
        "neither the wrong key nor the real one appears in str(exc)",
        ".venv/bin/pytest tests/unit/test_rpc_transport.py -q",
        "tests/unit/test_rpc_transport.py::TestAuthentication::test_the_refusal_does_not_quote_the_key",
        f"{RPC}/transport.py",
    ),
    (
        "Answer a malformed request rather than dropping the connection",
        6,
        "a client sending nonsense receives MALFORMED",
        ".venv/bin/pytest tests/unit/test_rpc_transport.py -q",
        "tests/unit/test_rpc_transport.py::TestFailureIsAnAnswer",
        f"{RPC}/transport.py",
    ),
    (
        "Turn a handler exception into CORE_REFUSED rather than a crash",
        6,
        "the server survives and the next call succeeds",
        ".venv/bin/pytest tests/unit/test_rpc_transport.py -q",
        "tests/unit/test_rpc_transport.py::TestFailureIsAnAnswer",
        f"{RPC}/transport.py",
    ),
    (
        "Give up on a server that does not answer within the deadline",
        6,
        "a stalled handler produces RpcUnavailable well before the grace period",
        ".venv/bin/pytest tests/unit/test_rpc_transport.py -q",
        "tests/unit/test_rpc_transport.py::TestTheDeadline",
        f"{RPC}/transport.py",
    ),
]

_E05_SERVER = [
    (
        "Unblock a thread parked in accept when the server closes",
        7,
        "serve_forever returns after close; the thread is not alive",
        ".venv/bin/pytest tests/unit/test_rpc_transport.py -q",
        "tests/unit/test_rpc_transport.py::TestShutdown::test_close_ends_the_serving_thread",
        f"{RPC}/transport.py",
    ),
    (
        "Make close idempotent",
        5,
        "closing twice raises nothing",
        ".venv/bin/pytest tests/unit/test_rpc_transport.py -q",
        "tests/unit/test_rpc_transport.py::TestShutdown",
        f"{RPC}/transport.py",
    ),
    (
        "Remove the POSIX socket file on close",
        5,
        "a second server binds the same runtime directory afterwards",
        ".venv/bin/pytest tests/unit/test_rpc_transport.py -q",
        "tests/unit/test_rpc_transport.py::TestShutdown",
        f"{RPC}/transport.py",
    ),
    (
        "Start the RPC server inside the sidecar's lifecycle",
        7,
        "pz-agent start brings up the server, writes the descriptor and issues a token",
        ".venv/bin/pytest tests/unit/test_cli_supervisor.py -q",
        "tests/unit/test_rpc_sidecar_lifecycle.py",
        "packages/pz_agent_cli/src/pz_agent_cli/supervisor.py",
    ),
    (
        "Revoke the token and remove the descriptor on sidecar shutdown",
        7,
        "after pz-agent stop, neither file exists",
        ".venv/bin/pytest tests/unit/test_rpc_sidecar_lifecycle.py -q",
        "tests/unit/test_rpc_sidecar_lifecycle.py",
        "packages/pz_agent_cli/src/pz_agent_cli/supervisor.py",
    ),
    (
        "Keep the RPC server off the panic-stop path so a stop never waits on it",
        7,
        "a panic stop completes while the RPC server is blocked",
        ".venv/bin/pytest tests/unit/test_rpc_sidecar_lifecycle.py -q",
        "tests/unit/test_rpc_sidecar_lifecycle.py",
        "packages/pz_agent_cli/src/pz_agent_cli/supervisor.py",
    ),
]

E05 = Epic(
    id="E05",
    title="Local Core RPC",
    subsystem="rpc",
    integration_scenario=(
        "The sidecar starts, publishes a descriptor and a token, a second process "
        "reads both, calls every method, and after the sidecar stops that second "
        "process is told the sidecar is gone rather than hanging."
    ),
    required_ci=RC,
    milestones=(
        Milestone(
            id="E05-M01",
            title="Protocol document and schemas",
            tasks=_tasks("E05", "M01", "rpc", "transport", "remote", rows=_E05_DOC),
            checks=(
                Check(
                    id="E05-M01-C01",
                    statement="The schemas and the encoder agree in both directions.",
                    command="pytest tests/contract/test_core_rpc_schema_conformance.py -q",
                ),
            ),
        ),
        Milestone(
            id="E05-M02",
            title="The envelope",
            tasks=_tasks("E05", "M02", "rpc", "transport", "remote", rows=_E05_ENVELOPE),
            checks=(
                Check(
                    id="E05-M02-C01",
                    statement="No code path in the rpc package unpickles.",
                    command=(
                        "grep -rn 'pickle\\|\\.recv()\\|\\.send(' "
                        "packages/pz_agent_core/src/pz_agent_core/rpc"
                    ),
                ),
            ),
        ),
        Milestone(
            id="E05-M03",
            title="The token",
            tasks=_tasks("E05", "M03", "rpc", "security", "remote", rows=_E05_TOKEN),
            checks=(
                Check(
                    id="E05-M03-C01",
                    statement="The token appears in no log, no bundle and no exception.",
                    command=".venv/bin/pytest tests/unit/test_rpc_token_and_descriptor.py -q",
                ),
            ),
        ),
        Milestone(
            id="E05-M04",
            title="The descriptor",
            tasks=_tasks("E05", "M04", "rpc", "transport", "remote", rows=_E05_DESCRIPTOR),
            checks=(
                Check(
                    id="E05-M04-C01",
                    statement="A stale descriptor never reaches another process.",
                    command=".venv/bin/pytest tests/unit/test_rpc_token_and_descriptor.py -q",
                ),
            ),
        ),
        Milestone(
            id="E05-M05",
            title="The transport",
            tasks=_tasks("E05", "M05", "rpc", "transport", "remote", rows=_E05_TRANSPORT),
            checks=(
                Check(
                    id="E05-M05-C01",
                    statement="The named pipe is bound for real on windows-latest.",
                    command="read the windows workflow run",
                ),
            ),
        ),
        Milestone(
            id="E05-M06",
            title="The server in the sidecar",
            tasks=_tasks("E05", "M06", "rpc", "transport", "remote", rows=_E05_SERVER),
            checks=(
                Check(
                    id="E05-M06-C01",
                    statement=(
                        "Starting and stopping the sidecar leaves no descriptor and no token "
                        "behind."
                    ),
                    command=".venv/bin/pytest tests/unit/test_rpc_sidecar_lifecycle.py -q",
                ),
            ),
        ),
    ),
)
