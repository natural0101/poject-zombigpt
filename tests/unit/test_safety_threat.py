"""Threat assessment: chasing beats close, and a pack beats a loner."""

from __future__ import annotations

import pytest

from pz_agent_core.protocol import DangerLevel, NearbyView, NearbyZombie, Position
from pz_agent_core.safety.threat import (
    DEFAULT_THREAT_CONFIG,
    ThreatConfig,
    assess_danger,
    assess_threat,
)
from tests.fixtures import make_player
from tests.fixtures.safety_builders import bleeding_wound, make_nearby, make_zombie

CALM = make_player()
BLEEDING = make_player(wounds=[bleeding_wound()])


def test_an_empty_world_is_not_dangerous() -> None:
    assert assess_danger(NearbyView(), CALM) is DangerLevel.NONE


def test_a_distant_chaser_outranks_a_much_closer_zombie_that_has_not_noticed() -> None:
    chaser = assess_danger(make_nearby(make_zombie("z1", 12.0, chasing=True)), CALM)
    unaware = assess_danger(make_nearby(make_zombie("z2", 4.0, chasing=False)), CALM)

    assert chaser > unaware
    assert chaser is DangerLevel.MEDIUM
    assert unaware is DangerLevel.LOW


def test_the_ordering_holds_at_contact_range_too() -> None:
    chaser = assess_danger(make_nearby(make_zombie("z1", 1.0, chasing=True)), CALM)
    unaware = assess_danger(make_nearby(make_zombie("z2", 1.0, chasing=False)), CALM)

    assert chaser is DangerLevel.CRITICAL
    assert unaware is DangerLevel.MEDIUM


@pytest.mark.parametrize(
    ("distance", "expected"),
    [
        (1.0, DangerLevel.CRITICAL),
        (2.0, DangerLevel.CRITICAL),
        (2.5, DangerLevel.HIGH),
        (6.0, DangerLevel.HIGH),
        (10.0, DangerLevel.MEDIUM),
        (15.0, DangerLevel.MEDIUM),
        (40.0, DangerLevel.LOW),
    ],
)
def test_a_chaser_escalates_as_it_closes(distance: float, expected: DangerLevel) -> None:
    assert assess_danger(make_nearby(make_zombie("z1", distance, chasing=True)), CALM) is expected


@pytest.mark.parametrize(
    ("distance", "expected"),
    [
        (1.0, DangerLevel.MEDIUM),
        (4.0, DangerLevel.LOW),
        (6.0, DangerLevel.LOW),
        (9.0, DangerLevel.NONE),
    ],
)
def test_an_unaware_zombie_stays_a_rung_lower(distance: float, expected: DangerLevel) -> None:
    assert assess_danger(make_nearby(make_zombie("z1", distance)), CALM) is expected


def test_an_unseen_zombie_at_arms_length_is_only_an_alert() -> None:
    hidden = make_zombie("z1", 1.5, visible=False)
    seen = make_zombie("z2", 1.5, visible=True)

    assert assess_danger(make_nearby(hidden), CALM) is DangerLevel.LOW
    assert assess_danger(make_nearby(seen), CALM) is DangerLevel.MEDIUM


def test_the_worst_zombie_sets_the_level() -> None:
    level = assess_danger(
        make_nearby(make_zombie("z1", 30.0), make_zombie("z2", 1.0, chasing=True)),
        CALM,
    )
    assert level is DangerLevel.CRITICAL


def test_a_pack_of_chasers_escalates_beyond_any_single_member() -> None:
    far = [make_zombie(f"z{i}", 14.0, chasing=True) for i in range(3)]
    assert assess_danger(make_nearby(far[0]), CALM) is DangerLevel.MEDIUM
    assert assess_danger(make_nearby(*far), CALM) is DangerLevel.HIGH


def test_a_swarm_escalates_twice() -> None:
    swarm = [make_zombie(f"z{i}", 14.0, chasing=True) for i in range(6)]
    assert assess_danger(make_nearby(*swarm), CALM) is DangerLevel.CRITICAL


