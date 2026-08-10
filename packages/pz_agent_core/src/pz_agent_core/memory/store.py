"""One save's memory: bounded on write, scoped by construction.

The store is deliberately not a cache with a cleanup task. Every mutator prunes
what has expired and evicts what overflows *before* it returns, so the invariant
"this object is within its bounds" holds after every single call rather than
after whatever process was supposed to run nightly. A retention policy that
depends on a sweeper nobody runs is not a policy.

Two collections are exempt from eviction and refuse instead: reservations and
preferences. Everything else here is the agent's own observation and can be
re-derived by looking again; those two are the user's words. Silently dropping
the oldest reservation to make room for a new one would mean the agent one day
consumes the thing it was told to leave alone and reports no error at all —
so the store raises and the caller has to tell the user something has to give.

Scoping is by construction: a :class:`SaveMemory` is created *for* a save id and
:meth:`SaveMemory.from_document` refuses a document belonging to another one. A
world reference from a different save names a place in a different world, and
acting on it is acting on the wrong object.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, TypeVar

from ..observation.compact import save_scope
from ..protocol import ContainerKind
from .model import (
    MAX_DETAIL_LEN,
    MAX_FULL_TYPE_LEN,
    MAX_LABEL_LEN,
    MAX_PREFERENCE_KEY_LEN,
    MAX_PREFERENCE_VALUE_LEN,
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

_Record = TypeVar("_Record")

__all__ = [
    "CEILINGS",
    "DEFAULT_MEMORY_CONFIG",
    "MemoryCapacityError",
    "MemoryConfig",
    "MemoryScopeError",
    "SaveMemory",
]


class MemoryCapacityError(RuntimeError):
    """A user-authored collection is full and refuses to drop an entry."""


class MemoryScopeError(ValueError):
    """A document belongs to a different save than the memory loading it."""


#: Hard ceilings on :class:`MemoryConfig`. Configuration may lower a bound but
#: never raise it past these — "bounded everything" is not a user preference,
#: and a memory file is read into a planner's context window.
CEILINGS: Final[Mapping[str, int]] = {
    "max_containers": 512,
    "max_safe_zones": 64,
    "max_failed_paths": 256,
    "max_reservations": 256,
    "max_preferences": 128,
    "max_tasks": 256,
    "max_categories_per_container": 32,
}


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    """Per-collection caps and the retention window.

    Each cap documents its own eviction rule:

    * ``max_containers`` — least-recently-*seen* container is evicted. Recency
      of sighting, not of inspection: a shelf seen this minute is where the
      character is now, and that is the fact worth keeping.
    * ``max_safe_zones``, ``max_failed_paths``, ``max_tasks`` — oldest entry
      first. All three are histories, and the oldest is the least useful.
    * ``max_reservations``, ``max_preferences`` — nothing is evicted; a write
      past the cap raises :class:`MemoryCapacityError`.
    * ``max_categories_per_container`` — the category list of one container is
      sorted and truncated, so the same shelf always yields the same prefix.
    """

    max_containers: int = 128
    max_safe_zones: int = 16
    max_failed_paths: int = 64
    max_reservations: int = 64
    max_preferences: int = 32
    max_tasks: int = 64
    max_categories_per_container: int = 12
    #: Age past which an *observed* fact is dropped on the next write. Thirty
    #: days of wall time: long enough to survive a break from a save, short
    #: enough that a container list does not describe a world that has since
    #: been looted by something else.
    retention_ms: int = 30 * 24 * 60 * 60 * 1000

    def __post_init__(self) -> None:
        for name, ceiling in CEILINGS.items():
            value = getattr(self, name)
            if not 1 <= value <= ceiling:
                raise ValueError(f"{name} must be within 1..{ceiling}, got {value}")
        if self.retention_ms <= 0:
            raise ValueError(f"retention_ms must be positive, got {self.retention_ms}")


DEFAULT_MEMORY_CONFIG: Final = MemoryConfig()


class SaveMemory:
    """Everything the agent remembers about one save.

    Mutators take ``now_ms`` explicitly. Nothing in this class reads a clock:
    the same sequence of calls with the same timestamps always produces the same
    memory, which is what makes eviction and retention testable without sleeping.
    """

    def __init__(self, save_id: str, *, config: MemoryConfig = DEFAULT_MEMORY_CONFIG) -> None:
        if not save_id:
            raise MemoryScopeError("a memory is always scoped to a save; save_id is empty")
        self._save_id = save_id
        self._scope = save_scope(save_id)
        self._config = config
        self._containers: dict[str, KnownContainer] = {}
        self._safe_zones: dict[str, SafeZone] = {}
        self._failed_paths: dict[str, FailedPath] = {}
        self._reservations: dict[str, Reservation] = {}
        self._preferences: dict[str, Preference] = {}
        self._tasks: list[TaskRecord] = []
        self._home: Square | None = None
        self._home_set_at_ms: int = 0
        self._evicted = 0

    # -- identity ----------------------------------------------------------

    @property
    def save_id(self) -> str:
        return self._save_id

    @property
    def scope(self) -> str:
        """Non-reversible handle for this save; what goes in the file and its name."""
        return self._scope

    @property
    def config(self) -> MemoryConfig:
        return self._config

    @property
    def evicted_count(self) -> int:
        """How many records were dropped to stay in bounds. A counter, not a list."""
        return self._evicted

    # -- containers --------------------------------------------------------

    def note_container(
        self,
        *,
        container_ref: str,
        kind: ContainerKind,
        name: str,
        now_ms: int,
        categories: Iterable[str] = (),
        inspected: bool = False,
        content_revision: str = "",
        item_count: int = -1,
    ) -> KnownContainer:
        """Record that a container was seen, and optionally that it was opened.

        ``content_revision`` and ``item_count`` are only meaningful with
        ``inspected=True`` — they describe what an enumeration found, and a
        sighting enumerates nothing. On the sighting path they are carried
        forward from the previous record exactly as the categories are; on the
        inspection path they *replace* what was remembered, defaults included,
        because the new look is the newer fact and repeating a stale digest
        would answer "unchanged" about contents nobody just checked.

        Raises:
            MemoryValueError: when *container_ref* is not a world container.
                Anything on the player travels with them and its reference
                embeds a runtime id that a reload invalidates.
        """
        record = KnownContainer.observed(
            container_ref=container_ref,
            kind=kind,
            name=name,
            seen_at_ms=now_ms,
            inspected_at_ms=now_ms if inspected else 0,
            categories=categories,
            category_limit=self._config.max_categories_per_container,
            content_revision=content_revision,
            item_count=item_count,
        )
        previous = self._containers.get(record.tail)
        if previous is not None and not inspected:
            # A sighting must not erase what a previous inspection learned: the
            # categories, the inspection time, the content revision and the
            # item count are facts about the contents, and walking past the
            # shelf did not disprove them.
            record = KnownContainer(
                tail=record.tail,
                kind=record.kind,
                label=record.label or previous.label,
                square=record.square,
                last_seen_ms=max(record.last_seen_ms, previous.last_seen_ms),
                last_inspected_ms=previous.last_inspected_ms,
                categories=previous.categories,
                content_revision=previous.content_revision,
                item_count=previous.item_count,
            )
        self._containers[record.tail] = record
        self._prune(now_ms)
        return record

    def containers(self) -> tuple[KnownContainer, ...]:
        """Known containers, most recently seen first."""
        return tuple(sorted(self._containers.values(), key=lambda c: (-c.last_seen_ms, c.tail)))

    def container(self, container_ref: str) -> KnownContainer | None:
        """The record for a container named by a *this-session* reference."""
        try:
            tail = container_tail(container_ref)
        except MemoryValueError:
            return None
        return self._containers.get(tail)

    def container_unchanged(self, tail: str, revision: str) -> bool:
        """True only when *revision* matches a *recorded* enumeration of *tail*.

        Three-state honesty collapsed into the only safe boolean: a container
        never remembered, or remembered but never enumerated (empty stored
        revision), or offered an empty *revision*, all answer False — "I do not
        know" must send the loot planner back to look, never spare it the trip.
        True is reserved for two non-empty digests agreeing, which is
        :func:`~pz_agent_core.memory.model.content_revision_of`'s change-detector
        promise: a changed world almost never keeps its revision.
        """
        if not revision:
            return False
        record = self._containers.get(tail)
        if record is None or not record.content_revision:
            return False
        return record.content_revision == revision

    def uninspected_tails(self) -> Iterator[str]:
        """Tails of containers seen but never enumerated, most recently seen first.

        The loot planner's worklist: places the agent has walked past without
        ever counting what is inside. ``item_count < 0`` is the "never
        enumerated" marker (:class:`~pz_agent_core.memory.model.KnownContainer`),
        so a container opened and found empty does not reappear here. Bounded by
        ``max_containers`` like everything the iterator walks.
        """
        return (record.tail for record in self.containers() if record.item_count < 0)

    # -- home, safe zones --------------------------------------------------

    def set_home(self, square: Square, now_ms: int) -> None:
        """Set the home point. §7.10 keeps it on save scope, which is here."""
        self._home = square
        self._home_set_at_ms = now_ms
        self._prune(now_ms)

    def home_point(self) -> Square | None:
        return self._home

    @property
    def home_set_at_ms(self) -> int:
        return self._home_set_at_ms

    def add_safe_zone(
        self, *, key: str, centre: Square, radius: int, label: str, now_ms: int
    ) -> SafeZone:
        zone = SafeZone(
            key=key[:MAX_LABEL_LEN],
            centre=centre,
            radius=radius,
            label=label[:MAX_LABEL_LEN],
            recorded_at_ms=now_ms,
        )
        self._safe_zones[zone.key] = zone
        self._evict_oldest(self._safe_zones, self._config.max_safe_zones, _zone_age)
        self._prune(now_ms)
        return zone

    def safe_zones(self) -> tuple[SafeZone, ...]:
        return tuple(sorted(self._safe_zones.values(), key=lambda z: (-z.recorded_at_ms, z.key)))

    def is_in_safe_zone(self, square: Square) -> bool:
        return any(zone.contains(square) for zone in self._safe_zones.values())

    # -- failed paths ------------------------------------------------------

    def note_failed_path(
        self, *, origin: Square, target: Square, reason: str, now_ms: int
    ) -> FailedPath:
        """Record a route that failed, merging repeats into one counter."""
        candidate = FailedPath(
            origin=origin,
            target=target,
            reason=reason[:MAX_DETAIL_LEN] or "unspecified",
            attempts=1,
            last_failed_ms=now_ms,
        )
        previous = self._failed_paths.get(candidate.key)
        if previous is not None:
            candidate = FailedPath(
                origin=origin,
                target=target,
                reason=candidate.reason,
                attempts=previous.attempts + 1,
                last_failed_ms=now_ms,
            )
        self._failed_paths[candidate.key] = candidate
        self._evict_oldest(self._failed_paths, self._config.max_failed_paths, _path_age)
        self._prune(now_ms)
        return candidate

    def failed_paths(self) -> tuple[FailedPath, ...]:
        return tuple(sorted(self._failed_paths.values(), key=lambda p: (-p.last_failed_ms, p.key)))

    def path_failures(self, origin: Square, target: Square) -> int:
        """How often this exact route has already failed."""
        key = FailedPath(
            origin=origin, target=target, reason="probe", attempts=1, last_failed_ms=0
        ).key
        record = self._failed_paths.get(key)
        return 0 if record is None else record.attempts

    # -- reservations (user-authored) --------------------------------------

    def reserve(self, *, full_type: str, reason: str, now_ms: int) -> Reservation:
        """Set an item type aside.

        Raises:
            MemoryCapacityError: when the reservation list is full. Evicting one
                to make room would let the agent consume something the user
                protected while reporting nothing.
        """
        if not full_type or len(full_type) > MAX_FULL_TYPE_LEN:
            raise MemoryValueError(f"full_type must be 1..{MAX_FULL_TYPE_LEN} characters")
        if (
            full_type not in self._reservations
            and len(self._reservations) >= self._config.max_reservations
        ):
            raise MemoryCapacityError(
                f"{self._config.max_reservations} reservations are already held; release one "
                "before adding another — I will not drop one of yours to make room"
            )
        record = Reservation(
            full_type=full_type, reason=reason[:MAX_DETAIL_LEN], created_at_ms=now_ms
        )
        self._reservations[full_type] = record
        self._prune(now_ms)
        return record

    def release(self, full_type: str) -> bool:
        """Drop a reservation. Returns whether there was one."""
        return self._reservations.pop(full_type, None) is not None

    def reservations(self) -> tuple[Reservation, ...]:
        return tuple(sorted(self._reservations.values(), key=lambda r: r.full_type))

    def reserves_item(self, full_type: str, /) -> bool:
        """True when the user set this item type aside.

        Positional-only so the structural protocol the autonomy gate declares
        matches this method exactly, with no adapter in between.
        """
        return full_type in self._reservations

    # -- preferences (user-authored) ---------------------------------------

    def set_preference(self, *, key: str, value: str, now_ms: int) -> Preference:
        """Record a save-scoped user setting.

        Raises:
            MemoryCapacityError: when the preference list is full, for the same
                reason reservations refuse.
        """
        if not key or len(key) > MAX_PREFERENCE_KEY_LEN:
            raise MemoryValueError(f"a preference key must be 1..{MAX_PREFERENCE_KEY_LEN} chars")
        if key not in self._preferences and len(self._preferences) >= self._config.max_preferences:
            raise MemoryCapacityError(
                f"{self._config.max_preferences} preferences are already stored; clear one first"
            )
        record = Preference(key=key, value=value[:MAX_PREFERENCE_VALUE_LEN], set_at_ms=now_ms)
        self._preferences[key] = record
        self._prune(now_ms)
        return record

    def preference(self, key: str) -> str | None:
        record = self._preferences.get(key)
        return None if record is None else record.value

    def preferences(self) -> tuple[Preference, ...]:
        return tuple(sorted(self._preferences.values(), key=lambda p: p.key))

    # -- task history ------------------------------------------------------

    def record_task(
        self, *, key: str, outcome: TaskOutcome, now_ms: int, detail: str = ""
    ) -> TaskRecord:
        record = TaskRecord(
            key=key[:MAX_LABEL_LEN],
            outcome=outcome,
            finished_at_ms=now_ms,
            detail=detail[:MAX_DETAIL_LEN],
        )
        self._tasks.append(record)
        self._prune(now_ms)
        return record

    def task_history(self) -> tuple[TaskRecord, ...]:
        """Recorded tasks, newest first."""
        return tuple(reversed(self._tasks))

    def task_failures(self, key: str) -> int:
        """How many of the remembered attempts at *key* did not succeed."""
        return sum(
            1 for t in self._tasks if t.key == key and t.outcome is not TaskOutcome.SUCCEEDED
        )

    # -- bounds ------------------------------------------------------------

    def _prune(self, now_ms: int) -> None:
        """Apply retention and every cap. Called by every mutator, always."""
        cutoff = now_ms - self._config.retention_ms
        self._drop_expired(cutoff)
        self._evict_oldest(self._containers, self._config.max_containers, _container_age)
        self._evict_oldest(self._safe_zones, self._config.max_safe_zones, _zone_age)
        self._evict_oldest(self._failed_paths, self._config.max_failed_paths, _path_age)
        overflow = len(self._tasks) - self._config.max_tasks
        if overflow > 0:
            del self._tasks[:overflow]
            self._evicted += overflow

    def _drop_expired(self, cutoff: int) -> None:
        """Forget observed facts older than the retention window.

        Only observed ones. A reservation, a preference, a safe zone and the home
        point are things the user decided; they do not go stale because nobody
        played for a month.
        """
        stale_containers = [t for t, c in self._containers.items() if c.last_seen_ms < cutoff]
        for tail in stale_containers:
            del self._containers[tail]
        stale_paths = [k for k, p in self._failed_paths.items() if p.last_failed_ms < cutoff]
        for key in stale_paths:
            del self._failed_paths[key]
        kept = [t for t in self._tasks if t.finished_at_ms >= cutoff]
        dropped = len(self._tasks) - len(kept)
        self._tasks = kept
        self._evicted += len(stale_containers) + len(stale_paths) + dropped

    def _evict_oldest(
        self,
        collection: dict[str, _Record],
        cap: int,
        age_key: Callable[[_Record], int],
    ) -> None:
        """Drop the oldest records until *collection* fits its cap.

        Ordered once, not once per eviction. The record removed by a repeated
        ``min`` never changes as its neighbours go, so a single sort drops
        exactly the same records in exactly the same order — and it costs
        O(n log n) rather than O(n * evicted).

        The difference is not academic, because the number this is handed is not
        the number the writer bounds. On load it is whatever the document held,
        and a memory file is bounded in *bytes*
        (:data:`~pz_agent_core.memory.persistence.MAX_MEMORY_BYTES`), which the
        smallest legal container record turns into tens of thousands of entries.
        Quadratic eviction made a file the store accepts cost minutes of CPU.
        """
        overflow = len(collection) - cap
        if overflow <= 0:
            return
        oldest_first = sorted(collection, key=lambda key: (age_key(collection[key]), key))
        for key in oldest_first[:overflow]:
            del collection[key]
        self._evicted += overflow

    def _latest_timestamp(self) -> int:
        """The newest moment the loaded document knows about.

        Retention on load is measured against the file's own newest fact, not
        against a wall clock this module refuses to read. Using "now" would make
        loading the memory of a save nobody has played for a year delete all of
        it before the caller could look at it — and the caller may be a
        diagnostic tool that only wants to look.
        """
        stamps = [c.last_seen_ms for c in self._containers.values()]
        stamps.extend(p.last_failed_ms for p in self._failed_paths.values())
        stamps.extend(t.finished_at_ms for t in self._tasks)
        return max(stamps, default=0)

    # -- serialisation -----------------------------------------------------

    def to_document(self, *, schema_version: int) -> dict[str, Any]:
        """The JSON body written to disk, minus the envelope's own fields."""
        document: dict[str, Any] = {
            "schema_version": schema_version,
            "save_scope": self._scope,
            "containers": [c.to_dict() for c in self.containers()],
            "safe_zones": [z.to_dict() for z in self.safe_zones()],
            "failed_paths": [p.to_dict() for p in self.failed_paths()],
            "reservations": [r.to_dict() for r in self.reservations()],
            "preferences": [p.to_dict() for p in self.preferences()],
            "tasks": [t.to_dict() for t in self._tasks],
        }
        if self._home is not None:
            document["home"] = {
                "square": self._home.to_dict(),
                "set_at_ms": self._home_set_at_ms,
            }
        return document

    @classmethod
    def from_document(
        cls,
        document: Mapping[str, Any],
        *,
        save_id: str,
        config: MemoryConfig = DEFAULT_MEMORY_CONFIG,
    ) -> SaveMemory:
        """Rebuild a memory from a *migrated* document.

        Raises:
            MemoryScopeError: when the document belongs to another save. The
                caller is not offered a "load anyway" flag: every world
                reference inside would name a place in a different world.
            MemoryValueError: when a record is malformed.
            MemoryCapacityError: when it holds more reservations or preferences
                than the configuration allows. Those two are the user's own
                words, so the same rule applies on the way in as on the way out:
                the store refuses rather than picking which of them to drop.
        """
        memory = cls(save_id, config=config)
        stored_scope = document.get("save_scope")
        if not isinstance(stored_scope, str) or not stored_scope:
            raise MemoryScopeError("the stored memory does not say which save it belongs to")
        if stored_scope != memory.scope:
            raise MemoryScopeError(
                f"the stored memory belongs to save {stored_scope}, not {memory.scope}; "
                "every world reference in it names a place in a different world"
            )
        memory._load_collections(document)
        memory._load_home(document)
        # Caps are applied on load as well as on write: the file may have been
        # produced by a build with looser bounds, and "bounded" has to mean
        # bounded for the object in memory, not only for the writer. The two
        # user-authored collections cannot be pruned into shape, so they are
        # checked separately and refuse.
        memory._check_user_collections()
        memory._prune(memory._latest_timestamp())
        return memory

    def _check_user_collections(self) -> None:
        """Refuse a document holding more of the user's own records than fit.

        Evicting here would be the silent-drop this store exists to avoid, only
        worse: the caller asked to read what the user said, and would be handed
        a subset that looks complete. Raising names both numbers so the fix —
        raise the cap, or release something — is the caller's to make.
        """
        for label, held, cap in (
            ("reservations", len(self._reservations), self._config.max_reservations),
            ("preferences", len(self._preferences), self._config.max_preferences),
        ):
            if held > cap:
                raise MemoryCapacityError(
                    f"the stored memory holds {held} {label} and this build is configured for "
                    f"{cap}; raise max_{label} (up to {CEILINGS[f'max_{label}']}) or remove some "
                    "before loading — I will not choose which of yours to drop"
                )

    def _load_collections(self, document: Mapping[str, Any]) -> None:
        for record in _records(document, "containers"):
            container = KnownContainer.from_dict(
                record, category_limit=self._config.max_categories_per_container
            )
            self._containers[container.tail] = container
        for record in _records(document, "safe_zones"):
            zone = SafeZone.from_dict(record)
            self._safe_zones[zone.key] = zone
        for record in _records(document, "failed_paths"):
            path = FailedPath.from_dict(record)
            self._failed_paths[path.key] = path
        for record in _records(document, "reservations"):
            reservation = Reservation.from_dict(record)
            self._reservations[reservation.full_type] = reservation
        for record in _records(document, "preferences"):
            preference = Preference.from_dict(record)
            self._preferences[preference.key] = preference
        self._tasks = [TaskRecord.from_dict(record) for record in _records(document, "tasks")]
        self._tasks.sort(key=lambda t: t.finished_at_ms)

    def _load_home(self, document: Mapping[str, Any]) -> None:
        home = document.get("home")
        if home is None:
            return
        if not isinstance(home, Mapping):
            raise MemoryValueError("home must be an object")
        square = home.get("square")
        if not isinstance(square, Mapping):
            raise MemoryValueError("home.square must be an object")
        self._home = Square.from_dict(square, field_name="home.square")
        set_at = home.get("set_at_ms", 0)
        if isinstance(set_at, bool) or not isinstance(set_at, int) or set_at < 0:
            raise MemoryValueError("home.set_at_ms must be a non-negative integer")
        self._home_set_at_ms = set_at


def _records(document: Mapping[str, Any], key: str) -> Sequence[Mapping[str, Any]]:
    value = document.get(key, ())
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MemoryValueError(f"{key} must be an array, got {type(value).__name__}")
    out: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise MemoryValueError(f"{key}[] must hold objects, got {type(item).__name__}")
        out.append(item)
    return out


def _container_age(record: KnownContainer) -> int:
    return record.last_seen_ms


def _zone_age(record: SafeZone) -> int:
    return record.recorded_at_ms


def _path_age(record: FailedPath) -> int:
    return record.last_failed_ms
