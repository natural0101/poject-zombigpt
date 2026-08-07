"""The absolute rule: no transcript text leaves the voice process.

An adversarial utterance is fed to :func:`~pz_agent_voice.intent.resolve_goal`
— the shipped resolver, named the survivor by ``docs/control/BLOCKERS.md``
R-007 — and then followed to each of the four places text could escape to: the
goal handed to the core, a diagnostic log, a support bundle, and an RPC call on
the wire. None of them is allowed to contain a byte of it.

Two things make this a test rather than a gesture:

* **Every sink is the real one.** A real :class:`~pz_agent_core.diagnostics.DiagnosticLog`
  writing real files, a real :class:`~pz_agent_core.diagnostics.BundleBuilder`
  producing a real zip that is read back member by member, and real
  :func:`~pz_agent_core.rpc.encode_request` bytes. Each is built with the *null*
  redactor, so nothing downstream is covering for this module: if a transcript
  reached them it would appear verbatim.
* **The scan is proved able to fail.** :func:`test_negative_control_the_scan_finds_a_leak`
  pushes the transcript into all four sinks deliberately and asserts every one
  of them is caught. Without it, "no needle found" would be indistinguishable
  from "the scanner does not work".

The strongest assertion here is not the needle hunt, which can only find what it
thought to look for. It is
:func:`test_every_string_in_a_resolution_is_a_constant_this_repository_wrote`,
which collects *every* string reachable from a resolution and requires each one
to be a module constant, an enum value, or a minted identifier — so an unknown
string fails whether or not anyone guessed at its contents.

The second half of the file: the shipped wiring
-----------------------------------------------

Everything above resolves a phrase and inspects the object that came back. That
cannot see a leak that happens *between* the resolver and the disk, so from
"The canary, and every surface a live session could carry it out on" onwards the
subject is the wiring a user actually starts:

* a real Local Core RPC link, with a listener that keeps the bytes it was sent
  rather than the request it could decode from them, driven through the shipped
  :func:`~pz_agent_voice.plan_port.core_plan_port`;
* a real ``pz-agent voice run``, against a recogniser that says the canary,
  writing its real log, its real record and its real panic latch;
* the real support bundle those files end up in, read back member by member and
  re-scanned by the product's own :func:`~pz_agent_core.diagnostics.verify_bundle`;
* a real bridge child process over real pipes, which records every byte it is
  sent, dies mid-session and is relaunched.

Two rules keep that half honest. The canary is chosen so that **no redaction
rule rewrites it** — asserted, not assumed, because a needle the redactor eats
would turn every empty scan below into a no-op. And every "nothing leaked"
assertion is paired with one that the thing which *should* have happened did:
the goal token reached the wire, the intents reached the log, the voice log
reached the bundle, the stop reached the latch. A session that did nothing
leaks nothing.

Where a claim is about *coverage* rather than about one payload — E10-M03-T010 —
the surfaces are enumerated and the enumeration is itself checked: every message
type declared ``TO_BRIDGE`` must appear in the list this file scans, every public
method of the two ports must appear in the set it names, and every file the
session wrote anywhere on the machine is walked rather than a chosen few.
"""

from __future__ import annotations

# Several assertions below compare against sentences from the closed Russian
# phrase table, and the canary itself is Russian. They are short enough that
# every character in them has a Latin lookalike, so the confusable-character
# rule reads them as mistyped ASCII; taking its suggestion would leave the tests
# asserting against strings this build never says.
# ruff: noqa: RUF001
import contextlib
import importlib
import json
import os
import re
import sys
import threading
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass, fields, is_dataclass
from multiprocessing.connection import Listener
from pathlib import Path, PureWindowsPath
from types import ModuleType
from typing import Any, Final

import pytest

from pz_agent_cli.config import ADAPTER_TEAMON
from pz_agent_cli.context import EXIT_OK, Workspace, resolve_workspace
from pz_agent_cli.support import build_support_bundle
from pz_agent_cli.voice import VOICE_RECORD_NAME, VoiceRefused, run_voice_run
from pz_agent_core.diagnostics import (
    BundleBuilder,
    DiagnosticLog,
    LogLevel,
    read_records,
    verify_bundle,
)
from pz_agent_core.diagnostics.redaction import null_redactor
from pz_agent_core.goals import (
    GOAL_SPECS,
    PARAM_NAMES,
    GoalAdmission,
    GoalKind,
    GoalRecord,
    GoalRequest,
    GoalState,
    TrainableSkill,
    key_digest,
)
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.planner.providers import DEFAULT_TEAMON_KEY_ENV
from pz_agent_core.protocol import ActionStatus, SessionMode
from pz_agent_core.rpc import (
    MAX_REQUEST_BYTES,
    ErrorCode,
    RpcDescriptor,
    RpcRequest,
    RpcResponse,
    decode_request,
    encode_request,
    encode_response,
    issue_token,
    runtime_dir,
    write_descriptor,
)
from pz_agent_core.rpc.descriptor import FAMILY_PIPE, FAMILY_UNIX
from pz_agent_core.rpc.transport import local_family, new_address
from pz_agent_mcp.ports import (
    GoalCancellation,
    GoalChannelStatus,
    GoalPort,
    PlanPort,
    PlanRecord,
    SessionPort,
)
from pz_agent_mcp.remote.codec.goals import (
    decode_goal_request,
    encode_goal_admission,
    encode_goal_cancellation,
    encode_goal_channel_status,
)
from pz_agent_mcp.remote.codec.plans import encode_plan_current, encode_plan_record
from pz_agent_mcp.remote.methods import Method
from pz_agent_voice import intent, phrases
from pz_agent_voice.adapters.teamon import TeamONTranscript
from pz_agent_voice.intent import ALL_VOICE_CAPABILITIES, GoalResolution, classify, resolve_goal
from pz_agent_voice.messages import IntentRefusal, VoiceGoal, VoiceInput, VoiceIntent
from pz_agent_voice.plan_port import VOICE_PLAN_DEADLINE_SECONDS, core_goal_port, core_plan_port
from pz_agent_voice.ports import VoiceServices
from pz_agent_voice.session import VoiceSession
from pz_agent_voice.teamon import (
    BRIDGE_PROTOCOL_VERSION,
    MESSAGE_DIRECTIONS,
    BridgeConfig,
    BridgeDirection,
    BridgeMessage,
    BridgeMessageType,
    BridgeReason,
    BridgeReport,
    JsonlBridge,
    encode,
    goal_message,
    hello_message,
    interrupt_message,
    speak_message,
)
from tests.fixtures.cli_worlds import CliWorld, make_world
from tests.fixtures.ipc_builders import FakeClock
from tests.fixtures.mcp_doubles import CountingIds, Doubles
from tests.fixtures.voice_doubles import FakeTeamONClient

# ---------------------------------------------------------------------------
# the adversarial material
# ---------------------------------------------------------------------------

#: Built as a Windows path explicitly. Windows is the shipping platform, these
#: tests run on Linux, and a leak of a backslash-separated drive path is exactly
#: the shape that a POSIX-only fixture would never produce.
WINDOWS_PATH = str(PureWindowsPath("C:/Users/ivan/Zomboid/Saves/pz_agent/token.txt"))

