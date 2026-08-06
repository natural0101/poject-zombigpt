"""The bridge between the sidecar loop's one-action port and the planner package.

Two subsystems that were each finished and tested separately speak different
shapes. :class:`~pz_agent_cli.runtime.Planner` is one observation in, at most one
:class:`~pz_agent_core.actions.engine.ActionRequest` out; a
:class:`~pz_agent_core.planner.PlanProvider` is a
:class:`~pz_agent_core.planner.PlanRequest` in and a whole
:class:`~pz_agent_core.planner.Plan` out. :class:`AutonomyPlanner` is the piece
that was missing between them, and it is deliberately the only thing in this
module that decides anything:

1. :class:`~pz_agent_core.policy.autonomy.AutonomyGate` answers "is there
   something I should start unasked, and what need is it for". It owns the
   session rules, §7.7's ask-vs-act list, the reflex-danger floor, the anti-loop
   brake, the permission ladder, the home radius and the action budget. None of
   that is repeated here; a second copy of a bound is how two subsystems end up
   disagreeing about it.
2. The provider turns that need into a typed plan. ``provider = "none"`` — the
   deterministic path — reaches it through the same selection policies the gate
   used, which is what keeps "the model never picks the sandwich" true no matter
   which provider is configured.
3. :class:`~pz_agent_core.planner.PlanCritic` reviews the plan on
   ``SELF_DIRECTED`` initiative, because a provider's answer is a proposal and
   not a permission.
4. The plan's **first step** becomes one :class:`ActionRequest`.

Only the first step, and that is not a simplification. The loop already
serialises work and re-observes between actions, so the next step is composed
next tick against a fresh observation — a plan runner here would be a second
executor driving a world the loop is already driving, and the item reference a
plan was written against may name something else by the time step two is
reached. :class:`~pz_agent_core.planner.PlanExecutor` is the component for
driving a whole plan, and it is deliberately not wired into this path.

**What the gate is given, this module does not invent.** The item choice comes
back from the gate, and a plan naming a different item is refused rather than
run: the gate removed everything the user reserved (§7.9) before it chose, and a
provider that named something else would be reaching around that.

**The backup is witnessed, never guessed.** §7.7 puts an unbacked save on the
ask-the-user side, so
:meth:`~pz_agent_core.policy.autonomy.AutonomyGate.evaluate` takes the evidence
as a parameter with no default. Local backups are named by save *directory*; the
mod reports the save as a digest of a key this side never sees
(``PZAgent.ObserveModel.saveId``), and nothing on this side can compute one from
the other. The bridge is therefore not a computation but a record: ``pz-agent
backup-save`` reads the save id out of the mod's own published snapshot and
writes it into the backup's manifest, and :class:`BackupAttribution` hands the
gate a backup whose manifest names the very id the observation carries — the
same value produced by the same code the gate compares against, so no hash is
re-implemented here and nothing is assumed.

**What the user set aside is read, not assumed.** The gate removes every item
type the user reserved before it chooses one (§7.9), and it asks memory which
those are. That memory is per save, and the save is only known from an
observation, so what this module holds is a :class:`SaveScopedMemory`:
:mod:`pz_agent_cli.memory` loads the store the first time a tick names a save
and answers as an empty memory until then. Empty means nothing is reserved and
no home is known — permissive about spending and refusing about travelling,
which is what :class:`~pz_agent_core.policy.autonomy.NoMemory` documents.

When no backup names the observed save the answer is still None and the gate
still asks: :func:`no_attributable_backup` remains what an unattributed machine
gets, and :data:`BACKUP_NOT_ATTRIBUTABLE` still says so through ``pz-agent
status``. A backup taken with no session attached records no save id at all, and
it stays unattributed for the rest of its life — the identifier can only be
learned from a mod that was running, and inventing it afterwards is the failure
this whole path exists to make impossible.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Protocol, TypeAlias, runtime_checkable

from pz_agent_core.actions.adapter import AdapterRegistry
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.capabilities import MAX_NOTES
from pz_agent_core.ipc.atomic import DocumentError, read_json_document
from pz_agent_core.ipc.clocks import Clock, system_clock_ms
from pz_agent_core.ipc.layout import IpcLayout, SnapshotSlot
from pz_agent_core.ipc.snapshot import POINTER_SLOT
from pz_agent_core.planner import (
    MAX_PLAN_STEPS,
    PROVIDER_NONE,
    PROVIDER_OPENAI_COMPATIBLE,
    PROVIDER_TEAMON,
    Goal,
    GoalKind,
    NullProvider,
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
    PlanCritic,
    PlanProvider,
    PlanRef,
    PlanRequest,
    TeamONConfig,
    TeamONProvider,
    TransportError,
)
from pz_agent_core.platform.backup import BackupError, BackupManager, BackupRecord
from pz_agent_core.policy.autonomy import (
    DEFAULT_AUTONOMY_CONFIG,
    NEED_BOREDOM,
    NEED_HUNGER,
    NEED_THIRST,
    NO_MEMORY,
    AutonomyConfig,
    AutonomyDecision,
    AutonomyGate,
    AutonomyMemory,
    BackupEvidence,
)
from pz_agent_core.policy.config import DEFAULT_POLICY_CONFIG, PolicyConfig
from pz_agent_core.policy.permissions import Initiative
from pz_agent_core.policy.selection import NO_CAPABILITIES, CapabilityLookup
from pz_agent_core.protocol import (
    CapabilityState,
    JsonDict,
    Observation,
    ProtocolError,
    RefKind,
)

from .config import AgentConfig
from .context import Workspace
from .runtime import CapabilityLedger
from .supervisor import _read_json, _write_json

__all__ = [
    "BACKUP_ATTRIBUTION_AVAILABLE",
    "BACKUP_NOT_ATTRIBUTABLE",
    "MAX_SNAPSHOT_AGE_MS",
    "PLANNER_FILE_NAME",
    "UNKNOWN_SAVE_ID",
    "AutonomyPlanner",
    "BackupAttribution",
    "BackupWitness",
    "LedgerCapabilities",
    "ObservedSave",
    "ObservedSnapshot",
    "PlannerError",
    "PlannerRecord",
    "SaveScopedMemory",
    "autonomy_config",
    "backup_note",
    "build_backup_witness",
    "build_planner",
    "no_attributable_backup",
    "observed_save",
    "observed_snapshot",
    "publish_planner_record",
    "read_planner_record",
    "resolve_provider",
    "workspace_backups",
]

#: Where the sidecar writes what it planned *with*, beside the capability record
#: and for the same reason: ``status`` must be able to say which planner a
#: running loop is on without re-deriving it and reaching a second opinion.
PLANNER_FILE_NAME: Final = "sidecar.planner.json"

#: Why ``pz-agent start`` can supply no backup evidence on this machine.
#: Carried into the planner record so the silence has a reason a user can read.
BACKUP_NOT_ATTRIBUTABLE: Final = (
    "autonomy will ask rather than act: no backup here records the save id the mod reported "
    "when it was taken, so none of them can be shown to cover the save being played. Run "
    "'pz-agent backup-save' with the game open and the save loaded, and the backup it takes "
    "will carry that id."
)

#: The other half of the same statement, for a machine that has one. It is still
#: conditional: a recorded id only attributes a backup to the save it names.
BACKUP_ATTRIBUTION_AVAILABLE: Final = (
    "autonomy acts unasked only while a backup here records the save id the mod is reporting "
    "now; a backup of another save is not a safety net for this one, and the backup's hashes "
    "are re-checked before it counts as one at all."
)

#: What the mod publishes when it could not read the save key
#: (``PZAgent.ObserveModel.UNKNOWN_SAVE_ID``). Every unreadable save reports the
#: same string, so it names no save in particular and must never be recorded on
#: a backup or matched against one — two different worlds would attribute to
#: each other through it.
UNKNOWN_SAVE_ID: Final = "unknown"

#: How old the mod's last published snapshot may be and still describe the save
#: that is open *now*. The mod publishes at the 4 Hz cadence of §3.3 while a save
#: is loaded, so this is generous by two orders of magnitude — a save pause, a
#: slow disk or a GC hitch cannot reach it — while still being far short of the
#: case that matters: a game that has quit leaves its last snapshot on disk
#: forever, and reading that as the save being played is how a backup would get
#: attributed to a world nobody is in.
MAX_SNAPSHOT_AGE_MS: Final = 30_000

#: What the gate needs and only a record can answer: the backup covering the save
#: an observation names. A callable rather than an object because the only input
#: a witness has is the save it is asked about.
BackupWitness: TypeAlias = Callable[[str], "BackupEvidence | None"]


def no_attributable_backup(save_id: str) -> BackupEvidence | None:
    """The witness for a machine where nothing can be attributed to *save_id*.

    Not a placeholder for a check that was skipped: it is what
    :func:`build_backup_witness` returns when no backup under the backup root
    carries a save id at all, and :data:`BACKUP_NOT_ATTRIBUTABLE` is published
    alongside it so the refusal it produces is explained rather than mysterious.
    """
    return None


# ---------------------------------------------------------------------------
# the save the mod itself is reporting
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObservedSnapshot:
    """The whole snapshot the mod last published, or why there is none to read.

    Separate from :class:`ObservedSave` because two commands want two different
    things out of one read. ``backup-save`` wants the save id; ``remember home``
    wants the square the character is standing on, and a home point read from
    anywhere but the mod's own snapshot would be a coordinate the user typed
    rather than the place they are.
    """

    observation: Observation | None
    detail: str

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError("an observed-snapshot reading must say how it reached its answer")


@dataclass(frozen=True, slots=True)
class ObservedSave:
    """The save id the mod last published, or why there is none to have.

    ``detail`` is written for the person reading ``backup-save`` or ``status``
    output, and it is never empty: "no save id was recorded" is only an honest
    thing to print next to the reason it was not.
    """

    save_id: str | None
    detail: str

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError("an observed-save reading must say how it reached its answer")


def observed_snapshot(
    ipc_root: Path | None,
    *,
    now_ms: int,
    max_age_ms: int = MAX_SNAPSHOT_AGE_MS,
) -> ObservedSnapshot:
    """Read the snapshot the mod finished publishing, or say why there is none.

    The pointer is read first because the mod writes it *last* (§3.5): it is the
    commit point, and the slot it names is the only file that holds a snapshot
    the mod finished publishing. When that slot cannot be read this reports
    nothing rather than following
    :class:`~pz_agent_core.ipc.snapshot.SnapshotReader` to the other slot — that
    fallback exists to keep a *world model* moving across a torn write, and the
    older snapshot it recovers describes the world before the one now loaded.
    Reading a save id or a home point off it would be exactly the guess this
    module refuses.

    Every failure is the same answer, ``None`` with a reason: no exchange
    directory, no pointer, an unusable slot, a document that is not an
    observation, or one older than *max_age_ms*.
    """
    if ipc_root is None or not ipc_root.is_dir():
        return ObservedSnapshot(
            None, "there is no exchange directory, so no session has published anything"
        )
    layout = IpcLayout(ipc_root)
    slot = _pointed_slot(layout)
    if slot is None:
        return ObservedSnapshot(
            None,
            "no session has published a snapshot here that this can read",
        )
    try:
        document = read_json_document(layout.snapshot_slot(slot))
    except DocumentError as exc:
        return ObservedSnapshot(
            None,
            f"the snapshot pointer names slot {slot.value}, which could not be read ({exc}); "
            "the other slot holds an older snapshot and is not what the mod last published",
        )
    try:
        observation = Observation.from_dict(document)
    except ProtocolError as exc:
        return ObservedSnapshot(
            None, f"slot {slot.value} does not hold a whole observation ({exc})"
        )
    age_ms = now_ms - observation.timestamp_ms
    if age_ms > max_age_ms:
        return ObservedSnapshot(
            None,
            f"the last snapshot the mod published is {age_ms} ms old, past the {max_age_ms} ms "
            "that still describes the save open now",
        )
    if age_ms < -max_age_ms:
        return ObservedSnapshot(
            None,
            f"the last snapshot the mod published is stamped {-age_ms} ms in the future; the two "
            "clocks disagree, so nothing in it describes this moment",
        )
    return ObservedSnapshot(
        observation, f"the mod published it {age_ms} ms ago in slot {slot.value}"
    )


def observed_save(
    ipc_root: Path | None,
    *,
    now_ms: int,
    max_age_ms: int = MAX_SNAPSHOT_AGE_MS,
) -> ObservedSave:
    """Read ``observation.game.save_id`` out of the mod's own published snapshot.

    Every way :func:`observed_snapshot` can fail is an absent save id with that
    reading's own reason, plus one more of this function's own: a snapshot the
    mod itself could not name a save for (:data:`UNKNOWN_SAVE_ID`).
    """
    snapshot = observed_snapshot(ipc_root, now_ms=now_ms, max_age_ms=max_age_ms)
    observation = snapshot.observation
    if observation is None:
        return ObservedSave(None, snapshot.detail)
    save_id = observation.game.save_id
    if save_id == UNKNOWN_SAVE_ID:
        return ObservedSave(
            None,
            "the mod could not read the save key and reported it as unknown, which every "
            "unreadable save reports; it names no save in particular",
        )
    return ObservedSave(save_id, snapshot.detail)


def _pointed_slot(layout: IpcLayout) -> SnapshotSlot | None:
    """Which slot the snapshot pointer names, or None when it cannot be trusted."""
    try:
        payload = read_json_document(layout.snapshot_pointer)
    except DocumentError:
        return None
    raw = payload.get(POINTER_SLOT)
    if not isinstance(raw, str):
        return None
    try:
        return SnapshotSlot(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# from a recorded save id to evidence the gate accepts
# ---------------------------------------------------------------------------


def workspace_backups(
    workspace: Workspace, *, clock: Callable[[], datetime] | None = None
) -> BackupManager | None:
    """The backup manager for this machine, or None when no game profile was found.

    One construction site for the three commands that need it, so ``status``,
    ``backup-save`` and the sidecar cannot end up reading different roots and
    disagreeing about whether a save is backed up.

    *clock* is the CLI's injected one where the caller has it. A manifest's
    timestamp and the age ``status`` prints beside it are then read from the same
    frame of reference; left out, the manager keeps its own wall clock, which is
    the same instant in production and a different one under a test harness.
    """
    user_dir = workspace.user_dir
    if user_dir is None:
        return None
    if clock is None:
        return BackupManager(user_dir, workspace.backup_root)
    return BackupManager(user_dir, workspace.backup_root, clock=clock)


@dataclass
class BackupAttribution:
    """Witness: the backup whose manifest names this save, with its hashes checked.

    Two rules, and the second is why this is a class rather than a function.

    **The match is exact.** :func:`~pz_agent_core.platform.backup.attributed_to`
    compares the observation's save id against the id the mod reported when the
    backup was taken. Newest-wins, only-one-backup and same-save-directory are
    all absent on purpose: each of them answers "probably", and a probable safety
    net is the thing :class:`~pz_agent_core.policy.autonomy.BackupEvidence`
    refuses to be.

    **``verified`` means the hashes were read.**
    :meth:`~pz_agent_core.platform.backup.BackupManager.verify` re-hashes every
    file in the backup against its manifest, which is the only evidence that it
    would restore — a manifest that merely lists files proves nothing. That is
    also gigabytes of reading, and this is called on every tick, so the result is
    remembered per backup id for the life of the process. What is remembered is
    therefore precisely "these hashes were checked, in this process, and they
    matched", which is what the flag claims; the check that guards the actual
    restore is run again by
    :meth:`~pz_agent_core.platform.backup.BackupManager.restore` immediately
    before it writes anything.

    A backup that fails verification is skipped rather than offered unverified: a
    corrupt backup is not a weaker safety net, it is none, and a deployment that
    turned ``require_verified_backup`` off would otherwise be handed one.
    """

    manager: BackupManager
    _checked: dict[str, bool] = field(default_factory=dict, init=False, repr=False)
    _detail: str = field(default="nothing has been asked about yet", init=False, repr=False)

    @property
    def last_detail(self) -> str:
        """Why the last question answered as it did, for a readout that asks."""
        return self._detail

    def __call__(self, save_id: str) -> BackupEvidence | None:
        """The :data:`BackupWitness` entry point: evidence for *save_id*, or None."""
        if not save_id or save_id == UNKNOWN_SAVE_ID:
            self._detail = "the observation names no save id I could attribute a backup to"
            return None
        candidates = self.manager.attributed_to(save_id)
        if not candidates:
            self._detail = f"no backup here records the save id {save_id}"
            return None
        for record in candidates:
            created_at_ms = record.created_at_ms
            if created_at_ms is None or created_at_ms < 0:
                self._detail = (
                    f"backup {record.backup_id} records this save but its timestamp cannot be "
                    "read as one instant, so I cannot say how old the safety net is"
                )
                continue
            if not self._hashes_check_out(record.backup_id):
                self._detail = (
                    f"backup {record.backup_id} records this save but failed its hash check, "
                    "so it is not a safety net for it"
                )
                continue
            self._detail = f"backup {record.backup_id} records this save and its hashes check out"
            return BackupEvidence(save_id=save_id, created_at_ms=created_at_ms, verified=True)
        return None

    def _hashes_check_out(self, backup_id: str) -> bool:
        remembered = self._checked.get(backup_id)
        if remembered is not None:
            return remembered
        try:
            self.manager.verify(backup_id)
        except BackupError:
            # Every refusal from the subsystem — missing, corrupt, unreadable —
            # is the same answer here: this one cannot be relied on.
            checked = False
        else:
            checked = True
        self._checked[backup_id] = checked
        return checked


def backup_note(records: tuple[BackupRecord, ...]) -> str:
    """What to tell ``status`` about what the backups here could ever prove.

    Read at assembly time, when no observation exists yet, so it is deliberately
    the weaker of the two statements it could make: whether *any* backup carries
    a save id at all. Whether one covers the save actually being played is a
    per-tick question, and the gate answers it every tick.
    """
    if any(record.observed_save_id for record in records):
        return BACKUP_ATTRIBUTION_AVAILABLE
    return BACKUP_NOT_ATTRIBUTABLE


def build_backup_witness(workspace: Workspace) -> tuple[BackupWitness, str]:
    """The witness ``build_loop`` passes the planner, and the note explaining it.

    Falls back to :func:`no_attributable_backup` — the same honest refusal this
    path had before anything could be attributed — when there is no backup root
    to read or nothing in it carries a save id. The fallback is a separate object
    rather than an empty :class:`BackupAttribution` so that the sidecar does not
    re-list a backup root on every tick to keep discovering that it is empty.
    """
    manager = workspace_backups(workspace)
    if manager is None:
        return no_attributable_backup, BACKUP_NOT_ATTRIBUTABLE
    try:
        records = manager.list_backups()
    except OSError as exc:
        # Listing reads a manifest per backup; a root that cannot be read is a
        # machine with no attributable backup, said out loud rather than a
        # traceback out of a sidecar that was only assembling itself.
        return no_attributable_backup, (
            f"{BACKUP_NOT_ATTRIBUTABLE} The backup root could not be read ({exc.strerror or exc})."
        )
    if not any(record.observed_save_id for record in records):
        return no_attributable_backup, BACKUP_NOT_ATTRIBUTABLE
    return BackupAttribution(manager), backup_note(records)


#: Which goal serves which need. The three are exactly the needs
#: :data:`~pz_agent_core.policy.autonomy.NEED_PLANS` has a plan for; bleeding and
#: fatigue reach the user through the gate's own ASK_USER path and never arrive
#: here, so an entry for them would be a goal nothing could ever ask for.
_GOALS: Final[Mapping[str, GoalKind]] = MappingProxyType(
    {
        NEED_HUNGER: GoalKind.SATISFY_HUNGER,
        NEED_THIRST: GoalKind.SATISFY_THIRST,
        NEED_BOREDOM: GoalKind.READ_FOR_BOREDOM,
    }
)


class PlannerError(ValueError):
    """A planner record could not be read or written as a whole document."""


# ---------------------------------------------------------------------------
# what status reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannerRecord:
    """Which planner one sidecar actually assembled, as ``status`` reads it.

    ``wired`` is written from the loop that was built, not from the intent of
    the code that built it — a sidecar whose planner argument went missing again
    would say ``NOT WIRED`` here instead of looking identical to a working one.

    ``configured`` and ``active`` are separate for the same reason ``resolved``
    exists on the capability record: "the deterministic path is what I asked
    for" and "the deterministic path is what I fell back to" produce the same
    behaviour and opposite facts, and only the second one is something to go and
    fix.
    """

    wired: bool
    configured: str
    active: str
    detail: str
    fallback_reason: str = ""
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise PlannerError("a planner record must say how it reached the planner it names")

    @property
    def fell_back(self) -> bool:
        """True when the configured provider is not the one that will answer."""
        return bool(self.fallback_reason)

    def to_dict(self) -> JsonDict:
        return {
            "wired": self.wired,
            "configured": self.configured,
            "active": self.active,
            "detail": self.detail,
            "fallback_reason": self.fallback_reason,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PlannerRecord:
        """Parse a record this process did not necessarily write.

        Raises:
            PlannerError: for anything malformed, rather than reading whichever
                field survived as the whole answer.
        """
        wired = payload.get("wired")
        if not isinstance(wired, bool):
            raise PlannerError("a planner record must say whether a planner was wired")
        return cls(
            wired=wired,
            configured=_text(payload.get("configured", ""), "configured"),
            active=_text(payload.get("active", ""), "active"),
            detail=_text(payload.get("detail", ""), "detail"),
            fallback_reason=_text(payload.get("fallback_reason", ""), "fallback_reason"),
            notes=_notes(payload.get("notes", [])),
        )


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise PlannerError(f"a planner record's {field_name} must be a string")
    return value


def _notes(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, list):
        raise PlannerError("a planner record's notes must be an array")
    return tuple(_text(item, "note") for item in value)[:MAX_NOTES]


def read_planner_record(path: Path) -> PlannerRecord | None:
    """The record at *path*, or None when no sidecar has written one there.

    None is a different statement from "no planner was wired", and ``status``
    reports it as one: nothing has assembled a loop in this state directory yet.
    """
    payload = _read_json(path)
    if payload is None:
        return None
    try:
        return PlannerRecord.from_dict(payload)
    except PlannerError:
        return None


def publish_planner_record(path: Path, record: PlannerRecord) -> PlannerRecord:
    """Write *record* where ``status`` reads it. Returns what was written.

    A record that cannot be written is noted rather than raised, the way the
    capability ledger treats the same failure: losing the status file is not a
    reason to refuse to drive the game.
    """
    try:
        _write_json(path, record.to_dict())
    except OSError as exc:
        note = f"the planner record could not be written ({exc.strerror or exc})"
        return replace(record, notes=(*record.notes, note)[-MAX_NOTES:])
    return record


# ---------------------------------------------------------------------------
# the bridge
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LedgerCapabilities:
    """The sidecar's capability ledger as a :class:`CapabilityLookup`.

    The ledger answers ``usable`` and the policies also ask ``state``, because
    §7.7 draws its line between *verified* and *available_unverified* rather than
    at usability. Both questions are forwarded at the moment they are asked, not
    captured at assembly: a capability a live run promotes to ``verified``
    mid-session has to become answerable to autonomy in that same session, and a
    snapshot taken at start-up would answer with the report as it was before
    anything ran.

    A ledger with no report answers ``UNSUPPORTED`` to everything, which is the
    fail-closed direction the whole capability system exists to keep.
    """

    ledger: CapabilityLedger

    def state(self, name: str, /) -> CapabilityState:
        report = self.ledger.report
        return CapabilityState.UNSUPPORTED if report is None else report.state(name)

    def usable(self, name: str, /) -> bool:
        return self.ledger.usable(name)


@runtime_checkable
class SaveScopedMemory(AutonomyMemory, Protocol):
    """An :class:`AutonomyMemory` that has to be told which save it answers for.

    :class:`~pz_agent_core.memory.store.SaveMemory` is built *for* one save id,
    and the save is only known once the mod has published an observation — the
    loop is assembled before the game attaches. So what the planner can be given
    at assembly is not a memory but something that can be pointed at the save
    each tick names, and :meth:`follow` is the whole of the difference.

    A plain :class:`~pz_agent_core.policy.autonomy.NoMemory` is not one of these
    and is used exactly as it is, which is what keeps the default behaviour of
    this module unchanged for every caller that has no store to offer.
    """

    def follow(self, save_id: str, /) -> None:
        """Answer for *save_id* from here on.

        Never raises. A memory that cannot be read is a state to report, not an
        exception to throw at a tick that was only asking what is reserved.
        """
        ...


@dataclass
class AutonomyPlanner:
    """One tick's answer to "should I start something, and what exactly"."""

    registry: AdapterRegistry
    provider: PlanProvider = field(default_factory=NullProvider)
    capabilities: CapabilityLookup = NO_CAPABILITIES
    backup: BackupWitness = no_attributable_backup
    #: What the user set aside and where home is. A :class:`SaveScopedMemory` is
    #: pointed at the save each observation names before it is asked anything;
    #: anything else is asked exactly as it stands.
    memory: AutonomyMemory = NO_MEMORY
    policy: PolicyConfig = DEFAULT_POLICY_CONFIG
    config: AutonomyConfig = DEFAULT_AUTONOMY_CONFIG
    clock: Clock = system_clock_ms
    #: Longest plan a provider may answer with. Clamped to the plan type's own
    #: ceiling, which is lower than the one ``config.toml`` accepts.
    max_steps: int = MAX_PLAN_STEPS
    _gate: AutonomyGate = field(init=False, repr=False)
    _decision: AutonomyDecision | None = field(default=None, init=False, repr=False)
    _detail: str = field(default="nothing has been proposed yet", init=False, repr=False)

    def __post_init__(self) -> None:
        if not 1 <= self.max_steps <= MAX_PLAN_STEPS:
            raise PlannerError(
                f"max_steps must be within 1..{MAX_PLAN_STEPS}, got {self.max_steps}"
            )
        self._gate = AutonomyGate(config=self.config, policy=self.policy)

    @property
    def gate(self) -> AutonomyGate:
        """The gate, whose arbiter and action budget are this object's only state."""
        return self._gate

    @property
    def last_decision(self) -> AutonomyDecision | None:
        """The gate's verdict on the most recent tick, or None before the first."""
        return self._decision

    @property
    def last_detail(self) -> str:
        """Why the last tick proposed what it did — including when that was nothing.

        Held in memory and not written anywhere: it changes every tick, and a
        file rewritten four times a second to say "nothing needs attention" would
        cost more than it tells anyone.
        """
        return self._detail

    def propose(self, observation: Observation) -> ActionRequest | None:
        """The next action to start unasked, or None.

        Nothing is remembered between calls except the gate's own bounded
        counters: every tick decides against the observation it was handed,
        because a reference chosen against an older one may name a different
        object by now.
        """
        memory = self._memory_for(observation)
        decision = self._gate.evaluate(
            observation,
            now_ms=self.clock(),
            backup=self.backup(observation.game.save_id),
            capabilities=self.capabilities,
            memory=memory,
        )
        self._decision = decision
        self._detail = decision.detail
        if not decision.should_act:
            return None
        return self._compose(decision, observation, memory)

    def _memory_for(self, observation: Observation) -> AutonomyMemory:
        """What is remembered about the save *this* observation names.

        The save id is read off the observation for the same reason the session
        id is: the loop is assembled before the mod attaches, so the tick is the
        earliest moment either of them exists. A save that changes mid-session is
        a different world, and telling the memory which one is what stops the
        previous one's reservations answering for it.
        """
        memory = self.memory
        if isinstance(memory, SaveScopedMemory):
            memory.follow(observation.game.save_id)
        return memory

    # -- from a need to one request ----------------------------------------

    def _compose(
        self,
        decision: AutonomyDecision,
        observation: Observation,
        memory: AutonomyMemory,
    ) -> ActionRequest | None:
        """Turn an ACT decision into the first step of a reviewed plan."""
        need = decision.need
        if need is None:
            # `should_act` guarantees a need; restating it narrows the type
            # without asserting something the caller cannot see.
            self._hold("the gate approved a tick without naming the need it serves")
            return None
        goal_kind = _GOALS.get(need.key)
        if goal_kind is None:
            self._hold(f"{need.key} is a need I have no goal for; there is nothing to plan for it")
            return None
        goal = Goal(goal_id=str(uuid.uuid4()), kind=goal_kind)
        proposal = self.provider.propose(
            PlanRequest(
                goal=goal,
                observation=observation,
                capabilities=self.capabilities,
                policy=self.policy,
                max_steps=self.max_steps,
            )
        )
        plan = proposal.plan
        if plan is None:
            self._hold(f"{self.provider.name} proposed nothing: {proposal.detail}")
            return None
        verdict = PlanCritic(
            session_id=observation.session_id,
            registry=self.registry,
            capabilities=self.capabilities,
            initiative=Initiative.SELF_DIRECTED,
            # The same memory the gate was handed on this tick, not a second
            # reading of it: a reload between the two would review the plan
            # against a home point the decision was not taken under.
            home=memory.home_point(),
            home_radius=self.config.home_radius,
            max_steps=self.max_steps,
        ).review(plan, observation)
        if verdict.refused:
            self._hold(f"the critic refused the plan: {verdict.detail}")
            return None
        step = plan.steps[0]
        mismatch = self._item_mismatch(decision, step_refs=step.args.refs())
        if mismatch is not None:
            self._hold(mismatch)
            return None
        self._detail = f"{decision.detail} {plan.summary}"
        return ActionRequest(
            action=step.action,
            # The observation's session, not one captured at assembly: the loop
            # is built before it attaches, and the queue refuses a command from
            # any session but its own, so this is both the earliest and the only
            # honest source.
            session_id=observation.session_id,
            idempotency_key=f"autonomy:{goal.goal_id}:{step.step_id}",
            args=step.args.to_payload(),
        )

    def _item_mismatch(
        self, decision: AutonomyDecision, *, step_refs: tuple[PlanRef, ...]
    ) -> str | None:
        """Refuse a plan that spends an item the selection policy did not choose.

        The gate has already run the deterministic policy over an inventory with
        the user's reservations removed, so its choice is the only item §7.9
        permits. A provider that named another one — a model may, and a
        mis-ordered deterministic call would — must not have that difference
        quietly executed.
        """
        authorised = decision.item_ref
        if authorised is None:
            return None
        named = next((ref.value for ref in step_refs if ref.kind is RefKind.ITEM), None)
        if named == authorised:
            return None
        return (
            "the plan's first step names an item the selection policy did not choose, "
            "so it is not something I may spend unasked"
        )

    def _hold(self, detail: str) -> None:
        """Record why this tick proposes nothing, for the readout that asks."""
        self._detail = detail


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def autonomy_config(config: AgentConfig) -> AutonomyConfig:
    """The autonomy bounds the user's configuration asks for.

    Only the two settings ``config.toml`` actually carries are read. The rest of
    :class:`~pz_agent_core.policy.autonomy.AutonomyConfig` keeps its documented
    default rather than being restated here, where a copy could drift.
    """
    return AutonomyConfig(
        home_radius=int(config.get("safety", "max_autonomous_radius")),
        require_verified_backup=bool(config.get("session", "require_backup")),
    )


