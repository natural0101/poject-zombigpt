"""The bridge between the sidecar loop's one-action port and the planner package.

Two subsystems that were each finished and tested separately speak different
shapes. :class:`~pz_agent_cli.runtime.Planner` is one observation in, at most one
:class:`~pz_agent_core.actions.engine.ActionRequest` out; a
:class:`~pz_agent_core.planner.PlanProvider` is a
:class:`~pz_agent_core.planner.PlanRequest` in and a whole
:class:`~pz_agent_core.planner.Plan` out. :class:`AutonomyPlanner` is the piece
that was missing between them, and it is deliberately the only thing in this
module that decides anything:

1. :class:`~pz_agent_core.policy.autonomy.AutonomyGate` answers "is there
   something I should start unasked, and what need is it for". It owns the
   session rules, §7.7's ask-vs-act list, the reflex-danger floor, the anti-loop
   brake, the permission ladder, the home radius and the action budget. None of
   that is repeated here; a second copy of a bound is how two subsystems end up
   disagreeing about it.
2. The provider turns that need into a typed plan. ``provider = "none"`` — the
   deterministic path — reaches it through the same selection policies the gate
   used, which is what keeps "the model never picks the sandwich" true no matter
   which provider is configured.
3. :class:`~pz_agent_core.planner.PlanCritic` reviews the plan on
   ``SELF_DIRECTED`` initiative, because a provider's answer is a proposal and
   not a permission.
4. The plan's **first step** becomes one :class:`ActionRequest`.

Only the first step, and that is not a simplification. The loop already
serialises work and re-observes between actions, so the next step is composed
next tick against a fresh observation — a plan runner here would be a second
executor driving a world the loop is already driving, and the item reference a
plan was written against may name something else by the time step two is
reached. :class:`~pz_agent_core.planner.PlanExecutor` is the component for
driving a whole plan, and it is deliberately not wired into this path.

**What the gate is given, this module does not invent.** The item choice comes
back from the gate, and a plan naming a different item is refused rather than
run: the gate removed everything the user reserved (§7.9) before it chose, and a
provider that named something else would be reaching around that.

**The backup is the one thing this build cannot witness.** §7.7 puts an
unbacked save on the ask-the-user side, so
:meth:`~pz_agent_core.policy.autonomy.AutonomyGate.evaluate` takes the evidence
as a parameter with no default. Local backups are named by save *directory*;
the mod reports the save as a digest of a key this side never sees
(``PZAgent.ObserveModel.saveId``). Nothing bridges those two identifier spaces,
so :data:`no_attributable_backup` is what ``pz-agent start`` passes, autonomy
asks instead of acting, and :data:`BACKUP_NOT_ATTRIBUTABLE` says so through
``pz-agent status``. It is a parameter rather than a hard-coded ``None`` because
the missing half is an attribution, not the evidence type.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, TypeAlias

from pz_agent_core.actions.adapter import AdapterRegistry
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.capabilities import MAX_NOTES
from pz_agent_core.ipc.clocks import Clock, system_clock_ms
from pz_agent_core.planner import (
    MAX_PLAN_STEPS,
    PROVIDER_NONE,
    PROVIDER_OPENAI_COMPATIBLE,
    PROVIDER_TEAMON,
    Goal,
    GoalKind,
    NullProvider,
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
    PlanCritic,
    PlanProvider,
    PlanRef,
    PlanRequest,
    TeamONConfig,
    TeamONProvider,
    TransportError,
)
from pz_agent_core.policy.autonomy import (
    DEFAULT_AUTONOMY_CONFIG,
    NEED_BOREDOM,
    NEED_HUNGER,
    NEED_THIRST,
    NO_MEMORY,
    AutonomyConfig,
    AutonomyDecision,
    AutonomyGate,
    AutonomyMemory,
    BackupEvidence,
)
from pz_agent_core.policy.config import DEFAULT_POLICY_CONFIG, PolicyConfig
from pz_agent_core.policy.permissions import Initiative
from pz_agent_core.policy.selection import NO_CAPABILITIES, CapabilityLookup
from pz_agent_core.protocol import CapabilityState, JsonDict, Observation, RefKind

from .config import AgentConfig
from .runtime import CapabilityLedger
from .supervisor import _read_json, _write_json

__all__ = [
    "BACKUP_NOT_ATTRIBUTABLE",
    "PLANNER_FILE_NAME",
    "AutonomyPlanner",
    "BackupWitness",
    "LedgerCapabilities",
    "PlannerError",
    "PlannerRecord",
    "autonomy_config",
    "build_planner",
    "no_attributable_backup",
    "publish_planner_record",
    "read_planner_record",
    "resolve_provider",
]

#: Where the sidecar writes what it planned *with*, beside the capability record
#: and for the same reason: ``status`` must be able to say which planner a
#: running loop is on without re-deriving it and reaching a second opinion.
PLANNER_FILE_NAME: Final = "sidecar.planner.json"

#: Why ``pz-agent start`` can supply no backup evidence. Carried into the
#: planner record so the silence has a reason a user can read.
BACKUP_NOT_ATTRIBUTABLE: Final = (
    "autonomy will ask rather than act: a backup here is named by its save directory and "
    "the mod reports the save as a digest, so this build cannot show that any local backup "
    "covers the save being played. Everything else in the autonomous path is wired."
)

#: What the gate needs and the filesystem cannot yet answer: the backup covering
#: the save an observation names. A callable rather than an object because the
#: only input a witness has is the save it is asked about.
BackupWitness: TypeAlias = Callable[[str], "BackupEvidence | None"]


def no_attributable_backup(save_id: str) -> BackupEvidence | None:
    """The honest witness for this build: nothing can be attributed to *save_id*.

    Not a placeholder for a check that was skipped — the check cannot be made
    from local files at all, and :data:`BACKUP_NOT_ATTRIBUTABLE` is published so
    the refusal it produces is explained rather than mysterious.
    """
    return None


#: Which goal serves which need. The three are exactly the needs
#: :data:`~pz_agent_core.policy.autonomy.NEED_PLANS` has a plan for; bleeding and
#: fatigue reach the user through the gate's own ASK_USER path and never arrive
#: here, so an entry for them would be a goal nothing could ever ask for.
_GOALS: Final[Mapping[str, GoalKind]] = MappingProxyType(
    {
        NEED_HUNGER: GoalKind.SATISFY_HUNGER,
        NEED_THIRST: GoalKind.SATISFY_THIRST,
        NEED_BOREDOM: GoalKind.READ_FOR_BOREDOM,
    }
)


class PlannerError(ValueError):
    """A planner record could not be read or written as a whole document."""


# ---------------------------------------------------------------------------
# what status reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannerRecord:
    """Which planner one sidecar actually assembled, as ``status`` reads it.

    ``wired`` is written from the loop that was built, not from the intent of
    the code that built it — a sidecar whose planner argument went missing again
    would say ``NOT WIRED`` here instead of looking identical to a working one.

    ``configured`` and ``active`` are separate for the same reason ``resolved``
    exists on the capability record: "the deterministic path is what I asked
    for" and "the deterministic path is what I fell back to" produce the same
    behaviour and opposite facts, and only the second one is something to go and
    fix.
    """

    wired: bool
    configured: str
    active: str
    detail: str
    fallback_reason: str = ""
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise PlannerError("a planner record must say how it reached the planner it names")

    @property
    def fell_back(self) -> bool:
        """True when the configured provider is not the one that will answer."""
        return bool(self.fallback_reason)

    def to_dict(self) -> JsonDict:
        return {
            "wired": self.wired,
            "configured": self.configured,
            "active": self.active,
            "detail": self.detail,
            "fallback_reason": self.fallback_reason,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PlannerRecord:
        """Parse a record this process did not necessarily write.

        Raises:
            PlannerError: for anything malformed, rather than reading whichever
                field survived as the whole answer.
        """
        wired = payload.get("wired")
        if not isinstance(wired, bool):
            raise PlannerError("a planner record must say whether a planner was wired")
        return cls(
            wired=wired,
            configured=_text(payload.get("configured", ""), "configured"),
            active=_text(payload.get("active", ""), "active"),
            detail=_text(payload.get("detail", ""), "detail"),
            fallback_reason=_text(payload.get("fallback_reason", ""), "fallback_reason"),
            notes=_notes(payload.get("notes", [])),
        )


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise PlannerError(f"a planner record's {field_name} must be a string")
    return value


def _notes(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, list):
        raise PlannerError("a planner record's notes must be an array")
    return tuple(_text(item, "note") for item in value)[:MAX_NOTES]


def read_planner_record(path: Path) -> PlannerRecord | None:
    """The record at *path*, or None when no sidecar has written one there.

    None is a different statement from "no planner was wired", and ``status``
    reports it as one: nothing has assembled a loop in this state directory yet.
    """
    payload = _read_json(path)
    if payload is None:
        return None
    try:
        return PlannerRecord.from_dict(payload)
    except PlannerError:
        return None


def publish_planner_record(path: Path, record: PlannerRecord) -> PlannerRecord:
    """Write *record* where ``status`` reads it. Returns what was written.

    A record that cannot be written is noted rather than raised, the way the
    capability ledger treats the same failure: losing the status file is not a
    reason to refuse to drive the game.
    """
    try:
        _write_json(path, record.to_dict())
    except OSError as exc:
        note = f"the planner record could not be written ({exc.strerror or exc})"
        return replace(record, notes=(*record.notes, note)[-MAX_NOTES:])
    return record


# ---------------------------------------------------------------------------
# the bridge
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LedgerCapabilities:
    """The sidecar's capability ledger as a :class:`CapabilityLookup`.

    The ledger answers ``usable`` and the policies also ask ``state``, because
    §7.7 draws its line between *verified* and *available_unverified* rather than
    at usability. Both questions are forwarded at the moment they are asked, not
    captured at assembly: a capability a live run promotes to ``verified``
    mid-session has to become answerable to autonomy in that same session, and a
    snapshot taken at start-up would answer with the report as it was before
    anything ran.

    A ledger with no report answers ``UNSUPPORTED`` to everything, which is the
    fail-closed direction the whole capability system exists to keep.
    """

    ledger: CapabilityLedger

    def state(self, name: str, /) -> CapabilityState:
        report = self.ledger.report
        return CapabilityState.UNSUPPORTED if report is None else report.state(name)

    def usable(self, name: str, /) -> bool:
        return self.ledger.usable(name)


@dataclass
class AutonomyPlanner:
    """One tick's answer to "should I start something, and what exactly"."""

    registry: AdapterRegistry
    provider: PlanProvider = field(default_factory=NullProvider)
    capabilities: CapabilityLookup = NO_CAPABILITIES
    backup: BackupWitness = no_attributable_backup
    memory: AutonomyMemory = NO_MEMORY
    policy: PolicyConfig = DEFAULT_POLICY_CONFIG
    config: AutonomyConfig = DEFAULT_AUTONOMY_CONFIG
    clock: Clock = system_clock_ms
    #: Longest plan a provider may answer with. Clamped to the plan type's own
    #: ceiling, which is lower than the one ``config.toml`` accepts.
    max_steps: int = MAX_PLAN_STEPS
    _gate: AutonomyGate = field(init=False, repr=False)
    _decision: AutonomyDecision | None = field(default=None, init=False, repr=False)
    _detail: str = field(default="nothing has been proposed yet", init=False, repr=False)

    def __post_init__(self) -> None:
        if not 1 <= self.max_steps <= MAX_PLAN_STEPS:
            raise PlannerError(
                f"max_steps must be within 1..{MAX_PLAN_STEPS}, got {self.max_steps}"
            )
        self._gate = AutonomyGate(config=self.config, policy=self.policy)

    @property
    def gate(self) -> AutonomyGate:
        """The gate, whose arbiter and action budget are this object's only state."""
        return self._gate

    @property
    def last_decision(self) -> AutonomyDecision | None:
        """The gate's verdict on the most recent tick, or None before the first."""
        return self._decision

    @property
    def last_detail(self) -> str:
        """Why the last tick proposed what it did — including when that was nothing.

        Held in memory and not written anywhere: it changes every tick, and a
        file rewritten four times a second to say "nothing needs attention" would
        cost more than it tells anyone.
        """
        return self._detail

    def propose(self, observation: Observation) -> ActionRequest | None:
        """The next action to start unasked, or None.

        Nothing is remembered between calls except the gate's own bounded
        counters: every tick decides against the observation it was handed,
        because a reference chosen against an older one may name a different
        object by now.
        """
        decision = self._gate.evaluate(
            observation,
            now_ms=self.clock(),
            backup=self.backup(observation.game.save_id),
            capabilities=self.capabilities,
            memory=self.memory,
        )
        self._decision = decision
        self._detail = decision.detail
        if not decision.should_act:
            return None
        return self._compose(decision, observation)

    # -- from a need to one request ----------------------------------------

    def _compose(
        self, decision: AutonomyDecision, observation: Observation
    ) -> ActionRequest | None:
        """Turn an ACT decision into the first step of a reviewed plan."""
        need = decision.need
        if need is None:
            # `should_act` guarantees a need; restating it narrows the type
            # without asserting something the caller cannot see.
            self._hold("the gate approved a tick without naming the need it serves")
            return None
        goal_kind = _GOALS.get(need.key)
        if goal_kind is None:
            self._hold(f"{need.key} is a need I have no goal for; there is nothing to plan for it")
            return None
        goal = Goal(goal_id=str(uuid.uuid4()), kind=goal_kind)
        proposal = self.provider.propose(
            PlanRequest(
                goal=goal,
                observation=observation,
                capabilities=self.capabilities,
                policy=self.policy,
                max_steps=self.max_steps,
            )
        )
        plan = proposal.plan
        if plan is None:
            self._hold(f"{self.provider.name} proposed nothing: {proposal.detail}")
            return None
        verdict = PlanCritic(
            session_id=observation.session_id,
            registry=self.registry,
            capabilities=self.capabilities,
            initiative=Initiative.SELF_DIRECTED,
            home=self.memory.home_point(),
            home_radius=self.config.home_radius,
            max_steps=self.max_steps,
        ).review(plan, observation)
        if verdict.refused:
            self._hold(f"the critic refused the plan: {verdict.detail}")
            return None
        step = plan.steps[0]
        mismatch = self._item_mismatch(decision, step_refs=step.args.refs())
        if mismatch is not None:
            self._hold(mismatch)
            return None
        self._detail = f"{decision.detail} {plan.summary}"
        return ActionRequest(
            action=step.action,
            # The observation's session, not one captured at assembly: the loop
            # is built before it attaches, and the queue refuses a command from
            # any session but its own, so this is both the earliest and the only
            # honest source.
            session_id=observation.session_id,
            idempotency_key=f"autonomy:{goal.goal_id}:{step.step_id}",
            args=step.args.to_payload(),
        )

    def _item_mismatch(
        self, decision: AutonomyDecision, *, step_refs: tuple[PlanRef, ...]
    ) -> str | None:
        """Refuse a plan that spends an item the selection policy did not choose.

        The gate has already run the deterministic policy over an inventory with
        the user's reservations removed, so its choice is the only item §7.9
        permits. A provider that named another one — a model may, and a
        mis-ordered deterministic call would — must not have that difference
        quietly executed.
        """
        authorised = decision.item_ref
        if authorised is None:
            return None
        named = next((ref.value for ref in step_refs if ref.kind is RefKind.ITEM), None)
        if named == authorised:
            return None
        return (
            "the plan's first step names an item the selection policy did not choose, "
            "so it is not something I may spend unasked"
        )

    def _hold(self, detail: str) -> None:
        """Record why this tick proposes nothing, for the readout that asks."""
        self._detail = detail


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def autonomy_config(config: AgentConfig) -> AutonomyConfig:
    """The autonomy bounds the user's configuration asks for.

    Only the two settings ``config.toml`` actually carries are read. The rest of
    :class:`~pz_agent_core.policy.autonomy.AutonomyConfig` keeps its documented
    default rather than being restated here, where a copy could drift.
    """
    return AutonomyConfig(
        home_radius=int(config.get("safety", "max_autonomous_radius")),
        require_verified_backup=bool(config.get("session", "require_backup")),
    )


