"""The game-smoke harness: load scenarios, drive them, write evidence.

Sixteen scenarios under ``tests/game-smoke/`` describe what cannot be closed
without a running game. This module loads them, runs the steps it can, prompts
for the ones only a human can perform, and writes one evidence file per
scenario.

The invariant the whole module exists for: **a scenario that did not run is
reported as not run.** Never as passing, never silently omitted.
``docs/RELEASE.md`` treats unrun scenarios as reportable rather than
omittable, and a harness that quietly dropped them would make the release gate
a formality. :class:`Outcome` therefore has no default and no "unknown"
collapse — every scenario in the requested set appears in the summary with one
of four states, and ``NOT_RUN`` is the state it starts in.

Evidence is the observed values, not a verdict. "hunger_before 0.72,
hunger_after 0.31" rather than "eat: ok", because a reviewer has to be able to
disagree with the verdict by reading what it was based on.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from pz_agent_core.protocol import JsonDict
from pz_agent_core.version import MOD_VERSION, PRODUCT_VERSION, SCHEMA_VERSION

#: A scenario file larger than this is not a scenario. Bounded because the
#: loader runs before anything has validated the directory it was pointed at.
MAX_SCENARIO_BYTES: Final = 256 * 1024

#: Scenario ids are printed, used as filenames and matched on the command line.
_ID_MAX_CHARS: Final = 16

#: How many evidence values a single scenario may collect. A scenario that
#: wanted more than this is asking for a trace, not evidence.
MAX_EVIDENCE_ENTRIES: Final = 64


class SmokeError(Exception):
    """A scenario file, or the set of them, cannot be used as written."""


class Outcome(StrEnum):
    """What became of one scenario.

    ``NOT_RUN`` is the initial state and the one that survives when a scenario
    is filtered out, skipped for a missing precondition, or interrupted. It is
    deliberately distinct from ``FAILED``: a scenario nobody ran tells you
    nothing, while a scenario that ran and failed tells you a great deal, and
    collapsing them would let a release claim coverage it does not have.
    """

    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"

    @property
    def is_conclusive(self) -> bool:
        """True when the scenario actually exercised the system."""
        return self in {Outcome.PASSED, Outcome.FAILED}


@dataclass(frozen=True, slots=True)
class Step:
    """One step of a scenario. ``manual`` steps need a human."""

    index: int
    action: str
    manual: bool

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any], *, position: int) -> Step:
        raw_id = payload.get("id", position)
        if not isinstance(raw_id, int):
            raise SmokeError(f"step id must be an integer, got {raw_id!r}")
        action = payload.get("action")
        if not isinstance(action, str) or not action.strip():
            raise SmokeError(f"step {raw_id} has no action text")
        manual = payload.get("manual", False)
        if not isinstance(manual, bool):
            raise SmokeError(f"step {raw_id}: manual must be a boolean")
        return cls(index=raw_id, action=action.strip(), manual=manual)


@dataclass(frozen=True, slots=True)
class EvidenceRequirement:
    """One field a scenario needs observed, and what must be true of it.

    ``assertion`` is prose on purpose. The scenarios were written to be read by
    a person deciding whether a release is honest, and encoding "strictly less
    than hunger_before" as a comparator DSL would have made them precise and
    unreadable. The runner collects the value; the assertion travels with it
    into the evidence file so the reviewer can check the pairing.
    """

    field_path: str
    assertion: str
    why: str = ""

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> EvidenceRequirement:
        field_path = payload.get("field")
        if not isinstance(field_path, str) or not field_path.strip():
            raise SmokeError("an evidence entry has no field")

        # `assertion: false` is a perfectly clear way to write "must be false",
        # and YAML hands it over as a bool rather than the string it looks
        # like. Requiring the author to quote it would push a parser detail
        # into a document meant to be read by whoever is deciding whether a
        # release is honest, so any scalar is accepted and rendered.
        assertion = payload.get("assertion")
        if assertion is None or isinstance(assertion, (list, dict)):
            raise SmokeError(f"evidence {field_path!r} has no assertion")
        rendered = str(assertion).strip()
        if not rendered:
            raise SmokeError(f"evidence {field_path!r} has no assertion")

        why = payload.get("why", "")
        if not isinstance(why, str):
            raise SmokeError(f"evidence {field_path!r}: why must be text")
        return cls(field_path=field_path.strip(), assertion=rendered, why=why.strip())


@dataclass(frozen=True, slots=True)
class Scenario:
    """A loaded scenario file."""

    id: str
    title: str
    path: Path
    requires_live_session: bool
    priority: str
    purpose: str
    preconditions: tuple[str, ...]
    steps: tuple[Step, ...]
    evidence: tuple[EvidenceRequirement, ...]
    fails_if: tuple[str, ...]

    @property
    def manual_steps(self) -> tuple[Step, ...]:
        return tuple(step for step in self.steps if step.manual)

    @property
    def is_automatable(self) -> bool:
        """True when no step needs a human.

        Not one scenario here is fully automatable — every one needs a live
        game — so this reports whether the *steps* could run unattended once a
        session exists, which is what the operator wants to know before
        starting a batch.
        """
        return not self.manual_steps


def _require_text(payload: Mapping[str, Any], key: str, *, path: Path) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SmokeError(f"{path.name}: {key} is missing or empty")
    return value.strip()


def _text_sequence(payload: Mapping[str, Any], key: str, *, path: Path) -> tuple[str, ...]:
    raw = payload.get(key, [])
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SmokeError(f"{path.name}: {key} must be a list")
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise SmokeError(f"{path.name}: {key} contains a non-text entry")
        out.append(entry.strip())
    return tuple(out)


def load_scenario(path: Path) -> Scenario:
    """Parse one scenario file.

    Raises:
        SmokeError: on anything malformed. A scenario that cannot be read is an
            error rather than a skip — silently dropping it would remove a
            check from the release gate without anyone deciding to.
    """
    # Deferred: PyYAML is a dev-extra, and a missing parser should say so
    # rather than surface as an ImportError from inside a CLI command.
    try:
        import yaml  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised by the extra being absent
        raise SmokeError(
            "PyYAML is required to read smoke scenarios; install the 'dev' extra"
        ) from exc

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SmokeError(f"{path}: cannot be read: {exc}") from exc
    if size > MAX_SCENARIO_BYTES:
        raise SmokeError(f"{path.name}: {size} bytes exceeds the {MAX_SCENARIO_BYTES} cap")

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise SmokeError(f"{path.name}: cannot be read: {exc}") from exc
    except yaml.YAMLError as exc:
        raise SmokeError(f"{path.name}: is not valid YAML: {exc}") from exc

    if not isinstance(document, Mapping):
        raise SmokeError(f"{path.name}: must be a mapping at the top level")

    scenario_id = _require_text(document, "id", path=path)
    if len(scenario_id) > _ID_MAX_CHARS or not scenario_id.isalnum():
        raise SmokeError(f"{path.name}: id {scenario_id!r} must be short and alphanumeric")

    raw_steps = document.get("steps", [])
    if not isinstance(raw_steps, list):
        raise SmokeError(f"{path.name}: steps must be a list")
    steps: list[Step] = []
    for position, entry in enumerate(raw_steps, start=1):
        if not isinstance(entry, Mapping):
            raise SmokeError(f"{path.name}: step {position} is not a mapping")
        try:
            steps.append(Step.from_mapping(entry, position=position))
        except SmokeError as exc:
            raise SmokeError(f"{path.name}: {exc}") from exc

    raw_evidence = document.get("evidence", [])
    if not isinstance(raw_evidence, list) or not raw_evidence:
        raise SmokeError(f"{path.name}: evidence must be a non-empty list")
    if len(raw_evidence) > MAX_EVIDENCE_ENTRIES:
        raise SmokeError(
            f"{path.name}: {len(raw_evidence)} evidence entries exceeds "
            f"the {MAX_EVIDENCE_ENTRIES} cap"
        )
    evidence: list[EvidenceRequirement] = []
    for entry in raw_evidence:
        if not isinstance(entry, Mapping):
            raise SmokeError(f"{path.name}: an evidence entry is not a mapping")
        try:
            evidence.append(EvidenceRequirement.from_mapping(entry))
        except SmokeError as exc:
            raise SmokeError(f"{path.name}: {exc}") from exc

    requires_live = document.get("requires_live_session", True)
    if not isinstance(requires_live, bool):
        raise SmokeError(f"{path.name}: requires_live_session must be a boolean")

    return Scenario(
        id=scenario_id,
        title=_require_text(document, "title", path=path),
        path=path,
        requires_live_session=requires_live,
        priority=str(document.get("priority", "normal")),
        purpose=str(document.get("purpose", "")).strip(),
        preconditions=_text_sequence(document, "preconditions", path=path),
        steps=tuple(steps),
        evidence=tuple(evidence),
        fails_if=_text_sequence(document, "fails_if", path=path),
    )


def load_scenarios(directory: Path) -> tuple[Scenario, ...]:
    """Load every scenario in *directory*, sorted by id.

    Raises:
        SmokeError: if the directory holds no scenarios, or any of them is
            malformed, or two share an id.
    """
    if not directory.is_dir():
        raise SmokeError(f"{directory}: not a directory")
    paths = sorted(p for p in directory.glob("*.yaml") if p.is_file())
    if not paths:
        raise SmokeError(f"{directory}: contains no scenario files")

    scenarios = [load_scenario(path) for path in paths]
    seen: dict[str, Path] = {}
    for scenario in scenarios:
        if scenario.id in seen:
            raise SmokeError(
                f"scenario id {scenario.id!r} used by both "
                f"{seen[scenario.id].name} and {scenario.path.name}"
            )
        seen[scenario.id] = scenario.path
    return tuple(sorted(scenarios, key=lambda s: s.id))


@dataclass(frozen=True, slots=True)
class CollectedValue:
    """One observed value, paired with the assertion it was collected for."""

    field_path: str
    assertion: str
    value: Any
    observed: bool

    def to_dict(self) -> JsonDict:
        return {
            "field": self.field_path,
            "assertion": self.assertion,
            "value": self.value,
            "observed": self.observed,
        }


@dataclass(slots=True)
class ScenarioResult:
    """What happened to one scenario, and why.

    Starts at ``NOT_RUN``. Nothing promotes it except a run that reached a
    conclusion, so a crash, a filter or an abandoned batch leaves it honest.
    """

    scenario_id: str
    title: str
    outcome: Outcome = Outcome.NOT_RUN
    reason: str = ""
    collected: list[CollectedValue] = field(default_factory=list)
    manual_steps_pending: list[str] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return {
            "scenario_id": self.scenario_id,
            "title": self.title,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "evidence": [value.to_dict() for value in self.collected],
            "manual_steps_pending": list(self.manual_steps_pending),
        }


@dataclass(frozen=True, slots=True)
class RunStamp:
    """Which build produced an evidence file.

    Evidence from an unknown build closes nothing: the whole point of a smoke
    run is that it happened against a particular game, and a file that cannot
    say which one is a note, not an artefact.
    """

    build: str
    mod_version: str
    product_version: str
    schema_version: str
    session_id: str
    timestamp_ms: int

    def to_dict(self) -> JsonDict:
        return {
            "build": self.build,
            "mod_version": self.mod_version,
            "product_version": self.product_version,
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "timestamp_ms": self.timestamp_ms,
        }


def dry_run_stamp(*, timestamp_ms: int) -> RunStamp:
    """The stamp for a dry run, which touched no game.

    The build is recorded as absent rather than guessed, so a dry-run artefact
    cannot be mistaken for one that ran against an installation.
    """
    return RunStamp(
        build="(not detected — dry run)",
        mod_version=MOD_VERSION,
        product_version=PRODUCT_VERSION,
        schema_version=SCHEMA_VERSION,
        session_id="(no session — dry run)",
        timestamp_ms=timestamp_ms,
    )


@dataclass(slots=True)
class SmokeReport:
    """The whole run: every requested scenario, with its outcome."""

    stamp: RunStamp
    results: list[ScenarioResult] = field(default_factory=list)
    dry_run: bool = False

    def counts(self) -> dict[str, int]:
        tally = {outcome.value: 0 for outcome in Outcome}
        for result in self.results:
            tally[result.outcome.value] += 1
        return tally

    @property
    def any_conclusive(self) -> bool:
        return any(result.outcome.is_conclusive for result in self.results)

    @property
    def failed(self) -> bool:
        return any(result.outcome is Outcome.FAILED for result in self.results)

    def to_dict(self) -> JsonDict:
        return {
            "dry_run": self.dry_run,
            "stamp": self.stamp.to_dict(),
            "counts": self.counts(),
            "scenarios": [result.to_dict() for result in self.results],
        }

    def render(self) -> list[str]:
        """A summary suitable for pasting into the final implementation report."""
        lines = [
            f"pz-agent smoke {'(dry run)' if self.dry_run else ''}".rstrip(),
            f"  build: {self.stamp.build}",
            f"  mod:   {self.stamp.mod_version}   product: {self.stamp.product_version}",
            "",
        ]
        width = max((len(r.scenario_id) for r in self.results), default=2)
        for result in self.results:
            lines.append(
                f"  {result.scenario_id:<{width}}  {result.outcome.value:<8}  {result.title}"
            )
            if result.reason:
                lines.append(f"  {'':<{width}}  {'':<8}  {result.reason}")
        tally = self.counts()
        lines.extend(
            [
                "",
                f"  passed {tally['passed']}   failed {tally['failed']}   "
                f"blocked {tally['blocked']}   not run {tally['not_run']}",
            ]
        )
        if not self.any_conclusive:
            lines.append(
                "  Nothing was exercised. Every scenario above requires a running "
                "Project Zomboid session."
            )
        return lines


def select(
    scenarios: Iterable[Scenario], *, only: Sequence[str] | None = None
) -> tuple[tuple[Scenario, ...], tuple[Scenario, ...]]:
    """Split *scenarios* into (selected, deselected) by id.

    Both halves are returned because the deselected ones still belong in the
    report as ``NOT_RUN``. Filtering a scenario out of a run must not filter it
    out of the accounting.
    """
    everything = tuple(scenarios)
    if not only:
        return everything, ()
    wanted = {token.strip().upper() for token in only if token.strip()}
    unknown = wanted - {s.id.upper() for s in everything}
    if unknown:
        raise SmokeError(f"unknown scenario id(s): {', '.join(sorted(unknown))}")
    selected = tuple(s for s in everything if s.id.upper() in wanted)
    rejected = tuple(s for s in everything if s.id.upper() not in wanted)
    return selected, rejected


def plan_dry_run(
    scenarios: Iterable[Scenario], *, only: Sequence[str] | None = None, timestamp_ms: int
) -> SmokeReport:
    """Validate every scenario and report what a real run would do.

    This is the path that runs in CI. It touches no game, so no scenario can
    pass: each selected one is ``BLOCKED`` on the live session it declares, and
    each deselected one stays ``NOT_RUN``. A dry run that reported a pass would
    be the exact failure this module is built to prevent.
    """
    selected, rejected = select(scenarios, only=only)
    report = SmokeReport(stamp=dry_run_stamp(timestamp_ms=timestamp_ms), dry_run=True)

    for scenario in selected:
        result = ScenarioResult(scenario_id=scenario.id, title=scenario.title)
        result.collected = [
            CollectedValue(
                field_path=requirement.field_path,
                assertion=requirement.assertion,
                value=None,
                observed=False,
            )
            for requirement in scenario.evidence
        ]
        result.manual_steps_pending = [step.action for step in scenario.manual_steps]
        if scenario.requires_live_session:
            result.outcome = Outcome.BLOCKED
            result.reason = "requires a running Project Zomboid session"
        else:
            # No scenario currently declares otherwise, but the loader accepts
            # it, so the branch has to exist rather than assume.
            result.outcome = Outcome.NOT_RUN
            result.reason = "dry run performs no steps"
        report.results.append(result)

    for scenario in rejected:
        report.results.append(
            ScenarioResult(
                scenario_id=scenario.id,
                title=scenario.title,
                outcome=Outcome.NOT_RUN,
                reason="not selected for this run",
            )
        )

    report.results.sort(key=lambda r: r.scenario_id)
    return report


def read_field(source: Mapping[str, Any], dotted: str) -> tuple[Any, bool]:
    """Read ``a.b.c`` out of nested mappings.

    Returns the value and whether it was found, rather than a sentinel: an
    evidence field whose real value is ``None`` and one that was never observed
    mean opposite things, and a runner that conflated them would write an
    evidence file asserting something it never saw.
    """
    current: Any = source
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None, False
        current = current[part]
    return current, True


def collect_evidence(
    scenario: Scenario, observed: Mapping[str, Any]
) -> tuple[list[CollectedValue], list[str]]:
    """Pull each declared evidence field out of *observed*.

    Returns the collected values and the paths that were missing. A missing
    path is never filled with a default — the scenario asked for a specific
    observation, and not making it is a result in itself.
    """
    collected: list[CollectedValue] = []
    missing: list[str] = []
    for requirement in scenario.evidence:
        value, found = read_field(observed, requirement.field_path)
        collected.append(
            CollectedValue(
                field_path=requirement.field_path,
                assertion=requirement.assertion,
                value=value,
                observed=found,
            )
        )
        if not found:
            missing.append(requirement.field_path)
    return collected, missing


def write_evidence(report: SmokeReport, directory: Path) -> list[Path]:
    """Write one evidence file per scenario, plus the summary.

    Every result is written, including ``NOT_RUN`` ones. An evidence directory
    that held only the scenarios that ran would let a reader infer coverage
    from what is present, which is precisely the inference this harness refuses
    to support.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for result in report.results:
        path = directory / f"{result.scenario_id}.json"
        payload = {"stamp": report.stamp.to_dict(), "dry_run": report.dry_run, **result.to_dict()}
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    summary = directory / "summary.json"
    summary.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    written.append(summary)
    return written


