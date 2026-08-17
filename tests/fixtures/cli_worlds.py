"""A whole fake machine for the CLI tests, driven in-process.

The CLI's failure modes are environmental — no game, no Zomboid directory, a
Cyrillic account name, a mod somebody else's tool already put in the mods
folder — and none of them are reproducible by running the real command on a
developer's box. :func:`make_world` builds the tree, wires it into a
:class:`~pz_agent_cli.context.CliContext` with captured streams and a fake
clock, and hands back a harness whose :meth:`CliWorld.run` calls the same
``main`` the console script does.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pz_agent_cli.app import main
from pz_agent_cli.context import CliContext
from pz_agent_core.platform.discovery import DiscoveryContext
from pz_agent_core.version import MOD_VERSION

from .capability_trees import full_lua_tree
from .platform_trees import (
    CYRILLIC_USER,
    install_game,
    make_library,
    make_steam_root,
    make_user_dir,
    write_modern_libraryfolders,
)

#: Fixed instant the fake clock starts from, so ids and timestamps in expected
#: output do not move with the wall clock.
START = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)

START_MS = int(START.timestamp() * 1000)


class StepClock:
    """A millisecond clock that advances by a fixed step on every read."""

    def __init__(self, start_ms: int = START_MS, step_ms: int = 1000) -> None:
        self._now = start_ms
        self._step = step_ms

    def __call__(self) -> int:
        current = self._now
        self._now += self._step
        return current

    @property
    def now_ms(self) -> int:
        return self._now

    def freeze(self) -> None:
        self._step = 0

    def advance(self, milliseconds: int) -> None:
        self._now += milliseconds


class StepDatetime:
    """The same idea for the ``datetime`` clock the bundle and manifests use."""

    def __init__(self, start: datetime = START, step_seconds: int = 1) -> None:
        self._now = start
        self._step = timedelta(seconds=step_seconds)

    def __call__(self) -> datetime:
        current = self._now
        self._now += self._step
        return current


@dataclass
class CliWorld:
    """One fake machine plus the streams the CLI wrote to."""

    root: Path
    home: Path
    ctx: CliContext
    clock: StepClock
    out: io.StringIO
    err: io.StringIO
    user_dir: Path | None = None
    install_dir: Path | None = None
    mod_source: Path | None = None
    extra: dict[str, Path] = field(default_factory=dict)

    def run(self, *argv: str) -> int:
        """Invoke the CLI exactly as the console script does."""
        return main(list(argv), context=self.ctx)

    @property
    def stdout(self) -> str:
        return self.out.getvalue()

    @property
    def stderr(self) -> str:
        return self.err.getvalue()

    def reset_streams(self) -> None:
        self.out.seek(0)
        self.out.truncate(0)
        self.err.seek(0)
        self.err.truncate(0)

    @property
    def state_dir(self) -> Path:
        override = self.ctx.state_dir_override
        if override is not None:
            return override
        base = self.user_dir if self.user_dir is not None else self.ctx.fallback_dir
        return base / "pz-agent"

    @property
    def mods_dir(self) -> Path | None:
        return None if self.user_dir is None else self.user_dir / "mods"

    @property
    def ipc_root(self) -> Path | None:
        return None if self.user_dir is None else self.user_dir / "Lua" / "pz_agent"


def make_mod_source(base: Path, *, version: str = MOD_VERSION) -> Path:
    """A minimal but structurally real mod source tree."""
    source = base / "pz-mod" / "42"
    (source / "media" / "lua" / "shared" / "PZAgent").mkdir(parents=True, exist_ok=True)
    (source / "media" / "lua" / "client" / "PZAgent").mkdir(parents=True, exist_ok=True)
    # pzversion mirrors the real mod.info: the major "42", never the point
    # release — the live Build 42.20.2 mod list hides a mod that declares one.
    (source / "mod.info").write_text(
        f"name=PZ Agent Bridge\nid=pz_agent_bridge\nmodversion={version}\npzversion=42\n",
        encoding="utf-8",
    )
    (source / "media" / "lua" / "shared" / "PZAgent" / "Json.lua").write_text(
        "PZAgent = PZAgent or {}\n", encoding="utf-8"
    )
    (source / "media" / "lua" / "client" / "PZAgent_Main.lua").write_text(
        "-- entry point\n", encoding="utf-8"
    )
    # Not in the allowlist: proves the installer copies mod content only.
    (source / "notes.bin").write_bytes(b"\x00\x01")
    return source


def make_world(
    tmp_path: Path,
    *,
    username: str = CYRILLIC_USER,
    with_game: bool = True,
    with_user_dir: bool = True,
    with_lua: bool = True,
    build_version: str | None = "42.20",
    with_mod_source: bool = True,
) -> CliWorld:
    """Build a fake machine. Every part can be left out to test its absence."""
    root = tmp_path / "drive"
    home = root / "Users" / username
    home.mkdir(parents=True, exist_ok=True)

    user_dir: Path | None = None
    if with_user_dir:
        user_dir = make_user_dir(home)

    install: Path | None = None
    if with_game:
        steam_root = make_steam_root(root / "Program Files (x86)")
        library = make_library(root / "Games", "SteamLibrary")
        write_modern_libraryfolders(steam_root, [steam_root, library])
        install = install_game(library, version=build_version)
        if with_lua:
            full_lua_tree(install)

    env = {
        "SystemDrive": str(root),
        "USERPROFILE": str(home),
        "USERNAME": username,
        "ProgramFiles(x86)": str(root / "Program Files (x86)"),
    }
    out = io.StringIO()
    err = io.StringIO()
    clock = StepClock()
    ctx = CliContext(
        discovery=DiscoveryContext(root=root, env=env),
        stdout=out,
        stderr=err,
        env=env,
        clock_ms=clock,
        now=StepDatetime(),
        fallback_dir=tmp_path / "cwd",
    )
    mod_source = make_mod_source(tmp_path) if with_mod_source else None
    if mod_source is not None:
        ctx = ctx.with_overrides(mod_source=mod_source)
    return CliWorld(
        root=root,
        home=home,
        ctx=ctx,
        clock=clock,
        out=out,
        err=err,
        user_dir=user_dir,
        install_dir=install,
        mod_source=mod_source,
    )