def resolve_provider(config: AgentConfig, *, env: Mapping[str, str]) -> tuple[PlanProvider, str]:
    """The configured provider, or the deterministic one and why it is not.

    Returns the provider and the fallback reason, which is empty when the
    configured provider is what came back. Nothing here contacts a network: a
    credential is checked because
    :meth:`~pz_agent_core.planner.OpenAICompatibleProvider.api_key` exists to be
    asked before a session starts, while reachability is not, because a start-up
    that blocks on an endpoint is a start-up that hangs when the endpoint does.
    An endpoint that turns out to be unreachable refuses its own tick with
    ``CAPABILITY_UNAVAILABLE`` and the agent starts nothing — it is never
    answered for by the deterministic path behind the user's back.
    """
    configured = str(config.get("planner", "provider"))
    if configured == PROVIDER_NONE:
        return NullProvider(), ""
    try:
        typed = config.planner_provider
        provider = _model_provider(configured, typed, env)
    except ValueError as exc:
        return NullProvider(), f"{configured} could not be built from its configuration: {exc}"
    if provider is None:
        return NullProvider(), (
            f"{configured} is not a provider this build can construct, so nothing would answer"
        )
    try:
        provider.api_key()
    except (TransportError, ValueError) as exc:
        # ``CredentialUnavailable`` is the ordinary case and names the variable;
        # the wider catch covers a key that cannot be sent as a header at all.
        return NullProvider(), str(exc)
    return provider, ""


