"""The sequence an operator actually performs, end to end, through the real CLI.

Every step of this had a unit test. The sequence did not, and the sequence is
what a person does: take a backup, prepare the evidence tree, run a scenario.
The gap that mattered lived exactly between two of those steps — ``prepare``
verified a test save and a backup that reads back, wrote ``prepare.json``, and
``run`` never looked at it. Twenty scenarios that wound the character and end in
restores would start against any save at all.

That gate exists now, which creates the opposite risk and the reason this file
does: a gate whose precondition can never be satisfied is a bricked release, and
nothing here could tell the difference between "refuses correctly" and "refuses
always". So this drives the whole loop and asserts both directions — the refusal
before, and the run *unblocked* after.

Everything is the real CLI over a synthetic Zomboid directory. Nothing is
mocked except the absence of a game, which is the one thing that cannot be
supplied here and which the runner already reports as ``BLOCKED`` rather than
as a pass.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Final

import pytest

from pz_agent_cli.context import BACKUP_DIR_NAME, EXIT_FAILURE, EXIT_OK, STATE_DIR_NAME
from pz_agent_cli.livetest.evidence import EvidenceLayout
from pz_agent_cli.livetest.scenarios import SCENARIO_IDS
from tests.fixtures.cli_worlds import CliWorld, make_world

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCHEMA_SOURCE: Final = REPO_ROOT / "evidence" / "schema"

#: The name has to say "test": prepare refuses a save whose name does not, and
#: that refusal is the whole reason `--save` has no default.
TEST_SAVE: Final = "Muldraugh, KY/testworld"


@pytest.fixture
def operator(tmp_path: Path) -> tuple[CliWorld, Path, Path]:
    """A machine with a Zomboid directory, a test save, and no game running."""
    world = make_world(tmp_path)
    saves = tmp_path / "Zomboid" / "Saves" / "Muldraugh, KY" / "testworld"
    saves.mkdir(parents=True, exist_ok=True)
    (saves / "map_p.bin").write_text("map", encoding="utf-8")
    (saves / "players.db").write_text("player", encoding="utf-8")

    return world, tmp_path / "Zomboid", _evidence_tree(tmp_path)


def _evidence_tree(base: Path) -> Path:
    """An evidence directory with the schemas prepare insists on."""
    evidence = base / "evidence"
    layout = EvidenceLayout(evidence)
    layout.ensure_tree(SCENARIO_IDS)
    for schema in SCHEMA_SOURCE.glob("*.json"):
        shutil.copyfile(schema, layout.schema_dir / schema.name)
    return evidence


def _live(world: CliWorld, zomboid: Path, evidence: Path, *argv: str) -> int:
    return world.run(
        "--zomboid-dir", str(zomboid), "live-test", "--evidence-dir", str(evidence), *argv
    )


def test_the_loop_a_person_performs_works_from_end_to_end(
    operator: tuple[CliWorld, Path, Path],
) -> None:
    """backup-save, prepare, run — in that order, with the real commands."""
    world, zomboid, evidence = operator

    # Before anything: run is refused, because nothing has proved the world is
    # safe to experiment on.
    world.reset_streams()
    assert _live(world, zomboid, evidence, "run", "--scenario", SCENARIO_IDS[3]) == EXIT_FAILURE
    assert "prepare has not completed" in world.stderr

    # 1. The backup. Without it prepare refuses, and prepare is right to.
    world.reset_streams()
    assert world.run("--zomboid-dir", str(zomboid), "backup-save", TEST_SAVE) == EXIT_OK

    # 2. Prepare. This is where the save name and the backup are checked.
    world.reset_streams()
    assert _live(world, zomboid, evidence, "prepare", "--save", TEST_SAVE) == EXIT_OK, world.stderr
    record = json.loads(EvidenceLayout(evidence).prepare_path.read_text(encoding="utf-8"))
    assert record["ready"] is True
    assert record["save_id"] == TEST_SAVE
    assert record["backup_id"], "prepare must name the backup it verified, not merely find one"

    # 3. Run is now permitted. It cannot pass — there is no game — and it says
    # BLOCKED rather than inventing an observation, which is the correct answer.
    world.reset_streams()
    _live(world, zomboid, evidence, "run", "--scenario", SCENARIO_IDS[3])
    assert "prepare has not completed" not in world.stderr, (
        "the gate stayed shut after a successful prepare, which would brick the release"
    )


def test_prepare_refuses_a_save_whose_name_does_not_say_test(
    operator: tuple[CliWorld, Path, Path],
) -> None:
    """The user's standing instruction, enforced rather than requested.

    "Создавай отдельный тестовый сейв" — these scenarios wound the character and
    end in restores, so the world has to be one somebody made for that.
    """
    world, zomboid, evidence = operator
    main_save = zomboid / "Saves" / "Muldraugh, KY" / "survivor"
    main_save.mkdir(parents=True, exist_ok=True)
    (main_save / "map_p.bin").write_text("map", encoding="utf-8")

    world.reset_streams()
    exit_code = _live(world, zomboid, evidence, "prepare", "--save", "Muldraugh, KY/survivor")

    assert exit_code == EXIT_FAILURE
    assert "does not contain 'test'" in world.stdout + world.stderr
    assert not EvidenceLayout(evidence).prepare_path.exists()


def test_prepare_refuses_when_no_backup_covers_the_save(
    operator: tuple[CliWorld, Path, Path],
) -> None:
    """A test world with no backup is still somebody's afternoon."""
    world, zomboid, evidence = operator

    world.reset_streams()
    exit_code = _live(world, zomboid, evidence, "prepare", "--save", TEST_SAVE)

    assert exit_code == EXIT_FAILURE
    assert "no backup" in world.stdout + world.stderr
    assert not EvidenceLayout(evidence).prepare_path.exists()