def test_a_crowd_that_has_not_noticed_still_escalates_once() -> None:
    crowd = [make_zombie(f"z{i}", 12.0) for i in range(8)]
    assert assess_danger(make_nearby(*crowd), CALM) is DangerLevel.LOW

    closer = [make_zombie(f"z{i}", 5.0) for i in range(8)]
    assert assess_danger(make_nearby(*closer), CALM) is DangerLevel.MEDIUM


def test_zombies_beyond_the_alert_radius_do_not_form_a_crowd() -> None:
    distant = [make_zombie(f"z{i}", 40.0) for i in range(20)]
    assert assess_danger(make_nearby(*distant), CALM) is DangerLevel.NONE


def test_a_zombie_on_another_floor_is_capped() -> None:
    upstairs = make_zombie("z1", 1.0, chasing=True, floor=1)
    same_floor = make_zombie("z2", 1.0, chasing=True, floor=0)

    assert assess_danger(make_nearby(upstairs), CALM) is DangerLevel.MEDIUM
    assert assess_danger(make_nearby(same_floor), CALM) is DangerLevel.CRITICAL


def test_a_zombie_without_a_position_is_assumed_to_share_the_floor() -> None:
    # Unknown is not the same as safe: the guard must not talk itself down.
    assert assess_danger(make_nearby(make_zombie("z1", 1.0, chasing=True)), CALM) is (
        DangerLevel.CRITICAL
    )


def test_bleeding_raises_the_floor_even_in_an_empty_room() -> None:
    assert assess_danger(NearbyView(), BLEEDING) is DangerLevel.LOW


def test_bleeding_while_chased_escalates_a_rung() -> None:
    nearby = make_nearby(make_zombie("z1", 12.0, chasing=True))

    assert assess_danger(nearby, CALM) is DangerLevel.MEDIUM
    assert assess_danger(nearby, BLEEDING) is DangerLevel.HIGH


def test_bleeding_does_not_escalate_when_nothing_is_chasing() -> None:
    nearby = make_nearby(make_zombie("z1", 4.0))

    assert assess_danger(nearby, CALM) is DangerLevel.LOW
    assert assess_danger(nearby, BLEEDING) is DangerLevel.LOW


def test_a_negative_distance_cannot_slip_under_every_threshold() -> None:
    assert assess_danger(make_nearby(make_zombie("z1", -3.0, chasing=True)), CALM) is (
        DangerLevel.CRITICAL
    )


def test_the_assessment_reports_the_evidence_it_used() -> None:
    assessment = assess_threat(
        make_nearby(
            make_zombie("z1", 3.0, chasing=True),
            make_zombie("z2", 8.0),
        ),
        BLEEDING,
    )

    assert assessment.zombie_count == 2
    assert assessment.chasing_count == 1
    assert assessment.nearest_distance == 3.0
    assert assessment.nearest_chasing_distance == 3.0
    assert "chasing" in assessment.reason
    assert "bleeding" in assessment.reason
    # A reason is spoken aloud, so it may not carry a ref.
    assert "zombie:" not in assessment.reason


def test_the_reason_is_readable_when_nothing_is_around() -> None:
    assert assess_threat(NearbyView(), CALM).reason == "nothing nearby"
    assert assess_threat(NearbyView(), BLEEDING).reason == "bleeding, nothing nearby"


def test_thresholds_are_configurable() -> None:
    twitchy = ThreatConfig(critical_distance=20.0, close_distance=30.0, alert_distance=40.0)
    nearby = make_nearby(make_zombie("z1", 10.0, chasing=True))

    assert assess_danger(nearby, CALM, DEFAULT_THREAT_CONFIG) is DangerLevel.MEDIUM
    assert assess_danger(nearby, CALM, twitchy) is DangerLevel.CRITICAL


@pytest.mark.parametrize(
    "kwargs",
    [
        {"critical_distance": 0.0},
        {"critical_distance": 10.0, "close_distance": 5.0},
        {"close_distance": 30.0},
        {"chasing_horde": 0},
        {"chasing_horde": 9, "chasing_swarm": 2},
        {"crowd_count": 0},
    ],
)
def test_an_incoherent_config_is_refused(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="must be"):
        ThreatConfig(**kwargs)  # type: ignore[arg-type]


