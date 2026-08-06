"""Applying the observation view's quarantine rule to payloads that are not observations.

:mod:`pz_agent_core.observation.compact` already guarantees that an observation
handed to a model carries no path, no username and no unmarked game text. The
other payloads that leave this boundary — postcondition evidence, memory
records, diagnostics, log lines — are assembled elsewhere and get the same
treatment here rather than a second, weaker one.

The rule is deliberately closed rather than heuristic: a string survives outside
:data:`~pz_agent_core.observation.compact.UNTRUSTED_TEXT_KEY` only if it is a
*reference* or a member of a protocol vocabulary. Everything else is
quarantined, whatever it looks like. A shape test would have let a display name
like ``IGNORE_PREVIOUS_INSTRUCTIONS`` through as an "identifier"; membership of
a closed set cannot be gamed that way.

Quarantined strings still go through
:func:`~pz_agent_core.observation.compact.redact_text`, so control characters,
path-shaped substrings and length are handled exactly as they are for an
observation. Depth, width and string count are all bounded: this walks data
another process produced.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from pz_agent_core.observation.compact import (
    CONTENT_MARKER,
    UNTRUSTED_TEXT_KEY,
    redact_text,
)
from pz_agent_core.protocol import (
    ActionName,
    ActionOwnership,
    ActionStatus,
    CapabilityState,
    ContainerKind,
    DangerLevel,
    JsonDict,
    ReasonCode,
    RefKind,
    RiskClass,
    SessionMode,
)

__all__ = [
    "CONTENT_MARKER_KEY",
    "MAX_DEPTH",
    "MAX_KEYS",
    "MAX_SEQUENCE",
    "as_token",
    "is_reference",
    "scrub_payload",
    "scrub_text",
]

#: The key :data:`CONTENT_MARKER` is published under. Named here because the two
#: keys that describe the quarantine have to be reserved from the payloads being
#: quarantined; see :data:`_RESERVED_KEYS`.
CONTENT_MARKER_KEY: Final = "content_marker"

#: Keys this module writes, and therefore keys a producer may not. A payload
#: arriving with its own ``untrusted_text`` or ``content_marker`` is claiming to
#: have already been quarantined; honouring that would let whoever wrote it
#: decide which of its strings a client treats as safe. They are dropped, and the
#: real marker is written from the rule actually applied here.
_RESERVED_KEYS: Final[frozenset[str]] = frozenset({UNTRUSTED_TEXT_KEY, CONTENT_MARKER_KEY})

#: A payload nesting deeper than this is truncated rather than walked. Evidence
#: objects are two or three levels deep; anything past this is a producer bug.
MAX_DEPTH: Final = 6

MAX_KEYS: Final = 32
MAX_SEQUENCE: Final = 32

#: Keys are structural, not content: they are written by our own code and by the
#: mod's fixed field list, so they are checked for shape and dropped when they
#: fail rather than quarantined.
#:
#: Every pattern here is applied with :meth:`re.Pattern.fullmatch`. ``$`` alone
#: also matches before a trailing newline, so ``match`` would let ``"item:1\n"``
#: out of the quarantine as a reference and ``"info\n"`` through as a token — a
#: control character in a field the client is told is safe to render.
_KEY: Final = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

#: ``<kind>:<session>:<tail>``. Bounded because a world container reference is
#: the longest legitimate one and it is nowhere near this.
_REFERENCE: Final = re.compile(
    r"^(?:" + "|".join(sorted(k.value for k in RefKind)) + r"):[A-Za-z0-9:_.\-]{1,200}$"
)


def _vocabulary() -> frozenset[str]:
    """Every value a protocol enum may legitimately put in a payload.

    Built from the enums themselves so a new status or reason code is admitted
    the moment it is defined, and a string that merely *looks* like one is not.
    """
    members: set[str] = set()
    for enum_cls in (
        ActionName,
        ActionOwnership,
        ActionStatus,
        CapabilityState,
        ContainerKind,
        DangerLevel,
        ReasonCode,
        RefKind,
        RiskClass,
        SessionMode,
    ):
        members.update(member.value for member in enum_cls)
    return frozenset(members)


_VOCABULARY: Final[frozenset[str]] = _vocabulary()


def is_reference(value: str) -> bool:
    """True for a session-scoped reference string."""
    return bool(_REFERENCE.fullmatch(value))


#: Identifier positions: a memory kind, a check code, a log component. These are
#: minted by our own code and are supposed to be tokens, so a value that is not
#: one is dropped rather than quarantined — a sentence where a component name
#: belongs means the producer is wrong, not that the text needs a warning label.
_TOKEN: Final = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def as_token(value: object) -> str | None:
    """Keep an identifier-shaped string, drop anything else."""
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        return None
    return value


def scrub_text(value: str) -> str:
    """Make one free-text string safe to carry. Never safe to obey."""
    return redact_text(value)


def scrub_payload(payload: Mapping[str, Any] | None) -> JsonDict:
    """Return *payload* with every free-text string quarantined and bounded.

    The result always carries :data:`CONTENT_MARKER` when something was
    quarantined, so the warning cannot be separated from the value it applies
    to by a client that flattens or reorders keys.
    """
    if not payload:
        return {}
    return _mapping(payload, depth=0)


def _mapping(payload: Mapping[str, Any], *, depth: int) -> JsonDict:
    out: JsonDict = {}
    text: JsonDict = {}
    for key, value in list(payload.items())[:MAX_KEYS]:
        if not isinstance(key, str) or not _KEY.fullmatch(key) or key in _RESERVED_KEYS:
            continue
        if isinstance(value, str) and not _passes(value):
            text[key] = scrub_text(value)
            continue
        scrubbed = _value(value, depth=depth + 1)
        if scrubbed is _DROPPED:
            continue
        out[key] = scrubbed
    if text:
        out[UNTRUSTED_TEXT_KEY] = text
        out[CONTENT_MARKER_KEY] = CONTENT_MARKER
    return out


#: Sentinel for "this value has no safe representation"; distinct from ``None``,
#: which is a legitimate JSON value a payload may carry deliberately.
_DROPPED: Final = object()


def _passes(value: str) -> bool:
    """True when a string may appear outside the quarantine."""
    return value in _VOCABULARY or is_reference(value)


def _value(value: Any, *, depth: int) -> Any:
    if depth > MAX_DEPTH:
        return _DROPPED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if _passes(value) else _DROPPED
    if isinstance(value, bytes):
        # ``bytes`` is a Sequence, so without this it would be walked byte by
        # byte into a list of integers instead of being refused.
        return _DROPPED
    if isinstance(value, Mapping):
        return _mapping(value, depth=depth)
    if isinstance(value, Sequence):
        return _sequence(value, depth=depth)
    return _DROPPED


def _sequence(value: Sequence[Any], *, depth: int) -> list[Any]:
    out: list[Any] = []
    for element in value[:MAX_SEQUENCE]:
        if isinstance(element, str) and not _passes(element):
            # A bare string in a list has no key to quarantine it under, so the
            # whole element is replaced by its quarantined form rather than
            # being dropped: the caller still learns something was there.
            out.append({UNTRUSTED_TEXT_KEY: {"value": scrub_text(element)}})
            continue
        scrubbed = _value(element, depth=depth + 1)
        if scrubbed is not _DROPPED:
            out.append(scrubbed)
    return out
