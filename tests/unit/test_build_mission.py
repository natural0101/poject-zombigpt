"""The build mission: one placement, decided before it is asked for, proven after.

Driven at the mission seam like the craft mission, because these are the
promises the building wave's safety argument turns into behaviour — and this is
the wave where a promise broken cannot be walked back:

* the deterministic building policy decides before the one command, so every
  refusal costs a sentence and no world change;
* a placement that would seal the character into the squares this observation
  described is refused, and the refusal is typed ``WOULD_TRAP_PLAYER``;
* a square something already stands on is refused, and nothing is ever cleared;
* one ``building.build`` per mission and no ``count`` anywhere in its arguments
  — the attempt ceiling is one and a limits object cannot raise it;
* a refused placement is never retried at another square: the square is the
  user's, and the mission carries exactly the one it was given;
* a shortfall is a report, with the missing material named and no step going
  out to look for it;
* success is the structure *observed* standing on the square — an object that
  was not there before, or the observer's own assessment turning to blocked —
  never the mod saying the build finished;
* every refusal's ``detail`` fits the one bounded printable line a goal record
  will accept, however the game spelled the material it names.
"""

from __future__ import annotations

from typing import Any

import pytest

from pz_agent_cli.build_mission import (
    BUILD_PHASES,
    ENDED_REFUSED,
    ENDED_UNCONFIRMED,
    MAX_BUILD_ATTEMPTS,
    REFUSAL_REASONS,
    BuildMissionLimits,
    BuildStructureMission,
)
from pz_agent_cli.loot_mission import (
    ENDED_COMPLETE,
    ENDED_NO_PROGRESS,
    MissionComplete,
    MissionProbe,
    MissionRefused,
    MissionStep,
)
from pz_agent_core.actions.adapters import building as building_adapter
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.goals.model import MAX_DETAIL_CHARS, GoalKind, GoalState, GoalTransition
from pz_agent_core.policy.building import (
    BUILDING_KEY,
    SEMANTIC_BLOCKED,
    SEMANTIC_LOADED,
    BuildingRefusal,
)
from pz_agent_core.policy.config import PolicyConfig
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ItemView,
    NearbyObject,
    Observation,
    ReasonCode,
)
from tests.fixtures import DEFAULT_SESSION
from tests.fixtures.adapter_worlds import (
    HOME_X,
    HOME_Y,
    HOME_Z,
    MAIN_REF,
    a_square,
    a_world,
    a_world_object,
    an_item,
    main_container,
    object_ref,
    square_ref,
)

GOAL_ID = "7c2a1d40-0000-4000-8000-0000000000b1"

WALL = "WoodenWall"
PLANK = "Base.Plank"
NAILS = "Base.Nails"

TARGET = (HOME_X + 1, HOME_Y, HOME_Z)
TARGET_REF = square_ref(TARGET[0], TARGET[1], TARGET[2])


# --------------------------------------------------------------------------
# worlds
# --------------------------------------------------------------------------


def structure_payload(**overrides: Any) -> dict[str, Any]:
    """A wall the character knows how to build, out of one plank."""
    payload: dict[str, Any] = {
        "name": WALL,
        "display_name": "Wooden Wall",
        "known": True,
        "blocks_movement": True,
        "materials": [{"full_type": PLANK, "count": 1}],
    }
    payload.update(overrides)
    return payload


