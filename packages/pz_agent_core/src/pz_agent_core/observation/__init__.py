"""Observation handling: tier-1 diffs, a bounded store, and the planner view.

Nothing in this package touches the filesystem or the game. It turns the
observation messages defined in ``protocol`` into the three things the rest of
the sidecar needs: a compact update, a short bounded history, and a redacted
summary that is safe to put in front of a language model.
"""

from __future__ import annotations

from .compact import (
    CONTENT_MARKER,
    CONTENT_RULE,
    MAX_CONTAINERS,
    MAX_ITEMS,
    MAX_OBJECTS,
    MAX_TEXT_CHARS,
    MAX_ZOMBIES,
    UNTRUSTED_TEXT_KEY,
    compact_for_planner,
    redact_text,
    save_scope,
)
from .diff import (
    DiffError,
    ListDelta,
    MappingDelta,
    ObservationDiff,
    apply_diff,
    diff_observations,
)
from .store import (
    DEFAULT_WINDOW,
    MAX_WINDOW,
    MIN_WINDOW,
    IngestResult,
    ObservationStore,
    SeqGap,
)

__all__ = [
    "CONTENT_MARKER",
    "CONTENT_RULE",
    "DEFAULT_WINDOW",
    "MAX_CONTAINERS",
    "MAX_ITEMS",
    "MAX_OBJECTS",
    "MAX_TEXT_CHARS",
    "MAX_WINDOW",
    "MAX_ZOMBIES",
    "MIN_WINDOW",
    "UNTRUSTED_TEXT_KEY",
    "DiffError",
    "IngestResult",
    "ListDelta",
    "MappingDelta",
    "ObservationDiff",
    "ObservationStore",
    "SeqGap",
    "apply_diff",
    "compact_for_planner",
    "diff_observations",
    "redact_text",
    "save_scope",
]
