"""Attributing a local backup to the save the mod says is open.

Two identifier spaces meet here and neither can be computed from the other. A
backup is named by its save *directory* (``<mode>/<name>``); an observation names
the save by a digest the mod computes inside the game
(``PZAgent.ObserveModel.saveId``) over a key that never crosses the boundary. The
autonomy gate compares :class:`~pz_agent_core.policy.autonomy.BackupEvidence`
against the second one and refuses anything else, and it is right to: "a backup
exists somewhere" is the kind of claim that reads as reassurance right up until a
restore is needed.

The bridge is a record, not a computation. ``pz-agent backup-save`` reads the
save id out of the mod's own published snapshot — the same value the gate will
later compare against, produced by the same code — and writes it into the
backup's manifest. Attribution is then exact string equality against that
recorded id, and every way of *not* having one produces the same answer as
before the feature existed: no evidence, and an agent that asks.

The test this file exists for is
:func:`test_a_backup_of_another_save_is_not_attributed_to_this_one`. The rest
guard the ways the recording can be absent — no session, a torn slot, a manifest
written before the field existed — because each of them is a place where a
plausible value could be invented, and the invented one would be wrong exactly
when a user needed it not to be.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from pz_agent_cli import app
from pz_agent_cli.autonomy import (
    BACKUP_ATTRIBUTION_AVAILABLE,
    BACKUP_NOT_ATTRIBUTABLE,
    PLANNER_FILE_NAME,
    AutonomyPlanner,
    BackupAttribution,
    BackupWitness,
    build_backup_witness,
    no_attributable_backup,
    read_planner_record,
    workspace_backups,
)
from pz_agent_cli.context import EXIT_OK, Workspace, resolve_workspace
from pz_agent_cli.runtime import LoopLimits
from pz_agent_core.ipc.layout import IpcLayout, SnapshotSlot
from pz_agent_core.ipc.snapshot import SnapshotWriter
from pz_agent_core.platform.backup import MANIFEST_NAME, BackupManager, BackupRecord
from pz_agent_core.policy.autonomy import AutonomyOutcome
from pz_agent_core.protocol import Observation, ReasonCode
from tests.fixtures import make_game, make_observation
from tests.fixtures.autonomy_worlds import autonomous_observation, calm_player
from tests.fixtures.cli_worlds import CliWorld, make_world
from tests.fixtures.platform_trees import make_save

#: Two save directories and the two digests the mod reports for them. Neither
#: pair is derivable from the other, which is the whole problem.
SAVE_DIR: Final = "Survivor/09-07-1993"
OTHER_SAVE_DIR: Final = "Builder/12-01-1994"
OBSERVED: Final = "1f3c9a2b7e40d115"
OBSERVED_OTHER: Final = "9911aabb00cc22dd"

SAVE_FILES: Final = {"map_t.bin": b"tiles", "players.db": b"player-state"}

#: Above the §17.1 hunger trigger, so the gate has a real need to weigh and its
#: refusal is about the backup rather than about there being nothing to do.
HUNGRY: Final = 0.60

LIMITS: Final = LoopLimits(
    tick_interval_ms=0,
    tick_budget=1,
    max_actions_per_window=2,
    action_window_ms=600_000,
    observations_per_tick=16,
    observation_window=8,
)


# ---------------------------------------------------------------------------
# a machine, a mod publishing snapshots, and the real commands
# ---------------------------------------------------------------------------


def make_machine(tmp_path: Path, *save_dirs: str) -> CliWorld:
    """A fake machine with the named saves on it and a frozen clock."""
    world = make_world(tmp_path)
    assert world.user_dir is not None
    for save_dir in save_dirs or (SAVE_DIR,):
        make_save(world.user_dir, save_dir, SAVE_FILES)
    world.clock.freeze()
    return world


def layout_of(world: CliWorld) -> IpcLayout:
    assert world.ipc_root is not None
    layout = IpcLayout(world.ipc_root)
    layout.ensure()
    return layout


def publish_snapshot(world: CliWorld, save_id: str, *, seq: int = 1) -> Observation:
    """Publish a full snapshot the way the mod does: slot first, pointer last.

    Written through the real :class:`~pz_agent_core.ipc.snapshot.SnapshotWriter`,
    so the file names, the alternation and the commit order are the ones the mod
    produces rather than a shape invented by this test.
    """
    layout = layout_of(world)
    observation = make_observation(
        seq=seq,
        timestamp_ms=world.clock.now_ms,
        game=make_game(save_id=save_id),
    )
    SnapshotWriter(layout, clock=lambda: world.clock.now_ms).publish(observation.to_dict())
    return observation


def backup_save(world: CliWorld, *args: str) -> str:
    """Run ``pz-agent backup-save`` and return the id of the backup it took."""
    world.reset_streams()
    assert world.run("backup-save", *args) == EXIT_OK, world.stderr
    return world.stdout.split(" as ")[1].split()[0]


def backups_of(world: CliWorld) -> BackupManager:
    manager = workspace_backups(resolve_workspace(world.ctx))
    assert manager is not None
    return manager


def witness_of(world: CliWorld) -> BackupWitness:
    """The witness ``build_loop`` would pass, built from this machine's backups."""
    witness, _ = build_backup_witness(resolve_workspace(world.ctx))
    return witness


