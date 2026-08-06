"""Memory on disk: one file per save, written whole or not at all.

The write discipline is the one :mod:`pz_agent_core.ipc.atomic` establishes —
temporary file with the pid and a serial in its name, flush, ``fsync``,
``os.replace``, and remove the scratch file if any of that fails. A reader
therefore sees either the previous complete memory or the new complete memory,
never a prefix. The IPC helper itself is not reused because it guards paths
against :class:`~pz_agent_core.ipc.layout.IpcLayout`, and memory does not live
in the exchange directory the mod can see: the game has no business reading what
the sidecar remembers.

The filename is derived from
:func:`~pz_agent_core.observation.compact.save_scope`, not from the save id. A
save id is a directory fragment — ``Muldraugh, KY\\survivor`` — so putting one in
a path would be a traversal waiting to happen and would spell the player's
profile name into a filename besides. The digest is stable for a save, which is
all a per-save file needs.
"""

from __future__ import annotations

import json
import os
from itertools import count
from pathlib import Path
from typing import Any, Final

from ..observation.compact import save_scope
from .migrations import MEMORY_SCHEMA_VERSION, migrate_document
from .store import DEFAULT_MEMORY_CONFIG, MemoryConfig, SaveMemory

__all__ = [
    "MAX_MEMORY_BYTES",
    "MEMORY_FILE_PREFIX",
    "MemoryStore",
    "MemoryStoreError",
]

#: A memory file holds a few hundred bounded records. Anything far larger is
#: corrupt or hostile and is refused before it is parsed, not after.
#:
#: Set *above* the largest document :data:`~pz_agent_core.memory.store.CEILINGS`
#: permits, which is a shade over 1.1 MiB when every collection is full of
#: maximum-length strings. The two bounds have to agree in this direction or the
#: byte cap becomes a trap: a store built entirely of accepted writes would
#: refuse to persist itself, and the user would lose the reservations they had
#: just been told were recorded. ``test_a_memory_at_every_ceiling_still_fits_
#: under_the_byte_cap`` is what keeps the relation true as the caps move.
MAX_MEMORY_BYTES: Final = 2 * 1024 * 1024

MEMORY_FILE_PREFIX: Final = "memory."
_TEMP_SUFFIX: Final = ".tmp"

#: Serial for the scratch filename, so two writers in one process cannot publish
#: each other's half-written bytes through a shared ``.tmp``.
_temp_serial = count()


class MemoryStoreError(OSError):
    """The memory file could not be read or written as a complete document."""


class MemoryStore:
    """Loads and saves one :class:`SaveMemory` per save id."""

    def __init__(self, root: Path, *, config: MemoryConfig = DEFAULT_MEMORY_CONFIG) -> None:
        self._root = Path(root)
        self._config = config

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, save_id: str) -> Path:
        """Where this save's memory lives.

        Raises:
            ValueError: when *save_id* is empty. There is no "default" memory
                file: a memory that is not scoped to a save is exactly the thing
                that leaks one world's containers into another.
        """
        if not save_id:
            raise ValueError("save_id must not be empty; memory is always save-scoped")
        return self._root / f"{MEMORY_FILE_PREFIX}{save_scope(save_id)}.json"

    def load(self, save_id: str) -> SaveMemory:
        """Read this save's memory, migrating it forward if it is older.

        A missing file is the ordinary first-run state and yields an empty
        memory. Every other failure is raised: substituting an empty memory for
        a file that exists but cannot be read would silently forget the user's
        reservations, and the agent would then consume what they set aside.

        Raises:
            MemoryStoreError: when the file exists but cannot be read as one
                complete JSON object, or is over :data:`MAX_MEMORY_BYTES`.
            MemorySchemaError: when its schema version is unsupported.
            MemoryScopeError: when it belongs to a different save.
            MemoryCapacityError: when it holds more of the user's own records
                than the configuration allows; those are never silently trimmed.
        """
        path = self.path_for(save_id)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return SaveMemory(save_id, config=self._config)
        except OSError as exc:
            raise MemoryStoreError(f"{path}: cannot stat memory ({exc.strerror or exc})") from exc
        if size > MAX_MEMORY_BYTES:
            raise MemoryStoreError(f"{path}: {size} bytes exceeds {MAX_MEMORY_BYTES}")
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise MemoryStoreError(f"{path}: cannot read memory ({exc})") from exc
        try:
            document: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MemoryStoreError(f"{path}: malformed JSON at byte {exc.pos}") from exc
        if not isinstance(document, dict):
            raise MemoryStoreError(f"{path}: expected a JSON object, got {type(document).__name__}")
        migrated = migrate_document(document)
        return SaveMemory.from_document(migrated, save_id=save_id, config=self._config)

    def save(self, memory: SaveMemory) -> int:
        """Write *memory* atomically. Returns the number of bytes written.

        Raises:
            MemoryStoreError: when the document exceeds :data:`MAX_MEMORY_BYTES`
                or the filesystem refuses the write.
        """
        path = self.path_for(memory.save_id)
        document = memory.to_document(schema_version=MEMORY_SCHEMA_VERSION)
        body = json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        encoded = body.encode("utf-8")
        if len(encoded) > MAX_MEMORY_BYTES:
            raise MemoryStoreError(
                f"memory document of {len(encoded)} bytes exceeds {MAX_MEMORY_BYTES}"
            )
        temp = path.with_name(f"{path.name}{_TEMP_SUFFIX}.{os.getpid()}.{next(_temp_serial)}")
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            with temp.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except OSError as exc:
            # A unique scratch name is only bounded if a failed write takes its
            # file with it; otherwise every crashed attempt leaves one forever.
            # The removal is itself guarded: when the directory is what is
            # broken, unlink raises too, and letting that escape would replace
            # the real diagnosis with a confusing one about a temporary file.
            try:
                temp.unlink(missing_ok=True)
            except OSError as cleanup_failure:
                raise MemoryStoreError(
                    f"{path}: cannot write memory ({exc.strerror or exc}); the partial file "
                    f"{temp.name} could not be removed either "
                    f"({cleanup_failure.strerror or cleanup_failure})"
                ) from exc
            raise MemoryStoreError(f"{path}: cannot write memory ({exc.strerror or exc})") from exc
        return len(encoded)

    def forget(self, save_id: str) -> bool:
        """Delete this save's memory. Returns whether there was one.

        The only deletion path, and it takes a save id rather than a path, so
        nothing can ask the store to remove a file it did not name itself.
        """
        path = self.path_for(save_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise MemoryStoreError(f"{path}: cannot remove memory ({exc.strerror or exc})") from exc
        return True
