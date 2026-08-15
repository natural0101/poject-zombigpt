"""The runner: the only thing that writes ``result.json``, and the only thing
that decides PASS.

The structure is the argument. :class:`ObservedRun` — what a driver hands the
runner — has no success field. There is nowhere for a driver to say "it worked";
it reports what it saw, and the runner evaluates each postcondition against
those observations with the deterministic checks declared in
:mod:`pz_agent_cli.livetest.scenarios`. A driver can report a failure it knows
about, and it can report that it was blocked, but it cannot report a pass,
because the type has no field for one.

That leaves exactly one path to PASS: every declared postcondition read a value
that was present and satisfied its check. A missing field never passes. An
exception never becomes a pass. "The command was accepted" is not a
postcondition and cannot be written as one, since none of the ten checks can be
satisfied by an absent observation.

There is deliberately no function here that edits a result. ``finalize``
re-hashes what was written and compares it to the ledger, so an operator who
"fixes" a scenario by editing its result produces a refusal rather than a
release.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

from pz_agent_core.ipc.clocks import Clock, system_clock_ms
from pz_agent_core.protocol import JsonDict
from pz_agent_core.version import MOD_VERSION, PRODUCT_VERSION, SCHEMA_VERSION

from ..smoke import read_field
from .evidence import (
    GITKEEP_NAME,
    EvidenceLayout,
    LiveTestError,
    ManifestEntry,
    TamperError,
    canonical_json,
    digest_entry,
    read_document,
    sha256_bytes,
    sha256_file,
    write_bytes_atomically,
    write_document,
)
from .scenarios import SCENARIO_IDS, Check, LiveScenario, Postcondition
from .state import Attempt, LiveState, ScenarioState, StateStore

#: Format markers, so a document from a future shape is refused rather than
#: half-understood by a reader that predates it.
RESULT_FORMAT: Final = "pz-agent/livetest-result/1"
MANIFEST_FORMAT: Final = "pz-agent/livetest-manifest/1"

#: Failure code used when the postconditions decided the verdict and the driver
#: offered nothing more specific.
POSTCONDITION_FAILED: Final = "POSTCONDITION_FAILED"

#: Failure code for an attempt that never reached a verdict.
NOT_OBSERVED: Final = "NOT_OBSERVED"

#: Recorded in place of a build string nobody read off a running game. Not a
#: guess at the supported build: evidence that cannot name the game it ran
#: against closes nothing.
UNOBSERVED_BUILD: Final = "(not observed)"

UNKNOWN_COMMIT: Final = "(unknown)"

#: Largest observations document the file driver will read.
MAX_OBSERVATIONS_BYTES: Final = 8 * 1024 * 1024

#: Latency samples one scenario may report. Past this it is a trace.
MAX_LATENCY_SAMPLES: Final = 10_000


@dataclass(frozen=True, slots=True)
class ObservedRun:
    """What a driver saw. Note what is not here: any way to claim success.

    ``blocked_reason`` is the one verdict-shaped thing a driver may set, and it
    only ever makes the outcome weaker. ``failure_code`` likewise names a
    failure the driver already knows about; the runner still evaluates every
    postcondition and will fail the scenario on its own if they do not hold.
    """

    before: JsonDict = field(default_factory=dict)
    after: JsonDict = field(default_factory=dict)
    observations: JsonDict = field(default_factory=dict)
    latencies_ms: tuple[int, ...] = ()
    log_paths: tuple[str, ...] = ()
    screenshot_paths: tuple[str, ...] = ()
    game_build: str = ""
    failure_code: str = ""
    detail: str = ""
    blocked_reason: str = ""


class ScenarioDriver(Protocol):
    """Where observations come from.

    A live driver reads the exchange directory while an operator works through
    the scenario in-game. The tests use a fake one. Either way the runner's
    judgement is unchanged, which is the point of the seam.
    """

    def observe(self, scenario: LiveScenario) -> ObservedRun: ...


@dataclass(frozen=True, slots=True)
class UnavailableDriver:
    """The driver used when there is nothing to observe.

    Returns a blocked run rather than raising, so a batch that cannot reach a
    game still records an honest BLOCKED attempt for every scenario in the batch
    instead of dying on the first one.
    """

    reason: str

    def observe(self, scenario: LiveScenario) -> ObservedRun:
        return ObservedRun(blocked_reason=self.reason, failure_code=NOT_OBSERVED)


@dataclass(frozen=True, slots=True)
class FileDriver:
    """Reads one scenario's observations from a JSON document on disk.

    This is how a live run actually happens today: the operator drives the
    scenario in the game with the sidecar attached, and hands the runner the
    values that were read back — from ``pz-agent status --json``, the ack
    journal and the snapshot. The runner then decides, and the operator cannot
    write a verdict into the file because the schema has no field for one.
    """

    path: Path

    def observe(self, scenario: LiveScenario) -> ObservedRun:
        # Hashed only to bound the read: the cap is enforced while reading, so
        # a file being appended to cannot slip past a stat taken beforehand.
        _, size = sha256_file(self.path, limit=MAX_OBSERVATIONS_BYTES)
        if size == 0:
            raise LiveTestError(f"{self.path}: is empty")
        return parse_observations(read_document(self.path), scenario=scenario)


def parse_observations(document: Mapping[str, Any], *, scenario: LiveScenario) -> ObservedRun:
    """Turn an observations document into an :class:`ObservedRun`.

    Raises:
        LiveTestError: on a document that names a different scenario, or whose
            sections are the wrong shape. Silently treating a malformed section
            as empty would turn an operator's mistake into a FAIL that reads
            like a defect in the game.
    """
    named = document.get("scenario_id")
    if named is not None and str(named) != scenario.id:
        raise LiveTestError(
            f"observations name scenario {named!r}, not {scenario.id!r}. "
            "Evidence in the wrong directory proves nothing about either scenario."
        )
    sections = {
        key: _mapping(document.get(key, {}), key) for key in ("before", "after", "observations")
    }
    raw_latencies = document.get("latencies_ms", [])
    if not isinstance(raw_latencies, list):
        raise LiveTestError("latencies_ms must be a list of integers")
    if len(raw_latencies) > MAX_LATENCY_SAMPLES:
        raise LiveTestError(
            f"{len(raw_latencies)} latency samples exceeds the {MAX_LATENCY_SAMPLES} cap"
        )
    latencies: list[int] = []
    for entry in raw_latencies:
        if isinstance(entry, bool) or not isinstance(entry, (int, float)):
            raise LiveTestError(f"latency sample {entry!r} is not a number")
        latencies.append(int(entry))
    return ObservedRun(
        before=dict(sections["before"]),
        after=dict(sections["after"]),
        observations=dict(sections["observations"]),
        latencies_ms=tuple(latencies),
        log_paths=_strings(document.get("logs", []), "logs"),
        screenshot_paths=_strings(document.get("screenshots", []), "screenshots"),
        game_build=str(document.get("game_build", "")).strip(),
        failure_code=str(document.get("failure_code", "")).strip(),
        detail=str(document.get("detail", "")).strip(),
        blocked_reason=str(document.get("blocked_reason", "")).strip(),
    )


def _mapping(value: Any, key: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LiveTestError(f"{key} must be an object, got {type(value).__name__}")
    return value


def _strings(value: Any, key: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise LiveTestError(f"{key} must be a list of strings")
    return tuple(str(entry) for entry in value)


@dataclass(frozen=True, slots=True)
class PostconditionOutcome:
    """One postcondition, decided.

    Carries the observed values as well as the verdict: a reviewer has to be
    able to disagree with the verdict by reading what it was based on, which is
    impossible if only the boolean survives.
    """

    key: str
    statement: str
    check: str
    field_path: str
    expected: Any
    observed: Any
    observed_before: Any
    present: bool
    passed: bool
    detail: str

    def to_dict(self) -> JsonDict:
        return {
            "key": self.key,
            "statement": self.statement,
            "check": self.check,
            "field": self.field_path,
            "expected": self.expected,
            "observed": self.observed,
            "observed_before": self.observed_before,
            "present": self.present,
            "passed": self.passed,
            "detail": self.detail,
        }


def _is_non_empty(value: Any) -> bool:
    """Present and carrying something.

    Numbers and booleans count — ``0`` and ``False`` are observations, and a
    truthiness test would have quietly rejected the reading that says "zero
    zombies were seen".
    """
    if value is None:
        return False
    if isinstance(value, (str, bytes, list, tuple, dict, set)):
        return len(value) > 0
    return True


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return None if math.isnan(float(value)) else float(value)


def evaluate(condition: Postcondition, run: ObservedRun) -> PostconditionOutcome:
    """Decide one postcondition against one observed run.

    Every path that cannot read what it needs produces ``passed=False`` with a
    reason. There is no branch that passes on a missing value.
    """
    if condition.check.reads_snapshots:
        before, found_before = read_field(run.before, condition.field)
        after, found_after = read_field(run.after, condition.field)
        return _decide_snapshot(condition, before, after, found_before and found_after)
    observed, found = read_field(run.observations, condition.field)
    return _decide_observation(condition, observed, found)


def _outcome(
    condition: Postcondition,
    *,
    observed: Any,
    observed_before: Any = None,
    present: bool,
    passed: bool,
    detail: str,
) -> PostconditionOutcome:
    return PostconditionOutcome(
        key=condition.key,
        statement=condition.statement,
        check=condition.check.value,
        field_path=condition.field,
        expected=condition.expected,
        observed=observed,
        observed_before=observed_before,
        present=present,
        passed=passed,
        detail=detail,
    )


def _decide_snapshot(
    condition: Postcondition, before: Any, after: Any, present: bool
) -> PostconditionOutcome:
    if not present:
        return _outcome(
            condition,
            observed=after,
            observed_before=before,
            present=False,
            passed=False,
            detail=f"{condition.field} was not present in both the before and after snapshots",
        )
    # The same emptiness rule the observation path applies, and for the same
    # reason. Key presence alone let ``UNCHANGED`` pass on a pair of unreadable
    # values: ``player.health`` published as ``null`` in both snapshots is equal
    # to itself, so ``S05_BLOCKED_PATH``'s "the character took no damage" — a
    # safety statement, and one of the twenty-two scenarios whose results become
    # the evidence manifest ``--release`` reads — was satisfiable by a reading
    # nobody took. This repository has the same shape recorded one layer down:
    # a zombie scan that could not run published an empty list and the danger
    # floor read it as NONE.
    #
    # ``_is_non_empty`` rather than truthiness, because ``0``, ``0.0`` and
    # ``False`` are real readings. Health at zero is a fact about a character,
    # not a failure to look.
    unreadable = [
        name for name, value in (("before", before), ("after", after)) if not _is_non_empty(value)
    ]
    if unreadable:
        return _outcome(
            condition,
            observed=after,
            observed_before=before,
            present=False,
            passed=False,
            detail=(
                f"{condition.field} was present but empty in the "
                f"{' and '.join(unreadable)} snapshot(s), so nothing was read to compare"
            ),
        )
    if condition.check is Check.CHANGED:
        passed = before != after
        detail = "" if passed else "the value is identical in both snapshots"
        return _outcome(
            condition,
            observed=after,
            observed_before=before,
            present=True,
            passed=passed,
            detail=detail,
        )
    if condition.check is Check.UNCHANGED:
        passed = before == after
        detail = "" if passed else "the value differs between the snapshots"
        return _outcome(
            condition,
            observed=after,
            observed_before=before,
            present=True,
            passed=passed,
            detail=detail,
        )
    lhs, rhs = _numeric(before), _numeric(after)
    if lhs is None or rhs is None:
        return _outcome(
            condition,
            observed=after,
            observed_before=before,
            present=True,
            passed=False,
            detail=f"{condition.check.value} needs numbers on both sides",
        )
    passed = rhs > lhs if condition.check is Check.INCREASED else rhs < lhs
    direction = "rise" if condition.check is Check.INCREASED else "fall"
    return _outcome(
        condition,
        observed=after,
        observed_before=before,
        present=True,
        passed=passed,
        detail="" if passed else f"{lhs} -> {rhs} is not a {direction}",
    )


def _decide_observation(
    condition: Postcondition, observed: Any, found: bool
) -> PostconditionOutcome:
    if not found:
        return _outcome(
            condition,
            observed=None,
            present=False,
            passed=False,
            detail=f"{condition.field} was never observed",
        )
    if not _is_non_empty(observed):
        return _outcome(
            condition,
            observed=observed,
            present=False,
            passed=False,
            detail=f"{condition.field} was observed but empty",
        )
    if condition.check is Check.OBSERVED:
        return _outcome(condition, observed=observed, present=True, passed=True, detail="")
    if condition.check in {Check.IS_TRUE, Check.IS_FALSE}:
        wanted = condition.check is Check.IS_TRUE
        passed = observed is wanted
        detail = "" if passed else f"expected the boolean {wanted}, observed {observed!r}"
        return _outcome(condition, observed=observed, present=True, passed=passed, detail=detail)
    if condition.check is Check.EQUALS:
        passed = observed == condition.expected
        detail = "" if passed else f"expected {condition.expected!r}, observed {observed!r}"
        return _outcome(condition, observed=observed, present=True, passed=passed, detail=detail)

    value, threshold = _numeric(observed), _numeric(condition.expected)
    if value is None or threshold is None:
        return _outcome(
            condition,
            observed=observed,
            present=True,
            passed=False,
            detail=f"{condition.check.value} needs a number, observed {observed!r}",
        )
    passed = value >= threshold if condition.check is Check.AT_LEAST else value <= threshold
    comparator = "at least" if condition.check is Check.AT_LEAST else "at most"
    return _outcome(
        condition,
        observed=observed,
        present=True,
        passed=passed,
        detail="" if passed else f"{value} is not {comparator} {threshold}",
    )


def percentile(samples: Sequence[int], fraction: float) -> int | None:
    """Nearest-rank percentile. ``None`` for an empty series.

    Nearest-rank rather than interpolated because the result is a latency that
    was actually measured, and an interpolated p95 is a number no request ever
    took.
    """
    if not samples:
        return None
    ordered = sorted(samples)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def latency_summary(scenario: LiveScenario, run: ObservedRun) -> JsonDict:
    """p50/p95 for the scenarios that measure repeated operations.

    A scenario that does not repeat an operation reports ``null`` rather than a
    percentile over one sample, which would be the sample wearing a statistic's
    name.
    """
    if not scenario.measures_latency or not run.latencies_ms:
        return {
            "measured": False,
            "samples": len(run.latencies_ms),
            "p50_ms": None,
            "p95_ms": None,
        }
    return {
        "measured": True,
        "samples": len(run.latencies_ms),
        "p50_ms": percentile(run.latencies_ms, 0.50),
        "p95_ms": percentile(run.latencies_ms, 0.95),
    }


@dataclass(frozen=True, slots=True)
class ScenarioRun:
    """The outcome of one attempt, and where it was written."""

    scenario_id: str
    status: LiveState
    attempt: int
    failure_code: str
    outcomes: tuple[PostconditionOutcome, ...]
    result_path: Path
    result_sha256: str
    state: ScenarioState

    @property
    def failed_keys(self) -> tuple[str, ...]:
        return tuple(outcome.key for outcome in self.outcomes if not outcome.passed)


def decide(
    scenario: LiveScenario, run: ObservedRun
) -> tuple[LiveState, tuple[PostconditionOutcome, ...]]:
    """Evaluate every postcondition and derive the status.

    Blocked is checked first and separately: an attempt that never reached the
    game has no observations to evaluate, and calling that a FAIL would blame
    the code for something nobody exercised.
    """
    outcomes = tuple(evaluate(condition, run) for condition in scenario.postconditions)
    if run.blocked_reason:
        return LiveState.BLOCKED, outcomes
    if not outcomes:
        # No scenario declares zero postconditions, and none may: a scenario
        # with nothing to observe would pass vacuously, which is the one
        # outcome this module exists to make impossible.
        raise LiveTestError(f"{scenario.id}: declares no postconditions, so it cannot pass")
    return (LiveState.PASS if all(o.passed for o in outcomes) else LiveState.FAIL), outcomes


def read_commit(repo_root: Path) -> str:
    """The checked-out commit, read from ``.git`` without running git.

    A result whose commit does not match the code under test is not evidence of
    anything, so this is recorded on every attempt. An unreadable ``.git`` is
    reported as unknown rather than guessed.
    """
    head = repo_root / ".git" / "HEAD"
    try:
        text = head.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return UNKNOWN_COMMIT
    if not text.startswith("ref:"):
        return text or UNKNOWN_COMMIT
    ref = text.partition(":")[2].strip()
    loose = repo_root / ".git" / ref
    try:
        return loose.read_text(encoding="utf-8").strip() or UNKNOWN_COMMIT
    except (OSError, UnicodeDecodeError):
        pass
    packed = repo_root / ".git" / "packed-refs"
    try:
        lines = packed.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return UNKNOWN_COMMIT
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and parts[1] == ref:
            return parts[0]
    return UNKNOWN_COMMIT


def _iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat()


def build_result(
    scenario: LiveScenario,
    run: ObservedRun,
    *,
    status: LiveState,
    outcomes: Sequence[PostconditionOutcome],
    attempt: int,
    started_at_ms: int,
    finished_at_ms: int,
    commit: str,
    failure_code: str,
) -> JsonDict:
    """The ``result.json`` document. Observed values, then the verdict."""
    return {
        "format": RESULT_FORMAT,
        "commit": commit,
        "game_build": run.game_build or UNOBSERVED_BUILD,
        "product_version": PRODUCT_VERSION,
        "mod_version": MOD_VERSION,
        "schema_version": SCHEMA_VERSION,
        "scenario_id": scenario.id,
        "title": scenario.title,
        "attempt": attempt,
        "status": status.value,
        "failure_code": failure_code,
        "detail": run.blocked_reason or run.detail,
        "started_at_ms": started_at_ms,
        "finished_at_ms": finished_at_ms,
        "timestamp": _iso(finished_at_ms),
        "duration_ms": max(0, finished_at_ms - started_at_ms),
        "time_budget_s": scenario.time_budget_s,
        "before": dict(run.before),
        "after": dict(run.after),
        "postconditions": [outcome.to_dict() for outcome in outcomes],
        "latency": latency_summary(scenario, run),
        "logs": list(run.log_paths),
        "screenshots": list(run.screenshot_paths),
    }


def run_scenario(
    scenario: LiveScenario,
    *,
    layout: EvidenceLayout,
    store: StateStore,
    driver: ScenarioDriver,
    commit: str,
    clock: Clock = system_clock_ms,
) -> ScenarioRun:
    """Run one scenario, write its result, append its attempt.

    The order is load-bearing. The result document is written *before* the
    ledger entry, so a crash between the two leaves an unrecorded result — a
    visible inconsistency — rather than a ledger entry pointing at a file that
    does not exist, which would look like tampering at finalize time.
    """
    started_at_ms = clock()
    try:
        run = driver.observe(scenario)
    except LiveTestError as exc:
        run = ObservedRun(blocked_reason=str(exc), failure_code=NOT_OBSERVED)
    status, outcomes = decide(scenario, run)
    finished_at_ms = clock()
    failure_code = _failure_code(status, run)

    attempt_number = store.read(scenario.id).attempt_count + 1
    document = build_result(
        scenario,
        run,
        status=status,
        outcomes=outcomes,
        attempt=attempt_number,
        started_at_ms=started_at_ms,
        finished_at_ms=finished_at_ms,
        commit=commit,
        failure_code=failure_code,
    )
    attempt_path = layout.attempt_result_path(scenario.id, attempt_number)
    digest = write_document(attempt_path, document, schema=layout.result_schema)

    state = store.record(
        scenario.id,
        status=status,
        started_at_ms=started_at_ms,
        finished_at_ms=finished_at_ms,
        result_sha256=digest.sha256,
        failure_code=failure_code,
        note=run.blocked_reason or run.detail,
    )
    result_path = _publish_verdict(layout, state)
    return ScenarioRun(
        scenario_id=scenario.id,
        status=status,
        attempt=attempt_number,
        failure_code=failure_code,
        outcomes=outcomes,
        result_path=result_path,
        result_sha256=digest.sha256,
        state=state,
    )


def _failure_code(status: LiveState, run: ObservedRun) -> str:
    if status is LiveState.PASS:
        return ""
    if status is LiveState.BLOCKED:
        return run.failure_code or NOT_OBSERVED
    return run.failure_code or POSTCONDITION_FAILED


def _publish_verdict(layout: EvidenceLayout, state: ScenarioState) -> Path:
    """Copy the verdict-bearing attempt's result to ``result.json``.

    The verdict-bearing attempt is the one that passed, if any, and otherwise
    the latest. Copying rather than rewriting keeps the bytes — and therefore
    the digest — identical to what the ledger recorded, which is what lets a
    later re-run leave the passing result untouched.
    """
    attempt = state.passing_attempt or state.last_attempt
    if attempt is None:
        raise LiveTestError(f"{state.scenario_id}: no attempt to publish")
    source = layout.attempt_result_path(state.scenario_id, attempt.number)
    destination = layout.result_path(state.scenario_id)
    # Bytes end to end. Reading as text and writing it back re-encodes through
    # the platform's newline translation, so on Windows the published
    # ``result.json`` differed from the attempt it was copied from and every
    # later verify called it tampered. Moving the bytes cannot do that, and the
    # digest is checked against the file rather than against a decoded copy.
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise LiveTestError(f"{source}: cannot be read: {exc}") from exc
    if sha256_bytes(data) != attempt.result_sha256:
        raise TamperError(
            f"{source}: does not match the digest recorded for attempt {attempt.number}"
        )
    write_bytes_atomically(destination, data)
    return destination


def verify_result(layout: EvidenceLayout, state: ScenarioState) -> JsonDict:
    """Read a scenario's published result, checking it against the ledger.

    Raises:
        TamperError: if ``result.json`` no longer hashes to what was recorded.
        LiveTestError: if the scenario has never run.
    """
    attempt = state.passing_attempt or state.last_attempt
    if attempt is None:
        raise LiveTestError(f"{state.scenario_id}: has never run, so it has no result")
    return read_document(
        layout.result_path(state.scenario_id), expected_sha256=attempt.result_sha256
    )


@dataclass(frozen=True, slots=True)
class ScenarioAudit:
    """What ``finalize`` found for one scenario."""

    scenario_id: str
    state: LiveState
    entries: tuple[ManifestEntry, ...]
    missing: tuple[str, ...]
    tampered: str = ""

    @property
    def ok(self) -> bool:
        return self.state is LiveState.PASS and not self.missing and not self.tampered


class FinalizeRefused(LiveTestError):
    """``finalize`` will not build a manifest, and this is everything wrong.

    Every problem is carried, not just the first. An operator who fixes one
    missing log at a time, re-running finalize between each, is being told the
    truth in the least useful order possible.
    """

    def __init__(
        self,
        *,
        missing: Sequence[str],
        not_passed: Sequence[str],
        tampered: Sequence[str],
    ) -> None:
        self.missing = tuple(missing)
        self.not_passed = tuple(not_passed)
        self.tampered = tuple(tampered)
        parts = [
            f"{len(self.not_passed)} scenario(s) have not passed",
            f"{len(self.missing)} required artefact(s) missing",
            f"{len(self.tampered)} result(s) do not match their recorded digest",
        ]
        super().__init__("refusing to build the evidence manifest: " + "; ".join(parts))

    def render_lines(self) -> list[str]:
        """Every problem, named, one per line."""
        lines: list[str] = [str(self)]
        for label, items in (
            ("not passed", self.not_passed),
            ("missing artefact", self.missing),
            ("tampered", self.tampered),
        ):
            for item in items:
                lines.append(f"  {label}: {item}")
        return lines


def audit_scenario(
    scenario: LiveScenario, *, layout: EvidenceLayout, store: StateStore
) -> ScenarioAudit:
    """Hash everything *scenario* owes, and name what it does not have."""
    state = store.read(scenario.id)
    entries: list[ManifestEntry] = [
        digest_entry(
            layout,
            scenario_id=scenario.id,
            kind="state",
            path=store.path_for(scenario.id),
            required=True,
        ),
        digest_entry(
            layout,
            scenario_id=scenario.id,
            kind="result",
            path=layout.result_path(scenario.id),
            required=True,
        ),
    ]
    for attempt in state.attempts:
        entries.append(
            digest_entry(
                layout,
                scenario_id=scenario.id,
                kind="attempt",
                path=layout.attempt_result_path(scenario.id, attempt.number),
                required=True,
            )
        )
    for log_name in scenario.logs:
        entries.append(
            digest_entry(
                layout,
                scenario_id=scenario.id,
                kind="log",
                path=layout.logs_dir(scenario.id) / log_name,
                required=True,
            )
        )
    if scenario.screenshots_required:
        entries.extend(_screenshot_entries(scenario, layout))

    missing = tuple(
        f"{entry.path} ({entry.problem or 'missing'})"
        for entry in entries
        if entry.required and not entry.present
    )
    tampered = ""
    if state.state is not LiveState.NOT_RUN:
        try:
            verify_result(layout, state)
        except TamperError as exc:
            tampered = str(exc)
        except LiveTestError:
            # Already accounted for as a missing or unreadable artefact above;
            # reporting it twice under two names would double-count the fix.
            tampered = ""
    return ScenarioAudit(
        scenario_id=scenario.id,
        state=state.state,
        entries=tuple(entries),
        missing=missing,
        tampered=tampered,
    )


def _screenshot_entries(
    scenario: LiveScenario, layout: EvidenceLayout
) -> tuple[ManifestEntry, ...]:
    directory = layout.screenshots_dir(scenario.id)
    shots = sorted(p for p in directory.glob("*") if p.is_file() and p.name != GITKEEP_NAME)
    if not shots:
        return (
            ManifestEntry(
                scenario_id=scenario.id,
                kind="screenshot",
                path=layout.relative(directory),
                required=True,
                present=False,
                problem="no screenshot was collected for a scenario that requires one",
            ),
        )
    return tuple(
        digest_entry(layout, scenario_id=scenario.id, kind="screenshot", path=shot, required=True)
        for shot in shots
    )


def finalize(
    *,
    layout: EvidenceLayout,
    store: StateStore,
    scenarios: Sequence[LiveScenario],
    output: Path,
    commit: str,
    clock: Clock = system_clock_ms,
) -> tuple[Path, JsonDict]:
    """Build the evidence manifest, or refuse and say exactly why.

    Raises:
        FinalizeRefused: carrying every missing artefact, every scenario that
            has not passed, and every result whose bytes no longer match the
            ledger. Nothing is written when it refuses — a partial manifest is
            the artefact a release gate would happily accept.
    """
    audits = tuple(audit_scenario(scenario, layout=layout, store=store) for scenario in scenarios)
    missing: list[str] = []
    not_passed: list[str] = []
    tampered: list[str] = []
    for audit in audits:
        missing.extend(audit.missing)
        if audit.state is not LiveState.PASS:
            not_passed.append(f"{audit.scenario_id} is {audit.state.value}")
        if audit.tampered:
            tampered.append(audit.tampered)
    if missing or not_passed or tampered:
        raise FinalizeRefused(missing=missing, not_passed=not_passed, tampered=tampered)

    entries = [entry for audit in audits for entry in audit.entries]
    generated_at_ms = clock()
    builds = sorted(
        {
            str(verify_result(layout, store.read(audit.scenario_id)).get("game_build", ""))
            for audit in audits
        }
    )
    document: JsonDict = {
        "format": MANIFEST_FORMAT,
        "complete": True,
        "commit": commit,
        "game_builds": builds,
        "product_version": PRODUCT_VERSION,
        "mod_version": MOD_VERSION,
        "schema_version": SCHEMA_VERSION,
        "generated_at_ms": generated_at_ms,
        "generated_at": _iso(generated_at_ms),
        "scenario_count": len(audits),
        "scenarios": [_scenario_summary(audit, store) for audit in audits],
        "artefacts": [entry.to_dict() for entry in entries],
        "totals": {
            "artefact_count": len(entries),
            "bytes": sum(entry.size_bytes for entry in entries),
        },
    }
    write_document(output, document, schema=layout.manifest_schema)
    return output, document


def _scenario_summary(audit: ScenarioAudit, store: StateStore) -> JsonDict:
    state = store.read(audit.scenario_id)
    attempt = state.passing_attempt
    return {
        "scenario_id": audit.scenario_id,
        "state": audit.state.value,
        "attempt_count": state.attempt_count,
        "last_run_ms": state.last_run_ms,
        "result_sha256": "" if attempt is None else attempt.result_sha256,
    }


def repo_root() -> Path:
    """The checkout this package was imported from.

    A packaged install has no ``.git`` and no ``evidence/`` tree; the caller
    passes explicit paths there. Returning a path that does not exist is fine —
    the commands report it by name, which beats guessing.
    """
    return Path(__file__).resolve().parents[5]


def default_evidence_root() -> Path:
    return repo_root() / "evidence"


def default_manifest_path() -> Path:
    """Where ``docs/LOCAL_AGENT_PROMPT.md`` tells the operator to look for it."""
    return repo_root() / "release" / "evidence-manifest.json"


def summarise(states: Sequence[ScenarioState]) -> dict[str, int]:
    """Count of scenarios per state, with every state present at zero."""
    tally = {member.value: 0 for member in LiveState}
    for state in states:
        tally[state.state.value] += 1
    return tally


def first_unpassed(store: StateStore, scenario_ids: Sequence[str] = SCENARIO_IDS) -> str | None:
    """The id ``resume`` starts from, in run order."""
    for scenario_id in scenario_ids:
        if store.read(scenario_id).state is not LiveState.PASS:
            return scenario_id
    return None


def attempt_lines(state: ScenarioState) -> list[str]:
    """One line per attempt, for ``status --verbose``."""
    lines: list[str] = []
    for attempt in state.attempts:
        stamp = _iso(attempt.finished_at_ms)
        code = f" {attempt.failure_code}" if attempt.failure_code else ""
        lines.append(f"    attempt {attempt.number}: {attempt.status.value}{code}  {stamp}")
    return lines


__all__ = [
    "MANIFEST_FORMAT",
    "NOT_OBSERVED",
    "POSTCONDITION_FAILED",
    "RESULT_FORMAT",
    "UNOBSERVED_BUILD",
    "Attempt",
    "FileDriver",
    "FinalizeRefused",
    "ObservedRun",
    "PostconditionOutcome",
    "ScenarioAudit",
    "ScenarioDriver",
    "ScenarioRun",
    "UnavailableDriver",
    "attempt_lines",
    "audit_scenario",
    "build_result",
    "canonical_json",
    "decide",
    "default_evidence_root",
    "default_manifest_path",
    "evaluate",
    "finalize",
    "first_unpassed",
    "latency_summary",
    "parse_observations",
    "percentile",
    "read_commit",
    "repo_root",
    "run_scenario",
    "summarise",
    "verify_result",
]
