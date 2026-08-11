"""Stable reason codes shared by the mod, the sidecar and the MCP boundary.

These strings are part of the public contract: they appear in action results,
MCP error payloads and log lines, so they are append-only. Renaming one is a
protocol major bump.
"""

from __future__ import annotations

from enum import StrEnum


class ReasonCode(StrEnum):
    """Why an action ended the way it did.

    Terminal-failure codes explain a refusal or an abort; ``POSTCONDITION_MET``
    is the *only* code that may accompany ``status = "succeeded"``.
    """

    # --- success -----------------------------------------------------------
    POSTCONDITION_MET = "POSTCONDITION_MET"

    # --- session / lifecycle ----------------------------------------------
    NOT_ARMED = "NOT_ARMED"
    STALE_SESSION = "STALE_SESSION"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    SEQ_CONFLICT = "SEQ_CONFLICT"
    GAME_DISCONNECTED = "GAME_DISCONNECTED"
    SAVE_CHANGED = "SAVE_CHANGED"
    SESSION_TERMINATED = "SESSION_TERMINATED"

    # --- capability / validation ------------------------------------------
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    INVALID_REF = "INVALID_REF"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    QUEUE_REJECTED = "QUEUE_REJECTED"
    POLICY_DENIED = "POLICY_DENIED"

    # --- runtime interruption ---------------------------------------------
    PLAYER_BUSY_MANUAL_ACTION = "PLAYER_BUSY_MANUAL_ACTION"
    USER_TAKEOVER = "USER_TAKEOVER"
    THREAT_INTERRUPTED = "THREAT_INTERRUPTED"
    PANIC_STOP = "PANIC_STOP"
    CANCELLED_BY_REQUEST = "CANCELLED_BY_REQUEST"

    # --- movement ----------------------------------------------------------
    PATH_NOT_FOUND = "PATH_NOT_FOUND"
    PATH_STUCK = "PATH_STUCK"
    TARGET_OUT_OF_RANGE = "TARGET_OUT_OF_RANGE"
    TARGET_NOT_LOADED = "TARGET_NOT_LOADED"
    # A locked and a barricaded door demand different replanning — a key hunt
    # versus a detour — so they are distinct codes rather than one
    # PRECONDITION_FAILED whose detail a planner would have to parse.
    DOOR_LOCKED = "DOOR_LOCKED"
    DOOR_BARRICADED = "DOOR_BARRICADED"

    # --- verification ------------------------------------------------------
    ACTION_TIMEOUT = "ACTION_TIMEOUT"
    POSTCONDITION_FAILED = "POSTCONDITION_FAILED"
    NO_PROGRESS = "NO_PROGRESS"

    # --- domain policy -----------------------------------------------------
    NO_SAFE_FOOD = "NO_SAFE_FOOD"
    NO_SAFE_DRINK = "NO_SAFE_DRINK"
    NO_SUITABLE_LITERATURE = "NO_SUITABLE_LITERATURE"
    CONTAINER_FULL = "CONTAINER_FULL"
    RESOURCE_RESERVED = "RESOURCE_RESERVED"
    RECIPE_UNKNOWN = "RECIPE_UNKNOWN"
    RECIPE_MATERIALS_MISSING = "RECIPE_MATERIALS_MISSING"
    SQUARE_OCCUPIED = "SQUARE_OCCUPIED"
    WOULD_TRAP_PLAYER = "WOULD_TRAP_PLAYER"

    # --- catch-all ---------------------------------------------------------
    INTERNAL_ERROR = "INTERNAL_ERROR"


#: Codes that may legitimately be retried by the executor with the same intent.
#: Everything else is a hard stop for that step — retrying an ``INVALID_REF``
#: or a ``POLICY_DENIED`` just burns the retry budget.
RETRYABLE_CODES: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.PATH_STUCK,
        ReasonCode.NO_PROGRESS,
        ReasonCode.ACTION_TIMEOUT,
        ReasonCode.QUEUE_REJECTED,
        ReasonCode.PLAYER_BUSY_MANUAL_ACTION,
    }
)

#: Codes that mean "stop the whole plan", not just this step.
PLAN_FATAL_CODES: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.PANIC_STOP,
        ReasonCode.USER_TAKEOVER,
        ReasonCode.GAME_DISCONNECTED,
        ReasonCode.SAVE_CHANGED,
        ReasonCode.STALE_SESSION,
        ReasonCode.SESSION_TERMINATED,
        ReasonCode.NOT_ARMED,
    }
)
