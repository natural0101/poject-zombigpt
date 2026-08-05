"""What the agent may claim about the game's API, and what it must prove first.

The whole subsystem exists to make one failure impossible: publishing a write
tool because a wiki, a stub repository or an old mod said the API exists
(docs/blueprint/12_COMPATIBILITY_AND_RESEARCH.md §12.7, §3.8).

Two invariants are enforced here, in the constructor, so no code path can route
around them:

* ``verified`` requires at least one piece of :data:`EvidenceKind.RUNTIME_PROBE`
  evidence — the same rule, and the same enforcement point, as
  :meth:`~pz_agent_core.protocol.ActionResult.succeeded`. A static scan of the
  installed Lua can never reach ``verified``; it proves a symbol is on disk, not
  that calling it does anything.
* ``unsupported``, ``experimental`` and ``disabled_by_policy`` require a reason,
  because every one of them ends up in front of the user as a refusal.

The report is bounded in every dimension: capability count, evidence per
capability, and the length of each free-text field.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from ..protocol import ActionResult, ActionStatus, CapabilityState
from ..version import PROTOCOL_VERSION

JsonDict = dict[str, Any]

#: A report with more entries than this is a producer bug, not a rich install.
MAX_CAPABILITIES: Final = 64

#: Evidence accumulates across probe runs; keep only the most recent few so a
#: long-lived report cannot grow without bound.
MAX_EVIDENCE_PER_CAPABILITY: Final = 8

#: Notes are diagnostics attached by :meth:`CapabilityReport.for_build` and by
#: whatever loaded the report. They are read back from a file this process did
#: not necessarily write, so both their count and their length are capped.
MAX_NOTES: Final = 32

MAX_NAME_LEN: Final = 64
MAX_SYMBOL_LEN: Final = 200
MAX_REASON_LEN: Final = 200
MAX_DETAIL_LEN: Final = 300
MAX_NOTE_LEN: Final = 300
MAX_PATH_LEN: Final = 400

_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]*$")

#: sha256, lowercase hex.
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")

# --- reason vocabulary -----------------------------------------------------
# These strings are user-visible and end up in the generated report, so they are
# treated as append-only in the same way as protocol reason codes.

#: No API exists that the project is willing to drive. Not "not probed yet" —
#: this is a permanent refusal until a human revisits the design.
REASON_NO_VERIFIED_API: Final = "NO_VERIFIED_API"

#: The local install does not define a symbol the probe requires.
REASON_SYMBOL_MISSING: Final = "SYMBOL_MISSING"

#: The API is present but the project does not trust it unattended.
REASON_EXPERIMENTAL_API: Final = "EXPERIMENTAL_API"

#: A report was loaded whose build differs from the installed one.
REASON_BUILD_CHANGED: Final = "BUILD_CHANGED"

#: The user or a policy switch turned the capability off.
REASON_POLICY_DISABLED: Final = "DISABLED_BY_POLICY"

#: A runtime confirmation was attempted and did not arrive.
REASON_PROBE_NOT_CONFIRMED: Final = "PROBE_NOT_CONFIRMED"


class CapabilityError(ValueError):
    """A capability claim is malformed or unproven."""


class EvidenceKind(StrEnum):
    """Where a claim came from.

    The distinction is the entire point of the type: only ``runtime_probe``
    evidence describes something that actually happened in a running game.
    """

    STATIC_SCAN = "static_scan"
    RUNTIME_PROBE = "runtime_probe"


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string, second resolution."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _check_text(value: str, *, field_name: str, max_len: int, required: bool) -> str:
    if required and not value:
        raise CapabilityError(f"{field_name} must not be empty")
    if len(value) > max_len:
        raise CapabilityError(f"{field_name} must be at most {max_len} characters")
    return value


def _check_timestamp(value: str) -> str:
    if not value:
        raise CapabilityError("observed_at must not be empty")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise CapabilityError(f"observed_at must be ISO-8601, got {value!r}") from exc
    return value


def _as_str(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise CapabilityError(f"{field_name} must be a string, got {type(value).__name__}")
    return value


def _as_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CapabilityError(f"{field_name} must be an integer, got {type(value).__name__}")
    return value


def _as_mapping(value: object, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CapabilityError(f"{field_name} must be an object, got {type(value).__name__}")
    return value


def _as_sequence(value: object, *, field_name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CapabilityError(f"{field_name} must be an array, got {type(value).__name__}")
    return value


def _require(payload: Mapping[str, Any], key: str) -> Any:
    if key not in payload:
        raise CapabilityError(f"missing required field {key!r}")
    return payload[key]


@dataclass(frozen=True, slots=True)
class Evidence:
    """One reason to believe a capability claim.

    ``detail`` holds a *canonical* signature for static evidence — the symbol and
    its parameter names as the scanner reconstructed them — never a line copied
    out of the game's source. Vanilla Lua may not be redistributed (§12.7), and a
    report that embedded a source snippet would be exactly that.
    """

    kind: EvidenceKind
    symbol: str
    observed_at: str
    probe: str = ""
    file: str = ""
    file_sha256: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        _check_text(self.symbol, field_name="symbol", max_len=MAX_SYMBOL_LEN, required=True)
        _check_text(self.detail, field_name="detail", max_len=MAX_DETAIL_LEN, required=False)
        _check_text(self.file, field_name="file", max_len=MAX_PATH_LEN, required=False)
        _check_text(self.probe, field_name="probe", max_len=MAX_NAME_LEN, required=False)
        _check_timestamp(self.observed_at)
        if self.kind is EvidenceKind.STATIC_SCAN:
            if not self.file:
                raise CapabilityError("static evidence must name the file the symbol was found in")
            if not _SHA256_RE.match(self.file_sha256):
                raise CapabilityError("static evidence must carry the file's sha256")
            if self.probe:
                raise CapabilityError("static evidence must not claim a probe ran")
        elif not self.probe:
            raise CapabilityError("runtime evidence must name the probe that produced it")

    @classmethod
    def from_scan(
        cls,
        *,
        symbol: str,
        file: str,
        file_sha256: str,
        signature: str,
        observed_at: str,
    ) -> Evidence:
        """Record that *symbol* was found on disk. Never enough for ``verified``."""
        return cls(
            kind=EvidenceKind.STATIC_SCAN,
            symbol=symbol,
            observed_at=observed_at,
            file=file,
            file_sha256=file_sha256,
            detail=signature[:MAX_DETAIL_LEN],
        )

    @classmethod
    def from_ack(
        cls,
        *,
        probe: str,
        symbol: str,
        ack: ActionResult,
        observed_at: str,
    ) -> Evidence:
        """Record that a probe command came back with an observed postcondition.

        This is the only constructor that yields ``runtime_probe`` evidence, and
        it refuses anything that is not a succeeded ack carrying postcondition
        evidence. Since ``verified`` requires runtime evidence, the chain
        ``verified -> runtime evidence -> succeeded ack -> observed
        postcondition`` has no shortcut.
        """
        if ack.status is not ActionStatus.SUCCEEDED:
            raise CapabilityError(
                f"probe {probe!r} cannot be evidence: ack status is {ack.status.value}"
            )
        if not ack.evidence:
            raise CapabilityError(f"probe {probe!r} ack carries no postcondition evidence")
        return cls(
            kind=EvidenceKind.RUNTIME_PROBE,
            symbol=symbol,
            observed_at=observed_at,
            probe=probe,
            detail=f"{ack.action} command_id={ack.command_id}"[:MAX_DETAIL_LEN],
        )

    def to_dict(self) -> JsonDict:
        out: JsonDict = {
            "kind": self.kind.value,
            "symbol": self.symbol,
            "observed_at": self.observed_at,
        }
        for key, value in (
            ("probe", self.probe),
            ("file", self.file),
            ("file_sha256", self.file_sha256),
            ("detail", self.detail),
        ):
            if value:
                out[key] = value
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Evidence:
        raw_kind = _as_str(_require(payload, "kind"), field_name="evidence.kind")
        try:
            kind = EvidenceKind(raw_kind)
        except ValueError as exc:
            raise CapabilityError(f"unknown evidence kind {raw_kind!r}") from exc
        return cls(
            kind=kind,
            symbol=_as_str(_require(payload, "symbol"), field_name="evidence.symbol"),
            observed_at=_as_str(
                _require(payload, "observed_at"), field_name="evidence.observed_at"
            ),
            probe=_as_str(payload.get("probe", ""), field_name="evidence.probe"),
            file=_as_str(payload.get("file", ""), field_name="evidence.file"),
            file_sha256=_as_str(payload.get("file_sha256", ""), field_name="evidence.file_sha256"),
            detail=_as_str(payload.get("detail", ""), field_name="evidence.detail"),
        )


@dataclass(frozen=True, slots=True)
class Capability:
    """One named ability, its state, and why the agent believes that state.

    ``build`` is part of the claim, not decoration: a probe that passed on 42.19
    says nothing about 42.20, and :meth:`CapabilityReport.for_build` uses this
    field to strip the claims that no longer apply.
    """

    name: str
    state: CapabilityState
    reason: str = ""
    build: str = ""
    evidence: tuple[Evidence, ...] = ()

    def __post_init__(self) -> None:
        _check_text(self.name, field_name="name", max_len=MAX_NAME_LEN, required=True)
        if not _NAME_RE.match(self.name):
            raise CapabilityError(f"capability name must be lower_snake_case, got {self.name!r}")
        _check_text(self.reason, field_name="reason", max_len=MAX_REASON_LEN, required=False)
        _check_text(self.build, field_name="build", max_len=MAX_NAME_LEN, required=False)
        if len(self.evidence) > MAX_EVIDENCE_PER_CAPABILITY:
            raise CapabilityError(
                f"{self.name}: at most {MAX_EVIDENCE_PER_CAPABILITY} evidence entries"
            )
        if self.state is CapabilityState.VERIFIED:
            if not any(e.kind is EvidenceKind.RUNTIME_PROBE for e in self.evidence):
                raise CapabilityError(
                    f"{self.name}: 'verified' requires runtime probe evidence; "
                    "a static scan only proves the symbol exists on disk"
                )
            if not self.build:
                raise CapabilityError(
                    f"{self.name}: 'verified' requires the build it was proven on"
                )
        elif self.state is CapabilityState.AVAILABLE_UNVERIFIED and not self.evidence:
            raise CapabilityError(
                f"{self.name}: 'available_unverified' requires at least one piece of evidence"
            )
        elif self.state in _REASON_REQUIRED_STATES and not self.reason:
            raise CapabilityError(f"{self.name}: state {self.state.value} requires a reason")

    # --- construction ------------------------------------------------------

    @classmethod
    def verified(
        cls,
        *,
        name: str,
        build: str,
        evidence: Sequence[Evidence],
        reason: str = "",
    ) -> Capability:
        """Build a verified capability. Requires runtime probe evidence."""
        return cls(
            name=name,
            state=CapabilityState.VERIFIED,
            reason=reason,
            build=build,
            evidence=_bounded_evidence(evidence),
        )

    @classmethod
    def available_unverified(
        cls,
        *,
        name: str,
        build: str,
        evidence: Sequence[Evidence],
        reason: str = "",
    ) -> Capability:
        """The symbols are present; nothing has been executed against them."""
        return cls(
            name=name,
            state=CapabilityState.AVAILABLE_UNVERIFIED,
            reason=reason,
            build=build,
            evidence=_bounded_evidence(evidence),
        )

    @classmethod
    def experimental(
        cls,
        *,
        name: str,
        build: str,
        reason: str,
        evidence: Sequence[Evidence] = (),
    ) -> Capability:
        """Present, but not trusted enough to publish as a ready write tool."""
        return cls(
            name=name,
            state=CapabilityState.EXPERIMENTAL,
            reason=reason,
            build=build,
            evidence=_bounded_evidence(evidence),
        )

    @classmethod
    def unsupported(
        cls,
        *,
        name: str,
        reason: str,
        build: str = "",
        evidence: Sequence[Evidence] = (),
    ) -> Capability:
        """Not available. The reason is mandatory — it is shown to the user."""
        return cls(
            name=name,
            state=CapabilityState.UNSUPPORTED,
            reason=reason,
            build=build,
            evidence=_bounded_evidence(evidence),
        )

    @classmethod
    def disabled_by_policy(
        cls,
        *,
        name: str,
        reason: str = REASON_POLICY_DISABLED,
        build: str = "",
    ) -> Capability:
        """Turned off deliberately; the API question is not even asked."""
        return cls(
            name=name,
            state=CapabilityState.DISABLED_BY_POLICY,
            reason=reason,
            build=build,
        )

    # --- queries -----------------------------------------------------------

    @property
    def usable(self) -> bool:
        """True when an MCP write tool backed by this may be published as ready."""
        return self.state.usable

    @property
    def has_runtime_evidence(self) -> bool:
        return any(e.kind is EvidenceKind.RUNTIME_PROBE for e in self.evidence)

    def static_symbols(self) -> tuple[str, ...]:
        return tuple(e.symbol for e in self.evidence if e.kind is EvidenceKind.STATIC_SCAN)

    def downgraded(self, *, build: str, reason: str) -> Capability:
        """Return this capability with every runtime claim dropped.

        Runtime evidence is discarded rather than kept alongside a weaker state:
        it was gathered against a different build, so leaving it in the report
        would let a later reader reconstruct a ``verified`` claim from it.
        """
        static_only = tuple(e for e in self.evidence if e.kind is EvidenceKind.STATIC_SCAN)
        if self.state is not CapabilityState.VERIFIED:
            if not self.has_runtime_evidence:
                return Capability(
                    name=self.name,
                    state=self.state,
                    reason=self.reason,
                    build=build,
                    evidence=self.evidence,
                )
            if static_only or self.state not in _EVIDENCE_REQUIRED_STATES:
                return Capability(
                    name=self.name,
                    state=self.state,
                    reason=self.reason,
                    build=build,
                    evidence=static_only,
                )
            # An ``available_unverified`` claim resting only on the discarded run
            # has nothing left to stand on. Keeping the state would be a claim
            # without evidence; raising here would abort the whole load.
            return Capability(
                name=self.name,
                state=CapabilityState.UNSUPPORTED,
                reason=reason,
                build=build,
            )
        if static_only:
            return Capability(
                name=self.name,
                state=CapabilityState.AVAILABLE_UNVERIFIED,
                reason=reason,
                build=build,
                evidence=static_only,
            )
        # Nothing survives: the only support for the claim was the discarded run.
        return Capability(
            name=self.name,
            state=CapabilityState.UNSUPPORTED,
            reason=reason,
            build=build,
        )

    # --- serialisation -----------------------------------------------------

    def to_dict(self) -> JsonDict:
        out: JsonDict = {"state": self.state.value}
        if self.reason:
            out["reason"] = self.reason
        if self.build:
            out["build"] = self.build
        if self.evidence:
            out["evidence"] = [e.to_dict() for e in self.evidence]
        return out

    @classmethod
    def from_dict(cls, name: str, payload: Mapping[str, Any]) -> Capability:
        raw_state = _as_str(_require(payload, "state"), field_name=f"{name}.state")
        try:
            state = CapabilityState(raw_state)
        except ValueError as exc:
            raise CapabilityError(f"{name}: unknown capability state {raw_state!r}") from exc
        raw_evidence = _as_sequence(payload.get("evidence", []), field_name=f"{name}.evidence")
        if len(raw_evidence) > MAX_EVIDENCE_PER_CAPABILITY:
            raise CapabilityError(f"{name}: at most {MAX_EVIDENCE_PER_CAPABILITY} evidence entries")
        return cls(
            name=name,
            state=state,
            reason=_as_str(payload.get("reason", ""), field_name=f"{name}.reason"),
            build=_as_str(payload.get("build", ""), field_name=f"{name}.build"),
            evidence=tuple(
                Evidence.from_dict(_as_mapping(item, field_name=f"{name}.evidence[]"))
                for item in raw_evidence
            ),
        )


_REASON_REQUIRED_STATES: Final = frozenset(
    {
        CapabilityState.EXPERIMENTAL,
        CapabilityState.UNSUPPORTED,
        CapabilityState.DISABLED_BY_POLICY,
    }
)

#: States whose constructor refuses an empty evidence tuple.
_EVIDENCE_REQUIRED_STATES: Final = frozenset(
    {CapabilityState.VERIFIED, CapabilityState.AVAILABLE_UNVERIFIED}
)


def _by_name(capability: Capability) -> str:
    return capability.name


def _bounded_evidence(evidence: Sequence[Evidence]) -> tuple[Evidence, ...]:
    """Keep the most recent entries; older ones are diagnostics, not proof."""
    items = tuple(evidence)
    if len(items) <= MAX_EVIDENCE_PER_CAPABILITY:
        return items
    return items[-MAX_EVIDENCE_PER_CAPABILITY:]


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    """The full capability set for one build (§3.8).

    ``revision`` is the counter the mod echoes back on every observation as
    ``capability_revision``. It increases whenever the set changes, so a consumer
    that cached a tool list can tell that its cache is stale without diffing.
    """

    build: str
    capabilities: tuple[Capability, ...] = ()
    revision: int = 0
    protocol_version: str = PROTOCOL_VERSION
    generated_at: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _check_text(self.build, field_name="build", max_len=MAX_NAME_LEN, required=True)
        if self.revision < 0:
            raise CapabilityError(f"revision must be non-negative, got {self.revision}")
        if len(self.capabilities) > MAX_CAPABILITIES:
            raise CapabilityError(f"a report may hold at most {MAX_CAPABILITIES} capabilities")
        names = [c.name for c in self.capabilities]
        if len(set(names)) != len(names):
            raise CapabilityError("a report may not list the same capability twice")
        for capability in self.capabilities:
            if capability.build and capability.build != self.build:
                # A claim proven on another build inside a report that says it
                # describes this one would pass `usable()` on the strength of
                # evidence about a different game. `for_build` is the only way to
                # carry claims across a build change, and it downgrades them.
                raise CapabilityError(
                    f"{capability.name}: proven on build {capability.build}, "
                    f"but this report describes {self.build}; rebase it with for_build()"
                )
        if self.generated_at:
            _check_timestamp(self.generated_at)
        if len(self.notes) > MAX_NOTES:
            raise CapabilityError(f"a report may hold at most {MAX_NOTES} notes")
        for note in self.notes:
            _check_text(note, field_name="note", max_len=MAX_NOTE_LEN, required=False)
        if names != sorted(names):
            # Storing them sorted keeps equality — and therefore the "did the set
            # change?" test behind the revision counter — independent of the
            # order a caller happened to build them in.
            object.__setattr__(self, "capabilities", tuple(sorted(self.capabilities, key=_by_name)))

    # --- queries -----------------------------------------------------------

    def get(self, name: str) -> Capability | None:
        return next((c for c in self.capabilities if c.name == name), None)

    def state(self, name: str) -> CapabilityState:
        """State of *name*; an absent capability is ``unsupported``, not missing.

        Defaulting to ``unsupported`` is the safe direction: a caller that
        misspells a name gets a refusal rather than an unguarded write.
        """
        capability = self.get(name)
        return CapabilityState.UNSUPPORTED if capability is None else capability.state

    def usable(self, name: str) -> bool:
        """Gate for publishing an MCP write tool as ready.

        ``experimental`` and ``unsupported`` are both false here: §3.8 forbids
        publishing an unsupported capability as a ready tool, and an experimental
        one is by definition not proven.
        """
        capability = self.get(name)
        return capability is not None and capability.usable

    def usable_names(self) -> tuple[str, ...]:
        return tuple(sorted(c.name for c in self.capabilities if c.usable))

    def unusable_reasons(self) -> dict[str, str]:
        """Name → reason for every capability that must not be published."""
        return {c.name: c.reason or c.state.value for c in self.capabilities if not c.usable}

    # --- derivation --------------------------------------------------------

    def with_capabilities(self, updated: Iterable[Capability]) -> CapabilityReport:
        """Return a new report with *updated* replacing or extending the set."""
        merged = {c.name: c for c in self.capabilities}
        changed = False
        for capability in updated:
            if merged.get(capability.name) != capability:
                changed = True
            merged[capability.name] = capability
        return CapabilityReport(
            build=self.build,
            capabilities=tuple(sorted(merged.values(), key=_by_name)),
            revision=self.revision + 1 if changed else self.revision,
            protocol_version=self.protocol_version,
            generated_at=self.generated_at,
            notes=self.notes,
        )

    def for_build(self, build: str, *, reason: str = REASON_BUILD_CHANGED) -> CapabilityReport:
        """Re-state this report against *build*, dropping claims it cannot support.

        A report generated on another build keeps its static findings — the file
        list is a starting point for the next scan — but every ``verified``
        capability is downgraded. 42.19 proves nothing about 42.20 (§12.6).
        """
        if build == self.build:
            return self
        downgraded = tuple(c.downgraded(build=build, reason=reason) for c in self.capabilities)
        note = f"downgraded from build {self.build} to {build}: {reason}"[:MAX_NOTE_LEN]
        return CapabilityReport(
            build=build,
            capabilities=downgraded,
            revision=self.revision + 1,
            protocol_version=self.protocol_version,
            generated_at=self.generated_at,
            notes=(*self.notes, note)[-MAX_NOTES:],
        )

    # --- serialisation -----------------------------------------------------

    def to_dict(self) -> JsonDict:
        out: JsonDict = {
            "build": self.build,
            "protocol_version": self.protocol_version,
            "revision": self.revision,
            "capabilities": {c.name: c.to_dict() for c in self.capabilities},
        }
        if self.generated_at:
            out["generated_at"] = self.generated_at
        if self.notes:
            out["notes"] = list(self.notes)
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CapabilityReport:
        raw = _as_mapping(_require(payload, "capabilities"), field_name="capabilities")
        if len(raw) > MAX_CAPABILITIES:
            raise CapabilityError(f"a report may hold at most {MAX_CAPABILITIES} capabilities")
        capabilities = tuple(
            Capability.from_dict(
                _as_str(name, field_name="capabilities key"),
                _as_mapping(body, field_name=f"capabilities.{name}"),
            )
            for name, body in sorted(raw.items())
        )
        notes = _as_sequence(payload.get("notes", []), field_name="notes")
        return cls(
            build=_as_str(_require(payload, "build"), field_name="build"),
            capabilities=capabilities,
            revision=_as_int(payload.get("revision", 0), field_name="revision"),
            protocol_version=_as_str(
                payload.get("protocol_version", PROTOCOL_VERSION), field_name="protocol_version"
            ),
            generated_at=_as_str(payload.get("generated_at", ""), field_name="generated_at"),
            notes=tuple(_as_str(n, field_name="notes[]") for n in notes),
        )