def test_prepare_refuses_when_the_save_directory_is_gone(
    operator: tuple[CliWorld, Path, Path],
) -> None:
    """A backup that covers a save says nothing about the save still being there.

    The order of the checks is the point: ``prepare`` looks for the save
    directory before it looks for a backup, because a backup record survives the
    save it was taken from. Renaming or deleting the test world after taking the
    backup leaves a machine where the backup verifies perfectly and the world it
    describes does not exist. Without this refusal ``prepare`` writes ``ready``,
    ``run`` unlocks, and twenty scenarios drive against a save id that resolves
    to nothing — with the record naming a backup as if it covered them.

    Found by planting: neutralising the refusal left the whole suite green.
    """
    world, zomboid, evidence = operator

    world.reset_streams()
    assert world.run("--zomboid-dir", str(zomboid), "backup-save", TEST_SAVE) == EXIT_OK

    shutil.rmtree(zomboid / "Saves" / "Muldraugh, KY" / "testworld")

    world.reset_streams()
    exit_code = _live(world, zomboid, evidence, "prepare", "--save", TEST_SAVE)

    said = world.stdout + world.stderr
    assert exit_code == EXIT_FAILURE, said
    assert "no save directory" in said, (
        "prepare armed against a save that is not on disk; the backup it found "
        "covers a world that no longer exists"
    )
    assert not EvidenceLayout(evidence).prepare_path.exists()


