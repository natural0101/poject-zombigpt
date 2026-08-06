"""Save-scoped, bounded memory: what survives a session, and nothing else.

The agent's working view of the world is the observation store — a ring of
recent snapshots that means nothing once the session ends. This package holds the
other half: the handful of derived facts worth keeping across restarts, and the
user's own instructions about them.

Three rules define the package:

* **Save scope is structural.** A :class:`SaveMemory` is built for one save id
  and a document from another is refused, not merged. World references name
  places, and the same coordinates in a different save are a different world.
* **Bounds are applied on write, and again on read.** Every mutator prunes and
  evicts before it returns, so "within bounds" is true after every call rather
  than after a cleanup nobody runs, and a document loaded from disk is held to
  the same caps as one built in memory. User-authored records — reservations and
  preferences — are never evicted in either direction; the store refuses instead.
* **An unreadable version is refused, never guessed.** A document from a newer
  build, or from one older than the migration chain reaches, raises with a
  sentence saying so.
"""

from __future__ import annotations

from .migrations import (
    MEMORY_SCHEMA_VERSION,
    MIN_SUPPORTED_SCHEMA,
    MemorySchemaError,
    document_version,
    migrate_document,
)
from .model import (
    MAX_CATEGORY_LEN,
    MAX_DETAIL_LEN,
    MAX_FULL_TYPE_LEN,
    MAX_LABEL_LEN,
    MAX_PREFERENCE_KEY_LEN,
    MAX_PREFERENCE_VALUE_LEN,
    MAX_TAIL_LEN,
    REASON_MIGRATED,
    FailedPath,
    KnownContainer,
    MemoryValueError,
    Preference,
    Reservation,
    SafeZone,
    Square,
    TaskOutcome,
    TaskRecord,
    container_tail,
)
from .persistence import (
    MAX_MEMORY_BYTES,
    MEMORY_FILE_PREFIX,
    MemoryStore,
    MemoryStoreError,
)
from .store import (
    CEILINGS,
    DEFAULT_MEMORY_CONFIG,
    MemoryCapacityError,
    MemoryConfig,
    MemoryScopeError,
    SaveMemory,
)

__all__ = [
    "CEILINGS",
    "DEFAULT_MEMORY_CONFIG",
    "MAX_CATEGORY_LEN",
    "MAX_DETAIL_LEN",
    "MAX_FULL_TYPE_LEN",
    "MAX_LABEL_LEN",
    "MAX_MEMORY_BYTES",
    "MAX_PREFERENCE_KEY_LEN",
    "MAX_PREFERENCE_VALUE_LEN",
    "MAX_TAIL_LEN",
    "MEMORY_FILE_PREFIX",
    "MEMORY_SCHEMA_VERSION",
    "MIN_SUPPORTED_SCHEMA",
    "REASON_MIGRATED",
    "FailedPath",
    "KnownContainer",
    "MemoryCapacityError",
    "MemoryConfig",
    "MemorySchemaError",
    "MemoryScopeError",
    "MemoryStore",
    "MemoryStoreError",
    "MemoryValueError",
    "Preference",
    "Reservation",
    "SafeZone",
    "SaveMemory",
    "Square",
    "TaskOutcome",
    "TaskRecord",
    "container_tail",
    "document_version",
    "migrate_document",
]
