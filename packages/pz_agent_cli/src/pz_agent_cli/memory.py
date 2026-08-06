"""What the agent remembers for the save being played, and the commands that write it.

Two halves that were each finished separately meet here.
:class:`~pz_agent_core.memory.store.SaveMemory` answers the two questions
autonomy asks — what did the user set aside (§7.9), and where is home (§7.10) —
and :class:`~pz_agent_core.memory.persistence.MemoryStore` loads and saves one
per save id. Neither of them could reach the sidecar, because of a mismatch in
*when*: a memory is scoped to a save, and the save is not known when the loop is
assembled. The mod publishes it, and the mod attaches afterwards.

:class:`SidecarMemory` is that seam, and its rules are the whole of this module:

* **Loaded on the first tick that names a save**, not at assembly. Until then it
  answers as :class:`~pz_agent_core.policy.autonomy.NoMemory` — nothing is
  reserved, no home is known. That asymmetry is deliberate and is not this
  module's to change: an empty memory must not be able to *forbid*, and it
  cannot vouch for a radius either, so travel is refused for want of a home
  point rather than allowed for want of a bound.
* **A save id that changes is a different world.** The loaded memory is dropped
  rather than carried across; one save's reservations are not the other's, and
  its home point names a place that does not exist there.
* **A file that exists and cannot be read is not an empty one.**
  :class:`UnreadableMemory` is what stands in then, and it reserves *everything*
  — see its docstring. Answering "nothing is reserved" because the file would
  not parse is how the agent eats the tin somebody set aside.
* **The file is re-read when it changes.** ``pz-agent remember reserve`` writes
  it from another process while the sidecar is running, and a reservation that
  only takes effect after a restart is a promise kept late.

The commands are the other half. Memory nothing can populate behaves exactly
like an empty one, so ``remember`` is what makes the wiring above observable:
``home``, ``reserve``, ``release``, ``list`` and ``forget``, all of them scoped
to the save the *mod* is reporting. None of them accepts a save id or a
coordinate as an argument: the point of ``remember home`` is to record where the
character is, and a square typed on a command line is not that. With no session
attached every one of them refuses and changes nothing — there is no save to
scope a memory to, and writing one under a guess is how a world ends up holding
another world's reservations.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from pz_agent_core.capabilities import MAX_NOTES
from pz_agent_core.memory import (
    MAX_FULL_TYPE_LEN,
    MemoryCapacityError,
    MemorySchemaError,
    MemoryScopeError,
    MemoryStore,
    MemoryStoreError,
    MemoryValueError,
    Reservation,
    SaveMemory,
    Square,
)
from pz_agent_core.policy.autonomy import NO_MEMORY, AutonomyMemory, HomeSquare
from pz_agent_core.protocol import JsonDict, Observation

from .autonomy import UNKNOWN_SAVE_ID, observed_snapshot
from .context import EXIT_FAILURE, EXIT_OK, CliContext, Workspace, resolve_workspace
from .output import Printer
from .supervisor import _read_json, _write_json

__all__ = [
    "MEMORY_DIR_NAME",
    "MEMORY_RECORD_NAME",
    "REMEMBER_SUBCOMMANDS",
    "RESERVED_BY_USER",
    "AttachedSave",
    "MemoryRecord",
    "MemoryRecordError",
    "RememberRefused",
    "SidecarMemory",
    "UnreadableMemory",
    "add_remember_parser",
    "attached_save",
    "build_sidecar_memory",
    "memory_root",
    "publish_memory_record",
    "read_memory_record",
    "run_remember",
    "validate_full_type",
]

#: Where the per-save memory files live, under the state directory beside the
#: backups and the capability report. Deliberately not in the exchange
#: directory: what the sidecar remembers is not the game's to read, and
#: uninstalling the mod must not delete the user's reservations.
MEMORY_DIR_NAME: Final = "memory"

#: Where the sidecar writes which memory it is on, beside the capability and
#: planner records and for the same reason: ``status`` must be able to say what
#: is in force without re-deriving it and reaching a second opinion.
MEMORY_RECORD_NAME: Final = "sidecar.memory.json"

#: Reason stored on a reservation the user made through the CLI. A reservation
#: carries a reason so a later reader can say *why* something is untouchable;
#: this one is the honest answer for "because they said so".
RESERVED_BY_USER: Final = "set aside by the user with 'pz-agent remember reserve'"

REMEMBER_SUBCOMMANDS: Final[tuple[str, ...]] = ("home", "reserve", "release", "list", "forget")

#: An item type as the game spells it: a module and a type, one dot between
#: them, letters, digits and underscores only. The module is not pinned to
#: ``Base`` because a modded item is a real item a user may want to set aside,
#: but the *shape* is pinned, because this string becomes a key in a file this
#: process writes: a path fragment, a control character or a newline in it would
#: be user input reaching a stored document unexamined.
_FULL_TYPE: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]*\.[A-Za-z0-9_]+")


class MemoryRecordError(ValueError):
    """A memory record could not be read or written as a whole document."""


class RememberRefused(ValueError):
    """A ``remember`` command was given something it will not store."""


# ---------------------------------------------------------------------------
# what autonomy sees while the store cannot answer
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnreadableMemory:
    """What autonomy sees when this save's memory exists and will not read back.

    Deliberately *not* :class:`~pz_agent_core.policy.autonomy.NoMemory`. An empty
    memory reserves nothing, and that is right for a save nobody has remembered
    anything about yet — there is nothing to protect. A file that exists and
    cannot be parsed is the opposite situation: the user's reservations are in
    there, unread, and answering "nothing is reserved" is precisely how the agent
    spends the thing they set aside.

    So every type reads as reserved until the file can be read again. The gate
    then finds nothing it may spend unasked and asks instead, which is the same
    answer it would give if the user really had reserved everything — a
    conservative reading of a document nobody could read, rather than a
    convenient one. Home stays unknown, because a home point cannot be inferred
    from a failure either.
    """

    def home_point(self) -> HomeSquare | None:
        return None

    def reserves_item(self, full_type: str, /) -> bool:
        return True


UNREADABLE_MEMORY: Final = UnreadableMemory()


# ---------------------------------------------------------------------------
# what status reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """Which memory one sidecar actually had in force, as ``status`` reads it.

    ``wired`` is written from the loop that was built rather than from the
    intent of the code that built it, exactly as the planner record's is: a
    sidecar whose memory argument went missing would say so here instead of
    looking identical to one that honours every reservation.

    ``loaded`` and ``readable`` are separate because their causes are opposite.
    Not loaded means no observation has named a save yet — ordinary, and it
    resolves itself the moment the game attaches. Not readable means a file for
    this save exists and would not parse, which resolves itself never and is
    something to go and fix.
    """

    wired: bool
    loaded: bool
    root: str
    detail: str
    readable: bool = True
    save_scope: str = ""
    reservations: int = 0
    home: bool = False
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise MemoryRecordError("a memory record must say how it reached the state it names")

    @property
    def empty(self) -> bool:
        """True for a memory that loaded and holds nothing yet."""
        return self.loaded and not self.reservations and not self.home

    def to_dict(self) -> JsonDict:
        return {
            "wired": self.wired,
            "loaded": self.loaded,
            "readable": self.readable,
            "root": self.root,
            "detail": self.detail,
            "save_scope": self.save_scope,
            "reservations": self.reservations,
            "home": self.home,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> MemoryRecord:
        """Parse a record this process did not necessarily write.

        Raises:
            MemoryRecordError: for anything malformed, rather than reading
                whichever field survived as the whole answer.
        """
        return cls(
            wired=_flag(payload, "wired"),
            loaded=_flag(payload, "loaded"),
            readable=_flag(payload, "readable", default=True),
            root=_text(payload.get("root", ""), "root"),
            detail=_text(payload.get("detail", ""), "detail"),
            save_scope=_text(payload.get("save_scope", ""), "save_scope"),
            reservations=_count(payload.get("reservations", 0)),
            home=_flag(payload, "home", default=False),
            notes=_notes(payload.get("notes", [])),
        )


def _flag(payload: Mapping[str, Any], name: str, *, default: bool | None = None) -> bool:
    value = payload.get(name, default)
    if not isinstance(value, bool):
        raise MemoryRecordError(f"a memory record's {name} must be true or false")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise MemoryRecordError(f"a memory record's {field_name} must be a string")
    return value


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MemoryRecordError("a memory record's reservations must be a non-negative integer")
    return value


def _notes(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, list):
        raise MemoryRecordError("a memory record's notes must be an array")
    return tuple(_text(item, "note") for item in value)[:MAX_NOTES]


def read_memory_record(path: Path) -> MemoryRecord | None:
    """The record at *path*, or None when no sidecar has written one there.

    None is a different statement from "no memory was wired", and ``status``
    reports it as one: nothing has assembled a loop in this state directory yet.
    """
    payload = _read_json(path)
    if payload is None:
        return None
    try:
        return MemoryRecord.from_dict(payload)
    except MemoryRecordError:
        return None


def publish_memory_record(path: Path, record: MemoryRecord) -> MemoryRecord:
    """Write *record* where ``status`` reads it. Returns what was written.

    A record that cannot be written is noted rather than raised, the way the
    capability ledger and the planner record treat the same failure: losing the
    status file is not a reason to stop honouring what the user reserved.
    """
    try:
        _write_json(path, record.to_dict())
    except OSError as exc:
        note = f"the memory record could not be written ({exc.strerror or exc})"
        return MemoryRecord(
            wired=record.wired,
            loaded=record.loaded,
            root=record.root,
            detail=record.detail,
            readable=record.readable,
            save_scope=record.save_scope,
            reservations=record.reservations,
            home=record.home,
            notes=(*record.notes, note)[-MAX_NOTES:],
        )
    return record


# ---------------------------------------------------------------------------
# the seam
# ---------------------------------------------------------------------------


def _plain(text: str) -> str:
    """The redactor a caller with no workspace gets: none."""
    return text


def _file_stamp(path: Path) -> tuple[int, int, int] | None:
    """What identifies this file's *contents* cheaply, or None when there is none.

    Read before the file is parsed, never after: a write that lands between the
    two produces a stamp older than the bytes just read, and the next tick's
    comparison then finds a difference and re-reads. Stamping afterwards would
    record the new file's identity against the old file's contents and the
    change would be missed for good.
    """
    try:
        status = path.stat()
    except OSError:
        return None
    return (status.st_mtime_ns, status.st_size, status.st_ino)


@dataclass
class SidecarMemory:
    """The save's memory, loaded the first time a tick names one.

    Implements :class:`~pz_agent_cli.autonomy.SaveScopedMemory`: the planner
    calls :meth:`follow` with the save id off each observation, then asks the two
    questions autonomy has. Everything between those two calls — loading,
    re-loading, dropping a world that changed, publishing the record ``status``
    reads — happens in :meth:`follow`, so the answers themselves are a dictionary
    lookup and cost nothing at the rate they are asked.
    """

    store: MemoryStore
    record_path: Path
    #: The store root as the user should see it printed. Redacted at assembly by
    #: the workspace that knows the account name; this object never renders a
    #: path itself.
    root: str = ""
    redact: Callable[[str], str] = _plain
    #: Whether this object actually reached the planner in the assembled loop.
    #: Set by the caller that built the loop, from the loop, not from intent.
    wired: bool = False
    _save_id: str = field(default="", init=False, repr=False)
    _loaded: SaveMemory | None = field(default=None, init=False, repr=False)
    _fallback: AutonomyMemory = field(default=NO_MEMORY, init=False, repr=False)
    _stamp: tuple[int, int, int] | None = field(default=None, init=False, repr=False)
    _detail: str = field(
        default="no observation has named a save yet, so nothing has been loaded",
        init=False,
        repr=False,
    )
    _notes: tuple[str, ...] = field(default=(), init=False, repr=False)

    # -- the SaveScopedMemory port -----------------------------------------

    def follow(self, save_id: str, /) -> None:
        """Answer for *save_id* from here on, loading or re-loading as needed."""
        if not save_id or save_id == UNKNOWN_SAVE_ID:
            # Every unreadable save reports the same id, so it names no save in
            # particular and must not be loaded as one. Whatever was loaded goes
            # with it: the world it described is not the world being reported.
            self._drop("the mod named no save this tick, so nothing of a previous one is answering")
            return
        if save_id != self._save_id:
            self._adopt(save_id)
            return
        if self._stamp != _file_stamp(self.store.path_for(save_id)):
            # Another process wrote this file — ``pz-agent remember`` is the one
            # that does. A reservation that waited for a restart to take effect
            # would be §7.9 honoured late, which is §7.9 broken once.
            self._adopt(save_id)

    def home_point(self) -> HomeSquare | None:
        return self.in_force.home_point()

    def reserves_item(self, full_type: str, /) -> bool:
        return self.in_force.reserves_item(full_type)

    # -- what it is answering with -----------------------------------------

    @property
    def loaded(self) -> SaveMemory | None:
        """The store behind the answers, or None when none is loaded."""
        return self._loaded

    @property
    def in_force(self) -> AutonomyMemory:
        """Whatever is answering right now: the store, empty, or unreadable."""
        return self._loaded if self._loaded is not None else self._fallback

    @property
    def detail(self) -> str:
        """How the current state was reached, for the readout that asks."""
        return self._detail

    def record(self) -> MemoryRecord:
        """This object's state, in the shape ``status`` reads."""
        memory = self._loaded
        return MemoryRecord(
            wired=self.wired,
            loaded=memory is not None,
            root=self.root,
            detail=self._detail,
            readable=self._fallback is not UNREADABLE_MEMORY,
            save_scope="" if memory is None else memory.scope,
            reservations=0 if memory is None else len(memory.reservations()),
            home=memory is not None and memory.home_point() is not None,
            notes=self._notes,
        )

    def publish(self) -> MemoryRecord:
        """Write the record where ``status`` reads it. Returns what was written."""
        record = publish_memory_record(self.record_path, self.record())
        self._notes = record.notes
        return record

    # -- loading -----------------------------------------------------------

    def _adopt(self, save_id: str) -> None:
        """Load *save_id*'s memory, replacing whatever was answering before."""
        self._save_id = save_id
        path = self.store.path_for(save_id)
        self._stamp = _file_stamp(path)
        try:
            self._loaded = self.store.load(save_id)
        except (
            MemoryStoreError,
            MemorySchemaError,
            MemoryScopeError,
            MemoryCapacityError,
            MemoryValueError,
        ) as exc:
            # Every refusal the store can raise means the same thing here: this
            # save's memory exists and this build cannot read it. It is not
            # retried until the file changes, so a broken document does not
            # become a re-parse four times a second.
            self._loaded = None
            self._fallback = UNREADABLE_MEMORY
            self._detail = self.redact(
                f"this save's memory could not be read ({exc}); until it can be, every item "
                "type reads as reserved and nothing is spent unasked"
            )
        else:
            self._fallback = NO_MEMORY
            held = len(self._loaded.reservations())
            self._detail = (
                f"loaded from {self.redact(str(path.name))}: {held} reservation(s), "
                f"home point {'set' if self._loaded.home_point() is not None else 'not set'}"
            )
        self.publish()

    def _drop(self, detail: str) -> None:
        """Stop answering from a loaded memory. Silent when nothing was loaded."""
        if self._loaded is None and self._fallback is NO_MEMORY and not self._save_id:
            return
        self._save_id = ""
        self._loaded = None
        self._fallback = NO_MEMORY
        self._stamp = None
        self._detail = detail
        self.publish()


