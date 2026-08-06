"""The save id a backup carries, and the rule that it is never inferred.

A backup is named on disk by its save *directory*; the autonomy gate is given a
digest the mod computed inside the game. The manifest field tested here is the
only thing that ever connects the two, so the interesting cases are all about
what happens when it is absent: an old manifest, a backup taken with nothing
attached, a manifest somebody edited. None of them may produce an attribution,
and none of them may produce a crash either — an unattributed backup is still a
backup, and it still has to list, verify, restore and prune.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pz_agent_core.platform.backup import (
    MANIFEST_NAME,
    BackupCorruptError,
    BackupError,
    BackupManager,
    BackupRecord,
    attributed_to,
)
from pz_agent_core.protocol.messages import MAX_SAVE_ID_LEN
from tests.fixtures.platform_trees import CYRILLIC_USER, FakeClock, make_save, make_user_dir

SAVE_ID = "Survivor/09-07-1993"
OTHER_SAVE_ID = "Builder/12-01-1994"

SAVE_FILES: dict[str, bytes] = {"map_t.bin": b"tiles", "players.db": b"player-state"}

#: What the mod's ``ObserveModel.saveId`` produces: two 32-bit FNV-1a digests,
#: hex, of a save key that never crosses the boundary.
OBSERVED = "1f3c9a2b7e40d115"
OBSERVED_OTHER = "9911aabb00cc22dd"


def _manager(tmp_path: Path) -> tuple[BackupManager, Path]:
    user_dir = make_user_dir(tmp_path / "Users" / CYRILLIC_USER)
    return BackupManager(user_dir, tmp_path / "backups", clock=FakeClock()), user_dir


def _rewrite_manifest(record: BackupRecord, **changes: object) -> None:
    """Edit one backup's manifest in place, the way an old build or a hand would."""
    manifest = record.directory / MANIFEST_NAME
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is _DROP:
            payload.pop(key, None)
        else:
            payload[key] = value
    manifest.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


class _Drop:
    """Sentinel for "remove this key", which None cannot express here."""


_DROP = _Drop()


# ---------------------------------------------------------------------------
# what a backup records
# ---------------------------------------------------------------------------


def test_the_observed_save_id_is_recorded_when_one_was_supplied(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)

    record = manager.create(SAVE_ID, observed_save_id=OBSERVED)

    assert record.observed_save_id == OBSERVED
    payload = json.loads((record.directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert payload["observed_save_id"] == OBSERVED
    assert manager.get(record.backup_id).observed_save_id == OBSERVED


def test_a_backup_with_nothing_attached_records_none_rather_than_the_directory(
    tmp_path: Path,
) -> None:
    """The save directory is *not* a fallback: it is the identifier that does not match."""
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)

    record = manager.create(SAVE_ID)

    assert record.observed_save_id is None
    assert record.save_id == SAVE_ID
    payload = json.loads((record.directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    # Written as an explicit null: a reader can then tell this backup from one
    # made before the field existed, and both from one that carries an id.
    assert "observed_save_id" in payload
    assert payload["observed_save_id"] is None


def test_a_manifest_from_before_the_field_existed_round_trips_as_unattributed(
    tmp_path: Path,
) -> None:
    """Requirement of the field's whole design: reading an old backup cannot raise."""
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)
    record = manager.create(SAVE_ID, observed_save_id=OBSERVED)
    _rewrite_manifest(record, observed_save_id=_DROP)

    reloaded = manager.get(record.backup_id)

    assert reloaded.observed_save_id is None
    assert reloaded.save_id == SAVE_ID
    assert manager.list_unreadable() == ()
    assert [item.backup_id for item in manager.list_backups()] == [record.backup_id]
    assert manager.verify(record.backup_id).backup_id == record.backup_id
    assert attributed_to((reloaded,), OBSERVED) == ()


def test_a_manifest_whose_save_id_is_not_one_the_mod_could_report_is_corruption(
    tmp_path: Path,
) -> None:
    """Present-and-unusable is neither of the two ordinary absences.

    Reading it as "unattributed" would let anything that rewrote a manifest do so
    invisibly, and this field decides whether an agent acts unasked.
    """
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)
    record = manager.create(SAVE_ID, observed_save_id=OBSERVED)
    _rewrite_manifest(record, observed_save_id=42)

    with pytest.raises(BackupCorruptError, match="observed_save_id"):
        manager.get(record.backup_id)
    assert any("observed_save_id" in problem for problem in manager.list_unreadable())


@pytest.mark.parametrize("value", ["", " ", "has space", "a" * (MAX_SAVE_ID_LEN + 1), "tab\there"])
def test_an_unusable_observed_save_id_is_refused_at_creation(tmp_path: Path, value: str) -> None:
    """A caller that lost the value must not produce a backup that names nothing."""
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)

    with pytest.raises(BackupError, match="observed save id"):
        manager.create(SAVE_ID, observed_save_id=value)

    assert manager.list_backups() == ()