def resolve_provider(config: AgentConfig, *, env: Mapping[str, str]) -> tuple[PlanProvider, str]:
    """The configured provider, or the deterministic one and why it is not.

    Returns the provider and the fallback reason, which is empty when the
    configured provider is what came back. Nothing here contacts a network: a
    credential is checked because
    :meth:`~pz_agent_core.planner.OpenAICompatibleProvider.api_key` exists to be
    asked before a session starts, while reachability is not, because a start-up
    that blocks on an endpoint is a start-up that hangs when the endpoint does.
    An endpoint that turns out to be unreachable refuses its own tick with
    ``CAPABILITY_UNAVAILABLE`` and the agent starts nothing — it is never
    answered for by the deterministic path behind the user's back.
    """
    configured = str(config.get("planner", "provider"))
    if configured == PROVIDER_NONE:
        return NullProvider(), ""
    try:
        typed = config.planner_provider
        provider = _model_provider(configured, typed, env)
    except ValueError as exc:
        return NullProvider(), f"{configured} could not be built from its configuration: {exc}"
    if provider is None:
        return NullProvider(), (
            f"{configured} is not a provider this build can construct, so nothing would answer"
        )
    try:
        provider.api_key()
    except (TransportError, ValueError) as exc:
        # ``CredentialUnavailable`` is the ordinary case and names the variable;
        # the wider catch covers a key that cannot be sent as a header at all.
        return NullProvider(), str(exc)
    return provider, ""


