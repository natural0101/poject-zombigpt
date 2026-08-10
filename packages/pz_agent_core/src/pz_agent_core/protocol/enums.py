"""Closed vocabularies used across the wire protocol.

Every enum here is mirrored by an ``enum``/``const`` in ``schemas/`` — the
contract test ``tests/contract/test_schema_sync.py`` fails when the two drift.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class ActionName(StrEnum):
    """The complete set of commands the sidecar may send to the mod.

    The mod rejects anything outside this list before it ever reaches an
    adapter, so adding an action means touching the schema, the mod dispatch
    table and the capability probe together.
    """

    SESSION_ARM = "session.arm"
    SESSION_DISARM = "session.disarm"
    SAFETY_STOP = "safety.stop"
    ACTION_WAIT = "action.wait"
    MOVEMENT_MOVE_TO = "movement.move_to"
    MOVEMENT_MOVE_NEAR = "movement.move_near"
    WORLD_INSPECT = "world.inspect"
    CONTAINER_INSPECT = "container.inspect"
    CONTAINER_OPEN_NEARBY = "container.open_nearby"
    INVENTORY_SEARCH = "inventory.search"
    INVENTORY_TRANSFER = "inventory.transfer"
    INVENTORY_ENSURE_MAIN = "inventory.ensure_main"
    CONSUME_EAT = "consume.eat"
    CONSUME_DRINK = "consume.drink"
    CONSUME_DRINK_SOURCE = "consume.drink_source"
    LITERATURE_READ = "literature.read"
    EQUIPMENT_EQUIP = "equipment.equip"
    EQUIPMENT_UNEQUIP = "equipment.unequip"
    MEDICAL_BANDAGE = "medical.bandage"
    SURVIVAL_REST = "survival.rest"
    SURVIVAL_SLEEP = "survival.sleep"
    PLAN_CANCEL = "plan.cancel"
    DOOR_OPEN = "door.open"
    DOOR_CLOSE = "door.close"
    DOOR_UNLOCK = "door.unlock"


#: Actions that only read state. They are permitted in OBSERVE mode and do not
#: require the session to be armed.
#:
#: ``container.open_nearby`` is deliberately *not* here. Its name reads like a
#: query, but opening a container is a timed action the character performs in
#: the world, and an unarmed session must not move the character.
READ_ONLY_ACTIONS: frozenset[ActionName] = frozenset(
    {
        ActionName.WORLD_INSPECT,
        ActionName.CONTAINER_INSPECT,
        ActionName.INVENTORY_SEARCH,
        ActionName.ACTION_WAIT,
    }
)

#: Actions that bypass the arming check entirely — stopping must always work.
ALWAYS_ALLOWED_ACTIONS: frozenset[ActionName] = frozenset(
    {ActionName.SAFETY_STOP, ActionName.SESSION_DISARM, ActionName.PLAN_CANCEL}
)


class ActionStatus(StrEnum):
    """Lifecycle of a single command, as reported on the ack stream."""

    RECEIVED = "received"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    STARTED = "started"
    PROGRESS = "progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"

    @property
    def is_terminal(self) -> bool:
        """True when no further ack will arrive for this command."""
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES: frozenset[ActionStatus] = frozenset(
    {
        ActionStatus.SUCCEEDED,
        ActionStatus.FAILED,
        ActionStatus.CANCELLED,
        ActionStatus.REJECTED,
        ActionStatus.LOST,
    }
)


class SessionMode(StrEnum):
    """How much the agent is allowed to do.

    The ladder is strictly increasing in authority; ``REFLEX_ONLY`` sits apart
    as a safety-only mode where the deterministic guard may still cancel work
    but no planner step is ever issued.
    """

    OFF = "OFF"
    OBSERVE = "OBSERVE"
    ASSISTED = "ASSISTED"
    AUTONOMOUS = "AUTONOMOUS"
    REFLEX_ONLY = "REFLEX_ONLY"
    EXPERIMENTAL_INPUT = "EXPERIMENTAL_INPUT"


#: Modes in which the agent may issue mutating commands on its own initiative.
SELF_DIRECTED_MODES: frozenset[SessionMode] = frozenset({SessionMode.AUTONOMOUS})

#: Modes in which mutating commands are accepted at all (user-driven or not).
MUTATING_MODES: frozenset[SessionMode] = frozenset(
    {SessionMode.ASSISTED, SessionMode.AUTONOMOUS, SessionMode.EXPERIMENTAL_INPUT}
)


class DangerLevel(StrEnum):
    """Coarse threat assessment produced by the deterministic reflex guard."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        """Ordinal for comparisons; higher means more dangerous."""
        return _DANGER_RANK[self]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, DangerLevel):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other: object) -> bool:
        if not isinstance(other, DangerLevel):
            return NotImplemented
        return self.rank <= other.rank

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, DangerLevel):
            return NotImplemented
        return self.rank > other.rank

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, DangerLevel):
            return NotImplemented
        return self.rank >= other.rank


_DANGER_RANK: dict[DangerLevel, int] = {
    DangerLevel.NONE: 0,
    DangerLevel.LOW: 1,
    DangerLevel.MEDIUM: 2,
    DangerLevel.HIGH: 3,
    DangerLevel.CRITICAL: 4,
}


class ActionOwnership(StrEnum):
    """Who queued the timed action the character is currently running.

    ``ambiguous`` is reported when the mod sees a queue it did not fully author
    — the guard treats it exactly like ``manual`` and refuses to clear it.
    """

    NONE = "none"
    MOD = "mod"
    MANUAL = "manual"
    AMBIGUOUS = "ambiguous"


class ContainerKind(StrEnum):
    """Where a container lives relative to the player."""

    PLAYER_MAIN = "player_main"
    CARRIED = "carried"
    WORN = "worn"
    WORLD = "world"
    VEHICLE = "vehicle"
    FLOOR = "floor"


#: Containers whose contents move with the player and need no travel to reach.
ON_PERSON_CONTAINERS: frozenset[ContainerKind] = frozenset(
    {ContainerKind.PLAYER_MAIN, ContainerKind.CARRIED, ContainerKind.WORN}
)


class CapabilityState(StrEnum):
    """Result of a runtime probe for one game API.

    ``verified`` is reserved for capabilities whose probe actually ran against
    the live game — never assumed from a version number.
    """

    VERIFIED = "verified"
    AVAILABLE_UNVERIFIED = "available_unverified"
    EXPERIMENTAL = "experimental"
    UNSUPPORTED = "unsupported"
    DISABLED_BY_POLICY = "disabled_by_policy"

    @property
    def usable(self) -> bool:
        """True when a write tool backed by this capability may be published."""
        return self in {CapabilityState.VERIFIED, CapabilityState.AVAILABLE_UNVERIFIED}


class RiskClass(StrEnum):
    """Permission tier a command needs, from harmless to never-automatic."""

    P0 = "P0"  # read-only observation
    P1 = "P1"  # reversible, on-person only (transfer within inventory)
    P2 = "P2"  # consumes a resource (eat, drink) or moves the character
    P3 = "P3"  # touches the world (containers, doors) or leaves the safe radius
    P4 = "P4"  # never taken autonomously; requires explicit per-call consent

    @property
    def rank(self) -> int:
        return int(self.value[1])


class Priority(IntEnum):
    """Interrupt hierarchy from the master prompt (§ "Иерархия приоритетов").

    Lower number wins. The policy engine compares these to decide whether an
    incoming need may suspend the plan that is currently running.
    """

    PANIC_STOP = 1
    MANUAL_INPUT = 2
    LETHAL_THREAT = 3
    CRITICAL_BLEEDING = 4
    HAZARD_TILE = 5
    CRITICAL_THIRST = 6
    CRITICAL_HUNGER = 7
    EXHAUSTION = 8
    USER_COMMAND = 9
    LONG_TERM_TASK = 10
    BASE_MAINTENANCE = 11
    OPTIONAL_ACTIVITY = 12
