"""Wire messages: commands, acks and observations.

These dataclasses are the in-process representation of the JSON documents in
``schemas/``. ``to_dict`` emits exactly what the schema accepts — optional
fields are omitted rather than emitted as ``null``, because every object in the
observation schema is ``additionalProperties: false`` and several of them do
not allow a null.

Parsing is deliberately strict and total: ``from_dict`` raises
:class:`ProtocolError` on anything it does not recognise, so a malformed line
from the mod fails loudly at the boundary instead of propagating a half-built
object into the action engine.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeVar

from ..version import PROTOCOL_VERSION, SCHEMA_VERSION
from .enums import (
    ActionName,
    ActionOwnership,
    ActionStatus,
    ContainerKind,
    DangerLevel,
    RiskClass,
    SessionMode,
)
from .reason_codes import ReasonCode

JsonDict = dict[str, Any]

_E = TypeVar("_E", bound=Enum)

#: Bounds copied from ``schemas/command.schema.json`` so validation and
#: serialisation cannot disagree about them.
MIN_LEASE_MS = 100
MAX_LEASE_MS = 300_000
MAX_IDEMPOTENCY_KEY_LEN = 128
MAX_SAVE_ID_LEN = 128
MAX_RETRIES = 3


class ProtocolError(ValueError):
    """A payload does not conform to the protocol."""


# --------------------------------------------------------------------------
# small helpers — strict readers with useful messages
# --------------------------------------------------------------------------


def _require(payload: Mapping[str, Any], key: str) -> Any:
    if key not in payload:
        raise ProtocolError(f"missing required field {key!r}")
    return payload[key]


def _as_str(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"{field_name} must be a string, got {type(value).__name__}")
    return value


def _as_int(value: Any, *, field_name: str) -> int:
    # bool is an int subclass in Python; the wire format never means that here.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{field_name} must be an integer, got {type(value).__name__}")
    return value


def _as_float(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"{field_name} must be a number, got {type(value).__name__}")
    return float(value)


def _as_bool(value: Any, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ProtocolError(f"{field_name} must be a boolean, got {type(value).__name__}")
    return value


def _as_optional_bool(value: Any, *, field_name: str) -> bool | None:
    if value is None:
        return None
    return _as_bool(value, field_name=field_name)


def _as_mapping(value: Any, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{field_name} must be an object, got {type(value).__name__}")
    return value


def _as_sequence(value: Any, *, field_name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ProtocolError(f"{field_name} must be an array, got {type(value).__name__}")
    return value


def _as_enum(enum_cls: type[_E], value: Any, *, field_name: str) -> _E:
    raw = _as_str(value, field_name=field_name)
    try:
        return enum_cls(raw)
    except ValueError as exc:
        raise ProtocolError(f"{field_name}: unknown value {raw!r}") from exc


def _as_uuid_str(value: Any, *, field_name: str) -> str:
    raw = _as_str(value, field_name=field_name)
    try:
        uuid.UUID(raw)
    except ValueError as exc:
        raise ProtocolError(f"{field_name} must be a UUID, got {raw!r}") from exc
    return raw


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandPolicy:
    """Per-command execution knobs the sidecar attaches to a request."""

    allow_interrupt: bool = True
    max_retries: int = 1
    risk_permission: RiskClass | None = None

    def to_dict(self) -> JsonDict:
        out: JsonDict = {
            "allow_interrupt": self.allow_interrupt,
            "max_retries": self.max_retries,
        }
        if self.risk_permission is not None:
            out["risk_permission"] = self.risk_permission.value
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CommandPolicy:
        retries = payload.get("max_retries", 1)
        parsed_retries = _as_int(retries, field_name="policy.max_retries")
        if not 0 <= parsed_retries <= MAX_RETRIES:
            raise ProtocolError(
                f"policy.max_retries must be within 0..{MAX_RETRIES}, got {parsed_retries}"
            )
        risk = payload.get("risk_permission")
        return cls(
            allow_interrupt=_as_bool(
                payload.get("allow_interrupt", True), field_name="policy.allow_interrupt"
            ),
            max_retries=parsed_retries,
            risk_permission=(
                None
                if risk is None
                else _as_enum(RiskClass, risk, field_name="policy.risk_permission")
            ),
        )


@dataclass(frozen=True, slots=True)
class Command:
    """One request from the sidecar to the mod.

    ``idempotency_key`` is what makes a redelivered command safe: the mod keeps
    the terminal result of every key it has completed within the session and
    replays that result rather than performing the action a second time.
    """

    session_id: str
    seq: int
    command_id: str
    idempotency_key: str
    issued_at_ms: int
    lease_ms: int
    action: ActionName
    args: JsonDict = field(default_factory=dict)
    expected_observation_seq: int | None = None
    policy: CommandPolicy | None = None
    protocol_version: str = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not MIN_LEASE_MS <= self.lease_ms <= MAX_LEASE_MS:
            raise ProtocolError(
                f"lease_ms must be within {MIN_LEASE_MS}..{MAX_LEASE_MS}, got {self.lease_ms}"
            )
        if not self.idempotency_key or len(self.idempotency_key) > MAX_IDEMPOTENCY_KEY_LEN:
            raise ProtocolError("idempotency_key must be 1..128 characters")
        if self.seq < 0:
            raise ProtocolError(f"seq must be non-negative, got {self.seq}")
        if self.issued_at_ms < 0:
            raise ProtocolError(f"issued_at_ms must be non-negative, got {self.issued_at_ms}")

    def to_dict(self) -> JsonDict:
        out: JsonDict = {
            "protocol_version": self.protocol_version,
            "session_id": self.session_id,
            "seq": self.seq,
            "command_id": self.command_id,
            "idempotency_key": self.idempotency_key,
            "issued_at_ms": self.issued_at_ms,
            "lease_ms": self.lease_ms,
            "action": self.action.value,
            "args": dict(self.args),
        }
        if self.expected_observation_seq is not None:
            out["expected_observation_seq"] = self.expected_observation_seq
        if self.policy is not None:
            out["policy"] = self.policy.to_dict()
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Command:
        version = _as_str(_require(payload, "protocol_version"), field_name="protocol_version")
        expected = payload.get("expected_observation_seq")
        policy = payload.get("policy")
        return cls(
            protocol_version=version,
            session_id=_as_uuid_str(_require(payload, "session_id"), field_name="session_id"),
            seq=_as_int(_require(payload, "seq"), field_name="seq"),
            command_id=_as_uuid_str(_require(payload, "command_id"), field_name="command_id"),
            idempotency_key=_as_str(
                _require(payload, "idempotency_key"), field_name="idempotency_key"
            ),
            issued_at_ms=_as_int(_require(payload, "issued_at_ms"), field_name="issued_at_ms"),
            lease_ms=_as_int(_require(payload, "lease_ms"), field_name="lease_ms"),
            action=_as_enum(ActionName, _require(payload, "action"), field_name="action"),
            args=dict(_as_mapping(_require(payload, "args"), field_name="args")),
            expected_observation_seq=(
                None
                if expected is None
                else _as_int(expected, field_name="expected_observation_seq")
            ),
            policy=(
                None
                if policy is None
                else CommandPolicy.from_dict(_as_mapping(policy, field_name="policy"))
            ),
        )

    def deadline_ms(self) -> int:
        """Absolute wall-clock deadline after which the mod must reject this."""
        return self.issued_at_ms + self.lease_ms

    def is_expired(self, now_ms: int) -> bool:
        """True once the lease has run out.

        Expiry is checked on receipt *and* immediately before execution: a
        command that sat in the queue behind a long timed action must not fire
        against a world that has moved on.
        """
        return now_ms > self.deadline_ms()


# --------------------------------------------------------------------------
# action results
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActionResult:
    """One ack for a command.

    ``status = succeeded`` is a claim about the world, not about the queue: it
    may only be constructed once a postcondition has been observed, which is
    enforced by :meth:`succeeded`.
    """

    session_id: str
    seq: int
    command_id: str
    action: str
    status: ActionStatus
    reason_code: ReasonCode
    timestamp_ms: int
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    attempt: int = 1
    progress: float | None = None
    message: str = ""
    evidence: JsonDict = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        succeeded_with_wrong_reason = (
            self.status is ActionStatus.SUCCEEDED
            and self.reason_code is not ReasonCode.POSTCONDITION_MET
        )
        if succeeded_with_wrong_reason:
            raise ProtocolError(
                f"a succeeded result must carry POSTCONDITION_MET; got {self.reason_code.value}"
            )
        if self.progress is not None and not 0.0 <= self.progress <= 1.0:
            raise ProtocolError(f"progress must be within 0..1, got {self.progress}")
        if self.attempt < 0:
            raise ProtocolError(f"attempt must be non-negative, got {self.attempt}")

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    @classmethod
    def succeeded(
        cls,
        *,
        session_id: str,
        seq: int,
        command_id: str,
        action: str,
        timestamp_ms: int,
        evidence: JsonDict,
        started_at_ms: int | None = None,
        attempt: int = 1,
        message: str = "",
    ) -> ActionResult:
        """Build a success ack.

        Requires non-empty *evidence* — the observed postcondition. Without it
        there is nothing distinguishing "we verified it" from "we queued it",
        which is precisely the failure mode this protocol exists to prevent.
        """
        if not evidence:
            raise ProtocolError("a succeeded result requires postcondition evidence")
        return cls(
            session_id=session_id,
            seq=seq,
            command_id=command_id,
            action=action,
            status=ActionStatus.SUCCEEDED,
            reason_code=ReasonCode.POSTCONDITION_MET,
            timestamp_ms=timestamp_ms,
            started_at_ms=started_at_ms,
            finished_at_ms=timestamp_ms,
            attempt=attempt,
            progress=1.0,
            message=message,
            evidence=evidence,
        )

    @classmethod
    def failure(
        cls,
        *,
        session_id: str,
        seq: int,
        command_id: str,
        action: str,
        timestamp_ms: int,
        reason_code: ReasonCode,
        status: ActionStatus = ActionStatus.FAILED,
        message: str = "",
        evidence: JsonDict | None = None,
        diagnostics: Sequence[str] = (),
        attempt: int = 1,
    ) -> ActionResult:
        """Build a non-success terminal ack."""
        if status is ActionStatus.SUCCEEDED:
            raise ProtocolError("use ActionResult.succeeded() for successful results")
        return cls(
            session_id=session_id,
            seq=seq,
            command_id=command_id,
            action=action,
            status=status,
            reason_code=reason_code,
            timestamp_ms=timestamp_ms,
            finished_at_ms=timestamp_ms if status.is_terminal else None,
            attempt=attempt,
            message=message,
            evidence=dict(evidence or {}),
            diagnostics=list(diagnostics),
        )

    def to_dict(self) -> JsonDict:
        out: JsonDict = {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "seq": self.seq,
            "command_id": self.command_id,
            "action": self.action,
            "status": self.status.value,
            "reason_code": self.reason_code.value,
            "timestamp_ms": self.timestamp_ms,
            "attempt": self.attempt,
        }
        if self.started_at_ms is not None:
            out["started_at_ms"] = self.started_at_ms
        if self.finished_at_ms is not None:
            out["finished_at_ms"] = self.finished_at_ms
        if self.progress is not None:
            out["progress"] = self.progress
        if self.message:
            out["message"] = self.message
        if self.evidence:
            out["evidence"] = dict(self.evidence)
        if self.diagnostics:
            out["diagnostics"] = list(self.diagnostics)
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ActionResult:
        raw_reason = _as_str(_require(payload, "reason_code"), field_name="reason_code")
        try:
            reason = ReasonCode(raw_reason)
        except ValueError as exc:
            # Unknown codes are a protocol violation rather than a soft warning:
            # the executor's recovery table is keyed by code, and silently
            # mapping to INTERNAL_ERROR would hide a version mismatch.
            raise ProtocolError(f"unknown reason_code {raw_reason!r}") from exc
        progress = payload.get("progress")
        started = payload.get("started_at_ms")
        finished = payload.get("finished_at_ms")
        diagnostics = payload.get("diagnostics", [])
        return cls(
            schema_version=_as_str(
                _require(payload, "schema_version"), field_name="schema_version"
            ),
            session_id=_as_uuid_str(_require(payload, "session_id"), field_name="session_id"),
            seq=_as_int(_require(payload, "seq"), field_name="seq"),
            command_id=_as_uuid_str(_require(payload, "command_id"), field_name="command_id"),
            action=_as_str(_require(payload, "action"), field_name="action"),
            status=_as_enum(ActionStatus, _require(payload, "status"), field_name="status"),
            reason_code=reason,
            timestamp_ms=_as_int(_require(payload, "timestamp_ms"), field_name="timestamp_ms"),
            started_at_ms=(
                None if started is None else _as_int(started, field_name="started_at_ms")
            ),
            finished_at_ms=(
                None if finished is None else _as_int(finished, field_name="finished_at_ms")
            ),
            attempt=_as_int(payload.get("attempt", 1), field_name="attempt"),
            progress=(None if progress is None else _as_float(progress, field_name="progress")),
            message=_as_str(payload.get("message", ""), field_name="message"),
            evidence=dict(_as_mapping(payload.get("evidence", {}), field_name="evidence")),
            diagnostics=[
                _as_str(d, field_name="diagnostics[]")
                for d in _as_sequence(diagnostics, field_name="diagnostics")
            ],
        )


# --------------------------------------------------------------------------
# observation
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Position:
    """A world position. ``z`` is the floor, always an integer."""

    x: float
    y: float
    z: int
    direction: str | None = None

    def to_dict(self, *, with_direction: bool = True) -> JsonDict:
        out: JsonDict = {"x": self.x, "y": self.y, "z": self.z}
        if with_direction and self.direction is not None:
            out["direction"] = self.direction
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Position:
        direction = payload.get("direction")
        return cls(
            x=_as_float(_require(payload, "x"), field_name="position.x"),
            y=_as_float(_require(payload, "y"), field_name="position.y"),
            z=_as_int(_require(payload, "z"), field_name="position.z"),
            direction=(
                None if direction is None else _as_str(direction, field_name="position.direction")
            ),
        )

    def square_distance_to(self, other: Position) -> float:
        """Chebyshev distance on the grid, ignoring floors."""
        return max(abs(self.x - other.x), abs(self.y - other.y))


@dataclass(frozen=True, slots=True)
class GameState:
    """Which game, which save, whether time is running, and who else is in it."""

    build: str
    save_id: str
    paused: bool
    speed: float
    world_time: str | None = None
    #: True when the mod read this session as multiplayer, False when it read it
    #: as single player, and **None when it could not tell**. The three values
    #: are distinct on purpose: this project treats an absent reading as an
    #: absent reading everywhere else — a missing ``is_bleeding`` never means
    #: "not bleeding" — and a safety gate is the last place to start guessing.
    #: :meth:`SidecarRuntime.arm` refuses on None exactly as it refuses on True.
    multiplayer: bool | None = None

    @property
    def provably_single_player(self) -> bool:
        """True only when the mod positively reported single player."""
        return self.multiplayer is False

    def to_dict(self) -> JsonDict:
        out: JsonDict = {
            "build": self.build,
            "save_id": self.save_id,
            "paused": self.paused,
            "speed": self.speed,
        }
        if self.world_time is not None:
            out["world_time"] = self.world_time
        if self.multiplayer is not None:
            out["multiplayer"] = self.multiplayer
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> GameState:
        world_time = payload.get("world_time")
        save_id = _as_str(_require(payload, "save_id"), field_name="game.save_id")
        if not 1 <= len(save_id) <= MAX_SAVE_ID_LEN:
            raise ProtocolError("game.save_id must be 1..128 characters")
        return cls(
            build=_as_str(_require(payload, "build"), field_name="game.build"),
            save_id=save_id,
            paused=_as_bool(_require(payload, "paused"), field_name="game.paused"),
            speed=_as_float(_require(payload, "speed"), field_name="game.speed"),
            multiplayer=(
                None
                if payload.get("multiplayer") is None
                else _as_bool(payload["multiplayer"], field_name="game.multiplayer")
            ),
            world_time=(
                None if world_time is None else _as_str(world_time, field_name="game.world_time")
            ),
        )


@dataclass(frozen=True, slots=True)
class Wound:
    """One injury. ``bleeding`` drives the reflex guard, so it is first-class."""

    ref: str
    kind: str
    severity: float
    bleeding: bool = False

    def to_dict(self) -> JsonDict:
        return {
            "ref": self.ref,
            "kind": self.kind,
            "severity": self.severity,
            "bleeding": self.bleeding,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Wound:
        return cls(
            ref=_as_str(_require(payload, "ref"), field_name="wound.ref"),
            kind=_as_str(_require(payload, "kind"), field_name="wound.kind"),
            severity=_as_float(_require(payload, "severity"), field_name="wound.severity"),
            bleeding=_as_bool(payload.get("bleeding", False), field_name="wound.bleeding"),
        )


@dataclass(frozen=True, slots=True)
class Hands:
    """What the character is holding, as item refs."""

    primary: str | None = None
    secondary: str | None = None

    def to_dict(self) -> JsonDict:
        return {"primary": self.primary, "secondary": self.secondary}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Hands:
        primary = payload.get("primary")
        secondary = payload.get("secondary")
        return cls(
            primary=None if primary is None else _as_str(primary, field_name="hands.primary"),
            secondary=(
                None if secondary is None else _as_str(secondary, field_name="hands.secondary")
            ),
        )

    def holds(self, item_ref: str) -> bool:
        return item_ref in (self.primary, self.secondary)


@dataclass(frozen=True, slots=True)
class PlayerState:
    """Scalar character state.

    ``stats`` and ``moodles`` stay open maps on purpose: the game exposes more
    of them than the agent reasons about, and dropping the unknown ones at the
    boundary would make diagnostics useless. Named accessors below cover the
    ones policy actually reads.
    """

    present: bool
    alive: bool
    position: Position
    stats: dict[str, float | int | bool | None] = field(default_factory=dict)
    moodles: dict[str, int] = field(default_factory=dict)
    wounds: list[Wound] = field(default_factory=list)
    hands: Hands = field(default_factory=Hands)

    def to_dict(self) -> JsonDict:
        out: JsonDict = {
            "present": self.present,
            "alive": self.alive,
            "position": self.position.to_dict(),
            "stats": dict(self.stats),
            "moodles": dict(self.moodles),
        }
        if self.wounds:
            out["wounds"] = [w.to_dict() for w in self.wounds]
        if self.hands.primary is not None or self.hands.secondary is not None:
            out["hands"] = self.hands.to_dict()
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PlayerState:
        stats_raw = _as_mapping(_require(payload, "stats"), field_name="player.stats")
        moodles_raw = _as_mapping(_require(payload, "moodles"), field_name="player.moodles")
        stats: dict[str, float | int | bool | None] = {}
        for key, value in stats_raw.items():
            if value is None or isinstance(value, (bool, int, float)):
                stats[key] = value
            else:
                raise ProtocolError(f"player.stats.{key} must be a scalar or null")
        return cls(
            present=_as_bool(_require(payload, "present"), field_name="player.present"),
            alive=_as_bool(_require(payload, "alive"), field_name="player.alive"),
            position=Position.from_dict(
                _as_mapping(_require(payload, "position"), field_name="player.position")
            ),
            stats=stats,
            moodles={
                key: _as_int(value, field_name=f"player.moodles.{key}")
                for key, value in moodles_raw.items()
            },
            wounds=[
                Wound.from_dict(_as_mapping(w, field_name="player.wounds[]"))
                for w in _as_sequence(payload.get("wounds", []), field_name="player.wounds")
            ],
            hands=Hands.from_dict(_as_mapping(payload.get("hands", {}), field_name="player.hands")),
        )

    def _stat(self, name: str, default: float) -> float:
        value = self.stats.get(name)
        if value is None or isinstance(value, bool):
            return default
        return float(value)

    @property
    def health(self) -> float:
        return self._stat("health", 1.0)

    @property
    def hunger(self) -> float:
        """0.0 = full, 1.0 = starving (the game's ``getHunger`` convention)."""
        return self._stat("hunger", 0.0)

    @property
    def thirst(self) -> float:
        return self._stat("thirst", 0.0)

    @property
    def fatigue(self) -> float:
        return self._stat("fatigue", 0.0)

    @property
    def endurance(self) -> float:
        return self._stat("endurance", 1.0)

    @property
    def is_bleeding(self) -> bool:
        return any(w.bleeding for w in self.wounds)


@dataclass(frozen=True, slots=True)
class ContainerView:
    """One container visible to the agent."""

    ref: str
    kind: ContainerKind
    name: str
    parent_ref: str | None
    capacity: float | None = None
    used_capacity: float | None = None
    accessible: bool = True

    def to_dict(self) -> JsonDict:
        out: JsonDict = {
            "ref": self.ref,
            "kind": self.kind.value,
            "name": self.name,
            "parent_ref": self.parent_ref,
            "accessible": self.accessible,
        }
        if self.capacity is not None:
            out["capacity"] = self.capacity
        if self.used_capacity is not None:
            out["used_capacity"] = self.used_capacity
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ContainerView:
        capacity = payload.get("capacity")
        used = payload.get("used_capacity")
        return cls(
            ref=_as_str(_require(payload, "ref"), field_name="container.ref"),
            kind=_as_enum(ContainerKind, _require(payload, "kind"), field_name="container.kind"),
            name=_as_str(_require(payload, "name"), field_name="container.name"),
            parent_ref=(
                None
                if _require(payload, "parent_ref") is None
                else _as_str(payload["parent_ref"], field_name="container.parent_ref")
            ),
            capacity=(None if capacity is None else _as_float(capacity, field_name="capacity")),
            used_capacity=(None if used is None else _as_float(used, field_name="used_capacity")),
            accessible=_as_bool(payload.get("accessible", True), field_name="accessible"),
        )

    @property
    def free_capacity(self) -> float | None:
        """Remaining weight allowance, or None when the game does not report it."""
        if self.capacity is None or self.used_capacity is None:
            return None
        return max(0.0, self.capacity - self.used_capacity)


@dataclass(frozen=True, slots=True)
class ItemView:
    """One item, with the domain sub-objects the policies need.

    ``food``/``literature``/``fluid`` stay raw mappings here; the typed views
    live in the policy layer, which is where the domain rules that interpret
    them live too.
    """

    ref: str
    container_ref: str
    full_type: str
    display_name: str
    category: str
    weight: float
    favorite: bool = False
    equipped: bool = False
    tags: list[str] = field(default_factory=list)
    food: JsonDict | None = None
    literature: JsonDict | None = None
    fluid: JsonDict | None = None
    extra: JsonDict = field(default_factory=dict)

    _KNOWN_KEYS = (
        "ref",
        "container_ref",
        "full_type",
        "display_name",
        "category",
        "weight",
        "favorite",
        "equipped",
        "tags",
        "food",
        "literature",
        "fluid",
    )

    def to_dict(self) -> JsonDict:
        out: JsonDict = {
            "ref": self.ref,
            "container_ref": self.container_ref,
            "full_type": self.full_type,
            "display_name": self.display_name,
            "category": self.category,
            "weight": self.weight,
            "favorite": self.favorite,
            "equipped": self.equipped,
        }
        if self.tags:
            out["tags"] = list(self.tags)
        if self.food is not None:
            out["food"] = dict(self.food)
        if self.literature is not None:
            out["literature"] = dict(self.literature)
        if self.fluid is not None:
            out["fluid"] = dict(self.fluid)
        out.update(self.extra)
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ItemView:
        food = payload.get("food")
        literature = payload.get("literature")
        fluid = payload.get("fluid")
        return cls(
            ref=_as_str(_require(payload, "ref"), field_name="item.ref"),
            container_ref=_as_str(
                _require(payload, "container_ref"), field_name="item.container_ref"
            ),
            full_type=_as_str(_require(payload, "full_type"), field_name="item.full_type"),
            display_name=_as_str(_require(payload, "display_name"), field_name="item.display_name"),
            category=_as_str(_require(payload, "category"), field_name="item.category"),
            weight=_as_float(_require(payload, "weight"), field_name="item.weight"),
            favorite=_as_bool(payload.get("favorite", False), field_name="item.favorite"),
            equipped=_as_bool(payload.get("equipped", False), field_name="item.equipped"),
            tags=[
                _as_str(t, field_name="item.tags[]")
                for t in _as_sequence(payload.get("tags", []), field_name="item.tags")
            ],
            food=(None if food is None else dict(_as_mapping(food, field_name="item.food"))),
            literature=(
                None
                if literature is None
                else dict(_as_mapping(literature, field_name="item.literature"))
            ),
            fluid=(None if fluid is None else dict(_as_mapping(fluid, field_name="item.fluid"))),
            extra={k: v for k, v in payload.items() if k not in cls._KNOWN_KEYS},
        )


@dataclass(frozen=True, slots=True)
class InventoryView:
    """The container tree plus every item the mod could enumerate."""

    containers: list[ContainerView] = field(default_factory=list)
    items: list[ItemView] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return {
            "containers": [c.to_dict() for c in self.containers],
            "items": [i.to_dict() for i in self.items],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> InventoryView:
        return cls(
            containers=[
                ContainerView.from_dict(_as_mapping(c, field_name="containers[]"))
                for c in _as_sequence(payload.get("containers", []), field_name="containers")
            ],
            items=[
                ItemView.from_dict(_as_mapping(i, field_name="items[]"))
                for i in _as_sequence(payload.get("items", []), field_name="items")
            ],
        )

    def container(self, ref: str) -> ContainerView | None:
        return next((c for c in self.containers if c.ref == ref), None)

    def item(self, ref: str) -> ItemView | None:
        return next((i for i in self.items if i.ref == ref), None)

    def items_in(self, container_ref: str) -> list[ItemView]:
        return [i for i in self.items if i.container_ref == container_ref]

    def main_container(self) -> ContainerView | None:
        return next((c for c in self.containers if c.kind is ContainerKind.PLAYER_MAIN), None)


@dataclass(frozen=True, slots=True)
class NearbyObject:
    """A world object within the observation radius.

    The four door-state fields are tri-state on purpose: ``None`` means the
    build did not expose the reader, which must never be conflated with an
    observed ``False`` — "the lock could not be read" and "the door is
    unlocked" authorise very different plans. They are ``None`` for every
    object that is not a door.
    """

    ref: str
    kind: str
    distance: float
    position: Position | None = None
    semantics: list[str] = field(default_factory=list)
    open: bool | None = None
    locked: bool | None = None
    barricaded: bool | None = None
    orientation: str | None = None

    def to_dict(self) -> JsonDict:
        out: JsonDict = {"ref": self.ref, "kind": self.kind, "distance": self.distance}
        if self.position is not None:
            out["position"] = self.position.to_dict(with_direction=False)
        if self.semantics:
            out["semantics"] = list(self.semantics)
        if self.open is not None:
            out["open"] = self.open
        if self.locked is not None:
            out["locked"] = self.locked
        if self.barricaded is not None:
            out["barricaded"] = self.barricaded
        if self.orientation is not None:
            out["orientation"] = self.orientation
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> NearbyObject:
        position = payload.get("position")
        orientation = payload.get("orientation")
        return cls(
            ref=_as_str(_require(payload, "ref"), field_name="object.ref"),
            kind=_as_str(_require(payload, "kind"), field_name="object.kind"),
            distance=_as_float(_require(payload, "distance"), field_name="object.distance"),
            position=(
                None
                if position is None
                else Position.from_dict(_as_mapping(position, field_name="object.position"))
            ),
            semantics=[
                _as_str(s, field_name="object.semantics[]")
                for s in _as_sequence(payload.get("semantics", []), field_name="object.semantics")
            ],
            open=_as_optional_bool(payload.get("open"), field_name="object.open"),
            locked=_as_optional_bool(payload.get("locked"), field_name="object.locked"),
            barricaded=_as_optional_bool(payload.get("barricaded"), field_name="object.barricaded"),
            orientation=(
                None
                if orientation is None
                else _as_str(orientation, field_name="object.orientation")
            ),
        )


@dataclass(frozen=True, slots=True)
class NearbyZombie:
    """A zombie within the observation radius.

    ``chasing`` matters more than ``distance`` for the reflex guard: a distant
    zombie that has noticed the player is a live interrupt, a close one that
    has not may be safely read past.
    """

    ref: str
    distance: float
    visible: bool = True
    chasing: bool = False
    position: Position | None = None

    def to_dict(self) -> JsonDict:
        out: JsonDict = {
            "ref": self.ref,
            "distance": self.distance,
            "visible": self.visible,
            "chasing": self.chasing,
        }
        if self.position is not None:
            out["position"] = self.position.to_dict(with_direction=False)
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> NearbyZombie:
        position = payload.get("position")
        return cls(
            ref=_as_str(_require(payload, "ref"), field_name="zombie.ref"),
            distance=_as_float(_require(payload, "distance"), field_name="zombie.distance"),
            visible=_as_bool(payload.get("visible", True), field_name="zombie.visible"),
            chasing=_as_bool(payload.get("chasing", False), field_name="zombie.chasing"),
            position=(
                None
                if position is None
                else Position.from_dict(_as_mapping(position, field_name="zombie.position"))
            ),
        )


@dataclass(frozen=True, slots=True)
class NearbyView:
    """Everything the mod saw around the player this tick."""

    objects: list[NearbyObject] = field(default_factory=list)
    zombies: list[NearbyZombie] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return {
            "objects": [o.to_dict() for o in self.objects],
            "zombies": [z.to_dict() for z in self.zombies],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> NearbyView:
        return cls(
            objects=[
                NearbyObject.from_dict(_as_mapping(o, field_name="nearby.objects[]"))
                for o in _as_sequence(payload.get("objects", []), field_name="nearby.objects")
            ],
            zombies=[
                NearbyZombie.from_dict(_as_mapping(z, field_name="nearby.zombies[]"))
                for z in _as_sequence(payload.get("zombies", []), field_name="nearby.zombies")
            ],
        )

    def nearest_zombie_distance(self) -> float | None:
        return min((z.distance for z in self.zombies), default=None)

    def chasing_zombies(self) -> list[NearbyZombie]:
        return [z for z in self.zombies if z.chasing]

    def objects_with(self, semantic: str) -> list[NearbyObject]:
        return [o for o in self.objects if semantic in o.semantics]


@dataclass(frozen=True, slots=True)
class ActionState:
    """What the character is doing right now, and who asked for it."""

    ownership: ActionOwnership
    busy: bool
    action_id: str | None = None
    type: str | None = None
    progress: float | None = None

    def to_dict(self) -> JsonDict:
        out: JsonDict = {"ownership": self.ownership.value, "busy": self.busy}
        if self.action_id is not None:
            out["action_id"] = self.action_id
        if self.type is not None:
            out["type"] = self.type
        if self.progress is not None:
            out["progress"] = self.progress
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ActionState:
        action_id = payload.get("action_id")
        type_ = payload.get("type")
        progress = payload.get("progress")
        return cls(
            ownership=_as_enum(
                ActionOwnership, _require(payload, "ownership"), field_name="action.ownership"
            ),
            busy=_as_bool(_require(payload, "busy"), field_name="action.busy"),
            action_id=(
                None if action_id is None else _as_str(action_id, field_name="action.action_id")
            ),
            type=(None if type_ is None else _as_str(type_, field_name="action.type")),
            progress=(
                None if progress is None else _as_float(progress, field_name="action.progress")
            ),
        )

    @property
    def blocks_automation(self) -> bool:
        """True when the agent must not enqueue anything.

        Anything the mod did not author — including ``ambiguous`` — is treated
        as the user's, so automation waits rather than fighting the player for
        the action queue.
        """
        return self.busy and self.ownership is not ActionOwnership.MOD


@dataclass(frozen=True, slots=True)
class SafetyState:
    """The guard's view: arming, mode, threat and takeover."""

    armed: bool
    mode: SessionMode
    danger_level: DangerLevel
    manual_takeover: bool
    sidecar_stale: bool = False

    def to_dict(self) -> JsonDict:
        return {
            "armed": self.armed,
            "mode": self.mode.value,
            "danger_level": self.danger_level.value,
            "manual_takeover": self.manual_takeover,
            "sidecar_stale": self.sidecar_stale,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SafetyState:
        return cls(
            armed=_as_bool(_require(payload, "armed"), field_name="safety.armed"),
            mode=_as_enum(SessionMode, _require(payload, "mode"), field_name="safety.mode"),
            danger_level=_as_enum(
                DangerLevel, _require(payload, "danger_level"), field_name="safety.danger_level"
            ),
            manual_takeover=_as_bool(
                _require(payload, "manual_takeover"), field_name="safety.manual_takeover"
            ),
            sidecar_stale=_as_bool(
                payload.get("sidecar_stale", False), field_name="safety.sidecar_stale"
            ),
        )


@dataclass(frozen=True, slots=True)
class Observation:
    """One snapshot (``full = True``) or diff (``full = False``) of the world."""

    session_id: str
    seq: int
    timestamp_ms: int
    game: GameState
    player: PlayerState
    safety: SafetyState
    action: ActionState
    full: bool = True
    inventory: InventoryView | None = None
    nearby: NearbyView | None = None
    capability_revision: int = 0
    active_goal_id: str | None = None
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> JsonDict:
        out: JsonDict = {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "seq": self.seq,
            "timestamp_ms": self.timestamp_ms,
            "full": self.full,
            "game": self.game.to_dict(),
            "player": self.player.to_dict(),
            "safety": self.safety.to_dict(),
            "action": self.action.to_dict(),
            "capability_revision": self.capability_revision,
        }
        if self.inventory is not None:
            out["inventory"] = self.inventory.to_dict()
        if self.nearby is not None:
            out["nearby"] = self.nearby.to_dict()
        if self.active_goal_id is not None:
            out["active_goal_id"] = self.active_goal_id
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Observation:
        inventory = payload.get("inventory")
        nearby = payload.get("nearby")
        goal = payload.get("active_goal_id")
        return cls(
            schema_version=_as_str(
                _require(payload, "schema_version"), field_name="schema_version"
            ),
            session_id=_as_uuid_str(_require(payload, "session_id"), field_name="session_id"),
            seq=_as_int(_require(payload, "seq"), field_name="seq"),
            timestamp_ms=_as_int(_require(payload, "timestamp_ms"), field_name="timestamp_ms"),
            full=_as_bool(payload.get("full", True), field_name="full"),
            game=GameState.from_dict(_as_mapping(_require(payload, "game"), field_name="game")),
            player=PlayerState.from_dict(
                _as_mapping(_require(payload, "player"), field_name="player")
            ),
            safety=SafetyState.from_dict(
                _as_mapping(_require(payload, "safety"), field_name="safety")
            ),
            action=ActionState.from_dict(
                _as_mapping(_require(payload, "action"), field_name="action")
            ),
            inventory=(
                None
                if inventory is None
                else InventoryView.from_dict(_as_mapping(inventory, field_name="inventory"))
            ),
            nearby=(
                None
                if nearby is None
                else NearbyView.from_dict(_as_mapping(nearby, field_name="nearby"))
            ),
            capability_revision=_as_int(
                payload.get("capability_revision", 0), field_name="capability_revision"
            ),
            active_goal_id=(None if goal is None else _as_str(goal, field_name="active_goal_id")),
        )

    @property
    def can_act(self) -> bool:
        """Cheap gate the executor checks before composing any mutating command."""
        return (
            self.player.present
            and self.player.alive
            and self.safety.armed
            and not self.safety.manual_takeover
            and not self.safety.sidecar_stale
            and not self.action.blocks_automation
        )
