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

The action tools map their arguments straight onto the adapter's argument names.
That is deliberate: the schema field is the adapter field, so an argument this
layer accepts is one the adapter understands, and there is no translation table
to fall out of date.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from itertools import islice
from typing import Any, Final

from pz_agent_core.actions import ActionRequest
from pz_agent_core.capabilities.model import (
    MAX_EVIDENCE_PER_CAPABILITY,
    Capability,
    CapabilityReport,
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
    ActionResult,
    ActionStatus,
    ContainerKind,
    JsonDict,
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
    PlanRecord,
    PlanRequest,
    evidence_payload,
)
from .scrub import as_token, is_reference, scrub_payload, scrub_text
from .validation import validate_arguments

__all__ = [
    "MAX_DOCTOR_CHECKS",
    "MAX_EVIDENCE_ENTRIES",
    "MAX_PLAN_STEPS_REPORTED",
    "MAX_REFS_PER_RECORD",
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

#: ``pz_debug_doctor`` takes no arguments, so it has no caller-supplied limit to
#: bound its answer with. The check list is short and fixed in the core, but a
#: port is foreign code and an unbounded read of one is a bug by house rule.
MAX_DOCTOR_CHECKS: Final = 64

_ZOMBIE_TYPE: Final = "zombie"

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
    ) -> None:
        self._services = services
        self._cache = cache if cache is not None else IdempotencyCache()
        self._request_ids = request_ids
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
            "pz_action_transfer": self._submit,
            "pz_action_ensure_main": self._submit,
            "pz_action_eat": self._submit,
            "pz_action_drink": self._submit,
            "pz_action_read": self._submit,
            "pz_action_equip": self._submit,
            "pz_action_unequip": self._submit,
            "pz_action_bandage": self._submit,
            "pz_action_rest": self._submit,
            "pz_action_sleep": self._submit,
            "pz_action_wait": self._submit,
            "pz_action_cancel": self._submit,
            "pz_plan_execute": self._plan_execute,
            "pz_plan_status": self._plan_status,
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
        return ToolOutcome(data=data, message="session status")

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
