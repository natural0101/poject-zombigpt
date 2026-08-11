"""Liveness for the two peers, each judged on its own clock.

§3.3 and §3.6 tier 0: the game writes ``heartbeat.game.json`` and the sidecar
writes ``heartbeat.sidecar.json``. Neither file is a status *claim* about the
other side — each peer only ever writes its own — which is what makes staleness
decidable: if a file stops moving, that peer stopped, and the other side must
act on that rather than assume the silence is benign.

The two heartbeats are tracked independently on purpose. A stale game heartbeat
means commands cannot be executed at all; a stale sidecar heartbeat means the
mod must disarm and stop obeying. Collapsing them into one "connected" flag
would lose the distinction between those two very different situations.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from ..ipc.atomic import DocumentError, read_json_document, write_json_atomic
from ..ipc.clocks import Clock, system_clock_ms
from ..ipc.layout import IpcLayout
from ..protocol import DangerLevel, JsonDict, SessionMode
from ..version import PROTOCOL_VERSION

#: A peer that has not written for this long is treated as gone. Chosen well
#: above the 4 Hz observation cadence of §3.3 so a single slow frame, a save
#: pause or a GC hitch cannot look like a crash.
DEFAULT_TIMEOUT_MS: Final = 5_000

#: How far ahead of the reader's clock a heartbeat may be stamped and still be
#: read as evidence about now. Beyond it the two clocks disagree, and since
#: :meth:`Heartbeat.age_ms` clamps a future stamp to zero, a heartbeat stamped
#: further ahead than this would read "fresh" for the whole of the skew — long
#: after the peer that wrote it stopped writing. Same tolerance and same reason
#: as the handshake's ``DEFAULT_CLOCK_SKEW_MS``.
DEFAULT_FUTURE_SKEW_MS: Final = 5_000

#: Nonces are 128 bits of CSPRNG output: they must be unguessable *and* unequal
#: across restarts, because §3.3 uses "the nonce changed" as the signal that a
#: peer is new rather than replayed.
NONCE_BYTES: Final = 16


class Peer(StrEnum):
    """Which side of the exchange a heartbeat belongs to."""

    GAME = "game"
    SIDECAR = "sidecar"

    @property
    def other(self) -> Peer:
        return Peer.SIDECAR if self is Peer.GAME else Peer.GAME


class HeartbeatError(ValueError):
    """A heartbeat document is malformed."""


def new_nonce() -> str:
    """Fresh session nonce."""
    return secrets.token_hex(NONCE_BYTES)


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """One tier-0 liveness record.

    The optional fields are the tier-0 payload of §3.6. They are optional
    because the sidecar's heartbeat legitimately knows none of them, and a
    heartbeat that invented ``armed: false`` would be a fabricated observation.
    """

    peer: Peer
    session_id: str
    nonce: str
    seq: int
    timestamp_ms: int
    version: str
    protocol_version: str = PROTOCOL_VERSION
    build: str | None = None
    player_present: bool | None = None
    armed: bool | None = None
    mode: SessionMode | None = None
    danger_level: DangerLevel | None = None
    active_action_id: str | None = None

    def __post_init__(self) -> None:
        if self.seq < 0:
            raise HeartbeatError(f"heartbeat seq must be non-negative, got {self.seq}")
        if self.timestamp_ms < 0:
            raise HeartbeatError(
                f"heartbeat timestamp must be non-negative, got {self.timestamp_ms}"
            )
        if not self.nonce:
            raise HeartbeatError("heartbeat nonce must not be empty")

    def age_ms(self, now_ms: int) -> int:
        """How long ago this heartbeat was written; never negative.

        A timestamp in the future means the peers' clocks disagree, not that
        the heartbeat is fresher than possible, so it is clamped to zero rather
        than allowed to hide a subsequent stall.
        """
        return max(0, now_ms - self.timestamp_ms)

    def is_stale(self, now_ms: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> bool:
        """True once the peer has been silent for longer than *timeout_ms*."""
        if timeout_ms <= 0:
            raise ValueError(f"timeout_ms must be positive, got {timeout_ms}")
        return self.age_ms(now_ms) > timeout_ms

    def to_dict(self) -> JsonDict:
        out: JsonDict = {
            "peer": self.peer.value,
            "protocol_version": self.protocol_version,
            "session_id": self.session_id,
            "nonce": self.nonce,
            "seq": self.seq,
            "timestamp_ms": self.timestamp_ms,
            "version": self.version,
        }
        optional: dict[str, Any] = {
            "build": self.build,
            "player_present": self.player_present,
            "active_action_id": self.active_action_id,
            "armed": self.armed,
            "mode": None if self.mode is None else self.mode.value,
            "danger_level": None if self.danger_level is None else self.danger_level.value,
        }
        out.update({key: value for key, value in optional.items() if value is not None})
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Heartbeat:
        try:
            peer = Peer(_text(payload, "peer"))
        except ValueError as exc:
            raise HeartbeatError(f"unknown heartbeat peer: {payload.get('peer')!r}") from exc
        mode_raw = payload.get("mode")
        danger_raw = payload.get("danger_level")
        try:
            mode = None if mode_raw is None else SessionMode(mode_raw)
            danger = None if danger_raw is None else DangerLevel(danger_raw)
        except ValueError as exc:
            raise HeartbeatError(f"unknown heartbeat enum value: {exc}") from exc
        return cls(
            peer=peer,
            session_id=_text(payload, "session_id"),
            nonce=_text(payload, "nonce"),
            seq=_number(payload, "seq"),
            timestamp_ms=_number(payload, "timestamp_ms"),
            version=_text(payload, "version"),
            protocol_version=_text(payload, "protocol_version"),
            build=_optional_text(payload, "build"),
            player_present=_optional_flag(payload, "player_present"),
            armed=_optional_flag(payload, "armed"),
            mode=mode,
            danger_level=danger,
            active_action_id=_optional_text(payload, "active_action_id"),
        )


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise HeartbeatError(f"heartbeat field {key!r} must be a non-empty string")
    return value


def _optional_text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HeartbeatError(f"heartbeat field {key!r} must be a string")
    return value


def _optional_flag(payload: Mapping[str, Any], key: str) -> bool | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise HeartbeatError(f"heartbeat field {key!r} must be a boolean")
    return value


def _number(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise HeartbeatError(f"heartbeat field {key!r} must be an integer")
    return value


@dataclass(frozen=True, slots=True)
class PeerLiveness:
    """The answer to "is that peer still there?", with the evidence."""

    peer: Peer
    heartbeat: Heartbeat | None
    alive: bool
    detail: str

    @property
    def missing(self) -> bool:
        return self.heartbeat is None


@dataclass
class HeartbeatMonitor:
    """Reads and writes the two heartbeat files for one exchange directory.

    Timeouts are per peer: the game ticks on its own frame budget while the
    sidecar ticks on a timer, and forcing them to share a threshold would make
    one of the two wrong.
    """

    layout: IpcLayout
    clock: Clock = system_clock_ms
    game_timeout_ms: int = DEFAULT_TIMEOUT_MS
    sidecar_timeout_ms: int = DEFAULT_TIMEOUT_MS
    future_skew_ms: int = DEFAULT_FUTURE_SKEW_MS
    _seq: dict[Peer, int] = field(default_factory=dict, init=False)

    def path_for(self, peer: Peer) -> Path:
        return self.layout.game_heartbeat if peer is Peer.GAME else self.layout.sidecar_heartbeat

    def timeout_for(self, peer: Peer) -> int:
        return self.game_timeout_ms if peer is Peer.GAME else self.sidecar_timeout_ms

    def publish(
        self,
        peer: Peer,
        *,
        session_id: str,
        nonce: str,
        version: str,
        build: str | None = None,
        player_present: bool | None = None,
        armed: bool | None = None,
        mode: SessionMode | None = None,
        danger_level: DangerLevel | None = None,
        active_action_id: str | None = None,
    ) -> Heartbeat:
        """Write this side's heartbeat, allocating its sequence number."""
        seq = self._seq.get(peer, 0)
        self._seq[peer] = seq + 1
        heartbeat = Heartbeat(
            peer=peer,
            session_id=session_id,
            nonce=nonce,
            seq=seq,
            timestamp_ms=self.clock(),
            version=version,
            build=build,
            player_present=player_present,
            armed=armed,
            mode=mode,
            danger_level=danger_level,
            active_action_id=active_action_id,
        )
        write_json_atomic(self.layout, self.path_for(peer), heartbeat.to_dict())
        return heartbeat

    def read(self, peer: Peer) -> Heartbeat | None:
        """Read a peer's heartbeat, or None when there is no usable one."""
        return self._probe(peer)[0]

    def _probe(self, peer: Peer) -> tuple[Heartbeat | None, str]:
        """Read a heartbeat and, when there is none, say what was wrong.

        "The peer never wrote a heartbeat", "the file is corrupt" and "the file
        belongs to the other peer" are three different situations for whoever
        has to diagnose a silent game, so the reason travels with the verdict
        instead of collapsing into one unexplained absence.
        """
        try:
            payload = read_json_document(self.path_for(peer))
        except DocumentError as exc:
            return None, f"no readable heartbeat: {exc}"
        try:
            heartbeat = Heartbeat.from_dict(payload)
        except HeartbeatError as exc:
            return None, f"no readable heartbeat: malformed document ({exc})"
        if heartbeat.peer is not peer:
            # A file naming the wrong peer means the directory is being shared
            # by something else; refusing it is safer than trusting the name.
            return None, f"no readable heartbeat: file claims to belong to {heartbeat.peer.value}"
        return heartbeat, "fresh"

    def liveness(self, peer: Peer, now_ms: int | None = None) -> PeerLiveness:
        """Judge one peer. Absent evidence is never treated as alive."""
        moment = self.clock() if now_ms is None else now_ms
        heartbeat, detail = self._probe(peer)
        if heartbeat is None:
            return PeerLiveness(peer, None, alive=False, detail=detail)
        ahead_ms = heartbeat.timestamp_ms - moment
        if ahead_ms > self.future_skew_ms:
            # Not a fresher heartbeat: two clocks that disagree. ``age_ms``
            # clamps a future stamp to zero, so left alone this file would read
            # "fresh" for the whole of the skew — including every second after
            # the peer stopped writing it. Refusing it keeps a stall visible,
            # which is the same call the snapshot reader makes on its own
            # documents.
            return PeerLiveness(
                peer,
                heartbeat,
                alive=False,
                detail=(
                    f"stamped {ahead_ms} ms in the future, beyond {self.future_skew_ms} ms "
                    "of clock skew; it describes no moment on this clock"
                ),
            )
        timeout = self.timeout_for(peer)
        if heartbeat.is_stale(moment, timeout):
            return PeerLiveness(
                peer,
                heartbeat,
                alive=False,
                detail=f"silent for {heartbeat.age_ms(moment)} ms (timeout {timeout} ms)",
            )
        return PeerLiveness(peer, heartbeat, alive=True, detail="fresh")
