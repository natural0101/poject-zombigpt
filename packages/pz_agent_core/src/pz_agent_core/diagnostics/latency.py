"""Latency measured from what one exchange directory actually recorded.

The P0 targets — a command accepted by the game within 250 ms of submission at
p95, a terminal ack visible within 250 ms, observations at 4 Hz, a safety stop
acted on within 200 ms — shipped without an instrument, so every number ever
quoted for them was a guess. This module is the instrument. It walks the
command and ack journals (rotated generations included), the observation
stream, the snapshot slots and pointer, and the two heartbeat files of a
single exchange directory, joins commands to their acks by ``command_id``, and
reports distributions over exactly what is on disk. Nothing here estimates,
extrapolates or fills a gap: a quantity the directory does not record comes
back with a count of zero, and the caller prints "unmeasured" rather than a
number.

Two clocks, stated plainly. ``issued_at_ms`` is the sidecar's wall clock,
stamped by :meth:`~pz_agent_core.ipc.queue.CommandQueue.build`; every stamp on
an ack — ``timestamp_ms``, ``started_at_ms``, ``finished_at_ms`` — is the game
process's wall clock. Nothing anywhere corrects the skew between the two (the
heartbeat age clamp is the only concession to its existence), so every
interval that subtracts one clock from the other is labelled ``cross_clock``
in the output and may legitimately be negative. A negative latency is reported
as what it is, because it is the clearest available evidence of skew.

One naming honesty note carried into the field names: the ack vocabulary has a
``received`` status and nothing ever emits it — the first game-side stamp a
command gets is the ``accepted`` ack. The metric the epic called "submit to
game received" is therefore named ``submit_to_accepted`` here, which is what
it actually measures.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from math import ceil
from pathlib import Path
from typing import Any, Final

from ..ipc.atomic import DocumentError, read_json_document
from ..ipc.journal import (
    DEFAULT_KEEP,
    JournalReader,
    probe_truncation,
    read_header,
    rotated_path,
)
from ..ipc.layout import IpcLayout, SnapshotSlot
from ..ipc.snapshot import POINTER_WRITTEN_AT
from ..protocol.enums import ActionName, ActionStatus

#: Newest-wins cap on the commands one report joins. The journals themselves
#: are size-bounded, so this is a second fence, not the first: it keeps the
#: report's memory proportional to what a human or a CI job can actually use,
#: and the count of everything dropped is carried in the report so a capped
#: run says so instead of looking complete.
MAX_TRACES: Final = 4096

#: Newest-wins cap on observation (seq, timestamp) points, for the same reason.
MAX_OBSERVATION_POINTS: Final = 8192

#: How many reader diagnostics the report retains before summarising the rest.
MAX_DIAGNOSTICS: Final = 64

#: The label every cross-clock interval carries in the JSON document.
CROSS_CLOCK_NOTE: Final = (
    "issued_at_ms is the sidecar's wall clock and every ack stamp is the game's; "
    "no skew correction exists anywhere, so this interval mixes two clocks and "
    "may be negative"
)

#: Why the heartbeat cadence distributions are empty in an offline report.
HEARTBEAT_NOTE: Final = (
    "each heartbeat file is overwritten in place, so the directory retains one "
    "beat per peer; cadence needs a live watcher, not a snapshot of the disk"
)

#: The P0 targets, spelled once. ``TARGET_OBSERVATION_INTERVAL_P95_MS`` is the
#: 4 Hz target expressed in the unit the instrument measures: an observation
#: cadence of at least 4 Hz means successive snapshots at most 250 ms apart.
TARGET_SUBMIT_TO_ACCEPTED_P95_MS: Final = 250
TARGET_TERMINAL_VISIBILITY_P95_MS: Final = 250
TARGET_OBSERVATION_HZ: Final = 4
TARGET_OBSERVATION_INTERVAL_P95_MS: Final = 1000 // TARGET_OBSERVATION_HZ
TARGET_SAFETY_REACTION_P95_MS: Final = 200


class LatencyError(ValueError):
    """A journal exists and cannot be measured from. Refused, never guessed at."""


def nearest_rank(samples: list[int], percent: int) -> int:
    """The exact nearest-rank percentile: the ⌈P·N/100⌉-th smallest sample.

    Nearest-rank rather than any interpolating definition, deliberately: every
    value it returns is a latency that actually happened, and two runs over the
    same journals produce the identical number with no floating point in
    between.
    """
    if not samples:
        raise ValueError("a percentile over no samples is not a number")
    if not 0 < percent <= 100:
        raise ValueError(f"percent must be within 1..100, got {percent}")
    ordered = sorted(samples)
    rank = max(1, ceil(len(ordered) * percent / 100))
    return ordered[rank - 1]


@dataclass(frozen=True, slots=True)
class Distribution:
    """count/min/p50/p95/max over one interval, in milliseconds.

    ``count == 0`` is the honest shape of "nothing on disk measures this":
    every statistic is None and stays None, because a distribution invented
    for an empty sample list is exactly the fake number this module exists to
    replace.
    """

    count: int
    minimum: int | None = None
    p50: int | None = None
    p95: int | None = None
    maximum: int | None = None
    cross_clock: bool = False
    note: str = ""

    @classmethod
    def from_samples(
        cls, samples: list[int], *, cross_clock: bool = False, note: str = ""
    ) -> Distribution:
        if not samples:
            return cls(count=0, cross_clock=cross_clock, note=note)
        return cls(
            count=len(samples),
            minimum=min(samples),
            p50=nearest_rank(samples, 50),
            p95=nearest_rank(samples, 95),
            maximum=max(samples),
            cross_clock=cross_clock,
            note=note,
        )

    @property
    def measured(self) -> bool:
        return self.count > 0

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "count": self.count,
            "min_ms": self.minimum,
            "p50_ms": self.p50,
            "p95_ms": self.p95,
            "max_ms": self.maximum,
            "cross_clock": self.cross_clock,
        }
        if self.cross_clock:
            out["cross_clock_note"] = CROSS_CLOCK_NOTE
        if self.note:
            out["note"] = self.note
        return out


@dataclass(frozen=True, slots=True)
class CommandTrace:
    """One command's lifecycle, joined from the two journals by ``command_id``.

    Every timestamp is optional because every one of them can genuinely be
    absent — a command the mod never answered has nothing after
    ``issued_at_ms``, and even that field is read leniently so one malformed
    record does not hide the rest of the stream.
    """

    command_id: str
    action: str
    issued_at_ms: int | None
    accepted_at_ms: int | None = None
    started_at_ms: int | None = None
    terminal_at_ms: int | None = None
    status: str = "pending"

    @property
    def terminal(self) -> bool:
        return self.terminal_at_ms is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "action": self.action,
            "issued_at_ms": self.issued_at_ms,
            "accepted_at_ms": self.accepted_at_ms,
            "started_at_ms": self.started_at_ms,
            "terminal_at_ms": self.terminal_at_ms,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class HeartbeatFact:
    """The one beat a heartbeat file holds: reported as a fact, never a rate."""

    peer: str
    seq: int
    timestamp_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {"peer": self.peer, "seq": self.seq, "timestamp_ms": self.timestamp_ms}


class TargetVerdict(StrEnum):
    """What comparing one measured distribution against its target concluded."""

    MET = "met"
    MISSED = "missed"
    UNMEASURED = "unmeasured"


@dataclass(frozen=True, slots=True)
class TargetCheck:
    """One P0 target compared against what the directory actually recorded."""

    name: str
    description: str
    verdict: TargetVerdict
    detail: str
    measured_p95_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "verdict": self.verdict.value,
            "detail": self.detail,
            "measured_p95_ms": self.measured_p95_ms,
        }


@dataclass(frozen=True, slots=True)
class LatencyReport:
    """Everything one pass over an exchange directory could honestly measure."""

    traces: tuple[CommandTrace, ...] = ()
    pending: int = 0
    dropped_commands: int = 0
    unmatched_acks: int = 0
    submit_to_accepted: Distribution = Distribution(count=0, cross_clock=True)
    accepted_to_started: Distribution = Distribution(count=0)
    started_to_terminal: Distribution = Distribution(count=0)
    submit_to_terminal: Distribution = Distribution(count=0, cross_clock=True)
    safety_submit_to_terminal: Distribution = Distribution(count=0, cross_clock=True)
    observation_intervals: Distribution = Distribution(count=0)
    observation_points: int = 0
    game_heartbeat_intervals: Distribution = Distribution(count=0, note=HEARTBEAT_NOTE)
    sidecar_heartbeat_intervals: Distribution = Distribution(count=0, note=HEARTBEAT_NOTE)
    heartbeats: tuple[HeartbeatFact, ...] = ()
    pointer_written_at_ms: int | None = None
    diagnostics: tuple[str, ...] = ()

    @classmethod
    def empty(cls) -> LatencyReport:
        """The report for a directory with nothing in it: everything unmeasured."""
        return cls()

    @property
    def implied_observation_hz(self) -> float | None:
        """The cadence the median interval implies, or None when unmeasured."""
        p50 = self.observation_intervals.p50
        if p50 is None or p50 <= 0:
            return None
        return round(1000 / p50, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_cap": MAX_TRACES,
            "traces": [trace.to_dict() for trace in self.traces],
            "pending": self.pending,
            "dropped_commands": self.dropped_commands,
            "unmatched_acks": self.unmatched_acks,
            "distributions": {
                "submit_to_accepted": self.submit_to_accepted.to_dict(),
                "accepted_to_started": self.accepted_to_started.to_dict(),
                "started_to_terminal": self.started_to_terminal.to_dict(),
                "submit_to_terminal": self.submit_to_terminal.to_dict(),
                "safety_submit_to_terminal": self.safety_submit_to_terminal.to_dict(),
                "observation_intervals": self.observation_intervals.to_dict(),
                "heartbeat_intervals": {
                    "game": self.game_heartbeat_intervals.to_dict(),
                    "sidecar": self.sidecar_heartbeat_intervals.to_dict(),
                },
            },
            "observation": {
                "points": self.observation_points,
                "implied_hz": self.implied_observation_hz,
            },
            "heartbeats": [fact.to_dict() for fact in self.heartbeats],
            "snapshot_pointer_written_at_ms": self.pointer_written_at_ms,
            "diagnostics": list(self.diagnostics),
        }


# --------------------------------------------------------------------------
# collection
# --------------------------------------------------------------------------


def _as_stamp(value: object) -> int | None:
    """A non-negative integer read leniently, or None. bool is never a stamp."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class _Trace:
    """Mutable accumulator for one command while acks are being joined in."""

    __slots__ = (
        "accepted_at_ms",
        "action",
        "command_id",
        "issued_at_ms",
        "last_status",
        "started_seen_ms",
        "started_stamp_ms",
        "terminal_at_ms",
        "terminal_status",
    )

    def __init__(self, command_id: str, action: str, issued_at_ms: int | None) -> None:
        self.command_id = command_id
        self.action = action
        self.issued_at_ms = issued_at_ms
        self.accepted_at_ms: int | None = None
        self.started_stamp_ms: int | None = None
        self.started_seen_ms: int | None = None
        self.terminal_at_ms: int | None = None
        self.terminal_status: ActionStatus | None = None
        self.last_status: ActionStatus | None = None

    def apply(
        self,
        status: ActionStatus,
        *,
        timestamp_ms: int | None,
        started_at_ms: int | None,
        finished_at_ms: int | None,
    ) -> None:
        """Fold one ack in. The earliest stamp wins everywhere, because a
        redelivered ack must not move a moment that already happened."""
        if status in (ActionStatus.RECEIVED, ActionStatus.ACCEPTED) and timestamp_ms is not None:
            self.accepted_at_ms = _earliest(self.accepted_at_ms, timestamp_ms)
        if started_at_ms is not None:
            self.started_stamp_ms = _earliest(self.started_stamp_ms, started_at_ms)
        if status is ActionStatus.STARTED and timestamp_ms is not None:
            self.started_seen_ms = _earliest(self.started_seen_ms, timestamp_ms)
        if status.is_terminal:
            if self.terminal_at_ms is None:
                stamp = finished_at_ms if finished_at_ms is not None else timestamp_ms
                self.terminal_at_ms = stamp
                self.terminal_status = status
        else:
            self.last_status = status

    def freeze(self) -> CommandTrace:
        # ``started_at_ms`` prefers the mod's own PREPARING stamp over the ack
        # record's timestamp: the stamp is the moment the transition happened,
        # the timestamp is merely the moment it was written about.
        started = (
            self.started_stamp_ms if self.started_stamp_ms is not None else (self.started_seen_ms)
        )
        if self.terminal_status is not None:
            status = self.terminal_status.value
        elif self.last_status is not None:
            status = self.last_status.value
        else:
            status = "pending"
        return CommandTrace(
            command_id=self.command_id,
            action=self.action,
            issued_at_ms=self.issued_at_ms,
            accepted_at_ms=self.accepted_at_ms,
            started_at_ms=started,
            terminal_at_ms=self.terminal_at_ms,
            status=status,
        )


