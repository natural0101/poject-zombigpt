"""What stands between a save and an agent acting on it (E12-M03-T011/T012).

"Arming" in this build is two words for two different grants. ``pz-agent arm``
grants the session authority to carry out what the *user* asks for; §7.7 puts a
second, higher bar in front of the agent acting **unasked**, and that bar is a
backup of the save being played. :class:`~pz_agent_core.policy.autonomy
.AutonomyGate` is where it stands, which is why it takes the evidence as a
keyword with no default: a caller that never thought about backups cannot get
autonomy by forgetting to mention them.

The two claims tested here are the ones a user's saved game rests on.

* **No backup, no unasked action** (T011) — not on the first tick and not on the
  two-hundredth, and no configuration turns it off. ``session.require_backup``
  decides whether an *unverified* backup counts; nothing anywhere decides that
  no backup counts.
* **The backup has to be of this save** (T012) — and "this save" means the id the
  mod computed inside the game, compared by exact equality. The check is made
  twice by two subsystems that cannot see each other's inputs:
  :meth:`~pz_agent_core.platform.backup.BackupManager.attributed_to` refuses to
  offer a backup whose manifest records another id, and the gate refuses
  evidence naming another save even if something hands it one.

Both are exercised through the real chain — a real backup root on disk, the real
attribution witness, the real gate — because the interesting failure is not any
one of those refusing, it is the two of them disagreeing about which save is
being played.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_cli.autonomy import BackupAttribution
from pz_agent_cli.config import SCHEMA
from pz_agent_core.platform.backup import BackupManager
from pz_agent_core.policy.autonomy import (
    AutonomyConfig,
    AutonomyDecision,
    AutonomyGate,
    AutonomyOutcome,
    BackupEvidence,
)
from pz_agent_core.policy.selection import CapabilitySnapshot
from pz_agent_core.protocol import Observation, ReasonCode
from tests.fixtures import make_game
from tests.fixtures.autonomy_worlds import NOW_MS, autonomous_observation
from tests.fixtures.platform_trees import CYRILLIC_USER, FakeClock, make_save, make_user_dir
from tests.fixtures.policy_items import food_item, hungry_player, inventory

CAPABILITIES: Final = CapabilitySnapshot.verified("eat_percentage")

#: The save as two identifiers that cannot be computed from each other: the
#: directory a backup is named by, and the digest the mod reports for it.
SAVE_DIR: Final = "Survivor/09-07-1993"
OTHER_SAVE_DIR: Final = "Builder/12-01-1994"
OBSERVED: Final = "1f3c9a2b7e40d115"
OBSERVED_OTHER: Final = "9911aabb00cc22dd"

SAVE_FILES: Final[dict[str, bytes]] = {"map_t.bin": b"tiles", "players.db": b"player-state"}

#: Above the §17.1 hunger trigger and with something to eat in reach, so a
#: refusal here is about the backup rather than about there being nothing to do.
HUNGRY: Final = 0.75


def _world(save_id: str = OBSERVED, **overrides: Any) -> Observation:
    """An armed AUTONOMOUS session on *save_id*, with a real need to serve."""
    return autonomous_observation(
        game=make_game(save_id=save_id),
        player=hungry_player(HUNGRY),
        inventory=inventory(food_item("f1")),
        **overrides,
    )


def _decide(
    gate: AutonomyGate,
    backup: BackupEvidence | None,
    *,
    world: Observation | None = None,
    now_ms: int = NOW_MS,
) -> AutonomyDecision:
    return gate.evaluate(
        _world() if world is None else world,
        now_ms=now_ms,
        backup=backup,
        capabilities=CAPABILITIES,
    )


def _backups(tmp_path: Path) -> tuple[BackupManager, Path]:
    user_dir = make_user_dir(tmp_path / "Users" / CYRILLIC_USER)
    for save_dir in (SAVE_DIR, OTHER_SAVE_DIR):
        make_save(user_dir, save_dir, SAVE_FILES)
    return BackupManager(user_dir, tmp_path / "backups", clock=FakeClock()), user_dir


# ---------------------------------------------------------------------------
# T011: no backup, no unasked action
# ---------------------------------------------------------------------------


def test_a_save_with_no_backup_is_asked_about_and_never_acted_on() -> None:
    gate = AutonomyGate()

    decision = _decide(gate, None)

    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert not decision.should_act
    assert decision.reason_code is ReasonCode.PRECONDITION_FAILED
    assert "no backup" in decision.detail
    assert gate.actions_spent(NOW_MS) == 0, "a refused tick must not spend the action budget"


def test_the_refusal_does_not_wear_off_after_enough_ticks() -> None:
    """A hungry character does not become an argument for acting unbacked.

    Two hundred ticks of the same unanswered need is exactly the pressure a
    "just this once" branch would relieve, so the loop is run rather than
    assumed to be stateless.
    """
    gate = AutonomyGate()

    outcomes = {_decide(gate, None, now_ms=NOW_MS + tick * 1_000).outcome for tick in range(200)}

    assert outcomes == {AutonomyOutcome.ASK_USER}
    assert gate.actions_spent(NOW_MS + 200_000) == 0


def test_the_same_gate_acts_once_a_confirmed_backup_of_this_save_exists() -> None:
    """The control: the refusals above are the backup gate, not a dead gate."""
    gate = AutonomyGate()

    decision = _decide(gate, BackupEvidence(save_id=OBSERVED, created_at_ms=NOW_MS, verified=True))

    assert decision.should_act
    assert gate.actions_spent(NOW_MS) == 1


def test_an_unverified_backup_is_not_a_confirmed_one() -> None:
    """`verified` means the hashes were checked; a listing proves nothing."""
    decision = _decide(
        AutonomyGate(), BackupEvidence(save_id=OBSERVED, created_at_ms=NOW_MS, verified=False)
    )

    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert decision.reason_code is ReasonCode.PRECONDITION_FAILED
    assert "never been verified" in decision.detail


def test_the_setting_that_relaxes_verification_does_not_relax_existence() -> None:
    """``session.require_backup = false`` is the far end of what is configurable.

    It lets a deployment that verifies its backups some other way proceed on an
    unverified one. It does not, and must not, turn "there is no backup at all"
    into permission.
    """
    relaxed = AutonomyGate(config=AutonomyConfig(require_verified_backup=False))

    unverified = BackupEvidence(save_id=OBSERVED, created_at_ms=NOW_MS, verified=False)
    assert _decide(relaxed, unverified).should_act

    missing = _decide(relaxed, None)
    assert missing.outcome is AutonomyOutcome.ASK_USER
    assert missing.reason_code is ReasonCode.PRECONDITION_FAILED


def test_no_other_setting_looks_like_a_way_round_the_backup_gate() -> None:
    """A gate with a switch beside it is not a gate."""
    escapes = sorted(
        f"{table}.{key}"
        for table, keys in SCHEMA.items()
        for key in keys
        if key != "require_backup"
        and any(word in key for word in ("backup", "unsafe", "override", "bypass", "force"))
    )
    assert escapes == [], f"these look like ways around the backup requirement: {escapes}"


def test_the_gate_cannot_be_asked_without_saying_whether_a_backup_exists() -> None:
    """No default, so no caller acquires autonomy by never mentioning backups."""
    with pytest.raises(TypeError, match="backup"):
        AutonomyGate().evaluate(  # type: ignore[call-arg]
            _world(), now_ms=NOW_MS, capabilities=CAPABILITIES
        )


# ---------------------------------------------------------------------------
# T012: the backup has to be of this save
# ---------------------------------------------------------------------------


def test_evidence_naming_another_save_is_refused_by_the_gate() -> None:
    decision = _decide(
        AutonomyGate(),
        BackupEvidence(save_id=OBSERVED_OTHER, created_at_ms=NOW_MS, verified=True),
    )

    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert decision.reason_code is ReasonCode.SAVE_CHANGED
    assert "different save" in decision.detail


def test_the_relaxed_configuration_still_refuses_another_saves_backup() -> None:
    """The save check runs before the verification check, and stays absolute."""
    decision = _decide(
        AutonomyGate(config=AutonomyConfig(require_verified_backup=False)),
        BackupEvidence(save_id=OBSERVED_OTHER, created_at_ms=NOW_MS, verified=True),
    )

    assert decision.reason_code is ReasonCode.SAVE_CHANGED


def test_a_backup_of_another_save_is_never_offered_as_this_ones_witness(
    tmp_path: Path,
) -> None:
    """The other half of the pair, on the real backup root.

    The only backup on this machine is of another world, and it is the newest
    one there is — so a newest-wins or only-one-backup shortcut would hand it
    over. Attribution is exact equality against the id the mod itself reported,
    so nothing comes back and the gate asks.
    """
    manager, user_dir = _backups(tmp_path)
    theirs = manager.create(OTHER_SAVE_DIR, observed_save_id=OBSERVED_OTHER)
    witness = BackupAttribution(manager)

    assert [record.backup_id for record in manager.list_backups()] == [theirs.backup_id]
    assert manager.attributed_to(OBSERVED) == ()
    assert witness(OBSERVED) is None
    assert "no backup here records the save id" in witness.last_detail

    decision = _decide(AutonomyGate(), witness(OBSERVED))
    assert decision.outcome is AutonomyOutcome.ASK_USER
    assert not decision.should_act
    assert user_dir.joinpath("Saves", *SAVE_DIR.split("/")).is_dir(), (
        "the save being played is on the machine; only its backup is missing"
    )


def test_the_backup_the_mod_can_name_is_the_one_that_unlocks_the_gate(tmp_path: Path) -> None:
    """The whole chain, end to end, in the state where autonomy is allowed."""
    manager, _ = _backups(tmp_path)
    manager.create(OTHER_SAVE_DIR, observed_save_id=OBSERVED_OTHER)
    mine = manager.create(SAVE_DIR, observed_save_id=OBSERVED)
    witness = BackupAttribution(manager)

    evidence = witness(OBSERVED)

    assert evidence is not None
    assert evidence.save_id == OBSERVED
    assert evidence.verified, "the witness checked the hashes before saying so"
    assert evidence.created_at_ms == manager.get(mine.backup_id).created_at_ms
    assert _decide(AutonomyGate(), evidence).should_act


def test_a_backup_whose_bytes_no_longer_match_its_manifest_is_not_a_safety_net(
    tmp_path: Path,
) -> None:
    """ "Confirmed" is a claim about the hashes, so it is withdrawn when they fail.

    Skipped rather than offered unverified: a deployment running with
    ``require_backup = false`` would otherwise be handed a backup that is known
    not to restore.
    """
    manager, _ = _backups(tmp_path)
    record = manager.create(SAVE_DIR, observed_save_id=OBSERVED)
    (record.data_dir / "map_t.bin").write_bytes(b"rot")

    witness = BackupAttribution(manager)

    assert witness(OBSERVED) is None
    assert "failed its hash check" in witness.last_detail
    assert not _decide(AutonomyGate(), witness(OBSERVED)).should_act