def test_prepare_refuses_a_backup_that_does_not_read_back(
    operator: tuple[CliWorld, Path, Path],
) -> None:
    """A backup that exists and a backup that restores are different facts.

    Both this repository's prose and ``_unprepared``'s docstring draw exactly
    this distinction — a backup that *reads back* rather than merely existing —
    and nothing tested it: replacing ``manager.verify(...)`` with ``pass`` left
    every one of the suite's tests green. The refusal that stands between a
    batch of deliberately destructive scenarios and an unrestorable save was the
    one refusal in ``prepare`` with no guard.

    The damage planted here is the kind that actually happens and the kind an
    existence check cannot see: the file is present, its size is unchanged, its
    contents are not what was written. Only re-hashing finds it, which is what
    ``verify`` does and what ``prepare`` must not skip.
    """
    world, zomboid, evidence = operator

    world.reset_streams()
    assert world.run("--zomboid-dir", str(zomboid), "backup-save", TEST_SAVE) == EXIT_OK

    rotted = _rot_one_file(zomboid / STATE_DIR_NAME / BACKUP_DIR_NAME)

    world.reset_streams()
    exit_code = _live(world, zomboid, evidence, "prepare", "--save", TEST_SAVE)

    said = world.stdout + world.stderr
    assert exit_code == EXIT_FAILURE, said
    assert "does not verify" in said, (
        f"prepare accepted a backup whose {rotted.name} no longer matches its manifest, "
        "so the restore every destructive scenario ends with would fail when it is "
        "the only thing left"
    )
    assert "take another one" in said, "the refusal must name its remedy"
    assert not EvidenceLayout(evidence).prepare_path.exists()


def _rot_one_file(backup_root: Path) -> Path:
    """Corrupt one backed-up file in place, keeping its length.

    Same size, different bytes: the file still exists, the directory listing is
    unchanged, and only the SHA-256 in the manifest disagrees. Anything that
    changed the size would also be caught by a cheaper check, which would make
    this test pass for a weaker reason than the one it is about.
    """
    candidates = sorted(
        path
        for directory in backup_root.iterdir()
        if directory.is_dir()
        for path in (directory / "data").rglob("*")
        if path.is_file()
    )
    assert candidates, f"no backed-up files under {backup_root}; the fixture took no backup"
    target = candidates[0]
    original = target.read_bytes()
    assert original, f"{target} is empty, so flipping a byte would not change its digest"
    target.write_bytes(bytes((original[0] ^ 0xFF,)) + original[1:])
    assert len(target.read_bytes()) == len(original)
    return target


def test_prepare_refuses_when_no_zomboid_directory_was_found(tmp_path: Path) -> None:
    """Without a game directory there is no save to check, and no check to trust.

    ``prepare``'s answer to this is a refusal naming ``pz-agent doctor``, which
    is the subcommand that explains *why* discovery failed. It was unguarded,
    and the alternative to a refusal here is not a helpful error: every later
    line of ``_verify_test_save`` assumes a user directory, so the operator
    would meet a traceback instead of the one command that diagnoses this.

    The machine is built without a Zomboid directory rather than pointed at a
    missing one: ``--zomboid-dir`` is taken as the answer whether or not the
    path exists, so an override reaches the *save* check, not this one. That is
    the honest way to reproduce a game that was never installed where discovery
    looks.
    """
    world = make_world(tmp_path, with_user_dir=False)
    evidence = _evidence_tree(tmp_path)

    world.reset_streams()
    exit_code = world.run(
        "live-test", "--evidence-dir", str(evidence), "prepare", "--save", TEST_SAVE
    )

    said = world.stdout + world.stderr
    assert exit_code == EXIT_FAILURE, said
    assert "no Zomboid directory" in said, said
    assert "doctor" in said, "the refusal must name the subcommand that explains discovery"
    assert not EvidenceLayout(evidence).prepare_path.exists()


def test_a_refusal_for_a_missing_schema_says_how_to_fix_it(
    operator: tuple[CliWorld, Path, Path], tmp_path: Path
) -> None:
    """Every other refusal here names its way out; this one did not.

    An operator meets it by pointing ``--evidence-dir`` somewhere new, or by
    running the bundled executable directly, where its idea of "the directory I
    came from" is a temporary unpack folder.
    """
    world, zomboid, _ = operator
    world.reset_streams()
    world.run("--zomboid-dir", str(zomboid), "backup-save", TEST_SAVE)

    world.reset_streams()
    exit_code = _live(world, zomboid, tmp_path / "nowhere", "prepare", "--save", TEST_SAVE)

    assert exit_code == EXIT_FAILURE
    said = world.stdout + world.stderr
    assert "evidence schema missing" in said
    assert "run-live-tests.bat" in said or "copy evidence/schema" in said, (
        "the refusal must name a remedy, not only the missing path"
    )
