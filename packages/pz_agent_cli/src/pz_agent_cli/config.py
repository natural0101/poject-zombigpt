"""The ``config.toml`` shape documented in ``docs/QUICKSTART.md``, and its validator.

Read with :mod:`tomllib` from the standard library — the core package carries no
third-party dependency and the CLI inherits that rule.

**An unknown key is an error, not a warning.** A misspelt safety setting must
fail loudly: ``manual_takover = false`` that validates as "unknown key ignored"
leaves the user believing they turned something off, and leaves the agent
running with it on — or the reverse, which is worse. The same applies to an
unknown table, because a typo in a section header silently discards every
setting inside it.

Validation runs before start rather than on first use, so a value that only the
planner reads is still rejected while the user is still looking at the terminal.
That is why a provider's own section is validated by *constructing* its typed
config here, faults and all: the rules live once, in the provider, and this
module's job is to run them early rather than to restate them.

**No section of this file ever holds a secret.** A provider names the
environment variable its API key lives in; the key itself is read from the
environment at the moment of the call. That is what makes this file safe to
paste into a bug report, and it is why ``api_key_env`` is validated as a
variable *name* — a pasted key does not look like one, and is refused.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from pz_agent_core.capabilities import PROBES_BY_NAME
from pz_agent_core.knowledge import GAMEPLAY_SUBDIR, default_corpus_root
from pz_agent_core.planner.providers import (
    DEFAULT_CONNECT_TIMEOUT_S,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_OPENAI_KEY_ENV,
    DEFAULT_READ_TIMEOUT_S,
    DEFAULT_TEAMON_KEY_ENV,
    MAX_ATTEMPTS,
    MAX_OUTPUT_TOKENS,
    MAX_RESPONSE_BYTES,
    MAX_TIMEOUT_S,
    PROVIDER_OPENAI_COMPATIBLE,
    PROVIDER_TEAMON,
    OpenAICompatibleConfig,
    TeamONConfig,
    TransportConfig,
    ensure_env_name,
    parse_endpoint,
)
from pz_agent_core.protocol import JsonDict, SessionMode
from pz_agent_core.version import SUPPORTED_BUILDS

#: A configuration file is a page of TOML. Anything larger is not one.
MAX_CONFIG_BYTES: Final = 256 * 1024

#: Blueprint §7: the autonomous radius is bounded by the product, not by taste.
#: Configuration may lower it and may never raise it past this.
MAX_AUTONOMOUS_RADIUS: Final = 100

#: Blueprint §7.4: plans are short by design.
MAX_PLAN_STEPS: Final = 32

MAX_HOTKEY_LEN: Final = 16

#: The key the mod actually binds. ``PZAgent_Main.lua`` hardcodes DirectInput
#: scancode 88 — F12 — deliberately, rather than reading a ``Keyboard`` global
#: whose presence in build 42.20 nobody has probed. Named here so the one place
#: that refuses another value and the one place that documents it cannot drift
#: apart. Rebinding needs the mod to read a published keycode *and* a live run
#: to prove the new key reaches the stop; until both exist, saying so is the
#: honest answer and silently accepting the setting is not.
BOUND_PANIC_HOTKEY: Final = "F12"

#: Providers this build can actually run. ``none`` — the deterministic path —
#: stays the default: it needs no network, no key and no endpoint, and it is the
#: only one that keeps working when the user is offline. The other two each
#: require their own section, and selecting one without filling that section in
#: is an error rather than a silent fall back to ``none``.
SUPPORTED_PROVIDERS: Final = ("none", PROVIDER_OPENAI_COMPATIBLE, PROVIDER_TEAMON)

#: Which table a provider's settings live in. Also the set of providers that
#: have one: ``none`` is absent because it has nothing to configure.
PROVIDER_TABLES: Final[Mapping[str, str]] = {
    PROVIDER_OPENAI_COMPATIBLE: f"planner.{PROVIDER_OPENAI_COMPATIBLE}",
    PROVIDER_TEAMON: f"planner.{PROVIDER_TEAMON}",
}

#: Keys with no usable default, per provider. A model name cannot be guessed and
#: an endpoint cannot be assumed, so selecting the provider without them is a
#: configuration that could only fail at the first request.
REQUIRED_PROVIDER_KEYS: Final[Mapping[str, tuple[str, ...]]] = {
    PROVIDER_OPENAI_COMPATIBLE: ("base_url", "model"),
    PROVIDER_TEAMON: ("base_url",),
}

SUPPORTED_CHANNELS: Final = ("stable", "unstable")

#: The voice backends a shipped configuration may select. Named as constants
#: because :mod:`pz_agent_cli.voice` dispatches on them, and a second spelling of
#: "teamon" there would be an adapter that validates and never gets built.
#: ``FakeVoiceAdapter`` is deliberately absent and must stay absent: it answers
#: scripted transcripts, so selecting it would leave a user talking to a test
#: double while the command reported that the companion was listening.
ADAPTER_TEAMON: Final = "teamon"
ADAPTER_NONE: Final = "none"

SUPPORTED_VOICE_ADAPTERS: Final = (ADAPTER_TEAMON, ADAPTER_NONE)

#: Problem codes. Stable, because they are what a script or a bug report cites.
CODE_UNKNOWN_TABLE: Final = "UNKNOWN_TABLE"
CODE_UNKNOWN_KEY: Final = "UNKNOWN_KEY"
CODE_TYPE_MISMATCH: Final = "TYPE_MISMATCH"
CODE_OUT_OF_RANGE: Final = "OUT_OF_RANGE"
CODE_NOT_ALLOWED: Final = "NOT_ALLOWED"
CODE_PARSE_ERROR: Final = "PARSE_ERROR"
CODE_NOT_FOUND: Final = "NOT_FOUND"
CODE_MISSING_VALUE: Final = "MISSING_VALUE"
CODE_TOO_LARGE: Final = "TOO_LARGE"
CODE_UNREADABLE: Final = "UNREADABLE"


class ConfigError(ValueError):
    """A configuration document could not be turned into an :class:`AgentConfig`."""


@dataclass(frozen=True, slots=True)
class ConfigProblem:
    """One finding, addressed by its dotted path so it can be found by eye."""

    path: str
    code: str
    detail: str
    remediation: str

    def __post_init__(self) -> None:
        if not self.remediation:
            raise ConfigError(f"{self.path}: a problem must say what to do about it")

    def render(self) -> str:
        return f"{self.path}: [{self.code}] {self.detail}\n    -> {self.remediation}"

    def to_dict(self) -> JsonDict:
        return {
            "path": self.path,
            "code": self.code,
            "detail": self.detail,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One key: its type, its default and the values it will accept."""

    kind: str
    default: Any
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    nullable: bool = False


