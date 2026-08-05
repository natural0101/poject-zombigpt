"""The published surface: exactly the tools and resources of ``docs/MCP_TOOLS.md``.

Each tool is one :class:`ToolSpec`. The schema in it is both what the client is
shown and what :mod:`.validation` enforces, so an advertised bound and a checked
bound cannot drift apart.

Three properties of the set are decided here rather than in the handlers:

* **What requires arming.** :class:`ToolKind` splits read tools (permitted in
  ``OBSERVE``) from write tools (armed session required) from control tools.
  Control is arming, disarming, cancelling and stopping — the four that must
  work *because* something has gone wrong, and gating them on a healthy session
  would make the agent unstoppable by the mechanism meant to stop it. It is the
  same set as the protocol's :data:`~pz_agent_core.protocol.ALWAYS_ALLOWED_ACTIONS`
  plus arming, which cannot require the state it establishes.
* **What is published.** A tool naming a capability that is not
  :meth:`~pz_agent_core.capabilities.model.CapabilityReport.usable` is withheld
  with its reason instead of being offered and then failing. ``experimental``
  and ``unsupported`` are both unusable; that judgement belongs to the
  capability model and is read from it, never recomputed.
* **Which numbers are legal.** The bounds come from the adapters that will
  receive the arguments, imported rather than restated, so a schema cannot
  advertise a radius the adapter refuses.

Deliberately absent, and each absence is a rule: no tool selects *which* item to
eat, drink or read (that is deterministic policy in
:mod:`pz_agent_core.policy`); no tool carries Lua, Python, shell, keystrokes or
a file path, because no field anywhere accepts a free string except a plan goal
and a filter token; and ``allow_windows`` is not published at all, since the
movement adapter refuses it with ``POLICY_DENIED`` and a boundary should not
offer what policy forbids.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pz_agent_core.actions.adapters.consume import MIN_CONSUME_FRACTION
from pz_agent_core.actions.adapters.literature import DEFAULT_READ_PAGES, MAX_READ_PAGES
from pz_agent_core.actions.adapters.movement import (
    DEFAULT_ARRIVAL_RADIUS,
    DEFAULT_MOVE_DISTANCE_SQUARES,
    MAX_ARRIVAL_RADIUS,
    MAX_MOVE_DISTANCE_SQUARES,
)
from pz_agent_core.actions.builtin import MAX_WAIT_GAME_SECONDS
from pz_agent_core.actions.engine import DEFAULT_LEASE_MS
from pz_agent_core.capabilities.model import CapabilityReport
from pz_agent_core.capabilities.probes import (
    DRINK_CARRIED,
    EAT_PERCENTAGE,
    INVENTORY_TRANSFER,
    MOVE_TO_SQUARE,
    READ_LITERATURE,
)
from pz_agent_core.protocol import ActionName, JsonDict, RefKind, RiskClass
from pz_agent_core.protocol.messages import MAX_LEASE_MS, MIN_LEASE_MS

__all__ = [
    "DEFAULT_MEMORY_RESULTS",
    "DEFAULT_OBSERVE_RADIUS",
    "DEFAULT_PLAN_REAL_SECONDS",
    "DEFAULT_TAIL_RECORDS",
    "MAX_GOAL_CHARS",
    "MAX_IDEMPOTENCY_KEY_CHARS",
    "MAX_MEMORY_RESULTS",
    "MAX_OBSERVE_RADIUS",
    "MAX_PLAN_REAL_SECONDS",
    "MAX_PLAN_STEPS",
    "MAX_TAIL_RECORDS",
    "RESOURCES",
    "RESOURCES_BY_URI",
    "TOOLS",
    "TOOLS_BY_NAME",
    "ResourceSpec",
    "ToolKind",
    "ToolSpec",
    "published_tools",
    "withheld_tools",
]

#: Nearby is reported within the observation radius; asking for more than the
#: mod scans would be a promise the observation cannot keep.
MAX_OBSERVE_RADIUS: Final = 30.0
DEFAULT_OBSERVE_RADIUS: Final = 10.0

MAX_IDEMPOTENCY_KEY_CHARS: Final = 120
MAX_GOAL_CHARS: Final = 200
MAX_TAIL_RECORDS: Final = 100
DEFAULT_TAIL_RECORDS: Final = 20
MAX_MEMORY_RESULTS: Final = 50
DEFAULT_MEMORY_RESULTS: Final = 20
MAX_PLAN_STEPS: Final = 8
MAX_PLAN_REAL_SECONDS: Final = 600
DEFAULT_PLAN_REAL_SECONDS: Final = 120

#: Filter tokens (categories, components, memory kinds) are identifiers, not
#: prose. A sentence in one of those positions is a red flag, so the pattern is
#: strict rather than merely length-bounded.
_IDENTIFIER_PATTERN: Final = r"^[a-z][a-z0-9_.\-]{0,63}$"

_REF_PATTERN: Final = r"^{kind}:[A-Za-z0-9:_.\-]{{1,200}}$"


def _ref_schema(kind: RefKind, description: str) -> JsonDict:
    """A reference argument. Shape only — the adapter decides whether it resolves.

    Session ownership is checked in
    :func:`pz_agent_core.actions.adapters.common.read_ref`, which is where
    ``INVALID_REF`` for a reference minted by another session is decided once
    for every adapter.
    """
    return {
        "type": "string",
        "description": description,
        "pattern": _REF_PATTERN.format(kind=kind.value),
        "maxLength": 220,
    }


_IDEMPOTENCY_KEY: Final[JsonDict] = {
    "type": "string",
    "description": (
        "Caller-chosen key. Calling again with the same key returns the original "
        "action instead of performing it a second time."
    ),
    "minLength": 1,
    "maxLength": MAX_IDEMPOTENCY_KEY_CHARS,
}

_TIMEOUT_MS: Final[JsonDict] = {
    "type": "integer",
    "description": "Lease for the command, in milliseconds.",
    "minimum": MIN_LEASE_MS,
    "maximum": MAX_LEASE_MS,
    "default": DEFAULT_LEASE_MS,
}


def _mutating(
    properties: Mapping[str, JsonDict], *, required: Iterable[str], lease: bool = True
) -> JsonDict:
    """An input schema for a tool that changes the world.

    Every one of them carries an idempotency key, and nothing else free-form:
    there is deliberately no field for prose on a write path.

    ``lease`` is false for the one mutating tool that does not submit a single
    command — a plan is bounded by ``limits.max_real_seconds``, not by a command
    lease. Publishing ``timeout_ms`` there would advertise an argument no handler
    reads, which is the same lie as advertising a bound nothing enforces.
    """
    envelope: JsonDict = {"idempotency_key": _IDEMPOTENCY_KEY}
    if lease:
        envelope["timeout_ms"] = _TIMEOUT_MS
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {**properties, **envelope},
        "required": [*required, "idempotency_key"],
    }


_NO_ARGUMENTS: Final[JsonDict] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
    "required": [],
}


class ToolKind(StrEnum):
    """What a tool needs from the session before it may run."""

    #: Observation and diagnostics. Permitted in ``OBSERVE``; never gated.
    READ = "read"
    #: Changes the world. Refused with ``NOT_ARMED`` on a disarmed session.
    WRITE = "write"
    #: Arming, disarming, cancelling, stopping. Never gated on arming, because
    #: these are how a disarmed or panicking session is driven.
    CONTROL = "control"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One published tool: what it needs, what it costs, and what it accepts."""

    name: str
    kind: ToolKind
    risk: RiskClass
    summary: str
    input_schema: JsonDict
    required_capability: str | None = None
    action: ActionName | None = None
    long_running: bool = False

    def __post_init__(self) -> None:
        if self.long_running and self.action is None:
            raise ValueError(f"{self.name}: a long-running tool must name the action it submits")
        if self.kind is ToolKind.READ and self.action is not None:
            raise ValueError(f"{self.name}: a read tool must not submit an action")

    @property
    def requires_armed(self) -> bool:
        return self.kind is ToolKind.WRITE

    def descriptor(self) -> JsonDict:
        """What a client is shown for this tool."""
        out: JsonDict = {
            "name": self.name,
            "description": self.summary,
            "inputSchema": self.input_schema,
            "kind": self.kind.value,
            "risk": self.risk.value,
            "requires_armed": self.requires_armed,
            "long_running": self.long_running,
        }
        if self.required_capability is not None:
            out["capability"] = self.required_capability
        return out


