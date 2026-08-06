"""Per-scenario state: an append-only ledger of attempts.

The state of a scenario is never stored. It is *derived* from the attempts, by
one rule:

    PASS if any attempt passed, otherwise the last attempt's status,
    otherwise NOT_RUN.

Writing the state down would create a second place it could be set, and the
whole design here exists to remove those. Because the rule reads the whole
history, three of the four invariants fall out of it rather than being enforced
by a check somebody could forget to call:

* ``NOT_RUN`` is what an empty ledger derives to, so a scenario nobody ran
  cannot report anything else.
* A pass cannot be overwritten — a later attempt is appended, and the rule still
  finds the passing one. A re-run of a passed scenario is visible in the record
  and does not change the verdict.
* A ``FAIL`` retried after a fix keeps both attempts, because nothing is ever
  removed.

Each attempt also carries a hash chained over its predecessor, so editing an
earlier entry invalidates every entry after it. Like the result digest, that is
a tripwire rather than a seal: there is no key, and this project ships no
secrets. It catches the edit somebody actually makes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from pz_agent_core.protocol import JsonDict

from .evidence import LiveTestError, canonical_json, sha256_text, write_document

#: Attempts one scenario may accumulate. High enough that a genuinely stubborn
#: scenario is not cut off mid-investigation, low enough that a runaway loop
#: cannot grow the ledger without bound.
MAX_ATTEMPTS: Final = 50

#: Format marker on the ledger file, so a future change to the shape is a
#: refusal to read rather than a misread.
LEDGER_FORMAT: Final = "pz-agent/livetest-state/1"


class LedgerError(LiveTestError):
    """The attempt ledger cannot be read, or does not verify."""


class LiveState(StrEnum):
    """What a scenario has been shown to do.

    ``BLOCKED`` and ``NOT_RUN`` are separate on purpose, and neither is a
    failure. ``NOT_RUN`` means nobody tried; ``BLOCKED`` means somebody tried
    and the attempt could not reach a verdict — no game session, no
    observations, a precondition the world did not satisfy. Collapsing them
    would hide which of the two a release is missing.
    """

    NOT_RUN = "NOT_RUN"
    PASS = "PASS"  # noqa: S105 — a verdict, not a credential
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"

    @property
    def is_conclusive(self) -> bool:
        """True when the attempt actually exercised the scenario."""
        return self in {LiveState.PASS, LiveState.FAIL}


@dataclass(frozen=True, slots=True)
class Attempt:
    """One run of one scenario. Immutable once appended."""

    number: int
    status: LiveState
    started_at_ms: int
    finished_at_ms: int
    result_sha256: str
    failure_code: str = ""
    note: str = ""
    chain: str = ""

    def payload(self) -> JsonDict:
        """The fields the chain hash covers — everything except the hash itself."""
        return {
            "number": self.number,
            "status": self.status.value,
            "started_at_ms": self.started_at_ms,
            "finished_at_ms": self.finished_at_ms,
            "result_sha256": self.result_sha256,
            "failure_code": self.failure_code,
            "note": self.note,
        }

    def to_dict(self) -> JsonDict:
        return {**self.payload(), "chain": self.chain}

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> Attempt:
        try:
            status = LiveState(str(document["status"]))
        except (KeyError, ValueError) as exc:
            raise LedgerError(f"attempt has no usable status: {document.get('status')!r}") from exc
        try:
            return cls(
                number=int(document["number"]),
                status=status,
                started_at_ms=int(document["started_at_ms"]),
                finished_at_ms=int(document["finished_at_ms"]),
                result_sha256=str(document["result_sha256"]),
                failure_code=str(document.get("failure_code", "")),
                note=str(document.get("note", "")),
                chain=str(document.get("chain", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerError(f"attempt is malformed: {exc}") from exc


def genesis_chain(scenario_id: str) -> str:
    """The chain value the first attempt links to.

    Seeded from the scenario id so an entry cannot be lifted wholesale out of
    one scenario's ledger and pasted into another's.
    """
    return sha256_text(f"{LEDGER_FORMAT}:{scenario_id}")


def _chain(previous: str, attempt: Attempt) -> str:
    return sha256_text(previous + canonical_json(attempt.payload()))


@dataclass(frozen=True, slots=True)
class ScenarioState:
    """The whole history of one scenario."""

    scenario_id: str
    attempts: tuple[Attempt, ...] = ()

    @property
    def state(self) -> LiveState:
        """The derived verdict. The one rule this module exists for."""
        for attempt in self.attempts:
            if attempt.status is LiveState.PASS:
                return LiveState.PASS
        return self.attempts[-1].status if self.attempts else LiveState.NOT_RUN

    @property
    def passing_attempt(self) -> Attempt | None:
        """The attempt that established the pass, if there is one."""
        for attempt in self.attempts:
            if attempt.status is LiveState.PASS:
                return attempt
        return None

    @property
    def last_attempt(self) -> Attempt | None:
        return self.attempts[-1] if self.attempts else None

    @property
    def last_run_ms(self) -> int | None:
        last = self.last_attempt
        return None if last is None else last.finished_at_ms

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    def appended(self, attempt: Attempt) -> ScenarioState:
        """This state with *attempt* chained onto the end.

        Raises:
            LedgerError: past :data:`MAX_ATTEMPTS`.
        """
        if len(self.attempts) >= MAX_ATTEMPTS:
            raise LedgerError(
                f"{self.scenario_id}: {MAX_ATTEMPTS} attempts already recorded. "
                "Something is being retried without being fixed."
            )
        previous = self.attempts[-1].chain if self.attempts else genesis_chain(self.scenario_id)
        numbered = Attempt(
            number=len(self.attempts) + 1,
            status=attempt.status,
            started_at_ms=attempt.started_at_ms,
            finished_at_ms=attempt.finished_at_ms,
            result_sha256=attempt.result_sha256,
            failure_code=attempt.failure_code,
            note=attempt.note,
        )
        return ScenarioState(
            scenario_id=self.scenario_id,
            attempts=(*self.attempts, _with_chain(numbered, _chain(previous, numbered))),
        )

    def verify_chain(self) -> None:
        """Recompute the chain over every attempt.

        Raises:
            LedgerError: naming the first attempt that does not verify. A break
                means an earlier entry was edited after it was written, and
                everything after it is equally untrustworthy.
        """
        previous = genesis_chain(self.scenario_id)
        for position, attempt in enumerate(self.attempts, start=1):
            if attempt.number != position:
                raise LedgerError(
                    f"{self.scenario_id}: attempt {position} is numbered {attempt.number}; "
                    "an entry was removed or reordered"
                )
            if attempt.chain != _chain(previous, attempt):
                raise LedgerError(
                    f"{self.scenario_id}: attempt {position} does not verify against the "
                    "attempt before it. The ledger was edited after it was written; "
                    "re-run the scenarios rather than trusting this record."
                )
            previous = attempt.chain

    def to_dict(self) -> JsonDict:
        return {
            "format": LEDGER_FORMAT,
            "scenario_id": self.scenario_id,
            "state": self.state.value,
            "attempt_count": self.attempt_count,
            "last_run_ms": self.last_run_ms,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any], *, scenario_id: str) -> ScenarioState:
        recorded_format = document.get("format")
        if recorded_format != LEDGER_FORMAT:
            raise LedgerError(
                f"{scenario_id}: ledger format {recorded_format!r} is not {LEDGER_FORMAT!r}"
            )
        if document.get("scenario_id") != scenario_id:
            raise LedgerError(f"{scenario_id}: ledger belongs to {document.get('scenario_id')!r}")
        raw = document.get("attempts", [])
        if not isinstance(raw, list):
            raise LedgerError(f"{scenario_id}: attempts must be a list")
        attempts = tuple(Attempt.from_dict(entry) for entry in _mappings(raw, scenario_id))
        state = cls(scenario_id=scenario_id, attempts=attempts)
        state.verify_chain()
        return state


def _with_chain(attempt: Attempt, chain: str) -> Attempt:
    return Attempt(
        number=attempt.number,
        status=attempt.status,
        started_at_ms=attempt.started_at_ms,
        finished_at_ms=attempt.finished_at_ms,
        result_sha256=attempt.result_sha256,
        failure_code=attempt.failure_code,
        note=attempt.note,
        chain=chain,
    )


def _mappings(raw: Sequence[Any], scenario_id: str) -> list[Mapping[str, Any]]:
    entries: list[Mapping[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise LedgerError(f"{scenario_id}: an attempt entry is not a mapping")
        entries.append(entry)
    return entries


@dataclass(frozen=True, slots=True)
class StateStore:
    """Reads and writes one ``state.json`` per scenario directory.

    There is no method here that sets a state. The only mutation is
    :meth:`record`, which appends an attempt; everything else derives.
    """

    root: Path

    def path_for(self, scenario_id: str) -> Path:
        return self.root / scenario_id / "state.json"

    def read(self, scenario_id: str) -> ScenarioState:
        """The ledger for *scenario_id*, or an empty one if it has never run.

        A missing file is ``NOT_RUN`` rather than an error: that is exactly
        what "nobody has run this" looks like on disk before ``prepare``.

        Raises:
            LedgerError: when the file exists but is unreadable, malformed, or
                fails chain verification.
        """
        path = self.path_for(scenario_id)
        if not path.is_file():
            return ScenarioState(scenario_id=scenario_id)
        # Read directly rather than through read_document: the ledger records
        # digests, it is not covered by one, and a JSON error here has to be
        # reported as a ledger problem.
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            raise LedgerError(f"{scenario_id}: state.json cannot be read: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise LedgerError(
                f"{scenario_id}: state.json is not valid JSON at line {exc.lineno}: {exc.msg}"
            ) from exc
        if not isinstance(document, dict):
            raise LedgerError(f"{scenario_id}: state.json must be a JSON object")
        return ScenarioState.from_dict(document, scenario_id=scenario_id)

    def write(self, state: ScenarioState) -> Path:
        """Persist a ledger. Verifies the chain first, so a corrupt one is never stored."""
        state.verify_chain()
        path = self.path_for(state.scenario_id)
        write_document(path, state.to_dict(), schema=None)
        return path

    def initialise(self, scenario_ids: Sequence[str]) -> tuple[str, ...]:
        """Create ledgers for scenarios that have none. Returns the ids created.

        Never touches an existing ledger. ``prepare`` is run again after a
        failed session, and re-initialising would erase the record of the
        attempts that failed — which is the record the operator needs most.
        """
        created: list[str] = []
        for scenario_id in scenario_ids:
            if self.path_for(scenario_id).is_file():
                continue
            self.write(ScenarioState(scenario_id=scenario_id))
            created.append(scenario_id)
        return tuple(created)

    def record(
        self,
        scenario_id: str,
        *,
        status: LiveState,
        started_at_ms: int,
        finished_at_ms: int,
        result_sha256: str,
        failure_code: str = "",
        note: str = "",
    ) -> ScenarioState:
        """Append one attempt and persist the ledger.

        Returns the state *after* the append, whose derived verdict may well be
        ``PASS`` when the appended attempt failed — that is the invariant
        working, not a bug.
        """
        current = self.read(scenario_id)
        updated = current.appended(
            Attempt(
                number=0,
                status=status,
                started_at_ms=started_at_ms,
                finished_at_ms=finished_at_ms,
                result_sha256=result_sha256,
                failure_code=failure_code,
                note=note,
            )
        )
        self.write(updated)
        return updated

    def read_all(self, scenario_ids: Sequence[str]) -> tuple[ScenarioState, ...]:
        return tuple(self.read(scenario_id) for scenario_id in scenario_ids)
