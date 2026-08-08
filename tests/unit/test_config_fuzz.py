"""Fuzzing the config boundary: user-authored TOML is hostile input.

``config.toml`` is edited by hand, so the file that reaches :func:`load_config`
is adversarial the way any parser's input is: truncated mid-token, a section
header that never closes, a safety flag typed as a string, a number past the
product ceiling, a key that is one dropped letter from a real one. The module's
whole contract (see its docstring) is that *none* of that reaches the caller as
a raw exception: every fault becomes a :class:`ConfigProblem` that names a
remedy, and ``config`` is produced only for a document that passed clean.

These properties drive the *real* parser — no re-implementation of the schema
to disagree with it — from two directions the module distinguishes internally:
whole files through :func:`load_config` (which owns the read, the size cap, the
UTF-8 decode and the :mod:`tomllib` parse), and already-parsed documents through
:func:`validate_document` (which owns the schema walk). The generators are
seeded off a single fixed literal so a red run reproduces byte for byte; nothing
here reads the clock or the global :mod:`random` state.

The fuzz found one real defect: :func:`AgentConfig.to_toml` did not escape a
newline inside a string value, so a config that validated could not re-load
from its own rendering. It is now fixed (``_toml_basic_string`` escapes every
control character), pinned by
:func:`test_a_control_char_in_a_string_round_trips_through_to_toml` below; the
broad round-trip property still feeds control-character-free strings, so the
two divide the general shape from the exact edge.
"""

from __future__ import annotations

import datetime as dt
import random
from pathlib import Path

from pz_agent_cli.config import (
    BOUND_PANIC_HOTKEY,
    CODE_MISSING_VALUE,
    CODE_NOT_ALLOWED,
    CODE_NOT_FOUND,
    CODE_OUT_OF_RANGE,
    CODE_PARSE_ERROR,
    CODE_TOO_LARGE,
    CODE_TYPE_MISMATCH,
    CODE_UNKNOWN_KEY,
    CODE_UNKNOWN_TABLE,
    CODE_UNREADABLE,
    KNOWN_CAPABILITIES,
    MAX_AUTONOMOUS_RADIUS,
    MAX_PLAN_STEPS,
    SCHEMA,
    ConfigProblem,
    ConfigValidation,
    default_config,
    load_config,
    validate_document,
)
from pz_agent_core.version import SUPPORTED_BUILDS

#: One literal seeds every generator in this file. Changing it reshuffles the
#: corpus; keeping it fixed makes any failure a stable, re-runnable case.
SEED = 0x2110B1_C0FFEE

#: The only problem codes the module documents. Anything outside this set would
#: mean a code path invented an ad-hoc code, which is the sort of drift the stable
#: -codes contract exists to prevent.
KNOWN_CODES = frozenset(
    {
        CODE_UNKNOWN_TABLE,
        CODE_UNKNOWN_KEY,
        CODE_TYPE_MISMATCH,
        CODE_OUT_OF_RANGE,
        CODE_NOT_ALLOWED,
        CODE_PARSE_ERROR,
        CODE_NOT_FOUND,
        CODE_MISSING_VALUE,
        CODE_TOO_LARGE,
        CODE_UNREADABLE,
    }
)

#: Table names the generators reach for: every real one, the sub-tables, and a
#: handful of near-misses (a transposed ``saftey``, a header that is empty, a
#: provider table that does not exist) so the unknown-table path is exercised
#: rather than assumed.
_TABLE_NAMES = (
    *SCHEMA.keys(),
    "saftey",
    "planner.bogus",
    "voice.extra",
    "",
    "a.b.c",
    "GAME",
)

#: Key names likewise: every documented key across all tables, plus typos and a
#: blank one. A key that belongs to another table is a fault when it lands in the
#: wrong section, which is exactly the confusion a flat pool provokes.
_KEY_NAMES = (
    *{key for keys in SCHEMA.values() for key in keys},
    "manual_takover",
    "max_autonamous_radius",
    "provder",
    "unknown_key",
    "",
)

#: String payloads: legal enum values, a valid endpoint, an env-var name, and
#: the values the module singles out for refusal — an over-length name, a hotkey
#: combination, a pasted-looking secret, and strings carrying control characters
#: (which validate for the free-form path keys and matter to serialization).
_STRINGS = (
    "stable",
    "unstable",
    "observe",
    "autonomous",
    "none",
    "teamon",
    "openai_compatible",
    "http://127.0.0.1:8080",
    "https://api.example.com/v1",
    "PZ_AGENT_OPENAI_API_KEY",
    "F12",
    "Ctrl+Shift+F12",
    "beta",
    "sk-0123456789abcdef",
    "D:/Games/ProjectZomboid",
    "with a space",
    "line\nbreak",
    "carriage\rreturn",
    "tab\tseparated",
    "\x00\x01\x02",
    'quote"inside',
    "back\\slash",
    "A" * 400,
    "",
    "42.20",
    "food.eat",
)