TOOLS: Final[tuple[ToolSpec, ...]] = (
    # --- session ----------------------------------------------------------
    ToolSpec(
        name="pz_session_status",
        kind=ToolKind.READ,
        risk=RiskClass.P0,
        summary=(
            "Mode, armed state, session id, heartbeat health, game build and "
            "capability revision. Answers even when the game is not connected."
        ),
        input_schema=_NO_ARGUMENTS,
    ),
    ToolSpec(
        name="pz_session_arm",
        kind=ToolKind.CONTROL,
        risk=RiskClass.P0,
        summary=(
            "Move the session to ASSISTED or AUTONOMOUS. Autonomous requires an "
            "existing save backup; the core refuses without one."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "mode": {
                    "type": "string",
                    "description": "Target mode.",
                    "enum": ["ASSISTED", "AUTONOMOUS"],
                },
                "confirm_backup": {
                    "type": "boolean",
                    "description": "The caller has confirmed a save backup exists.",
                    "default": False,
                },
            },
            "required": ["mode"],
        },
    ),
    ToolSpec(
        name="pz_session_disarm",
        kind=ToolKind.CONTROL,
        risk=RiskClass.P0,
        summary="Return to OBSERVE and stop accepting new automation. Always permitted.",
        input_schema=_NO_ARGUMENTS,
    ),
    # --- observation ------------------------------------------------------
    ToolSpec(
        name="pz_observe_snapshot",
        kind=ToolKind.READ,
        risk=RiskClass.P0,
        summary=(
            "Current world state, compacted for a model. 'compact' is the "
            "player and safety header, 'standard' adds the surroundings, 'full' "
            "adds the inventory. There is no rawer level: every level is the "
            "redacted planner view."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "detail": {
                    "type": "string",
                    "description": "How much of the compacted view to return.",
                    "enum": ["compact", "standard", "full"],
                    "default": "compact",
                }
            },
            "required": [],
        },
    ),
    ToolSpec(
        name="pz_observe_inventory",
        kind=ToolKind.READ,
        risk=RiskClass.P0,
        summary=(
            "Container tree with stable refs, recursing into nested carried "
            "containers. Item display names are untrusted data and are marked as such."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "scope": {
                    "type": "string",
                    "description": "Which containers to report.",
                    "enum": ["all", "on_person", "player_main", "carried", "worn", "world"],
                    "default": "all",
                },
                "include_nested": {
                    "type": "boolean",
                    "description": "Recurse into containers held inside other containers.",
                    "default": True,
                },
                "category": {
                    "type": "string",
                    "description": "Keep only items whose category token matches.",
                    "pattern": _IDENTIFIER_PATTERN,
                    "maxLength": 64,
                },
            },
            "required": [],
        },
    ),
    ToolSpec(
        name="pz_observe_nearby",
        kind=ToolKind.READ,
        risk=RiskClass.P0,
        summary="World objects and zombies within a bounded radius, with their semantics.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "radius": {
                    "type": "number",
                    "description": "Radius in squares.",
                    "exclusiveMinimum": 0,
                    "maximum": MAX_OBSERVE_RADIUS,
                    "default": DEFAULT_OBSERVE_RADIUS,
                },
                "types": {
                    "type": "array",
                    "description": "Object kinds or semantics to keep; zombies are a kind.",
                    "maxItems": 8,
                    "items": {
                        "type": "string",
                        "pattern": _IDENTIFIER_PATTERN,
                        "maxLength": 64,
                    },
                },
            },
            "required": [],
        },
    ),
    # --- actions ----------------------------------------------------------
    ToolSpec(
        name="pz_action_move_to",
        kind=ToolKind.WRITE,
        risk=RiskClass.P3,
        summary=(
            "Walk to a square. Verified by the character's observed position "
            "being within the radius on the correct floor."
        ),
        required_capability=MOVE_TO_SQUARE,
        action=ActionName.MOVEMENT_MOVE_TO,
        long_running=True,
        input_schema=_mutating(
            {
                "target": {
                    "type": "object",
                    "description": "Destination square.",
                    "additionalProperties": False,
                    "properties": {
                        "x": {"type": "integer", "description": "World x."},
                        "y": {"type": "integer", "description": "World y."},
                        "z": {"type": "integer", "description": "Floor; basements are negative."},
                    },
                    "required": ["x", "y", "z"],
                },
                "radius": {
                    "type": "number",
                    "description": "How close counts as arrived, in squares.",
                    "exclusiveMinimum": 0,
                    "maximum": MAX_ARRIVAL_RADIUS,
                    "default": DEFAULT_ARRIVAL_RADIUS,
                },
                "max_distance": {
                    "type": "integer",
                    "description": "Refuse if the destination is further than this.",
                    "minimum": 1,
                    "maximum": MAX_MOVE_DISTANCE_SQUARES,
                    "default": DEFAULT_MOVE_DISTANCE_SQUARES,
                },
                "allow_doors": {
                    "type": "boolean",
                    "description": "May open doors on the way.",
                    "default": True,
                },
                "allow_stairs": {
                    "type": "boolean",
                    "description": "May change floor by stairs.",
                    "default": True,
                },
            },
            required=("target",),
        ),
    ),
    ToolSpec(
        name="pz_action_transfer",
        kind=ToolKind.WRITE,
        risk=RiskClass.P1,
        summary=(
            "Move one item into a container. Verified by the item resolving "
            "inside the destination and nowhere else."
        ),
        required_capability=INVENTORY_TRANSFER,
        action=ActionName.INVENTORY_TRANSFER,
        long_running=True,
        input_schema=_mutating(
            {
                "item_ref": _ref_schema(RefKind.ITEM, "The item to move."),
                "destination_container_ref": _ref_schema(
                    RefKind.CONTAINER, "Where it must end up."
                ),
                "source_container_ref": _ref_schema(
                    RefKind.CONTAINER, "Where it is now, when the caller knows."
                ),
            },
            required=("item_ref", "destination_container_ref"),
        ),
    ),
    ToolSpec(
        name="pz_action_eat",
        kind=ToolKind.WRITE,
        risk=RiskClass.P2,
        summary=(
            "Eat a named item. Verified by hunger falling or the item's uses "
            "decrementing. Which item is safe to eat is decided by core policy, "
            "not here: there is no tool that chooses one."
        ),
        required_capability=EAT_PERCENTAGE,
        action=ActionName.CONSUME_EAT,
        long_running=True,
        input_schema=_mutating(
            {
                "item_ref": _ref_schema(RefKind.ITEM, "The item to eat."),
                "fraction": {
                    "type": "number",
                    "description": "How much of it to consume.",
                    "minimum": MIN_CONSUME_FRACTION,
                    "maximum": 1.0,
                    "default": 1.0,
                },
            },
            required=("item_ref",),
        ),
    ),
    ToolSpec(
        name="pz_action_drink",
        kind=ToolKind.WRITE,
        risk=RiskClass.P2,
        summary=(
            "Drink from a named carried item. Verified by thirst falling or the "
            "container's volume decreasing."
        ),
        required_capability=DRINK_CARRIED,
        action=ActionName.CONSUME_DRINK,
        long_running=True,
        input_schema=_mutating(
            {
                "item_ref": _ref_schema(RefKind.ITEM, "The item to drink from."),
                "fraction": {
                    "type": "number",
                    "description": "How much of it to consume.",
                    "minimum": MIN_CONSUME_FRACTION,
                    "maximum": 1.0,
                    "default": 1.0,
                },
            },
            required=("item_ref",),
        ),
    ),
    ToolSpec(
        name="pz_action_read",
        kind=ToolKind.WRITE,
        risk=RiskClass.P2,
        summary="Read a named book. Verified by the observed page counter advancing.",
        required_capability=READ_LITERATURE,
        action=ActionName.LITERATURE_READ,
        long_running=True,
        input_schema=_mutating(
            {
                "item_ref": _ref_schema(RefKind.ITEM, "The book to read."),
                "pages": {
                    "type": "integer",
                    "description": "How many pages to read.",
                    "minimum": 1,
                    "maximum": MAX_READ_PAGES,
                    "default": DEFAULT_READ_PAGES,
                },
            },
            required=("item_ref",),
        ),
    ),
    ToolSpec(
        name="pz_action_wait",
        kind=ToolKind.WRITE,
        risk=RiskClass.P0,
        summary=(
            "Hold still until the world clock has advanced. Verified against "
            "observed game time, never the sidecar's wall clock."
        ),
        action=ActionName.ACTION_WAIT,
        long_running=True,
        input_schema=_mutating(
            {
                "game_seconds": {
                    "type": "number",
                    "description": "In-game seconds to wait for.",
                    "exclusiveMinimum": 0,
                    "maximum": MAX_WAIT_GAME_SECONDS,
                }
            },
            required=("game_seconds",),
        ),
    ),
    ToolSpec(
        name="pz_action_cancel",
        kind=ToolKind.CONTROL,
        risk=RiskClass.P1,
        summary=(
            "Cancel a mod-owned action. Verified by no mod-owned entry "
            "remaining in the queue; an action the player queued is never touched."
        ),
        action=ActionName.PLAN_CANCEL,
        long_running=True,
        input_schema=_mutating(
            {
                "command_id": {
                    "type": "string",
                    "description": "The action to cancel; omit to clear every mod-owned entry.",
                    "pattern": (
                        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                        r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                    ),
                    "maxLength": 36,
                }
            },
            required=(),
        ),
    ),
    # --- plans ------------------------------------------------------------
    ToolSpec(
        name="pz_plan_execute",
        kind=ToolKind.WRITE,
        risk=RiskClass.P2,
        summary=(
            "Submit a goal for the typed planner. The plan is validated before "
            "anything runs. There is no field for raw steps, code or file paths."
        ),
        input_schema=_mutating(
            {
                "goal": {
                    "type": "string",
                    "description": "What the user wants, in their own words.",
                    "minLength": 1,
                    "maxLength": MAX_GOAL_CHARS,
                },
                "mode": {
                    "type": "string",
                    "description": "Autonomy the plan may use.",
                    "enum": ["ASSISTED", "AUTONOMOUS"],
                    "default": "ASSISTED",
                },
                "limits": {
                    "type": "object",
                    "description": "Ceilings the plan must stay within.",
                    "additionalProperties": False,
                    "properties": {
                        "max_steps": {
                            "type": "integer",
                            "description": "Longest plan accepted.",
                            "minimum": 1,
                            "maximum": MAX_PLAN_STEPS,
                            "default": MAX_PLAN_STEPS,
                        },
                        "max_real_seconds": {
                            "type": "integer",
                            "description": "Wall-clock budget for the whole plan.",
                            "minimum": 1,
                            "maximum": MAX_PLAN_REAL_SECONDS,
                            "default": DEFAULT_PLAN_REAL_SECONDS,
                        },
                    },
                    "required": [],
                },
            },
            required=("goal",),
            lease=False,
        ),
    ),
    ToolSpec(
        name="pz_plan_status",
        kind=ToolKind.READ,
        risk=RiskClass.P0,
        summary="Current step, the results so far, and why the plan stopped.",
        input_schema=_NO_ARGUMENTS,
    ),
    # --- safety -----------------------------------------------------------
    ToolSpec(
        name="pz_safety_stop",
        kind=ToolKind.CONTROL,
        risk=RiskClass.P0,
        summary=(
            "Always available. Clears mod-owned queue entries only, disarms, and "
            "works while unarmed, while the planner is absent and while the "
            "queue is backed up. Takes no arguments so nothing can make it fail."
        ),
        input_schema=_NO_ARGUMENTS,
    ),
    # --- memory and diagnostics -------------------------------------------
    ToolSpec(
        name="pz_memory_query",
        kind=ToolKind.READ,
        risk=RiskClass.P0,
        summary=(
            "Known containers, home point, safe zones, failed paths and user "
            "reservations. Read-only; returns no secrets and no paths."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kinds": {
                    "type": "array",
                    "description": "Restrict to these record kinds.",
                    "maxItems": 8,
                    "items": {"type": "string", "pattern": _IDENTIFIER_PATTERN, "maxLength": 64},
                },
                "limit": {
                    "type": "integer",
                    "description": "Most records to return.",
                    "minimum": 1,
                    "maximum": MAX_MEMORY_RESULTS,
                    "default": DEFAULT_MEMORY_RESULTS,
                },
            },
            "required": [],
        },
    ),
    ToolSpec(
        name="pz_debug_doctor",
        kind=ToolKind.READ,
        risk=RiskClass.P0,
        summary="Full environment report with stable check codes and remediation.",
        input_schema=_NO_ARGUMENTS,
    ),
    ToolSpec(
        name="pz_debug_tail",
        kind=ToolKind.READ,
        risk=RiskClass.P0,
        summary="Recent structured log records, redacted and bounded.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Most records to return.",
                    "minimum": 1,
                    "maximum": MAX_TAIL_RECORDS,
                    "default": DEFAULT_TAIL_RECORDS,
                },
                "level": {
                    "type": "string",
                    "description": "Minimum severity.",
                    "enum": ["debug", "info", "warning", "error"],
                },
                "component": {
                    "type": "string",
                    "description": "Restrict to one component.",
                    "pattern": _IDENTIFIER_PATTERN,
                    "maxLength": 64,
                },
                "action_id": {
                    "type": "string",
                    "description": "Restrict to one action.",
                    "pattern": (
                        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                        r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
                    ),
                    "maxLength": 36,
                },
            },
            "required": [],
        },
    ),
)