#: An ASCII credential, which survives a JSON encoder's ``ensure_ascii`` intact.
SECRET = "sk-live-4f3a2b1c9d8e7f6a"

#: A Cyrillic credential, which does *not* — it becomes ``\uXXXX`` escapes in a
#: bundle member. The scan below decodes JSON before looking, so both are found.
CYRILLIC_SECRET = "парольИванова"

NEEDLES: tuple[str, ...] = (
    WINDOWS_PATH,
    "C:\\Users",
    "ivan",
    "Zomboid",
    "token.txt",
    SECRET,
    "sk-live",
    "4f3a2b1c9d8e7f6a",
    CYRILLIC_SECRET,
    "Иванова",
)

#: One phrase per outcome the resolver can produce, each carrying the path and
#: the credential. A rule that held only for refusals would be no rule at all.
ADVERSARIAL: tuple[tuple[str, str], ...] = (
    (
        "goal",
        f"агент поешь путь {WINDOWS_PATH} пароль {SECRET} {CYRILLIC_SECRET}",
    ),
    (
        "goal_with_params",
        f"прокачай механику до 7 ключ {SECRET} файл {WINDOWS_PATH} {CYRILLIC_SECRET}",
    ),
    ("unmapped", f"{WINDOWS_PATH} {SECRET} {CYRILLIC_SECRET}"),
    ("ambiguous", f"почитай попей {SECRET} {WINDOWS_PATH} {CYRILLIC_SECRET}"),
    ("out_of_range", f"прокачай механику до 70 {SECRET} {WINDOWS_PATH} {CYRILLIC_SECRET}"),
    ("stop", f"стоп {SECRET} {WINDOWS_PATH} {CYRILLIC_SECRET}"),
    (
        "too_long",
        f"поешь {WINDOWS_PATH} {SECRET} {CYRILLIC_SECRET} " + "и еще раз " * 40,
    ),
)


def _haystacks(data: bytes) -> tuple[str, ...]:
    """Every readable form of *data*: the raw text, and any JSON strings in it.

    Both are needed. ``json.dumps(..., ensure_ascii=True)`` — which the bundle
    builder uses — turns Cyrillic into escapes that a raw substring scan would
    walk straight past, and a leak nobody can grep for is still a leak.
    """
    text = data.decode("utf-8", errors="replace")
    forms = [text]
    for line in text.splitlines():
        try:
            document = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        forms.extend(_strings(document))
    # A body that is not JSON — the human-readable log line, the raw refusal
    # text — is already covered by the plain text form above; there is nothing
    # to add and nothing lost by the failed parse.
    with contextlib.suppress(json.JSONDecodeError, ValueError):
        forms.extend(_strings(json.loads(text)))
    return tuple(forms)