def memory_root(workspace: Workspace) -> Path:
    """Where this machine keeps its per-save memory files."""
    return workspace.state_dir / MEMORY_DIR_NAME


def memory_store(workspace: Workspace) -> MemoryStore:
    """The store every part of this build reads and writes memory through.

    One construction site, so the sidecar, ``remember`` and ``status`` cannot end
    up on different roots and disagree about what the user reserved.
    """
    return MemoryStore(memory_root(workspace))


def build_sidecar_memory(workspace: Workspace) -> SidecarMemory:
    """The memory ``build_loop`` hands the planner, and the record it publishes.

    Nothing is loaded here and nothing can be: the save is the mod's to name and
    the mod has not attached yet. What this does is settle *where* it will be
    loaded from, and publish a record saying that nothing is loaded — which is
    the state ``status`` has to be able to distinguish from a memory that loaded
    and turned out to be empty.
    """
    return SidecarMemory(
        store=memory_store(workspace),
        record_path=workspace.state_dir / MEMORY_RECORD_NAME,
        root=workspace.redact(memory_root(workspace)),
        redact=workspace.redactor.text,
    )


# ---------------------------------------------------------------------------
# the save a remember command is for
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttachedSave:
    """The save the mod is reporting, and the snapshot that said so."""

    save_id: str
    observation: Observation
    detail: str


