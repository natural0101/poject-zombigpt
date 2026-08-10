"""The published surface: exactly the tools and resources of ``docs/MCP_TOOLS.md``.

Each tool is one :class:`ToolSpec`. The schema in it is both what the client is
shown and what :mod:`.validation` enforces, so an advertised bound and a checked
bound cannot drift apart.

Three properties of the set are decided here rather than in the handlers:

* **What requires arming.** :class:`ToolKind` splits read tools (permitted in
  ``OBSERVE``) from query tools (a command that only looks) from write tools
  (armed session required) from control tools. Control is arming, disarming,
  cancelling and stopping — the four that must work *because* something has gone
  wrong, and gating them on a healthy session would make the agent unstoppable
  by the mechanism meant to stop it. It is the same set as the protocol's
  :data:`~pz_agent_core.protocol.ALWAYS_ALLOWED_ACTIONS` plus arming, which
  cannot require the state it establishes. Which side of the read/write line an
  *action* falls on is never decided here either: it is read from
  :data:`~pz_agent_core.protocol.READ_ONLY_ACTIONS`, so
  ``container.open_nearby`` — whose name reads like a query and whose body walks
  the character across a room — cannot be talked into the unarmed half.
* **What is published.** A tool naming a capability that is not
  :meth:`~pz_agent_core.capabilities.model.CapabilityReport.usable` is withheld
  with its reason instead of being offered and then failing. ``experimental``
  and ``unsupported`` are both unusable; that judgement belongs to the
  capability model and is read from it, never recomputed.
* **Which numbers are legal.** The bounds come from the adapters that will
  receive the arguments, imported rather than restated, so a schema cannot
  advertise a radius the adapter refuses.

``risk`` is the *base* tier of the action a tool submits — the one its adapter
declares — and never a worst case invented here. Several adapters assess a
higher tier per call: ``movement.move_to`` is ``P3`` when the destination
changes floor or leaves the safe radius, and both transfer forms —
``inventory.transfer`` and ``inventory.transfer_batch`` — are ``P3`` when a
source is a world container. None of that is visible from the tool name, so
none of it can be published; what the descriptor states is the floor a caller needs
before the permission engine has seen the arguments. Publishing the escalated
tier instead would tell a caller holding a ``P2`` grant that a step across the
room is out of reach, and the engine would then allow it.

Every tool also carries one ``example``, validated against its own schema at
import. An example that a tool's schema rejects is caught here rather than by
the first client that pastes it.

The three ``pz_goal_*`` tools publish :mod:`pz_agent_core.goals`, and they are
the one part of this surface with no free-text field at all: a goal is a member
of a closed enum plus range-checked numbers, so ``pz_goal_submit`` is what a
caller reaches for when the *words* must not travel. ``pz_plan_execute`` still
carries a sentence, and that sentence is what a voice loop is forbidden to
forward (§7.11); the goal channel is the same intent expressed as tokens. Their
schemas therefore pin :class:`~pz_agent_core.goals.GoalKind` and
:class:`~pz_agent_core.goals.TrainableSkill` as ``enum``\\ s rather than as
patterns — an invented kind has to be *refused*, and a pattern that merely
looked plausible would let one through to a channel whose whole promise is that
it cannot carry one.

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
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from pz_agent_core.actions.adapters.consume import MIN_CONSUME_FRACTION
from pz_agent_core.actions.adapters.container import (
    DEFAULT_OPEN_RADIUS,
    MAX_LISTED_ITEMS,
    MIN_OPEN_RADIUS,
)
from pz_agent_core.actions.adapters.doors import DEFAULT_DOOR_RADIUS, MIN_DOOR_RADIUS
from pz_agent_core.actions.adapters.equipment import HANDS, MAX_SLOT_NAME_LEN
from pz_agent_core.actions.adapters.inventory import MAX_SEARCH_RESULTS, MAX_TYPE_FILTER_LEN
from pz_agent_core.actions.adapters.literature import DEFAULT_READ_PAGES, MAX_READ_PAGES
from pz_agent_core.actions.adapters.medical import BODY_PARTS
from pz_agent_core.actions.adapters.movement import (
    DEFAULT_ARRIVAL_RADIUS,
    DEFAULT_MOVE_DISTANCE_SQUARES,
    DEFAULT_NEAR_RADIUS,
    MAX_ARRIVAL_RADIUS,
    MAX_MOVE_DISTANCE_SQUARES,
)
from pz_agent_core.actions.adapters.survival import (
    DEFAULT_REST_TARGET,
    DEFAULT_REST_WAIT_MS,
    DEFAULT_SLEEP_HOURS,
    DEFAULT_SLEEP_WAIT_MS,
    MAX_REST_TARGET,
    MAX_REST_WAIT_MS,
    MAX_SLEEP_HOURS,
    MAX_SLEEP_WAIT_MS,
    MIN_REST_TARGET,
    MIN_SLEEP_HOURS,
    MIN_WAIT_MS,
)
from pz_agent_core.actions.adapters.world import DEFAULT_INSPECT_RADIUS, MAX_INSPECT_RADIUS
from pz_agent_core.actions.builtin import MAX_WAIT_GAME_SECONDS
from pz_agent_core.actions.engine import DEFAULT_LEASE_MS
from pz_agent_core.capabilities.model import CapabilityReport
from pz_agent_core.capabilities.probes import (
    DOOR_TOGGLE,
    DRINK_CARRIED,
    DRINK_WORLD_SOURCE,
    EAT_PERCENTAGE,
    EQUIPMENT_EQUIP,
    EQUIPMENT_UNEQUIP,
    INVENTORY_TRANSFER,
    MEDICAL_BANDAGE,
    MOVE_TO_SQUARE,
    READ_LITERATURE,
    SURVIVAL_REST,
    SURVIVAL_SLEEP,
)
from pz_agent_core.goals import (
    MAX_IDEMPOTENCY_KEY_LEN,
    NUMERIC_RANGES,
    GoalKind,
    TrainableSkill,
)
from pz_agent_core.protocol import READ_ONLY_ACTIONS, ActionName, JsonDict, RefKind, RiskClass
from pz_agent_core.protocol.messages import MAX_LEASE_MS, MIN_LEASE_MS

from .validation import validate_arguments

__all__ = [
    "DEFAULT_ACTION_WAIT_MS",
    "DEFAULT_MEMORY_RESULTS",
    "DEFAULT_OBSERVE_RADIUS",
    "DEFAULT_PLAN_REAL_SECONDS",
    "DEFAULT_TAIL_RECORDS",
    "EXAMPLE_SESSION_ID",
    "MAX_ACTION_WAIT_MS",
    "MAX_BATCH_ITEMS",
    "MAX_GOAL_CHARS",
    "MAX_IDEMPOTENCY_KEY_CHARS",
    "MAX_MEMORY_RESULTS",
    "MAX_OBSERVE_RADIUS",
    "MAX_PLAN_REAL_SECONDS",
    "MAX_PLAN_STEPS",
    "MAX_TAIL_RECORDS",
    "MIN_ACTION_WAIT_MS",
    "MIN_APPROACH_RADIUS",
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

#: The wait budget ``pz_action_await`` accepts. Not the command lease: a lease
#: bounds how long the *loop* may hold a command, this bounds how long one
#: status call may keep re-reading before it answers with the record as it
#: stands. The floor stops a budget shorter than a single poll interval from
#: advertising a wait that cannot happen; the ceiling keeps a stuck action from
#: parking a client for longer than a minute per call.
MIN_ACTION_WAIT_MS: Final = 100
MAX_ACTION_WAIT_MS: Final = 60_000
DEFAULT_ACTION_WAIT_MS: Final = 5_000

#: Items one ``inventory.transfer_batch`` may name: the batch contract's own
#: ceiling, the same eight the adapter reads ``item_refs`` against. Small on
#: purpose — every item is verified individually in the destination, so a wider
#: batch is a longer list of claims one command id has to answer for.
#: Restated from the contract rather than imported, and the seam check in
#: ``tests/contract/test_mcp_action_coverage.py`` is what keeps the two sides
#: of the wire agreeing about it.
MAX_BATCH_ITEMS: Final = 8

#: The floor ``movement.move_near`` applies to an approach radius. Restated
#: rather than imported because the adapter inlines it in its own reader instead
#: of naming it; a radius this schema waved through would be one the adapter
#: refuses with ``INVALID_ARGUMENT`` after the call has already been made.
MIN_APPROACH_RADIUS: Final = 0.1

#: The session every ``example`` below is minted under. A reference is
#: session-scoped, so an example has to name *some* session — naming one constant
#: is what lets ``tests/contract/test_mcp_action_coverage.py`` replay the
#: examples against the adapters that would receive them.
EXAMPLE_SESSION_ID: Final = "00000000-0000-4000-8000-000000000001"

_EXAMPLE_ITEM: Final = f"item:{EXAMPLE_SESSION_ID}:worn:Back:99001:4210:0"

#: A second carried item, distinct in its runtime id. The batch example must
#: show a list that is really a list, and two copies of one reference would be
#: exactly the duplicate its schema refuses.
_EXAMPLE_ITEM_2: Final = f"item:{EXAMPLE_SESSION_ID}:worn:Back:99001:4211:0"

_EXAMPLE_MAIN: Final = f"container:{EXAMPLE_SESSION_ID}:player-main"
_EXAMPLE_CRATE: Final = f"container:{EXAMPLE_SESSION_ID}:world:1200:3400:0:0:0"
_EXAMPLE_SQUARE: Final = f"square:{EXAMPLE_SESSION_ID}:1200:3400:0"

#: A door as the observer mints one: the square it stands on and its index in
#: that square's object list. The ``object`` kind carries no runtime id — a
#: door is furniture, not an entity — which is why the reference is positional.
_EXAMPLE_DOOR: Final = f"object:{EXAMPLE_SESSION_ID}:1200:3401:0:2"

#: A goal id the way :func:`~pz_agent_core.goals.mint_goal_id` spells one. Not
#: derived from :data:`EXAMPLE_SESSION_ID`: a goal id is minted by the channel
#: and is not session-scoped, and an example that reused the session's id would
#: teach a client the two are interchangeable.
_EXAMPLE_GOAL_ID: Final = "00000000-0000-4000-8000-0000000000a1"

#: An action id the way the sidecar's own ``mint_action_id`` spells one — a
#: UUID the process minted and handed back on submission. Distinct from the
#: goal id above for the same reason that one is distinct from the session id:
#: three different minters, three values a client must not conflate.
_EXAMPLE_ACTION_ID: Final = "00000000-0000-4000-8000-0000000000c1"

#: Filter tokens (categories, components, memory kinds) are identifiers, not
#: prose. A sentence in one of those positions is a red flag, so the pattern is
#: strict rather than merely length-bounded.
_IDENTIFIER_PATTERN: Final = r"^[a-z][a-z0-9_.\-]{0,63}$"

#: An item type as the game spells it — ``Base.Bandage``. The alphabet is the
#: one :mod:`~pz_agent_core.actions.adapters.inventory` accepts; a value with a
#: space, a colon or a wildcard is a pattern nothing implements rather than a
#: type that was not found.
_TYPE_FILTER_PATTERN: Final = rf"^[A-Za-z0-9._\-]{{1,{MAX_TYPE_FILTER_LEN}}}$"

#: A body location as the engine spells it — ``Torso``, ``Jacket``, ``Back``.
_SLOT_PATTERN: Final = rf"^[A-Za-z0-9._\-]{{1,{MAX_SLOT_NAME_LEN}}}$"

#: The hands an unequip may name: :data:`~pz_agent_core.actions.adapters.equipment.HANDS`
#: without ``both``, because a hand is emptied one at a time and "take off both"
#: is two commands with two results. Derived rather than typed out so the two
#: cannot drift into disagreeing about what a hand is called.
_UNEQUIP_HANDS: Final[tuple[str, ...]] = tuple(sorted(HANDS - {"both"}))

_REF_PATTERN: Final = r"^{head}:[A-Za-z0-9:_.\-]{{1,200}}$"

#: An identifier this process minted and handed to the caller: a command id, a
#: goal id. Named once because it is the same grammar in every position — three
#: copies of it would be three chances for one of them to lose a group and start
#: admitting free text into a field that is meant to be echoed back.
_UUID_PATTERN: Final = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_UUID_CHARS: Final = 36


def _ref_schema(kind: RefKind | tuple[RefKind, ...], description: str) -> JsonDict:
    """A reference argument. Shape only — the adapter decides whether it resolves.

    Session ownership is checked in
    :func:`pz_agent_core.actions.adapters.common.read_ref`, which is where
    ``INVALID_REF`` for a reference minted by another session is decided once
    for every adapter.

    *kind* takes a tuple for the same reason ``read_ref`` does: the mod names a
    nearby thing as a ``container`` reference when it holds one and as a
    ``square`` reference otherwise, so an argument meaning "something to walk up
    to" that insisted on one kind would refuse most of what the observer
    actually reports.
    """
    kinds = (kind,) if isinstance(kind, RefKind) else kind
    head = kinds[0].value if len(kinds) == 1 else f"(?:{'|'.join(k.value for k in kinds)})"
    return {
        "type": "string",
        "description": description,
        "pattern": _REF_PATTERN.format(head=head),
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
    """An input schema for a tool that submits a command.

    Every one of them carries an idempotency key, and nothing else free-form:
    there is deliberately no field for prose on a write path.

    The three read-only actions take the same envelope, which is not an
    oversight: a look is still a command with a lease, it still comes back as an
    action id, and replaying its key is how a client polls it. What makes them
    read-only is what the character does — nothing — not whether they queue
    anything.

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