def _strings(value: Any) -> list[str]:
    """Every string anywhere inside a decoded JSON document."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for key, item in value.items() for s in (*_strings(key), *_strings(item))]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


def _found(data: bytes, needles: Sequence[str]) -> tuple[str, ...]:
    """Which of *needles* appear in any readable form of *data*.

    Case-folded as well as literal, because
    :func:`~pz_agent_voice.messages.normalise_transcript` folds a transcript
    before any matcher reads it — so a leak of what the matcher saw is a
    lowercase leak, and a case-sensitive scan would walk past it.
    """
    forms = _haystacks(data)
    return tuple(
        needle
        for needle in needles
        if any(needle in form or needle.casefold() in form.casefold() for form in forms)
    )


def _leaks(data: bytes) -> tuple[str, ...]:
    """Which needles *data* contains, if any."""
    return _found(data, NEEDLES)


def resolve(utterance: str) -> GoalResolution:
    """The shipped resolver, as production wires it: every capability observed."""
    return resolve_goal(VoiceInput(transcript=utterance, at_ms=0), available=ALL_VOICE_CAPABILITIES)


def _spoken_refusal(resolution: GoalResolution) -> str:
    """The sentence the companion would say about a refused resolution.

    Built by :func:`~pz_agent_voice.phrases.intent_refusal` from closed tables
    and the names the resolution minted — the same route the session takes.
    """
    assert resolution.refusal is not None
    return phrases.intent_refusal(
        resolution.refusal,
        parameter=resolution.parameter,
        capability=resolution.capability,
    )


def to_goal_request(resolution: GoalResolution, *, sequence: int) -> GoalRequest:
    """A submission built the way :class:`~pz_agent_voice.session.VoiceSession` builds one.

    The idempotency key — the goal channel's one caller-supplied string — is
    minted from a counter, exactly as the session's ``IdFactory`` mints it, so
    no transcript byte has a route into it.
    """
    assert resolution.resolved and resolution.kind is not None
    return GoalRequest(
        kind=resolution.kind,
        idempotency_key=f"voice-{sequence:06d}",
        params=resolution.params,
    )


# ---------------------------------------------------------------------------
# sink one: the goal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("label", "utterance"), ADVERSARIAL)
def test_no_transcript_reaches_the_resolution(label: str, utterance: str) -> None:
    resolution = resolve(utterance)
    assert _leaks(repr(resolution).encode("utf-8")) == (), label


@pytest.mark.parametrize(("label", "utterance"), ADVERSARIAL)
def test_no_transcript_reaches_the_goal_request(label: str, utterance: str) -> None:
    """Every phrase is exercised: the ones that resolve, and the ones that cannot.

    Nothing is skipped here. A phrase that does not become a goal is asserted to
    produce no request at all, which is the same rule stated at its other end.
    """
    resolution = resolve(utterance)
    if not resolution.resolved:
        # A refusal carries no kind — GoalResolution's constructor enforces it —
        # so there is nothing the session could submit. That is the same rule
        # stated at its other end.
        assert resolution.kind is None
        return
    request = to_goal_request(resolution, sequence=7)
    assert _leaks(repr(request).encode("utf-8")) == ()
    assert re.fullmatch(r"voice-\d{6}", request.idempotency_key)


def test_the_goal_phrases_actually_produce_a_goal() -> None:
    """Guards the branch above: a rule proved only on refusals proves nothing.

    ``too_long`` is in the list: the resolver truncates to the transcript bound
    and matches the surviving prefix, so the goal word at the front of that
    phrase still resolves — and the needles behind it still must go nowhere.
    """
    produced = [label for label, u in ADVERSARIAL if resolve(u).resolved]
    assert produced == ["goal", "goal_with_params", "too_long"]


def test_the_parameters_that_survive_are_numbers_inside_declared_ranges() -> None:
    """The one thing an utterance may contribute is a range-checked number.

    Said plainly because it is the boundary of the privacy rule rather than an
    exception to it: "до 7" becomes the integer 7 and nothing else about the
    sentence survives. No string the speaker chose crosses.
    """
    resolution = resolve(ADVERSARIAL[1][1])
    assert resolution.kind is GoalKind.TRAIN_SKILL
    assert resolution.params.skill is TrainableSkill.MECHANICS
    assert resolution.params.target_level == 7
    assert resolution.params.pages is None
    assert resolution.params.satisfy_to is None


def test_every_string_in_a_resolution_is_a_constant_this_repository_wrote() -> None:
    """The structural proof, which does not depend on guessing the needle.

    Every string reachable from a resolution — for every adversarial phrase and
    for a large sweep of ordinary ones — must be a member of the closed set this
    repository authored. An arbitrary string fails here even if nobody thought
    to look for it.
    """
    allowed = {
        *(kind.value for kind in GoalKind),
        *(skill.value for skill in TrainableSkill),
        *(member.value for member in VoiceIntent),
        *(code.value for code in IntentRefusal),
        *ALL_VOICE_CAPABILITIES,
        *PARAM_NAMES,
        "",
    }
    phrases = [utterance for _, utterance in ADVERSARIAL] + [
        "поешь",
        "почитай 20 страниц",
        f"почитай {WINDOWS_PATH} страниц",
        f"{SECRET}",
        f"прокачай {CYRILLIC_SECRET} до 3",
        "стоп",
    ]
    for utterance in phrases:
        resolution = resolve(utterance)
        for found in _resolution_strings(resolution):
            assert found in allowed, (utterance, found)


def _resolution_strings(value: Any, *, depth: int = 0) -> list[str]:
    """Every string reachable from *value*, walked by field rather than by name.

    Generic on purpose. A version that listed the fields it knew about would
    keep passing the day somebody adds ``heard: str`` to the resolution "just
    for debugging", which is precisely the edit this rule exists to stop.
    """
    assert depth < 8, "a resolution should not be this deep"
    if isinstance(value, str):
        return [str(value)]
    if isinstance(value, bool | int | float) or value is None:
        return []
    if is_dataclass(value) and not isinstance(value, type):
        return [
            found
            for item in fields(value)
            for found in _resolution_strings(getattr(value, item.name), depth=depth + 1)
        ]
    if isinstance(value, list | tuple | set | frozenset):
        return [found for item in value for found in _resolution_strings(item, depth=depth + 1)]
    raise AssertionError(f"a resolution carried an unexpected {type(value).__name__}")


def test_the_field_walk_sees_a_field_nobody_told_it_about() -> None:
    """The walk is generic, so an added field is covered without an edit here."""

    @dataclass(frozen=True)
    class Sneaky:
        outcome: str
        heard: str

    assert _resolution_strings(Sneaky(outcome="goal", heard=SECRET)) == ["goal", SECRET]


# ---------------------------------------------------------------------------
# sink two: a log
# ---------------------------------------------------------------------------


def _log_a_resolution(directory: Path, resolution: GoalResolution) -> DiagnosticLog:
    """Write the record a caller would write about *resolution*.

    Deliberately verbose: it logs every public field, and the sentence the
    companion would speak, because the rule has to hold for a caller that
    reports everything, not only for a careful one. The log is built with the
    default null redactor so nothing is scrubbed for us.
    """
    log = DiagnosticLog(directory, name="voice")
    log.log(
        LogLevel.INFO,
        "voice.resolved",
        intent=resolution.intent.value,
        kind=None if resolution.kind is None else resolution.kind.value,
        skill=None if resolution.params.skill is None else resolution.params.skill.value,
        target_level=resolution.params.target_level,
        pages=resolution.params.pages,
        satisfy_to=resolution.params.satisfy_to,
        refusal=None if resolution.refusal is None else resolution.refusal.value,
        parameter=resolution.parameter,
        capability=resolution.capability,
        candidates=[kind.value for kind in resolution.candidates],
        message=None if resolution.refusal is None else _spoken_refusal(resolution),
    )
    return log


@pytest.mark.parametrize(("label", "utterance"), ADVERSARIAL)
def test_no_transcript_reaches_a_log(label: str, utterance: str, tmp_path: Path) -> None:
    log = _log_a_resolution(tmp_path, resolve(utterance))
    written = [path for path in log.files() if path.exists()]
    assert written, "the log wrote nothing, so this test proved nothing"
    for path in written:
        assert _leaks(path.read_bytes()) == (), (label, path.name)


def test_the_module_cannot_write_anywhere_on_its_own() -> None:
    """Nothing in the resolver can emit, open or spawn. Checked in the source.

    A structural claim rather than a behavioural one, because the behavioural
    version — "call resolve and see that nothing was written" — passes for a
    module that writes to a file the test does not know about.
    """
    source = Path(intent.__file__).read_text(encoding="utf-8")
    for forbidden in ("import logging", "print(", "open(", "subprocess", "socket", "sys.std"):
        assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# sink three: a support bundle
# ---------------------------------------------------------------------------


def _bundle_a_resolution(destination: Path, resolution: GoalResolution) -> Path:
    """Build a real archive holding everything a bundle would carry about it."""
    builder = BundleBuilder()
    builder.add_json(
        "voice/resolution.json",
        {
            "intent": resolution.intent.value,
            "kind": None if resolution.kind is None else resolution.kind.value,
            "refusal": None if resolution.refusal is None else resolution.refusal.value,
            "parameter": resolution.parameter,
            "capability": resolution.capability,
            "message": None if resolution.refusal is None else _spoken_refusal(resolution),
        },
    )
    if resolution.refusal is not None:
        builder.add_text("voice/refusal.txt", _spoken_refusal(resolution) + "\n")
    builder.build(destination)
    return destination


@pytest.mark.parametrize(("label", "utterance"), ADVERSARIAL)
def test_no_transcript_reaches_a_support_bundle(label: str, utterance: str, tmp_path: Path) -> None:
    archive = _bundle_a_resolution(tmp_path / "bundle.zip", resolve(utterance))
    with zipfile.ZipFile(archive) as opened:
        names = opened.namelist()
        assert names, "the bundle held nothing, so this test proved nothing"
        for name in names:
            assert _leaks(opened.read(name)) == (), (label, name)
            assert _leaks(name.encode("utf-8")) == (), (label, name)


# ---------------------------------------------------------------------------
# sink four: an RPC call
# ---------------------------------------------------------------------------


def _rpc_bytes(request: GoalRequest) -> bytes:
    """The call an adapter would put on the Local Core RPC link."""
    params: dict[str, Any] = {
        "kind": request.kind.value,
        "idempotency_key": request.idempotency_key,
        "params": {
            name: value
            for name, value in (
                ("skill", None if request.params.skill is None else request.params.skill.value),
                ("target_level", request.params.target_level),
                ("satisfy_to", request.params.satisfy_to),
                ("pages", request.params.pages),
            )
            if value is not None
        },
    }
    return encode_request(RpcRequest(id="voice-0001", method="goals.submit", params=params))


@pytest.mark.parametrize(("label", "utterance"), ADVERSARIAL)
def test_no_transcript_reaches_an_rpc_call(label: str, utterance: str) -> None:
    resolution = resolve(utterance)
    if not resolution.resolved:
        # A non-goal never reaches the link at all: it carries no kind, so
        # there is no request to encode, and asserting that is the honest form
        # of "nothing leaked".
        assert resolution.kind is None
        return
    assert _leaks(_rpc_bytes(to_goal_request(resolution, sequence=1))) == (), label


def test_a_refusal_crossing_the_link_carries_only_its_constant() -> None:
    """Even when a refusal *is* reported over RPC, it carries no transcript."""
    resolution = resolve(ADVERSARIAL[2][1])
    assert resolution.refusal is not None
    message = _spoken_refusal(resolution)
    encoded = encode_request(
        RpcRequest(
            id="voice-0002",
            method="voice.refused",
            params={"refusal": resolution.refusal.value, "message": message},
        )
    )
    assert _leaks(encoded) == ()
    # The whole sentence is the closed table's constant for this refusal.
    assert message == phrases.REFUSAL_SPEECH[IntentRefusal.NOT_A_GOAL]


# ---------------------------------------------------------------------------
# the negative control
# ---------------------------------------------------------------------------


def test_negative_control_the_scan_finds_a_leak(tmp_path: Path) -> None:
    """Push the transcript into all four sinks on purpose; each must be caught.

    This is what makes every empty ``_leaks(...) == ()`` above meaningful. If
    this test ever passes with fewer than four detections, the scan has stopped
    working and the other tests in this file are decoration.
    """
    _, utterance = ADVERSARIAL[0]
    caught: list[str] = []

    if _leaks(repr({"transcript": utterance}).encode("utf-8")):
        caught.append("goal")

    log = DiagnosticLog(tmp_path / "logs", name="leaky")
    log.log(LogLevel.INFO, "voice.leaked", transcript=utterance)
    if any(_leaks(path.read_bytes()) for path in log.files() if path.exists()):
        caught.append("log")

    builder = BundleBuilder()
    builder.add_json("voice/leak.json", {"transcript": utterance})
    archive = tmp_path / "leaky.zip"
    builder.build(archive)
    with zipfile.ZipFile(archive) as opened:
        if any(_leaks(opened.read(name)) for name in opened.namelist()):
            caught.append("bundle")

    encoded = encode_request(
        RpcRequest(id="leak", method="goals.submit", params={"transcript": utterance})
    )
    if _leaks(encoded):
        caught.append("rpc")

    assert caught == ["goal", "log", "bundle", "rpc"]


def test_negative_control_the_cyrillic_needle_survives_json_escaping() -> None:
    """The escape-aware half of the scan, on its own.

    ``add_json`` writes ``ensure_ascii=True``; a raw substring scan for a
    Cyrillic secret would find nothing in the bytes it produced. This asserts
    that the decode step is what catches it, not luck.
    """
    escaped = json.dumps({"secret": CYRILLIC_SECRET}, ensure_ascii=True).encode("utf-8")
    assert CYRILLIC_SECRET.encode("utf-8") not in escaped
    assert CYRILLIC_SECRET in _leaks(escaped)


# ===========================================================================
# The canary, and every surface a live session could carry it out on.
#
# Everything above resolves a phrase and inspects the result. Everything below
# runs the *shipped wiring* — a real Local Core RPC link with a listener that
# keeps the bytes it was sent, the real ``pz-agent voice run`` writing real
# files, the real support bundle, and a real bridge child process over real
# pipes — and then searches what came out. A resolver that is clean and a
# session that leaks would pass the first half of this file and fail here.
# ===========================================================================


#: A nonsense token. No redaction rule matches it, so it survives every writer
#: in this repository and is therefore the needle that cannot be scrubbed away
#: on the way to a file. The path and the key below are redacted in places, and
#: :func:`test_the_canary_outlives_redaction_so_a_leak_could_not_be_scrubbed`
#: is what keeps that from quietly turning this whole section into a no-op.
CANARY_MARK: Final = "КАНАРЕЙКА7"

#: A Windows path, built as one explicitly: Windows is the shipping platform,
#: these tests run on Linux, and a backslash-separated drive path is exactly the
#: shape a POSIX-only fixture would never produce.
CANARY_PATH: Final = str(PureWindowsPath(f"C:/Users/Иван Петров/Zomboid/{CANARY_MARK}.txt"))

#: Credential-shaped, and deliberately shaped so that no rule in
#: :data:`~pz_agent_core.diagnostics.redaction.SECRET_RULES` rewrites it: a key
#: the redactor happens to know is a key the redactor would cover for.
CANARY_KEY: Final = "sk-canary-ZOMBIGPT-770131"

CANARY_NEEDLES: Final[tuple[str, ...]] = (
    CANARY_MARK,
    CANARY_KEY,
    CANARY_PATH,
    "sk-canary",
    "770131",
    "Иван Петров",
)

#: One canary-bearing phrase per branch of
#: :meth:`~pz_agent_voice.session.VoiceSession.handle`. Each carries a real
#: command word, so a classifier that works still finds the intent in it — which
#: is the only way to prove that the *rest* of the sentence went nowhere.
CANARY_PHRASES: Final[tuple[tuple[str, str], ...]] = (
    ("goal", f"агент поешь файл {CANARY_PATH} ключ {CANARY_KEY}"),
    ("status", f"агент статус файл {CANARY_PATH} ключ {CANARY_KEY}"),
    ("ambiguous", f"агент поешь попей {CANARY_PATH} {CANARY_KEY}"),
    ("wake", f"агент бармаглот {CANARY_PATH} {CANARY_KEY}"),
    ("stop", f"стоп {CANARY_PATH} {CANARY_KEY}"),
)

#: Every wait in this section. A test that hangs is not a test that fails.
PATIENCE: Final = 10.0

#: How many times :meth:`RawCore._serve` will shrug off an accept it could not
#: complete before it gives up. One per unblocking poke plus a wide margin; the
#: point is that the number exists at all.
MAX_ACCEPT_REFUSALS: Final = 64

#: Longest line the bridge child will read from its pipe, and the longest
#: transcript file it will load. Both are comfortably past
#: :data:`~pz_agent_voice.teamon.MAX_MESSAGE_BYTES`, so no legal message is
#: truncated — they are here so that neither read is unbounded.
CHILD_READ_CAP: Final = 65_536

#: How long a bounded poll waits between looks, and how many looks it takes.
_POLL_SECONDS: Final = 0.01
_POLL_TRIES: Final = int(PATIENCE / _POLL_SECONDS)


def _canary_leaks(data: bytes) -> tuple[str, ...]:
    """Which canary needles *data* carries, if any."""
    return _found(data, CANARY_NEEDLES)


def _until(condition: Callable[[], bool]) -> bool:
    """Poll *condition* until it holds, bounded by :data:`PATIENCE`."""
    for _ in range(_POLL_TRIES):
        if condition():
            return True
        time.sleep(_POLL_SECONDS)
    return condition()


# ---------------------------------------------------------------------------
# the controls: this scan can fail, and nothing downstream scrubs for it
# ---------------------------------------------------------------------------


def test_the_canary_scan_finds_the_canary_wherever_it_is_written() -> None:
    """Every phrase, in every encoding a sink here writes, is caught."""
    for label, phrase in CANARY_PHRASES:
        assert _canary_leaks(phrase.encode("utf-8")), label
        assert _canary_leaks(json.dumps({"heard": phrase}, ensure_ascii=True).encode()), label
        assert _canary_leaks(json.dumps({"heard": phrase}, ensure_ascii=False).encode()), label
        assert _canary_leaks(phrase.casefold().encode("utf-8")), label


def test_the_canary_outlives_redaction_so_a_leak_could_not_be_scrubbed() -> None:
    """The floor under every "no canary in the file" assertion below.

    The log, the record and the bundle all run through a redactor on the way to
    disk. If the redactor struck the canary out, an empty scan would say nothing
    about whether the transcript reached the file — so the two needles the
    assertions actually rest on are required to survive a redactor pass here.
    """
    redacted = null_redactor().text(CANARY_PHRASES[0][1]).encode("utf-8")
    survivors = _canary_leaks(redacted)
    assert CANARY_MARK in survivors
    assert CANARY_KEY in survivors
    # And the half that does *not* survive, stated rather than assumed: the
    # profile directory is struck out, which is why it is not leaned on.
    assert "Иван Петров" not in survivors


def test_the_canary_phrases_still_classify_so_the_intent_really_travels() -> None:
    """A phrase nothing recognises would prove nothing by not leaking."""
    resolved = {
        label: classify(VoiceInput(transcript=phrase, at_ms=0)).intent
        for label, phrase in CANARY_PHRASES
    }
    assert resolved == {
        "goal": VoiceIntent.GOAL,
        "status": VoiceIntent.STATUS,
        "ambiguous": VoiceIntent.AMBIGUOUS,
        "wake": VoiceIntent.WAKE,
        "stop": VoiceIntent.STOP,
    }
    assert classify(VoiceInput(transcript=CANARY_PHRASES[0][1], at_ms=0)).goal is VoiceGoal.EAT


# ---------------------------------------------------------------------------
# E09-M02-T007: the Local Core RPC link, searched as bytes
# ---------------------------------------------------------------------------


class RawCore:
    """A listener that keeps the exact bytes of every request it is sent.

    Deliberately not :class:`~pz_agent_core.rpc.transport.RpcServer`: that
    decodes the line before a handler ever sees it, and a decoded request is the
    wrong thing to search — a leak in a key the decoder drops would vanish
    between the socket and the assertion. What is kept here is what arrived on
    the wire, byte for byte, which is the claim being made.
    """

    def __init__(self, state_dir: Path) -> None:
        runtime = runtime_dir(state_dir)
        runtime.mkdir(parents=True, exist_ok=True)
        self._family = local_family()
        key = issue_token(runtime)
        self._listener = Listener(
            new_address(runtime),
            family="AF_PIPE" if self._family == FAMILY_PIPE else "AF_UNIX",
            authkey=key,
        )
        self.address = str(self._listener.address)
        write_descriptor(
            state_dir,
            RpcDescriptor(address=self.address, family=self._family, pid=os.getpid()),
        )
        self.payloads: list[bytes] = []
        self._stopping = threading.Event()
        self._refusals = 0
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def methods(self) -> list[str]:
        """The method of every call this core was sent, in order."""
        return [decode_request(payload).method for payload in self.payloads]

    def _serve(self) -> None:
        while not self._stopping.is_set():
            try:
                connection = self._listener.accept()
            except (OSError, EOFError):
                return
            except Exception:
                # `multiprocessing` raises AuthenticationError, which is not an
                # OSError. The bare socket `close` uses to unblock this accept
                # arrives as one, and it is not an event worth ending on. It is
                # counted rather than merely skipped: an accept that refuses
                # without blocking would otherwise spin this thread forever, and
                # a retry with no limit is the thing this repository calls a bug.
                self._refusals += 1
                if self._refusals > MAX_ACCEPT_REFUSALS:
                    return
                continue
            with closing(connection):
                if self._stopping.is_set():
                    return
                try:
                    data = connection.recv_bytes(maxlength=MAX_REQUEST_BYTES)
                except (OSError, EOFError):
                    continue
                self.payloads.append(data)
                with suppress(OSError, EOFError):
                    connection.send_bytes(encode_response(_answer(data)))

    def close(self) -> None:
        """Stop listening, bounded, and leave nothing behind."""
        self._stopping.set()
        self._wake()
        with suppress(OSError):
            self._listener.close()
        self._thread.join(timeout=PATIENCE)
        if self._family == FAMILY_UNIX:
            with suppress(OSError):
                Path(self.address).unlink()

    def _wake(self) -> None:
        """Unblock the thread parked in ``accept``; see ``RpcServer._wake``."""
        if self._family == FAMILY_UNIX:
            import socket  # noqa: PLC0415  (only this path needs it)

            with suppress(OSError), socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                sock.connect(self.address)
            return
        with suppress(OSError), open(self.address, "rb"):
            pass


def _answer(data: bytes) -> RpcResponse:
    """A well-formed answer to whichever plan or goal method was asked.

    Answering rather than refusing matters here: a refusal short-circuits the
    caller, so a port whose method always failed would put one request on the
    wire and never the ones that follow it — and this file scans what is on the
    wire. The bodies are the smallest well-formed ones, because nothing here
    asserts about them; the assertions are all about the bytes going the other
    way.
    """
    request = decode_request(data)
    if request.method == Method.PLAN_EXECUTE:
        record = PlanRecord(plan_id="plan-1", status=ActionStatus.ACCEPTED, step_index=0)
        return RpcResponse(id=request.id, ok=True, result=encode_plan_record(record))
    if request.method == Method.PLAN_CURRENT:
        return RpcResponse(id=request.id, ok=True, result=encode_plan_current(None))
    if request.method == Method.GOAL_SUBMIT:
        return RpcResponse(id=request.id, ok=True, result=encode_goal_admission(_admitted(request)))
    if request.method == Method.GOAL_STATUS:
        return RpcResponse(
            id=request.id, ok=True, result=encode_goal_channel_status(GoalChannelStatus())
        )
    if request.method == Method.GOAL_CANCEL:
        return RpcResponse(
            id=request.id,
            ok=True,
            result=encode_goal_cancellation(GoalCancellation(goal=None, requested=False)),
        )
    return RpcResponse(
        id=request.id,
        ok=False,
        error_code=ErrorCode.UNKNOWN_METHOD,
        error_message="this core answers plan and goal methods only",
    )


def _admitted(request: RpcRequest) -> GoalAdmission:
    """A goal admitted, built from what was actually asked for.

    Echoing the request's own kind rather than a constant, so a companion that
    submitted the wrong kind would be visible in the record it gets back rather
    than masked by a fixture that always says the same thing.
    """
    submitted = decode_goal_request(request.params)
    return GoalAdmission(
        goal=GoalRecord(
            goal_id="goal-0001",
            kind=submitted.kind,
            params=submitted.params,
            budget=submitted.budget or GOAL_SPECS[submitted.kind].budget,
            key_digest=key_digest(submitted.idempotency_key),
            sequence=1,
            state=GoalState.PENDING,
            submitted_at_ms=0,
        )
    )


@pytest.fixture
def core(tmp_path: Path) -> Iterator[RawCore]:
    """A capturing core, always taken down again.

    The state directory is one character long because a POSIX socket path is
    bounded by ``sun_path``, and a fixture that spelled out the test's name
    would fail to bind instead of testing anything.
    """
    started = RawCore(tmp_path / "s")
    try:
        yield started
    finally:
        started.close()


def test_no_rpc_call_from_the_voice_package_carries_transcript_text(
    core: RawCore, tmp_path: Path
) -> None:
    """E09-M02-T007. Every phrase, through the real link, searched as bytes.

    The ports are the shipped ones —
    :func:`~pz_agent_voice.plan_port.core_goal_port` and
    :func:`~pz_agent_voice.plan_port.core_plan_port` over the real remote
    client — resolving a real descriptor and a real token, so what the
    assertions read is what the voice package actually put on the socket.

    Both are wired on purpose. A spoken goal travels on ``goal.submit`` now
    (``docs/control/DECISIONS.md``); this test used to wire the plan port alone,
    and after the companion moved it went on passing while scanning a route
    nothing used and missing the one every goal takes. A privacy floor that
    watches the wrong wire is worse than none, because it reads as coverage.
    """
    goals = core_goal_port(tmp_path / "s", deadline=VOICE_PLAN_DEADLINE_SECONDS)
    plans = core_plan_port(tmp_path / "s", deadline=VOICE_PLAN_DEADLINE_SECONDS)
    doubles = Doubles()
    session = VoiceSession(
        VoiceServices(session=doubles.session, plans=plans, goals=goals),
        clock=FakeClock(),
        ids=CountingIds("voice"),
    )

    for index, (_, phrase) in enumerate(CANARY_PHRASES):
        session.handle(VoiceInput(transcript=phrase, at_ms=index, final=True, confidence=1.0))
    # The other halves of both ports. No phrase reaches these on its own, and a
    # method nothing exercised is a method nothing scanned.
    assert plans.current() is None
    goals.status()

    assert core.payloads, "nothing reached the link, so this test proved nothing"
    assert Method.GOAL_SUBMIT in core.methods(), (
        "no goal was submitted, so the route every spoken goal takes went unscanned"
    )
    assert Method.GOAL_STATUS in core.methods()
    assert Method.PLAN_CURRENT in core.methods()
    # The kind did cross — the channel works, and it is one enum member wide.
    # Written as the literal bytes rather than derived from the encoder.
    assert any(b'"kind":"satisfy_hunger"' in payload for payload in core.payloads)
    for payload in core.payloads:
        assert _canary_leaks(payload) == (), decode_request(payload).method


def test_every_method_the_voice_package_can_reach_over_the_link_is_enumerated() -> None:
    """The completeness half: a new port method fails here until it is scanned.

    The sets are written out rather than derived from the protocols, so adding
    ``PlanPort.cancel`` breaks this test instead of silently adding an outbound
    call nothing in this file searches.
    """
    assert _protocol_methods(PlanPort) == {"execute", "current"}
    assert _protocol_methods(GoalPort) == {"submit", "status", "cancel"}
    assert _protocol_methods(SessionPort) == {"status", "arm", "disarm", "stop"}


def _protocol_methods(protocol: type) -> frozenset[str]:
    """Every public callable a port protocol declares."""
    return frozenset(
        name
        for name in dir(protocol)
        if not name.startswith("_") and callable(getattr(protocol, name, None))
    )


# ---------------------------------------------------------------------------
# E09-M02-T008, E09-M02-T009, E09-M03-T004, E10-M03-T010:
# the shipped command, its log, its bundle and everything it wrote
# ---------------------------------------------------------------------------


def _a_voice_world(tmp_path: Path) -> tuple[CliWorld, Workspace]:
    """A fake machine on which ``pz-agent voice run`` will actually start.

    The exchange directory is created because the panic latch lives in it: on a
    machine where the mod has never run there is nowhere to write the latch, the
    stop is reported as failed, and the sweep below would then be scanning a
    session in which the one path that writes a file never ran.
    """
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    assert workspace.ipc_root is not None
    IpcLayout(workspace.ipc_root).ensure()
    workspace.state_dir.mkdir(parents=True, exist_ok=True)
    workspace.config_path.write_text(
        f'[voice]\nenabled = true\nadapter = "{ADAPTER_TEAMON}"\n', encoding="utf-8"
    )
    world.ctx.env[DEFAULT_TEAMON_KEY_ENV] = "teamon-key-for-tests"  # type: ignore[index]
    return world, workspace


def _canary_client() -> FakeTeamONClient:
    """A recogniser that says every canary phrase and then ends the session."""
    return FakeTeamONClient(
        [
            TeamONTranscript(text=phrase, at_ms=1000 + index, final=True, confidence=1.0)
            for index, (_, phrase) in enumerate(CANARY_PHRASES)
        ]
    )


def _run_a_canary_session(tmp_path: Path) -> tuple[CliWorld, Workspace, FakeTeamONClient]:
    """Run the real command against a recogniser that speaks the canary."""
    world, workspace = _a_voice_world(tmp_path)
    client = _canary_client()
    assert run_voice_run(world.ctx, as_json=False, client=client) == EXIT_OK
    return world, workspace, client


def _log_records(workspace: Workspace) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    return read_records(workspace.logs_dir / "pz-agent.jsonl")


def test_an_intent_and_its_outcome_reach_a_file_under_logs(tmp_path: Path) -> None:
    """E09-M03-T004. The half that must be there, before the half that must not.

    ``docs/LOCAL_DEBUG_MAP.md`` sends an operator to ``logs/`` for both voice
    symptoms it lists, so a companion that wrote nothing would be a failure of
    its own — and an empty file is also how "no transcript leaked" is trivially
    satisfied. So the intents are required to be present, by name, before
    anything below asserts what is absent.
    """
    _, workspace, _ = _run_a_canary_session(tmp_path)

    records, problems = _log_records(workspace)
    assert problems == ()
    events = [str(record["event"]) for record in records]
    assert "voice.start" in events
    assert "voice.finished" in events

    turns = [record for record in records if record["event"] == "voice.turn"]
    assert [turn["fields"]["intent"] for turn in turns] == [
        "goal",
        "status",
        "ambiguous",
        "ambiguous",
        "stop",
    ]
    # The outcome, not only the intent: every turn says what it caused, and the
    # stop says that the lever was pulled.
    assert all(str(turn["fields"]["detail"]).strip() for turn in turns)
    assert [turn["fields"]["stopped"] for turn in turns] == [False, False, False, False, True]


def test_no_voice_log_carries_the_canary(tmp_path: Path) -> None:
    """E09-M02-T008. Intents and outcomes only, in both streams the log writes."""
    _, workspace, _ = _run_a_canary_session(tmp_path)

    written = sorted(path for path in workspace.logs_dir.iterdir() if path.is_file())
    assert [path.name for path in written] == ["pz-agent.jsonl", "pz-agent.log"]
    for path in written:
        assert _canary_leaks(path.read_bytes()) == (), path.name


def test_no_support_bundle_member_carries_the_canary(tmp_path: Path) -> None:
    """E09-M02-T009. A real archive, read back member by member.

    Built by :func:`~pz_agent_cli.support.build_support_bundle`, which is what
    ``pz-agent support bundle`` runs, so the members are the ones a user would
    attach to a public issue rather than a set this test chose.
    """
    world, workspace, _ = _run_a_canary_session(tmp_path)
    destination = workspace.bundle_dir / "support.zip"

    bundle = build_support_bundle(world.ctx, workspace, destination)

    # The voice log is in the archive: without it the scan below would be
    # searching a bundle that never carried the session at all.
    assert "logs/pz-agent.jsonl" in bundle.names
    with zipfile.ZipFile(destination) as archive:
        names = archive.namelist()
        assert names
        for name in names:
            assert _canary_leaks(archive.read(name)) == (), name
            assert _canary_leaks(name.encode("utf-8")) == (), name

    # And the product's own verifier agrees, told the two literals by name.
    verification = verify_bundle(
        destination,
        redactor=workspace.redactor,
        forbidden={"<CANARY>": CANARY_MARK, "<CANARY_KEY>": CANARY_KEY},
    )
    assert verification.entries
    assert [
        finding
        for entry in verification.entries
        for finding in entry.findings
        if finding.startswith("literal:")
    ] == []


def test_nothing_the_session_wrote_anywhere_on_the_machine_carries_the_canary(
    tmp_path: Path,
) -> None:
    """E10-M03-T010. Enumeration by exhaustion: every file, not one file.

    A needle hunt aimed at the sinks somebody thought of proves only that those
    sinks are clean. This walks the whole fake machine — the state directory,
    the exchange directory, the logs, the bundle, the voice record, the panic
    latch — and requires that no byte of the canary is anywhere in it.
    """
    world, workspace, _ = _run_a_canary_session(tmp_path)
    build_support_bundle(world.ctx, workspace, workspace.bundle_dir / "support.zip")

    scanned = sorted(path for path in tmp_path.rglob("*") if path.is_file())
    names = {path.name for path in scanned}
    # The sweep is only worth as much as what it happened to cover, so the three
    # files that would carry a leak are required to be among them.
    assert "pz-agent.jsonl" in names
    assert VOICE_RECORD_NAME in names
    assert workspace.ipc_root is not None
    latch = IpcLayout(workspace.ipc_root).panic_stop
    assert latch.is_file()
    assert latch.stat().st_size > 0, "the spoken stop never reached the latch"
    assert latch in scanned

    for path in scanned:
        assert _canary_leaks(path.read_bytes()) == (), str(path.relative_to(tmp_path))


def test_the_adversarial_phrase_is_refused_on_the_one_route_it_could_have_taken(
    tmp_path: Path,
) -> None:
    """E10-M03-T010, the other end: arming is not something a room can say.

    :meth:`~pz_agent_cli.voice.ExchangeSessionPort.arm` is the one port method
    the sweep above never reaches, because no phrase in the vocabulary resolves
    to it. It raises rather than writing anything, so there is no file for the
    sweep to find — and that is asserted here rather than left implied.
    """
    world, workspace = _a_voice_world(tmp_path)
    from pz_agent_cli.voice import voice_services  # noqa: PLC0415  (one call site)

    services = voice_services(workspace, clock=world.ctx.clock_ms)
    before = {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(VoiceRefused):
        services.session.arm(SessionMode.ASSISTED, confirm_backup=True)
    # "It raises rather than writing anything" is the claim, so it is checked
    # rather than left to the docstring: no file appeared and none was touched.
    after = {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before


# ---------------------------------------------------------------------------
# E10-M02-T003 and E10-M02-T009: the bridge, over real pipes
# ---------------------------------------------------------------------------


#: The child. It records every byte it is sent before it does anything with it,
#: which is what makes "no transcript crossed" an observation rather than an
#: inference from this side's own encoder. It exits on an ``interrupt``, so a
#: test can make a real process die under a running supervisor.
BRIDGE_PROGRAM: Final = f"""
import json, sys