TOOLS_BY_NAME: Final[dict[str, ToolSpec]] = {spec.name: spec for spec in TOOLS}


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    """A read-only view over state the core already holds.

    ``subscribable`` says the server will *push* this URI when it changes. It is
    false on every resource here and stays false until a change source exists:
    :func:`~.server.build_server` registers no ``subscribe_resource`` handler,
    and nothing in the core publishes resource-change events yet. Advertising a
    subscription a client could accept and then never be notified on is the
    quietest failure this surface could ship — the client would simply believe
    the world had stopped moving. Until then a client polls and uses the ``seq``
    each read carries as its ETag.
    """

    uri: str
    name: str
    summary: str
    subscribable: bool = False

    def descriptor(self) -> JsonDict:
        return {
            "uri": self.uri,
            "name": self.name,
            "description": self.summary,
            "mimeType": "application/json",
            "subscribable": self.subscribable,
        }


RESOURCES: Final[tuple[ResourceSpec, ...]] = (
    ResourceSpec(
        uri="pz://session/current",
        name="session",
        summary="Session id, mode, armed state and protocol version.",
    ),
    ResourceSpec(
        uri="pz://observation/latest",
        name="observation",
        summary="Most recent compacted snapshot.",
    ),
    ResourceSpec(
        uri="pz://inventory/current",
        name="inventory",
        summary="Container tree with stable refs.",
    ),
    ResourceSpec(
        uri="pz://capabilities",
        name="capabilities",
        summary="Probe results and the evidence behind them.",
    ),
    ResourceSpec(
        uri="pz://plan/current",
        name="plan",
        summary="Active plan and its step results.",
    ),
    ResourceSpec(
        uri="pz://safety/status",
        name="safety",
        summary=(
            "Danger level, takeover state and heartbeat health. Poll this one "
            "often: a safety change is the last thing to learn late, and this "
            "server does not push resource updates yet."
        ),
    ),
    ResourceSpec(
        uri="pz://diagnostics/recent",
        name="diagnostics",
        summary="Recent diagnostics, redacted.",
    ),
)

RESOURCES_BY_URI: Final[dict[str, ResourceSpec]] = {spec.uri: spec for spec in RESOURCES}


def published_tools(report: CapabilityReport) -> tuple[ToolSpec, ...]:
    """The tools that may be offered as ready against *report*."""
    return tuple(
        spec
        for spec in TOOLS
        if spec.required_capability is None or report.usable(spec.required_capability)
    )


def withheld_tools(report: CapabilityReport) -> dict[str, str]:
    """Tool name → why it is not published, for the capability report.

    A withheld tool is named with its reason rather than hidden: "eating is not
    available because ``eat_percentage`` has no verified API on this build" is
    actionable, and a silently missing tool is not.
    """
    reasons = report.unusable_reasons()
    withheld: dict[str, str] = {}
    for spec in TOOLS:
        capability = spec.required_capability
        if capability is None or report.usable(capability):
            continue
        withheld[spec.name] = reasons.get(capability, report.state(capability).value)
    return withheld
