"""Every ``pz-agent`` command line a document prints, through the real parser.

Two shipped documents were telling users to run commands that do not exist:

- ``SECURITY.md`` — the page a vulnerability reporter lands on — said to check a
  support bundle with ``pz-agent logs --redact --verify`` before attaching it to
  a public issue. There is no ``--redact`` flag. The command exits 2 with an
  argparse usage message, so the one gate between a reporter and an unredacted
  archive on a public issue was an instruction that could not be followed.
- ``PRIVACY.md`` said ``pz-agent memory --forget`` clears the memory store.
  There is no ``memory`` command; it is ``remember forget``, which appeared in
  no document at all.

Both are the same shape as nine defects before them, and both would have been
caught the first time anyone ran what the page said. That is all this file does,
for every document at once, so the next one is caught before it ships rather
than by a user following instructions that fail.

Parsed rather than run: executing ``uninstall-mod`` to check its spelling would
be a poor trade. The parser is the part that decides whether a user's first
attempt ends in a usage error.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from pz_agent_cli import voice as cli_voice
from pz_agent_cli.app import build_parser
from pz_agent_cli.config import SCHEMA, SUPPORTED_VOICE_ADAPTERS
from pz_agent_core.platform.paths import portable_relative_path
from pz_agent_mcp.__main__ import build_parser as mcp_parser
from pz_agent_voice import plan_port
from pz_agent_voice.config import DEFAULT_VOICE_CONFIG, MAX_PLAN_REAL_SECONDS, MAX_PLAN_STEPS
from pz_agent_voice.intent import STOP_WORDS
from pz_agent_voice.messages import MAX_TEXT_CHARS, MAX_TRANSCRIPT_CHARS, IntentRefusal, VoiceGoal
from pz_agent_voice.teamon import (
    BRIDGE_PROTOCOL_VERSION,
    BRIDGE_UNAVAILABLE_PHRASE,
    MAX_LINE_BYTES,
    MAX_MESSAGE_BYTES,
    MESSAGE_DIRECTIONS,
    BridgeConfig,
    BridgeDirection,
    BridgeFaultCode,
    OutcomeStatus,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

#: Where a user-facing document may live. ``docs/blueprint/`` is deliberately
#: excluded: it is the specification the product was built from, it describes an
#: intended surface rather than this build's, and holding it to the parser would
#: be asserting that the design document is a user manual.
DOC_ROOTS: Final = (REPO_ROOT, REPO_ROOT / "docs", REPO_ROOT / "configs", REPO_ROOT / "packaging")

#: Records rather than manuals. A changelog entry that closes "PRIVACY.md said
#: `pz-agent memory --forget`" has to be able to quote the command that did not
#: work, and a report describing a defect is not an instruction to reproduce it.
#: Kept to three files by name, so the exemption cannot quietly grow to cover a
#: page somebody actually follows.
RECORDS: Final = frozenset({"CHANGELOG.md", "FINAL_IMPLEMENTATION_REPORT.md", "PROGRESS.md"})

#: ``pz-agent doctor --json`` — with or without a Windows launcher prefix.
#: ``pz-agent-mcp`` is a different program with a different parser and is not
#: matched; the negative lookahead sees to the hyphen.
#: Anchored at the start of its fragment, so a mention inside another command
#: — ``git tag -m "pz-agent 0.1.0"`` — or inside a directory tree is not read as
#: an instruction to run something.
_INVOCATION: Final = re.compile(
    r"^\s*(?:\.venv[\\/]Scripts[\\/])?pz-agent(?!-)\s+([^\n`\"'|;]+)",
)

#: Only what a reader would copy: a fenced block, or an inline code span. Prose
#: mentions ("`pz-agent` runs on the user's own machine", "see `pz-agent
#: replay`") name the program rather than instruct, and holding them to the
#: parser would make this file noise that gets switched off.
_INLINE_CODE: Final = re.compile(r"`([^`]+)`")
_FENCE: Final = re.compile(r"^\s*```")

#: Tokens a document writes for the user to replace. Each becomes something the
#: parser will accept, since the placeholder itself is not the thing under test.
_PLACEHOLDERS: Final[dict[str, str]] = {
    "<backup-id>": "backup-0001",
    "<trace>": "trace.jsonl",
    "<state>": "C:\\state",
    "<id>": "S04_MOVE",
    "<path>": "C:\\path",
    "<name>": "name",
    "<phrase>": "стоп",
}

#: Prose that follows a command on the same line and is not an argument: a
#: trailing comment, an em dash, or the aligned description these documents put
#: beside a command in a table or a listing.
_PROSE: Final = re.compile(r"(?:\s+(?:#|—|--\s|\(|\[)|\s{2,}).*$")


def _clean(raw: str) -> list[str]:
    """One matched tail, as the argument list a shell would build."""
    line = _PROSE.sub("", raw).strip().rstrip(".,;:")
    for token, replacement in _PLACEHOLDERS.items():
        line = line.replace(token, replacement)
    return [piece for piece in line.split() if piece]


def _documents() -> list[Path]:
    seen: dict[Path, None] = {}
    for root in DOC_ROOTS:
        for path in sorted(root.rglob("*.md") if root != REPO_ROOT else root.glob("*.md")):
            if "blueprint" in path.parts or ".venv" in path.parts:
                continue
            if path.name in RECORDS:
                continue
            seen[path] = None
    return list(seen)


def _commands_on(line: str, *, fenced: bool) -> list[str]:
    """The command-shaped fragments of one line, chained shells split apart."""
    sources = [line] if fenced else _INLINE_CODE.findall(line)
    found: list[str] = []
    for source in sources:
        for fragment in source.split("&&"):
            found.extend(_INVOCATION.findall(fragment))
    return found


def _invocations() -> list[tuple[str, str, list[str]]]:
    built: list[tuple[str, str, list[str]]] = []
    for path in _documents():
        fenced = False
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _FENCE.match(line):
                fenced = not fenced
                continue
            for raw in _commands_on(line, fenced=fenced):
                argv = _clean(raw)
                if not argv or argv[0].startswith("-"):
                    continue
                if not fenced and len(argv) == 1:
                    # "`pz-agent replay` has something to replay" names the
                    # command; it does not tell anyone to type it bare. A block
                    # is different — what is in one is what a reader copies.
                    continue
                built.append(
                    (f"{portable_relative_path(path, REPO_ROOT)}:{number}", raw.strip(), argv)
                )
    assert built, "no invocations were extracted; the pattern has stopped matching"
    return built


_CASES: Final = _invocations()


def test_the_documents_are_being_read_at_all() -> None:
    """A pattern that silently stopped matching would make every case below pass."""
    files = {case[0].split(":")[0] for case in _CASES}

    assert len(_CASES) > 40, f"only {len(_CASES)} invocations found across {len(files)} file(s)"
    # The pages a user actually follows, named so the RECORDS exemption above
    # cannot grow into one of them without this failing.
    for manual in ("docs/QUICKSTART.md", "docs/TROUBLESHOOTING.md", "SECURITY.md"):
        assert manual in files, f"{manual} is no longer being checked"


@pytest.mark.parametrize(
    ("where", "raw", "argv"),
    _CASES,
    ids=[f"{where} {raw[:36]}" for where, raw, _ in _CASES],
)
def test_the_parser_accepts_what_the_document_tells_a_user_to_type(
    where: str, raw: str, argv: list[str]
) -> None:
    parser = build_parser()
    try:
        parser.parse_args(argv)
    except SystemExit:
        pytest.fail(f"{where} says to run `pz-agent {raw}`, and the parser rejects it")


# ---------------------------------------------------------------------------
# the second executable, which has its own parser
# ---------------------------------------------------------------------------

#: ``pz-agent-mcp --describe`` and friends. A separate program with a separate
#: and much smaller parser — ``--version`` and ``--describe`` and nothing else —
#: so a document naming a flag for it is the same defect with a different
#: argument table. ``.exe`` and ``.spec`` are packaging file names rather than
#: invocations and are excluded by requiring whitespace after the program name.
_MCP_INVOCATION: Final = re.compile(r"^\s*pz-agent-mcp(?!\S)\s*([^\n`\"'|;]*)")


def _mcp_invocations() -> list[tuple[str, str, list[str]]]:
    built: list[tuple[str, str, list[str]]] = []
    for path in _documents():
        fenced = False
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _FENCE.match(line):
                fenced = not fenced
                continue
            sources = [line] if fenced else _INLINE_CODE.findall(line)
            for source in sources:
                for raw in _MCP_INVOCATION.findall(source):
                    argv = _clean(raw)
                    if not fenced and not argv:
                        # A bare mention of the program by name is not an
                        # instruction; in a block it is one, and is checked.
                        continue
                    built.append(
                        (f"{portable_relative_path(path, REPO_ROOT)}:{number}", raw.strip(), argv)
                    )
    return built


_MCP_CASES: Final = _mcp_invocations()


def test_the_server_invocations_are_being_read_at_all() -> None:
    """``configs/mcp/README.md`` is a first-contact document and prints several."""
    assert _MCP_CASES, "no pz-agent-mcp invocations found; the pattern has stopped matching"
    assert any("configs/mcp/README.md" in where for where, _, _ in _MCP_CASES)


@pytest.mark.parametrize(
    ("where", "raw", "argv"),
    _MCP_CASES,
    ids=[f"{where} {raw[:24] or '<bare>'}" for where, raw, _ in _MCP_CASES],
)
def test_the_server_parser_accepts_what_the_document_names(
    where: str, raw: str, argv: list[str]
) -> None:
    try:
        mcp_parser().parse_args(argv)
    except SystemExit:
        pytest.fail(f"{where} says to run `pz-agent-mcp {raw}`, and its parser rejects it")


# ---------------------------------------------------------------------------
# docs/VOICE.md, beyond its command lines
# ---------------------------------------------------------------------------
#
# The scan above already runs VOICE.md's `voice ...` invocations through the
# parser. That page also prints names and numbers — configuration keys, closed
# refusal vocabularies, the bridge contract's constants — and each of those is
# an instruction in the same sense a command line is: a user copies the key, a
# bridge implementer branches on the code, and a value the runtime does not
# hold is the `--redact` defect wearing a number. Pinned here are exactly the
# claims whose ground truth is importable. Behaviour the page describes ("stop
# is tested first", the queue's eviction rules) is the voice suites' job, and
# prose stays prose — this must not become a document linter.


class TestVoiceMdNamesTheRuntime:
    """The importable claims of ``docs/VOICE.md``, value for value."""

    def test_the_voice_config_keys_exist_in_the_validator_with_the_stated_choices(self) -> None:
        """The ``[voice]`` sample: three keys, the adapter set, the variable name.

        The validator refuses unknown keys as typos, so a key the page prints
        must be in the schema or following the page is an error. The sample's
        ``api_key_env`` value is pinned too, because the page is where a user
        learns which environment variable to export.
        """
        assert set(SCHEMA["voice"]) == {"enabled", "adapter", "api_key_env"}
        assert SUPPORTED_VOICE_ADAPTERS == ("teamon", "none")
        assert SCHEMA["voice"]["adapter"].choices == SUPPORTED_VOICE_ADAPTERS
        assert SCHEMA["voice"]["api_key_env"].default == "PZ_AGENT_TEAMON_API_KEY"

    def test_the_bounds_table_is_the_shipped_default_config(self) -> None:
        """The table in § Configuration, against ``DEFAULT_VOICE_CONFIG``.

        These are the loop's own limits, published so a user can reason about
        what the companion may do; a table that drifted from the constants
        would have them reasoning about a different program.
        """
        assert DEFAULT_VOICE_CONFIG.wake_words == frozenset({"агент", "ассистент", "agent"})
        assert DEFAULT_VOICE_CONFIG.wake_ttl_ms == 30_000
        assert DEFAULT_VOICE_CONFIG.require_wake_word is True
        assert DEFAULT_VOICE_CONFIG.min_confidence == 0.6
        assert DEFAULT_VOICE_CONFIG.max_clarifications == 1
        assert DEFAULT_VOICE_CONFIG.plan_max_steps == MAX_PLAN_STEPS == 8
        assert DEFAULT_VOICE_CONFIG.plan_max_real_seconds == 120
        assert MAX_PLAN_REAL_SECONDS == 600
        assert MAX_TRANSCRIPT_CHARS == 400
        assert MAX_TEXT_CHARS == 240

    def test_the_refusal_vocabularies_are_the_documented_closed_sets(self) -> None:
        """Every code the page lists, and no other.

        These sets are what the page calls closed, so equality is the honest
        assertion in both directions: a member missing from the page is a
        refusal nobody can look up, and a member missing from the runtime is
        the page telling an implementer to handle a code that never comes.
        """
        assert {member.value for member in IntentRefusal} == {
            "not_a_goal",
            "ambiguous_goal",
            "skill_not_named",
            "parameter_out_of_range",
            "parameter_not_accepted",
            "capability_unavailable",
            "internal",
        }
        assert {member.value for member in VoiceGoal} == {"eat", "drink", "read", "resume"}
        assert {member.value for member in OutcomeStatus} == {
            "ended",
            "failed",
            "refused",
            "cancelled",
        }
        assert {member.value for member in BridgeFaultCode} == {
            "audio_device",
            "recogniser",
            "synthesis",
            "network",
            "internal",
            "unknown",
        }
        # "There is one stop vocabulary, `intent.STOP_WORDS`". Its members are
        # deliberately not restated in the page, so only existence is a claim.
        assert STOP_WORDS, "the one stop vocabulary must exist and must not be empty"

    def test_the_bridge_contract_constants_are_what_the_page_prints(self) -> None:
        """The wire numbers an implementer copies into another program.

        The bridge program is written from this page, in another language,
        against another SDK — so a number here that disagrees with the runtime
        is a bridge that framed correctly against a contract this build does
        not speak.
        """
        assert BRIDGE_PROTOCOL_VERSION == "1.0"
        assert MAX_MESSAGE_BYTES == MAX_LINE_BYTES == 16_384

        defaults = BridgeConfig(command=("a-bridge",))
        assert (defaults.startup_timeout, defaults.reply_timeout) == (5.0, 5.0)
        assert (defaults.speech_timeout, defaults.stop_timeout) == (30.0, 1.0)
        assert (defaults.max_restarts, defaults.max_pending) == (3, 64)

        # The closed message set: eight types, each travelling the stated way.
        to_bridge = {"hello", "speak", "interrupt", "goal"}
        directions = {kind.value: direction for kind, direction in MESSAGE_DIRECTIONS.items()}
        assert set(directions) == to_bridge | {"ready", "transcript", "outcome", "error"}
        sent = {name for name, way in directions.items() if way is BridgeDirection.TO_BRIDGE}
        assert sent == to_bridge

        assert BRIDGE_UNAVAILABLE_PHRASE == "Голосовой мост не работает."

    def test_the_spoken_goal_route_is_the_one_the_page_describes(self) -> None:
        """VOICE.md's own verification grep, made of imports instead.

        The page says a spoken goal now travels ``services_over_core_rpc`` and
        that ``UnroutedPlanPort`` is gone, and prints a grep to prove it. The
        same facts from the import system: the CLI wires the builder the page
        names, the retired refusal port is nowhere to be imported, and the
        deadlines the page quotes — including ``voice check``'s two-second
        probe — are the shipped values.
        """
        # Via the module namespace, not attribute access: the CLI imports the
        # builder for its own wiring rather than re-exporting it, and mypy's
        # no-implicit-reexport rule is right to hold the distinction.
        assert vars(cli_voice).get("services_over_core_rpc") is plan_port.services_over_core_rpc
        for retired in ("UnroutedPlanPort", "GoalUnroutable"):
            assert not hasattr(plan_port, retired), f"{retired} has come back"
            assert not hasattr(cli_voice, retired), f"{retired} has come back"
        assert plan_port.VOICE_PLAN_DEADLINE_SECONDS == 3.0
        assert plan_port.MAX_VOICE_PLAN_DEADLINE_SECONDS == 10.0
        assert cli_voice.GOAL_CHANNEL_PROBE_SECONDS == 2.0