def _model_provider(
    configured: str,
    typed: OpenAICompatibleConfig | TeamONConfig | None,
    env: Mapping[str, str],
) -> OpenAICompatibleProvider | TeamONProvider | None:
    """Construct the named provider over *typed*, or None when it is not one.

    Raises:
        ValueError: from the provider's own construction, which owns its bounds.
    """
    if configured == PROVIDER_OPENAI_COMPATIBLE and isinstance(typed, OpenAICompatibleConfig):
        return OpenAICompatibleProvider(typed, environ=env)
    if configured == PROVIDER_TEAMON and isinstance(typed, TeamONConfig):
        return TeamONProvider(typed, environ=env)
    return None


def build_planner(
    *,
    registry: AdapterRegistry,
    capabilities: CapabilityLedger,
    config: AgentConfig,
    env: Mapping[str, str],
    clock: Clock = system_clock_ms,
    backup: BackupWitness = no_attributable_backup,
    notes: tuple[str, ...] = (),
) -> tuple[AutonomyPlanner, PlannerRecord]:
    """Assemble the planner ``pz-agent start`` runs, and the record describing it.

    The record's ``wired`` field is left False here on purpose: only the caller
    that builds the loop can say whether the planner reached it, and that is the
    fact ``status`` has to report.
    """
    configured = str(config.get("planner", "provider"))
    provider, fallback = resolve_provider(config, env=env)
    planner = AutonomyPlanner(
        registry=registry,
        provider=provider,
        capabilities=LedgerCapabilities(capabilities),
        backup=backup,
        policy=DEFAULT_POLICY_CONFIG,
        config=autonomy_config(config),
        clock=clock,
        # The plan type's ceiling is below what ``planner.max_steps`` accepts,
        # and a request over it is refused outright, so the smaller wins.
        max_steps=min(int(config.get("planner", "max_steps")), MAX_PLAN_STEPS),
    )
    active = provider.name
    detail = (
        f"planner.provider is {configured!r} and {active} is answering"
        if not fallback
        else f"planner.provider is {configured!r}; {active} is answering instead"
    )
    record = PlannerRecord(
        wired=False,
        configured=configured,
        active=active,
        detail=detail,
        fallback_reason=fallback,
        notes=notes[:MAX_NOTES],
    )
    return planner, record
