"""The absolute rule: no transcript text leaves the voice process.

An adversarial utterance is fed to :func:`~pz_agent_voice.intents.resolve` and
then followed to each of the four places text could escape to — the goal handed
to the core, a diagnostic log, a support bundle, and an RPC call on the wire —
and none of them is allowed to contain a byte of it.

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
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest

from pz_agent_core.diagnostics import BundleBuilder, DiagnosticLog, LogLevel
from pz_agent_core.goals import PARAM_NAMES, GoalKind, GoalRequest, TrainableSkill
from pz_agent_core.rpc import RpcRequest, encode_request
from pz_agent_voice.intents import (
    REFUSAL_MESSAGES,
    VoiceOutcome,
    VoiceResolution,
    resolve,
    to_goal_request,
)

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
    try:
        forms.extend(_strings(json.loads(text)))
    except (json.JSONDecodeError, ValueError):
        # Not JSON. The raw text form above is the whole haystack, which is the
        # normal case for a human-readable log line.
        pass
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


def _leaks(data: bytes) -> tuple[str, ...]:
    """Which needles *data* contains, if any."""
    forms = _haystacks(data)
    return tuple(
        needle
        for needle in NEEDLES
        if any(needle in form or needle.casefold() in form.casefold() for form in forms)
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
    if resolution.outcome is not VoiceOutcome.GOAL:
        with pytest.raises(ValueError, match="only a goal resolution"):
            to_goal_request(resolution, sequence=7)
        return
    request = to_goal_request(resolution, sequence=7)
    assert _leaks(repr(request).encode("utf-8")) == ()
    assert re.fullmatch(r"voice\.[a-z_]+\.\d{6}", request.idempotency_key)


def test_both_goal_phrases_actually_produce_a_goal() -> None:
    """Guards the branch above: a rule proved only on refusals proves nothing."""
    produced = [label for label, u in ADVERSARIAL if resolve(u).outcome is VoiceOutcome.GOAL]
    assert produced == ["goal", "goal_with_params"]


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
        *(outcome.value for outcome in VoiceOutcome),
        *REFUSAL_MESSAGES.values(),
        *(code.value for code in REFUSAL_MESSAGES),
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


def _resolution_strings(resolution: VoiceResolution) -> list[str]:
    """Every string a resolution carries, including inside its refusal."""
    found = [resolution.outcome.value]
    if resolution.kind is not None:
        found.append(resolution.kind.value)
    if resolution.params.skill is not None:
        found.append(resolution.params.skill.value)
    if resolution.refusal is not None:
        found.extend(
            [
                resolution.refusal.code.value,
                resolution.refusal.message,
                resolution.refusal.parameter,
            ]
        )
    return found


# ---------------------------------------------------------------------------
# sink two: a log
# ---------------------------------------------------------------------------


def _log_a_resolution(directory: Path, resolution: VoiceResolution) -> DiagnosticLog:
    """Write the record a caller would write about *resolution*.

    Deliberately verbose: it logs every public field, because the rule has to
    hold for a caller that reports everything, not only for a careful one. The
    log is built with the default null redactor so nothing is scrubbed for us.
    """
    log = DiagnosticLog(directory, name="voice")
    log.log(
        LogLevel.INFO,
        "voice.resolved",
        outcome=resolution.outcome.value,
        kind=None if resolution.kind is None else resolution.kind.value,
        truncated=resolution.truncated,
        skill=None if resolution.params.skill is None else resolution.params.skill.value,
        target_level=resolution.params.target_level,
        pages=resolution.params.pages,
        satisfy_to=resolution.params.satisfy_to,
        refusal=None if resolution.refusal is None else resolution.refusal.code.value,
        parameter=None if resolution.refusal is None else resolution.refusal.parameter,
        message=None if resolution.refusal is None else resolution.refusal.message,
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
    from pz_agent_voice import intents

    source = Path(intents.__file__).read_text(encoding="utf-8")
    for forbidden in ("import logging", "print(", "open(", "subprocess", "socket", "sys.std"):
        assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# sink three: a support bundle
# ---------------------------------------------------------------------------


def _bundle_a_resolution(destination: Path, resolution: VoiceResolution) -> Path:
    """Build a real archive holding everything a bundle would carry about it."""
    builder = BundleBuilder()
    builder.add_json(
        "voice/resolution.json",
        {
            "outcome": resolution.outcome.value,
            "kind": None if resolution.kind is None else resolution.kind.value,
            "truncated": resolution.truncated,
            "refusal": None if resolution.refusal is None else resolution.refusal.code.value,
            "message": None if resolution.refusal is None else resolution.refusal.message,
        },
    )
    if resolution.refusal is not None:
        builder.add_text("voice/refusal.txt", resolution.refusal.message + "\n")
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
    if resolution.outcome is not VoiceOutcome.GOAL:
        # A non-goal never reaches the link at all: there is no request to
        # encode, and asserting that is the honest form of "nothing leaked".
        with pytest.raises(ValueError, match="only a goal resolution"):
            to_goal_request(resolution, sequence=1)
        return
    assert _leaks(_rpc_bytes(to_goal_request(resolution, sequence=1))) == (), label


def test_a_refusal_crossing_the_link_carries_only_its_constant() -> None:
    """Even when a refusal *is* reported over RPC, it carries no transcript."""
    resolution = resolve(ADVERSARIAL[2][1])
    assert resolution.refusal is not None
    core = resolution.refusal.to_goal_refusal()
    encoded = encode_request(
        RpcRequest(
            id="voice-0002",
            method="voice.refused",
            params={"reason_code": core.reason_code.value, "message": core.message},
        )
    )
    assert _leaks(encoded) == ()
    assert core.message in set(REFUSAL_MESSAGES.values())


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