def _random_scalar(rng: random.Random) -> object:
    """One TOML-representable value, spanning the types :mod:`tomllib` yields.

    Restricted to what a real document can carry — str, int, float, bool, list,
    datetime, nested table — so a crash found here is a crash the module can
    actually be handed, not an artefact of an input TOML could never produce.
    """
    pick = rng.randint(0, 9)
    if pick <= 2:
        return rng.choice(_STRINGS)
    if pick == 3:
        # Includes values past every ``maximum`` and below every ``minimum`` so
        # the range checks fire, and an integer far beyond 64 bits because TOML
        # integers are unbounded in Python and the validator must not assume a width.
        return rng.choice([0, 1, -1, 30, MAX_AUTONOMOUS_RADIUS + 1, -9999, 10**40])
    if pick == 4:
        return rng.choice([True, False])
    if pick == 5:
        return rng.choice([0.5, -0.1, 3.14, 1e309])
    if pick == 6:
        return [rng.choice(_STRINGS) for _ in range(rng.randint(0, 4))]
    if pick == 7:
        # A ragged array: strings mixed with a nested list. The list-valued key
        # accepts only flat quoted strings, so this drives the entry-type refusal.
        return [rng.choice(_STRINGS), [rng.choice(_STRINGS)]]
    if pick == 8:
        return rng.choice([dt.date(2020, 1, 1), dt.datetime(2020, 1, 1, 12, 0), dt.time(6, 30)])
    return {rng.choice(_KEY_NAMES): rng.choice(_STRINGS)}


