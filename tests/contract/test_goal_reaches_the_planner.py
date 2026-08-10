"""The seam: a goal submitted to the channel is the goal the planner serves.

Two subsystems, each already green on its own. ``pz_agent_core.goals`` proves
that a submission is admitted, bounded, exclusive and eventually terminal;
``pz_agent_core.planner`` proves that a :class:`Goal` handed to
:class:`~pz_agent_core.planner.provider.NullProvider` becomes a plan the critic
will approve. Neither suite ever looks at the join. Between them sits
:func:`~pz_agent_core.goals.model.to_planner_goal`, one function, four lines,
and every field it forgets to copy is a field that silently defaults on the far
side — the planner would still answer, the executor would still run, and the
character would eat instead of read, or read the wrong book. That is not a
crash; it is the agent doing something the user did not ask for, which is the
one failure this project cannot ship.

So these tests own the join and assert on the far end of it. Nothing between
the queue and the planner is faked: a real :class:`GoalQueue` admits the goal, a
real ``to_planner_goal`` widens it, a real ``NullProvider`` and the real
selection policies build the plan, a real :class:`PlanCritic` approves it and a
real :class:`PlanExecutor` ships it. The only doubles are past the far end —
the mod, the clock and the observation stream, because there is no game here.

Every assertion is written so that losing a field *fails* it:

* **The kind.** One pantry holding a tin and a bottle, two goals over it. A kind
  that defaulted or was substituted on the way answers hunger with the bottle,
  or answers "learn a recipe" with the novel that happens to be in the bag.
* **The parameters.** A library of three skill books that differ only in the
  skill they teach. ``train_skill(metalworking)`` must reach the metalworking
  book by name and by reference; if the skill were dropped the planner refuses
  outright (a ``train_skill`` goal with no skill cannot even be constructed),
  and if it were substituted the plan names a different book.
* **The identity.** ``Plan.goal_id`` is the id *the queue minted*, so the report
  a user reads is attributable to the goal they asked for and not to some other
  one in flight.

The last class is the other half of the promise: a kind the planner cannot serve
is refused at *submission*. The channel's defence is that the door from a string
to a :class:`GoalKind` is :func:`~pz_agent_core.goals.model.parse_kind` and it
opens onto an enum member or onto nothing, so an unservable kind never becomes a
:class:`GoalRequest` at all — it is not admitted and then quietly reinterpreted
downstream into a plan for something else.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Final

import pytest

from pz_agent_cli.navigation_planner import NavigatingPlanner
from pz_agent_cli.runtime import ActionChannel
from pz_agent_core.actions import ActionEngine, AdapterRegistry, register_builtins
from pz_agent_core.actions.engine import ActionRequest as EngineActionRequest
from pz_agent_core.goals import (
    GoalKind,
    GoalParams,
    GoalQueue,
    GoalRecord,
    GoalRequest,
    TrainableSkill,
    parse_kind,
    parse_skill,
    to_planner_goal,
)
from pz_agent_core.planner import Goal as PlannerGoal
from pz_agent_core.planner import GoalKind as PlannerGoalKind
from pz_agent_core.planner import NullProvider, PlanProposal, PlanRequest
from pz_agent_core.planner.critic import PlanCritic
from pz_agent_core.planner.executor import PlanExecutor, PlanOutcome
from pz_agent_core.protocol import (
    ActionName,
    ActionStatus,
    InventoryView,
    NearbyObject,
    NearbyView,
    Observation,
    PlayerState,
    Position,
    ReasonCode,
    RiskClass,
)
from tests.fixtures import DEFAULT_SESSION, make_player
from tests.fixtures.action_doubles import (
    AckPlan,
    FakeClock,
    FakeCommandSink,
    FakeObservationSource,
    StubAdapter,
)
from tests.fixtures.planner_worlds import (
    ALL_CAPABILITIES,
    GOAL_ID,
    item_ref,
    planner_observation,
)
from tests.fixtures.policy_items import drink_item, food_item, inventory, literature_item

pytestmark = pytest.mark.contract

#: The kinds this channel carries, written out by hand rather than derived from
#: either enum. Comparing an enum against itself would pass however far the two
#: vocabularies had drifted; this list is what makes the comparison mean
#: something, and adding a kind is meant to fail it until a human looks.
CHANNEL_KINDS: Final[frozenset[str]] = frozenset(
    {
        "satisfy_hunger",
        "satisfy_thirst",
        "read_for_boredom",
        "train_skill",
        "learn_recipe",
        "navigate_to",
        "loot_area",
        "return_home",
        "explore_area",
    }
)

#: The kinds no plan provider serves: the deterministic route executor walks
#: ``navigate_to`` and ``return_home`` (``pz_agent_core.navigation`` — the
#: homeward kind is the same walk with its target read from the save's
#: memory), the deterministic loot mission drives ``loot_area``
#: (``pz_agent_cli.loot_mission``) and the deterministic explore mission
#: drives ``explore_area`` (``pz_agent_cli.explore_mission``), all behind
#: the CLI's NavigatingPlanner. Written out by hand for the same reason
#: CHANNEL_KINDS is.
EXECUTOR_KINDS: Final[frozenset[str]] = frozenset(
    {"navigate_to", "loot_area", "return_home", "explore_area"}
)

#: The empty parameter set, as a singleton because :class:`GoalParams` is frozen
#: and a default argument that constructs one is a call at import time.
NO_PARAMS: Final = GoalParams()

#: A goal a user might plausibly speak and this build cannot serve. It is not a
#: member of either vocabulary, which is the whole point of naming it here.
UNSERVABLE_KIND: Final = "wander_outside"

#: Skill books that differ in exactly one field: the skill they teach. Anything
#: else about them is identical, so a plan that picks the wrong one picked it
#: because of the goal's parameter and not because of a score.
LIBRARY_BOOKS: Final[tuple[tuple[str, str, str], ...]] = (
    ("metal", "Metalworking For Beginners", "Metalworking"),
    ("carp", "Carpentry Volume One", "Carpentry"),
    ("tail", "Tailoring Volume One", "Tailoring"),
)


# --------------------------------------------------------------------------
# the seam, assembled
# --------------------------------------------------------------------------


def channel() -> GoalQueue:
    """A real goal queue on a clock that only moves when a test moves it."""
    return GoalQueue(clock=FakeClock())


def activated(
    queue: GoalQueue,
    kind: GoalKind,
    *,
    params: GoalParams = NO_PARAMS,
    key: str = "seam-key",
) -> GoalRecord:
    """Submit *kind* through the real admission path and start it.

    Both answers are checked rather than assumed. A refusal here would leave
    every planner assertion below asserting about a goal that never existed,
    which is exactly the shape of vacuous test this file is meant to be the
    opposite of.
    """
    admission = queue.submit(GoalRequest(kind=kind, idempotency_key=key, params=params))
    assert admission.refusal is None
    assert admission.goal is not None
    started = queue.activate_next()
    assert started.refusal is None
    assert started.goal is not None
    return started.goal


def proposed_for(record: GoalRecord, observation: Observation) -> PlanProposal:
    """Everything between the queue and a plan, with nothing faked in it."""
    return NullProvider().propose(
        PlanRequest(
            goal=to_planner_goal(record),
            observation=observation,
            capabilities=ALL_CAPABILITIES,
        )
    )


def plan_step_refs(proposal: PlanProposal) -> tuple[str, ...]:
    """Every item reference the proposed plan would touch."""
    assert proposal.plan is not None
    return tuple(ref.value for step in proposal.plan.steps for ref in step.args.refs())


# --------------------------------------------------------------------------
# worlds
# --------------------------------------------------------------------------


def pantry() -> InventoryView:
    """One safe tin and one full bottle, both in reach.

    Holding both matters: a hunger goal that arrived as a thirst goal would
    still produce a plan here, and only the action in it gives that away.
    """
    return inventory(food_item("beans", display_name="Tinned Beans"), drink_item("bottle"))


def library() -> InventoryView:
    """Three skill books and nothing else worth reading."""
    return inventory(
        *(
            literature_item(
                runtime_id,
                display_name=display_name,
                kind="skill_book",
                skill=skill,
                pages_total=120,
            )
            for runtime_id, display_name, skill in LIBRARY_BOOKS
        )
    )


def needy() -> PlayerState:
    """Hungry and thirsty at once, so neither goal is refused as already met."""
    return make_player(stats={"hunger": 0.6, "thirst": 0.6})


def world(items: InventoryView, player: PlayerState | None = None) -> Observation:
    return planner_observation(
        inventory=items, player=player if player is not None else make_player()
    )


# --------------------------------------------------------------------------
# the kind
# --------------------------------------------------------------------------


class TestTheKindSurvivesTheTrip:
    """Same world, different goals, and the plans have to differ."""

    def test_a_hunger_goal_becomes_a_plan_to_eat_the_tin(self) -> None:
        record = activated(channel(), GoalKind.SATISFY_HUNGER)

        proposal = proposed_for(record, world(pantry(), needy()))

        assert proposal.plan is not None
        assert [step.action.value for step in proposal.plan.steps] == ["consume.eat"]
        assert plan_step_refs(proposal) == (item_ref("beans"),)

    def test_a_thirst_goal_over_that_same_pantry_becomes_a_plan_to_drink(self) -> None:
        record = activated(channel(), GoalKind.SATISFY_THIRST)

        proposal = proposed_for(record, world(pantry(), needy()))

        assert proposal.plan is not None
        assert [step.action.value for step in proposal.plan.steps] == ["consume.drink"]
        assert plan_step_refs(proposal) == (item_ref("bottle"),)

    def test_learn_recipe_is_refused_rather_than_served_as_a_boredom_read(self) -> None:
        """A novel answers boredom and answers no recipe at all.

        Both goals are ``literature.read`` at the action level, so a kind lost
        between the channel and the planner produces a plan that looks perfectly
        well formed. The world is what separates them: one paperback, which the
        boredom goal is happy with and the recipe goal must refuse.
        """
        shelf = world(inventory(literature_item("paper", display_name="Paperback")))

        boredom = proposed_for(activated(channel(), GoalKind.READ_FOR_BOREDOM), shelf)
        recipe = proposed_for(activated(channel(), GoalKind.LEARN_RECIPE), shelf)

        assert boredom.plan is not None
        assert plan_step_refs(boredom) == (item_ref("paper"),)
        assert recipe.refused
        assert recipe.reason_code is ReasonCode.NO_SUITABLE_LITERATURE


# --------------------------------------------------------------------------
# the parameters
# --------------------------------------------------------------------------


class TestTheParametersSurviveTheTrip:
    @pytest.mark.parametrize(
        ("skill", "runtime_id", "title"),
        [
            (TrainableSkill.METALWORKING, "metal", "Metalworking For Beginners"),
            (TrainableSkill.CARPENTRY, "carp", "Carpentry Volume One"),
            (TrainableSkill.TAILORING, "tail", "Tailoring Volume One"),
        ],
    )
    def test_the_plan_reads_the_book_the_named_skill_asked_for(
        self, skill: TrainableSkill, runtime_id: str, title: str
    ) -> None:
        """Three goals, one library, three different books.

        A skill that was dropped, defaulted or pinned to a constant answers all
        three parametrisations with the same book, and at most one of them can
        then be right.
        """
        record = activated(channel(), GoalKind.TRAIN_SKILL, params=GoalParams(skill=skill))

        proposal = proposed_for(record, world(library()))

        assert proposal.plan is not None
        assert plan_step_refs(proposal) == (item_ref(runtime_id),)
        assert title in proposal.plan.summary
        other_titles = [t for _, t, _ in LIBRARY_BOOKS if t != title]
        assert all(other not in proposal.plan.summary for other in other_titles)

    def test_a_skill_no_book_teaches_is_refused_not_answered_with_another_book(self) -> None:
        """The library is full and the plan is still a refusal.

        This is the substitution the seam has to make impossible: two readable,
        healthy, in-reach skill books sit in the bag, and the honest answer to
        "train metalworking" is still that there is nothing here that teaches
        it.
        """
        shelf = inventory(
            literature_item(
                "carp",
                display_name="Carpentry Volume One",
                kind="skill_book",
                skill="Carpentry",
                pages_total=120,
            ),
            literature_item(
                "tail",
                display_name="Tailoring Volume One",
                kind="skill_book",
                skill="Tailoring",
                pages_total=120,
            ),
        )
        record = activated(
            channel(),
            GoalKind.TRAIN_SKILL,
            params=GoalParams(skill=TrainableSkill.METALWORKING),
        )

        proposal = proposed_for(record, world(shelf))

        assert proposal.refused
        assert proposal.reason_code is ReasonCode.NO_SUITABLE_LITERATURE
        assert any("not metalworking" in reason for reason in proposal.reasons)

    def test_the_plan_carries_the_goal_id_the_queue_minted(self) -> None:
        """Attribution: the plan a user is shown belongs to the goal they asked for."""
        first = activated(channel(), GoalKind.SATISFY_HUNGER, key="first-key")
        second = activated(channel(), GoalKind.SATISFY_HUNGER, key="second-key")
        hungry = world(pantry(), needy())

        one = proposed_for(first, hungry)
        two = proposed_for(second, hungry)

        assert one.plan is not None
        assert two.plan is not None
        # Two identical goals over an identical world: the plans are the same
        # sentence and must still be attributable to different goals.
        assert first.goal_id != second.goal_id
        assert one.plan.goal_id == first.goal_id
        assert two.plan.goal_id == second.goal_id


# --------------------------------------------------------------------------
# what the core actually runs
# --------------------------------------------------------------------------


class TestTheCoreRunsThatPlan:
    """The far end: the command that leaves for the mod.

    A plan that names the right book and an executor that ships something else
    would still pass every assertion above. This is the only place in the tree
    where the goal's parameter and the bytes headed for the game are compared.
    """

    def test_the_command_the_mod_receives_names_the_goals_own_book(self) -> None:
        record = activated(
            channel(),
            GoalKind.TRAIN_SKILL,
            params=GoalParams(skill=TrainableSkill.METALWORKING),
        )
        observation = world(library())
        clock = FakeClock()
        observations = FakeObservationSource(clock)
        observations.repeat(observation)
        sink = FakeCommandSink(clock, acks=(AckPlan(status=ActionStatus.ACCEPTED),))
        registry = register_builtins(AdapterRegistry())
        registry.register(
            StubAdapter(name=ActionName.LITERATURE_READ, risk=RiskClass.P2, verify_after=1)
        )
        executor = PlanExecutor(
            engine=ActionEngine(
                registry=registry,
                sink=sink,
                observations=observations,
                clock=clock,
                capability_check=lambda name: True,
            ),
            observations=observations,
            provider=NullProvider(),
            critic=PlanCritic(
                session_id=DEFAULT_SESSION, registry=registry, capabilities=ALL_CAPABILITIES
            ),
            session_id=DEFAULT_SESSION,
            clock=clock,
        )

        report = executor.run(
            PlanRequest(
                goal=to_planner_goal(record),
                observation=observation,
                capabilities=ALL_CAPABILITIES,
            )
        )

        assert report.outcome is PlanOutcome.COMPLETED
        assert report.goal_id == record.goal_id
        assert [command.action for command in sink.sent] == [ActionName.LITERATURE_READ]
        assert sink.last.args["item_ref"] == item_ref("metal")


class TestEveryAdmissibleKindIsServable:
    def test_every_kind_the_channel_admits_reaches_a_plan(self) -> None:
        """Nothing gets in that the deterministic path cannot answer.

        The alternative is a kind that submits cleanly, activates cleanly and
        then meets a planner with no branch for it — which the current
        implementation would serve as a read, silently, because that is what its
        final ``return`` does.
        """
        stocked = inventory(
            food_item("beans", display_name="Tinned Beans"),
            drink_item("bottle"),
            literature_item(
                "mag",
                display_name="Cookery Monthly",
                kind="magazine",
                unread_recipes=3,
                pages_total=40,
            ),
            literature_item("paper", display_name="Paperback"),
            literature_item(
                "carp",
                display_name="Carpentry Volume One",
                kind="skill_book",
                skill="Carpentry",
                pages_total=120,
            ),
        )
        observation = world(stocked, needy())

        served: dict[str, tuple[str, ...]] = {}
        for index, kind in enumerate(GoalKind):
            if kind.value in EXECUTOR_KINDS:
                # Served below by the route executor, and *refused* by the
                # provider — TestNavigateToIsServedByTheExecutor owns both
                # halves of that claim.
                continue
            params = (
                GoalParams(skill=TrainableSkill.CARPENTRY)
                if kind is GoalKind.TRAIN_SKILL
                else GoalParams()
            )
            record = activated(channel(), kind, params=params, key=f"kind-{index}")
            proposal = proposed_for(record, observation)
            assert not proposal.refused, f"{kind.value}: {proposal.detail}"
            served[kind.value] = plan_step_refs(proposal)

        assert set(served) == CHANNEL_KINDS - EXECUTOR_KINDS
        # The two consumables and the skill book are decided by the goal, not by
        # a shared default: three of the five answers must be distinct items.
        assert served["satisfy_hunger"] == (item_ref("beans"),)
        assert served["satisfy_thirst"] == (item_ref("bottle"),)
        assert served["train_skill"] == (item_ref("carp"),)
        assert served["learn_recipe"] == (item_ref("mag"),)


# --------------------------------------------------------------------------
# refused at submission, not reinterpreted later
# --------------------------------------------------------------------------


class SpyGoalPlanner:
    """A goal-capable planner that must never be asked; every ask is recorded."""

    def __init__(self) -> None:
        self.propose_calls = 0
        self.goal_calls: list[PlannerGoal] = []

    def propose(self, observation: Observation) -> EngineActionRequest | None:
        self.propose_calls += 1
        return None

    def propose_for_goal(
        self, goal: PlannerGoal, observation: Observation
    ) -> EngineActionRequest | None:
        self.goal_calls.append(goal)
        return None


@dataclass
class ExecutorHost:
    """The three loop attributes the navigating planner binds to, no loop needed."""

    goals: GoalQueue | None
    goal_lock: threading.Lock = field(default_factory=threading.Lock)
    actions: ActionChannel | None = None


class TestNavigateToIsServedByTheExecutor:
    """The sixth kind's half of the seam: the executor answers, no provider does.

    The same joins the rest of this file proves for the provider-served kinds,
    proven for ``navigate_to``: a real queue admits and activates it, the real
    ``to_planner_goal`` widens it, and the real :class:`NavigatingPlanner`
    turns it into the ``movement.move_to`` the engine already serves — while
    the wrapped planner, a spy, is never asked anything at all.
    """

    def navigate_params(self) -> GoalParams:
        # Five squares east of the player fixture's own square (1200, 3400, 0):
        # near enough for a single final leg, far enough that a move is needed.
        return GoalParams(target_x=1205, target_y=3400, target_z=0)

    def test_null_provider_refuses_navigate_to_by_name(self) -> None:
        """The deterministic provider's honest answer is a typed refusal.

        This is what makes a mis-assembled loop — a goal-capable planner with
        no navigating wrapper around it — safe: the goal is refused and ends
        on its budgets, never approximated with a plan to eat or read.
        """
        record = activated(channel(), GoalKind.NAVIGATE_TO, params=self.navigate_params())

        proposal = proposed_for(record, world(pantry(), needy()))

        assert proposal.refused
        assert proposal.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
        assert "route executor" in proposal.detail

    def test_the_wrapper_answers_with_a_move_and_never_asks_the_planner(self) -> None:
        queue = channel()
        spy = SpyGoalPlanner()
        wrapper = NavigatingPlanner(spy)
        wrapper.bind(ExecutorHost(goals=queue, actions=ActionChannel(clock=FakeClock())))
        record = activated(queue, GoalKind.NAVIGATE_TO, params=self.navigate_params())

        value = wrapper.propose_for_goal(to_planner_goal(record), world(pantry()))

        assert value is not None
        assert value.action is ActionName.MOVEMENT_MOVE_TO
        assert value.args["target"] == {"x": 1205, "y": 3400, "z": 0}
        assert value.args["allow_doors"] is True
        assert value.args["allow_stairs"] is True
        # Attribution, as everywhere in this file: the request is the goal's.
        assert value.idempotency_key.startswith(f"nav:{record.goal_id}")
        assert spy.goal_calls == [] and spy.propose_calls == 0, (
            "navigate_to must never reach a plan provider"
        )

    def test_every_channel_kind_is_owned_by_exactly_one_server(self) -> None:
        """Provider-served and executor-served partition the vocabulary."""
        assert EXECUTOR_KINDS <= CHANNEL_KINDS
        assert {kind.value for kind in GoalKind} == CHANNEL_KINDS


class TestLootAreaIsServedByTheMission:
    """The seventh kind's half of the seam: the loot mission answers, no provider.

    The same joins proven for ``navigate_to`` above: a real queue admits and
    activates it, the real ``to_planner_goal`` widens it, and the real
    :class:`NavigatingPlanner` drives the mission's first step — a
    ``container.open_nearby`` — into the loop's action channel, while the
    wrapped planner, a spy, is never asked anything at all.
    """

    def loot_world(self) -> Observation:
        crate = f"container:{DEFAULT_SESSION}:world:1202:3400:0:1:0"
        return planner_observation(
            inventory=inventory(),
            player=make_player(room="kitchen", building="apartments"),
            nearby=NearbyView(
                objects=[
                    NearbyObject(
                        ref=crate,
                        kind="container",
                        distance=2.0,
                        position=Position(x=1202.0, y=3400.0, z=0),
                        room="kitchen",
                        building="apartments",
                    )
                ]
            ),
        )

    def test_null_provider_refuses_loot_area_by_name(self) -> None:
        """The deterministic provider's honest answer is a typed refusal.

        The same property that keeps a mis-assembled loop safe for
        navigation: the goal is refused and ends on its budgets, never
        approximated with a plan to eat or read.
        """
        record = activated(channel(), GoalKind.LOOT_AREA)

        proposal = proposed_for(record, world(pantry(), needy()))

        assert proposal.refused
        assert proposal.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
        assert "loot mission" in proposal.detail

    def test_the_wrapper_drives_the_mission_and_never_asks_the_planner(self) -> None:
        queue = channel()
        spy = SpyGoalPlanner()
        wrapper = NavigatingPlanner(spy)
        actions = ActionChannel(clock=FakeClock())
        wrapper.bind(ExecutorHost(goals=queue, actions=actions))
        record = activated(queue, GoalKind.LOOT_AREA)

        value = wrapper.propose_for_goal(to_planner_goal(record), self.loot_world())

        # Every ordinary mission step travels the action channel, never the
        # goal seam — no step's success is the goal's postcondition.
        assert value is None
        assert actions.pending_count == 1
        taken = actions.take_next()
        assert taken is not None
        _, request = taken
        assert request.action is ActionName.CONTAINER_OPEN_NEARBY
        # Attribution, as everywhere in this file: the request is the goal's.
        assert request.idempotency_key.startswith(f"loot:{record.goal_id}")
        assert spy.goal_calls == [] and spy.propose_calls == 0, (
            "loot_area must never reach a plan provider"
        )


@dataclass(frozen=True)
class RememberedSquare:
    """A remembered grid square, as much of one as the wrapper reads."""

    x: int
    y: int
    z: int = 0


@dataclass(frozen=True)
class HomeMemory:
    """A memory answering ``home_point``, standing where the sidecar's would."""

    square: RememberedSquare

    def home_point(self) -> RememberedSquare:
        return self.square