def strip_from_manifest(record: BackupRecord, key: str) -> None:
    """Make a manifest look like one an older build wrote."""
    manifest = record.directory / MANIFEST_NAME
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload.pop(key, None)
    manifest.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def hungry_in(save_id: str) -> Observation:
    """An observation the autonomy gate would act on but for the backup rule."""
    return autonomous_observation(
        game=make_game(save_id=save_id),
        player=calm_player(hunger=HUNGRY),
    )


def planner_of(world: CliWorld, workspace: Workspace) -> AutonomyPlanner:
    """The planner ``pz-agent start`` assembles, with its production witness."""
    loop = app.build_loop(world.ctx, workspace, limits=LIMITS)
    planner = loop.planner
    assert isinstance(planner, AutonomyPlanner), planner
    return planner


# ---------------------------------------------------------------------------
# what a backup records
# ---------------------------------------------------------------------------


def test_a_backup_taken_with_a_session_attached_records_the_id_the_snapshot_carried(
    tmp_path: Path,
) -> None:
    """The recorded value is the mod's own, copied — never a hash computed here."""
    world = make_machine(tmp_path)
    publish_snapshot(world, OBSERVED)

    backup_id = backup_save(world)

    record = backups_of(world).get(backup_id)
    assert record.observed_save_id == OBSERVED
    assert record.save_id == SAVE_DIR
    assert OBSERVED in world.stdout
    evidence = witness_of(world)(OBSERVED)
    assert evidence is not None
    assert evidence.save_id == OBSERVED
    assert evidence.verified is True
    assert evidence.created_at_ms == record.created_at_ms


def test_a_backup_taken_with_no_session_records_none_and_autonomy_then_refuses(
    tmp_path: Path,
) -> None:
    """The safe direction, proven rather than assumed.

    There is a real, complete, hash-verified backup of the save directory on this
    machine. Nothing said which save the mod calls it, so nothing may act as if
    something had, and the gate has to reach ASK_USER through its own unbacked-save
    rule rather than through some other refusal that happens to fire first.
    """
    world = make_machine(tmp_path)

    backup_id = backup_save(world)

    record = backups_of(world).get(backup_id)
    assert record.observed_save_id is None
    assert "no save id was recorded" in world.stderr
    witness = witness_of(world)
    assert witness is no_attributable_backup
    assert witness(OBSERVED) is None

    workspace = resolve_workspace(world.ctx)
    planner = planner_of(world, workspace)
    assert planner.propose(hungry_in(OBSERVED)) is None
    decision = planner.last_decision
    assert decision is not None
    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert decision.reason_code is ReasonCode.PRECONDITION_FAILED
    assert "no backup" in decision.detail
    record_read = read_planner_record(workspace.state_dir / PLANNER_FILE_NAME)
    assert record_read is not None
    assert BACKUP_NOT_ATTRIBUTABLE in record_read.notes


