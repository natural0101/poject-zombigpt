"""The documented escape hatch for a game discovery cannot find.

``game.install_dir`` and ``game.user_dir`` were parsed, validated, typed, given
properties on :class:`AgentConfig` — and read by nothing. Meanwhile:

- ``doctor``'s own remediation for ``PZD001`` says "set install_dir under [game]
  in config.toml";
- ``docs/TROUBLESHOOTING.md`` says it for ``PZD001`` *and* ``PZD003``;
- ``configs/mcp/README.md`` names both keys as where the paths come from.

Those two failures brick every other command — a GOG or manual copy Steam does
not list, and a Zomboid profile moved by OneDrive or ``-cachedir`` — and the
only escape the documents offer did nothing. A user who followed it was told
"configuration is valid", re-ran ``doctor``, and read the identical failure
telling them to do the thing they had just done. Nothing anywhere said the key
was ignored.

The tests below drive the whole scenario through the real CLI rather than
asserting that a property returns a path: the property always did.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from pz_agent_cli.context import EXIT_OK, resolve_workspace
from tests.fixtures.cli_worlds import CliWorld, make_world
from tests.fixtures.platform_trees import install_game

#: Somewhere Steam would never look, which is the entire point of the keys.
ELSEWHERE: Final = "GOG Games"


def _write_config(world: CliWorld, body: str) -> Path:
    path = resolve_workspace(world.ctx).config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_a_configured_install_is_found_when_discovery_cannot_find_one(
    tmp_path: Path,
) -> None:
    """PZD001's remedy, driven end to end."""
    world = make_world(tmp_path, with_game=False)
    manual = install_game(tmp_path / ELSEWHERE, version="42.20")
    _write_config(world, f'[game]\ninstall_dir = "{manual.as_posix()}"\n')

    workspace = resolve_workspace(world.ctx)

    assert workspace.install_dir == manual, (
        "the install named in config.toml was ignored, which is what doctor tells "
        "a blocked user to write"
    )


def test_a_configured_profile_is_used_for_the_exchange_directory(
    tmp_path: Path,
) -> None:
    """PZD003's remedy. The exchange directory is what the mod and sidecar meet in.

    Getting the profile right and the exchange directory wrong would be worse
    than not resolving it at all: the sidecar would attach to a directory the
    mod never writes to and wait there.
    """
    world = make_world(tmp_path, with_user_dir=False)
    moved = tmp_path / "OneDrive" / "Zomboid"
    (moved / "Lua").mkdir(parents=True, exist_ok=True)
    _write_config(world, f'[game]\nuser_dir = "{moved.as_posix()}"\n')

    workspace = resolve_workspace(world.ctx)

    assert workspace.user_dir == moved
    assert workspace.ipc_root == moved / "Lua" / "pz_agent"


def test_the_command_line_wins_over_the_file(tmp_path: Path) -> None:
    """A flag is passed for one invocation and is the more specific statement.

    A file quietly overriding it would make ``--install-dir`` untestable by the
    person holding the machine, which is the one situation it exists for.
    """
    world = make_world(tmp_path, with_game=False)
    from_file = install_game(tmp_path / ELSEWHERE, version="42.20")
    from_flag = install_game(tmp_path / "Other Games", version="42.20")
    _write_config(world, f'[game]\ninstall_dir = "{from_file.as_posix()}"\n')

    overridden = world.ctx.with_overrides(install_dir=from_flag)

    assert resolve_workspace(overridden).install_dir == from_flag


def test_doctor_stops_telling_a_user_to_do_what_they_have_done(tmp_path: Path) -> None:
    """The loop the defect put a user in, closed.

    ``doctor`` is where they would look to find out whether it worked, and it
    was the command repeating the advice they had just followed.
    """
    world = make_world(tmp_path, with_game=False)
    manual = install_game(tmp_path / ELSEWHERE, version="42.20")

    world.reset_streams()
    world.run("doctor", "--json")
    before = json.loads(world.stdout)

    _write_config(world, f'[game]\ninstall_dir = "{manual.as_posix()}"\n')
    world.reset_streams()
    world.run("doctor", "--json")
    after = json.loads(world.stdout)

    def install_check(document: dict[str, object]) -> dict[str, object]:
        checks = document["checks"]
        assert isinstance(checks, list)
        found = [row for row in checks if row["code"] == "PZD001"]
        assert found, "doctor no longer reports on the game installation at all"
        return dict(found[0])

    assert install_check(before)["status"] == "fail"
    assert install_check(after)["status"] != "fail", (
        "doctor still fails after the user did exactly what its remediation said"
    )


def test_a_configuration_that_does_not_parse_does_not_break_resolving(
    tmp_path: Path,
) -> None:
    """``doctor`` and ``validate-config`` are how a bad file gets reported.

    Neither can run if resolving a workspace raises first, so a broken document
    is ignored here and left for the commands whose job it is to explain it.
    """
    world = make_world(tmp_path)
    _write_config(world, "[game\nthis is not toml at all")

    workspace = resolve_workspace(world.ctx)

    assert workspace.install_dir == world.install_dir
    world.reset_streams()
    assert world.run("validate-config") != EXIT_OK, "the broken file went unreported"


def test_a_configured_path_that_does_not_exist_is_reported_at_that_path(
    tmp_path: Path,
) -> None:
    """Not silently ignored in favour of a search somewhere else.

    A user who typed the path wrongly needs to see *their* path in the failure.
    Falling back to discovery would hide the typo behind the original error.
    """
    world = make_world(tmp_path, with_game=False)
    _write_config(world, f'[game]\ninstall_dir = "{(tmp_path / "nowhere").as_posix()}"\n')

    workspace = resolve_workspace(world.ctx)

    assert workspace.install_dir is None
    world.reset_streams()
    world.run("doctor", "--json")
    check = next(row for row in json.loads(world.stdout)["checks"] if row["code"] == "PZD001")
    # One place, not a Steam sweep. The paths themselves are redacted on the way
    # out — correctly, since this document is designed to be attached to a public
    # issue — so what is asserted is that exactly the configured location was
    # looked at: a fallback to discovery would hide a typo behind the original
    # error, and would show here as a longer list.
    assert len(check["facts"]["searched"]) == 1, check["facts"]["searched"]
    assert check["status"] == "fail"