CAP = {CHILD_READ_CAP}
OUT = sys.stdout.buffer
IN = sys.stdin.buffer
RECORD = open(sys.argv[1], "ab")
with open(sys.argv[2], encoding="utf-8") as handle:
    TRANSCRIPT = handle.read(CAP)


def send(**body):
    body.setdefault("v", "1.0")
    OUT.write(json.dumps(body, ensure_ascii=False).encode("utf-8") + b"\\n")
    OUT.flush()


for line in iter(lambda: IN.readline(CAP), b""):
    RECORD.write(line)
    RECORD.flush()
    try:
        message = json.loads(line)
    except ValueError:
        continue
    kind = message.get("type")
    if kind == "hello":
        send(type="ready")
        send(type="transcript", text=TRANSCRIPT, at_ms=1, final=True, confidence=0.95)
    elif kind == "goal":
        send(type="outcome", request_id=message["request_id"], status="ended")
    elif kind == "speak":
        send(type="outcome", request_id=message["utterance_id"], status="ended")
    elif kind == "interrupt":
        sys.exit(0)
"""


@dataclass(frozen=True)
class BridgeFixture:
    """A launchable bridge configuration and the file the child writes to."""

    config: BridgeConfig
    received: Path


def _a_bridge(tmp_path: Path, transcript: str) -> BridgeFixture:
    """Write the child, and the transcript it will say, and configure it.

    The transcript goes in a file rather than on the command line: an argument
    is visible in a process list, and a test that put the canary there would be
    leaking it itself.
    """
    program = tmp_path / "bridge.py"
    program.write_text(BRIDGE_PROGRAM, encoding="utf-8")
    heard = tmp_path / "heard.txt"
    heard.write_text(transcript, encoding="utf-8")
    received = tmp_path / "received.jsonl"
    received.write_bytes(b"")
    return BridgeFixture(
        config=BridgeConfig(
            command=(sys.executable, "-u", str(program), str(received), str(heard)),
            startup_timeout=PATIENCE,
            reply_timeout=PATIENCE,
            speech_timeout=PATIENCE,
            stop_timeout=0.5,
            max_restarts=2,
        ),
        received=received,
    )


def test_an_intent_carried_to_the_bridge_carries_no_transcript(tmp_path: Path) -> None:
    """E10-M02-T003. The canary arrives from the child and does not go back.

    Every byte the child was sent is on disk before this reads it, so the scan
    is of the wire and not of this side's opinion of the wire.
    """
    fixture = _a_bridge(tmp_path, CANARY_PHRASES[0][1])
    bridge = JsonlBridge(fixture.config)
    try:
        bridge.start()
        heard = bridge.next_transcript(PATIENCE)
        assert heard is not None, "the child said nothing, so this test proved nothing"
        assert CANARY_MARK in heard.text, "the canary never reached this process"

        match = classify(VoiceInput(transcript=heard.text, at_ms=heard.at_ms))
        assert match.goal is VoiceGoal.EAT
        outcome = bridge.exchange(
            goal_message(request_id="voice-1", goal=match.goal),
            request_id="voice-1",
            timeout=PATIENCE,
        )
        assert outcome.ended
        bridge.exchange(
            speak_message(
                utterance_id="teamon-1",
                text=phrases.GOAL_ACCEPTED[VoiceGoal.EAT],
                priority=0,
                interruptible=True,
            ),
            request_id="teamon-1",
            timeout=PATIENCE,
        )
    finally:
        bridge.stop()

    sent = fixture.received.read_bytes()
    # The intent crossed — one enum member, spelled as the literal bytes.
    assert b'"goal":"eat"' in sent
    assert _canary_leaks(sent) == ()


def test_bridge_start_exit_and_restart_are_logged_and_payloads_are_not(tmp_path: Path) -> None:
    """E10-M02-T009. A real child dies and is relaunched, and the log says so.

    The listener here logs *everything* a :class:`BridgeReport` carries, because
    the rule has to hold for a caller that reports everything rather than only
    for a careful one. The log takes the null redactor, so nothing downstream is
    covering for the bridge.
    """
    fixture = _a_bridge(tmp_path, CANARY_PHRASES[0][1])
    logs = tmp_path / "logs"
    log = DiagnosticLog(logs, name="voice")
    reports: list[BridgeReport] = []

    def listener(report: BridgeReport) -> None:
        reports.append(report)
        log.log(
            LogLevel.WARNING,
            "voice.bridge.report",
            reason=report.reason.value,
            state=report.state.value,
            detail=report.detail,
            spoken=report.spoken() or "",
        )

    spoken = phrases.GOAL_ACCEPTED[VoiceGoal.EAT]
    bridge = JsonlBridge(fixture.config, listener=listener)
    try:
        bridge.start()
        log.log(LogLevel.INFO, "voice.bridge.started", state=bridge.state.value)
        heard = bridge.next_transcript(PATIENCE)
        assert heard is not None
        bridge.exchange(
            speak_message(utterance_id="teamon-1", text=spoken, priority=0, interruptible=True),
            request_id="teamon-1",
            timeout=PATIENCE,
        )
        # A real process ending under a running supervisor.
        bridge.send(interrupt_message("teamon-1"))
        assert _until(lambda: not bridge.alive), "the child never exited"
        assert _until(lambda: BridgeReason.CRASHED in [item.reason for item in reports])
        log.log(LogLevel.WARNING, "voice.bridge.exited", alive=bridge.alive)

        bridge.send(hello_message())
        assert bridge.restarts == 1
        log.log(LogLevel.INFO, "voice.bridge.restarted", restarts=bridge.restarts)
    finally:
        bridge.stop()

    reasons = [item.reason for item in reports]
    assert BridgeReason.CRASHED in reasons
    assert BridgeReason.RESTARTED in reasons

    records, problems = read_records(logs / "voice.jsonl")
    assert problems == ()
    events = [str(record["event"]) for record in records]
    assert "voice.bridge.started" in events
    assert "voice.bridge.exited" in events
    assert "voice.bridge.restarted" in events
    assert "voice.bridge.report" in events

    written = sorted(path for path in logs.iterdir() if path.is_file())
    assert written
    for path in written:
        raw = path.read_bytes()
        assert _canary_leaks(raw) == (), path.name
        # Not only the transcript: no payload at all. The sentence that was
        # synthesised is this build's own, and it is still not a diagnostic.
        assert _found(raw, (spoken,)) == (), path.name


# ---------------------------------------------------------------------------
# E10-M03-T010: the outbound surfaces, enumerated rather than sampled
# ---------------------------------------------------------------------------


def _bridge_modules() -> tuple[ModuleType, ...]:
    """Every module in this package that owns a bridge wire.

    Enumerated rather than named because the wire lived in two places during
    this branch: two agents built it independently, and only one of them was
    ever imported or tested. The unreachable copy has been removed, so the list
    is down to one — but the enumeration stays, along with the assertion below
    that it found something, because a test pinned to a single module name is a
    test that silently stops covering anything if the module is renamed.
    """
    found: list[ModuleType] = []
    for name in ("pz_agent_voice.teamon",):
        with contextlib.suppress(ImportError):
            found.append(importlib.import_module(name))
    assert found, "the voice package ships no bridge wire; this test covered nothing"
    return tuple(found)


def _phrase_table() -> frozenset[str]:
    """Every sentence :mod:`pz_agent_voice.phrases` can produce."""
    out: set[str] = set()
    for name in dir(phrases):
        if name.startswith("_"):
            continue
        value = getattr(phrases, name)
        if isinstance(value, str):
            out.add(value)
        elif isinstance(value, Mapping):
            out.update(item for item in value.values() if isinstance(item, str))
    return frozenset(out)


def _outbound_messages() -> tuple[BridgeMessage, ...]:
    """One of every message this build can put on the bridge's pipe."""
    return (
        hello_message(),
        speak_message(
            utterance_id="teamon-1",
            text=phrases.STOP_ACK,
            priority=0,
            interruptible=False,
        ),
        interrupt_message("teamon-1"),
        goal_message(request_id="voice-1", goal=VoiceGoal.EAT),
    )