def _earliest(current: int | None, candidate: int) -> int:
    return candidate if current is None else min(current, candidate)


def _drain_stream(layout: IpcLayout, live: Path, diagnostics: list[str]) -> list[dict[str, Any]]:
    """Every record of one journal, rotated generations first, oldest to newest.

    Refuses — rather than partially reports — a live file whose tail is
    truncated or whose header is unusable: a report over a journal that lost
    its last record would quietly measure a stream that is not the one on
    disk. A missing or empty file is not damage; it drains to nothing.
    """
    problem = probe_truncation(live)
    if problem is not None:
        raise LatencyError(
            f"cannot measure from {live.name}: {problem}. If a session is live the "
            "producer may simply be mid-write; run again in a moment."
        )
    generations: list[tuple[int, Path]] = []
    for index in range(DEFAULT_KEEP, 0, -1):
        candidate = rotated_path(live, index)
        header = read_header(candidate)
        if header is not None:
            generations.append((header.serial, candidate))
    live_header = read_header(live)
    if live_header is not None:
        generations.append((live_header.serial, live))
    generations.sort(key=lambda entry: entry[0])

    payloads: list[dict[str, Any]] = []
    for _serial, path in generations:
        reader = JournalReader(layout, path)
        while True:
            before = reader.offset
            read = reader.read()
            payloads.extend(record.payload for record in read.records)
            for diagnostic in read.diagnostics:
                diagnostics.append(f"{path.name}: {diagnostic.detail}")
            if reader.offset == before:
                # Nothing moved, so nothing is left; the loop is bounded by
                # the file size because every earlier pass consumed bytes.
                break
    return payloads


