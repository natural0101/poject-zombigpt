"""Session establishment, staleness and the recovery rules of §3.12.

``session.json`` is the sidecar's proposal, not a fact about the game. The mod
accepts it only under the five conditions of §3.3 — a well-formed id, a
supported protocol major, a timestamp that is not stale, a nonce that differs
from the previous session's, and a live peer heartbeat — and
:func:`evaluate_handshake` is that decision expressed once so both sides agree
on it.

The rest of this module implements what happens when one side disappears:

* **Sidecar restart.** The session survives, a fresh sidecar nonce is minted,
  and nothing on the command stream is replayed. Re-executing a command the
  player already lived through is the failure mode that costs food, durability
  or a life.
* **Game restart.** The session generation is bumped, every reference minted
  before it becomes invalid, and whatever was in flight is closed as ``lost`` —
  the honest status for work whose outcome nobody observed.
* **Save changed.** References are invalidated and the agent stays disarmed
  until the user re-arms. Autonomy never resumes by itself on a world the
  agent has not looked at yet.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from ..ipc.atomic import DocumentError, read_json_document, write_json_atomic
from ..ipc.clocks import Clock, system_clock_ms
from ..ipc.layout import IpcLayout
from ..protocol import (
    ActionResult,
    ActionStatus,
    Command,
    ItemRef,
    JsonDict,
    ReasonCode,
    RefError,
    RefKind,
    SessionMode,
    ZombieRef,
    belongs_to_session,
    ref_kind,
)
from ..version import PRODUCT_VERSION, PROTOCOL_VERSION, protocol_compatible
from .heartbeat import DEFAULT_TIMEOUT_MS, HeartbeatMonitor, Peer, PeerLiveness, new_nonce

#: A handshake proposal older than this is refused: it was written before the
#: peer that is answering it started, so it says nothing about the present.
DEFAULT_MAX_SESSION_AGE_MS: Final = 60_000

#: Tolerance for the two clocks disagreeing. A proposal from slightly in the
#: future is a skewed clock, not a forgery; far in the future is neither and is
#: refused the same way a stale one is.
DEFAULT_CLOCK_SKEW_MS: Final = 5_000

DEFAULT_OBSERVATION_HZ: Final = 4
MAX_OBSERVATION_HZ: Final = 30


class SessionError(ValueError):
    """A session document is malformed or a transition was requested out of order."""


@dataclass(frozen=True, slots=True)
class SessionDescriptor:
    """The contents of ``session.json`` (§3.3)."""

    session_id: str
    nonce: str
    created_at_ms: int
    mode: SessionMode
    sidecar_version: str = PRODUCT_VERSION
    protocol_version: str = PROTOCOL_VERSION
    requested_observation_hz: int = DEFAULT_OBSERVATION_HZ
    generation: int = 0
    save_id: str | None = None

    def __post_init__(self) -> None:
        try:
            uuid.UUID(self.session_id)
        except ValueError as exc:
            raise SessionError(f"session_id must be a UUID, got {self.session_id!r}") from exc
        if not self.nonce:
            raise SessionError("session nonce must not be empty")
        if self.created_at_ms < 0:
            raise SessionError(f"created_at_ms must be non-negative, got {self.created_at_ms}")
        if not 1 <= self.requested_observation_hz <= MAX_OBSERVATION_HZ:
            raise SessionError(
                f"requested_observation_hz must be within 1..{MAX_OBSERVATION_HZ}, "
                f"got {self.requested_observation_hz}"
            )
        if self.generation < 0:
            raise SessionError(f"generation must be non-negative, got {self.generation}")

    def to_dict(self) -> JsonDict:
        out: JsonDict = {
            "protocol_version": self.protocol_version,
            "session_id": self.session_id,
            "created_at_ms": self.created_at_ms,
            "sidecar_version": self.sidecar_version,
            "requested_observation_hz": self.requested_observation_hz,
            "mode": self.mode.value,
            "nonce": self.nonce,
            "generation": self.generation,
        }
        if self.save_id is not None:
            out["save_id"] = self.save_id
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SessionDescriptor:
        def text(key: str) -> str:
            value = payload.get(key)
            if not isinstance(value, str) or not value:
                raise SessionError(f"session field {key!r} must be a non-empty string")
            return value

        def integer(key: str, default: int | None = None) -> int:
            value = payload.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int):
                raise SessionError(f"session field {key!r} must be an integer")
            return value

        raw_mode = text("mode")
        try:
            mode = SessionMode(raw_mode)
        except ValueError as exc:
            raise SessionError(f"unknown session mode {raw_mode!r}") from exc
        save_id = payload.get("save_id")
        if save_id is not None and not isinstance(save_id, str):
            raise SessionError("session field 'save_id' must be a string")
        return cls(
            session_id=text("session_id"),
            nonce=text("nonce"),
            created_at_ms=integer("created_at_ms"),
            mode=mode,
            sidecar_version=text("sidecar_version"),
            protocol_version=text("protocol_version"),
            requested_observation_hz=integer("requested_observation_hz", DEFAULT_OBSERVATION_HZ),
            generation=integer("generation", 0),
            save_id=save_id,
        )

    def with_generation(self, generation: int) -> SessionDescriptor:
        return SessionDescriptor(
            session_id=self.session_id,
            nonce=self.nonce,
            created_at_ms=self.created_at_ms,
            mode=self.mode,
            sidecar_version=self.sidecar_version,
            protocol_version=self.protocol_version,
            requested_observation_hz=self.requested_observation_hz,
            generation=generation,
            save_id=self.save_id,
        )

    def with_save_id(self, save_id: str | None) -> SessionDescriptor:
        return SessionDescriptor(
            session_id=self.session_id,
            nonce=self.nonce,
            created_at_ms=self.created_at_ms,
            mode=self.mode,
            sidecar_version=self.sidecar_version,
            protocol_version=self.protocol_version,
            requested_observation_hz=self.requested_observation_hz,
            generation=self.generation,
            save_id=save_id,
        )


@dataclass(frozen=True, slots=True)
class HandshakeResult:
    """Whether a proposed session may be accepted, and why not if it may not."""

    accepted: bool
    detail: str
    reason_code: ReasonCode | None = None
    session: SessionDescriptor | None = None


def evaluate_handshake(
    payload: Mapping[str, Any],
    *,
    now_ms: int,
    peer: Peer,
    peer_liveness: PeerLiveness | None = None,
    previous: SessionDescriptor | None = None,
    max_age_ms: int = DEFAULT_MAX_SESSION_AGE_MS,
    skew_ms: int = DEFAULT_CLOCK_SKEW_MS,
) -> HandshakeResult:
    """Apply the five acceptance rules of §3.3 to a proposed session.

    *peer* is the side whose liveness is being required — the mod passes
    ``SIDECAR``, the sidecar passes ``GAME`` — and decides which reason code a
    dead peer produces.
    """
    try:
        candidate = SessionDescriptor.from_dict(payload)
    except SessionError as exc:
        return HandshakeResult(False, str(exc), ReasonCode.INVALID_ARGUMENT)

    if not protocol_compatible(candidate.protocol_version):
        return HandshakeResult(
            False,
            f"protocol {candidate.protocol_version} is not compatible with {PROTOCOL_VERSION}",
            ReasonCode.INVALID_ARGUMENT,
        )

    age = now_ms - candidate.created_at_ms
    if age > max_age_ms:
        return HandshakeResult(
            False,
            f"session was created {age} ms ago, older than {max_age_ms} ms",
            ReasonCode.STALE_SESSION,
        )
    if age < -skew_ms:
        return HandshakeResult(
            False,
            f"session timestamp is {-age} ms in the future, beyond {skew_ms} ms of skew",
            ReasonCode.STALE_SESSION,
        )

    if previous is not None and candidate.nonce == previous.nonce:
        # A repeated nonce means we are looking at a replay of the previous
        # handshake, not at a peer that restarted. Accepting it would let a
        # stale session.json re-attach after the session it described ended.
        return HandshakeResult(
            False,
            "nonce is identical to the previous session's",
            ReasonCode.STALE_SESSION,
        )

    if peer_liveness is None or not peer_liveness.alive:
        detail = "no peer liveness evidence" if peer_liveness is None else peer_liveness.detail
        reason = ReasonCode.GAME_DISCONNECTED if peer is Peer.GAME else ReasonCode.STALE_SESSION
        return HandshakeResult(False, f"{peer.value} heartbeat: {detail}", reason)

    return HandshakeResult(True, "accepted", None, candidate)


class RecoveryKind(StrEnum):
    """Which of the §3.12 recovery paths was taken."""

    SIDECAR_RESTART = "sidecar_restart"
    GAME_RESTART = "game_restart"
    SAVE_CHANGED = "save_changed"


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """What a recovery transition invalidated, and what it closed."""

    kind: RecoveryKind
    reason_code: ReasonCode
    generation: int
    refs_invalidated: bool
    requires_rearm: bool
    detail: str
    lost: tuple[ActionResult, ...] = ()


@dataclass(frozen=True, slots=True)
class ResumeOutcome:
    """The result of a sidecar reattaching to an existing session."""

    resumed: bool
    detail: str
    session: SessionDescriptor | None = None
    sidecar_nonce: str | None = None
    requires_rearm: bool = False
    reason_code: ReasonCode | None = None


@dataclass(frozen=True, slots=True)
class SessionStaleness:
    """Why the current session can or cannot still be used."""

    stale: bool
    detail: str
    reason_code: ReasonCode | None = None


@dataclass
class SessionManager:
    """Owns ``session.json``, the generation counter and the recovery rules.

    It deliberately does not own arming: it only records that a re-arm is
    *required*, and only an explicit :meth:`mark_rearmed` clears that. §3.12
    forbids autonomy resuming by itself after a save or a game restart, and a
    flag nothing but a user action can clear is how that is guaranteed.
    """

    layout: IpcLayout
    clock: Clock = system_clock_ms
    sidecar_version: str = PRODUCT_VERSION
    heartbeats: HeartbeatMonitor | None = None
    max_session_age_ms: int = DEFAULT_MAX_SESSION_AGE_MS
    game_timeout_ms: int = DEFAULT_TIMEOUT_MS
    _session: SessionDescriptor | None = field(default=None, init=False)
    _sidecar_nonce: str | None = field(default=None, init=False)
    _requires_rearm: bool = field(default=False, init=False)
    _refs_valid: bool = field(default=True, init=False)
    _local_seq: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.heartbeats is None:
            self.heartbeats = HeartbeatMonitor(
                self.layout, clock=self.clock, game_timeout_ms=self.game_timeout_ms
            )

    @property
    def monitor(self) -> HeartbeatMonitor:
        assert self.heartbeats is not None  # set in __post_init__
        return self.heartbeats

    @property
    def session(self) -> SessionDescriptor | None:
        return self._session

    @property
    def generation(self) -> int:
        return 0 if self._session is None else self._session.generation

    @property
    def requires_rearm(self) -> bool:
        return self._requires_rearm

    @property
    def refs_valid(self) -> bool:
        """False while references must be re-derived from a full snapshot."""
        return self._refs_valid

    @property
    def sidecar_nonce(self) -> str | None:
        return self._sidecar_nonce

    # -- creation and loading ---------------------------------------------

    def load(self) -> SessionDescriptor | None:
        """Read ``session.json``, or None when it is absent or unusable."""
        try:
            payload = read_json_document(self.layout.session)
        except DocumentError:
            return None
        try:
            return SessionDescriptor.from_dict(payload)
        except SessionError:
            return None

    def create(
        self,
        *,
        mode: SessionMode,
        requested_observation_hz: int = DEFAULT_OBSERVATION_HZ,
        nonce: str | None = None,
        session_id: str | None = None,
        save_id: str | None = None,
    ) -> SessionDescriptor:
        """Write a new ``session.json``.

        The nonce must differ from the previous session's: §3.3 makes "the
        nonce changed" the mod's only evidence that this is a genuinely new
        sidecar rather than a leftover file.
        """
        self.layout.ensure()
        previous = self.load()
        chosen = nonce or new_nonce()
        if previous is not None and chosen == previous.nonce:
            raise SessionError("session nonce must differ from the previous session's")
        session = SessionDescriptor(
            session_id=session_id or str(uuid.uuid4()),
            nonce=chosen,
            created_at_ms=self.clock(),
            mode=mode,
            sidecar_version=self.sidecar_version,
            requested_observation_hz=requested_observation_hz,
            save_id=save_id,
        )
        write_json_atomic(self.layout, self.layout.session, session.to_dict())
        self._session = session
        self._sidecar_nonce = chosen
        self._requires_rearm = False
        self._refs_valid = True
        return session

    def adopt(self, session: SessionDescriptor) -> None:
        """Take an already-established session as the current one."""
        self._session = session
        self._refs_valid = True

    # -- staleness ---------------------------------------------------------

    def staleness(self, now_ms: int | None = None) -> SessionStaleness:
        """Decide whether the session on disk can still be used.

        Absence of evidence counts as stale in every branch: a missing file, an
        unreadable one and a silent game are all "we do not know what is going
        on", and the only safe reading of that is to stop.
        """
        moment = self.clock() if now_ms is None else now_ms
        session = self._session or self.load()
        if session is None:
            return SessionStaleness(True, "no readable session.json", ReasonCode.STALE_SESSION)
        liveness = self.monitor.liveness(Peer.GAME, moment)
        if not liveness.alive:
            return SessionStaleness(
                True, f"game heartbeat: {liveness.detail}", ReasonCode.GAME_DISCONNECTED
            )
        heartbeat = liveness.heartbeat
        if heartbeat is not None and heartbeat.session_id != session.session_id:
            return SessionStaleness(
                True,
                f"game is on session {heartbeat.session_id}, not {session.session_id}",
                ReasonCode.STALE_SESSION,
            )
        return SessionStaleness(False, "session is current")

    # -- recovery ----------------------------------------------------------

    def resume(self, now_ms: int | None = None) -> ResumeOutcome:
        """Reattach to the session already on disk after a sidecar restart.

        A new sidecar nonce is minted and published so the mod can tell this is
        a new process. Nothing is replayed: callers take their command-stream
        consumer from
        :meth:`~pz_agent_core.ipc.queue.CommandQueue.command_reader_at_end`
        precisely so that the commands of the previous run are never executed
        a second time.
        """
        moment = self.clock() if now_ms is None else now_ms
        session = self.load()
        if session is None:
            return ResumeOutcome(
                False, "no readable session.json", reason_code=ReasonCode.STALE_SESSION
            )
        liveness = self.monitor.liveness(Peer.GAME, moment)
        if not liveness.alive:
            return ResumeOutcome(
                False,
                f"game heartbeat: {liveness.detail}",
                session=session,
                reason_code=ReasonCode.GAME_DISCONNECTED,
            )
        heartbeat = liveness.heartbeat
        if heartbeat is not None and heartbeat.session_id != session.session_id:
            return ResumeOutcome(
                False,
                f"game is on session {heartbeat.session_id}, not {session.session_id}",
                session=session,
                reason_code=ReasonCode.STALE_SESSION,
            )
        nonce = new_nonce()
        self._session = session
        self._sidecar_nonce = nonce
        self._refs_valid = True
        self.monitor.publish(
            Peer.SIDECAR,
            session_id=session.session_id,
            nonce=nonce,
            version=self.sidecar_version,
            mode=session.mode,
        )
        return ResumeOutcome(
            True,
            "resumed existing session with a new sidecar nonce",
            session=session,
            sidecar_nonce=nonce,
            requires_rearm=self._requires_rearm,
        )

    def on_game_restart(
        self,
        *,
        in_flight: Sequence[Command] = (),
        now_ms: int | None = None,
    ) -> RecoveryOutcome:
        """Bump the generation, invalidate refs, close in-flight work as lost."""
        moment = self.clock() if now_ms is None else now_ms
        session = self._session or self.load()
        if session is None:
            raise SessionError("cannot recover a game restart without a session")
        updated = session.with_generation(session.generation + 1)
        self._session = updated
        self._refs_valid = False
        self._requires_rearm = True
        write_json_atomic(self.layout, self.layout.session, updated.to_dict())
        return RecoveryOutcome(
            kind=RecoveryKind.GAME_RESTART,
            reason_code=ReasonCode.GAME_DISCONNECTED,
            generation=updated.generation,
            refs_invalidated=True,
            requires_rearm=True,
            detail=f"game restarted; session generation is now {updated.generation}",
            lost=self._close_as_lost(in_flight, ReasonCode.GAME_DISCONNECTED, moment),
        )

    def note_save_id(
        self,
        save_id: str,
        *,
        in_flight: Sequence[Command] = (),
        now_ms: int | None = None,
    ) -> RecoveryOutcome | None:
        """Record the observed save id; recover when it changed.

        Returns None while the save is the same one. A change means the world
        the references described no longer exists, so they are invalidated and
        the agent stays disarmed until the user says otherwise.
        """
        moment = self.clock() if now_ms is None else now_ms
        session = self._session or self.load()
        if session is None:
            raise SessionError("cannot record a save id without a session")
        if session.save_id == save_id:
            return None
        previous = session.save_id
        if previous is None:
            # First observation of the save: no reference was ever minted
            # against a *different* world, so this is not a recovery event — and
            # the generation must stay where it is. Bumping it here would
            # invalidate every ref already handed out for this very save while
            # reporting that nothing happened, which is the silent kind of
            # failure this module exists to avoid.
            updated = session.with_save_id(save_id)
            self._session = updated
            write_json_atomic(self.layout, self.layout.session, updated.to_dict())
            return None
        updated = session.with_save_id(save_id).with_generation(session.generation + 1)
        self._session = updated
        write_json_atomic(self.layout, self.layout.session, updated.to_dict())
        self._refs_valid = False
        self._requires_rearm = True
        return RecoveryOutcome(
            kind=RecoveryKind.SAVE_CHANGED,
            reason_code=ReasonCode.SAVE_CHANGED,
            generation=updated.generation,
            refs_invalidated=True,
            requires_rearm=True,
            detail=f"save changed from {previous} to {save_id}",
            lost=self._close_as_lost(in_flight, ReasonCode.SAVE_CHANGED, moment),
        )

    def note_full_snapshot(self, save_id: str) -> bool:
        """Re-validate references after a full snapshot of the current save.

        Returns True when references became usable again. A snapshot of some
        other save does not re-validate anything.
        """
        session = self._session
        if session is None or session.save_id != save_id:
            return False
        already_valid = self._refs_valid
        self._refs_valid = True
        return not already_valid

    def mark_rearmed(self) -> None:
        """Clear the re-arm requirement. Only a user action may call this."""
        self._requires_rearm = False

    # -- references --------------------------------------------------------

    def is_ref_current(self, raw: str) -> bool:
        """True when *raw* may still be resolved against the live world.

        Three ways to fail: it belongs to another session, it was minted before
        a generation bump, or references are wholesale invalid because the
        world was reloaded and nothing has been re-observed since.
        """
        session = self._session
        if session is None or not belongs_to_session(raw, session.session_id):
            return False
        if not self._refs_valid:
            return False
        try:
            kind = ref_kind(raw)
        except RefError:
            return False
        try:
            if kind is RefKind.ITEM:
                return ItemRef.parse(raw).generation == session.generation
            if kind is RefKind.ZOMBIE:
                return ZombieRef.parse(raw).generation == session.generation
        except RefError:
            return False
        # Containers and squares carry no generation of their own; the session
        # and the validity flag above are what bound their lifetime.
        return True

    # -- helpers -----------------------------------------------------------

    def _close_as_lost(
        self, commands: Sequence[Command], reason: ReasonCode, now_ms: int
    ) -> tuple[ActionResult, ...]:
        """Terminate commands whose outcome nobody observed.

        ``lost`` rather than ``failed``: the action may well have completed in
        the game before it went away, and claiming either outcome would be a
        statement about the world that was never verified.
        """
        results: list[ActionResult] = []
        for command in commands:
            results.append(
                ActionResult.failure(
                    session_id=command.session_id,
                    seq=self._next_local_seq(),
                    command_id=command.command_id,
                    action=command.action.value,
                    timestamp_ms=now_ms,
                    reason_code=reason,
                    status=ActionStatus.LOST,
                    message="outcome unobserved when the session was interrupted",
                )
            )
        return tuple(results)

    def _next_local_seq(self) -> int:
        seq = self._local_seq
        self._local_seq = seq + 1
        return seq