def run_smoke(
    *,
    scenario_dir: Path,
    evidence_dir: Path | None,
    only: Sequence[str] | None,
    dry_run: bool,
    timestamp_ms: int,
    emit: Callable[[str], None],
    as_json: bool = False,
) -> int:
    """Handler for ``pz-agent smoke``.

    Returns a non-zero exit code when a scenario failed, when the scenarios
    cannot be loaded, or when a non-dry run was asked for — driving a live
    session is not something this process can do on its own, and pretending
    otherwise by reporting success would be the lie the harness exists to
    prevent.
    """
    try:
        scenarios = load_scenarios(scenario_dir)
    except SmokeError as exc:
        emit(f"smoke: {exc}")
        return 1

    if not dry_run:
        emit(
            "smoke: a live run drives a running Project Zomboid session and is started "
            "from the sidecar, not from this command. Use --dry-run to validate the "
            "scenarios and see what a live run would ask of you."
        )
        return 1

    try:
        report = plan_dry_run(scenarios, only=only, timestamp_ms=timestamp_ms)
    except SmokeError as exc:
        emit(f"smoke: {exc}")
        return 1

    if evidence_dir is not None:
        written = write_evidence(report, evidence_dir)
        emit(f"smoke: wrote {len(written)} file(s) to {evidence_dir}")

    if as_json:
        emit(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        for line in report.render():
            emit(line)

    return 1 if report.failed else 0


def default_scenario_dir() -> Path:
    """Where the scenarios live in a source checkout.

    A packaged install has no ``tests/`` directory, so this resolves relative
    to the repository rather than the installed package and the caller is
    expected to pass ``--scenario-dir`` when running from a wheel. Returning a
    path that does not exist is fine: the loader reports it by name, which is
    more useful than this function guessing.
    """
    return Path(__file__).resolve().parents[4] / "tests" / "game-smoke"