def attached_save(ctx: CliContext, workspace: Workspace) -> tuple[AttachedSave | None, str]:
    """The save a ``remember`` command is about, or why there is none.

    Read from the mod's own published snapshot, pointer first, because that is
    the only reading that describes the save open *now*. A command that cannot
    reach one changes nothing: memory is save-scoped by construction, and the
    alternative to knowing which save is guessing one.
    """
    snapshot = observed_snapshot(workspace.ipc_root, now_ms=ctx.clock_ms())
    observation = snapshot.observation
    if observation is None:
        return None, snapshot.detail
    save_id = observation.game.save_id
    if save_id == UNKNOWN_SAVE_ID:
        return None, (
            "the mod could not read the save key and reported it as unknown, which every "
            "unreadable save reports; it names no save to remember this for"
        )
    return AttachedSave(save_id=save_id, observation=observation, detail=snapshot.detail), ""


def validate_full_type(value: str) -> str:
    """The item type as it will be stored, or a refusal saying what is wrong.

    This is the one place a string a user typed becomes a key inside a document
    this process writes, so it is checked rather than trusted. Length first and
    shape second: bounding the input before matching it is what keeps a
    pathological argument from being an expensive one.

    Raises:
        RememberRefused: for anything that is not a plain ``Module.Type`` token.
    """
    text = value.strip()
    if not text:
        raise RememberRefused("an item type is required, for example Base.TinnedBeans")
    if len(text) > MAX_FULL_TYPE_LEN:
        raise RememberRefused(
            f"an item type is at most {MAX_FULL_TYPE_LEN} characters; that one is {len(text)}"
        )
    if not _FULL_TYPE.fullmatch(text):
        raise RememberRefused(
            f"{text!r} is not an item type. They look like Base.TinnedBeans: a module and a "
            "type with one dot between them, letters, digits and underscores only"
        )
    return text


