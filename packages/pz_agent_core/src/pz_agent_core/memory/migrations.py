"""Reading a memory file that an older build wrote — or refusing to.

A store on disk outlives the code that produced it. There are exactly three
honest answers to a document whose schema version is not the current one:
upgrade it, refuse it, or say the version is unreadable. Guessing is not among
them — a field whose meaning changed between versions would be read with the
wrong meaning and never look wrong.

So the rule here is: every version from :data:`MIN_SUPPORTED_SCHEMA` up has a
named migration to the next one, and anything outside that range is a
:class:`MemorySchemaError` with a sentence saying which build wrote it. A file
from the *future* is refused rather than partially read, because a newer writer
may have added a constraint this build does not know it is violating.

The migration chain is bounded by construction: it steps one version at a time
from the document's version to :data:`MEMORY_SCHEMA_VERSION`, and the loop can
run at most ``MEMORY_SCHEMA_VERSION - MIN_SUPPORTED_SCHEMA`` times.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

from .model import REASON_MIGRATED

__all__ = [
    "MEMORY_SCHEMA_VERSION",
    "MIN_SUPPORTED_SCHEMA",
    "MemorySchemaError",
    "document_version",
    "migrate_document",
]

#: Schema the current build writes.
#:
#: * 1 — first on-disk layout. Containers carried ``tail``/``kind``/``label``/
#:   ``last_seen_ms`` only, reservations were a flat array of item type strings,
#:   and there were no safe zones or preferences.
#: * 2 — containers gained ``last_inspected_ms`` and ``categories``,
#:   reservations became objects with a reason and a creation time, and
#:   ``safe_zones`` and ``preferences`` were added.
MEMORY_SCHEMA_VERSION: Final = 2

#: Oldest version this build will still upgrade. A document below it is refused
#: rather than migrated through code nobody has exercised in years.
MIN_SUPPORTED_SCHEMA: Final = 1


class MemorySchemaError(ValueError):
    """A stored memory cannot be read as this build understands it."""


def document_version(document: Mapping[str, Any]) -> int:
    """The declared schema version.

    Raises:
        MemorySchemaError: when the field is missing or is not an integer. An
            undeclared version cannot be assumed to be the current one: that is
            precisely the silent misread this module exists to prevent.
    """
    if "schema_version" not in document:
        raise MemorySchemaError(
            "the stored memory declares no schema_version; refusing to guess which build wrote it"
        )
    value = document["schema_version"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise MemorySchemaError(f"schema_version must be an integer, got {type(value).__name__}")
    return value


def migrate_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return *document* as a version :data:`MEMORY_SCHEMA_VERSION` document.

    Raises:
        MemorySchemaError: when the version is newer than this build, older than
            :data:`MIN_SUPPORTED_SCHEMA`, or not declared at all.
    """
    version = document_version(document)
    if version > MEMORY_SCHEMA_VERSION:
        raise MemorySchemaError(
            f"the stored memory is schema {version}; this build reads at most "
            f"{MEMORY_SCHEMA_VERSION}. A newer pz-agent wrote it — upgrade rather than "
            "letting this one reinterpret fields it does not know about"
        )
    if version < MIN_SUPPORTED_SCHEMA:
        raise MemorySchemaError(
            f"the stored memory is schema {version}; this build no longer upgrades "
            f"anything below schema {MIN_SUPPORTED_SCHEMA}. Delete it and the agent will "
            "learn the save again"
        )
    working = dict(document)
    for _ in range(MEMORY_SCHEMA_VERSION - MIN_SUPPORTED_SCHEMA):
        if version >= MEMORY_SCHEMA_VERSION:
            break
        step = _MIGRATIONS.get(version)
        if step is None:
            raise MemorySchemaError(
                f"no migration is registered from memory schema {version}; the chain is broken"
            )
        working = step(working)
        version += 1
        working["schema_version"] = version
    return working


def _v1_to_v2(document: Mapping[str, Any]) -> dict[str, Any]:
    """Upgrade the first layout.

    The interesting half is reservations. Schema 1 stored them as bare item type
    strings, so the reason the user gave was never recorded. The migration says
    exactly that instead of inventing one: a reservation whose reason reads
    "because the user said so" would be a sentence this build made up and then
    showed back to the user as their own.
    """
    upgraded = dict(document)
    upgraded["containers"] = [
        {**_as_dict(container), "last_inspected_ms": 0, "categories": []}
        for container in _as_list(document.get("containers", []))
    ]
    upgraded["reservations"] = [
        {"full_type": entry, "reason": REASON_MIGRATED, "created_at_ms": 0}
        for entry in _as_list(document.get("reservations", []))
        if isinstance(entry, str) and entry
    ]
    upgraded.setdefault("safe_zones", [])
    upgraded.setdefault("preferences", [])
    return upgraded


#: version -> the function that turns it into version + 1.
_MIGRATIONS: Final[Mapping[int, Callable[[Mapping[str, Any]], dict[str, Any]]]] = {
    1: _v1_to_v2,
}


def _as_list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise MemorySchemaError(
            f"expected an array in the stored memory, got {type(value).__name__}"
        )
    return value


def _as_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MemorySchemaError(
            f"expected an object in the stored memory, got {type(value).__name__}"
        )
    return value