_STR: Final = "string"
_BOOL: Final = "boolean"
_INT: Final = "integer"

#: An array of quoted strings. One key uses it — ``safety.disabled_capabilities``
#: — and it exists because that key is a *set* of names: spelling it as a
#: comma-joined string would make "did I get the separator right" a thing the
#: user has to guess about a safety control.
_STR_LIST: Final = "string-list"

#: Entries one list-valued key may hold. Twelve probes exist; the bound is well
#: clear of that and keeps a pasted file from becoming an unbounded loop.
MAX_LIST_ITEMS: Final = 64

#: Capability names ``safety.disabled_capabilities`` will accept, taken from the
#: probe table rather than restated, so a probe added tomorrow is switchable off
#: the same day and a name this file invented could never validate.
KNOWN_CAPABILITIES: Final = tuple(sorted(PROBES_BY_NAME))


def _transport_keys() -> dict[str, FieldSpec]:
    """The bounds every HTTP-backed provider shares.

    Milliseconds rather than seconds because TOML integers are the one numeric
    type this validator handles, and a timeout written as ``0.5`` would be
    refused as a type error for a value the user got right.
    """
    return {
        "connect_timeout_ms": FieldSpec(
            _INT,
            int(DEFAULT_CONNECT_TIMEOUT_S * 1000),
            minimum=1,
            maximum=int(MAX_TIMEOUT_S * 1000),
        ),
        "read_timeout_ms": FieldSpec(
            _INT, int(DEFAULT_READ_TIMEOUT_S * 1000), minimum=1, maximum=int(MAX_TIMEOUT_S * 1000)
        ),
        "max_response_bytes": FieldSpec(
            _INT, DEFAULT_MAX_RESPONSE_BYTES, minimum=1, maximum=MAX_RESPONSE_BYTES
        ),
        "max_attempts": FieldSpec(_INT, DEFAULT_MAX_ATTEMPTS, minimum=1, maximum=MAX_ATTEMPTS),
    }


