"""Builders for the IPC and session tests (T007/T008).

The subsystem under test is defined by *time* and *file state*, so the two
things every test here needs are a clock it controls and an exchange directory
it owns. Both are built by hand rather than mocked: a fake clock that is just a
counter keeps the assertions about lease expiry and staleness readable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pz_agent_core.ipc import IpcLayout, JournalWriter
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    Command,
    ReasonCode,
)

#: A fixed, valid session UUID so refs and payloads line up across a test.
IPC_SESSION_ID = str(uuid.UUID(int=0x9CE55))

BASE_TIME_MS = 1_700_000_000_000


@dataclass
class FakeClock:
    """A wall clock the test moves by hand."""

    now: int = BASE_TIME_MS

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> int:
        self.now += ms
        return self.now


def make_layout(root: Path) -> IpcLayout:
    """An exchange directory ready to be written to."""
    layout = IpcLayout(root)
    layout.ensure()
    return layout


def make_command(
    action: ActionName = ActionName.CONSUME_EAT,
    *,
    session_id: str = IPC_SESSION_ID,
    seq: int = 0,
    idempotency_key: str = "goal-1-step-1-attempt-1",
    issued_at_ms: int = BASE_TIME_MS,
    lease_ms: int = 10_000,
    command_id: str | None = None,
    args: dict[str, Any] | None = None,
) -> Command:
    return Command(
        session_id=session_id,
        seq=seq,
        command_id=command_id or str(uuid.uuid4()),
        idempotency_key=idempotency_key,
        issued_at_ms=issued_at_ms,
        lease_ms=lease_ms,
        action=action,
        args=args or {},
    )


def make_success(
    command: Command, *, seq: int = 0, timestamp_ms: int = BASE_TIME_MS
) -> ActionResult:
    """A terminal success carrying the evidence the constructor demands."""
    return ActionResult.succeeded(
        session_id=command.session_id,
        seq=seq,
        command_id=command.command_id,
        action=command.action.value,
        timestamp_ms=timestamp_ms,
        evidence={"hunger_before": 0.6, "hunger_after": 0.2},
    )


def make_failure(
    command: Command,
    *,
    seq: int = 0,
    timestamp_ms: int = BASE_TIME_MS,
    reason_code: ReasonCode = ReasonCode.POSTCONDITION_FAILED,
    status: ActionStatus = ActionStatus.FAILED,
) -> ActionResult:
    return ActionResult.failure(
        session_id=command.session_id,
        seq=seq,
        command_id=command.command_id,
        action=command.action.value,
        timestamp_ms=timestamp_ms,
        reason_code=reason_code,
        status=status,
    )


def make_started(
    command: Command, *, seq: int = 0, timestamp_ms: int = BASE_TIME_MS
) -> ActionResult:
    """A non-terminal ack — the kind that must never be cached.

    The protocol constrains reason codes in one direction only (a *succeeded*
    result must carry ``POSTCONDITION_MET``), so a non-terminal ack may carry
    it without claiming anything: the status is what says the work is running.
    """
    return ActionResult(
        session_id=command.session_id,
        seq=seq,
        command_id=command.command_id,
        action=command.action.value,
        status=ActionStatus.STARTED,
        reason_code=ReasonCode.POSTCONDITION_MET,
        timestamp_ms=timestamp_ms,
    )


def publish_acks(layout: IpcLayout, results: list[ActionResult]) -> None:
    """Append *results* to the ack journal the way the mod would."""
    writer = JournalWriter(layout, layout.command_ack)
    try:
        for result in results:
            writer.append(result.to_dict())
    finally:
        writer.close()
