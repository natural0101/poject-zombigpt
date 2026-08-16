"""A mod that claims success without evidence must be quoted, not silenced.

``ActionResult.succeeded()`` refuses to build a success ack without observed
evidence, and AGENTS.md states that rule as the protocol's central invariant.
``ActionResult.from_dict`` does not enforce it — measured, not assumed — and
reading the invariant as "the type refuses it everywhere" makes moving the check
into ``__post_init__`` look like a straightforward hardening.

It is not. The decoder does not *make* a claim, it transcribes the peer's, and
the engine is built to name a dishonest one: a terminal ``succeeded`` ack whose
postcondition never showed up ends as ``POSTCONDITION_FAILED`` saying *the mod
reported success*. Refusing the claim at the decoder loses that answer, measured
by planting the check and reading what came back instead — ``poll_acks`` catches
the ``ProtocolError``, files the ack as an unusable one and drops it, so the
engine waits out the budget and reports ``ACTION_TIMEOUT``; a sink that decodes
inline raises through and the engine reports ``INTERNAL_ERROR``. Neither says
what happened. The receiver would stop being able to tell "the mod lied" from
"the mod went quiet", and the first is the diagnosis a live session most needs.

Nothing pinned that. The same plant leaves ``test_actions_engine.py`` and
``test_ipc_queue.py`` entirely green: both build their success acks through
``ActionResult.succeeded()``, so the acks carry evidence and never take the
route at issue. These three tests cover the path an ack actually travels —
journal bytes, decoder, engine — so the change described above fails here
instead of shipping.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pz_agent_core.actions.engine import Dispatch
from pz_agent_core.ipc.journal import JournalWriter
from pz_agent_core.ipc.queue import CommandQueue
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    Command,
    ReasonCode,
)
from pz_agent_core.version import SCHEMA_VERSION
from tests.fixtures import make_observation
from tests.fixtures.action_doubles import FakeClock as EngineClock
from tests.fixtures.action_doubles import FakeCommandSink, StubAdapter
from tests.fixtures.ipc_builders import IPC_SESSION_ID, FakeClock, make_command, make_layout

from .test_actions_engine import a_request, make_harness


def a_success_claim(command: Command, *, seq: int = 0) -> dict[str, Any]:
    """The ack bytes a mod writes when it says it worked and proves nothing.

    Built as a wire payload rather than through :class:`ActionResult`, because
    the constructor this test exists to talk about is the one that refuses it.
    ``evidence`` is absent entirely, which is what the mod's ``Handle:ack``
    emits: it appends the key only when the bag has entries.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": command.session_id,
        "seq": seq,
        "command_id": command.command_id,
        "action": command.action.value,
        "status": ActionStatus.SUCCEEDED.value,
        "reason_code": ReasonCode.POSTCONDITION_MET.value,
        "timestamp_ms": 1_700_000_000_000,
        "attempt": 1,
        "finished_at_ms": 1_700_000_000_000,
        "progress": 1.0,
        "message": "phase=succeeded",
    }


def test_the_decoder_transcribes_the_claim_it_cannot_verify() -> None:
    command = make_command(ActionName.CONSUME_EAT, idempotency_key="eat-1")

    decoded = ActionResult.from_dict(a_success_claim(command))

    assert decoded.status is ActionStatus.SUCCEEDED
    assert decoded.evidence == {}
    assert decoded.is_terminal
    # The producer side is the half that refuses; the two are not the same rule.
    with pytest.raises(Exception, match="requires postcondition evidence"):
        ActionResult.succeeded(
            session_id=command.session_id,
            seq=1,
            command_id=command.command_id,
            action=command.action.value,
            timestamp_ms=1_700_000_000_000,
            evidence={},
        )


def test_the_ack_stream_hands_the_claim_over_rather_than_dropping_it(tmp_path: Path) -> None:
    clock = FakeClock()
    layout = make_layout(tmp_path)
    queue = CommandQueue(layout, session_id=IPC_SESSION_ID, clock=clock)
    command = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=10_000)
    queue.submit(command)

    writer = JournalWriter(layout, layout.command_ack)
    try:
        writer.append(a_success_claim(command))
    finally:
        writer.close()
    poll = queue.poll_acks()

    assert [r.status for r in poll.results] == [ActionStatus.SUCCEEDED]
    assert poll.results[0].evidence == {}
    # Refusing it in the decoder would land here as an unusable-ack diagnostic,
    # and the engine below would never learn the mod had claimed anything.
    assert [d.detail for d in poll.diagnostics if "unusable ack" in d.detail] == []
    queue.close()


class ClaimingSink(FakeCommandSink):
    """A sink whose ack arrives the way the journal delivers one: decoded."""

    def __init__(self, clock: EngineClock) -> None:
        super().__init__(clock)
        self._claimed = False

    def send(self, command: Command) -> Dispatch:
        self._claimed = False
        return super().send(command)

    def poll_acks(self) -> tuple[ActionResult, ...]:
        if not self.sent or self._claimed:
            return ()
        self._claimed = True
        return (ActionResult.from_dict(a_success_claim(self.sent[-1], seq=self._next_seq())),)


def test_the_engine_names_the_claim_instead_of_reporting_a_timeout() -> None:
    harness = make_harness(
        StubAdapter(verify_after=None, timeout_ms=5_000),
        sink_factory=ClaimingSink,
    )
    harness.source.repeat(make_observation(seq=1))

    result = harness.engine.execute(a_request())

    assert result.status is ActionStatus.FAILED
    assert result.reason_code is ReasonCode.POSTCONDITION_FAILED
    assert "the mod reported success" in result.message
    # ``budget`` belongs to the other message — the one for a command nothing
    # ever answered. Reading it here would mean the claim had been lost on the
    # way in rather than judged.
    assert "budget" not in result.message
