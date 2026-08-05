"""The action lifecycle: validate, prepare, enqueue, observe, verify, finalise.

Every command is a transaction with exactly one terminal :class:`ActionResult`.
:meth:`ActionEngine.execute` never returns None and never lets an exception
escape, because the layers above it report what it returns to the user.

Three rules shape the whole file:

* **Preconditions are checked against a fresh observation.** The engine waits
  for an observation newer than anything it has already seen before it
  validates. A precondition checked against a cached snapshot is a statement
  about the past.
* **The mod's opinion never overrides observation.** An ack that says
  ``succeeded`` is a claim about the game's action queue, not about the world.
  If :meth:`ActionAdapter.verify` never produces evidence, the result is
  ``POSTCONDITION_FAILED`` — and, symmetrically, an observed postcondition wins
  over a failure ack, because both directions follow from the same rule.
* **Everything is bounded.** The poll loop has a computed iteration ceiling as
  well as a deadline, so a clock that stops moving cannot make it spin; retries
  are bounded by the command policy, by the protocol's own retry ceiling and by
  the retryable-code set; the diagnostics attached to a failure are truncated.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Final, Protocol, TypeAlias

from ..ipc.clocks import Clock, system_clock_ms
from ..protocol import (
    ALWAYS_ALLOWED_ACTIONS,
    READ_ONLY_ACTIONS,
    RETRYABLE_CODES,
    ActionName,
    ActionResult,
    ActionStatus,
    Command,
    CommandPolicy,
    DangerLevel,
    JsonDict,
    Observation,
    ReasonCode,
)
from ..protocol.messages import MAX_LEASE_MS, MAX_RETRIES, MIN_LEASE_MS
from .adapter import (
    ActionAdapter,
    AdapterRegistry,
    Evidence,
    PreconditionFailed,
    ProgressReporter,
    UnknownActionError,
)

__all__ = [
    "ActionEngine",
    "ActionRequest",
    "CommandSink",
    "Dispatch",
    "ObservationSource",
    "deny_capability",
    "no_panic",
]

#: Passed to :meth:`ObservationSource.wait_for_next` to mean "any observation".
ANY_SEQ: Final = -1

DEFAULT_LEASE_MS: Final = 15_000

#: A base idempotency key must leave room for the per-attempt suffix; the wire
#: limit is 128 characters and the suffix is at most six of them.
MAX_BASE_IDEMPOTENCY_KEY_LEN: Final = 120

#: How long the engine waits for the fresh observation it validates against.
DEFAULT_OBSERVATION_TIMEOUT_MS: Final = 2_000

#: Window in which *something* about the action must visibly change, or the
#: attempt is declared stuck instead of burning the whole timeout.
DEFAULT_NO_PROGRESS_MS: Final = 5_000

#: Observations lag acks by up to a tick, so a mod that reports a terminal
#: status gets this long for the world to catch up before the engine rules on
#: the postcondition.
DEFAULT_POST_ACK_GRACE_MS: Final = 500

#: Slack added to the computed poll ceiling, covering pause extensions and the
#: post-ack grace without letting the loop become unbounded.
POLL_SLACK: Final = 16

#: Hard ceiling on poll iterations regardless of the configured timeout.
MAX_POLLS: Final = 10_000

#: Failure diagnostics are for a human reading a log line, not an archive.
MAX_DIAGNOSTICS: Final = 10

#: Aborts that mean "someone else is driving now" rather than "this failed".
_CANCELLING_REASONS: Final[frozenset[ReasonCode]] = frozenset(
    {
        ReasonCode.PANIC_STOP,
        ReasonCode.USER_TAKEOVER,
        ReasonCode.THREAT_INTERRUPTED,
        ReasonCode.CANCELLED_BY_REQUEST,
    }
)

#: True when the stop lever has been pulled since the engine last looked.
PanicSwitch: TypeAlias = Callable[[], bool]

#: True when a probed game capability is usable right now.
CapabilityCheck: TypeAlias = Callable[[str], bool]


def no_panic() -> bool:
    """Default :data:`PanicSwitch`: nobody has pulled the stop lever."""
    return False


def deny_capability(name: str) -> bool:
    """Default :data:`CapabilityCheck`, which fails closed.

    An adapter that names a required capability cannot run until a real probe
    is wired in. Defaulting to "allowed" would mean the engine assumes a game
    API exists because nobody said otherwise, which is exactly the assumption
    the capability system exists to prevent.
    """
    return False


@dataclass(frozen=True, slots=True)
class Dispatch:
    """What the sink did with one :meth:`CommandSink.send`.

    ``command`` is the command as *shipped* — the sink owns stream sequencing,
    so its ``seq`` and ``issued_at_ms`` may differ from the draft the engine
    handed over. Acks are matched against this one.
    """

    command: Command
    rejection: ActionResult | None = None

    @property
    def accepted(self) -> bool:
        return self.rejection is None


class CommandSink(Protocol):
    """Ships commands to the mod and exposes the ack stream.

    Deliberately narrow: the engine does no IO and knows nothing about
    journals, files or sockets, which is what lets the whole lifecycle be
    tested with a list and a counter.
    """

    def send(self, command: Command) -> Dispatch:
        """Ship *command*, stamping stream sequencing onto it.

        The engine mints ``command_id`` and ``idempotency_key`` because it owns
        retry identity; the sink owns ``seq``. A refusal is reported as a
        terminal :class:`ActionResult` in the dispatch rather than an
        exception, since the queue rejecting a command is ordinary traffic.
        """
        ...

    def poll_acks(self) -> Sequence[ActionResult]:
        """Acks that arrived since the previous call, oldest first. Never blocks."""
        ...

    def cancel(self, command: Command, reason: ReasonCode) -> None:
        """Ask the mod to abandon *command*.

        Called on every abort where the mod may still be working, so the engine
        never walks away from an action it started.
        """
        ...


class ObservationSource(Protocol):
    """The engine's only window onto the world."""

    def latest(self) -> Observation | None:
        """The newest observation received, or None if none has arrived yet."""
        ...

    def wait_for_next(self, after_seq: int, timeout_ms: int) -> Observation | None:
        """Block for the first observation with ``seq > after_seq``.

        Returns None when *timeout_ms* elapses first. Pass :data:`ANY_SEQ` to
        accept whatever arrives. This is the engine's only wait primitive: it
        never sleeps on a wall clock, so a test that supplies observations runs
        the whole lifecycle instantly.
        """
        ...


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """One intent for the engine to drive to a terminal result."""

    action: ActionName
    session_id: str
    idempotency_key: str
    args: JsonDict = field(default_factory=dict)
    policy: CommandPolicy = field(default_factory=CommandPolicy)
    lease_ms: int = DEFAULT_LEASE_MS

    def __post_init__(self) -> None:
        if not 1 <= len(self.idempotency_key) <= MAX_BASE_IDEMPOTENCY_KEY_LEN:
            raise ValueError(
                f"idempotency_key must be 1..{MAX_BASE_IDEMPOTENCY_KEY_LEN} characters "
                "so the per-attempt suffix fits within the protocol limit"
            )
        # Checked here rather than at send time so an out-of-range lease is a
        # programming error at the call site, not an INTERNAL_ERROR mid-flight.
        if not MIN_LEASE_MS <= self.lease_ms <= MAX_LEASE_MS:
            raise ValueError(
                f"lease_ms must be within {MIN_LEASE_MS}..{MAX_LEASE_MS}, got {self.lease_ms}"
            )

    def attempt_key(self, attempt: int) -> str:
        """The idempotency key for *attempt*.

        A retry is a new unit of work, so it gets a new key; a *redelivery* of
        the same attempt keeps that attempt's key and is deduplicated by the
        mod. Reusing the base key across retries would be actively wrong: the
        idempotency cache replays the first terminal result for a key, so the
        second attempt would be answered with the first attempt's failure and
        the retry budget would burn without anything being tried again. The
        shared prefix keeps every attempt of one intent traceable together.
        """
        return self.idempotency_key if attempt <= 1 else f"{self.idempotency_key}#a{attempt}"