def test_the_manifest_still_round_trips_through_from_dict(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)
    record = manager.create(SAVE_ID, observed_save_id=OBSERVED)

    payload = json.loads((record.directory / MANIFEST_NAME).read_text(encoding="utf-8"))

    assert BackupRecord.from_dict(payload, directory=record.directory) == record


# ---------------------------------------------------------------------------
# attribution
# ---------------------------------------------------------------------------


def test_attribution_matches_the_recorded_id_and_not_the_newest_backup(tmp_path: Path) -> None:
    """The assertion the field exists for, at the level the filter lives on."""
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)
    make_save(user_dir, OTHER_SAVE_ID, SAVE_FILES)
    mine = manager.create(SAVE_ID, observed_save_id=OBSERVED)
    # Taken later, so any newest-wins rule would return this one instead.
    theirs = manager.create(OTHER_SAVE_ID, observed_save_id=OBSERVED_OTHER)

    matched = manager.attributed_to(OBSERVED)

    assert [record.backup_id for record in matched] == [mine.backup_id]
    assert [record.backup_id for record in manager.attributed_to(OBSERVED_OTHER)] == [
        theirs.backup_id
    ]
    assert manager.attributed_to("1f3c9a2b7e40d116") == ()


def test_two_backups_of_one_save_come_back_newest_first(tmp_path: Path) -> None:
    """Ordering is the only preference; it never widens what matches."""
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)
    older = manager.create(SAVE_ID, observed_save_id=OBSERVED)
    newer = manager.create(SAVE_ID, observed_save_id=OBSERVED)

    matched = manager.attributed_to(OBSERVED)

    assert [record.backup_id for record in matched] == [newer.backup_id, older.backup_id]


def test_an_empty_save_id_selects_nothing_including_the_unattributed(tmp_path: Path) -> None:
    """ "No session reported a save" must not match every backup that reported none."""
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)
    manager.create(SAVE_ID)

    assert manager.attributed_to("") == ()


# ---------------------------------------------------------------------------
# the age a witness needs
# ---------------------------------------------------------------------------


def test_created_at_ms_reads_the_manifest_timestamp(tmp_path: Path) -> None:
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)

    record = manager.create(SAVE_ID, observed_save_id=OBSERVED)

    # FakeClock starts at 2026-08-05T12:00:00Z.
    assert record.created_at_ms == 1785931200000


@pytest.mark.parametrize(
    "created_at",
    [
        "not a timestamp",
        # No offset: this names a different instant in every time zone, and
        # assuming UTC would report an age that is wrong by hours.
        "2026-08-05T12:00:00",
    ],
)
def test_a_timestamp_that_names_no_instant_reads_as_no_age(tmp_path: Path, created_at: str) -> None:
    manager, user_dir = _manager(tmp_path)
    make_save(user_dir, SAVE_ID, SAVE_FILES)
    record = manager.create(SAVE_ID, observed_save_id=OBSERVED)
    _rewrite_manifest(record, created_at=created_at)

    assert manager.get(record.backup_id).created_at_ms is None