#: The whole documented surface. Anything not named here is a typo by
#: definition, which is what makes the unknown-key rule enforceable.
#:
#: A dotted name is a sub-table — ``[planner.teamon]`` in the file. Providers get
#: one each so that the settings for the provider you are *not* using can sit in
#: the file, validated, without being read.
SCHEMA: Final[Mapping[str, Mapping[str, FieldSpec]]] = {
    "game": {
        "channel": FieldSpec(_STR, "stable", choices=SUPPORTED_CHANNELS),
        "expected_build": FieldSpec(_STR, SUPPORTED_BUILDS[0]),
        # Not in the QUICKSTART sample because a Steam install needs neither:
        # TROUBLESHOOTING tells the owner of a GOG or manual copy to point at it
        # explicitly, and this is the key that does it.
        "install_dir": FieldSpec(_STR, None, nullable=True),
        "user_dir": FieldSpec(_STR, None, nullable=True),
    },
    "session": {
        "default_mode": FieldSpec(_STR, "observe", choices=("observe", "assisted", "autonomous")),
        "require_backup": FieldSpec(_BOOL, True),
    },
    "safety": {
        "panic_hotkey": FieldSpec(_STR, "F12"),
        "manual_takeover": FieldSpec(_BOOL, True),
        "max_autonomous_radius": FieldSpec(_INT, 30, minimum=1, maximum=MAX_AUTONOMOUS_RADIUS),
        "allow_multiplayer": FieldSpec(_BOOL, False),
        "disabled_capabilities": FieldSpec(_STR_LIST, [], choices=KNOWN_CAPABILITIES),
    },
    "planner": {
        "provider": FieldSpec(_STR, "none", choices=SUPPORTED_PROVIDERS),
        "max_steps": FieldSpec(_INT, 8, minimum=1, maximum=MAX_PLAN_STEPS),
    },
    f"planner.{PROVIDER_OPENAI_COMPATIBLE}": {
        "base_url": FieldSpec(_STR, None, nullable=True),
        "model": FieldSpec(_STR, None, nullable=True),
        # The name of a variable, never the key. `_check_value` enforces the
        # shape, and the shape is what tells a user who pasted their key here
        # that they have put a secret in a file they may well share.
        "api_key_env": FieldSpec(_STR, DEFAULT_OPENAI_KEY_ENV),
        "max_output_tokens": FieldSpec(
            _INT, DEFAULT_MAX_OUTPUT_TOKENS, minimum=1, maximum=MAX_OUTPUT_TOKENS
        ),
        # The directory holding knowledge/gameplay, for the prompt's bounded
        # knowledge block. Unset means the corpus shipped next to this source
        # tree when there is one, and honestly none when there is not — an
        # installed build without the tree sends the same prompt it always did.
        "knowledge_root": FieldSpec(_STR, None, nullable=True),
        **_transport_keys(),
    },
    f"planner.{PROVIDER_TEAMON}": {
        "base_url": FieldSpec(_STR, None, nullable=True),
        "assistant": FieldSpec(_STR, None, nullable=True),
        "api_key_env": FieldSpec(_STR, DEFAULT_TEAMON_KEY_ENV),
        # Same knob as the OpenAI-compatible provider's: the two share the
        # planner payload, so they share the knowledge block wired into it.
        "knowledge_root": FieldSpec(_STR, None, nullable=True),
        **_transport_keys(),
    },
    "voice": {
        "adapter": FieldSpec(_STR, ADAPTER_TEAMON, choices=SUPPORTED_VOICE_ADAPTERS),
        "enabled": FieldSpec(_BOOL, False),
        # The same rule and the same validator as the planner's providers: this
        # names the variable holding the key, never the key. The voice backend
        # and the planner are the same vendor, so the default is the same
        # variable — a user running both sets it once.
        "api_key_env": FieldSpec(_STR, DEFAULT_TEAMON_KEY_ENV),
    },
}


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """A validated configuration. Constructed only from a document that passed."""

    values: Mapping[str, Mapping[str, Any]]

    def get(self, table: str, key: str) -> Any:
        return self.values[table][key]

    @property
    def default_mode(self) -> SessionMode:
        return SessionMode(str(self.get("session", "default_mode")).upper())

    @property
    def planner_provider(self) -> OpenAICompatibleConfig | TeamONConfig | None:
        """The selected provider's typed config, or None for ``provider = "none"``.

        Safe to build without catching: an :class:`AgentConfig` only exists for
        a document whose provider section already validated.
        """
        provider = str(self.get("planner", "provider"))
        table = PROVIDER_TABLES.get(provider)
        if table is None:
            return None
        return build_provider_config(provider, self.values[table])

    @property
    def install_dir(self) -> Path | None:
        raw = self.get("game", "install_dir")
        return None if raw is None else Path(str(raw))

    @property
    def user_dir(self) -> Path | None:
        raw = self.get("game", "user_dir")
        return None if raw is None else Path(str(raw))

    def to_dict(self) -> JsonDict:
        return {table: dict(keys) for table, keys in self.values.items()}

    def to_toml(self) -> str:
        """Render the validated configuration back as TOML, for the support bundle."""
        lines: list[str] = []
        for table, keys in self.values.items():
            lines.append(f"[{table}]")
            for key, value in keys.items():
                if value is None:
                    continue
                lines.append(f"{key} = {_toml_scalar(value)}")
            lines.append("")
        return "\n".join(lines)


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        # Rendered as an array rather than as str(list), which produces
        # ``"['a']"`` — a quoted string that parses, validates as the wrong type
        # and makes the support bundle's copy of the file disagree with the
        # user's. What this method renders has to load again as what went in.
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    return _toml_basic_string(str(value))