def test_a_backup_of_another_save_is_not_attributed_to_this_one(tmp_path: Path) -> None:
    """The assertion the whole feature exists for.

    The backup of the *other* save is deliberately the newest one on the machine,
    so a newest-wins rule — the one plausible shortcut — hands it back as the
    safety net for a world it has nothing to do with.
    """
    world = make_machine(tmp_path, SAVE_DIR, OTHER_SAVE_DIR)
    publish_snapshot(world, OBSERVED, seq=1)
    mine = backup_save(world, SAVE_DIR)
    publish_snapshot(world, OBSERVED_OTHER, seq=2)
    theirs = backup_save(world, OTHER_SAVE_DIR)

    manager = backups_of(world)
    witness = witness_of(world)
    assert isinstance(witness, BackupAttribution)

    newest = next(record.backup_id for record in manager.list_backups())
    assert newest == theirs, "the other save's backup must be the newest for this to bite"
    assert [record.backup_id for record in manager.attributed_to(OBSERVED)] == [mine]
    for save_id, expected in ((OBSERVED, mine), (OBSERVED_OTHER, theirs)):
        evidence = witness(save_id)
        assert evidence is not None
        assert evidence.save_id == save_id
        assert evidence.created_at_ms == manager.get(expected).created_at_ms
    # A third save, backed up by nobody: two backups exist and neither is its.
    assert witness("00000000deadbeef") is None
    assert "no backup here records the save id" in witness.last_detail

    planner = planner_of(world, resolve_workspace(world.ctx))
    assert planner.propose(hungry_in("00000000deadbeef")) is None
    decision = planner.last_decision
    assert decision is not None
    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert decision.reason_code is ReasonCode.PRECONDITION_FAILED


def test_an_old_manifest_with_no_save_id_round_trips_and_does_not_attribute(
    tmp_path: Path,
) -> None:
    """A backup written before the field existed is unattributed, not corrupt."""
    world = make_machine(tmp_path)
    publish_snapshot(world, OBSERVED)
    backup_id = backup_save(world)
    manager = backups_of(world)
    strip_from_manifest(manager.get(backup_id), "observed_save_id")

    reloaded = manager.get(backup_id)

    assert reloaded.observed_save_id is None
    assert manager.list_unreadable() == ()
    assert [record.backup_id for record in manager.list_backups()] == [backup_id]
    assert manager.verify(backup_id).backup_id == backup_id
    witness = witness_of(world)
    assert witness is no_attributable_backup
    assert witness(OBSERVED) is None


def test_a_pointer_naming_a_slot_that_cannot_be_read_yields_no_id(tmp_path: Path) -> None:
    """No stale id, even though a whole readable snapshot is sitting in the other slot.

    The mod writes the pointer last, so the slot it names is the only one that
    holds a snapshot the mod finished publishing. Falling back to the other slot
    keeps a world model moving, which is why the loop's reader does it; here it
    would attribute a backup to whichever save was open one snapshot ago.
    """
    world = make_machine(tmp_path)
    publish_snapshot(world, OBSERVED, seq=1)
    publish_snapshot(world, OBSERVED_OTHER, seq=2)
    layout = layout_of(world)
    pointed = layout.snapshot_slot(SnapshotSlot.B)
    assert json.loads(pointed.read_text(encoding="utf-8"))["game"]["save_id"] == OBSERVED_OTHER
    pointed.write_text('{"session_id": "tru', encoding="utf-8")

    backup_id = backup_save(world)

    record = backups_of(world).get(backup_id)
    assert record.observed_save_id is None, "a torn slot fell back to the older snapshot"
    assert "could not be read" in world.stderr
    assert witness_of(world)(OBSERVED) is None


def test_a_snapshot_older_than_a_session_is_not_read_as_the_save_being_played(
    tmp_path: Path,
) -> None:
    """A game that has quit leaves its last snapshot on disk forever."""
    world = make_machine(tmp_path)
    publish_snapshot(world, OBSERVED)
    world.clock.advance(60 * 60 * 1000)

    backup_id = backup_save(world)

    assert backups_of(world).get(backup_id).observed_save_id is None
    assert "ms old" in world.stderr


def test_a_backup_whose_bytes_no_longer_match_its_manifest_is_not_evidence(
    tmp_path: Path,
) -> None:
    """``verified`` means the hashes were read, so a corrupt backup is not a net.

    It is skipped rather than offered as unverified evidence: a deployment that
    turned ``require_verified_backup`` off would otherwise be handed a backup that
    is known not to restore.
    """
    world = make_machine(tmp_path)
    publish_snapshot(world, OBSERVED)
    backup_id = backup_save(world)
    record = backups_of(world).get(backup_id)
    (record.data_dir / "map_t.bin").write_bytes(b"rot")

    witness = witness_of(world)
    assert isinstance(witness, BackupAttribution)
    assert witness(OBSERVED) is None
    assert "failed its hash check" in witness.last_detail