def _join_traces(
    commands: list[dict[str, Any]],
    acks: list[dict[str, Any]],
    diagnostics: list[str],
) -> tuple[tuple[CommandTrace, ...], int, int]:
    """Traces joined by command_id, plus (dropped_commands, unmatched_acks)."""
    builders: OrderedDict[str, _Trace] = OrderedDict()
    dropped = 0
    for payload in commands:
        command_id = payload.get("command_id")
        if not isinstance(command_id, str) or not command_id:
            diagnostics.append("command record without a usable command_id; skipped")
            continue
        if command_id in builders:
            # A redelivered command record carries the same stamps; the first
            # copy already holds them.
            continue
        action = payload.get("action")
        builders[command_id] = _Trace(
            command_id=command_id,
            action=action if isinstance(action, str) else "",
            issued_at_ms=_as_stamp(payload.get("issued_at_ms")),
        )
        while len(builders) > MAX_TRACES:
            builders.popitem(last=False)
            dropped += 1

    unmatched = 0
    for payload in acks:
        command_id = payload.get("command_id")
        raw_status = payload.get("status")
        if not isinstance(command_id, str) or not isinstance(raw_status, str):
            diagnostics.append("ack record without a usable command_id and status; skipped")
            continue
        try:
            status = ActionStatus(raw_status)
        except ValueError:
            diagnostics.append(f"ack with unknown status {raw_status!r}; skipped")
            continue
        builder = builders.get(command_id)
        if builder is None:
            # An ack for a command outside the trace window — evicted by the
            # cap, or its command record rotated away. Counted, not guessed at.
            unmatched += 1
            continue
        builder.apply(
            status,
            timestamp_ms=_as_stamp(payload.get("timestamp_ms")),
            started_at_ms=_as_stamp(payload.get("started_at_ms")),
            finished_at_ms=_as_stamp(payload.get("finished_at_ms")),
        )
    return tuple(builder.freeze() for builder in builders.values()), dropped, unmatched


