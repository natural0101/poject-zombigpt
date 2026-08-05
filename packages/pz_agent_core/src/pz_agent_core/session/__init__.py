"""Session lifecycle: handshake, heartbeats, locking and recovery.

The rules implemented here are the ones that decide whether the agent is
allowed to believe anything at all: which session is current (:mod:`.handshake`),
whether each peer is still alive (:mod:`.heartbeat`), and whether this process
is the only sidecar attached to the exchange directory (:mod:`.lock`).
"""

from __future__ import annotations

from .handshake import (
    DEFAULT_CLOCK_SKEW_MS,
    DEFAULT_MAX_SESSION_AGE_MS,
    HandshakeResult,
    RecoveryKind,
    RecoveryOutcome,
    ResumeOutcome,
    SessionDescriptor,
    SessionError,
    SessionManager,
    SessionStaleness,
    evaluate_handshake,
)
from .heartbeat import (
    DEFAULT_TIMEOUT_MS,
    Heartbeat,
    HeartbeatError,
    HeartbeatMonitor,
    Peer,
    PeerLiveness,
    new_nonce,
)
from .lock import DEFAULT_STALE_AFTER_MS, LockError, LockInfo, LockOutcome, SidecarLock

__all__ = [
    "DEFAULT_CLOCK_SKEW_MS",
    "DEFAULT_MAX_SESSION_AGE_MS",
    "DEFAULT_STALE_AFTER_MS",
    "DEFAULT_TIMEOUT_MS",
    "HandshakeResult",
    "Heartbeat",
    "HeartbeatError",
    "HeartbeatMonitor",
    "LockError",
    "LockInfo",
    "LockOutcome",
    "Peer",
    "PeerLiveness",
    "RecoveryKind",
    "RecoveryOutcome",
    "ResumeOutcome",
    "SessionDescriptor",
    "SessionError",
    "SessionManager",
    "SessionStaleness",
    "SidecarLock",
    "evaluate_handshake",
    "new_nonce",
]
