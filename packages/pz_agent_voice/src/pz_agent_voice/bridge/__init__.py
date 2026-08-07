"""The voice bridge: a child process that owns the vendor SDK, and its wire.

:mod:`.protocol` is the JSONL wire — a closed set of message types, a version on
every line and a byte cap in both directions. :mod:`.client` is the supervisor:
it launches the bridge program, reads it on a thread, restarts it a bounded
number of times and stops it without ever waiting for it to agree.

The split is the usual one in this project. The protocol has no I/O in it and is
tested by feeding it bytes; the client has nothing to say about the wire and is
tested against real child processes, because framing, back-pressure and a
refused kill are claims about processes and a mock cannot make them.

Nothing here is imported by :mod:`pz_agent_voice` itself: the bridge is
optional. A build with no bridge program installed runs the rest of the sidecar,
and :func:`~.client.check_bridge` says so at check time rather than in the
middle of the first sentence the user waits on.
"""

from __future__ import annotations

from .client import (
    MAX_TIMEOUT_SECONDS,
    BridgeCheck,
    BridgeConfig,
    BridgeError,
    BridgeFailed,
    BridgeListener,
    BridgeReason,
    BridgeReport,
    BridgeState,
    BridgeTimeout,
    BridgeUnavailable,
    JsonlBridge,
    TeamONBridgeClient,
    check_bridge,
)
from .protocol import (
    BRIDGE_PROTOCOL_VERSION,
    BRIDGE_UNAVAILABLE_PHRASE,
    MAX_LINE_BYTES,
    MAX_MESSAGE_BYTES,
    MESSAGE_DIRECTIONS,
    BridgeDirection,
    BridgeFault,
    BridgeFaultCode,
    BridgeMessage,
    BridgeMessageType,
    BridgeOutcome,
    BridgeProtocolError,
    BridgeTranscript,
    LineFramer,
    MalformedMessage,
    MessageTooLarge,
    OutcomeStatus,
    OverlongLine,
    ProtocolMismatch,
    UnknownMessageType,
    decode,
    encode,
    goal_message,
    hello_message,
    interrupt_message,
    read_error,
    read_outcome,
    read_ready,
    read_transcript,
    speak_message,
)

__all__ = [
    "BRIDGE_PROTOCOL_VERSION",
    "BRIDGE_UNAVAILABLE_PHRASE",
    "MAX_LINE_BYTES",
    "MAX_MESSAGE_BYTES",
    "MAX_TIMEOUT_SECONDS",
    "MESSAGE_DIRECTIONS",
    "BridgeCheck",
    "BridgeConfig",
    "BridgeDirection",
    "BridgeError",
    "BridgeFailed",
    "BridgeFault",
    "BridgeFaultCode",
    "BridgeListener",
    "BridgeMessage",
    "BridgeMessageType",
    "BridgeOutcome",
    "BridgeProtocolError",
    "BridgeReason",
    "BridgeReport",
    "BridgeState",
    "BridgeTimeout",
    "BridgeTranscript",
    "BridgeUnavailable",
    "JsonlBridge",
    "LineFramer",
    "MalformedMessage",
    "MessageTooLarge",
    "OutcomeStatus",
    "OverlongLine",
    "ProtocolMismatch",
    "TeamONBridgeClient",
    "UnknownMessageType",
    "check_bridge",
    "decode",
    "encode",
    "goal_message",
    "hello_message",
    "interrupt_message",
    "read_error",
    "read_outcome",
    "read_ready",
    "read_transcript",
    "speak_message",
]