def _observation_points(
    layout: IpcLayout, events: list[dict[str, Any]], diagnostics: list[str]
) -> tuple[list[int], int]:
    """Interval samples between consecutive observation seqs, plus point count.

    Points come from the observation events journal and from whichever
    snapshot slot documents currently parse, deduplicated by ``seq`` — the
    observation stream is one sequence (§3.4) however a record was published.
    Only pairs whose seqs are adjacent yield an interval: across a seq gap the
    delta spans an unknown number of missed publications, and dividing it up
    would be an estimate, not a measurement.
    """
    points: dict[int, int] = {}
    for payload in events:
        seq = _as_stamp(payload.get("seq"))
        stamp = _as_stamp(payload.get("timestamp_ms"))
        if seq is None or stamp is None:
            continue
        points.setdefault(seq, stamp)
    for slot in (SnapshotSlot.A, SnapshotSlot.B):
        try:
            document = read_json_document(layout.snapshot_slot(slot))
        except (DocumentError, OSError):
            # An absent, torn or briefly locked slot offers no timestamp; the
            # journal remains the primary source, so nothing is lost by moving
            # on without one.
            continue
        seq = _as_stamp(document.get("seq"))
        stamp = _as_stamp(document.get("timestamp_ms"))
        if seq is None or stamp is None:
            diagnostics.append(f"snapshot slot {slot.value} carries no seq and timestamp_ms")
            continue
        points.setdefault(seq, stamp)

    ordered = sorted(points.items())[-MAX_OBSERVATION_POINTS:]
    intervals = [
        later_stamp - stamp
        for (seq, stamp), (later_seq, later_stamp) in pairwise(ordered)
        if later_seq - seq == 1
    ]
    return intervals, len(ordered)