def _toml_scalar(rng: random.Random, value: object) -> str | None:
    """Render *value* as a TOML fragment, or ``None`` when it has no scalar form.

    Nested tables and lists holding non-scalars are skipped rather than forced
    into a shape TOML cannot express; the dict-driven property below exercises
    those instead.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.isoformat()
    if isinstance(value, list):
        parts = [_toml_scalar(rng, item) for item in value]
        if any(part is None for part in parts):
            return None
        return "[" + ", ".join(p for p in parts if p is not None) + "]"
    if isinstance(value, str):
        # A basic string with the two escapes TOML requires, so a payload that is
        # itself hostile still lands as a string rather than as a syntax error the
        # generator did not intend — the syntax faults are injected separately.
        body = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        body = body.replace("\r", "\\r").replace("\t", "\\t")
        return f'"{body}"'
    return None


def _hostile_toml(rng: random.Random) -> str:
    """A document assembled from the pools, then sometimes broken at the byte level.

    The structural faults (wrong table, wrong key, wrong type, out-of-range) come
    from the pools; the lexical faults (an unclosed bracket, a stray control byte,
    a dangling ``=``) come from the corruption pass, so both the validator and the
    :mod:`tomllib` boundary above it are driven.
    """
    lines: list[str] = []
    for _ in range(rng.randint(0, 6)):
        lines.append(f"[{rng.choice(_TABLE_NAMES)}]")
        for _ in range(rng.randint(0, 5)):
            rendered = _toml_scalar(rng, _random_scalar(rng))
            if rendered is None:
                continue
            lines.append(f"{rng.choice(_KEY_NAMES)} = {rendered}")
    text = "\n".join(lines)
    if rng.random() < 0.35 and text:
        cut = rng.randrange(len(text))
        wedge = rng.choice(["[", "]", "=", '"', "\x00", "\n\n[", "###", "\\"])
        text = text[:cut] + wedge + text[cut:]
    if rng.random() < 0.1:
        text = text[: rng.randint(0, len(text))]  # truncation mid-token
    return text


def _mutated_default(rng: random.Random) -> str:
    """The shipped default TOML with a few lines damaged.

    Starting from a document known to validate and injuring it one edit at a time
    reaches faults the from-scratch builder rarely lands on: a real key given a
    wrong-typed value, a section header retyped as a neighbour, a lone key with
    its ``=`` removed.
    """
    lines = default_config().to_toml().split("\n")
    for _ in range(rng.randint(1, 4)):
        if not lines:
            break
        index = rng.randrange(len(lines))
        op = rng.randint(0, 4)
        if op == 0:
            del lines[index]
        elif op == 1:
            rendered = _toml_scalar(rng, _random_scalar(rng)) or "0"
            lines[index] = f"{rng.choice(_KEY_NAMES)} = {rendered}"
        elif op == 2:
            lines[index] = f"[{rng.choice(_TABLE_NAMES)}]"
        elif op == 3:
            lines[index] = lines[index].replace("=", " ", 1)
        else:
            lines.insert(index, rng.choice(["garbage", "= 5", "[unterminated", "\x00"]))
    return "\n".join(lines)


def _hostile_document(rng: random.Random) -> dict[str, object]:
    """A parsed document, as :mod:`tomllib` would hand one to the validator.

    This bypasses the lexer to reach the schema walk directly, and it can express
    shapes the TOML text generator cannot round-trip — a table whose body is a
    scalar, a datetime where a string is wanted — so the validator's own type
    guards are what gets tested rather than the parser's.
    """
    document: dict[str, object] = {}
    for _ in range(rng.randint(0, 5)):
        table = rng.choice(_TABLE_NAMES)
        if rng.random() < 0.15:
            document[table] = _random_scalar(rng)  # a section written as a value
            continue
        body: dict[str, object] = {}
        for _ in range(rng.randint(0, 5)):
            body[rng.choice(_KEY_NAMES)] = _random_scalar(rng)
        document[table] = body
    return document


def _assert_well_formed(validation: ConfigValidation) -> None:
    """Every observable postcondition the module promises, on one result.

    Kept in one place because all three fuzz properties assert the same shape:
    a typed problem for every fault, a remedy on each, only documented codes, and
    the ``config``-present-iff-clean invariant that is the point of the whole
    module — a returned config is one nothing can start a session on by mistake.
    """
    assert isinstance(validation, ConfigValidation)
    for problem in (*validation.errors, *validation.warnings):
        assert isinstance(problem, ConfigProblem)
        assert problem.remediation, f"a problem named no remedy: {problem!r}"
        assert problem.code in KNOWN_CODES, f"undocumented code {problem.code!r}"
        # render() and to_dict() are what the CLI and the --json output call; a
        # problem that cannot describe itself is a crash deferred to the reporter.
        assert problem.path in problem.render()
        assert set(problem.to_dict()) == {"path", "code", "detail", "remediation"}
    assert validation.ok == (not validation.errors)
    assert (validation.config is None) == (not validation.ok)


def test_load_config_never_lets_a_bare_exception_reach_the_caller(tmp_path: Path) -> None:
    """PROPERTY (catches: raw tomllib.TOMLDecodeError / KeyError / ValueError / OSError
    escaping the file boundary). Whatever bytes sit in the file, ``load_config``
    returns a :class:`ConfigValidation`, never raises: the size cap, the decode,
    the parse and the schema walk each turn their failure into a typed problem.
    """
    rng = random.Random(SEED)
    for index in range(700):
        text = _hostile_toml(rng) if index % 2 == 0 else _mutated_default(rng)
        # Named by index, not by chance, so a failure points at a fixed file.
        path = tmp_path / f"hostile_{index}.toml"
        path.write_text(text, encoding="utf-8")
        _assert_well_formed(load_config(path))


def test_validate_document_never_lets_a_bare_exception_reach_the_caller() -> None:
    """PROPERTY (catches: KeyError / ValueError / TypeError inside the schema walk,
    reached without the lexer). A document shaped like anything :mod:`tomllib`
    could yield — scalars where tables belong, datetimes where strings belong,
    unknown tables and keys — is validated into typed problems, never a traceback.
    """
    rng = random.Random(SEED + 1)
    for _ in range(700):
        document = _hostile_document(rng)
        _assert_well_formed(validate_document(document, path=Path("fuzz.toml")))


def test_a_forbidden_but_well_typed_value_is_a_typed_refusal_not_silence(
    tmp_path: Path,
) -> None:
    """PROPERTY (catches: a semantically forbidden setting loading as if accepted).
    Values that parse and type-check but the build refuses on their meaning — a
    non-F12 panic key, multiplayer turned on, an unknown adapter, a pasted secret
    where a variable name belongs — each yield an error, never a clean config.
    """
    rng = random.Random(SEED + 2)
    # None of these is the bound key, and none lowercases onto a real adapter, so
    # every branch below is genuinely refused rather than quietly normalised.
    hotkeys = ("F1", "escape", "Ctrl+F12", "f12x", "", "A" * 40)
    adapters = ("fake", "openai", "voice", "microphone", "")
    for index in range(200):
        forbidden = rng.choice(
            [
                f'[safety]\npanic_hotkey = "{rng.choice(hotkeys)}"\n',
                "[safety]\nallow_multiplayer = true\n",
                f'[voice]\nadapter = "{rng.choice(adapters)}"\n',
                '[planner.openai_compatible]\napi_key_env = "sk-pasted-secret-value"\n'
                '[planner]\nprovider = "openai_compatible"\n'
                'base_url = "http://127.0.0.1:8080"\nmodel = "m"\n',
            ]
        )
        path = tmp_path / f"forbidden_{index}.toml"
        path.write_text(forbidden, encoding="utf-8")
        validation = load_config(path)
        _assert_well_formed(validation)
        # A refusal, not a config: none of the branches above names a value the
        # build accepts, so every one of them must fail to produce a config.
        assert not validation.ok, f"a forbidden value loaded clean: {forbidden!r}"
        assert validation.config is None


def _valid_toml(rng: random.Random) -> str:
    """A document built to pass, drawing only from values the schema accepts.

    Kept free of control characters on purpose: their round trip has its own
    regression test (they used to break :meth:`AgentConfig.to_toml`, now fixed),
    and this broad property is about the ordinary space the serializer handles.
    """
    lines = [
        "[game]",
        f'channel = "{rng.choice(("stable", "unstable"))}"',
        f'expected_build = "{rng.choice((*SUPPORTED_BUILDS, "41.78", "99.9"))}"',
    ]
    if rng.random() < 0.5:
        lines.append(f'install_dir = "D:/Games/PZ{rng.randint(0, 99)}"')
    lines += [
        "[session]",
        f'default_mode = "{rng.choice(("observe", "assisted", "autonomous"))}"',
        f"require_backup = {rng.choice(('true', 'false'))}",
        "[safety]",
        f'panic_hotkey = "{BOUND_PANIC_HOTKEY}"',
        f"manual_takeover = {rng.choice(('true', 'false'))}",
        f"max_autonomous_radius = {rng.randint(1, MAX_AUTONOMOUS_RADIUS)}",
    ]
    caps = rng.sample(list(KNOWN_CAPABILITIES), rng.randint(0, min(3, len(KNOWN_CAPABILITIES))))
    lines.append("disabled_capabilities = [" + ", ".join(f'"{c}"' for c in caps) + "]")
    provider = rng.choice(("none", "none", "openai_compatible", "teamon"))
    lines += [
        "[planner]",
        f'provider = "{provider}"',
        f"max_steps = {rng.randint(1, MAX_PLAN_STEPS)}",
    ]
    if provider == "openai_compatible":
        lines += [
            "[planner.openai_compatible]",
            f'base_url = "http://127.0.0.1:{rng.randint(1, 65535)}"',
            f'model = "model-{rng.randint(0, 9)}"',
        ]
    elif provider == "teamon":
        lines += [
            "[planner.teamon]",
            f'base_url = "https://host{rng.randint(0, 9)}.example.com"',
        ]
    lines += ["[voice]", f'adapter = "{rng.choice(("teamon", "none"))}"']
    return "\n".join(lines) + "\n"


def test_a_valid_config_round_trips_through_to_toml(tmp_path: Path) -> None:
    """PROPERTY (catches: a serializer that disagrees with the validated config it
    renders). A document that validates, rendered by ``to_toml`` and loaded again,
    must validate to the *same* config — the support bundle's copy has to mean what
    the user's file meant. Control-character strings are covered separately by
    ``test_a_control_char_in_a_string_round_trips_through_to_toml``.
    """
    rng = random.Random(SEED + 3)
    for index in range(300):
        original = tmp_path / f"valid_{index}.toml"
        original.write_text(_valid_toml(rng), encoding="utf-8")
        first = load_config(original)
        assert first.ok, [p.render() for p in first.errors]
        assert first.config is not None

        echoed = tmp_path / f"valid_{index}_echo.toml"
        echoed.write_text(first.config.to_toml(), encoding="utf-8")
        second = load_config(echoed)

        assert second.ok, [p.render() for p in second.errors]
        assert second.config is not None
        assert second.config.to_dict() == first.config.to_dict()


def test_a_control_char_in_a_string_round_trips_through_to_toml(tmp_path: Path) -> None:
    """The defect closed: a config with a control char re-loads from its render.

    ``to_toml`` escaped only ``\\`` and ``"``, so a newline inside a free-form
    string key — ``game.install_dir`` and its siblings — was re-emitted as a
    literal newline, which TOML forbids inside a basic string; the support
    bundle's rendered copy then failed to parse. ``_toml_basic_string`` now
    escapes every control character, so a config that validated round-trips
    through its own serializer.

    Minimal, seed-independent: ``install_dir = "a\\nb"`` (a real newline).
    """
    source = tmp_path / "c.toml"
    source.write_text('[game]\ninstall_dir = "a\\nb"\n', encoding="utf-8")
    validated = load_config(source)
    assert validated.ok and validated.config is not None

    echoed = tmp_path / "c_echo.toml"
    echoed.write_text(validated.config.to_toml(), encoding="utf-8")
    reloaded = load_config(echoed)
    assert reloaded.ok, (
        "to_toml did not escape the newline; reload failed with "
        f"{[(p.code, p.detail) for p in reloaded.errors]}"
    )
    assert reloaded.config is not None
    # The escaped byte survived as itself, not as a literal newline the parser
    # would have split the line on.
    assert "a\nb" in reloaded.config.to_toml() or "a\\nb" in echoed.read_text(encoding="utf-8")