def _model_provider(
    configured: str,
    typed: OpenAICompatibleConfig | TeamONConfig | None,
    env: Mapping[str, str],
) -> OpenAICompatibleProvider | TeamONProvider | None:
    """Construct the named provider over *typed*, or None when it is not one.

    Raises:
        ValueError: from the provider's own construction, which owns its bounds.
    """
    if configured == PROVIDER_OPENAI_COMPATIBLE and isinstance(typed, OpenAICompatibleConfig):
        return OpenAICompatibleProvider(typed, environ=env)
    if configured == PROVIDER_TEAMON and isinstance(typed, TeamONConfig):
        return TeamONProvider(typed, environ=env)
    return None


def build_planner(
    *,
    registry: AdapterRegistry,
    capabilities: CapabilityLedger,
    config: AgentConfig,
    env: Mapping[str, str],
    clock: Clock = system_clock_ms,
    backup: BackupWitness = no_attributable_backup,
    memory: AutonomyMemory = NO_MEMORY,
    notes: tuple[str, ...] = (),
) -> tuple[AutonomyPlanner, PlannerRecord]:
    """Assemble the planner ``pz-agent start`` runs, and the record describing it.

    The record's ``wired`` field is left False here on purpose: only the caller
    that builds the loop can say whether the planner reached it, and that is the
    fact ``status`` has to report.

    *memory* defaults to the empty one for the same reason *backup* defaults to
    the refusing witness: a caller with no store to offer gets the documented
    fail-safe rather than a silent one. The empty memory reserves nothing, which
    is the permissive direction, so the caller that means to honour §7.9 has to
    pass something — and :mod:`pz_agent_cli.memory` is what ``build_loop`` passes.
    """
    configured = str(config.get("planner", "provider"))
    provider, fallback = resolve_provider(config, env=env)
    planner = AutonomyPlanner(
        registry=registry,
        provider=provider,
        capabilities=LedgerCapabilities(capabilities),
        backup=backup,
        memory=memory,
        policy=DEFAULT_POLICY_CONFIG,
        config=autonomy_config(config),
        clock=clock,
        # The plan type's ceiling is below what ``planner.max_steps`` accepts,
        # and a request over it is refused outright, so the smaller wins.
        max_steps=min(int(config.get("planner", "max_steps")), MAX_PLAN_STEPS),
    )
    active = provider.name
    detail = (
        f"planner.provider is {configured!r} and {active} is answering"
        if not fallback
        else f"planner.provider is {configured!r}; {active} is answering instead"
    )
    record = PlannerRecord(
        wired=False,
        configured=configured,
        active=active,
        detail=detail,
        fallback_reason=fallback,
        notes=notes[:MAX_NOTES],
    )
    return planner, record
