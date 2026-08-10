"""Translating one MCP tool call into one core command, and back.

This is the whole adapter. It validates arguments against the published schema,
applies the three gates the boundary owns — capability, idempotency, arming —
hands the call to a port, and serialises whatever comes back. It decides nothing
else. Which sandwich is safe, whether a square can be walked to, what counts as
proof: all of that is core's, and a copy of any of it here would be a second
implementation that drifts.

Four rules are enforced here and nowhere else, because here is where a client
can be lied to:

* **Success is never claimed early.** A long-running tool answers with the
  action id and the status the core reported. ``succeeded`` reaches a client
  only through :class:`~.ports.ActionRecord`, which refuses to exist without the
  observed postcondition.
* **Withheld means withheld.** A tool whose capability is not
  :meth:`~pz_agent_core.capabilities.model.CapabilityReport.usable` is neither
  listed nor callable; calling it says which capability and why.
* **A replay is not a second action.** The idempotency check runs *before* the
  arming gate, so a retry after the session disarmed still answers with the
  original call rather than ``NOT_ARMED``.
* **Nothing the game wrote reaches a client unmarked.** Observations go through
  :mod:`pz_agent_core.observation.compact`; everything else goes through
  :mod:`.scrub`, which applies the same rule.

The three ``pz_goal_*`` tools follow the same shape one layer up: they translate
into :mod:`pz_agent_core.goals` and decide nothing about goals. Admission,
exclusivity, budgets and expiry are the channel's; what is decided here is only
that a goal cannot be *submitted* on a disarmed session — ``pz_goal_submit`` is
a write, so the gate below refuses it with the core's own ``NOT_ARMED`` — while
``pz_goal_status`` and ``pz_goal_cancel`` are never gated, because reading the
channel and stopping it are how a disarmed session is understood and driven.

The action tools map their arguments straight onto the adapter's argument names.
That is deliberate: the schema field is the adapter field, so an argument this
layer accepts is one the adapter understands, and there is no translation table
to fall out of date.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from itertools import islice
from typing import Any, Final

from pz_agent_core.actions import ActionRequest
from pz_agent_core.capabilities.model import (
    MAX_EVIDENCE_PER_CAPABILITY,
    Capability,
    CapabilityReport,
)
from pz_agent_core.goals import (
    DEFAULT_MAX_OPEN,
    GoalKind,
    GoalParams,
    GoalRecord,
    GoalRequest,
    TrainableSkill,
)
from pz_agent_core.observation.compact import (
    CONTENT_MARKER,
    CONTENT_RULE,
    UNTRUSTED_TEXT_KEY,
    compact_for_planner,
    save_scope,
)
from pz_agent_core.protocol import (
    ON_PERSON_CONTAINERS,
    ActionName,
    ActionResult,
    ActionStatus,
    ContainerKind,
    JsonDict,
    Observation,
    ReasonCode,
    SessionMode,
)
from pz_agent_core.version import PROTOCOL_VERSION

from .catalog import (
    DEFAULT_PLAN_REAL_SECONDS,
    MAX_PLAN_STEPS,
    RESOURCES,
    TOOLS,
    TOOLS_BY_NAME,
    ToolSpec,
    published_tools,
    withheld_tools,
)
from .envelope import (
    MAX_DIAGNOSTICS,
    IdFactory,
    ToolFailure,
    ToolOutcome,
    ToolSuccess,
    failure_from,
    is_retryable,
    new_request_id,
)
from .idempotency import CachedCall, IdempotencyCache
from .ports import (
    ActionRecord,
    CoreServices,
    GoalPort,
    PlanRecord,
    PlanRequest,
    evidence_payload,
)
from .scrub import as_token, is_reference, scrub_payload, scrub_text
from .validation import validate_arguments

__all__ = [
    "ACTION_WAIT_POLL_MS",
    "MAX_DOCTOR_CHECKS",
    "MAX_EVIDENCE_ENTRIES",
    "MAX_PENDING_GOALS_REPORTED",
    "MAX_PLAN_STEPS_REPORTED",
    "MAX_REFS_PER_RECORD",
    "UNKNOWN_ACTION_CAUSES",
    "ToolRouter",
]

#: Fields the mutating tools carry that are not adapter arguments.
_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset({"idempotency_key", "timeout_ms"})

#: Sections of the compacted view that every snapshot detail level includes.
_SNAPSHOT_HEADER: Final[tuple[str, ...]] = (
    "view",
    "content_marker",
    "content_rule",
    "seq",
    "timestamp_ms",
    "full",
    "capability_revision",
    "active_goal_id",
    "game",
    "player",
    "safety",
    "action",
    "capabilities",
    "limits",
)

#: Container kinds each inventory scope admits.
_SCOPES: Final[dict[str, frozenset[str]]] = {
    "on_person": frozenset(kind.value for kind in ON_PERSON_CONTAINERS),
    "player_main": frozenset({ContainerKind.PLAYER_MAIN.value}),
    "carried": frozenset({ContainerKind.CARRIED.value}),
    "worn": frozenset({ContainerKind.WORN.value}),
    "world": frozenset(k.value for k in ContainerKind if k not in ON_PERSON_CONTAINERS),
}

MAX_REFS_PER_RECORD: Final = 8

#: Taken from the capability model rather than restated: a second number here
#: would be a second opinion on how much evidence one capability may carry, and
#: the looser of the two would win silently.
MAX_EVIDENCE_ENTRIES: Final = MAX_EVIDENCE_PER_CAPABILITY

MAX_PLAN_STEPS_REPORTED: Final = 16

#: Backlog entries ``pz_goal_status`` will list. Taken from the channel's own cap
#: rather than restated: a queue holding at most that many open goals cannot have
#: more waiting, so a port that offers more is a port disagreeing with the queue,
#: and the answer says it was cut off instead of presenting a short list as whole.
MAX_PENDING_GOALS_REPORTED: Final = DEFAULT_MAX_OPEN

#: ``pz_debug_doctor`` takes no arguments, so it has no caller-supplied limit to
#: bound its answer with. The check list is short and fixed in the core, but a
#: port is foreign code and an unbounded read of one is a bug by house rule.
MAX_DOCTOR_CHECKS: Final = 64

_ZOMBIE_TYPE: Final = "zombie"

#: How often ``pz_action_await`` re-reads the action port, in milliseconds.
#: Fifty is well under the loop's tick — a settlement is seen within one poll of
#: happening — and well over a busy spin; each read is one bounded port call
#: with nothing held between reads.
ACTION_WAIT_POLL_MS: Final = 50

#: Why an action id can be unknown here while the work it named was real. A
#: closed vocabulary rather than prose, so an agent loop can branch on the
#: answer instead of parsing a sentence: the record store is a bounded ring
#: that evicts terminal records, and a restarted sidecar holds nothing the
#: previous process minted.
UNKNOWN_ACTION_CAUSES: Final[tuple[str, ...]] = ("evicted", "sidecar_restarted")

Handler = Callable[[ToolSpec, JsonDict], ToolOutcome]


def _plan_envelope_status(status: ActionStatus) -> str:
    """The envelope status for a plan, which never borrows ``succeeded``.

    ``ToolSuccess`` reserves that word for a result carrying the observed
    postcondition under ``data.evidence`` (``docs/MCP_TOOLS.md``), and a plan
    record has none to carry: its steps' evidence was observed by the engine and
    stops at :class:`~.ports.PlanStepRecord`. ``PlanExecutor.run`` is
    synchronous, so a plan that worked comes back terminal on the *first* call —
    borrowing the word there refused the envelope, reported a plan that ran as
    ``INTERNAL_ERROR``, and skipped the idempotency record, so the client's retry
    ran the plan a second time. ``data.status`` and ``data.terminal`` say what
    the plan finished as, which is what ``pz_plan_status`` already relies on.
    """
    return "ok" if status is ActionStatus.SUCCEEDED else status.value


def _omitted_warning(dropped: int, noun: str) -> tuple[str, ...]:
    """Say out loud that the answer is shorter than what the port offered.

    A record whose own identifiers are not identifiers cannot be reported, but
    dropping it silently leaves a client reading a short list as a complete one.
    """
    if dropped <= 0:
        return ()
    return (
        f"{dropped} {noun}(s) omitted: their identifying fields are not "
        f"identifiers, which is a producer bug rather than a filter",
    )


class ToolRouter:
    """The translation layer, testable without the MCP SDK and without a game."""

    def __init__(
        self,
        services: CoreServices,
        *,
        cache: IdempotencyCache | None = None,
        request_ids: IdFactory = new_request_id,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._services = services
        self._cache = cache if cache is not None else IdempotencyCache()
        self._request_ids = request_ids
        #: Injected for the same reason the CLI's control waiter injects them: a
        #: test drives the bounded wait to its deadline instead of sleeping
        #: through it, and a frozen clock proves the poll count is the second
        #: bound rather than hoping it is.
        self._sleep = sleep
        self._monotonic = monotonic
        #: The session the cached calls belong to; see :meth:`_scope_cache_to_session`.
        self._session_id: str | None = None
        self._handlers: dict[str, Handler] = {
            "pz_session_status": self._session_status,
            "pz_session_arm": self._session_arm,
            "pz_session_disarm": self._session_disarm,
            "pz_observe_snapshot": self._observe_snapshot,
            "pz_observe_inventory": self._observe_inventory,
            "pz_observe_nearby": self._observe_nearby,
            # Every tool that names an action is routed to the same handler, and
            # that is the point: the schema field is the adapter argument, so
            # there is nothing per-action to translate. A handler that knew
            # which arguments `medical.bandage` takes would be a second copy of
            # the adapter's own parser, and the copy is what drifts.
            "pz_action_inspect_world": self._submit,
            "pz_action_inspect_container": self._submit,
            "pz_action_search_inventory": self._submit,
            "pz_action_move_to": self._submit,
            "pz_action_move_near": self._submit,
            "pz_action_open_container": self._submit,
            "pz_action_open_door": self._submit,
            "pz_action_close_door": self._submit,
            "pz_action_unlock_door": self._submit,
            "pz_action_transfer": self._submit,
            "pz_action_ensure_main": self._submit,
            "pz_action_eat": self._submit,
            "pz_action_drink": self._submit,
            "pz_action_drink_source": self._submit,
            "pz_action_read": self._submit,
            "pz_action_equip": self._submit,
            "pz_action_unequip": self._submit,
            "pz_action_bandage": self._submit,
            "pz_action_rest": self._submit,
            "pz_action_sleep": self._submit,
            "pz_action_wait": self._submit,
            "pz_action_cancel": self._submit,
            "pz_action_cancel_all": self._cancel_all,
            "pz_action_status": self._action_status,
            "pz_action_await": self._action_await,
            "pz_plan_execute": self._plan_execute,
            "pz_plan_status": self._plan_status,
            "pz_goal_submit": self._goal_submit,
            "pz_goal_status": self._goal_status,
            "pz_goal_cancel": self._goal_cancel,
            "pz_safety_stop": self._safety_stop,
            "pz_memory_query": self._memory_query,
            "pz_debug_doctor": self._debug_doctor,
            "pz_debug_tail": self._debug_tail,
        }
        missing = sorted({spec.name for spec in TOOLS} - set(self._handlers))
        if missing:
            # A published tool with no handler would be advertised and then
            # answer INTERNAL_ERROR, which is worse than not shipping it.
            raise ValueError(f"published tools without a handler: {missing}")

    # -- catalogue ---------------------------------------------------------

    def list_tools(self) -> list[JsonDict]:
        """Descriptors for the tools that are ready on this install."""
        return [spec.descriptor() for spec in published_tools(self._report())]

    def list_resources(self) -> list[JsonDict]:
        return [spec.descriptor() for spec in RESOURCES]

    # -- calling -----------------------------------------------------------

    def call(self, name: str, arguments: Mapping[str, Any] | None = None) -> JsonDict:
        """Run one tool and return its payload. Never raises."""
        request_id = self._request_ids()
        try:
            return self.invoke(name, arguments, request_id=request_id).to_payload()
        except Exception as exc:  # ports are foreign code; a crash is a payload, not a raise
            return failure_from(exc).to_payload(tool=name, request_id=request_id)

    def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> ToolSuccess:
        """Run one tool, raising :class:`ToolFailure` on a domain refusal."""
        rid = request_id if request_id is not None else self._request_ids()
        spec = TOOLS_BY_NAME.get(name)
        if spec is None:
            raise ToolFailure(ReasonCode.INVALID_ARGUMENT, f"no such tool: {name!r}")
        args = validate_arguments(spec.input_schema, arguments)
        self._require_capability(spec)

        key = args.get("idempotency_key")
        if isinstance(key, str):
            self._scope_cache_to_session()
            replayed = self._replay(spec, key, rid)
            if replayed is not None:
                return replayed
        self._require_armed(spec)

        outcome = self._handlers[spec.name](spec, args)
        answer = ToolSuccess.of(
            outcome,
            tool=spec.name,
            request_id=rid,
            warnings=self._warnings(spec, outcome.warnings),
        )
        if isinstance(key, str):
            self._cache.remember(
                key,
                CachedCall(
                    tool=spec.name,
                    payload=answer.data,
                    action_id=answer.action_id,
                    status=answer.status,
                ),
            )
        return answer

    # -- gates -------------------------------------------------------------

    def _require_capability(self, spec: ToolSpec) -> None:
        """Refuse a tool whose capability is not usable on this install."""
        capability = spec.required_capability
        if capability is None:
            return
        report = self._report()
        if report.usable(capability):
            return
        reason = report.unusable_reasons().get(capability, report.state(capability).value)
        raise ToolFailure(
            ReasonCode.CAPABILITY_UNAVAILABLE,
            f"{spec.name} is not published: capability {capability} is {reason}",
        )

    def _require_armed(self, spec: ToolSpec) -> None:
        """Refuse a write tool on a disarmed session.

        Control tools skip this by construction, not by exception: stopping,
        disarming and cancelling are how a disarmed or panicking session is
        driven, and gating them on arming would make the agent unstoppable by
        the mechanism meant to stop it.
        """
        if not spec.requires_armed:
            return
        session = self._services.session.status()
        if session.armed:
            return
        raise ToolFailure(
            ReasonCode.NOT_ARMED,
            f"session is in {session.mode.value}; call pz_session_arm first",
        )

    def _scope_cache_to_session(self) -> None:
        """Forget every key when the session changes.

        Idempotency keys are session-scoped because the refs the recorded call
        acted on are: replaying a previous session's answer would report work
        done in a world whose references no longer denote the same objects.
        """
        session_id = self._services.session.status().session_id
        if session_id == self._session_id:
            return
        self._cache.clear()
        self._session_id = session_id

    def _replay(self, spec: ToolSpec, key: str, request_id: str) -> ToolSuccess | None:
        """The answer this key already produced, refreshed if it named an action."""
        cached = self._cache.lookup(spec.name, key)
        if cached is None:
            return None
        record = (
            None if cached.action_id is None else self._services.actions.status(cached.action_id)
        )
        if record is None:
            # Either the call put nothing in flight, or the core no longer knows
            # about the action. Returning what this key produced — including the
            # status it answered with — is the honest answer; inventing a current
            # status for work nobody can see is not, and so is downgrading an
            # ``accepted`` to a bare ``ok`` because the envelope was rebuilt.
            return ToolSuccess(
                tool=spec.name,
                request_id=request_id,
                status=cached.status,
                data=cached.payload,
                message=f"{spec.name} already ran under this idempotency key",
                warnings=self._warnings(spec, ()),
                action_id=cached.action_id,
                replayed=True,
            )
        return ToolSuccess.of(
            self._action_outcome(record),
            tool=spec.name,
            request_id=request_id,
            warnings=self._warnings(spec, ()),
            replayed=True,
        )

    def _warnings(self, spec: ToolSpec, extra: Sequence[str]) -> tuple[str, ...]:
        """Warnings a caller should read before trusting the result."""
        warnings = list(extra)
        capability = spec.required_capability
        if capability is not None:
            state = self._report().state(capability)
            if state.usable and state.value != "verified":
                warnings.append(
                    f"capability {capability} is {state.value}: the API is present "
                    "on this install but no probe has been confirmed against it"
                )
        return tuple(warnings)

    # -- shared reads ------------------------------------------------------

    def _report(self) -> CapabilityReport:
        return self._services.capabilities.report()

    def _compact_view(self) -> JsonDict:
        """The redacted planner view of the newest observation."""
        observation = self._services.observations.latest()
        if observation is None:
            raise ToolFailure(
                ReasonCode.GAME_DISCONNECTED,
                "no observation has arrived; the game is not reporting state",
            )
        states = {c.name: c.state for c in self._report().capabilities}
        return compact_for_planner(observation, states)

    # -- session -----------------------------------------------------------

    def _session_status(self, spec: ToolSpec, args: JsonDict) -> ToolOutcome:
        session = self._services.session.status()
        data: JsonDict = {
            "connected": session.connected,
            "session_id": session.session_id,
            "mode": session.mode.value,
            "armed": session.armed,
            # ``mode`` and ``armed`` restated under the name that says whose
            # word they are. The pair above is kept unrenamed for existing
            # clients; the pair of vocabularies below is what makes the
            # disagreement readable.
            "desired_mode": session.mode.value,
            "protocol_version": session.protocol_version,
            "capability_revision": session.capability_revision,
            "observation_seq": session.observation_seq,
            "danger_level": session.danger_level.value,
            "active_action_id": as_token(session.active_action_id),
            "heartbeat": {
                "game_ok": session.game_heartbeat_ok,
                "sidecar_ok": session.sidecar_heartbeat_ok,
            },
            "game_build": as_token(session.build),
            # The raw save id is a directory fragment that can embed the profile
            # name, so what crosses the boundary is its digest (§3.13).
            "save_scope": None if session.save_id is None else save_scope(session.save_id),
        }
        # The game's own word beside the sidecar's, because the two disagreed
        # in the wild: a session armed on this side while the mod ran OFF
        # answered every status call as if the agent were driving. What the
        # game last said is read from the newest observation — the mod authored
        # it, so it is the game's claim and not this process's — and
        # ``heartbeat.game_ok`` above is how fresh that word is.
        data.update(self._game_arming_view(self._services.observations.latest(), session.armed))
        message = "session status"
        warnings: tuple[str, ...] = ()
        if data["armed_mismatch"]:
            message = (
                f"session status: the sidecar says armed={session.armed} and the "
                f"game's last word says armed={data['game_armed']}"
            )
            staleness = (
                ""
                if session.game_heartbeat_ok
                else " — and the game heartbeat is stale, so even that word is old"
            )
            warnings = (
                "arming disagreement: trust the game's word (observation seq "
                f"{data['game_view_seq']}) over the sidecar's flag{staleness}",
            )
        try:
            view = self._compact_view()
        except ToolFailure:
            data["observation"] = None
        else:
            data["observation"] = {
                "seq": view["seq"],
                "game": view["game"],
                "safety": view["safety"],
                "action": view["action"],
            }
        return ToolOutcome(data=data, message=message, warnings=warnings)

    @staticmethod
    def _game_arming_view(observation: Observation | None, sidecar_armed: bool) -> JsonDict:
        """The game's last word on arming, with nothing invented to fill gaps.

        ``None`` throughout means the game has said nothing yet, and
        ``armed_mismatch`` stays ``None`` with it rather than collapsing to
        ``False``: absent-as-agreement is exactly the reading the live defect
        hid behind. ``effective_mode`` is the mode the game is actually
        running, against the sidecar's ``desired_mode``.
        """
        if observation is None:
            return {
                "effective_mode": None,
                "game_armed": None,
                "game_session_id": None,
                "game_view_seq": None,
                "armed_mismatch": None,
            }
        return {
            "effective_mode": observation.safety.mode.value,
            "game_armed": observation.safety.armed,
            "game_session_id": as_token(observation.session_id),
            "game_view_seq": observation.seq,
            "armed_mismatch": sidecar_armed != observation.safety.armed,
        }

    def _session_arm(self, spec: ToolSpec, args: JsonDict) -> ToolOutcome:
        mode = SessionMode(args["mode"])
        session = self._services.session.arm(mode, confirm_backup=bool(args["confirm_backup"]))
        return ToolOutcome(
            data={"mode": session.mode.value, "armed": session.armed},
            message=f"session armed in {session.mode.value}",
        )

    def _session_disarm(self, spec: ToolSpec, args: JsonDict) -> ToolOutcome:
        session = self._services.session.disarm()
        return ToolOutcome(
            data={"mode": session.mode.value, "armed": session.armed},
            message="session disarmed",
        )

    def _safety_stop(self, spec: ToolSpec, args: JsonDict) -> ToolOutcome:
        report = self._services.session.stop()
        return ToolOutcome(
            data={
                "cleared": report.cleared,
                "disarmed": report.disarmed,
                "mode": report.mode.value,
            },
            message=f"stopped; {report.cleared} mod-owned queue entr(ies) cleared",
        )

    # -- observation -------------------------------------------------------

    def _observe_snapshot(self, spec: ToolSpec, args: JsonDict) -> ToolOutcome:
        view = self._compact_view()
        detail = str(args["detail"])
        data: JsonDict = {key: view[key] for key in _SNAPSHOT_HEADER}
        data["detail"] = detail
        if detail in {"standard", "full"}:
            data["nearby"] = view["nearby"]
        if detail == "full":
            data["inventory"] = view["inventory"]
        return ToolOutcome(data=data, message=f"{detail} snapshot")

    def _observe_inventory(self, spec: ToolSpec, args: JsonDict) -> ToolOutcome:
        view = self._compact_view()
        inventory = view["inventory"]
        if not inventory["available"]:
            raise ToolFailure(
                ReasonCode.CAPABILITY_UNAVAILABLE,
                "the mod did not report an inventory in the latest observation",
            )
        scope = str(args["scope"])
        containers = list(inventory["containers"])
        if scope != "all":
            kinds = _SCOPES[scope]
            containers = [c for c in containers if c["kind"] in kinds]
        if not args["include_nested"]:
            containers = [c for c in containers if c["parent_ref"] is None]
        visible = {c["ref"] for c in containers}
        items = [i for i in inventory["items"] if i["container_ref"] in visible]
        category = args.get("category")
        if category is not None:
            items = [i for i in items if i["category"] == category]
        return ToolOutcome(
            data={
                "seq": view["seq"],
                "content_marker": CONTENT_MARKER,
                "content_rule": CONTENT_RULE,
                "scope": scope,
                "include_nested": args["include_nested"],
                "category": category,
                "containers": containers,
                "items": items,
                "container_count": inventory["container_count"],
                "item_count": inventory["item_count"],
                "containers_truncated": inventory["containers_truncated"],
                "items_truncated": inventory["items_truncated"],
            },
            message=f"{len(items)} item(s) across {len(containers)} container(s)",
        )

    def _observe_nearby(self, spec: ToolSpec, args: JsonDict) -> ToolOutcome:
        view = self._compact_view()
        nearby = view["nearby"]
        if not nearby["available"]:
            raise ToolFailure(
                ReasonCode.CAPABILITY_UNAVAILABLE,
                "the mod did not report the surroundings in the latest observation",
            )
        radius = float(args["radius"])
        zombies = [z for z in nearby["zombies"] if z["distance"] <= radius]
        objects = [o for o in nearby["objects"] if o["distance"] <= radius]
        types = args.get("types")
        if types:
            wanted = set(types)
            objects = [o for o in objects if o["kind"] in wanted or wanted & set(o["semantics"])]
            if _ZOMBIE_TYPE not in wanted:
                zombies = []
        return ToolOutcome(
            data={
                "seq": view["seq"],
                "content_marker": CONTENT_MARKER,
                "radius": radius,
                "types": list(types) if types else [],
                "zombies": zombies,
                "objects": objects,
                "chasing_count": nearby["chasing_count"],
                "zombie_count": nearby["zombie_count"],
                "object_count": nearby["object_count"],
                "zombies_truncated": nearby["zombies_truncated"],
                "objects_truncated": nearby["objects_truncated"],
            },
            message=f"{len(zombies)} zombie(s), {len(objects)} object(s) within {radius}",
        )

    # -- actions -----------------------------------------------------------

    def _submit(self, spec: ToolSpec, args: JsonDict) -> ToolOutcome:
        """Hand one action to the core and report the id it came back with.

        Returns as soon as the action has an id. The status is whatever the core
        said it is — ``accepted`` for work that has only been queued, which is
        the honest word for it and the only one available until an adapter has
        observed the postcondition.
        """
        action = spec.action
        if action is None:
            raise ToolFailure(
                ReasonCode.INTERNAL_ERROR, f"{spec.name} is routed to submit but names no action"
            )
        session = self._services.session.status()
        record = self._services.actions.submit(
            ActionRequest(
                action=action,
                session_id=session.session_id,
                idempotency_key=str(args["idempotency_key"]),
                args={key: value for key, value in args.items() if key not in _ENVELOPE_KEYS},
                lease_ms=int(args["timeout_ms"]),
            )
        )
        if record.action is not action:
            # A port that answered about a different action would make the
            # returned id refer to work the caller never asked for.
            raise ToolFailure(
                ReasonCode.INTERNAL_ERROR,
                f"{spec.name} submitted {action.value} but the core reported {record.action.value}",
            )
        return self._action_outcome(record)

    def _action_outcome(self, record: ActionRecord) -> ToolOutcome:
        data: JsonDict = {
            "action": record.action.value,
            "status": record.status.value,
            "terminal": record.terminal,
        }
        if record.progress is not None:
            data["progress"] = record.progress
        if record.result is not None:
            data.update(self._result_payload(record.result))
        return ToolOutcome(
            data=data,
            status=record.status.value,
            action_id=record.action_id,
            message=f"{record.action.value} is {record.status.value}",
        )

    def _result_payload(self, result: ActionResult) -> JsonDict:
        """The terminal ack, with its own text quarantined and its evidence scrubbed.

        ``detail`` and ``diagnostics`` are the adapter's refusal wording, and the
        adapters interpolate game-authored text into it — ``consume.eat`` answers
        ``f"{item.display_name} has no portions left"``, and the display name is
        whatever a mod called the item. Redaction alone is not the rule this
        boundary states: free text leaves it *marked*, so a client can tell the
        game's words from the protocol's. Carried under the quarantine key with
        the marker beside it, exactly as ``_plan_execute`` carries the echoed
        goal — ``reason_code`` is what a client should branch on, and that stays
        outside because it is a member of a closed protocol vocabulary.
        """
        payload: JsonDict = {
            "reason_code": result.reason_code.value,
            "retryable": is_retryable(result.reason_code),
            "attempt": result.attempt,
            UNTRUSTED_TEXT_KEY: {
                "detail": scrub_text(result.message),
                "diagnostics": [scrub_text(line) for line in result.diagnostics[:MAX_DIAGNOSTICS]],
            },
            "content_marker": CONTENT_MARKER,
        }
        evidence = self._evidence_payload(result)
        if evidence:
            payload["evidence"] = evidence
        return payload

    @staticmethod
    def _evidence_payload(result: ActionResult) -> JsonDict:
        """The observed postcondition, structurally intact and content-scrubbed.

        ``kind`` and ``observation_seq`` are the adapter's own structural labels
        and are kept as such; ``observed`` is whatever it read out of the world,
        so that is the half the quarantine applies to.
        """
        evidence = evidence_payload(result)
        if not evidence:
            return {}
        observed = evidence.get("observed")
        if not isinstance(observed, Mapping):
            return scrub_payload(evidence)
        payload: JsonDict = {"observed": scrub_payload(observed)}
        kind = as_token(evidence.get("kind"))
        if kind is not None:
            payload["kind"] = kind
        seq = evidence.get("observation_seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            payload["observation_seq"] = seq
        return payload

    # -- asking after submitted work ----------------------------------------

    def _action_status(self, spec: ToolSpec, args: JsonDict) -> ToolOutcome:
        """One action's current record, or the honest 'unknown here'."""
        action_id = str(args["action_id"])
        record = self._services.actions.status(action_id)
        if record is None:
            return self._unknown_action(action_id)
        outcome = self._action_outcome(record)
        return replace(outcome, data={"known": True, **outcome.data})

    def _action_await(self, spec: ToolSpec, args: JsonDict) -> ToolOutcome:
        """Poll one action, bounded, until terminal; report the record either way.

        A budget that runs out is the *call's* end, not the action's, and the
        answer keeps the two apart: ``timed_out: true`` beside the record as it
        stands, so a caller never mistakes a slow action for a lost one. An id
        nobody here knows answers immediately — waiting for a record that
        cannot appear would be a timeout dressed as patience.
        """
        action_id = str(args["action_id"])
        record, waited_ms, timed_out = self._poll_until_terminal(action_id, int(args["timeout_ms"]))
        if record is None:
            unknown = self._unknown_action(action_id)
            return replace(
                unknown, data={**unknown.data, "waited_ms": waited_ms, "timed_out": False}
            )
        outcome = self._action_outcome(record)
        data = {"known": True, **outcome.data, "waited_ms": waited_ms, "timed_out": timed_out}
        message = (
            f"{record.action.value} is still {record.status.value} after {waited_ms} ms; "
            "the wait budget ended, not the action"
            if timed_out
            else outcome.message
        )
        return replace(outcome, data=data, message=message)

    def _poll_until_terminal(
        self, action_id: str, budget_ms: int
    ) -> tuple[ActionRecord | None, int, bool]:
        """Re-read *action_id* until its record is terminal or the budget ends.

        Doubly bounded — a deadline on the injected clock and a poll count —
        for the same reason the CLI's control waiter is: a monotonic clock that
        stopped moving must not turn the deadline into a spin. Nothing is held
        between reads; each ``status`` call is one bounded port read, so a stop
        tool on another connection is never waiting on this wait.
        """
        started = self._monotonic()
        deadline = started + budget_ms / 1000.0
        polls = max(1, budget_ms // ACTION_WAIT_POLL_MS + 1)
        record = self._services.actions.status(action_id)
        for _ in range(polls):
            if record is None or record.terminal:
                break
            if self._monotonic() >= deadline:
                break
            self._sleep(ACTION_WAIT_POLL_MS / 1000.0)
            record = self._services.actions.status(action_id)
        waited_ms = int((self._monotonic() - started) * 1000)
        return record, waited_ms, record is not None and not record.terminal

    @staticmethod
    def _unknown_action(action_id: str) -> ToolOutcome:
        """A typed 'unknown here', which is not 'it never ran'.

        The record store is a bounded ring that evicts terminal records, and a
        restarted sidecar holds nothing the previous process minted, so an
        unknown id is a routine fact of this surface rather than a fault. It is
        answered as data an agent loop can branch on; refusing instead would
        turn every poll of an old id into an error path, which is how the live
        session ended up with in-flight work nobody could ask about.
        """
        return ToolOutcome(
            data={
                "known": False,
                "action_id": action_id,
                "status": None,
                "terminal": None,
                "likely_causes": list(UNKNOWN_ACTION_CAUSES),
            },
            message=(
                "no record of that action survives here: either its terminal record "
                "was evicted from the bounded store, or the sidecar restarted since "
                "the id was minted. Unknown is not 'it did not run'."
            ),
        )

    def _cancel_all(self, spec: ToolSpec, args: JsonDict) -> ToolOutcome:
        """Submit the mass cancel and report exactly what this side can see.

        The command is the same capability-free ``plan.cancel`` the reflex
        guard and the panic path use, submitted with no ``command_id`` — the
        spelling every cancel adapter reads as "clear everything of ours".
        Ownership is decided by the mod's own tag against this session's
        observation, so the player's queued actions are out of reach by
        construction, and the negative postcondition makes a repeat call
        succeed against work that is already gone — idempotent by the action's
        own shape, not by caching.

        What is *not* reported is a number nobody on this side measured. The
        loop records ``CANCELLED_BY_REQUEST`` against each waiting submission
        and in-flight command its levers end, and no port on this surface
        carries those counts back, so ``cancelled_counts`` answers null — the
        same rule that keeps the panic stop's ``cleared`` at what was observed
        rather than what was hoped.
        """
        session = self._services.session.status()
        record = self._services.actions.submit(
            ActionRequest(
                action=ActionName.PLAN_CANCEL,
                session_id=session.session_id,
                idempotency_key=str(args["idempotency_key"]),
                args={},
                lease_ms=int(args["timeout_ms"]),
            )
        )
        if record.action is not ActionName.PLAN_CANCEL:
            raise ToolFailure(
                ReasonCode.INTERNAL_ERROR,
                f"{spec.name} submitted {ActionName.PLAN_CANCEL.value} but the core "
                f"reported {record.action.value}",
            )
        outcome = self._action_outcome(record)
        return replace(
            outcome,
            data={
                **outcome.data,
                "scope": "mod_owned",
                "requested_reason": ReasonCode.CANCELLED_BY_REQUEST.value,
                "cancelled_counts": {"channel_pending": None, "in_flight": None},
            },
            message=(
                f"mass cancel of mod-owned work is {record.status.value}; "
                "pz_action_await the action id for the engine's verdict"
            ),
            warnings=(
                "per-layer cancellation counts live with the loop that applies the "
                "levers and are not readable through this surface; null means "
                "uncounted, never zero",
            ),
        )

    # -- plans -------------------------------------------------------------

    def _plan_execute(self, spec: ToolSpec, args: JsonDict) -> ToolOutcome:
        limits = args.get("limits") or {}
        goal = str(args["goal"])
        record = self._services.plans.execute(
            PlanRequest(
                goal=goal,
                mode=SessionMode(args["mode"]),
                max_steps=int(limits.get("max_steps", MAX_PLAN_STEPS)),
                max_real_seconds=int(limits.get("max_real_seconds", DEFAULT_PLAN_REAL_SECONDS)),
                idempotency_key=str(args["idempotency_key"]),
            )
        )
        data = self._plan_payload(record)
        # The goal is echoed so the caller can confirm what was planned for, and
        # quarantined because free text arriving back unmarked would be exactly
        # the instruction channel §7.11 closes.
        data[UNTRUSTED_TEXT_KEY] = {"goal": scrub_text(goal)}
        data["content_marker"] = CONTENT_MARKER
        return ToolOutcome(
            data=data,
            status=_plan_envelope_status(record.status),
            message=f"plan is {record.status.value}",
        )

    def _plan_status(self, spec: ToolSpec, args: JsonDict) -> ToolOutcome:
        record = self._services.plans.current()
        if record is None:
            return ToolOutcome(data={"active": False}, message="no plan is running")
        data = self._plan_payload(record)
        data["active"] = not record.status.is_terminal
        return ToolOutcome(data=data, message=f"plan is {record.status.value}")

    @staticmethod
    def _plan_payload(record: PlanRecord) -> JsonDict:
        return {
            "plan_id": as_token(record.plan_id),
            "status": record.status.value,
            "terminal": record.status.is_terminal,
            "step_index": record.step_index,
            "step_count": len(record.steps),
            "stopped_reason": (
                None if record.stopped_reason is None else record.stopped_reason.value
            ),
            "retryable": (
                False if record.stopped_reason is None else is_retryable(record.stopped_reason)
            ),
            "steps": [
                {
                    "index": step.index,
                    "action": step.action.value,
                    "status": step.status.value,
                    "reason_code": (None if step.reason_code is None else step.reason_code.value),
                    "action_id": as_token(step.action_id),
                }
                for step in record.steps[:MAX_PLAN_STEPS_REPORTED]
            ],
        }

    # -- goals -------------------------------------------------------------

    def _goal_channel(self, spec: ToolSpec) -> GoalPort:
        """The goal port, or a refusal that names the leg which is missing.

        A bundle without one is not a bug here; it is a build whose Core RPC
        link carries no ``goal.*`` method (see
        :class:`~.ports.CoreServices`). ``CAPABILITY_UNAVAILABLE`` is the code
        this boundary already uses for "this install cannot do that", and the
        message says which half is absent so a user is not sent to look at the
        game.
        """
        goals = self._services.goals
        if goals is None:
            raise ToolFailure(
                ReasonCode.CAPABILITY_UNAVAILABLE,
                f"{spec.name} needs the typed goal channel and this build's core "
                "link exposes none; the channel is not reachable from here",
            )
        return goals

    def _goal_submit(self, spec: ToolSpec, args: JsonDict) -> ToolOutcome:
        """Admit one goal to the channel and report the id it came back with.

        Never claims the goal is being served: the channel's own answer is
        ``pending`` until its activation loop promotes it, and that word is
        carried through rather than smoothed into "ok, working on it".
        """
        goals = self._goal_channel(spec)
        admission = goals.submit(self._goal_request(spec, args))
        refusal = admission.refusal
        if refusal is not None:
            # Relayed, not reclassified: the channel decided the code, and this
            # side knows strictly less about why it said no. Its messages are
            # assembled from constants and ids it minted — never from a caller's
            # bytes — which is what makes relaying one safe.
            raise ToolFailure(refusal.reason_code, refusal.message)
        goal = admission.goal
        if goal is None:
            raise ToolFailure(
                ReasonCode.INTERNAL_ERROR,
                f"{spec.name}: the channel accepted a goal and named none",
            )
        data = self._goal_payload(goal)
        data["duplicate"] = admission.duplicate
        return ToolOutcome(
            data=data,
            message=(
                f"this key already created a goal; it is {goal.state.value}"
                if admission.duplicate
                else f"goal admitted; it is {goal.state.value}"
            ),
        )

    def _goal_status(self, spec: ToolSpec, args: JsonDict) -> ToolOutcome:
        goals = self._goal_channel(spec)
        wanted = args.get("goal_id")
        goal_id = None if wanted is None else str(wanted)
        status = goals.status(goal_id)
        named = status.named
        if goal_id is not None and named is None:
            # Answering "goal: null" would read as "that goal is not running",
            # which is a fact nothing here established: the id may name a goal
            # the channel finished and forgot, or no goal that ever existed.
            raise ToolFailure(
                ReasonCode.INVALID_ARGUMENT,
                f"{spec.name}: this channel holds no goal with that id; it may "
                "have been forgotten after finishing",
            )
        # One past the ceiling, so a port offering more than this tool reports
        # can be *said* to have done so rather than quietly cut off.
        pending = list(islice(status.pending, MAX_PENDING_GOALS_REPORTED + 1))
        truncated = len(pending) > MAX_PENDING_GOALS_REPORTED
        data: JsonDict = {
            "goal": None if named is None else self._goal_payload(named),
            "active": None if status.active is None else self._goal_payload(status.active),
            "pending": [
                self._goal_payload(record) for record in pending[:MAX_PENDING_GOALS_REPORTED]
            ],
            "pending_truncated": truncated,
        }
        warnings = (
            (
                f"the backlog was cut off at {MAX_PENDING_GOALS_REPORTED} goal(s); "
                "this answer is not the whole channel",
            )
            if truncated
            else ()
        )
        if named is not None:
            return ToolOutcome(data=data, message=f"goal is {named.state.value}", warnings=warnings)
        return ToolOutcome(
            data=data,
            message=(
                f"{'one' if status.active is not None else 'no'} active goal, "
                f"{len(pending[:MAX_PENDING_GOALS_REPORTED])} pending"
            ),
            warnings=warnings,
        )

    def _goal_cancel(self, spec: ToolSpec, args: JsonDict) -> ToolOutcome:
        goals = self._goal_channel(spec)
        cancellation = goals.cancel(str(args["goal_id"]))
        goal = cancellation.goal
        if goal is None:
            raise ToolFailure(
                ReasonCode.INVALID_ARGUMENT,
                f"{spec.name}: this channel holds no goal with that id; it may "
                "have been forgotten after finishing",
            )
        data = self._goal_payload(goal)
        # Not "cancelled". The channel applies a cancellation on its next tick,
        # so an accepted request is routinely reported against a goal that is
        # still running; naming this field for the outcome would be the same
        # early claim ActionRecord refuses to make about a postcondition. What
        # the goal *is* stays in `state`, where it came from the record.
        data["cancel_requested"] = cancellation.requested
        return ToolOutcome(
            data=data,
            message=(
                f"cancellation requested; goal is {goal.state.value}"
                if cancellation.requested
                else f"nothing to cancel; the goal had already ended as {goal.state.value}"
            ),
        )

    @staticmethod
    def _goal_request(spec: ToolSpec, args: JsonDict) -> GoalRequest:
        """The channel's own request object, built from validated arguments.

        The schema pins the closed sets and every numeric range, so what is left
        for these constructors to refuse is the one rule the validated schema
        subset cannot state: which parameters each kind requires and which it
        forbids — ``train_skill`` without a skill, ``satisfy_to`` on a reading
        goal. The channel refuses those with a ``ValueError``, which
        :func:`~.envelope.failure_from` would report as ``INTERNAL_ERROR``,
        blaming this process for the caller's argument. It is translated here
        instead, and the channel's wording is safe to carry because
        :mod:`pz_agent_core.goals.model` never quotes a byte the caller sent.
        """
        skill = args.get("skill")
        try:
            return GoalRequest(
                kind=GoalKind(str(args["kind"])),
                idempotency_key=str(args["idempotency_key"]),
                params=GoalParams(
                    skill=None if skill is None else TrainableSkill(str(skill)),
                    target_level=args.get("target_level"),
                    satisfy_to=args.get("satisfy_to"),
                    pages=args.get("pages"),
                    target_x=args.get("target_x"),
                    target_y=args.get("target_y"),
                    target_z=args.get("target_z"),
                ),
            )
        except ValueError as rejected:
            raise ToolFailure(ReasonCode.INVALID_ARGUMENT, f"{spec.name}: {rejected}") from rejected

    @staticmethod
    def _goal_payload(record: GoalRecord) -> JsonDict:
        """One goal, in the vocabulary a client branches on.

        The state is reported as ``data.state`` and never as the envelope's
        ``status``, for the reason :func:`_plan_envelope_status` spells out one
        section up: ``ToolSuccess`` reserves ``succeeded`` for a result carrying
        the observed postcondition under ``data.evidence``, and a goal record
        carries only the *names* of the fields that were observed — the channel
        drops their values deliberately, because they are forwarded from the
        game. Borrowing the word would refuse a perfectly good answer about a
        goal that finished.

        ``key_digest`` is not published. It is value-free by construction, but
        it is a fingerprint of a caller's key and it buys a client nothing it
        cannot get from the goal id.
        """
        params = record.params
        param_payload: JsonDict = {}
        if params.skill is not None:
            param_payload["skill"] = params.skill.value
        if params.target_level is not None:
            param_payload["target_level"] = params.target_level
        if params.satisfy_to is not None:
            param_payload["satisfy_to"] = params.satisfy_to
        if params.pages is not None:
            param_payload["pages"] = params.pages
        if params.target_x is not None:
            param_payload["target_x"] = params.target_x
        if params.target_y is not None:
            param_payload["target_y"] = params.target_y
        if params.target_z is not None:
            param_payload["target_z"] = params.target_z
        data: JsonDict = {
            "goal_id": as_token(record.goal_id),
            "kind": record.kind.value,
            "state": record.state.value,
            "terminal": record.state.is_terminal,
            "reason_code": (None if record.reason_code is None else record.reason_code.value),
            "retryable": (
                False if record.reason_code is None else is_retryable(record.reason_code)
            ),
            "params": param_payload,
            "budget": {
                "max_wall_ms": record.budget.max_wall_ms,
                "max_steps": record.budget.max_steps,
                "pending_ttl_ms": record.budget.pending_ttl_ms,
            },
            "steps_used": record.steps_used,
            "steps_left": record.steps_left,
            "submitted_at_ms": record.submitted_at_ms,
            "started_at_ms": record.started_at_ms,
            "finished_at_ms": record.finished_at_ms,
            "deadline_ms": record.deadline_ms,
            # Field *names* only, and each one dropped unless it is a token: the
            # record's own bound caps how many there are, and the shape check is
            # what stops a port putting a sentence where a field name belongs.
            "evidence_keys": [key for key in map(as_token, record.evidence_keys) if key],
        }
        if record.detail:
            # The channel assembles `detail` from its own constants, so this is
            # belt and braces — but it is the one string on a goal that a future
            # producer could widen, and quarantining costs a key.
            data[UNTRUSTED_TEXT_KEY] = {"detail": scrub_text(record.detail)}
            data["content_marker"] = CONTENT_MARKER
        return data

    # -- memory and diagnostics -------------------------------------------

    def _memory_query(self, spec: ToolSpec, args: JsonDict) -> ToolOutcome:
        limit = int(args["limit"])
        kinds = tuple(args.get("kinds") or ())
        records = self._services.memory.query(kinds=kinds, limit=limit)
        payload: list[JsonDict] = []
        dropped = 0
        for record in islice(records, limit):
            kind = as_token(record.kind)
            key = as_token(record.key)
            if kind is None or key is None:
                # A record whose own identifiers are not identifiers cannot be
                # reported without inventing names for it. It is counted, not
                # silently skipped: a shorter list with no explanation reads as
                # "that is all there is".
                dropped += 1
                continue
            entry: JsonDict = {
                "kind": kind,
                "key": key,
                "refs": [ref for ref in record.refs if is_reference(ref)][:MAX_REFS_PER_RECORD],
                "data": scrub_payload(record.data),
            }
            if record.label is not None:
                entry[UNTRUSTED_TEXT_KEY] = {"label": scrub_text(record.label)}
                entry["content_marker"] = CONTENT_MARKER
            payload.append(entry)
        return ToolOutcome(
            data={
                "kinds": list(kinds),
                "limit": limit,
                "records": payload,
                "omitted": dropped,
            },
            message=f"{len(payload)} memory record(s)",
            warnings=_omitted_warning(dropped, "memory record"),
        )

    def _debug_doctor(self, spec: ToolSpec, args: JsonDict) -> ToolOutcome:
        checks: list[JsonDict] = []
        dropped = 0
        # One past the ceiling, so a port that offered more than the tool will
        # report can be *said* to have done so rather than quietly cut off.
        raw = list(islice(self._services.diagnostics.doctor(), MAX_DOCTOR_CHECKS + 1))
        truncated = len(raw) > MAX_DOCTOR_CHECKS
        for check in raw[:MAX_DOCTOR_CHECKS]:
            code = as_token(check.code)
            if code is None:
                dropped += 1
                continue
            checks.append(
                {
                    "code": code,
                    "ok": check.ok,
                    "detail": scrub_text(check.detail),
                    "remediation": scrub_text(check.remediation),
                }
            )
        failed = [check["code"] for check in checks if not check["ok"]]
        warnings = _omitted_warning(dropped, "environment check")
        if truncated:
            warnings = (
                *warnings,
                f"the environment report was cut off at {MAX_DOCTOR_CHECKS} checks; "
                "this answer is not the whole doctor",
            )
        return ToolOutcome(
            data={
                "checks": checks,
                # A doctor that could not report every check has not found the
                # environment healthy; it has found part of it healthy.
                "ok": not failed and not dropped and not truncated,
                "failed": failed,
                "omitted": dropped,
                "truncated": truncated,
                "protocol_version": PROTOCOL_VERSION,
            },
            message=f"{len(checks)} check(s), {len(failed)} failing",
            warnings=warnings,
        )

    def _debug_tail(self, spec: ToolSpec, args: JsonDict) -> ToolOutcome:
        limit = int(args["limit"])
        records = self._services.diagnostics.tail(
            limit=limit,
            level=args.get("level"),
            component=args.get("component"),
            action_id=args.get("action_id"),
        )
        lines: list[JsonDict] = []
        dropped = 0
        for record in islice(records, limit):
            level = as_token(record.level)
            component = as_token(record.component)
            if level is None or component is None:
                dropped += 1
                continue
            lines.append(
                {
                    "timestamp_ms": record.timestamp_ms,
                    "level": level,
                    "component": component,
                    "code": as_token(record.code),
                    "action_id": as_token(record.action_id),
                    "message": scrub_text(record.message),
                }
            )
        return ToolOutcome(
            data={"limit": limit, "records": lines, "omitted": dropped},
            message=f"{len(lines)} log record(s)",
            warnings=_omitted_warning(dropped, "log record"),
        )

    # -- documents behind the resources ------------------------------------

    def capability_document(self) -> JsonDict:
        """The probe results, their evidence, and what is published because of them.

        Evidence is summarised rather than copied: the ``file`` of a static
        finding is an absolute path into the game install, and §3.13 does not let
        one cross this boundary.
        """
        report = self._report()
        return {
            "build": as_token(report.build),
            "revision": report.revision,
            "protocol_version": report.protocol_version,
            "capabilities": {c.name: self._capability_payload(c) for c in report.capabilities},
            "published_tools": [spec.name for spec in published_tools(report)],
            "withheld_tools": {
                name: scrub_text(reason) for name, reason in withheld_tools(report).items()
            },
        }

    @staticmethod
    def _capability_payload(capability: Capability) -> JsonDict:
        return {
            "state": capability.state.value,
            "reason": scrub_text(capability.reason),
            "usable": capability.usable,
            "has_runtime_evidence": capability.has_runtime_evidence,
            "evidence": [
                {
                    "kind": item.kind.value,
                    "symbol": as_token(item.symbol),
                    "probe": as_token(item.probe),
                    "observed_at": item.observed_at,
                }
                for item in capability.evidence[:MAX_EVIDENCE_ENTRIES]
            ],
        }

    def safety_document(self) -> JsonDict:
        """Danger level, takeover state and heartbeat health, in one view."""
        session = self._services.session.status()
        document: JsonDict = {
            "session_id": session.session_id,
            "mode": session.mode.value,
            "armed": session.armed,
            "connected": session.connected,
            "danger_level": session.danger_level.value,
            "heartbeat": {
                "game_ok": session.game_heartbeat_ok,
                "sidecar_ok": session.sidecar_heartbeat_ok,
            },
        }
        try:
            view = self._compact_view()
        except ToolFailure:
            document["observed"] = None
            return document
        document["observed"] = {"seq": view["seq"], **view["safety"]}
        return document