class TestReturnHomeIsServedByTheExecutor:
    """The eighth kind's half of the seam: the same walk, the target from memory."""

    def test_null_provider_refuses_return_home_by_name(self) -> None:
        record = activated(channel(), GoalKind.RETURN_HOME)

        proposal = proposed_for(record, world(pantry(), needy()))

        assert proposal.refused
        assert proposal.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
        assert "route executor" in proposal.detail
        assert "home" in proposal.detail

    def test_the_wrapper_walks_home_and_never_asks_the_planner(self) -> None:
        queue = channel()
        spy = SpyGoalPlanner()
        wrapper = NavigatingPlanner(spy, loot_memory=HomeMemory(RememberedSquare(1205, 3400)))
        wrapper.bind(ExecutorHost(goals=queue, actions=ActionChannel(clock=FakeClock())))
        record = activated(queue, GoalKind.RETURN_HOME)

        value = wrapper.propose_for_goal(to_planner_goal(record), world(pantry()))

        # Five squares is one final leg, so the request goes out the goal
        # seam carrying the *remembered* square — nothing was submitted.
        assert value is not None
        assert value.action is ActionName.MOVEMENT_MOVE_TO
        assert value.args["target"] == {"x": 1205, "y": 3400, "z": 0}
        assert spy.goal_calls == [] and spy.propose_calls == 0, (
            "return_home must never reach a plan provider"
        )


