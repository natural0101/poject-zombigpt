"""``pz-agent doctor``: every check passing and failing, with its code and remedy."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pz_agent_cli.context import EXIT_FAILURE, EXIT_OK, Workspace, resolve_workspace
from pz_agent_cli.doctor import (
    MAX_LISTED_PATHS,
    CheckResult,
    CheckStatus,
    DoctorError,
    _listed,
    check_permissions,
)
from pz_agent_cli.modinstall import MOD_ID, install_mod
from pz_agent_core.capabilities import AUTONOMOUS_ATTACK
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.platform.discovery import UserDirectory
from pz_agent_core.protocol import CapabilityState, SessionMode
from pz_agent_core.session.handshake import SessionDescriptor
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer
from pz_agent_core.version import MOD_VERSION, PRODUCT_VERSION
from tests.fixtures.cli_worlds import CliWorld, make_world
from tests.fixtures.platform_trees import CYRILLIC_USER

CODES = (
    "PZD001",
    "PZD002",
    "PZD003",
    "PZD004",
    "PZD005",
    "PZD006",
    "PZD007",
    "PZD008",
    "PZD009",
    "PZD010",
)


def _report(world: CliWorld) -> dict[str, Any]:
    world.reset_streams()
    assert world.run("doctor", "--json") in (EXIT_OK, EXIT_FAILURE)
    document: dict[str, Any] = json.loads(world.stdout)
    return document


def _check(document: Mapping[str, Any], code: str) -> Mapping[str, Any]:
    for check in document["checks"]:
        if check["code"] == code:
            assert isinstance(check, Mapping)
            return check
    raise AssertionError(f"no check {code} in the report")


# ---------------------------------------------------------------------------
# report shape
# ---------------------------------------------------------------------------


def test_every_documented_check_runs_in_the_documented_order(tmp_path: Path) -> None:
    document = _report(make_world(tmp_path))

    assert [check["code"] for check in document["checks"]] == list(CODES)


def test_the_json_report_carries_the_versions_and_a_summary(tmp_path: Path) -> None:
    document = _report(make_world(tmp_path))

    assert document["product_version"] == PRODUCT_VERSION
    assert document["mod_version"] == MOD_VERSION
    assert sum(document["summary"].values()) == len(CODES)
    assert set(document["summary"]) == {"pass", "info", "warn", "fail", "unknown"}
    assert document["ok"] is (document["summary"]["fail"] == 0)


def test_a_non_pass_check_cannot_be_built_without_remediation() -> None:
    with pytest.raises(DoctorError, match="must tell the user what to do next"):
        CheckResult(
            code="PZD001",
            name="game_installation",
            title="Game installation",
            status=CheckStatus.FAIL,
            detail="not found",
        )


def test_every_reported_problem_carries_remediation_text(tmp_path: Path) -> None:
    document = _report(make_world(tmp_path, with_game=False, with_user_dir=False))

    for check in document["checks"]:
        if check["status"] in {"warn", "fail", "unknown"}:
            assert check["remediation"], check["code"]


def test_no_output_carries_the_cyrillic_account_name(tmp_path: Path) -> None:
    """doctor --json is the artefact TROUBLESHOOTING tells users to attach."""
    world = make_world(tmp_path)
    world.run("doctor")
    text_output = world.stdout + world.stderr
    world.reset_streams()
    world.run("doctor", "--json")

    assert CYRILLIC_USER not in text_output
    assert CYRILLIC_USER not in world.stdout
    assert str(world.home) not in world.stdout


def test_the_human_rendering_prints_the_code_and_the_remedy(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    exit_code = world.run("doctor")

    assert exit_code == EXIT_FAILURE  # the mod is not installed yet
    assert "PZD005" in world.stdout
    assert "run pz-agent install-mod" in world.stdout


def test_the_paths_listed_in_the_report_are_bounded_and_counted(tmp_path: Path) -> None:
    """Discovery bounds its own search; the report bounds what it prints of it.

    A corrupt ``libraryfolders.vdf`` can name a great many locations, and a
    report that pasted all of them would be the thing nobody could read.
    """
    workspace = resolve_workspace(make_world(tmp_path).ctx)
    paths = [f"/drive/library-{index}/steamapps" for index in range(MAX_LISTED_PATHS + 9)]

    shown = _listed(paths, workspace)

    assert len(shown) == MAX_LISTED_PATHS + 1
    assert shown[-1] == "... and 9 more"
    assert all(entry.startswith("<PATH>/") for entry in shown[:MAX_LISTED_PATHS])


# ---------------------------------------------------------------------------
# PZD001 / PZD002 — install and build
# ---------------------------------------------------------------------------


def test_the_install_check_passes_when_the_game_is_in_a_second_library(
    tmp_path: Path,
) -> None:
    check = _check(_report(make_world(tmp_path)), "PZD001")

    assert check["status"] == "pass"
    assert check["facts"]["libraries"] == 2


def test_a_missing_install_fails_and_points_at_the_searched_list(tmp_path: Path) -> None:
    check = _check(_report(make_world(tmp_path, with_game=False)), "PZD001")

    assert check["status"] == "fail"
    assert "install_dir under [game]" in check["remediation"]
    assert "no Steam library folder was found" in check["facts"]["problems"]


def test_an_undetectable_build_warns_rather_than_failing(tmp_path: Path) -> None:
    check = _check(_report(make_world(tmp_path, build_version=None)), "PZD002")

    assert check["status"] == "warn"
    assert check["facts"]["known"] is False
    assert "console.txt" in check["remediation"]


def test_a_build_outside_the_supported_set_warns_with_the_downgrade_rule(
    tmp_path: Path,
) -> None:
    check = _check(_report(make_world(tmp_path, build_version="41.78")), "PZD002")

    assert check["status"] == "warn"
    assert check["facts"]["build"] == "41.78"
    assert "downgraded" in check["remediation"]


# ---------------------------------------------------------------------------
# PZD003 / PZD004 — user directory and permissions
# ---------------------------------------------------------------------------


def test_a_missing_user_directory_fails_and_permissions_become_unknown(
    tmp_path: Path,
) -> None:
    document = _report(make_world(tmp_path, with_user_dir=False))

    assert _check(document, "PZD003")["status"] == "fail"
    assert _check(document, "PZD004")["status"] == "unknown"
    assert "PZD003" in _check(document, "PZD004")["remediation"]


def test_the_permission_check_leaves_no_probe_file_behind(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.run("doctor")

    assert world.user_dir is not None
    assert not list(world.user_dir.glob(".pz-agent-write-test-*"))


def test_a_user_directory_that_cannot_be_written_fails_with_the_do_not_elevate_advice(
    tmp_path: Path,
) -> None:
    """A directory that is not one is the portable stand-in for a denied ACL.

    Permission bits cannot express "denied" to the account this suite may run
    as, so the failure branch is driven by a path the probe genuinely cannot
    write to rather than by a chmod that root ignores.
    """
    world = make_world(tmp_path)
    blocked = tmp_path / "Zomboid-is-a-file"
    blocked.write_text("not a directory", encoding="utf-8")

    check = check_permissions(world.ctx, _with_user_dir(world, blocked))

    assert check.status is CheckStatus.FAIL
    assert "administrator" in check.remediation


# ---------------------------------------------------------------------------
# PZD005 — the mod
# ---------------------------------------------------------------------------


def test_the_mod_check_fails_before_install_and_passes_after(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert _check(_report(world), "PZD005")["status"] == "fail"

    assert world.mods_dir is not None
    assert world.mod_source is not None
    install_mod(world.mod_source, world.mods_dir)

    check = _check(_report(world), "PZD005")
    assert check["status"] == "pass"
    assert check["facts"]["mod_version"] == MOD_VERSION


def test_a_mod_from_another_release_warns_rather_than_passing(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.mods_dir is not None
    assert world.mod_source is not None
    install_mod(world.mod_source, world.mods_dir)
    info = world.mods_dir / MOD_ID / "mod.info"
    info.write_text(
        info.read_text(encoding="utf-8").replace(f"modversion={MOD_VERSION}", "modversion=0.0.9"),
        encoding="utf-8",
    )

    check = _check(_report(world), "PZD005")

    assert check["status"] == "warn"
    assert check["facts"]["mod_version"] == "0.0.9"


# ---------------------------------------------------------------------------
# PZD006 / PZD007 — heartbeat and the exchange directory
# ---------------------------------------------------------------------------


def test_a_missing_heartbeat_warns_and_names_the_three_causes(tmp_path: Path) -> None:
    check = _check(_report(make_world(tmp_path)), "PZD006")

    assert check["status"] == "warn"
    assert "not enabled" in check["remediation"]
    assert "no save is loaded" in check["remediation"]


def test_a_live_heartbeat_passes_and_reports_the_build(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    _publish_heartbeat(world)

    check = _check(_report(world), "PZD006")

    assert check["status"] == "pass"
    assert check["facts"]["build"] == "42.20"
    assert check["facts"]["armed"] is False


def test_the_exchange_directory_is_created_and_reported_as_writable(tmp_path: Path) -> None:
    world = make_world(tmp_path)

    check = _check(_report(world), "PZD007")

    assert check["status"] == "pass"
    assert check["facts"]["existed"] is False
    assert world.ipc_root is not None
    assert world.ipc_root.is_dir()


# ---------------------------------------------------------------------------
# PZD008 — capabilities
# ---------------------------------------------------------------------------


def test_the_capability_scan_reports_what_the_local_lua_declares(tmp_path: Path) -> None:
    document = _report(make_world(tmp_path))
    check = _check(document, "PZD008")

    assert check["status"] == "pass"
    assert check["facts"]["symbols"] > 0
    states = {
        name: capability["state"]
        for name, capability in document["capabilities"]["capabilities"].items()
    }
    assert states["move_to_square"] == CapabilityState.AVAILABLE_UNVERIFIED.value
    # A static scan can never mint 'verified', and doctor runs before any session.
    assert CapabilityState.VERIFIED.value not in states.values()
    assert states[AUTONOMOUS_ATTACK] == CapabilityState.UNSUPPORTED.value


def test_the_capability_report_is_written_where_the_rest_of_the_system_loads_it(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path)
    world.run("doctor")

    workspace = resolve_workspace(world.ctx)
    stored = json.loads(workspace.capability_report_path.read_text(encoding="utf-8"))
    assert stored["build"] == "42.20"


def test_a_missing_timed_action_class_fails_and_names_the_symbol(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.install_dir is not None
    (
        world.install_dir / "media" / "lua" / "client" / "TimedActions" / "ISEatFoodAction.lua"
    ).unlink()

    check = _check(_report(world), "PZD008")

    assert check["status"] == "fail"
    assert "eat_percentage" in check["detail"]
    assert "ISEatFoodAction" in str(check["facts"]["missing_symbols"])


def test_no_installation_makes_the_capability_check_unknown_not_a_pass(
    tmp_path: Path,
) -> None:
    document = _report(make_world(tmp_path, with_game=False))
    check = _check(document, "PZD008")

    assert check["status"] == "unknown"
    assert document["capabilities"] is None


# ---------------------------------------------------------------------------
# PZD009 / PZD010 — leftovers and the session
# ---------------------------------------------------------------------------


def test_a_leftover_panic_file_and_session_are_reported_together(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.ipc_root is not None
    layout = IpcLayout(world.ipc_root)
    layout.ensure()
    layout.panic_stop.write_text("", encoding="utf-8")
    _write_session(world)

    check = _check(_report(world), "PZD009")

    assert check["status"] == "warn"
    assert "panic-stop sentinel" in check["detail"]
    assert "session.json is present while the game heartbeat is not" in check["detail"]
    assert "close the game" in check["remediation"]


def test_a_file_the_layout_does_not_own_is_named(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.ipc_root is not None
    IpcLayout(world.ipc_root).ensure()
    (world.ipc_root / "leftover.txt").write_text("x", encoding="utf-8")

    check = _check(_report(world), "PZD009")

    assert check["status"] == "warn"
    assert "leftover.txt" in check["detail"]


def test_no_session_file_is_information_rather_than_a_pass(tmp_path: Path) -> None:
    check = _check(_report(make_world(tmp_path)), "PZD010")

    assert check["status"] == "info"
    assert check["remediation"] == ""


def test_a_session_without_a_matching_heartbeat_warns(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    _write_session(world)

    check = _check(_report(world), "PZD010")

    assert check["status"] == "warn"
    assert "re-arms itself" in check["remediation"]


def test_a_session_matching_the_live_heartbeat_passes(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    session = _write_session(world)
    _publish_heartbeat(world, session_id=session.session_id)

    check = _check(_report(world), "PZD010")

    assert check["status"] == "pass"
    assert check["facts"]["mode"] == SessionMode.OBSERVE.value


def test_an_unreadable_session_file_warns_with_how_to_clear_it(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.ipc_root is not None
    layout = IpcLayout(world.ipc_root)
    layout.ensure()
    layout.session.write_text("{not json", encoding="utf-8")

    check = _check(_report(world), "PZD010")

    assert check["status"] == "warn"
    assert "delete session.json" in check["remediation"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _with_user_dir(world: CliWorld, path: Path) -> Workspace:
    """A workspace whose Zomboid directory is *path*, whatever discovery found."""
    workspace = resolve_workspace(world.ctx)
    discovery = replace(
        workspace.discovery,
        user_dir=UserDirectory(found=True, path=path, source="test"),
    )
    return replace(workspace, discovery=discovery)


def _publish_heartbeat(world: CliWorld, session_id: str | None = None) -> None:
    assert world.ipc_root is not None
    layout = IpcLayout(world.ipc_root)
    layout.ensure()
    # The monitor's own clock is the world's, so the heartbeat it writes is
    # fresh against the clock the doctor reads it with.
    HeartbeatMonitor(layout, clock=lambda: world.clock.now_ms).publish(
        Peer.GAME,
        session_id=session_id or "00000000-0000-0000-0000-0000005e5510",
        nonce="nonce-1",
        version=MOD_VERSION,
        build="42.20",
        player_present=True,
        armed=False,
        mode=SessionMode.OBSERVE,
    )


def _write_session(world: CliWorld) -> SessionDescriptor:
    assert world.ipc_root is not None
    layout = IpcLayout(world.ipc_root)
    layout.ensure()
    descriptor = SessionDescriptor(
        session_id="00000000-0000-0000-0000-0000005e5510",
        nonce="nonce-1",
        created_at_ms=world.clock.now_ms,
        mode=SessionMode.OBSERVE,
    )
    layout.session.write_text(json.dumps(descriptor.to_dict()), encoding="utf-8")
    return descriptor