@dataclass
class ActionEngine:
    """Drives commands through the lifecycle against two injected ports."""

    registry: AdapterRegistry
    sink: CommandSink
    observations: ObservationSource
    clock: Clock = system_clock_ms
    panic_stop: PanicSwitch = no_panic
    capability_check: CapabilityCheck = deny_capability
    threat_threshold: DangerLevel = DangerLevel.HIGH
    observation_timeout_ms: int = DEFAULT_OBSERVATION_TIMEOUT_MS
    no_progress_ms: int = DEFAULT_NO_PROGRESS_MS
    post_ack_grace_ms: int = DEFAULT_POST_ACK_GRACE_MS
    #: Latched from the first observation seen when not supplied; any later
    #: change means the player loaded a different world and every reference the
    #: engine holds is meaningless.
    expected_save_id: str | None = None
    _local_seq: int = field(default=0, init=False)

    # -- public API --------------------------------------------------------

    def execute(self, request: ActionRequest) -> ActionResult:
        """Run *request* to a terminal result.

        Always returns; never raises. Retries are bounded by
        ``request.policy.max_retries`` *and* by the protocol's
        :data:`~pz_agent_core.protocol.messages.MAX_RETRIES`, and are only
        attempted for codes in :data:`RETRYABLE_CODES` — retrying an
        ``INVALID_REF`` or a ``POLICY_DENIED`` just spends the budget on the
        same refusal.
        """
        try:
            adapter = self.registry.get(request.action)
        except UnknownActionError as exc:
            return self._failure(
                request,
                str(uuid.uuid4()),
                exc.reason_code,
                str(exc),
                attempt=1,
                status=ActionStatus.REJECTED,
            )

        # ``CommandPolicy`` only enforces the ceiling when it is parsed off the
        # wire, so an in-process caller can hand over any number. The engine
        # clamps rather than trusts it: an unbounded retry budget is an
        # unbounded loop against the game.
        max_attempts = 1 + min(MAX_RETRIES, max(0, request.policy.max_retries))
        attempt = 1
        result = self._run_attempt(adapter, request, attempt)
        while (
            attempt < max_attempts
            and result.status is not ActionStatus.SUCCEEDED
            and result.reason_code in RETRYABLE_CODES
        ):
            attempt += 1
            result = self._run_attempt(adapter, request, attempt)
        return result

    # -- one attempt -------------------------------------------------------

    def _run_attempt(
        self, adapter: ActionAdapter, request: ActionRequest, attempt: int
    ) -> ActionResult:
        command_id = str(uuid.uuid4())
        try:
            return self._drive(adapter, request, attempt, command_id)
        except PreconditionFailed as exc:
            return self._failure(
                request,
                command_id,
                exc.reason_code,
                str(exc),
                attempt=attempt,
                status=ActionStatus.REJECTED,
                evidence=exc.evidence,
            )
        except Exception as exc:  # adapters are foreign code; a crash is a result, not a raise
            return self._failure(
                request,
                command_id,
                ReasonCode.INTERNAL_ERROR,
                f"{type(exc).__name__}: {exc}",
                attempt=attempt,
            )

    def _drive(
        self,
        adapter: ActionAdapter,
        request: ActionRequest,
        attempt: int,
        command_id: str,
    ) -> ActionResult:
        before = self._fresh_observation()
        if before is None:
            return self._failure(
                request,
                command_id,
                ReasonCode.GAME_DISCONNECTED,
                f"no observation within {self.observation_timeout_ms} ms",
                attempt=attempt,
                status=ActionStatus.REJECTED,
            )

        refusal = self._pre_flight(request, before)
        if refusal is not None:
            reason, message = refusal
            return self._failure(
                request,
                command_id,
                reason,
                message,
                attempt=attempt,
                status=ActionStatus.REJECTED,
                evidence=self._world_evidence(before),
            )

        capability = adapter.required_capability
        if capability is not None and not self.capability_check(capability):
            return self._failure(
                request,
                command_id,
                ReasonCode.CAPABILITY_UNAVAILABLE,
                f"capability {capability!r} is not usable in this session",
                attempt=attempt,
                status=ActionStatus.REJECTED,
            )

        draft = Command(
            session_id=request.session_id,
            seq=0,
            command_id=command_id,
            idempotency_key=request.attempt_key(attempt),
            issued_at_ms=self.clock(),
            lease_ms=request.lease_ms,
            action=request.action,
            args=dict(request.args),
            expected_observation_seq=before.seq,
            policy=request.policy,
        )
        adapter.validate(draft, before)
        prepared = replace(draft, args=adapter.build_args(draft, before))

        dispatch = self.sink.send(prepared)
        if dispatch.rejection is not None:
            return self._sink_refusal(request, dispatch.rejection, command_id, attempt)

        shipped = dispatch.command
        try:
            return self._observe(adapter, request, shipped, before, attempt)
        except Exception as exc:
            # The command is already with the mod. Whatever went wrong while
            # watching it — a crashing adapter, a port that raised — the action
            # must not be left running after the caller has been told the
            # attempt is over.
            self.sink.cancel(shipped, ReasonCode.INTERNAL_ERROR)
            return self._failure(
                request,
                shipped.command_id,
                ReasonCode.INTERNAL_ERROR,
                f"{type(exc).__name__}: {exc}",
                attempt=attempt,
                evidence=self._world_evidence(before),
            )

    def _sink_refusal(
        self,
        request: ActionRequest,
        rejection: ActionResult,
        command_id: str,
        attempt: int,
    ) -> ActionResult:
        """Adopt the sink's refusal — once it has been checked to be one.

        The sink is another subsystem's code, and "the only route to success is
        :meth:`_success`" is worth nothing if a port can hand back a ready-made
        ``succeeded`` result instead of a rejection. A non-terminal one is
        refused for the same reason: ``execute`` promises exactly one terminal
        result, so an ``accepted`` reported as a refusal is a defect, not an
        answer to relay.
        """
        if rejection.status is ActionStatus.SUCCEEDED or not rejection.is_terminal:
            return self._failure(
                request,
                command_id,
                ReasonCode.INTERNAL_ERROR,
                f"the sink refused the command with a non-terminal or successful "
                f"result ({rejection.status.value})",
                attempt=attempt,
            )
        return replace(rejection, attempt=attempt)

    # -- the observe / verify loop ----------------------------------------

    def _observe(
        self,
        adapter: ActionAdapter,
        request: ActionRequest,
        command: Command,
        before: Observation,
        attempt: int,
    ) -> ActionResult:
        started_at = self.clock()
        deadline = started_at + adapter.timeout_ms
        stuck_deadline = started_at + self.no_progress_ms
        reporter = adapter if isinstance(adapter, ProgressReporter) else None

        latest = before
        last_ack: ActionResult | None = None
        mod_claimed_success = False
        fingerprint = self._fingerprint(reporter, command, before, latest, last_ack)
        tick = started_at

        for _ in range(self._poll_ceiling(adapter)):
            now = self.clock()
            if now >= deadline:
                break

            observation = self.observations.wait_for_next(
                latest.seq, min(adapter.poll_interval_ms, deadline - now)
            )
            if observation is not None:
                latest = observation
            for ack in self.sink.poll_acks():
                if ack.command_id != command.command_id:
                    continue
                if last_ack is not None and last_ack.is_terminal:
                    # Nothing follows a terminal ack. Letting a late progress
                    # frame overwrite one would turn a reported failure into a
                    # timeout — the engine would stop knowing why it failed.
                    continue
                last_ack = ack

            now = self.clock()
            if latest.game.paused:
                # § 4.14: a paused game suspends the action timeout. Time the
                # player spent in a menu is not time the character failed in.
                elapsed = now - tick
                deadline += elapsed
                stuck_deadline += elapsed
            tick = now

            abort = self._in_flight_abort(request, latest)
            if abort is not None:
                reason, message = abort
                self.sink.cancel(command, reason)
                return self._failure(
                    request,
                    command.command_id,
                    reason,
                    message,
                    attempt=attempt,
                    status=ActionStatus.CANCELLED,
                    evidence=self._world_evidence(latest, before=before),
                    started_at_ms=started_at,
                    last_ack=last_ack,
                )

            if observation is not None:
                evidence = adapter.verify(command, before, observation)
                if evidence is not None:
                    return self._success(request, command, evidence, attempt, started_at)

            if last_ack is not None and last_ack.is_terminal:
                if last_ack.status is not ActionStatus.SUCCEEDED:
                    # The mod says it failed. Observation still gets the last
                    # word: if the postcondition is visible anyway the honest
                    # answer is success, otherwise the ack's code stands.
                    proof = adapter.verify(command, before, latest)
                    if proof is not None:
                        return self._success(request, command, proof, attempt, started_at)
                    return self._failure(
                        request,
                        command.command_id,
                        self._failure_code(last_ack),
                        last_ack.message or f"mod reported {last_ack.status.value}",
                        attempt=attempt,
                        status=ActionStatus.FAILED,
                        evidence=self._world_evidence(latest, before=before),
                        started_at_ms=started_at,
                        last_ack=last_ack,
                    )
                # A "succeeded" ack only buys the world a little longer to show
                # the postcondition; it is never itself the answer.
                mod_claimed_success = True
                deadline = min(deadline, now + self.post_ack_grace_ms)
                continue

            # The window is refreshed *before* it is judged: an iteration that
            # both saw the action advance and reached the old deadline is
            # progress, not a stall, and reporting NO_PROGRESS for it would be
            # a claim contradicted by the observation just processed.
            current = self._fingerprint(reporter, command, before, latest, last_ack)
            if current != fingerprint:
                fingerprint = current
                stuck_deadline = now + self.no_progress_ms
            if now >= stuck_deadline:
                self.sink.cancel(command, ReasonCode.NO_PROGRESS)
                return self._failure(
                    request,
                    command.command_id,
                    ReasonCode.NO_PROGRESS,
                    f"nothing observable changed for {self.no_progress_ms} ms",
                    attempt=attempt,
                    status=ActionStatus.FAILED,
                    evidence=self._world_evidence(latest, before=before),
                    started_at_ms=started_at,
                    last_ack=last_ack,
                )

        return self._budget_spent(
            adapter,
            request,
            command,
            before,
            latest,
            attempt=attempt,
            started_at=started_at,
            last_ack=last_ack,
            mod_claimed_success=mod_claimed_success,
        )

    def _budget_spent(
        self,
        adapter: ActionAdapter,
        request: ActionRequest,
        command: Command,
        before: Observation,
        latest: Observation,
        *,
        attempt: int,
        started_at: int,
        last_ack: ActionResult | None,
        mod_claimed_success: bool,
    ) -> ActionResult:
        """The poll loop ended without evidence — either exit says so plainly.

        Whether the mod claimed success decides which lie is refused: "it
        worked" or "it is still going". The elapsed time is measured rather than
        assumed to be the configured timeout, because a paused game extends the
        deadline (§ 4.14) and the loop then ends on its iteration ceiling
        instead, having run far longer than the budget.
        """
        elapsed = self.clock() - started_at
        reason = (
            ReasonCode.POSTCONDITION_FAILED if mod_claimed_success else ReasonCode.ACTION_TIMEOUT
        )
        message = (
            f"the mod reported success but the postcondition was never observed in {elapsed} ms"
            if mod_claimed_success
            else f"postcondition not observed after {elapsed} ms (budget {adapter.timeout_ms} ms)"
        )
        if last_ack is None or not last_ack.is_terminal:
            self.sink.cancel(command, reason)
        return self._failure(
            request,
            command.command_id,
            reason,
            message,
            attempt=attempt,
            status=ActionStatus.FAILED,
            evidence=self._world_evidence(latest, before=before),
            started_at_ms=started_at,
            last_ack=last_ack,
        )

    def _poll_ceiling(self, adapter: ActionAdapter) -> int:
        """Iteration bound for the poll loop.

        The deadline alone is not enough: the clock is injected, and a source
        that returns immediately without the clock moving would spin forever.
        """
        interval = max(1, adapter.poll_interval_ms)
        return min(MAX_POLLS, adapter.timeout_ms // interval + POLL_SLACK)

    def _fingerprint(
        self,
        reporter: ProgressReporter | None,
        command: Command,
        before: Observation,
        latest: Observation,
        last_ack: ActionResult | None,
    ) -> tuple[object, ...]:
        """Everything the engine can see about the action advancing.

        When it stops changing, nothing *observable* is happening — which is
        precisely what ``NO_PROGRESS`` claims, and no more than that.
        """
        return (
            None if reporter is None else reporter.progress(command, before, latest),
            latest.action.busy,
            latest.action.ownership,
            latest.action.action_id,
            latest.action.type,
            latest.action.progress,
            None if last_ack is None else (last_ack.status, last_ack.progress),
        )

    # -- guards ------------------------------------------------------------

    def _fresh_observation(self) -> Observation | None:
        """Wait for an observation strictly newer than anything already seen.

        Validating against ``latest()`` would check the preconditions of a
        world that may have moved on between deciding to act and acting.
        """
        latest = self.observations.latest()
        after = ANY_SEQ if latest is None else latest.seq
        return self.observations.wait_for_next(after, self.observation_timeout_ms)

    def _pre_flight(
        self, request: ActionRequest, observation: Observation
    ) -> tuple[ReasonCode, str] | None:
        """Refusals that apply before anything is sent."""
        session = self._session_abort(request, observation)
        if session is not None:
            return session
        return self._interrupt_abort(request, observation)

    def _in_flight_abort(
        self, request: ActionRequest, observation: Observation
    ) -> tuple[ReasonCode, str] | None:
        """Aborts that apply to a command already with the mod.

        A busy queue is excluded here: the mod-owned action *is* the command
        being driven, so treating "busy" as a takeover would abort every
        successful action the instant it started.
        """
        session = self._session_abort(request, observation)
        if session is not None:
            return session
        if self._exempt(request):
            return None
        if observation.safety.manual_takeover:
            return ReasonCode.USER_TAKEOVER, "the player took manual control"
        if observation.action.blocks_automation:
            return (
                ReasonCode.PLAYER_BUSY_MANUAL_ACTION,
                f"the action queue is running a {observation.action.ownership.value} action",
            )
        return self._threat_abort(request, observation)

    def _session_abort(
        self, request: ActionRequest, observation: Observation
    ) -> tuple[ReasonCode, str] | None:
        """Conditions no command survives, not even ``safety.stop``.

        Ordered by the interrupt hierarchy: the panic stop outranks everything
        except the checks that say the world itself is gone, where there is
        nothing left to verify against anyway.
        """
        if self.panic_stop() and not self._exempt(request):
            return ReasonCode.PANIC_STOP, "panic stop engaged"
        if observation.session_id != request.session_id:
            return (
                ReasonCode.STALE_SESSION,
                f"observation belongs to session {observation.session_id}",
            )
        if self.expected_save_id is None:
            self.expected_save_id = observation.game.save_id
        elif observation.game.save_id != self.expected_save_id:
            return (
                ReasonCode.SAVE_CHANGED,
                f"save changed to {observation.game.save_id!r}; every reference is void",
            )
        if observation.safety.sidecar_stale:
            return ReasonCode.STALE_SESSION, "the mod considers this sidecar stale"
        if not observation.player.present:
            return ReasonCode.PRECONDITION_FAILED, "no active character"
        if not observation.player.alive:
            # The catalogue (§ 18, PLAYER_DEAD) closes the session on death:
            # there is no character left for a retry to act on.
            return ReasonCode.SESSION_TERMINATED, "the character is dead"
        return None

    def _interrupt_abort(
        self, request: ActionRequest, observation: Observation
    ) -> tuple[ReasonCode, str] | None:
        if self._exempt(request):
            return None
        if observation.safety.manual_takeover:
            return ReasonCode.USER_TAKEOVER, "the player took manual control"
        if not observation.safety.armed and request.action not in READ_ONLY_ACTIONS:
            return ReasonCode.NOT_ARMED, "automation is disarmed"
        if observation.action.blocks_automation:
            return (
                ReasonCode.PLAYER_BUSY_MANUAL_ACTION,
                f"the action queue is running a {observation.action.ownership.value} action",
            )
        return self._threat_abort(request, observation)

    def _threat_abort(
        self, request: ActionRequest, observation: Observation
    ) -> tuple[ReasonCode, str] | None:
        """A threat only interrupts a command the caller allowed to be interrupted."""
        if not request.policy.allow_interrupt:
            return None
        if observation.safety.danger_level >= self.threat_threshold:
            return (
                ReasonCode.THREAT_INTERRUPTED,
                f"danger level {observation.safety.danger_level.value}",
            )
        return None

    def _exempt(self, request: ActionRequest) -> bool:
        """True for the commands that must work while everything else is refused.

        Stopping, disarming and cancelling are how a takeover or a panic stop is
        carried out; gating them on "no takeover in progress" would make the
        agent unstoppable by exactly the mechanism meant to stop it.
        """
        return request.action in ALWAYS_ALLOWED_ACTIONS

    # -- results -----------------------------------------------------------

    def _success(
        self,
        request: ActionRequest,
        command: Command,
        evidence: Evidence,
        attempt: int,
        started_at_ms: int,
    ) -> ActionResult:
        """The engine's only successful exit.

        This is the one call to :meth:`ActionResult.succeeded` in the package,
        and its evidence always comes from an :class:`Evidence` an adapter
        returned after looking at an observation.
        """
        return ActionResult.succeeded(
            session_id=request.session_id,
            seq=self._next_seq(),
            command_id=command.command_id,
            action=request.action.value,
            timestamp_ms=self.clock(),
            evidence=evidence.to_payload(),
            started_at_ms=started_at_ms,
            attempt=attempt,
            message=f"postcondition observed: {evidence.kind}",
        )

    def _failure(
        self,
        request: ActionRequest,
        command_id: str,
        reason: ReasonCode,
        message: str,
        *,
        attempt: int,
        status: ActionStatus = ActionStatus.FAILED,
        evidence: JsonDict | None = None,
        started_at_ms: int | None = None,
        last_ack: ActionResult | None = None,
    ) -> ActionResult:
        if status is ActionStatus.FAILED and reason in _CANCELLING_REASONS:
            status = ActionStatus.CANCELLED
        return ActionResult.failure(
            session_id=request.session_id,
            seq=self._next_seq(),
            command_id=command_id,
            action=request.action.value,
            timestamp_ms=self.clock(),
            reason_code=reason,
            status=status,
            message=message,
            evidence=evidence,
            diagnostics=self._diagnostics(attempt, started_at_ms, last_ack),
            attempt=attempt,
        )

    def _diagnostics(
        self, attempt: int, started_at_ms: int | None, last_ack: ActionResult | None
    ) -> list[str]:
        """The § 4.15 failure context, bounded and free of raw game text."""
        lines = [f"attempt={attempt}"]
        if started_at_ms is not None:
            lines.append(f"elapsed_ms={self.clock() - started_at_ms}")
        if last_ack is not None:
            lines.append(f"last_ack={last_ack.status.value}/{last_ack.reason_code.value}")
            if last_ack.progress is not None:
                lines.append(f"last_ack_progress={last_ack.progress}")
        if self.expected_save_id is not None:
            lines.append(f"save_id={self.expected_save_id}")
        return lines[:MAX_DIAGNOSTICS]

    @staticmethod
    def _failure_code(ack: ActionResult) -> ReasonCode:
        """The reason to report for a non-successful terminal ack.

        A mod that lost track of itself can send ``failed`` while still naming
        ``POSTCONDITION_MET``. Relaying that would put the one code reserved
        for observed success on a result the engine could not verify, so it is
        reported as what it actually is.
        """
        if ack.reason_code is ReasonCode.POSTCONDITION_MET:
            return ReasonCode.POSTCONDITION_FAILED
        return ack.reason_code

    def _world_evidence(
        self, observation: Observation, *, before: Observation | None = None
    ) -> JsonDict:
        """What was observed at the moment of the refusal.

        Attached to failures only. It is deliberately not shaped like postcondition
        evidence: it explains a refusal, it never proves one happened. ``before``
        supplies the other half of § 4.15's "observation seq before/after".
        """
        payload: JsonDict = {
            "observation_seq": observation.seq,
            "danger_level": observation.safety.danger_level.value,
            "manual_takeover": observation.safety.manual_takeover,
            "action_ownership": observation.action.ownership.value,
            "action_busy": observation.action.busy,
            "action_type": observation.action.type,
            "save_id": observation.game.save_id,
        }
        if before is not None:
            payload["observation_seq_before"] = before.seq
        return payload

    def _next_seq(self) -> int:
        """Sequence numbers for results this process synthesised itself.

        Kept apart from the mod's ack stream for the same reason as in
        ``ipc.queue``: a locally produced result never appeared there, so
        borrowing that numbering would fake a gap.
        """
        seq = self._local_seq
        self._local_seq = seq + 1
        return seq