def test_a_zombie_on_another_floor_of_a_multi_storey_building_still_counts_a_little() -> None:
    player = make_player(position=Position(x=1200.0, y=3400.0, z=2, direction="S"))
    below = make_zombie("z1", 2.0, chasing=True, floor=0)

    assert assess_danger(make_nearby(below), player) is DangerLevel.MEDIUM


# --------------------------------------------------------------------------
# a zombie whose intent nobody could read
# --------------------------------------------------------------------------
# The mod omits ``chasing`` deliberately when the build exposes no ``getTarget``
# -- its own comment says an absent accessor "must stay absent so the sidecar is
# told it could not be read". The schema agrees: only ``ref`` and ``distance``
# are required. This side is where that declaration has to survive, because
# ``_zombie_level`` runs a full rung lower at every band for a zombie it
# believes is unaware.


def test_an_unread_intent_is_not_a_reading_of_calm() -> None:
    """Absent is not False. The parser is where the mod's declaration survives."""
    parsed = NearbyZombie.from_dict({"ref": "zombie:s:1", "distance": 3.0})

    assert parsed.chasing is None, (
        "the mod left chasing out to say it could not be read, and it arrived as a "
        "positive claim that the zombie is not chasing"
    )


def test_a_zombie_that_might_be_chasing_is_assessed_as_though_it_is() -> None:
    """The guard's whole job is the case nobody can rule out.

    Cost accepted deliberately, exactly as the failed zombie scan's floor was:
    on a build with no ``getTarget`` the agent is more cautious than it needs to
    be. The other direction is a chaser at contact range assessed as an unaware
    one, which is the guarantee the README makes about threat interruption.
    """
    for distance in (1.0, 4.0, 12.0):
        unknown = assess_danger(make_nearby(make_zombie("z1", distance, chasing=None)), CALM)
        chaser = assess_danger(make_nearby(make_zombie("z2", distance, chasing=True)), CALM)
        assert unknown is chaser, f"at {distance} tiles an unread intent dropped a rung"


def test_the_count_of_chasers_stays_a_count_of_known_chasers() -> None:
    """Treating unknowns as chasers for the ladder must not make them chasers in
    the evidence: "three chasing" has to mean three that were observed chasing,
    or the reason the agent speaks to the player is itself a false reading.
    """
    assessment = assess_threat(
        make_nearby(
            make_zombie("z1", 3.0, chasing=True),
            make_zombie("z2", 3.0, chasing=None),
            make_zombie("z3", 3.0, chasing=False),
        ),
        CALM,
    )

    assert assessment.chasing_count == 1
    assert assessment.chasing_unknown_count == 1


# --------------------------------------------------------------------------
# a body nobody could read
# --------------------------------------------------------------------------
# ``is_bleeding`` is ``any(w.bleeding for w in wounds)``, and the mod publishes
# an empty wound list both when nothing is wrong and when it could not read the
# body at all -- declaring the second case as ``observe.wounds_unknown``. Three
# separate files in this codebase cite "a missing is_bleeding never means 'not
# bleeding'" as settled practice while arguing for some other gate. This is that
# gate.


def test_an_unread_body_does_not_skip_the_bleeding_floor() -> None:
    """The floor exists because a bleeding player has a clock running."""
    unread = make_player(stats={"observe.wounds_unknown": True})

    quiet = assess_danger(make_nearby(), unread)

    assert quiet is not DangerLevel.NONE, (
        "the mod said it could not read the body, and the bleeding floor was "
        "skipped as though it had read one with nothing wrong with it"
    )


def test_a_body_that_was_read_and_is_whole_keeps_its_calm() -> None:
    """The control: an observed-healthy body must not be dragged up the ladder."""
    assert assess_danger(make_nearby(), CALM) is DangerLevel.NONE


def test_the_spoken_reason_does_not_claim_bleeding_nobody_saw() -> None:
    """The reason is spoken to the player, so it may not invent a wound."""
    unread = make_player(stats={"observe.wounds_unknown": True})

    reason = assess_threat(make_nearby(), unread).reason

    assert "bleeding" not in reason, reason
    assert "could not" in reason or "unread" in reason, reason