#: The goal channel checks a caller's key against its own shape and its own
#: length — :data:`~pz_agent_core.goals.MAX_IDEMPOTENCY_KEY_LEN`, half of what
#: :data:`_IDEMPOTENCY_KEY` allows — and refuses rather than sanitises, because
#: folding two distinct keys into one would silently merge two goals. So the
#: goal tools do not reuse the command envelope's key: a key this schema waved
#: through would be one the channel refuses *after* the call was made, which is
#: exactly the drift the bounds in this module are imported to prevent. The
#: alphabet is restated because the channel states it as a compiled private
#: pattern rather than as a constant; ``tests/unit/test_mcp_catalog_goals.py``
#: holds the two together by feeding one's rejects to the other.
_GOAL_KEY_PATTERN: Final = rf"^[A-Za-z0-9][A-Za-z0-9_.:\-]{{0,{MAX_IDEMPOTENCY_KEY_LEN - 1}}}$"

_GOAL_KEY: Final[JsonDict] = {
    "type": "string",
    "description": (
        "Caller-chosen key. Submitting again with the same key returns the goal "
        "that key created instead of starting a second one."
    ),
    "minLength": 1,
    "maxLength": MAX_IDEMPOTENCY_KEY_LEN,
    "pattern": _GOAL_KEY_PATTERN,
}