def _pointer_written_at(layout: IpcLayout) -> int | None:
    try:
        document = read_json_document(layout.snapshot_pointer)
    except (DocumentError, OSError):
        return None
    return _as_stamp(document.get(POINTER_WRITTEN_AT))


def _heartbeat_facts(layout: IpcLayout, diagnostics: list[str]) -> tuple[HeartbeatFact, ...]:
    facts: list[HeartbeatFact] = []
    for peer, path in (("game", layout.game_heartbeat), ("sidecar", layout.sidecar_heartbeat)):
        try:
            payload = read_json_document(path)
        except (DocumentError, OSError):
            # No heartbeat is a fact the report states by omission; the peers
            # that never ran simply have no row.
            continue
        seq = _as_stamp(payload.get("seq"))
        stamp = _as_stamp(payload.get("timestamp_ms"))
        if seq is None or stamp is None:
            diagnostics.append(f"heartbeat.{peer} carries no usable seq and timestamp_ms")
            continue
        facts.append(HeartbeatFact(peer=peer, seq=seq, timestamp_ms=stamp))
    return tuple(facts)


def _bounded(diagnostics: list[str]) -> tuple[str, ...]:
    if len(diagnostics) <= MAX_DIAGNOSTICS:
        return tuple(diagnostics)
    kept = diagnostics[:MAX_DIAGNOSTICS]
    kept.append(f"... and {len(diagnostics) - MAX_DIAGNOSTICS} more diagnostics")
    return tuple(kept)


def collect_latency(root: Path) -> LatencyReport:
    """One pass over the exchange directory at *root*.

    Raises :class:`LatencyError` when a journal is present and unreadable —
    truncated tail, unusable header — because a report over a stream that
    provably lost records would present a partial measurement as the whole.
    A directory that simply has no data yet returns the empty report.
    """
    if not root.is_dir():
        return LatencyReport.empty()
    layout = IpcLayout(root)
    diagnostics: list[str] = []
    commands = _drain_stream(layout, layout.command_queue, diagnostics)
    acks = _drain_stream(layout, layout.command_ack, diagnostics)
    events = _drain_stream(layout, layout.observation_events, diagnostics)

    traces, dropped, unmatched = _join_traces(commands, acks, diagnostics)

    submit_to_accepted: list[int] = []
    accepted_to_started: list[int] = []
    started_to_terminal: list[int] = []
    submit_to_terminal: list[int] = []
    safety_submit_to_terminal: list[int] = []
    pending = 0
    for trace in traces:
        if not trace.terminal:
            pending += 1
        if trace.issued_at_ms is not None and trace.accepted_at_ms is not None:
            submit_to_accepted.append(trace.accepted_at_ms - trace.issued_at_ms)
        if trace.accepted_at_ms is not None and trace.started_at_ms is not None:
            accepted_to_started.append(trace.started_at_ms - trace.accepted_at_ms)
        if trace.started_at_ms is not None and trace.terminal_at_ms is not None:
            started_to_terminal.append(trace.terminal_at_ms - trace.started_at_ms)
        if trace.issued_at_ms is not None and trace.terminal_at_ms is not None:
            delta = trace.terminal_at_ms - trace.issued_at_ms
            submit_to_terminal.append(delta)
            if trace.action == ActionName.SAFETY_STOP.value:
                safety_submit_to_terminal.append(delta)

    intervals, observation_points = _observation_points(layout, events, diagnostics)
    return LatencyReport(
        traces=traces,
        pending=pending,
        dropped_commands=dropped,
        unmatched_acks=unmatched,
        submit_to_accepted=Distribution.from_samples(submit_to_accepted, cross_clock=True),
        accepted_to_started=Distribution.from_samples(accepted_to_started),
        started_to_terminal=Distribution.from_samples(started_to_terminal),
        submit_to_terminal=Distribution.from_samples(submit_to_terminal, cross_clock=True),
        safety_submit_to_terminal=Distribution.from_samples(
            safety_submit_to_terminal, cross_clock=True
        ),
        observation_intervals=Distribution.from_samples(intervals),
        observation_points=observation_points,
        game_heartbeat_intervals=Distribution.from_samples([], note=HEARTBEAT_NOTE),
        sidecar_heartbeat_intervals=Distribution.from_samples([], note=HEARTBEAT_NOTE),
        heartbeats=_heartbeat_facts(layout, diagnostics),
        pointer_written_at_ms=_pointer_written_at(layout),
        diagnostics=_bounded(diagnostics),
    )