def test_every_message_the_bridge_can_be_sent_is_enumerated_here() -> None:
    """The completeness half: a new outbound type fails until it is scanned."""
    declared = {
        kind
        for kind, direction in MESSAGE_DIRECTIONS.items()
        if direction is BridgeDirection.TO_BRIDGE
    }
    assert declared == {
        BridgeMessageType.HELLO,
        BridgeMessageType.SPEAK,
        BridgeMessageType.INTERRUPT,
        BridgeMessageType.GOAL,
    }
    assert {message.type for message in _outbound_messages()} == declared
    # Compared by value across modules: the wire has lived in two of them during
    # this branch, and their enums are distinct classes even where the protocol
    # they spell out is the same one.
    for module in _bridge_modules():
        assert {
            str(kind)
            for kind, direction in module.MESSAGE_DIRECTIONS.items()
            if str(direction) == str(BridgeDirection.TO_BRIDGE)
        } == {str(kind) for kind in declared}


def test_every_string_on_the_bridge_wire_is_one_this_repository_wrote() -> None:
    """The structural proof, which does not depend on guessing the needle.

    A scan finds what it thought to look for. This requires every string in
    every outbound message — the field names as well as the values — to be a
    member of the closed set this repository authored: an enum value, a sentence
    from the phrase table, a handle minted here, a declared field name, or the
    protocol version. An arbitrary string fails whether or not anybody guessed
    at its contents, and a new field fails until it is declared here.
    """
    field_names = {
        "v",
        "type",
        "utterance_id",
        "text",
        "priority",
        "interruptible",
        "request_id",
        "goal",
    }
    allowed = {
        *field_names,
        *(goal.value for goal in VoiceGoal),
        *(kind.value for kind in BridgeMessageType),
        *_phrase_table(),
        BRIDGE_PROTOCOL_VERSION,
        "teamon-1",
        "voice-1",
    }
    for message in _outbound_messages():
        line = encode(message)
        assert _canary_leaks(line) == ()
        for found in _strings(json.loads(line.decode("utf-8"))):
            assert found in allowed, (message.type, found)


