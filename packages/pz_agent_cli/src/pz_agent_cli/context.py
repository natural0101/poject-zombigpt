"""Everything a command is allowed to reach outside itself.

No handler in this package calls :func:`os.environ`, :func:`sys.stdout` or
:func:`datetime.now` directly. They arrive in a :class:`CliContext`, which is
what makes the whole CLI drivable in-process from a test: the same code path a
user runs is exercised against a temporary tree, a fake environment and a
captured stream, rather than a subprocess whose output is all a test can see.

:func:`resolve_workspace` is the one place that turns discovery into the
concrete directories a command writes to, so "where does the log live" has a
single answer that the doctor, the bundle builder and the logs command all read
from the same object.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TextIO

from pz_agent_core.diagnostics import Redactor, build_redactor
from pz_agent_core.ipc.clocks import Clock, system_clock_ms
from pz_agent_core.platform.discovery import Discovery, DiscoveryContext, discover

# No cycle: config.py imports nothing from this package — it is a pure reader of
# one TOML document, which is why it can be consulted this early.
from .config import load_config

#: Exit code for a command that did what it was asked.
EXIT_OK: Final = 0

#: Exit code for a command that ran and reported a problem — a doctor check
#: that failed, a configuration that did not validate, a refused install.
EXIT_FAILURE: Final = 1

#: Exit code for a malformed invocation. Matches argparse's own.
EXIT_USAGE: Final = 2

#: Directory pz-agent keeps its own state in, under the ``Zomboid`` directory
#: when one was found. The game never reads it; it is beside the save data so a
#: user who moves their profile takes the diagnostics with it.
STATE_DIR_NAME: Final = "pz-agent"

CONFIG_NAME: Final = "config.toml"

#: The mod/sidecar exchange directory, relative to the ``Zomboid`` directory
#: (blueprint §3.2). Mirrored by ``PZAgent.Ipc.ROOT`` on the Lua side.
IPC_SUBPATH: Final = ("Lua", "pz_agent")

LOGS_DIR_NAME: Final = "logs"
TRACE_DIR_NAME: Final = "traces"
BUNDLE_DIR_NAME: Final = "bundles"
BACKUP_DIR_NAME: Final = "backups"
COMPAT_DIR_NAME: Final = "compat"

#: The one trace a workspace keeps, inside :data:`TRACE_DIR_NAME`. Named here
#: rather than in ``app`` because ``pz-agent replay`` takes a path from the
#: operator, and a path they cannot predict is one they cannot type.
TRACE_NAME: Final = "session.jsonl"

CAPABILITY_REPORT_NAME: Final = "generated_api_report.json"


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(frozen=True, slots=True)
class CliContext:
    """The process boundary, injected.

    ``fallback_dir`` is where state goes when no ``Zomboid`` directory was
    found. Without it, a first run on a machine with no game installed would
    have nowhere to write the diagnostics that explain why.
    """

    discovery: DiscoveryContext
    stdout: TextIO
    stderr: TextIO
    env: Mapping[str, str] = field(default_factory=dict)
    clock_ms: Clock = system_clock_ms
    now: Callable[[], datetime] = _utc_now
    fallback_dir: Path = field(default_factory=Path.cwd)
    state_dir_override: Path | None = None
    config_override: Path | None = None
    mod_source_override: Path | None = None

    @classmethod
    def from_process(cls) -> CliContext:
        """Build a context from this process. The only place the CLI reads globals."""
        discovery = DiscoveryContext.from_process()
        return cls(
            discovery=discovery,
            stdout=sys.stdout,
            stderr=sys.stderr,
            env=discovery.env,
            fallback_dir=Path.cwd(),
        )

    def with_overrides(
        self,
        *,
        state_dir: Path | None = None,
        config: Path | None = None,
        mod_source: Path | None = None,
        install_dir: Path | None = None,
        zomboid_dir: Path | None = None,
    ) -> CliContext:
        """A copy carrying the global command-line overrides."""
        discovery = self.discovery
        if install_dir is not None or zomboid_dir is not None:
            discovery = DiscoveryContext(
                root=discovery.root,
                env=discovery.env,
                steam_root_hints=discovery.steam_root_hints,
                install_override=install_dir or discovery.install_override,
                home_override=discovery.home_override,
                zomboid_dir_override=zomboid_dir or discovery.zomboid_dir_override,
            )
        return CliContext(
            discovery=discovery,
            stdout=self.stdout,
            stderr=self.stderr,
            env=self.env,
            clock_ms=self.clock_ms,
            now=self.now,
            fallback_dir=self.fallback_dir,
            state_dir_override=state_dir or self.state_dir_override,
            config_override=config or self.config_override,
            mod_source_override=mod_source or self.mod_source_override,
        )


@dataclass(frozen=True, slots=True)
class Workspace:
    """Where this machine's game, state and exchange directories are."""

    discovery: Discovery
    state_dir: Path
    logs_dir: Path
    trace_dir: Path
    bundle_dir: Path
    backup_root: Path
    compat_dir: Path
    config_path: Path
    redactor: Redactor
    ipc_root: Path | None = None
    mods_dir: Path | None = None

    @property
    def user_dir(self) -> Path | None:
        return self.discovery.user_dir.path

    @property
    def install_dir(self) -> Path | None:
        return self.discovery.install.path

    @property
    def capability_report_path(self) -> Path:
        return self.compat_dir / CAPABILITY_REPORT_NAME

    def redact(self, path: Path | None) -> str:
        """Render a path for output with the user's directories replaced."""
        return "" if path is None else self.redactor.path(path)