# --------------------------------------------------------------------------
# targets
# --------------------------------------------------------------------------


def _against(
    name: str,
    description: str,
    distribution: Distribution,
    threshold_ms: int,
    *,
    unmeasured_detail: str,
    met_detail: str = "",
) -> TargetCheck:
    if distribution.p95 is None:
        return TargetCheck(
            name=name,
            description=description,
            verdict=TargetVerdict.UNMEASURED,
            detail=unmeasured_detail,
        )
    verdict = TargetVerdict.MET if distribution.p95 <= threshold_ms else TargetVerdict.MISSED
    detail = f"p95 is {distribution.p95} ms over {distribution.count} sample(s)"
    if distribution.cross_clock:
        detail += "; cross-clock, uncorrected skew included"
    if met_detail:
        detail += f"; {met_detail}"
    return TargetCheck(
        name=name,
        description=description,
        verdict=verdict,
        detail=detail,
        measured_p95_ms=distribution.p95,
    )


def evaluate_targets(report: LatencyReport) -> tuple[TargetCheck, ...]:
    """The four P0 targets against what *report* measured. Never a fake number.

    ``terminal_ack_visibility`` is permanently unmeasurable from disk and says
    so: every stamp on a terminal ack is the game's, and no record anywhere
    carries the moment the sidecar *read* it, which is what visibility means.
    """
    checks = [
        _against(
            "submit_to_accepted",
            f"command submitted → accepted ack, p95 ≤ {TARGET_SUBMIT_TO_ACCEPTED_P95_MS} ms "
            "(the epic said 'received'; nothing emits a received ack, so accepted is the "
            "first game-side stamp)",
            report.submit_to_accepted,
            TARGET_SUBMIT_TO_ACCEPTED_P95_MS,
            unmeasured_detail="no command on disk has an accepted ack; run against a live session",
        ),
        TargetCheck(
            name="terminal_ack_visibility",
            description=(
                f"terminal ack visible to the operator, p95 ≤ "
                f"{TARGET_TERMINAL_VISIBILITY_P95_MS} ms"
            ),
            verdict=TargetVerdict.UNMEASURED,
            detail=(
                "no on-disk record carries the moment the sidecar read a terminal ack — "
                "every stamp on the ack is the game's; measuring visibility needs a live "
                "session, not a journal"
            ),
        ),
        _against(
            "observation_rate",
            f"observations at ≥ {TARGET_OBSERVATION_HZ} Hz "
            f"(successive intervals p95 ≤ {TARGET_OBSERVATION_INTERVAL_P95_MS} ms)",
            report.observation_intervals,
            TARGET_OBSERVATION_INTERVAL_P95_MS,
            unmeasured_detail=(
                "fewer than two consecutive observation records are on disk; nothing to "
                "take an interval between"
            ),
            met_detail=(
                f"implied {report.implied_observation_hz} Hz at the median"
                if report.implied_observation_hz is not None
                else ""
            ),
        ),
        _against(
            "safety_reaction",
            f"safety.stop submitted → terminal ack, p95 ≤ {TARGET_SAFETY_REACTION_P95_MS} ms",
            report.safety_submit_to_terminal,
            TARGET_SAFETY_REACTION_P95_MS,
            unmeasured_detail=(
                "no safety.stop command on disk reached a terminal ack; nothing to measure"
            ),
        ),
    ]
    return tuple(checks)