class TestExploreAreaIsServedByTheMission:
    """The ninth kind's half of the seam: the explore mission answers."""

    def test_null_provider_refuses_explore_area_by_name(self) -> None:
        record = activated(channel(), GoalKind.EXPLORE_AREA)

        proposal = proposed_for(record, world(pantry(), needy()))

        assert proposal.refused
        assert proposal.reason_code is ReasonCode.CAPABILITY_UNAVAILABLE
        assert "explore mission" in proposal.detail

    def test_the_wrapper_drives_the_mission_and_never_asks_the_planner(self) -> None:
        queue = channel()
        spy = SpyGoalPlanner()
        wrapper = NavigatingPlanner(spy)
        wrapper.bind(ExecutorHost(goals=queue, actions=ActionChannel(clock=FakeClock())))
        record = activated(queue, GoalKind.EXPLORE_AREA)

        value = wrapper.propose_for_goal(to_planner_goal(record), world(pantry()))

        # The default radius sweep around a single known square resolves in
        # arrival reach, so the mission's completion probe goes out the goal
        # seam — a real engine request the loop settles the goal with.
        assert value is not None
        assert value.action is ActionName.MOVEMENT_MOVE_TO
        assert value.idempotency_key.startswith(f"explore:{record.goal_id}")
        assert spy.goal_calls == [] and spy.propose_calls == 0, (
            "explore_area must never reach a plan provider"
        )


