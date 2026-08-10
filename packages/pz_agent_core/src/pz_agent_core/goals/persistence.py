"""The goal channel on disk: one file, written whole or not at all.

Goals are *session work*, not save knowledge, which is why this is one
``goals.json`` in the sidecar's state directory — beside the memory directory,
never inside it and never per save. A goal the user stated binds the sidecar
process that admitted it, whatever save the game is on, and a restart of that
process is exactly the event this file exists to survive: the pending backlog
comes back pending under its original ids and idempotency digests, the goal
that was active comes back as an honest terminal record (``FAILED`` /
``SESSION_TERMINATED`` — the queue's :meth:`~.queue.GoalQueue.restore` owns
that reading), and the recent terminal history comes back so a client polling
a finished goal's id still gets its answer.

The write discipline is :mod:`pz_agent_core.memory.persistence`'s, restated
rather than imported because the two stores share no path and deliberately no
code that could couple their formats: temporary file with the pid and a serial
in its name, flush, ``fsync``, ``os.replace``, scratch removed on failure. A
reader sees either the previous complete channel or the new complete channel,
never a prefix.

Everything read back is treated as untrusted — the file lives on the user's
disk and anything can have edited it. The byte cap is checked before the parse,
every string resolves through the model's own closed doors (:func:`parse_kind`,
:func:`parse_skill`, :func:`parse_scope`, the enum constructors under a guard
that never echoes the failing value), and :class:`~.model.GoalRecord`'s own
invariants are the final validator: a record this module returns is a record
the queue could have held. A document that fails any of it raises
:class:`GoalDocumentError`; the caller's honest move is to set the file aside
as evidence and start empty, never to guess.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from itertools import count
from pathlib import Path
from typing import Any, Final

from ..protocol import ReasonCode
from .model import (
    PARAM_NAMES,
    GoalBudget,
    GoalParams,
    GoalRecord,
    GoalState,
    parse_kind,
    parse_scope,
    parse_skill,
)
from .queue import GoalSnapshot

__all__ = [
    "GOALS_FILE_NAME",
    "GOALS_QUARANTINE_NAME",
    "GOALS_SCHEMA_VERSION",
    "MAX_GOAL_FILE_BYTES",
    "MAX_PERSISTED_TERMINAL",
    "MIN_SUPPORTED_GOALS_SCHEMA",
    "GoalDocumentError",
    "GoalStore",
    "GoalStoreError",
    "snapshot_from_document",
    "snapshot_to_document",
]

#: The one file. Not save-scoped (see the module docstring) and not named by
#: anything a caller supplies, so there is nothing to traverse with.
GOALS_FILE_NAME: Final = "goals.json"

#: Where an unreadable file is set aside. A fixed name rather than a serial,
#: so a channel that keeps being corrupted keeps exactly one corpse — the
#: newest — instead of an unbounded morgue.
GOALS_QUARANTINE_NAME: Final = "goals.json.corrupt"

#: The channel holds at most ``max_open`` open goals plus
#: :data:`MAX_PERSISTED_TERMINAL` terminal records, each a bounded record —
#: well under 256 KiB. Anything larger is corrupt or hostile and is refused
#: before it is parsed, not after.
MAX_GOAL_FILE_BYTES: Final = 256 * 1024

#: Terminal records the document keeps, newest last. Enough for a client to
#: resolve a recently finished goal id across a restart, and deliberately
#: fewer than the queue's own ``max_remembered``: history that only ever
#: existed to answer "what just happened" does not compound across restarts.
MAX_PERSISTED_TERMINAL: Final = 16

#: Schema this build writes.
#:
#: * 1 — first on-disk layout: ``open`` (pending and active records, admission
#:   order) and ``terminal`` (recent finished records, oldest first).
GOALS_SCHEMA_VERSION: Final = 1

#: Oldest schema this build still upgrades; below it the document is refused
#: rather than migrated through code nobody exercises.
MIN_SUPPORTED_GOALS_SCHEMA: Final = 1

_TEMP_SUFFIX: Final = ".tmp"

#: Serial for the scratch filename, so two writers in one process cannot
#: publish each other's half-written bytes through a shared ``.tmp``.
_temp_serial = count()


class GoalStoreError(OSError):
    """The goals file could not be read or written as a complete document."""


class GoalDocumentError(ValueError):
    """The goals file parsed as JSON but is not a channel this build wrote.

    Deliberately never quotes a value from the document: the file is untrusted
    input, and a diagnostic that echoed its bytes would hand whoever edited it
    a line in the sidecar's log.
    """


# --------------------------------------------------------------------------
# document <-> snapshot
# --------------------------------------------------------------------------


def snapshot_to_document(snapshot: GoalSnapshot) -> dict[str, Any]:
    """*snapshot* as the versioned document :class:`GoalStore` writes.

    Every open record is kept — losing a pending goal would silently drop work
    the user asked for — and terminal history is capped at
    :data:`MAX_PERSISTED_TERMINAL`, newest kept, which is the queue's own
    eviction order applied at the file boundary.
    """
    open_records = sorted(
        (record for record in snapshot.records if record.is_open),
        key=lambda record: record.sequence,
    )
    terminal = [record for record in snapshot.records if not record.is_open]
    return {
        "schema_version": GOALS_SCHEMA_VERSION,
        "open": [_record_to_document(record) for record in open_records],
        "terminal": [_record_to_document(record) for record in terminal[-MAX_PERSISTED_TERMINAL:]],
    }


def snapshot_from_document(document: Mapping[str, Any]) -> GoalSnapshot:
    """The stored channel, re-validated record by record.

    The returned snapshot's records are ordered terminal-then-open, which is
    the retention order a restored queue wants: history first (evictable,
    oldest first), the backlog after it. Its ``revision`` is zero — a revision
    counts one process's mutations and does not survive it.

    Raises:
        GoalDocumentError: the document's schema version is missing, older
            than :data:`MIN_SUPPORTED_GOALS_SCHEMA` or newer than this build;
            a list is over its cap; or any record fails the model's own
            validation. No message quotes a byte of the document.
    """
    working = _migrate(document)
    open_docs = _as_list(working.get("open", []), name="open")
    terminal_docs = _as_list(working.get("terminal", []), name="terminal")
    if len(terminal_docs) > MAX_PERSISTED_TERMINAL:
        raise GoalDocumentError(
            f"the stored channel holds {len(terminal_docs)} terminal record(s), over the cap of "
            f"{MAX_PERSISTED_TERMINAL} this build ever writes"
        )
    records: list[GoalRecord] = []
    for kept, docs in (("terminal", terminal_docs), ("open", open_docs)):
        for index, entry in enumerate(docs):
            if not isinstance(entry, dict):
                raise GoalDocumentError(f"{kept}[{index}] is not a JSON object")
            record = _record_from_document(entry, where=f"{kept}[{index}]")
            if kept == "terminal" and record.is_open:
                raise GoalDocumentError(f"{kept}[{index}] claims to be terminal and is not")
            if kept == "open" and not record.is_open:
                raise GoalDocumentError(f"{kept}[{index}] claims to be open and is not")
            records.append(record)
    return GoalSnapshot(records=tuple(records), revision=0)


def _migrate(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return *document* at :data:`GOALS_SCHEMA_VERSION`, or refuse.

    The same seam :mod:`pz_agent_core.memory.migrations` keeps: every version
    from :data:`MIN_SUPPORTED_GOALS_SCHEMA` up has a named step to the next
    one, a future version is refused rather than partially read, and the chain
    is bounded by the version arithmetic. With one schema in existence the
    chain is empty; the seam exists so that adding schema 2 is a migration
    function and a constant, not a file-format fork.
    """
    if "schema_version" not in document:
        raise GoalDocumentError(
            "the stored channel declares no schema_version; refusing to guess which build wrote it"
        )
    version = document["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise GoalDocumentError("schema_version must be an integer")
    if version > GOALS_SCHEMA_VERSION:
        raise GoalDocumentError(
            f"the stored channel is schema {version}; this build reads at most "
            f"{GOALS_SCHEMA_VERSION}. A newer pz-agent wrote it"
        )
    if version < MIN_SUPPORTED_GOALS_SCHEMA:
        raise GoalDocumentError(
            f"the stored channel is schema {version}; this build upgrades nothing below "
            f"{MIN_SUPPORTED_GOALS_SCHEMA}"
        )
    working = dict(document)
    for _ in range(GOALS_SCHEMA_VERSION - MIN_SUPPORTED_GOALS_SCHEMA):
        if version >= GOALS_SCHEMA_VERSION:
            break
        step = _MIGRATIONS.get(version)
        if step is None:
            raise GoalDocumentError(
                f"no migration is registered from goals schema {version}; the chain is broken"
            )
        working = step(working)
        version += 1
        working["schema_version"] = version
    return working


#: version -> the function that turns it into version + 1. Empty until a
#: second schema exists; see :func:`_migrate`.
_MIGRATIONS: Final[dict[int, Any]] = {}


def _record_to_document(record: GoalRecord) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for name in PARAM_NAMES:
        value = getattr(record.params, name)
        if value is None:
            continue
        # The two enum-typed parameters cross to their wire values; everything
        # else the model already constrained to a JSON scalar.
        params[name] = value.value if hasattr(value, "value") else value
    return {
        "goal_id": record.goal_id,
        "kind": record.kind.value,
        "params": params,
        "budget": {
            "max_wall_ms": record.budget.max_wall_ms,
            "max_steps": record.budget.max_steps,
            "pending_ttl_ms": record.budget.pending_ttl_ms,
        },
        "key_digest": record.key_digest,
        "sequence": record.sequence,
        "state": record.state.value,
        "submitted_at_ms": record.submitted_at_ms,
        "started_at_ms": record.started_at_ms,
        "finished_at_ms": record.finished_at_ms,
        "steps_used": record.steps_used,
        "reason_code": None if record.reason_code is None else record.reason_code.value,
        "evidence_keys": list(record.evidence_keys),
        "detail": record.detail,
    }


def _record_from_document(entry: Mapping[str, Any], *, where: str) -> GoalRecord:
    """One stored record, through every door the live channel uses.

    ``ValueError`` from the model's own constructors is re-raised as
    :class:`GoalDocumentError` naming *where* — the model's messages are built
    from constants and numbers, so relaying them keeps the no-echo rule.
    """
    kind = parse_kind(_string(entry, "kind", where=where))
    if kind is None:
        raise GoalDocumentError(f"{where}: kind is not a goal kind this build carries")
    state_token = _string(entry, "state", where=where)
    try:
        state = GoalState(state_token)
    except ValueError:
        # The enum's own message quotes the value; this one does not.
        raise GoalDocumentError(f"{where}: state is not a goal state this build knows") from None
    reason_raw = entry.get("reason_code")
    reason: ReasonCode | None = None
    if reason_raw is not None:
        if not isinstance(reason_raw, str):
            raise GoalDocumentError(f"{where}: reason_code must be a string or null")
        try:
            reason = ReasonCode(reason_raw)
        except ValueError:
            raise GoalDocumentError(
                f"{where}: reason_code is not a reason this build knows"
            ) from None
    evidence_raw = entry.get("evidence_keys", [])
    if not isinstance(evidence_raw, list) or not all(
        isinstance(item, str) for item in evidence_raw
    ):
        raise GoalDocumentError(f"{where}: evidence_keys must be an array of strings")
    detail = entry.get("detail", "")
    if not isinstance(detail, str):
        raise GoalDocumentError(f"{where}: detail must be a string")
    budget_doc = entry.get("budget")
    if not isinstance(budget_doc, dict):
        raise GoalDocumentError(f"{where}: budget must be a JSON object")
    try:
        budget = GoalBudget(
            max_wall_ms=_whole(budget_doc, "max_wall_ms", where=f"{where}.budget"),
            max_steps=_whole(budget_doc, "max_steps", where=f"{where}.budget"),
            pending_ttl_ms=_whole(budget_doc, "pending_ttl_ms", where=f"{where}.budget"),
        )
        record = GoalRecord(
            goal_id=_string(entry, "goal_id", where=where),
            kind=kind,
            params=_params_from_document(entry.get("params", {}), where=where),
            budget=budget,
            key_digest=_string(entry, "key_digest", where=where),
            sequence=_whole(entry, "sequence", where=where),
            state=state,
            submitted_at_ms=_whole(entry, "submitted_at_ms", where=where),
            started_at_ms=_optional_whole(entry, "started_at_ms", where=where),
            finished_at_ms=_optional_whole(entry, "finished_at_ms", where=where),
            steps_used=_whole(entry, "steps_used", where=where),
            reason_code=reason,
            evidence_keys=tuple(evidence_raw),
            detail=detail,
        )
    except ValueError as exc:
        raise GoalDocumentError(f"{where}: {exc}") from None
    return record


def _params_from_document(raw: Any, *, where: str) -> GoalParams:
    if not isinstance(raw, dict):
        raise GoalDocumentError(f"{where}: params must be a JSON object")
    unknown = set(raw) - set(PARAM_NAMES)
    if unknown:
        # The count, never the names: the keys are the file's own bytes.
        raise GoalDocumentError(f"{where}: params carries {len(unknown)} unknown name(s)")
    kwargs: dict[str, Any] = dict(raw)
    if "skill" in kwargs:
        skill = parse_skill(_string(raw, "skill", where=f"{where}.params"))
        if skill is None:
            raise GoalDocumentError(f"{where}: skill is not a skill this build trains")
        kwargs["skill"] = skill
    if "scope" in kwargs:
        scope = parse_scope(_string(raw, "scope", where=f"{where}.params"))
        if scope is None:
            raise GoalDocumentError(f"{where}: scope is not a loot scope this build sweeps")
        kwargs["scope"] = scope
    try:
        return GoalParams(**kwargs)
    except (TypeError, ValueError) as exc:
        raise GoalDocumentError(f"{where}: params did not validate ({exc})") from None


def _string(entry: Mapping[str, Any], name: str, *, where: str) -> str:
    value = entry.get(name)
    if not isinstance(value, str):
        raise GoalDocumentError(f"{where}: {name} must be a string")
    return value


def _whole(entry: Mapping[str, Any], name: str, *, where: str) -> int:
    value = entry.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise GoalDocumentError(f"{where}: {name} must be a whole number")
    return value


def _optional_whole(entry: Mapping[str, Any], name: str, *, where: str) -> int | None:
    if entry.get(name) is None:
        return None
    return _whole(entry, name, where=where)


def _as_list(value: Any, *, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise GoalDocumentError(f"the stored channel's {name} must be an array")
    return value


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------


class GoalStore:
    """Loads and saves the one goals file in a sidecar's state directory."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = Path(state_dir)

    @property
    def path(self) -> Path:
        return self._state_dir / GOALS_FILE_NAME

    @property
    def quarantine_path(self) -> Path:
        return self._state_dir / GOALS_QUARANTINE_NAME

    def load(self) -> GoalSnapshot | None:
        """Read the stored channel. ``None`` is the ordinary no-file first run.

        Raises:
            GoalStoreError: the file exists but cannot be read as one complete
                JSON object, or is over :data:`MAX_GOAL_FILE_BYTES` — checked
                on the byte count before anything is parsed.
            GoalDocumentError: it parsed and is not a channel this build wrote;
                see :func:`snapshot_from_document`.
        """
        path = self.path
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise GoalStoreError(f"{path}: cannot stat goals ({exc.strerror or exc})") from exc
        if size > MAX_GOAL_FILE_BYTES:
            raise GoalStoreError(f"{path}: {size} bytes exceeds {MAX_GOAL_FILE_BYTES}")
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise GoalStoreError(f"{path}: cannot read goals ({exc})") from exc
        try:
            document: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GoalStoreError(f"{path}: malformed JSON at byte {exc.pos}") from exc
        if not isinstance(document, dict):
            raise GoalStoreError(f"{path}: expected a JSON object, got {type(document).__name__}")
        return snapshot_from_document(document)

    def save_snapshot(self, snapshot: GoalSnapshot) -> int:
        """Write *snapshot* atomically. Returns the number of bytes written.

        Raises:
            GoalStoreError: the document exceeds :data:`MAX_GOAL_FILE_BYTES` —
                which no snapshot a bounded queue produces can reach, but the
                cap is enforced on the writer as well as the reader so the two
                cannot drift — or the filesystem refuses the write.
        """
        path = self.path
        document = snapshot_to_document(snapshot)
        body = json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        encoded = body.encode("utf-8")
        if len(encoded) > MAX_GOAL_FILE_BYTES:
            raise GoalStoreError(
                f"goals document of {len(encoded)} bytes exceeds {MAX_GOAL_FILE_BYTES}"
            )
        temp = path.with_name(f"{path.name}{_TEMP_SUFFIX}.{os.getpid()}.{next(_temp_serial)}")
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            with temp.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except OSError as exc:
            # A unique scratch name is only bounded if a failed write takes its
            # file with it. The removal is itself guarded: when the directory
            # is what is broken, unlink raises too, and letting that escape
            # would replace the real diagnosis with one about a scratch file.
            try:
                temp.unlink(missing_ok=True)
            except OSError as cleanup_failure:
                raise GoalStoreError(
                    f"{path}: cannot write goals ({exc.strerror or exc}); the partial file "
                    f"{temp.name} could not be removed either "
                    f"({cleanup_failure.strerror or cleanup_failure})"
                ) from exc
            raise GoalStoreError(f"{path}: cannot write goals ({exc.strerror or exc})") from exc
        return len(encoded)

    def quarantine(self) -> Path:
        """Set the unreadable file aside as evidence. Returns where it went.

        ``os.replace``, so the previous corpse (if any) is the one thing lost —
        the newest broken document is always the one kept, and the channel's
        own name is freed for the fresh start.

        Raises:
            GoalStoreError: the file could not be moved — including the case
                where it vanished between the failed load and this call, which
                is reported rather than shrugged at because a file that
                disappears out from under the sidecar is itself evidence.
        """
        try:
            os.replace(self.path, self.quarantine_path)
        except OSError as exc:
            raise GoalStoreError(
                f"{self.path}: cannot set the unreadable goals file aside ({exc.strerror or exc})"
            ) from exc
        return self.quarantine_path