def _configured(ctx: CliContext, found: Discovery) -> CliContext:
    """Re-apply ``[game]`` from ``config.toml`` as a discovery override.

    ``game.install_dir`` and ``game.user_dir`` were parsed, validated, typed and
    read by nothing. Meanwhile ``doctor``'s own remediation for ``PZD001`` says
    "set install_dir under [game] in config.toml", ``docs/TROUBLESHOOTING.md``
    says it for ``PZD001`` and ``PZD003``, and ``configs/mcp/README.md`` names
    both keys — so the documented escape hatch for the two failures that brick
    every other command (a GOG or manual copy Steam does not list, a profile
    moved by OneDrive or ``-cachedir``) did nothing at all. A user who followed
    it got "configuration is valid" and the identical failure.

    Precedence is command line, then configuration, then discovery: a flag is
    the more specific statement and is passed for this one invocation, so it
    must not be silently replaced by a file.

    **The boundary, which is real and is documented in TROUBLESHOOTING.md.**
    This runs after a first discovery pass, because the configuration lives
    inside the state directory, which hangs off the Zomboid directory this
    function is resolving. ``install_dir`` has no such circularity and always
    applies. ``user_dir`` applies whenever the configuration was reachable
    without it — which includes the case that matters, since a failed discovery
    falls back to a state directory beside the working directory — and
    ``--config`` names the file directly when it does not.

    A configuration that does not parse is ignored here rather than raised:
    ``doctor`` and ``validate-config`` exist to report that, and neither can run
    if resolving a workspace throws first.
    """
    configured = load_config(ctx.config_override or _provisional_config_path(ctx, found)).config
    if configured is None:
        return ctx
    install = None if ctx.discovery.install_override else configured.install_dir
    zomboid = None if ctx.discovery.zomboid_dir_override else configured.user_dir
    if install is None and zomboid is None:
        return ctx
    return ctx.with_overrides(install_dir=install, zomboid_dir=zomboid)


def _provisional_config_path(ctx: CliContext, found: Discovery) -> Path:
    """Where the configuration would be, given what discovery found so far."""
    user_dir = found.user_dir.path
    state_dir = ctx.state_dir_override or (
        user_dir / STATE_DIR_NAME if user_dir is not None else ctx.fallback_dir / STATE_DIR_NAME
    )
    return state_dir / CONFIG_NAME


def resolve_workspace(ctx: CliContext) -> Workspace:
    """Run discovery and derive every directory the commands use.

    The redactor is built here rather than per command, so a path printed by
    ``doctor`` and the same path written into a support bundle are redacted by
    the same rules.

    Discovery runs a second time when ``[game]`` in ``config.toml`` names a path
    — see :func:`_configured`, which is where the documented escape hatch for an
    unfindable install or profile became something other than a comment.
    """
    found = discover(ctx.discovery)
    reconfigured = _configured(ctx, found)
    if reconfigured is not ctx:
        ctx = reconfigured
        found = discover(ctx.discovery)
    user_dir = found.user_dir.path
    state_dir = ctx.state_dir_override or (
        user_dir / STATE_DIR_NAME if user_dir is not None else ctx.fallback_dir / STATE_DIR_NAME
    )
    usernames: list[str] = []
    for key in ("USERNAME", "USER"):
        value = ctx.env.get(key) or ctx.discovery.env.get(key)
        if value:
            usernames.append(value)
    if user_dir is not None:
        # The profile directory's name is the account name even when the
        # environment does not say so — a relocated or portable profile is the
        # case where the variables are absent and the path still carries it.
        usernames.append(user_dir.parent.name)
    redactor = build_redactor(
        user_dir=user_dir,
        home_dir=None if user_dir is None else user_dir.parent,
        install_dir=found.install.path,
        usernames=usernames,
    )
    return Workspace(
        discovery=found,
        state_dir=state_dir,
        logs_dir=state_dir / LOGS_DIR_NAME,
        trace_dir=state_dir / TRACE_DIR_NAME,
        bundle_dir=state_dir / BUNDLE_DIR_NAME,
        backup_root=state_dir / BACKUP_DIR_NAME,
        compat_dir=state_dir / COMPAT_DIR_NAME,
        config_path=ctx.config_override or (state_dir / CONFIG_NAME),
        redactor=redactor,
        ipc_root=None if user_dir is None else user_dir.joinpath(*IPC_SUBPATH),
        mods_dir=found.user_dir.mods_dir,
    )