_GOAL_ID: Final[JsonDict] = {
    "type": "string",
    "description": "A goal id the channel minted and handed back on submission.",
    "pattern": _UUID_PATTERN,
    "maxLength": _UUID_CHARS,
}

_ACTION_ID: Final[JsonDict] = {
    "type": "string",
    "description": "An action id this sidecar minted and handed back on submission.",
    "pattern": _UUID_PATTERN,
    "maxLength": _UUID_CHARS,
}


def _goal_channel(properties: Mapping[str, JsonDict], *, required: Iterable[str] = ()) -> JsonDict:
    """An input schema for a tool that speaks to the goal channel.

    Not :func:`_mutating`, for two reasons that are both about advertising
    something the receiver would refuse. The key is the channel's own — see
    :data:`_GOAL_KEY`. And there is no ``timeout_ms``: a goal is bounded by its
    :class:`~pz_agent_core.goals.GoalBudget`, which is wall clock *and* step
    count *and* a pending time to live, none of which is a command lease.
    Publishing a lease here would offer an argument no handler reads.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {**properties, "idempotency_key": _GOAL_KEY},
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

    #: Observation and diagnostics, answered from state the sidecar already
    #: holds. Permitted in ``OBSERVE``; never gated.
    READ = "read"
    #: Asks the *game* to describe something: one of the protocol's
    #: :data:`~pz_agent_core.protocol.READ_ONLY_ACTIONS`, so it comes back with
    #: an action id and its own evidence, and still needs no arming because the
    #: character neither moves nor touches anything.
    QUERY = "query"
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
    example: JsonDict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.long_running and self.action is None:
            raise ValueError(f"{self.name}: a long-running tool must name the action it submits")
        if self.kind is ToolKind.READ and self.action is not None:
            raise ValueError(f"{self.name}: a read tool must not submit an action")
        if self.kind is ToolKind.QUERY and self.action not in READ_ONLY_ACTIONS:
            # The one place a mistake here would be silent and expensive: a
            # query tool is exempt from the arming gate, so an action that moves
            # the character wearing this kind would move it on a disarmed
            # session. The protocol's own list is the authority, not the name.
            raise ValueError(
                f"{self.name}: a query tool must submit a read-only action, not {self.action}"
            )
        try:
            validate_arguments(self.input_schema, self.example)
        except Exception as rejected:
            # Reported as a construction error rather than relayed: a refused
            # example is this module's bug, and a ToolFailure escaping an import
            # would read like a caller's argument was rejected.
            raise ValueError(f"{self.name}: the example its own schema rejects: {rejected}") from (
                rejected
            )

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
        # ASSISTED rather than AUTONOMOUS: an example is the shape a client
        # copies first, and the first arming should be the one that acts only
        # when asked.
        example={"mode": "ASSISTED"},
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
    # --- looking, through the character -----------------------------------
    # These three submit a command and change nothing. They are the protocol's
    # READ_ONLY_ACTIONS, so they run in OBSERVE and on a disarmed session, and
    # none of them names a capability: what each needs is an observation tier
    # the mod either produced or did not, and every probe resolves a *Lua*
    # symbol, so a probe over the Java accessors behind a look would report
    # 'unsupported' on a perfectly healthy install.
    ToolSpec(
        name="pz_action_inspect_world",
        kind=ToolKind.QUERY,
        risk=RiskClass.P0,
        summary=(
            "Describe the block of squares around a centre, with what the mod "
            "makes of each one. Omit 'ref' to look around the character. Nothing "
            "moves: an inspect that walked round a corner to see better would be "
            "a mutating command wearing a read-only command's permissions."
        ),
        action=ActionName.WORLD_INSPECT,
        long_running=True,
        input_schema=_mutating(
            {
                "ref": _ref_schema(
                    RefKind.SQUARE,
                    "Centre of the block; omit for the square the character stands on.",
                ),
                "radius": {
                    "type": "integer",
                    "description": "Squares out from the centre; 2 is a five-by-five block.",
                    "minimum": 0,
                    "maximum": MAX_INSPECT_RADIUS,
                    "default": DEFAULT_INSPECT_RADIUS,
                },
            },
            required=(),
        ),
        example={"radius": 1, "idempotency_key": "goal-1:look:attempt-1"},
    ),
    ToolSpec(
        name="pz_action_inspect_container",
        kind=ToolKind.QUERY,
        risk=RiskClass.P0,
        summary=(
            "List what one container holds, with the real total beside the "
            "bounded listing. Reads the engine's own item list; no UI is driven "
            "and nothing is opened, so the character does not move."
        ),
        action=ActionName.CONTAINER_INSPECT,
        long_running=True,
        input_schema=_mutating(
            {
                "container_ref": _ref_schema(RefKind.CONTAINER, "The container to read."),
                "limit": {
                    "type": "integer",
                    "description": "Most items to list; the untruncated count is reported too.",
                    "minimum": 1,
                    "maximum": MAX_LISTED_ITEMS,
                    "default": MAX_LISTED_ITEMS,
                },
            },
            required=("container_ref",),
        ),
        example={
            "container_ref": _EXAMPLE_MAIN,
            "idempotency_key": "goal-1:look:attempt-1",
        },
    ),
    ToolSpec(
        name="pz_action_search_inventory",
        kind=ToolKind.QUERY,
        risk=RiskClass.P0,
        summary=(
            "List what the character is carrying that matches a filter. Every "
            "reference returned resolves inside the character's own containers, "
            "so a result is something the next step can act on without walking "
            "anywhere. It reports what matches; it never picks one."
        ),
        action=ActionName.INVENTORY_SEARCH,
        long_running=True,
        input_schema=_mutating(
            {
                "full_type": {
                    "type": "string",
                    "description": "Keep only items of exactly this game type.",
                    "pattern": _TYPE_FILTER_PATTERN,
                    "maxLength": MAX_TYPE_FILTER_LEN,
                },
                "type_prefix": {
                    "type": "string",
                    "description": "Keep only items whose game type starts with this.",
                    "pattern": _TYPE_FILTER_PATTERN,
                    "maxLength": MAX_TYPE_FILTER_LEN,
                },
                # No default on the three tristates, deliberately. Absent means
                # "do not filter on this"; false means "must not be edible", and
                # a default of false would silently narrow every search that
                # left one out.
                "edible": {
                    "type": "boolean",
                    "description": "Keep only items the game would let the character eat.",
                },
                "drinkable": {
                    "type": "boolean",
                    "description": "Keep only items the game would let the character drink.",
                },
                "readable": {
                    "type": "boolean",
                    "description": "Keep only literature.",
                },
                "exclude_equipped": {
                    "type": "boolean",
                    "description": "Drop what the character is holding or wearing.",
                    "default": False,
                },
                "limit": {
                    "type": "integer",
                    "description": "Most references to return.",
                    "minimum": 1,
                    "maximum": MAX_SEARCH_RESULTS,
                    "default": MAX_SEARCH_RESULTS,
                },
            },
            required=(),
        ),
        example={"edible": True, "limit": 8, "idempotency_key": "goal-1:search:attempt-1"},
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
        example={
            "target": {"x": 1200, "y": 3400, "z": 0},
            "idempotency_key": "goal-1:step-1:attempt-1",
        },
    ),
    ToolSpec(
        name="pz_action_move_near",
        kind=ToolKind.WRITE,
        risk=RiskClass.P3,
        summary=(
            "Walk to within interaction range of something in the world. "
            "Verified against the object's *re-observed* position, not the one "
            "it had when the call was made: an object that is no longer in view "
            "cannot be proven to be within arm's reach."
        ),
        required_capability=MOVE_TO_SQUARE,
        action=ActionName.MOVEMENT_MOVE_NEAR,
        long_running=True,
        input_schema=_mutating(
            {
                "object_ref": _ref_schema(
                    (RefKind.CONTAINER, RefKind.SQUARE, RefKind.ITEM),
                    "What to walk up to, as the observation named it.",
                ),
                "radius": {
                    "type": "number",
                    "description": "How close counts as within reach, in squares.",
                    "minimum": MIN_APPROACH_RADIUS,
                    "maximum": MAX_ARRIVAL_RADIUS,
                    "default": DEFAULT_NEAR_RADIUS,
                },
                "max_distance": {
                    "type": "integer",
                    "description": "Refuse if the object is further than this.",
                    "minimum": 1,
                    "maximum": MAX_MOVE_DISTANCE_SQUARES,
                    "default": DEFAULT_MOVE_DISTANCE_SQUARES,
                },
                "allow_doors": {
                    "type": "boolean",
                    "description": "May open doors on the way.",
                    "default": True,
                },
            },
            required=("object_ref",),
        ),
        example={"object_ref": _EXAMPLE_CRATE, "idempotency_key": "goal-1:step-1:attempt-1"},
    ),
    ToolSpec(
        name="pz_action_open_container",
        kind=ToolKind.WRITE,
        risk=RiskClass.P3,
        summary=(
            "Get within reach of a world container, so its contents can be "
            "taken. Its name reads like a query and it is not one: this walks "
            "the character across a room, so it needs an armed session like any "
            "other move. A door in the way is not opened — that is a different "
            "object and a different action."
        ),
        required_capability=MOVE_TO_SQUARE,
        action=ActionName.CONTAINER_OPEN_NEARBY,
        long_running=True,
        input_schema=_mutating(
            {
                "container_ref": _ref_schema(
                    RefKind.CONTAINER, "The world container to stand next to."
                ),
                "radius": {
                    "type": "number",
                    "description": "How close counts as within reach, in squares.",
                    "minimum": MIN_OPEN_RADIUS,
                    "maximum": MAX_ARRIVAL_RADIUS,
                    "default": DEFAULT_OPEN_RADIUS,
                },
            },
            required=("container_ref",),
        ),
        example={"container_ref": _EXAMPLE_CRATE, "idempotency_key": "goal-1:step-1:attempt-1"},
    ),
    # --- doors -------------------------------------------------------------
    # Three tools rather than one with a mode, because the three fail
    # differently and a planner replans them differently: a merely-closed door
    # is not an error at all, a locked one needs a key hunt (DOOR_LOCKED), a
    # barricaded one needs the planks off or a detour (DOOR_BARRICADED). All
    # three ride the one `door_toggle` capability and are long-running for the
    # same reason the container open is — a walk may precede the toggle.
    ToolSpec(
        name="pz_action_open_door",
        kind=ToolKind.WRITE,
        risk=RiskClass.P3,
        summary=(
            "Open a named door. Verified by the following observation "
            "describing it open; a door already open comes back as an "
            "unchanged success, not an error. A door observed locked is "
            "refused with DOOR_LOCKED — it needs its key (pz_action_unlock_door) "
            "before it will open — and one observed barricaded with "
            "DOOR_BARRICADED, which no toggle fixes."
        ),
        required_capability=DOOR_TOGGLE,
        action=ActionName.DOOR_OPEN,
        long_running=True,
        input_schema=_mutating(
            {
                "door_ref": _ref_schema(
                    RefKind.OBJECT,
                    "The door, as pz_observe_nearby reported it: an object "
                    "reference naming the square it stands on and its index in "
                    "that square's object list.",
                ),
                "radius": {
                    "type": "number",
                    "description": "How close counts as within reach of the door, in squares.",
                    "minimum": MIN_DOOR_RADIUS,
                    "maximum": MAX_ARRIVAL_RADIUS,
                    "default": DEFAULT_DOOR_RADIUS,
                },
            },
            required=("door_ref",),
        ),
        example={"door_ref": _EXAMPLE_DOOR, "idempotency_key": "goal-1:step-1:attempt-1"},
    ),
    ToolSpec(
        name="pz_action_close_door",
        kind=ToolKind.WRITE,
        risk=RiskClass.P3,
        summary=(
            "Close a named door. Verified by the following observation "
            "describing it closed. A lock never blocks this — a lock holds a "
            "door closed — so the one state refusal is DOOR_BARRICADED, and a "
            "door already closed comes back as an unchanged success."
        ),
        required_capability=DOOR_TOGGLE,
        action=ActionName.DOOR_CLOSE,
        long_running=True,
        input_schema=_mutating(
            {
                "door_ref": _ref_schema(
                    RefKind.OBJECT,
                    "The door, as pz_observe_nearby reported it: an object "
                    "reference naming the square it stands on and its index in "
                    "that square's object list.",
                ),
                "radius": {
                    "type": "number",
                    "description": "How close counts as within reach of the door, in squares.",
                    "minimum": MIN_DOOR_RADIUS,
                    "maximum": MAX_ARRIVAL_RADIUS,
                    "default": DEFAULT_DOOR_RADIUS,
                },
            },
            required=("door_ref",),
        ),
        example={"door_ref": _EXAMPLE_DOOR, "idempotency_key": "goal-1:step-1:attempt-1"},
    ),
    ToolSpec(
        name="pz_action_unlock_door",
        kind=ToolKind.WRITE,
        risk=RiskClass.P3,
        summary=(
            "Unlock a named door. A matching key must be observably usable — "
            "the mod checks the character's own key ring against the engine's "
            "key ids — and a locked door with no such key aboard answers "
            "DOOR_LOCKED: a key hunt, not a retry. A barricaded door answers "
            "DOOR_BARRICADED, which is a detour. Verified by the following "
            "observation reporting the lock off; a door already unlocked is "
            "an unchanged success."
        ),
        required_capability=DOOR_TOGGLE,
        action=ActionName.DOOR_UNLOCK,
        long_running=True,
        input_schema=_mutating(
            {
                "door_ref": _ref_schema(
                    RefKind.OBJECT,
                    "The door, as pz_observe_nearby reported it: an object "
                    "reference naming the square it stands on and its index in "
                    "that square's object list.",
                ),
                "radius": {
                    "type": "number",
                    "description": "How close counts as within reach of the door, in squares.",
                    "minimum": MIN_DOOR_RADIUS,
                    "maximum": MAX_ARRIVAL_RADIUS,
                    "default": DEFAULT_DOOR_RADIUS,
                },
            },
            required=("door_ref",),
        ),
        example={"door_ref": _EXAMPLE_DOOR, "idempotency_key": "goal-1:step-1:attempt-1"},
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
        example={
            "item_ref": _EXAMPLE_ITEM,
            "destination_container_ref": _EXAMPLE_MAIN,
            "idempotency_key": "goal-1:step-1:attempt-1",
        },
    ),
    ToolSpec(
        name="pz_action_transfer_batch",
        kind=ToolKind.WRITE,
        risk=RiskClass.P1,
        summary=(
            "Move up to eight named items into one container, each by the "
            "game's own transfer, with capacity re-checked before every item "
            "and the batch stopped at the first that would not fit. Succeeded "
            "only when every requested item is observed in the destination "
            "afterwards; a stop partway is a CONTAINER_FULL failure whose "
            "evidence carries the honest partial record — what landed, what "
            "stopped, and why. Each reference moves as one item, exactly as "
            "pz_action_transfer moves it."
        ),
        required_capability=INVENTORY_TRANSFER,
        action=ActionName.INVENTORY_TRANSFER_BATCH,
        long_running=True,
        input_schema=_mutating(
            {
                "item_refs": {
                    "type": "array",
                    "description": (
                        "The items to move, each named once; they may live in "
                        "different source containers."
                    ),
                    "minItems": 1,
                    "maxItems": MAX_BATCH_ITEMS,
                    "uniqueItems": True,
                    "items": _ref_schema(RefKind.ITEM, "One item to move."),
                },
                "destination_container_ref": _ref_schema(
                    RefKind.CONTAINER, "Where every one of them must end up."
                ),
            },
            required=("item_refs", "destination_container_ref"),
        ),
        example={
            "item_refs": [_EXAMPLE_ITEM, _EXAMPLE_ITEM_2],
            "destination_container_ref": _EXAMPLE_MAIN,
            "idempotency_key": "goal-1:step-1:attempt-1",
        },
    ),
    ToolSpec(
        name="pz_action_ensure_main",
        kind=ToolKind.WRITE,
        risk=RiskClass.P1,
        summary=(
            "Bring one item into the main inventory. This is the preparation "
            "step eating, drinking, reading, equipping and bandaging all "
            "require, and it is its own action with its own evidence rather "
            "than something those adapters do on the side."
        ),
        required_capability=INVENTORY_TRANSFER,
        action=ActionName.INVENTORY_ENSURE_MAIN,
        long_running=True,
        input_schema=_mutating(
            # `destination_container_ref` is not published although the adapter
            # accepts it: the only value it accepts is the main inventory, which
            # its own build_args fills in, and any other container is refused as
            # "that is inventory.transfer". An argument with one legal value the
            # caller cannot name is an argument that only produces mistakes.
            {"item_ref": _ref_schema(RefKind.ITEM, "The item to bring to hand.")},
            required=("item_ref",),
        ),
        example={"item_ref": _EXAMPLE_ITEM, "idempotency_key": "goal-1:step-1:attempt-1"},
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
        example={
            "item_ref": _EXAMPLE_ITEM,
            "idempotency_key": "goal-1:step-2:attempt-1",
        },
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
        example={
            "item_ref": _EXAMPLE_ITEM,
            "idempotency_key": "goal-1:step-2:attempt-1",
        },
    ),
    ToolSpec(
        name="pz_action_drink_source",
        kind=ToolKind.WRITE,
        risk=RiskClass.P2,
        summary=(
            "Fill a carried vessel at a sink, well or rain collector and drink from "
            "it. Verified by thirst falling; the vessel's own volume proves nothing "
            "here, because the fill raises it and the drink lowers it again."
        ),
        required_capability=DRINK_WORLD_SOURCE,
        action=ActionName.CONSUME_DRINK_SOURCE,
        long_running=True,
        input_schema=_mutating(
            {
                "item_ref": _ref_schema(RefKind.ITEM, "The vessel to fill and drink from."),
                "fraction": {
                    "type": "number",
                    "description": "How much of it to consume once filled.",
                    "minimum": MIN_CONSUME_FRACTION,
                    "maximum": 1.0,
                    "default": 1.0,
                },
                "source_ref": _ref_schema(
                    RefKind.SQUARE,
                    "The square the water source stands on. It must be reported in "
                    "the observation with the 'water_source' semantic.",
                ),
            },
            required=("item_ref", "source_ref"),
        ),
        example={
            "item_ref": _EXAMPLE_ITEM,
            "source_ref": _EXAMPLE_SQUARE,
            "idempotency_key": "goal-1:step-2:attempt-1",
        },
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
        example={
            "item_ref": _EXAMPLE_ITEM,
            "idempotency_key": "goal-1:step-1:attempt-1",
        },
    ),
    ToolSpec(
        name="pz_action_equip",
        kind=ToolKind.WRITE,
        risk=RiskClass.P2,
        summary=(
            "Put one item in a hand or on the body. Omit 'hand' for anything "
            "the character wears: the item's own body location is what decides "
            "between a hand and a slot, and naming a hand for a garment would "
            "refuse every garment. Verified by the requested slot holding it."
        ),
        required_capability=EQUIPMENT_EQUIP,
        action=ActionName.EQUIPMENT_EQUIP,
        long_running=True,
        input_schema=_mutating(
            {
                "item_ref": _ref_schema(RefKind.ITEM, "The item to equip."),
                "hand": {
                    "type": "string",
                    "description": "Which hand to fill; 'both' is a two-handed grip.",
                    "enum": sorted(HANDS),
                },
            },
            required=("item_ref",),
        ),
        example={"item_ref": _EXAMPLE_ITEM, "idempotency_key": "goal-1:step-1:attempt-1"},
    ),
    ToolSpec(
        name="pz_action_unequip",
        kind=ToolKind.WRITE,
        risk=RiskClass.P2,
        summary=(
            "Take one item off and keep it. Name it exactly one way — by item, "
            "by hand or by slot — because the three can disagree and there is no "
            "defensible rule for which would win. Verified by no slot holding it "
            "*and* it still being on the character: an item that left the hand "
            "and the inventory was dropped, not unequipped."
        ),
        required_capability=EQUIPMENT_UNEQUIP,
        action=ActionName.EQUIPMENT_UNEQUIP,
        long_running=True,
        input_schema=_mutating(
            # "Exactly one of three" is not expressible in the schema subset this
            # boundary validates, so it is not half-stated here: the adapter
            # refuses both none and two, and that is the single place the rule
            # lives. What the schema does state is that there is nothing else.
            {
                "item_ref": _ref_schema(RefKind.ITEM, "The item to take off."),
                "hand": {
                    "type": "string",
                    "description": "Empty this hand; one hand per command.",
                    "enum": list(_UNEQUIP_HANDS),
                },
                "slot": {
                    "type": "string",
                    "description": "Empty this body location, as the engine spells it.",
                    "pattern": _SLOT_PATTERN,
                    "maxLength": MAX_SLOT_NAME_LEN,
                },
            },
            required=(),
        ),
        example={"hand": "primary", "idempotency_key": "goal-1:step-1:attempt-1"},
    ),
    ToolSpec(
        name="pz_action_bandage",
        kind=ToolKind.WRITE,
        risk=RiskClass.P2,
        summary=(
            "Dress one bleeding wound with one carried dressing. Verified by the "
            "named body part no longer being reported as bleeding — never by the "
            "dressing leaving the inventory, which is equally true of one that "
            "was dropped. A part that is not bleeding is refused rather than "
            "attempted: the observation carries no dressing state to check "
            "against. Which part and which dressing are core policy's decision."
        ),
        required_capability=MEDICAL_BANDAGE,
        action=ActionName.MEDICAL_BANDAGE,
        long_running=True,
        input_schema=_mutating(
            {
                "body_part": {
                    "type": "string",
                    "description": "The part to dress, as BodyPartType spells it.",
                    "enum": sorted(BODY_PARTS),
                },
                "item_ref": _ref_schema(RefKind.ITEM, "The dressing to use."),
            },
            required=("body_part", "item_ref"),
        ),
        example={
            "body_part": "ForeArm_L",
            "item_ref": _EXAMPLE_ITEM,
            "idempotency_key": "goal-1:step-1:attempt-1",
        },
    ),
    ToolSpec(
        name="pz_action_rest",
        kind=ToolKind.WRITE,
        risk=RiskClass.P2,
        summary=(
            "Recover endurance up to a target. Verified by the endurance reading "
            "rising to it — or, on a build that reports no endurance, by the "
            "character being observed sitting, but only if sitting is what was "
            "asked for. A standing rest with no readable stat has nothing to "
            "show for itself and times out."
        ),
        required_capability=SURVIVAL_REST,
        action=ActionName.SURVIVAL_REST,
        long_running=True,
        input_schema=_mutating(
            {
                "target_endurance": {
                    "type": "number",
                    "description": "Endurance to reach; the stat runs 0..1.",
                    "minimum": MIN_REST_TARGET,
                    "maximum": MAX_REST_TARGET,
                    "default": DEFAULT_REST_TARGET,
                },
                "seat_ref": _ref_schema(RefKind.SQUARE, "A square with something to sit on."),
                "allow_ground": {
                    "type": "boolean",
                    "description": "May sit on the ground when there is no seat.",
                    "default": False,
                },
                "max_wait_ms": {
                    "type": "integer",
                    "description": "How long the mod may hold the rest, in milliseconds.",
                    "minimum": MIN_WAIT_MS,
                    "maximum": MAX_REST_WAIT_MS,
                    "default": DEFAULT_REST_WAIT_MS,
                },
            },
            required=(),
        ),
        example={"target_endurance": 0.95, "idempotency_key": "goal-1:step-1:attempt-1"},
    ),
    ToolSpec(
        name="pz_action_sleep",
        kind=ToolKind.WRITE,
        risk=RiskClass.P4,
        summary=(
            "Sleep a night off in a named bed. The most consequential action in "
            "this build, and the reason it is P4: once the character is asleep "
            "the mod cannot wake them — sleep runs through the bed's context "
            "menu, so there is no timed action to interrupt and no queue entry "
            "to cancel, and a panic stop cannot reach it. It is refused outright "
            "while the guard reports any danger at all, and it is never taken on "
            "the agent's own initiative. Its capability is 'experimental' on a "
            "clean scan, so on most installs this tool is withheld rather than "
            "offered. Verified by fatigue falling *and* the world clock "
            "advancing; fatigue alone is a quiet afternoon."
        ),
        required_capability=SURVIVAL_SLEEP,
        action=ActionName.SURVIVAL_SLEEP,
        long_running=True,
        input_schema=_mutating(
            {
                "bed_ref": _ref_schema(RefKind.SQUARE, "The square the bed stands on."),
                "hours": {
                    "type": "integer",
                    "description": "In-game hours to sleep for.",
                    "minimum": MIN_SLEEP_HOURS,
                    "maximum": MAX_SLEEP_HOURS,
                    "default": DEFAULT_SLEEP_HOURS,
                },
                "allow_vehicle_seat": {
                    "type": "boolean",
                    "description": "Sleep in a vehicle seat when no bed is named.",
                    "default": False,
                },
                "max_wait_ms": {
                    "type": "integer",
                    "description": "How long the mod may hold the sleep, in milliseconds.",
                    "minimum": MIN_WAIT_MS,
                    "maximum": MAX_SLEEP_WAIT_MS,
                    "default": DEFAULT_SLEEP_WAIT_MS,
                },
            },
            required=(),
        ),
        example={
            "bed_ref": _EXAMPLE_SQUARE,
            "hours": 8,
            "idempotency_key": "goal-1:step-1:attempt-1",
        },
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
        example={"game_seconds": 30, "idempotency_key": "goal-1:step-3:attempt-1"},
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
                    "pattern": _UUID_PATTERN,
                    "maxLength": _UUID_CHARS,
                }
            },
            required=(),
        ),
        example={"idempotency_key": "goal-1:cancel:attempt-1"},
    ),
    ToolSpec(
        name="pz_action_cancel_all",
        kind=ToolKind.CONTROL,
        risk=RiskClass.P1,
        summary=(
            "Clear every mod-owned entry in one call: the mass form of "
            "pz_action_cancel, with nothing narrower to mis-aim. Ownership is "
            "the mod's own tag, so an action the player queued is never "
            "touched, and the postcondition is negative — no entry this "
            "session owns still in flight — so a second call finds it already "
            "true and succeeds clearing nothing. Returns the cancel's action "
            "id; pz_action_await turns it into the engine's verdict."
        ),
        input_schema=_mutating({}, required=()),
        example={"idempotency_key": "goal-1:cancel-all:attempt-1"},
    ),
    # --- asking after submitted work ---------------------------------------
    # Read tools, not queries: they answer from the record store the sidecar
    # already holds and submit nothing, so they run in OBSERVE, on a disarmed
    # session, and against a game that is gone. Until they existed the only
    # public read of an action's fate was replaying its idempotency key, and an
    # agent that had lost the key — or the sidecar that minted it — could ask
    # nobody; a live session sat watching an 'accepted' nothing could explain.
    ToolSpec(
        name="pz_action_status",
        kind=ToolKind.READ,
        risk=RiskClass.P0,
        summary=(
            "The current record of one submitted action: its status, its "
            "terminal result, and — for an observed success — its evidence. An "
            "id this sidecar does not hold is answered as known: false with "
            "the likely causes, not as an error: the record store is a bounded "
            "ring that evicts finished work, and a restarted sidecar holds "
            "nothing the previous process minted, so unknown here is a routine "
            "fact and never means the action did not run."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"action_id": _ACTION_ID},
            "required": ["action_id"],
        },
        example={"action_id": _EXAMPLE_ACTION_ID},
    ),
    ToolSpec(
        name="pz_action_await",
        kind=ToolKind.READ,
        risk=RiskClass.P0,
        summary=(
            "Wait, bounded, for a submitted action to reach a terminal state, "
            "re-reading its record on a small interval with no lock held "
            "across the wait — the stop tools stay reachable while it runs. "
            "Answers the pz_action_status shape plus waited_ms and timed_out; "
            "a budget that ends first reports the record as it stands with "
            "timed_out: true, and an unknown id answers immediately."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "action_id": {**_ACTION_ID, "description": "The action to wait on."},
                "timeout_ms": {
                    "type": "integer",
                    "description": (
                        "The wait budget for this one call, in milliseconds — "
                        "how long to keep re-reading, not the action's lease."
                    ),
                    "minimum": MIN_ACTION_WAIT_MS,
                    "maximum": MAX_ACTION_WAIT_MS,
                    "default": DEFAULT_ACTION_WAIT_MS,
                },
            },
            "required": ["action_id"],
        },
        example={"action_id": _EXAMPLE_ACTION_ID, "timeout_ms": 2000},
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
        example={"goal": "eat something safe", "idempotency_key": "goal-1:attempt-1"},
    ),
    ToolSpec(
        name="pz_plan_status",
        kind=ToolKind.READ,
        risk=RiskClass.P0,
        summary="Current step, the results so far, and why the plan stopped.",
        input_schema=_NO_ARGUMENTS,
    ),
    # --- goals ------------------------------------------------------------
    # The typed goal channel, published as three tools. What separates them
    # from `pz_plan_execute` is that nothing here is a sentence: a kind is a
    # member of a closed enum and every parameter is a number with a declared
    # range, so a caller that must not forward words — a voice loop, §7.11 —
    # has somewhere to put the intent instead of nowhere.
    ToolSpec(
        name="pz_goal_submit",
        kind=ToolKind.WRITE,
        risk=RiskClass.P2,
        summary=(
            "Ask the typed goal channel for one of the things it carries. The "
            "kind set is closed and there is no free-text field at all: an "
            "invented kind is refused, never approximated. The channel admits "
            "the goal to a bounded backlog and answers with its id and state — "
            "'pending' is the honest word for a goal nothing has started yet, "
            "and every goal carries a wall-clock, step and time-to-live budget "
            "so that it reaches a terminal state whether or not it is served. "
            "Which sandwich satisfies a hunger goal is never decided here."
        ),
        input_schema=_goal_channel(
            {
                "kind": {
                    "type": "string",
                    "description": "What to ask for.",
                    "enum": sorted(kind.value for kind in GoalKind),
                },
                "skill": {
                    "type": "string",
                    "description": "Which skill a 'train_skill' goal is for.",
                    "enum": sorted(skill.value for skill in TrainableSkill),
                },
                # No defaults on the three numbers, deliberately: which of them
                # a kind accepts is declared per kind in
                # `pz_agent_core.goals.GOAL_SPECS`, and a default here would
                # attach a parameter to a kind that refuses it — turning an
                # omission into an INVALID_ARGUMENT the caller never asked for.
                "target_level": {
                    "type": "integer",
                    "description": "Skill level a 'train_skill' goal reads towards.",
                    "minimum": NUMERIC_RANGES["target_level"].minimum,
                    "maximum": NUMERIC_RANGES["target_level"].maximum,
                },
                "satisfy_to": {
                    "type": "number",
                    "description": "How far to satisfy hunger or thirst; the stat runs 0..1.",
                    "minimum": NUMERIC_RANGES["satisfy_to"].minimum,
                    "maximum": NUMERIC_RANGES["satisfy_to"].maximum,
                },
                "pages": {
                    "type": "integer",
                    "description": "How many pages a reading goal may read.",
                    "minimum": NUMERIC_RANGES["pages"].minimum,
                    "maximum": NUMERIC_RANGES["pages"].maximum,
                },
                # The three below belong to 'navigate_to' and to nothing else:
                # the world square the deterministic route executor walks to.
                # Whole squares, exactly as movement.move_to takes them.
                "target_x": {
                    "type": "integer",
                    "description": "World square X a 'navigate_to' goal walks to.",
                    "minimum": NUMERIC_RANGES["target_x"].minimum,
                    "maximum": NUMERIC_RANGES["target_x"].maximum,
                },
                "target_y": {
                    "type": "integer",
                    "description": "World square Y a 'navigate_to' goal walks to.",
                    "minimum": NUMERIC_RANGES["target_y"].minimum,
                    "maximum": NUMERIC_RANGES["target_y"].maximum,
                },
                "target_z": {
                    "type": "integer",
                    "description": "Floor of the target square; 0 is ground level.",
                    "minimum": NUMERIC_RANGES["target_z"].minimum,
                    "maximum": NUMERIC_RANGES["target_z"].maximum,
                },
            },
            required=("kind",),
        ),
        example={"kind": "satisfy_hunger", "idempotency_key": "goal-1:attempt-1"},
    ),
    ToolSpec(
        name="pz_goal_status",
        kind=ToolKind.READ,
        risk=RiskClass.P0,
        summary=(
            "The goal channel: which goal is active, what is waiting behind it, "
            "and — when 'goal_id' names one — that goal's state, budget and how "
            "much of it is left. An id the channel has finished and forgotten is "
            "refused rather than answered as 'no such goal', because the two are "
            "not the same fact."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"goal_id": _GOAL_ID},
            "required": [],
        },
    ),
    ToolSpec(
        name="pz_goal_cancel",
        kind=ToolKind.CONTROL,
        risk=RiskClass.P1,
        summary=(
            "Ask for one goal to end. Control, not write: a goal is cancelled "
            "*because* something has gone wrong, and gating that on an armed "
            "session would make the channel unstoppable by the lever meant to "
            "stop it. The channel applies a cancellation on its next tick, so "
            "the answer reports the request and the goal's state as it stands "
            "and does not claim the goal is already over."
        ),
        input_schema=_goal_channel(
            {"goal_id": {**_GOAL_ID, "description": "The goal to end."}},
            required=("goal_id",),
        ),
        example={"goal_id": _EXAMPLE_GOAL_ID, "idempotency_key": "goal-1:cancel:attempt-1"},
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
                    "pattern": _UUID_PATTERN,
                    "maxLength": _UUID_CHARS,
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
