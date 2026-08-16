"""The sink's progress table must not outgrow the queue it describes.

``QueueCommandSink`` remembers when each shipped command was last heard from, so
the reflex guard can tell a command that is working from one that has stopped.
Entries were added on every send and every ack, and removed on exactly one
event: a terminal ack. A command that never gets one therefore stayed for the
life of the session — and the queue guarantees there are such commands.
``CommandQueue._track`` sheds the oldest evictable entry once ``pending_limit``
is reached, and a shed command's terminal ack is filed against nothing.

Measured with the real classes rather than argued: at ``pending_limit=8``, two
hundred accepted commands left the queue tracking eight and the sink holding two
hundred — one hundred and ninety-two of them for commands the queue had already
forgotten, unreachable by construction and never removable. AGENTS.md says
bounded memory, and calls anything unbounded a bug.

The pruning is to the queue's own pending set rather than to a second limit of
the sink's own, because a duplicate bound is a bound that drifts.
:meth:`AgentRuntime._in_flight` is the only caller of ``last_progress_ms`` and
iterates exactly that set; the in-flight command is the one entry
``_evictable_command_id`` refuses to shed. Both facts are pinned below, because
the fix is only safe while they hold.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Final

from pz_agent_cli.runtime import QueueCommandSink
from pz_agent_core.ipc.queue import CommandQueue
from pz_agent_core.protocol import ActionName
from tests.fixtures.ipc_builders import FakeClock, make_layout, make_started, publish_acks

#: Small enough that the queue's own bound bites within a short test, and the
#: shedding this file is about is the bound working as designed, not a fault.
PENDING_LIMIT: Final = 8
BASE_MS: Final = 1_700_000_000_000


def _queue_and_sink(tmp_path: Path) -> tuple[CommandQueue, QueueCommandSink, FakeClock]:
    clock = FakeClock()
    session = str(uuid.uuid4())
    queue = CommandQueue(
        make_layout(tmp_path),
        session_id=session,
        clock=clock,
        pending_limit=PENDING_LIMIT,
    )
    return queue, QueueCommandSink(queue, clock=clock), clock


def _ship(queue: CommandQueue, sink: QueueCommandSink, clock: FakeClock, count: int) -> None:
    """*count* read-only commands, none of them ever acked.

    Read-only on purpose: a queue-occupying command is refused while another is
    in flight, so shipping many of them would measure the backpressure rule
    instead of the growth. It is also the honest case — ``world.inspect`` is
    what the loop issues most often, and the pending bound sheds read-only
    commands first by design.
    """
    for index in range(count):
        clock.advance(10)
        sink.send(
            queue.build(
                ActionName.WORLD_INSPECT,
                idempotency_key=f"look-{index}",
                lease_ms=300_000,
            )
        )


def test_progress_never_outgrows_the_queues_own_bound(tmp_path: Path) -> None:
    queue, sink, clock = _queue_and_sink(tmp_path)

    _ship(queue, sink, clock, count=200)

    tracked = {command.command_id for command in queue.pending}
    assert len(tracked) == PENDING_LIMIT
    remembered = set(sink.progress_command_ids)
    assert remembered == tracked, (
        f"the sink remembers {len(remembered)} command(s) while the queue tracks "
        f"{len(tracked)}; the difference is progress for commands no ack can ever "
        "arrive for and nothing will ever read"
    )
    queue.close()


def test_a_command_still_in_flight_keeps_its_progress(tmp_path: Path) -> None:
    """The control: pruning must not take the entry the guard actually reads.

    Without this the first test passes just as well with the table emptied on
    every tick, and the reflex guard would then see every command as never
    having made progress since it was issued.
    """
    queue, sink, clock = _queue_and_sink(tmp_path)
    clock.advance(10)
    command = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=300_000)
    sink.send(command)
    issued_at = sink.last_progress_ms(command)

    clock.advance(5_000)
    publish_acks(queue.layout, [make_started(command, seq=0, timestamp_ms=clock())])
    sink.poll_acks()

    heard_at = sink.last_progress_ms(command)
    assert heard_at > issued_at, "a started ack is the command being heard from"
    assert command.command_id in set(sink.progress_command_ids)
    queue.close()


def test_the_occupying_command_is_never_the_one_shed(tmp_path: Path) -> None:
    """The queue-side fact the pruning leans on, asserted rather than assumed.

    ``_forget_what_the_queue_forgot`` is only safe because the command the
    engine is driving cannot leave ``queue.pending``. If shedding ever started
    evicting the in-flight command, the sink would drop its progress and the
    guard would misread a working command as stalled — so the property is
    checked here, next to the code that depends on it.
    """
    queue, sink, clock = _queue_and_sink(tmp_path)
    clock.advance(10)
    occupying = queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=300_000)
    sink.send(occupying)

    _ship(queue, sink, clock, count=200)

    assert occupying.command_id in {c.command_id for c in queue.pending}
    assert sink.last_progress_ms(occupying) == BASE_MS + 10
    queue.close()