# ---------------------------------------------------------------------------
# what the sidecar is assembled with
# ---------------------------------------------------------------------------


def test_build_loop_passes_a_witness_that_can_attribute_and_says_so(tmp_path: Path) -> None:
    """The wiring the endurance scenarios need: an armed sidecar that acts."""
    world = make_machine(tmp_path)
    publish_snapshot(world, OBSERVED)
    backup_save(world)
    workspace = resolve_workspace(world.ctx)

    planner = planner_of(world, workspace)

    assert isinstance(planner.backup, BackupAttribution)
    evidence = planner.backup(OBSERVED)
    assert evidence is not None and evidence.verified is True
    decision_before = planner.propose(hungry_in(OBSERVED))
    decision = planner.last_decision
    assert decision is not None
    assert decision.reason_code is not ReasonCode.PRECONDITION_FAILED, (
        f"the backup gate refused an attributed backup: {decision.detail}"
    )
    # The capability ladder is what stops this one, and it is a different
    # refusal in a different place; §7.7 will not try an unverified capability
    # unattended however well backed up the save is.
    assert decision_before is None
    assert decision.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
    record = read_planner_record(workspace.state_dir / PLANNER_FILE_NAME)
    assert record is not None
    assert BACKUP_ATTRIBUTION_AVAILABLE in record.notes


# ---------------------------------------------------------------------------
# the three states, as status prints them
# ---------------------------------------------------------------------------


def test_status_shows_no_backup_at_all_as_its_own_state(tmp_path: Path) -> None:
    world = make_machine(tmp_path)
    publish_snapshot(world, OBSERVED)

    world.reset_streams()
    assert world.run("status") == EXIT_OK

    assert "backup" in world.stdout
    assert "nothing has been backed up here" in world.stdout
    assert "autonomy asks rather than acts" in world.stdout


def test_status_shows_a_backup_that_cannot_be_attributed_as_its_own_state(
    tmp_path: Path,
) -> None:
    """One backup exists and it covers nothing anyone can point at."""
    world = make_machine(tmp_path)
    backup_save(world)
    publish_snapshot(world, OBSERVED)

    world.reset_streams()
    assert world.run("status") == EXIT_OK

    assert "1 here, none attributable to this save" in world.stdout
    assert "no backup here records the save the mod is reporting" in world.stdout


def test_status_shows_an_attributed_backup_with_its_id_and_age(tmp_path: Path) -> None:
    world = make_machine(tmp_path)
    publish_snapshot(world, OBSERVED, seq=1)
    backup_id = backup_save(world)
    # The session is still attached an hour later, so the snapshot is fresh and
    # the backup is not: the age printed is the backup's, not the snapshot's.
    world.clock.advance(60 * 60 * 1000)
    publish_snapshot(world, OBSERVED, seq=2)

    world.reset_streams()
    assert world.run("status") == EXIT_OK

    assert f"{backup_id} — of the save now open ({OBSERVED})" in world.stdout
    assert "60 min ago" in world.stdout


def test_the_json_form_carries_the_same_three_way_answer(tmp_path: Path) -> None:
    """The form that ends up in a bug report says which state it was in."""
    world = make_machine(tmp_path)
    publish_snapshot(world, OBSERVED)
    backup_id = backup_save(world)

    world.reset_streams()
    assert world.run("status", "--json") == EXIT_OK

    backup = json.loads(world.stdout)["backup"]
    assert backup["attributed"] is True
    assert backup["backup_id"] == backup_id
    assert backup["observed_save_id"] == OBSERVED
    assert backup["count"] == 1


def test_the_backup_listing_says_which_rows_can_be_attributed(tmp_path: Path) -> None:
    world = make_machine(tmp_path)
    unattributed = backup_save(world)
    publish_snapshot(world, OBSERVED)
    attributed = backup_save(world)

    world.reset_streams()
    assert world.run("backup-save", "--list") == EXIT_OK

    rows = {line.split()[0]: line for line in world.stdout.splitlines() if line.strip()}
    assert "no save id" in rows[unattributed]
    assert f"save id {OBSERVED}" in rows[attributed]