def plank(
    runtime_id: str = "m1",
    *,
    full_type: str = PLANK,
    structures: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> ItemView:
    """One material item, carrying the building readout it participates in."""
    return an_item(
        runtime_id=runtime_id,
        container_ref=MAIN_REF,
        full_type=full_type,
        display_name=full_type,
        category="Item",
        extra={
            BUILDING_KEY: {
                "structure_count": 1,
                "known_structure_count": 1,
                "structures": structures if structures is not None else [structure_payload()],
            }
        },
        **overrides,
    )


def window(*, sealed: bool = False, unloaded: bool = False) -> list[NearbyObject]:
    """The three-by-three block around the character, as the observer describes it.

    ``sealed`` blocks every square but the character's own and the target, so
    the target is the last exit this observation can see — the picture in which
    a wall there is the mistake with no undo. ``unloaded`` drops the ``loaded``
    mark from the target alone, which is the other thing a placement cannot
    reason about.
    """
    squares: list[NearbyObject] = []
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            x, y = HOME_X + dx, HOME_Y + dy
            own = (dx, dy) == (0, 0)
            target = (x, y) == (TARGET[0], TARGET[1])
            marks = [SEMANTIC_LOADED]
            if sealed and not (own or target):
                marks.append(SEMANTIC_BLOCKED)
            if unloaded and target:
                marks = []
            squares.append(a_square(x, y, HOME_Z, semantics=marks))
    return squares


def a_wall_on_target(runtime_id: str = "wall1") -> NearbyObject:
    """A structure standing on the target square, as the observer reports one."""
    return a_world_object(
        object_ref(runtime_id),
        x=TARGET[0],
        y=TARGET[1],
        z=TARGET[2],
        kind="object",
        semantics=["obstacle"],
    )


def site(
    seq: int = 1,
    *items: ItemView,
    objects: list[NearbyObject] | None = None,
    sealed: bool = False,
    unloaded: bool = False,
    no_nearby: bool = False,
) -> Observation:
    """The workable world: one plank, the wall's readout, a window to build in."""
    return a_world(
        seq=seq,
        items=list(items) if items else [plank()],
        containers=[main_container()],
        objects=window(sealed=sealed, unloaded=unloaded) + list(objects or []),
        no_nearby=no_nearby,
    )


def succeeded(request: ActionRequest, *, seq: int, evidence: dict[str, object]) -> ActionResult:
    return ActionResult.succeeded(
        session_id=request.session_id,
        seq=seq,
        command_id=f"c-{seq}",
        action=request.action.value,
        timestamp_ms=1_700_000_000_000 + seq,
        evidence=dict(evidence),
    )


def failed(request: ActionRequest, *, seq: int, reason: ReasonCode) -> ActionResult:
    return ActionResult.failure(
        session_id=request.session_id,
        seq=seq,
        command_id=f"c-{seq}",
        action=request.action.value,
        timestamp_ms=1_700_000_000_000 + seq,
        reason_code=reason,
    )


def built(request: ActionRequest, *, seq: int) -> ActionResult:
    return succeeded(
        request,
        seq=seq,
        evidence={
            "blueprint": WALL,
            "square": {"x": TARGET[0], "y": TARGET[1], "z": TARGET[2]},
            "object_refs": [object_ref("wall1")],
        },
    )


def step_of(move: object) -> ActionRequest:
    assert isinstance(move, MissionStep)
    return move.request


def mission(**kwargs: Any) -> BuildStructureMission:
    return BuildStructureMission(GOAL_ID, structure=WALL, square=TARGET, **kwargs)


# --------------------------------------------------------------------------
# the one command
# --------------------------------------------------------------------------


class TestThePlacement:
    def test_one_command_then_the_observed_structure_completes(self) -> None:
        """One placement, one bounded command, and the ending comes from the
        square — not from the build's own result."""
        build = mission()

        first = step_of(build.next_step(site(1)))
        assert first.action is ActionName.BUILDING_BUILD
        assert first.args == {"blueprint": WALL, "square": TARGET_REF}
        assert first.idempotency_key == f"build:{GOAL_ID}:s1"
        build.note_result(built(first, seq=2))

        done = build.next_step(site(3, objects=[a_wall_on_target()]))
        assert isinstance(done, MissionComplete)
        assert build.ended == ENDED_COMPLETE
        assert build.placed is True
        assert build.report["attempts"] == 1
        assert build.report["builds_succeeded"] == 1

    def test_the_command_carries_no_count_of_any_kind(self) -> None:
        # One command builds one structure once. A number in these arguments is
        # the first thing a loop in the mod would read, so its absence is the
        # property, not an omission.
        first = step_of(mission().next_step(site(1)))
        assert set(first.args) == {"blueprint", "square"}
        assert "count" not in first.args

    def test_a_square_that_turns_blocked_with_no_new_object_also_completes(self) -> None:
        """The other half of the postcondition: a build that produced no
        separately described object still shows up as the observer's own
        assessment of the square changing."""
        build = mission()
        first = step_of(build.next_step(site(1)))
        build.note_result(built(first, seq=2))

        after = a_world(
            seq=3,
            items=[plank()],
            containers=[main_container()],
            objects=[
                a_square(HOME_X, HOME_Y, HOME_Z),
                a_square(
                    TARGET[0], TARGET[1], HOME_Z, semantics=[SEMANTIC_LOADED, SEMANTIC_BLOCKED]
                ),
            ],
        )
        done = build.next_step(after)

        assert isinstance(done, MissionComplete)
        assert build.placed is True

    def test_a_square_that_reads_exactly_as_before_is_not_a_wall(self) -> None:
        """A succeeded ack over an unchanged square proves nothing: the mission
        keeps looking rather than claiming the structure."""
        build = mission()
        first = step_of(build.next_step(site(1)))
        build.note_result(built(first, seq=2))

        assert build.next_step(site(3)) is None
        assert build.placed is False
        assert build.report["confirmation_looks"] == 1

    def test_something_that_was_already_standing_there_proves_nothing(self) -> None:
        """The postcondition is a *difference*. A crate that was on the square
        all along is not this mission's wall — and in fact the policy refuses
        the placement outright, which is the stronger statement."""
        crowded = site(1, objects=[a_world_object(object_ref("crate1"), x=TARGET[0], y=TARGET[1])])

        ended = mission().next_step(crowded)

        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.SQUARE_OCCUPIED

    def test_one_step_in_flight_at_a_time(self) -> None:
        build = mission()
        step_of(build.next_step(site(1)))
        assert build.next_step(site(2)) is None

    def test_the_phases_are_the_closed_tokens(self) -> None:
        assert BUILD_PHASES == ("start", "build", "confirm")


# --------------------------------------------------------------------------
# the policy decides, and the mission reports what it said
# --------------------------------------------------------------------------


class TestThePolicyDecides:
    def test_a_placement_that_would_seal_the_character_in_is_refused(self) -> None:
        """The wave's central promise: the last exit the observation can see is
        not a square this agent puts a wall on. Nothing is queued."""
        build = mission()

        ended = build.next_step(site(1, sealed=True))

        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.WOULD_TRAP_PLAYER
        assert "no way out" in ended.detail
        assert build.report["attempts"] == 0
        assert build.report["refusal"] == BuildingRefusal.WOULD_TRAP_PLAYER.value

    def test_the_report_carries_the_enclosure_answer_and_its_claim(self) -> None:
        """What the check proved and what it did not, in the same breath.

        The window is bounded, so a report that said "safe" without the claim
        would be overstating the one thing this wave is about.
        """
        build = mission()
        build.next_step(site(1, sealed=True))

        enclosure = build.report["enclosure"]
        assert isinstance(enclosure, dict)
        assert enclosure["passed"] is False
        assert "the squares this observation described" in str(enclosure["claim"])

    def test_a_window_the_mod_did_not_send_refuses_as_a_trap_not_as_a_shrug(self) -> None:
        """An unreadable map is where a trapping wall is most likely, not least."""
        ended = mission().next_step(site(1, no_nearby=True))

        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.WOULD_TRAP_PLAYER

    def test_an_occupied_square_refuses_and_nothing_is_cleared(self) -> None:
        crate = a_world_object(object_ref("crate1"), x=TARGET[0], y=TARGET[1])

        ended = mission().next_step(site(1, objects=[crate]))

        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.SQUARE_OCCUPIED
        assert "clearing one is not mine to do" in ended.detail

    def test_a_square_that_is_described_but_not_loaded_refuses(self) -> None:
        ended = mission().next_step(site(1, unloaded=True))

        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.TARGET_NOT_LOADED

    def test_missing_materials_refuse_typed_and_name_what_is_short(self) -> None:
        readout = structure_payload(
            materials=[{"full_type": PLANK, "count": 1}, {"full_type": NAILS, "count": 4}]
        )

        ended = mission().next_step(site(1, plank(structures=[readout])))

        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.RECIPE_MATERIALS_MISSING
        assert NAILS in ended.detail
        assert "0 of 4" in ended.detail

    def test_a_shortfall_never_becomes_an_errand(self) -> None:
        """The craft wave's boundary, one rung along: the mission does not go
        looting for the missing nails, and the refusal it made stands."""
        readout = structure_payload(materials=[{"full_type": NAILS, "count": 2}])
        short = site(1, plank(structures=[readout]))
        build = mission()

        first = build.next_step(short)

        assert isinstance(first, MissionRefused)
        assert build.report["attempts"] == 0
        # And it stays refused: a second look at a stocked world does not
        # promote the mission into fetching what it lacked.
        assert build.next_step(site(2)) is first

    def test_a_reserve_is_a_refusal_the_user_can_lift(self) -> None:
        """The loot policy's unbreakable rule, honoured here: "put a wall
        there" is not permission to spend what the player marked as kept."""
        kept = plank(favorite=True)

        ended = mission(policy=PolicyConfig()).next_step(site(1, kept))

        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.RESOURCE_RESERVED
        assert "you reserved" in ended.detail

    def test_a_structure_the_build_never_reported_learning_refuses(self) -> None:
        unlearned = site(1, plank(structures=[structure_payload(known=None)]))

        ended = mission().next_step(unlearned)

        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.RECIPE_UNKNOWN

    def test_a_structure_nothing_observed_participates_in_refuses_by_name(self) -> None:
        other = site(1, plank(structures=[structure_payload(name="MetalWall")]))

        ended = mission().next_step(other)

        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.RECIPE_UNKNOWN
        assert "not one I saw a way to build" in ended.detail

    def test_the_refusal_table_is_total_and_matches_the_adapter(self) -> None:
        """Two copies of one table, knowingly — and pinned equal here rather
        than left to a comment. The adapter's is private to it because it
        refuses at validation time; this one refuses before a command exists."""
        assert set(REFUSAL_REASONS) == set(BuildingRefusal)
        assert REFUSAL_REASONS == building_adapter._REFUSAL_REASONS


# --------------------------------------------------------------------------
# nothing runs twice, and nothing moves along to the next square
# --------------------------------------------------------------------------


class TestTheOneAttempt:
    def test_the_attempt_ceiling_is_one_and_cannot_be_raised(self) -> None:
        assert MAX_BUILD_ATTEMPTS == 1
        for bad in (0, MAX_BUILD_ATTEMPTS + 1):
            with pytest.raises(ValueError, match="max_attempts"):
                BuildMissionLimits(max_attempts=bad)

    @pytest.mark.parametrize("field", ["max_confirmation_looks", "max_completion_probes"])
    def test_the_other_bounds_must_be_positive(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            BuildMissionLimits(**{field: 0})

    def test_a_failed_build_is_never_sent_again(self) -> None:
        """A build that failed may or may not have put something on the square,
        so a second command would be a second irreversible attempt made on this
        side's initiative. The mission looks, then reports."""
        build = mission(limits=BuildMissionLimits(max_confirmation_looks=1))
        first = step_of(build.next_step(site(1)))
        build.note_result(failed(first, seq=2, reason=ReasonCode.ACTION_TIMEOUT))

        ended = build.next_step(site(3))

        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.NO_PROGRESS
        assert build.ended == ENDED_UNCONFIRMED
        assert "nothing was observed standing" in ended.detail
        assert build.report["attempts"] == 1
        assert build.report["builds_succeeded"] == 0

    def test_the_confirmation_looks_are_bounded_and_then_honest(self) -> None:
        """A succeeded ack is a statement about the queue. After the bounded
        looks the mission says the structure was never observed — it never
        promotes the ack into a wall."""
        build = mission()
        first = step_of(build.next_step(site(1)))
        build.note_result(built(first, seq=2))

        assert build.next_step(site(3)) is None
        assert build.next_step(site(4)) is None
        ended = build.next_step(site(5))

        assert isinstance(ended, MissionRefused)
        assert build.ended == ENDED_UNCONFIRMED
        assert build.placed is False
        assert "never observed standing" in ended.detail

    def test_a_refused_placement_never_names_another_square(self) -> None:
        """The square is the user's choice. Whatever the refusal, the mission's
        square is the square it was given and no request ever names another."""
        build = mission()

        ended = build.next_step(site(1, sealed=True))

        assert isinstance(ended, MissionRefused)
        assert build.square == TARGET
        assert build.report["square"] == {"x": TARGET[0], "y": TARGET[1], "z": TARGET[2]}
        # Every later call replays the same refusal rather than trying again.
        assert build.next_step(site(2)) is ended


# --------------------------------------------------------------------------
# the edges
# --------------------------------------------------------------------------


class TestTheEdges:
    def test_a_structure_that_arrives_without_a_succeeded_command_takes_the_probe(self) -> None:
        """Real, not formal: the build failed as far as this side could tell,
        and the wall is standing anyway. There is no result of ours to succeed
        the goal with, so the bounded probe earns one."""
        build = mission()
        first = step_of(build.next_step(site(1)))
        build.note_result(failed(first, seq=2, reason=ReasonCode.ACTION_TIMEOUT))

        move = build.next_step(site(3, objects=[a_wall_on_target()]))

        assert isinstance(move, MissionProbe)
        assert move.request.action is ActionName.MOVEMENT_MOVE_TO
        assert move.request.idempotency_key == f"build:{GOAL_ID}:done1"
        assert build.ended == ENDED_COMPLETE
        assert build.any_success is False

    def test_a_probe_that_is_never_observed_ends_the_mission_honestly(self) -> None:
        build = mission(limits=BuildMissionLimits(max_completion_probes=1))
        first = step_of(build.next_step(site(1)))
        build.note_result(failed(first, seq=2, reason=ReasonCode.ACTION_TIMEOUT))
        standing = site(3, objects=[a_wall_on_target()])
        assert isinstance(build.next_step(standing), MissionProbe)

        ended = build.next_step(site(4, objects=[a_wall_on_target()]))

        assert isinstance(ended, MissionRefused)
        assert ended.reason_code is ReasonCode.NO_PROGRESS
        assert build.ended == ENDED_NO_PROGRESS

    def test_a_sealed_ending_replays(self) -> None:
        build = mission()
        first = build.next_step(site(1, sealed=True))
        second = build.next_step(site(2))
        assert first is second

    def test_mark_abandoned_seals_only_a_mission_in_flight(self) -> None:
        running = mission()
        step_of(running.next_step(site(1)))
        running.mark_abandoned()
        assert running.ended == "cancelled"

        finished = mission()
        finished.next_step(site(1, sealed=True))
        finished.mark_abandoned()
        assert finished.ended == ENDED_REFUSED

    def test_results_for_foreign_actions_are_ignored(self) -> None:
        build = mission()
        step = step_of(build.next_step(site(1)))
        foreign = succeeded(
            ActionRequest(
                action=ActionName.MOVEMENT_MOVE_TO,
                session_id=step.session_id,
                idempotency_key="someone-else",
                args={},
            ),
            seq=2,
            evidence={"position": {"x": 1}},
        )
        build.note_result(foreign)
        # Still waiting on its own step: the foreign result changed nothing.
        assert build.next_step(site(3)) is None

    def test_a_mission_must_name_a_structure_a_square_and_a_goal(self) -> None:
        with pytest.raises(ValueError, match="structure"):
            BuildStructureMission(GOAL_ID, structure="", square=TARGET)
        with pytest.raises(ValueError, match="goal_id"):
            BuildStructureMission("", structure=WALL, square=TARGET)
        with pytest.raises(ValueError, match="one square"):
            BuildStructureMission(GOAL_ID, structure=WALL, square=(1, 2))  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# what a goal record will actually accept
# --------------------------------------------------------------------------


class TestTheDetailFitsTheGoalChannel:
    """A refusal the queue cannot record is a goal that never ends.

    ``GoalQueue.fail`` builds a record whose ``detail`` must be one printable
    line of at most :data:`MAX_DETAIL_CHARS` characters, and the material names
    in a build refusal come from the game.
    """

    def hostile_world(self) -> Observation:
        """A readout spelled as unpleasantly as a mod can spell one."""
        nasty = "Mod.\n\tA" + "Ω" * 40 + "_" * 200
        readout = structure_payload(
            materials=[
                {"full_type": nasty, "count": 10**9},
                {"full_type": "Ω" * 30, "count": 7},
                {"full_type": "Mod.AlsoMissing", "count": 2},
            ]
        )
        return site(1, plank(structures=[readout]))

    def test_the_refusal_detail_is_one_bounded_printable_line(self) -> None:
        ended = mission().next_step(self.hostile_world())

        assert isinstance(ended, MissionRefused)
        assert len(ended.detail) <= MAX_DETAIL_CHARS
        assert "\n" not in ended.detail and "\t" not in ended.detail
        # The goal channel's own validator, run on the real string: this is the
        # constructor GoalQueue.fail's transition goes through.
        GoalTransition(
            goal_id=GOAL_ID,
            kind=GoalKind.BUILD_STRUCTURE,
            previous=GoalState.ACTIVE,
            state=GoalState.FAILED,
            reason_code=ended.reason_code,
            at_ms=1,
            detail=ended.detail,
        )

    def test_the_summary_line_is_constants_and_counts_only(self) -> None:
        build = mission()
        build.next_step(site(1, sealed=True))
        line = build.summary_line()
        assert line == (
            f"build refused: structure at ({TARGET[0]}, {TARGET[1]}) "
            "placed=no attempts=0 refusal=would_trap_player"
        )
        assert "\n" not in line and DEFAULT_SESSION not in line

    def test_the_report_redacts_game_authored_names(self) -> None:
        readout = structure_payload(materials=[{"full_type": "Mod.ΩNails", "count": 2}])
        build = mission()

        build.next_step(site(1, plank(structures=[readout])))

        report = build.report
        assert report["structure"] == WALL
        shortfalls = report["shortfalls"]
        assert isinstance(shortfalls, list) and shortfalls
        assert "\n" not in str(shortfalls[0]["full_type"])