#: The control characters TOML's basic-string grammar gives a short escape.
#: Any other control character has to travel as ``\uXXXX``; a raw one — a
#: newline inside ``game.install_dir``, say — is a byte TOML forbids in a basic
#: string, so a config that validated could not be loaded back from its own
#: rendering. Backslash and quote lead because the later passes must not double
#: an escape they introduced.
_TOML_SHORT_ESCAPES: Final = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _toml_basic_string(text: str) -> str:
    """Render *text* as a TOML basic string that loads back as itself.

    Every control character is escaped — the seven with a short form by name,
    the rest as ``\\uXXXX`` — because ``to_toml`` feeds the support bundle a
    copy of the config that has to parse, and a literal control byte in a basic
    string does not.
    """
    out: list[str] = []
    for char in text:
        short = _TOML_SHORT_ESCAPES.get(char)
        if short is not None:
            out.append(short)
        elif char < " " or char == "\x7f":
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


@dataclass(frozen=True, slots=True)
class ConfigValidation:
    """The outcome. ``config`` is present only when there are no errors."""

    path: Path
    config: AgentConfig | None = None
    errors: tuple[ConfigProblem, ...] = ()
    warnings: tuple[ConfigProblem, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> JsonDict:
        return {
            "ok": self.ok,
            "errors": [problem.to_dict() for problem in self.errors],
            "warnings": [problem.to_dict() for problem in self.warnings],
            "config": None if self.config is None else self.config.to_dict(),
        }


def default_config() -> AgentConfig:
    """The documented defaults, as a validated configuration."""
    return AgentConfig(
        values={
            table: {key: spec.default for key, spec in keys.items()}
            for table, keys in SCHEMA.items()
        }
    )


def _read(path: Path) -> tuple[str | None, ConfigProblem | None]:
    """Read the file, bounded. Returns ``(text, problem)``; exactly one is set."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, ConfigProblem(
            path=str(path.name),
            code=CODE_NOT_FOUND,
            detail="no configuration file at this path",
            remediation=(
                # Names the file rather than a document. QUICKSTART.md shows a
                # fragment and never names config.example.toml, so an operator
                # whose very first `start` failed was sent to a page that does
                # not contain the thing the message told them to copy. Both
                # paths below ship in the release archive, and install.bat
                # performs this copy itself.
                "copy configs/agent/config.example.toml to this path — install.bat does "
                "it for you — or pass --config with the path to yours"
            ),
        )
    except OSError as exc:
        return None, ConfigProblem(
            path=str(path.name),
            code=CODE_UNREADABLE,
            detail=f"cannot read the file ({exc.strerror or exc})",
            remediation="check the file's permissions, then run validate-config again",
        )
    if len(raw) > MAX_CONFIG_BYTES:
        return None, ConfigProblem(
            path=str(path.name),
            code=CODE_TOO_LARGE,
            detail=f"{len(raw)} bytes exceeds the {MAX_CONFIG_BYTES} byte limit",
            remediation="point --config at the agent's configuration rather than another file",
        )
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        return None, ConfigProblem(
            path=str(path.name),
            code=CODE_PARSE_ERROR,
            detail=f"not valid UTF-8 ({exc.reason})",
            remediation="save the file as UTF-8; TOML has no other encoding",
        )


def validate_document(document: Mapping[str, Any], *, path: Path) -> ConfigValidation:
    """Validate an already-parsed TOML document.

    Every table and key is checked against :data:`SCHEMA`; anything absent takes
    its documented default, and anything unrecognised is an error.
    """
    errors: list[ConfigProblem] = []
    warnings: list[ConfigProblem] = []
    values: dict[str, dict[str, Any]] = {
        table: {key: spec.default for key, spec in keys.items()} for table, keys in SCHEMA.items()
    }

    for table, body in document.items():
        if table not in SCHEMA:
            errors.append(_unknown_table(table))
            continue
        errors.extend(_check_table(table, body, values))

    errors.extend(_provider_problems(values))
    errors.extend(_forbidden(values))
    warnings.extend(_advisories(values))
    config = None if errors else AgentConfig(values={t: dict(k) for t, k in values.items()})
    return ConfigValidation(
        path=path,
        config=config,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def load_config(path: Path) -> ConfigValidation:
    """Read and validate the configuration at *path*.

    A missing file is an error here rather than a silent fall back to defaults:
    the user named a file, and reporting "valid" for one that does not exist is
    the same class of lie as ignoring an unknown key.
    """
    text, problem = _read(path)
    if text is None:
        return (
            ConfigValidation(path=path, errors=(problem,))
            if problem
            else ConfigValidation(path=path)
        )
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return ConfigValidation(
            path=path,
            errors=(
                ConfigProblem(
                    path=path.name,
                    code=CODE_PARSE_ERROR,
                    detail=f"not valid TOML ({exc})",
                    remediation="fix the syntax the message points at, then run this again",
                ),
            ),
        )
    return validate_document(document, path=path)


def _check_table(table: str, body: Any, values: dict[str, dict[str, Any]]) -> list[ConfigProblem]:
    """Validate one section into *values*, recursing into its sub-tables.

    A sub-table is a key whose dotted name is itself in :data:`SCHEMA`. It is
    dispatched before the key lookup, so ``[planner.teamon]`` is a section
    rather than an unknown key in ``[planner]``.
    """
    if not isinstance(body, dict):
        return [
            ConfigProblem(
                path=table,
                code=CODE_TYPE_MISMATCH,
                detail=f"expected a table, got {type(body).__name__}",
                remediation=f"write it as a section header: [{table}]",
            )
        ]
    specs = SCHEMA[table]
    problems: list[ConfigProblem] = []
    for key, raw in body.items():
        nested = f"{table}.{key}"
        if nested in SCHEMA:
            problems.extend(_check_table(nested, raw, values))
            continue
        spec = specs.get(key)
        if spec is None:
            problems.append(_unknown_key(table, key, specs))
            continue
        checked, problem = _check_value(nested, raw, spec)
        if problem is not None:
            problems.append(problem)
            continue
        values[table][key] = checked
    return problems


def _unknown_table(table: str) -> ConfigProblem:
    known = ", ".join(sorted(SCHEMA))
    return ConfigProblem(
        path=table,
        code=CODE_UNKNOWN_TABLE,
        detail=f"no such section; this build knows {known}",
        remediation=(
            f"remove [{table}] or correct its spelling — a mistyped section header "
            "silently discards every setting inside it"
        ),
    )


def _unknown_key(table: str, key: str, specs: Mapping[str, FieldSpec]) -> ConfigProblem:
    suggestion = _closest(key, tuple(specs))
    hint = f"; did you mean {suggestion!r}?" if suggestion else ""
    return ConfigProblem(
        path=f"{table}.{key}",
        code=CODE_UNKNOWN_KEY,
        detail=f"no such key in [{table}]{hint}",
        remediation=(
            f"remove it or correct the spelling; keys in [{table}] are {', '.join(sorted(specs))}"
        ),
    )


def _closest(key: str, candidates: Sequence[str]) -> str | None:
    """The candidate sharing the longest prefix with *key*, when it is close.

    Deliberately not a general edit distance: the failure this serves is a
    dropped or doubled letter in a long name, and a prefix rule names it without
    pulling in a similarity metric that could suggest a different setting.
    """
    best: tuple[int, str] | None = None
    for candidate in candidates:
        shared = 0
        for left, right in zip(key, candidate, strict=False):
            if left != right:
                break
            shared += 1
        if shared >= 4 and (best is None or shared > best[0]):
            best = (shared, candidate)
    return None if best is None else best[1]


def _check_list(dotted: str, raw: Any, spec: FieldSpec) -> tuple[Any, ConfigProblem | None]:
    """One array of quoted strings, checked against the names it may hold.

    A misspelt entry is an error rather than an ignored line. The key names
    capabilities to switch *off*, so an entry that matches nothing would leave
    the user believing they disabled something and leave the agent free to use
    it — the same failure the module docstring refuses for unknown keys, and
    with more at stake.
    """
    if not isinstance(raw, list):
        return None, _type_problem(dotted, raw, "an array of quoted strings")
    if len(raw) > MAX_LIST_ITEMS:
        return None, ConfigProblem(
            path=dotted,
            code=CODE_OUT_OF_RANGE,
            detail=f"{len(raw)} entries; the most this key takes is {MAX_LIST_ITEMS}",
            remediation=f"list at most {MAX_LIST_ITEMS} names",
        )
    cleaned: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry:
            return None, _type_problem(dotted, entry, "a quoted string")
        if spec.choices and entry not in spec.choices:
            return None, ConfigProblem(
                path=dotted,
                code=CODE_NOT_ALLOWED,
                detail=f"{entry!r} is not a capability this build knows",
                remediation=(
                    "use one of: " + ", ".join(spec.choices) + ". Run 'pz-agent doctor' to "
                    "see the state each one is currently in"
                ),
            )
        if entry not in cleaned:
            cleaned.append(entry)
    return cleaned, None


def _check_value(dotted: str, raw: Any, spec: FieldSpec) -> tuple[Any, ConfigProblem | None]:
    """Type-, choice- and range-check one value."""
    if raw is None and spec.nullable:
        return None, None
    if spec.kind == _STR_LIST:
        return _check_list(dotted, raw, spec)
    if spec.kind == _BOOL:
        if not isinstance(raw, bool):
            return None, _type_problem(dotted, raw, "true or false")
        return raw, None
    if spec.kind == _INT:
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None, _type_problem(dotted, raw, "a whole number")
        if spec.minimum is not None and raw < spec.minimum:
            return None, _range_problem(dotted, raw, spec)
        if spec.maximum is not None and raw > spec.maximum:
            return None, _range_problem(dotted, raw, spec)
        return raw, None
    if not isinstance(raw, str):
        return None, _type_problem(dotted, raw, "a quoted string")
    if spec.choices and raw.lower() not in spec.choices:
        return None, ConfigProblem(
            path=dotted,
            code=CODE_NOT_ALLOWED,
            detail=f"{raw!r} is not one of {', '.join(spec.choices)}",
            remediation=f"set it to one of: {', '.join(spec.choices)}",
        )
    if dotted == "safety.panic_hotkey":
        problem = _check_hotkey(dotted, raw)
        if problem is not None:
            return None, problem
    if dotted.endswith(".base_url"):
        problem = _refused(
            dotted,
            parse_endpoint,
            raw,
            "give the endpoint's root, as in "
            "http://127.0.0.1:8080 — the path is appended by the provider",
        )
        if problem is not None:
            return None, problem
    if dotted.endswith(".api_key_env"):
        problem = _refused(
            dotted,
            ensure_env_name,
            raw,
            "name the environment variable that "
            "holds the key, such as PZ_AGENT_OPENAI_API_KEY, and set the key "
            "in your environment — a key written in this file is a key in every "
            "copy of this file",
        )
        if problem is not None:
            return None, problem
    return raw.lower() if spec.choices else raw, None


def _refused(
    dotted: str, check: Callable[[str], object], raw: str, remediation: str
) -> ConfigProblem | None:
    """Run one of the providers' own validators and report what it said.

    The rule stays where it is enforced at runtime; this only moves the moment
    it fires to before the session starts.
    """
    try:
        check(raw)
    except ValueError as exc:
        return ConfigProblem(
            path=dotted, code=CODE_NOT_ALLOWED, detail=str(exc), remediation=remediation
        )
    return None


def _check_hotkey(dotted: str, raw: str) -> ConfigProblem | None:
    if raw and len(raw) <= MAX_HOTKEY_LEN and raw.isalnum():
        return None
    return ConfigProblem(
        path=dotted,
        code=CODE_NOT_ALLOWED,
        detail=f"{raw!r} is not a single key name",
        remediation="use a plain key name such as F12 — combinations are not supported",
    )


def _type_problem(dotted: str, raw: Any, expected: str) -> ConfigProblem:
    return ConfigProblem(
        path=dotted,
        code=CODE_TYPE_MISMATCH,
        detail=f"expected {expected}, got {type(raw).__name__}",
        remediation=f"write {dotted.rsplit('.', maxsplit=1)[-1]} as {expected}",
    )


def _range_problem(dotted: str, raw: int, spec: FieldSpec) -> ConfigProblem:
    low = "" if spec.minimum is None else str(spec.minimum)
    high = "" if spec.maximum is None else str(spec.maximum)
    return ConfigProblem(
        path=dotted,
        code=CODE_OUT_OF_RANGE,
        detail=f"{raw} is outside {low}..{high}",
        remediation=(
            f"choose a value between {low} and {high}; the upper bound is a product "
            "limit and cannot be raised by configuration"
        ),
    )


def _provider_problems(values: Mapping[str, Mapping[str, Any]]) -> list[ConfigProblem]:
    """Check the selected provider's section, by building its typed config.

    Only the *selected* provider is built. A half-filled section for the
    provider the user is not running is not a reason to refuse to start, and
    treating it as one would punish keeping two endpoints in one file.
    """
    provider = str(values["planner"]["provider"])
    table = PROVIDER_TABLES.get(provider)
    if table is None:
        return []
    section = values[table]
    missing = [
        ConfigProblem(
            path=f"{table}.{key}",
            code=CODE_MISSING_VALUE,
            detail=f"planner.provider is {provider!r} and this key has no value",
            remediation=f'set {key} under [{table}], or set planner.provider = "none"',
        )
        for key in REQUIRED_PROVIDER_KEYS[provider]
        if not str(section.get(key) or "").strip()
    ]
    if missing:
        return missing
    problems = _knowledge_problems(table, section)
    if problems:
        return problems
    try:
        build_provider_config(provider, section)
    except ValueError as exc:
        return [
            ConfigProblem(
                path=table,
                code=CODE_NOT_ALLOWED,
                detail=str(exc),
                remediation=f"correct the offending key in [{table}]",
            )
        ]
    return []


def _knowledge_problems(table: str, section: Mapping[str, Any]) -> list[ConfigProblem]:
    """An explicitly set ``knowledge_root`` must actually hold a corpus.

    Only the explicit value is checked: the unset default resolves to the
    shipped corpus or to honest None, and neither of those is a mistake the
    user made. A named root without ``knowledge/gameplay`` under it would load
    as a silently empty knowledge base — the user believing rules are wired
    in while the prompt carries none — which is the same class of lie as an
    ignored unknown key, so it is an error rather than a shrug.
    """
    raw = section.get("knowledge_root")
    if raw is None or not str(raw).strip():
        return []
    corpus_dir = "/".join(GAMEPLAY_SUBDIR)
    try:
        found = Path(str(raw)).joinpath(*GAMEPLAY_SUBDIR).is_dir()
    except (OSError, ValueError):
        # Discarding the OS's own complaint (an embedded NUL, an over-long
        # name): "no corpus at this path" is the answer either way, and the
        # remediation below says more than the errno would.
        found = False
    if found:
        return []
    return [
        ConfigProblem(
            path=f"{table}.knowledge_root",
            code=CODE_NOT_FOUND,
            detail=f"no {corpus_dir} directory under this path",
            remediation=(
                f"point it at a directory containing {corpus_dir}, or remove the key "
                "to use the corpus shipped with the source tree when there is one"
            ),
        )
    ]


def _knowledge_root(section: Mapping[str, Any]) -> Path | None:
    """The corpus root the provider should read.

    The explicit value wins; an unset one falls back to
    :func:`~pz_agent_core.knowledge.retrieval.default_corpus_root`, which is
    the repo-shipped ``knowledge/gameplay`` in a source checkout and an honest
    None in an installed build, where no corpus ships and no block is sent.
    """
    raw = section.get("knowledge_root")
    if raw is None or not str(raw).strip():
        return default_corpus_root()
    return Path(str(raw))


def build_provider_config(
    provider: str, section: Mapping[str, Any]
) -> OpenAICompatibleConfig | TeamONConfig | None:
    """The typed provider config for *section*, or None for ``provider = "none"``.

    Raises:
        ValueError: from the provider's own dataclass, which owns every bound
            here — this function converts units and nothing else.
    """
    transport = TransportConfig(
        connect_timeout_s=int(section["connect_timeout_ms"]) / 1000,
        read_timeout_s=int(section["read_timeout_ms"]) / 1000,
        max_response_bytes=int(section["max_response_bytes"]),
        max_attempts=int(section["max_attempts"]),
    )
    if provider == PROVIDER_OPENAI_COMPATIBLE:
        return OpenAICompatibleConfig(
            base_url=str(section["base_url"]),
            model=str(section["model"]),
            api_key_env=str(section["api_key_env"]),
            max_output_tokens=int(section["max_output_tokens"]),
            transport=transport,
            knowledge_root=_knowledge_root(section),
        )
    if provider == PROVIDER_TEAMON:
        return TeamONConfig(
            base_url=str(section["base_url"]),
            assistant=str(section["assistant"] or ""),
            api_key_env=str(section["api_key_env"]),
            transport=transport,
            knowledge_root=_knowledge_root(section),
        )
    return None


def _forbidden(values: Mapping[str, Mapping[str, Any]]) -> list[ConfigProblem]:
    """Settings this build refuses to load at all.

    ``allow_multiplayer`` used to sit in :func:`_advisories`, whose contract is
    "never errors", carrying the sentence "multiplayer is refused at the
    handshake regardless of this setting". No such refusal existed — not in the
    sidecar, not in the mod, nowhere. So the warning was false and the setting
    was exactly the bypass it claimed not to be: turn it on, get a line of
    advice, and proceed.

    It is an error now, and :meth:`SidecarRuntime.arm` refuses any session the
    mod does not positively report as single player. Two gates rather than one,
    because a configuration check protects the person who reads the file and
    the arm check protects the person who does not.
    """
    problems: list[ConfigProblem] = []
    if values["safety"]["allow_multiplayer"]:
        problems.append(
            ConfigProblem(
                path="safety.allow_multiplayer",
                code=CODE_NOT_ALLOWED,
                detail=(
                    "this build does not act in multiplayer, and this setting does not "
                    "make it. It is refused rather than warned about, because a flag that "
                    "loads is a flag someone will rely on."
                ),
                remediation="remove it, or set it to false",
            )
        )
    hotkey = str(values["safety"]["panic_hotkey"])
    if hotkey.upper() != BOUND_PANIC_HOTKEY:
        problems.append(
            ConfigProblem(
                path="safety.panic_hotkey",
                code=CODE_NOT_ALLOWED,
                detail=(
                    f"this build's panic key is fixed at {BOUND_PANIC_HOTKEY}. The mod binds "
                    f"its scancode directly and reads no configuration, so {hotkey!r} would "
                    "bind nothing — and this is the stop button, which is the last setting "
                    "that may appear to work and not"
                ),
                remediation=(
                    f'set it to "{BOUND_PANIC_HOTKEY}". To stop the agent by another route, '
                    "run 'pz-agent stop', or create the file panic.stop in the exchange "
                    "directory — the mod obeys it while it is present"
                ),
            )
        )
    return problems


def _advisories(values: Mapping[str, Mapping[str, Any]]) -> list[ConfigProblem]:
    """Settings that validate but deserve a word. Never errors."""
    problems: list[ConfigProblem] = []
    if not values["session"]["require_backup"]:
        problems.append(
            ConfigProblem(
                path="session.require_backup",
                code=CODE_NOT_ALLOWED,
                detail="the first autonomous run on a save will proceed without a verified backup",
                remediation="leave it true, or run pz-agent backup-save yourself before arming",
            )
        )
    if values["voice"]["enabled"] and values["voice"]["adapter"] == ADAPTER_NONE:
        problems.append(
            ConfigProblem(
                path="voice.enabled",
                code=CODE_NOT_ALLOWED,
                detail='voice.adapter is "none", so nothing will listen however this is set',
                remediation=(
                    f'set voice.adapter = "{ADAPTER_TEAMON}", or set voice.enabled = false '
                    "so the file says what happens"
                ),
            )
        )
    elif values["voice"]["enabled"]:
        # Not a problem with the file: it is the one thing about voice that the
        # file cannot say. Nothing starts the companion — not the sidecar, not
        # arming — so a user who set this and heard nothing needs the command,
        # and the advisory is where they will be looking.
        problems.append(
            ConfigProblem(
                path="voice.enabled",
                code=CODE_NOT_ALLOWED,
                detail="voice is enabled, but no process starts the companion on its own",
                remediation=(
                    "run 'pz-agent voice run' in its own terminal; 'pz-agent voice check "
                    "<phrase>' says what a phrase resolves to without one"
                ),
            )
        )
    expected = str(values["game"]["expected_build"])
    if expected not in SUPPORTED_BUILDS:
        problems.append(
            ConfigProblem(
                path="game.expected_build",
                code=CODE_NOT_ALLOWED,
                detail=f"{expected} is outside the builds this release was designed against",
                remediation=(
                    f"set it to one of {', '.join(SUPPORTED_BUILDS)}, or expect every "
                    "capability to be reported unverified"
                ),
            )
        )
    return problems