class TestAKindThePlannerCannotServeIsRefusedAtSubmission:
    def test_an_unservable_kind_never_becomes_a_request(self) -> None:
        """The refusal is at the door, and the queue stays empty behind it."""
        assert parse_kind(UNSERVABLE_KIND) is None

        queue = channel()
        with pytest.raises(ValueError, match="GoalKind"):
            GoalRequest(kind=UNSERVABLE_KIND, idempotency_key="k")  # type: ignore[arg-type]

        assert queue.open_count == 0
        assert queue.active is None
        assert queue.pending == ()

    def test_an_unservable_kind_is_not_in_either_vocabulary(self) -> None:
        assert UNSERVABLE_KIND not in CHANNEL_KINDS
        assert {kind.value for kind in GoalKind} == CHANNEL_KINDS
        assert {kind.value for kind in PlannerGoalKind} == CHANNEL_KINDS

    def test_a_train_skill_goal_with_no_skill_is_refused_before_the_planner(self) -> None:
        """The parameter is required at the door because it is required at the end.

        The planner's own :class:`Goal` refuses the same thing, but by then the
        goal has been admitted, has a minted id and occupies the channel's one
        active slot; the refusal would arrive as an exception out of a code path
        the user cannot see. Asserting both halves is what shows the channel's
        check is the *earlier* of the two rather than a duplicate of it.
        """
        queue = channel()
        with pytest.raises(ValueError, match="requires"):
            GoalRequest(kind=GoalKind.TRAIN_SKILL, idempotency_key="k")

        assert queue.open_count == 0
        with pytest.raises(ValueError, match="must name the skill"):
            PlannerGoal(goal_id=GOAL_ID, kind=PlannerGoalKind.TRAIN_SKILL)

    def test_an_unparsed_skill_never_becomes_a_parameter(self) -> None:
        """The other door: a skill string the enum does not know opens onto nothing."""
        assert parse_skill("levitation") is None
        assert parse_skill("first aid") is TrainableSkill.FIRST_AID

        with pytest.raises(ValueError, match="parse_skill"):
            GoalParams(skill="carpentry")  # type: ignore[arg-type]