def test_the_goal_that_reaches_the_wire_is_always_an_enum_member() -> None:
    """The channel is one enum member wide, checked where the value is minted.

    :func:`~pz_agent_voice.teamon.goal_message` writes ``str(goal)``, so *its*
    guard against a transcript is the type checker and nothing else: a caller
    who passed a string would put the string on the wire, and this test was
    written expecting a refusal that does not happen. What actually keeps the
    channel narrow is the source of the value — the only thing in this package
    that produces a goal is the matcher, and it can produce nothing but a
    member. So that is what is asserted, over every adversarial phrase in this
    file, by identity of *type*: a ``StrEnum`` member compares equal to its own
    value, so an equality check would pass for a bare string as well.
    """
    produced: list[VoiceGoal] = []
    for _, phrase in (*ADVERSARIAL, *CANARY_PHRASES):
        match = classify(VoiceInput(transcript=phrase, at_ms=0))
        for goal in match.goals:
            assert type(goal) is VoiceGoal, phrase
            produced.append(goal)
    assert produced, "no phrase produced a goal, so this test proved nothing"

    # And the whole width of the channel, against hand-written literals.
    assert {str(goal) for goal in VoiceGoal} == {"eat", "drink", "read", "resume"}
    for goal in VoiceGoal:
        line = encode(goal_message(request_id="voice-1", goal=goal))
        assert json.loads(line.decode("utf-8"))["goal"] == str(goal)