# ---------------------------------------------------------------------------
# the commands
# ---------------------------------------------------------------------------


def add_remember_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``remember`` group and its five subcommands."""
    group = subparsers.add_parser(
        "remember",
        help="set aside an item type, record where home is, or read back what is remembered",
        description=(
            "Everything here is scoped to the save the mod is reporting, so the game has "
            "to be running with that save loaded. Nothing takes a save id or a coordinate: "
            "'remember home' records the square the character is standing on."
        ),
    )
    inner = group.add_subparsers(dest="remember_command", metavar="SUBCOMMAND")

    home = inner.add_parser("home", help="remember the square the character is standing on")

    reserve = inner.add_parser("reserve", help="set an item type aside; autonomy will not use it")
    reserve.add_argument("full_type", metavar="FULL_TYPE", help="item type, e.g. Base.TinnedBeans")

    release = inner.add_parser("release", help="take back a reservation")
    release.add_argument("full_type", metavar="FULL_TYPE", help="item type, e.g. Base.TinnedBeans")

    listing = inner.add_parser("list", help="what is remembered for the save now open")

    forget = inner.add_parser("forget", help="delete this save's memory")

    for parser in (home, reserve, release, listing, forget):
        parser.add_argument("--json", action="store_true")


def run_remember(ctx: CliContext, args: argparse.Namespace) -> int:
    """Dispatch a parsed ``remember`` invocation."""
    printer = Printer(ctx.stdout, ctx.stderr)
    subcommand = getattr(args, "remember_command", None)
    if subcommand is None:
        printer.error(f"remember needs a subcommand: {', '.join(REMEMBER_SUBCOMMANDS)}")
        return EXIT_FAILURE
    workspace = resolve_workspace(ctx)
    as_json = bool(args.json)
    attached, problem = attached_save(ctx, workspace)
    if attached is None:
        return _no_session(workspace, printer, problem=problem, as_json=as_json)

    store = memory_store(workspace)
    path = store.path_for(attached.save_id)
    if subcommand == "forget":
        # Ahead of the load, and on purpose: a memory file that will not parse is
        # exactly the one a user needs to be able to delete, and refusing to
        # remove it because it could not be read would be a dead end.
        return _forget(store, workspace, printer, save=attached, as_json=as_json)
    try:
        memory = store.load(attached.save_id)
    except (
        MemoryStoreError,
        MemorySchemaError,
        MemoryScopeError,
        MemoryCapacityError,
        MemoryValueError,
    ) as exc:
        printer.error(
            f"this save's memory could not be read: {workspace.redactor.text(str(exc))}. "
            "Nothing was changed. 'pz-agent remember forget' removes it if it cannot be "
            "repaired — the reservations in it would be lost with it."
        )
        return EXIT_FAILURE

    try:
        if subcommand == "home":
            return _home(
                store,
                memory,
                workspace,
                printer,
                save=attached,
                now_ms=ctx.clock_ms(),
                as_json=as_json,
            )
        if subcommand == "reserve":
            return _reserve(
                store,
                memory,
                printer,
                save=attached,
                full_type=args.full_type,
                now_ms=ctx.clock_ms(),
                as_json=as_json,
            )
        if subcommand == "release":
            return _release(store, memory, printer, full_type=args.full_type, as_json=as_json)
        if subcommand == "list":
            return _list(memory, path, workspace, printer, save=attached, as_json=as_json)
    except RememberRefused as exc:
        printer.error(f"{exc}. Nothing was changed.")
        return EXIT_FAILURE
    except MemoryCapacityError as exc:
        # The store refuses rather than evicting one of the user's own records,
        # and that refusal is the message: something has to give, and which is
        # theirs to decide.
        printer.error(f"{exc}. Nothing was changed.")
        return EXIT_FAILURE
    except MemoryStoreError as exc:
        printer.error(f"the memory could not be written: {workspace.redactor.text(str(exc))}")
        return EXIT_FAILURE
    # Unreachable through the parser: every choice it accepts is handled above.
    raise AssertionError(f"unrouted remember subcommand: {subcommand!r}")


def _no_session(workspace: Workspace, printer: Printer, *, problem: str, as_json: bool) -> int:
    """Refuse a command that has no save to be about, and change nothing."""
    detail = workspace.redactor.text(problem)
    message = (
        f"no session is attached, so there is no save to remember this for: {detail}. "
        "Nothing was changed. Start Project Zomboid with the mod enabled, load the save, "
        "and check 'pz-agent status'."
    )
    if as_json:
        printer.json({"changed": False, "attached": False, "detail": message})
    else:
        printer.error(message)
    return EXIT_FAILURE


def _home(
    store: MemoryStore,
    memory: SaveMemory,
    workspace: Workspace,
    printer: Printer,
    *,
    save: AttachedSave,
    now_ms: int,
    as_json: bool,
) -> int:
    """``remember home`` — the square the mod says the character is on."""
    position = save.observation.player.position
    # Integral because a remembered place is a tile: the radius check compares
    # grid squares, and half a square is not a place anyone can stand.
    square = Square(x=int(position.x), y=int(position.y), z=int(position.z))
    # The CLI's clock, not the snapshot's: every timestamp in one memory file is
    # written from one frame of reference, and retention compares them.
    memory.set_home(square, now_ms)
    written = store.save(memory)
    if as_json:
        printer.json(
            {
                "changed": True,
                "home": square.to_dict(),
                "save_id": save.save_id,
                "bytes": written,
                "detail": workspace.redactor.text(save.detail),
            }
        )
        return EXIT_OK
    printer.line(f"home is now {square.x}, {square.y}, floor {square.z}")
    printer.field("read from", workspace.redactor.text(save.detail))
    printer.field("scope", f"the save the mod is reporting ({save.save_id})")
    return EXIT_OK


def _reserve(
    store: MemoryStore,
    memory: SaveMemory,
    printer: Printer,
    *,
    save: AttachedSave,
    full_type: str,
    now_ms: int,
    as_json: bool,
) -> int:
    """``remember reserve`` — §7.9, written down where autonomy reads it."""
    wanted = validate_full_type(full_type)
    record = memory.reserve(full_type=wanted, reason=RESERVED_BY_USER, now_ms=now_ms)
    written = store.save(memory)
    held = memory.reservations()
    if as_json:
        printer.json(
            {
                "changed": True,
                "reserved": record.to_dict(),
                "reservations": len(held),
                "cap": memory.config.max_reservations,
                "bytes": written,
            }
        )
        return EXIT_OK
    printer.line(f"{record.full_type} is set aside; I will not use it unasked")
    printer.field("reservations", f"{len(held)} of {memory.config.max_reservations} for this save")
    printer.field("scope", f"the save the mod is reporting ({save.save_id})")
    return EXIT_OK


def _release(
    store: MemoryStore,
    memory: SaveMemory,
    printer: Printer,
    *,
    full_type: str,
    as_json: bool,
) -> int:
    """``remember release`` — take a reservation back."""
    wanted = validate_full_type(full_type)
    released = memory.release(wanted)
    # Written only when something changed: rewriting a file to record that
    # nothing happened is a write that can fail for no reason at all.
    written = store.save(memory) if released else 0
    if as_json:
        printer.json(
            {
                "changed": released,
                "full_type": wanted,
                "reservations": len(memory.reservations()),
                "bytes": written,
            }
        )
        return EXIT_OK
    if released:
        printer.line(f"{wanted} is no longer reserved")
    else:
        printer.line(f"{wanted} was not reserved; nothing changed")
    printer.field("reservations", str(len(memory.reservations())))
    return EXIT_OK


def _list(
    memory: SaveMemory,
    path: Path,
    workspace: Workspace,
    printer: Printer,
    *,
    save: AttachedSave,
    as_json: bool,
) -> int:
    """``remember list`` — what is remembered for the save now open."""
    home = memory.home_point()
    held = memory.reservations()
    if as_json:
        printer.json(
            {
                "changed": False,
                "save_id": save.save_id,
                "file": workspace.redact(path),
                "home": None if home is None else home.to_dict(),
                "reservations": [record.to_dict() for record in held],
            }
        )
        return EXIT_OK
    printer.heading(f"remembered for {save.save_id}")
    printer.field("file", workspace.redact(path))
    printer.field("home", "not set" if home is None else f"{home.x}, {home.y}, floor {home.z}")
    printer.field("reservations", f"{len(held)} of {memory.config.max_reservations}")
    printer.lines(_reservation_lines(held))
    return EXIT_OK


def _reservation_lines(held: tuple[Reservation, ...]) -> tuple[str, ...]:
    return tuple(f"    {record.full_type}  ({record.reason})" for record in held)


def _forget(
    store: MemoryStore,
    workspace: Workspace,
    printer: Printer,
    *,
    save: AttachedSave,
    as_json: bool,
) -> int:
    """``remember forget`` — drop this save's memory file."""
    try:
        removed = store.forget(save.save_id)
    except MemoryStoreError as exc:
        printer.error(f"the memory could not be removed: {workspace.redactor.text(str(exc))}")
        return EXIT_FAILURE
    if as_json:
        printer.json({"changed": removed, "save_id": save.save_id})
        return EXIT_OK
    if removed:
        printer.line("this save's memory is gone: no reservations, no home point")
    else:
        printer.line("there was nothing remembered for this save")
    return EXIT_OK
