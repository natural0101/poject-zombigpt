"""``backup-save`` and ``restore-save``.

Both are thin: :mod:`pz_agent_core.platform.backup` owns the manifest, the hash
verification, the staging swap and the retention rule, and this module adds only
the argument handling and the output.

The one judgement call lives here. ``BackupManager.restore`` takes
``game_running`` as a required keyword and refuses when it is true, so something
has to decide, and a wrong answer overwrites a live save.

The decision is :func:`~pz_agent_cli.supervisor.probe_game_running`, which is
three-valued on purpose. A fresh game heartbeat proves the game is open. Its
*absence* proves nothing on its own — a game sitting on the main menu, or one
whose mod was disabled, writes no heartbeat either — so the process table is
consulted second, and only a listing that actually ran may be read as an
absence. Everything except a positive "closed" collapses to *refuse*
(:func:`~pz_agent_cli.supervisor.game_running_for_restore`). There is no flag to
override that, exactly as ``docs/TROUBLESHOOTING.md`` states.

``backup-save`` carries one more thing across, and it can only be collected
while the game is open: the save id the *mod* reports. It is read out of the
mod's own published snapshot (:func:`~pz_agent_cli.autonomy.observed_save`) and
recorded in the manifest, which is what later lets autonomy see that a backup
covers the save being played. A backup taken with nothing attached — before the
mod is installed, or from a machine whose game is closed — records no save id
and says so; it is a real backup and a real safety net, it simply is not one
anything can *attribute*, and the output says which of the two the user just
took.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from pz_agent_core.platform.backup import (
    SAVES_DIR_NAME,
    BackupError,
    BackupManager,
    BackupRecord,
)
from pz_agent_core.protocol import JsonDict

from .autonomy import ObservedSave, observed_save, workspace_backups
from .context import EXIT_FAILURE, EXIT_OK, CliContext, Workspace, resolve_workspace
from .output import Printer
from .status import game_liveness
from .supervisor import game_running_for_restore, list_processes, probe_game_running

#: Depth of a save id under ``Saves``: ``<game mode>/<save name>``.
_SAVE_DEPTH: Final = 2

#: Save directories listed when the user has to pick one.
MAX_LISTED_SAVES: Final = 40


def find_saves(saves_dir: Path) -> tuple[str, ...]:
    """Every ``<mode>/<name>`` save directory, sorted.

    The shape is fixed by the game, so the walk is depth-limited rather than
    recursive: a save's own subdirectories are not saves.
    """
    if not saves_dir.is_dir():
        return ()
    found: list[str] = []
    for mode_dir in sorted(saves_dir.iterdir()):
        if not mode_dir.is_dir():
            continue
        for save_dir in sorted(mode_dir.iterdir()):
            if not save_dir.is_dir():
                continue
            if len(found) >= MAX_LISTED_SAVES:
                return tuple(found)
            found.append(f"{mode_dir.name}/{save_dir.name}")
    return tuple(found)


def _manager(ctx: CliContext, workspace: Workspace) -> BackupManager | None:
    """The one this workspace's other commands use, so all three read one root.

    On the CLI's clock, not the wall clock: a manifest's ``created_at`` is what
    ``status`` later measures a backup's age against, and the two have to be read
    from the same place for that subtraction to mean anything.
    """
    return workspace_backups(workspace, clock=ctx.now)


def _record_dict(record: BackupRecord, workspace: Workspace) -> JsonDict:
    payload = record.to_dict()
    payload["directory"] = workspace.redact(record.directory)
    return payload


def _no_user_dir(printer: Printer) -> int:
    printer.error(
        "no Zomboid directory was found, so there are no saves to back up. "
        "Run pz-agent doctor: check PZD003 names the locations that were searched."
    )
    return EXIT_FAILURE


def run_backup_save(
    ctx: CliContext,
    *,
    save_id: str | None,
    list_only: bool,
    prune: int | None,
    as_json: bool,
) -> int:
    """Handler for ``pz-agent backup-save``."""
    workspace = resolve_workspace(ctx)
    printer = Printer(ctx.stdout, ctx.stderr)
    manager = _manager(ctx, workspace)
    if manager is None:
        return _no_user_dir(printer)

    if list_only:
        return _list_backups(manager, workspace, printer, as_json=as_json)
    if prune is not None:
        return _prune(manager, workspace, printer, keep=prune, as_json=as_json)

    target, problem = _resolve_save_id(manager, save_id)
    if target is None:
        printer.error(problem)
        return EXIT_FAILURE
    # Read before the copy rather than after it: the id is the one that was
    # being played when this backup was taken, and a copy of a large save can
    # outlast a session that ends halfway through it.
    observed = _observed_save(ctx, workspace)
    try:
        record = manager.create(target, observed_save_id=observed.save_id)
    except BackupError as exc:
        printer.error(f"backup failed: {workspace.redactor.text(str(exc))}")
        return EXIT_FAILURE
    if as_json:
        payload = _record_dict(record, workspace)
        payload["observed_save_detail"] = workspace.redactor.text(observed.detail)
        printer.json(payload)
    else:
        printer.line(f"backed up {record.save_id} as {record.backup_id}")
        printer.field("files", str(record.file_count))
        printer.field("bytes", str(record.total_bytes))
        printer.field("location", workspace.redact(record.directory))
        printer.field("verified", "every file re-hashed against the manifest")
        _render_attribution(record.observed_save_id, observed, workspace, printer)
    return EXIT_OK


def _observed_save(ctx: CliContext, workspace: Workspace) -> ObservedSave:
    """The save id the mod is reporting right now, or why there is none."""
    return observed_save(workspace.ipc_root, now_ms=ctx.clock_ms())


def _render_attribution(
    recorded: str | None,
    observed: ObservedSave,
    workspace: Workspace,
    printer: Printer,
) -> None:
    """Say which of the two kinds of backup this is, and never imply the other.

    An unattributed backup is not a lesser copy of the save — the bytes and their
    hashes are the same — but it is one autonomy can never accept as covering the
    save being played, and a user who took it expecting otherwise would find that
    out from a sidecar that quietly keeps asking.
    """
    if recorded is not None:
        printer.field("save id", f"{recorded} — reported by the mod while this ran")
        return
    printer.field("save id", "none recorded")
    printer.error(
        f"no save id was recorded for this backup: {workspace.redactor.text(observed.detail)}. "
        "The backup itself is complete and restorable; what it cannot do is prove to autonomy "
        "that it covers the save you are playing. Take another with the game open and the save "
        "loaded if you want that."
    )


def _resolve_save_id(manager: BackupManager, save_id: str | None) -> tuple[str | None, str]:
    """Pick the save to back up, or explain why the user has to.

    A single save is unambiguous and is used. More than one is not, and guessing
    which of somebody's characters to snapshot is not a decision this tool gets
    to make silently.
    """
    if save_id:
        return save_id, ""
    saves = find_saves(manager.saves_dir)
    if len(saves) == 1:
        return saves[0], ""
    if not saves:
        return None, "no save directories were found under Saves/; load a game once first"
    listed = "\n  ".join(saves)
    return None, f"more than one save exists; name the one to back up:\n  {listed}"


def _list_backups(
    manager: BackupManager, workspace: Workspace, printer: Printer, *, as_json: bool
) -> int:
    records = manager.list_backups()
    unreadable = manager.list_unreadable()
    if as_json:
        printer.json(
            {
                "backups": [_record_dict(record, workspace) for record in records],
                "unreadable": [workspace.redactor.text(item) for item in unreadable],
                "root": workspace.redact(manager.backup_root),
            }
        )
        return EXIT_OK
    if not records:
        printer.line(f"no backups in {workspace.redact(manager.backup_root)}")
    for record in records:
        # The recorded save id is part of the identity of a backup here, not a
        # detail: it is what decides whether autonomy can ever treat this one as
        # a safety net, so the listing says which kind each row is.
        attribution = (
            f"save id {record.observed_save_id}"
            if record.observed_save_id is not None
            else "no save id"
        )
        printer.line(
            f"{record.backup_id}  {record.save_id}  {record.file_count} file(s)  "
            f"{record.total_bytes} B  {record.created_at}  {attribution}"
        )
    for problem in unreadable:
        printer.error(f"unreadable: {workspace.redactor.text(problem)}")
    return EXIT_OK


def _prune(
    manager: BackupManager, workspace: Workspace, printer: Printer, *, keep: int, as_json: bool
) -> int:
    try:
        removed = manager.prune(keep)
    except BackupError as exc:
        # Redacted like every other error path: a prune that fails part way
        # names the backup directory it could not remove.
        printer.error(f"prune failed: {workspace.redactor.text(str(exc))}")
        return EXIT_FAILURE
    if as_json:
        printer.json({"pruned": list(removed), "kept": keep})
    else:
        printer.line(f"pruned {len(removed)} backup(s), keeping the newest {keep}")
        printer.lines(f"  removed {backup_id}" for backup_id in removed)
    return EXIT_OK


def run_restore_save(ctx: CliContext, *, backup_id: str, as_json: bool) -> int:
    """Handler for ``pz-agent restore-save``."""
    workspace = resolve_workspace(ctx)
    printer = Printer(ctx.stdout, ctx.stderr)
    manager = _manager(ctx, workspace)
    if manager is None:
        return _no_user_dir(printer)

    # ``list_processes`` is read from the module at call time rather than bound
    # as a default argument: that is the seam the tests drive to stand up a
    # machine with a Project Zomboid process on it.
    probe = probe_game_running(
        heartbeat=game_liveness(ctx, workspace),
        lister=list_processes,
    )
    running = game_running_for_restore(probe)
    if running:
        printer.error(
            f"refusing to restore: {workspace.redactor.text(probe.detail)}. "
            "Close Project Zomboid and run this again — restoring a save under a "
            "running game is how you get a corrupted one, and there is no flag to "
            "override this."
        )
        return EXIT_FAILURE

    try:
        # Passed rather than hard-coded: the value the probe produced is the value
        # the manager checks, so a later edit to the branch above cannot leave a
        # literal ``False`` behind claiming something nobody established.
        result = manager.restore(backup_id, game_running=running)
    except BackupError as exc:
        printer.error(f"restore failed: {workspace.redactor.text(str(exc))}")
        return EXIT_FAILURE
    # Stated in the past tense because it is printed after the restore: telling a
    # user to close the game "before restoring" once the files are already back
    # would describe a step that can no longer be taken.
    inference = (
        f"{workspace.redactor.text(probe.detail)}. That was read before the restore "
        "and not during it, so if Project Zomboid was launched while this ran, close "
        "it and load the save once before playing on."
    )
    if as_json:
        printer.json(
            {
                "backup_id": result.backup_id,
                "save_id": result.save_id,
                "target_dir": workspace.redact(result.target_dir),
                "file_count": result.file_count,
                "total_bytes": result.total_bytes,
                "game_probe": probe.to_dict(),
                "game_running_evidence": inference,
            }
        )
        return EXIT_OK
    printer.line(f"restored {result.save_id} from {result.backup_id}")
    printer.field("files", str(result.file_count))
    printer.field("bytes", str(result.total_bytes))
    printer.field("target", workspace.redact(result.target_dir))
    printer.error(inference)
    return EXIT_OK


def saves_root(workspace: Workspace) -> Path | None:
    """The ``Saves`` directory, when the Zomboid directory was found."""
    user_dir = workspace.user_dir
    return None if user_dir is None else user_dir / SAVES_DIR_NAME
