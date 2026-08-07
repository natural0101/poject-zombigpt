"""The stop lever, tested under the failures it exists for (E12-M03-T007/T008).

A stop that only works while the rest of the system does is not a stop. The two
claims here are the ones a user is entitled to make about the panic key:

* **It is honoured with nothing else working.** :meth:`ReflexGuard.evaluate` is
  a pure function — no core loop, no planner, no queue read, no file — so a
  panic request produces its event with a dead game, a stale link, no previous
  observation and no world data at all. The engine half is the other direction:
  while the latch is down nothing mutating reaches the mod, and the three
  always-allowed commands still do, because an agent that cannot be stopped by
  the mechanism meant to stop it is the failure this task is about.
* **It clears only what the mod queued.** §8.12 and AGENTS.md: under any
  uncertainty the character stays under manual control. ``ambiguous`` counts as
  the player's, and so does ``none`` — the guard may cancel the *running* action
  only on positive proof that the mod owns it, and the merge in
  :func:`~pz_agent_core.safety.reflex._first_per_reason` may not widen that
  even when four rules fire on one tick.
* **It survives an exchange directory in every broken state at once.** The latch
  is a file whose *presence* is the whole signal, so it is still readable when
  the state documents cannot be written, the ack journal ends mid-line and both
  snapshot slots are torn. Those three wrecks are applied for real and each is
  proved to be a real wreck before it is used as a backdrop.
* **It is never queued behind anything.** §3.11:
  :meth:`~pz_agent_core.ipc.queue.CommandQueue.send_stop` bypasses backpressure
  and the idempotency cache, because each of those is a way for a stop not to
  happen.

What is *not* asserted here is the game-side half: cancelling a queue entry
happens in ``PZAgent.Safety.applyStop``, is covered by ``tests/lua/test_safety
.lua``, and refuses to touch a queue holding anything foreign. This file is
about the decisions this process makes.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path, PureWindowsPath
from typing import Any, Final

import pytest

from pz_agent_core.actions import ActionEngine, ActionRequest, AdapterRegistry
from pz_agent_core.actions.engine import PanicSwitch
from pz_agent_core.ipc.atomic import write_json_atomic
from pz_agent_core.ipc.journal import JournalReader, JournalWriter
from pz_agent_core.ipc.layout import PANIC_STOP_FILE, IpcLayout, SnapshotSlot
from pz_agent_core.ipc.queue import CommandQueue
from pz_agent_core.ipc.snapshot import SnapshotMiss, SnapshotReader, SnapshotWriter
from pz_agent_core.protocol import (
    ALWAYS_ALLOWED_ACTIONS,
    ActionName,
    ActionOwnership,
    ActionResult,
    ActionStatus,
    Command,
    CommandPolicy,
    DangerLevel,
    Observation,
    Priority,
    ReasonCode,
)
from pz_agent_core.safety.reflex import (
    ReflexGuard,
    ReflexSignals,
    SafetyEvent,
    may_cancel_running_action,
)
from tests.fixtures import (
    DEFAULT_SESSION,
    make_action_state,
    make_observation,
    make_player,
    make_safety,
)
from tests.fixtures.action_doubles import (
    FakeClock,
    FakeCommandSink,
    FakeObservationSource,
    StubAdapter,
)
from tests.fixtures.ipc_builders import IPC_SESSION_ID, make_layout
from tests.fixtures.ipc_builders import FakeClock as IpcClock
from tests.fixtures.safety_builders import NOW_MS, in_flight, make_nearby, make_zombie

GUARD: Final = ReflexGuard()

#: Every mutating action must be refused while the latch is down; this one
#: stands for all of them because the refusal is in ``_session_abort``, which
#: runs before anything action-specific.
MUTATING: Final = ActionName.MOVEMENT_MOVE_TO

#: The three commands that carry a stop out, written out rather than iterated
#: from the frozenset the engine consults. A test that derives its cases from
#: the set under test cannot notice the set shrinking: dropping ``safety.stop``
#: from it would silently leave this file exercising two actions and passing.
EXPECTED_ALWAYS_ALLOWED: Final[frozenset[str]] = frozenset(
    {"safety.stop", "session.disarm", "plan.cancel"}
)


def _obs(**overrides: Any) -> Observation:
    base: dict[str, Any] = {"seq": 1, "timestamp_ms": NOW_MS}
    base.update(overrides)
    return make_observation(**base)


def _panic(**overrides: Any) -> ReflexSignals:
    base: dict[str, Any] = {"now_ms": NOW_MS, "panic_requested": True}
    base.update(overrides)
    return ReflexSignals(**base)


def _one(events: list[SafetyEvent], code: ReasonCode) -> SafetyEvent:
    matching = [event for event in events if event.reason_code is code]
    assert len(matching) == 1, (
        f"expected exactly one {code.value}, got {[e.reason_code.value for e in events]}"
    )
    return matching[0]


# ---------------------------------------------------------------------------
# no core, no game, no queue, no link
# ---------------------------------------------------------------------------

#: One degraded world each, named for the subsystem that is gone. The panic
#: verdict must be identical in all of them.
DEGRADED: Final[tuple[tuple[str, dict[str, Any], dict[str, Any]], ...]] = (
    # Nothing else is wrong: the baseline the rest are compared against.
    ("everything_up", {}, {}),
    # No game: the mod's heartbeat stopped and no world data came with the tick.
    ("no_game", {"nearby": None}, {"game_alive": False}),
    # No link: the mod says it has stopped seeing this sidecar.
    ("no_link", {"safety": make_safety(sidecar_stale=True)}, {"sidecar_alive": False}),
    # No queue: nothing is in flight and nothing is running, so there is
    # nothing for the stop to cancel — it must still disarm.
    ("no_queue", {"action": make_action_state()}, {"in_flight": ()}),
    # No character: the session is over, which outranks nothing here.
    ("no_character", {"player": make_player(alive=False)}, {}),
    # A world on fire, so the threat rules fire on the same tick.
    (
        "under_attack",
        {
            "safety": make_safety(danger_level=DangerLevel.CRITICAL),
            "nearby": make_nearby(make_zombie("z1", 1.0, chasing=True)),
        },
        {},
    ),
)


@pytest.mark.parametrize(
    ("world", "signals"),
    [(world, signals) for _, world, signals in DEGRADED],
    ids=[name for name, _, _ in DEGRADED],
)
def test_a_panic_stop_is_honoured_whatever_else_has_failed(
    world: dict[str, Any], signals: dict[str, Any]
) -> None:
    """The guard needs no peer, no history and no world to obey a stop."""
    events = GUARD.evaluate(None, _obs(**world), _panic(**signals))

    event = _one(events, ReasonCode.PANIC_STOP)
    assert event.priority is Priority.PANIC_STOP
    assert event.forces_disarm, "a stop that does not disarm leaves the agent armed"
    assert event.cancels_mod_owned_queue, "a stop must clear what the mod queued"
    assert events[0] is event, (
        "the stop must be reported first; it is what the caller acts on before "
        f"anything else, and it came behind {events[0].reason_code.value}"
    )


def test_the_stop_names_the_commands_it_closes_even_with_the_game_gone() -> None:
    """In-flight work is closed by id, so nothing is left believed-running."""
    events = GUARD.evaluate(
        None,
        _obs(),
        _panic(game_alive=False, in_flight=(in_flight("c1"), in_flight("c2"))),
    )

    assert _one(events, ReasonCode.PANIC_STOP).command_ids == ("c1", "c2")


def test_the_stop_is_a_pure_decision_and_holds_without_signals_or_history() -> None:
    """No signals object at all means no panic; the flag is never assumed.

    The complement of the tests above, and the reason they can fail: a guard
    that returned a PANIC_STOP unconditionally would satisfy every assertion in
    this file up to here.
    """
    assert GUARD.evaluate(None, _obs()) == []
    assert GUARD.evaluate(_obs(), _obs(seq=2)) == []


# ---------------------------------------------------------------------------
# the engine while the latch is down
# ---------------------------------------------------------------------------


def _engine(*, panic: bool, action: ActionName = MUTATING) -> tuple[ActionEngine, FakeCommandSink]:
    """The production engine over doubles, with the stop lever in one position."""
    clock = FakeClock()
    sink = FakeCommandSink(clock)
    source = FakeObservationSource(clock)
    source.repeat(_obs())
    registry = AdapterRegistry()
    registry.register(StubAdapter(name=action, verify_after=1))
    engine = ActionEngine(
        registry=registry,
        sink=sink,
        observations=source,
        clock=clock,
        panic_stop=lambda: panic,
        capability_check=lambda _: True,
    )
    return engine, sink


def _request(action: ActionName) -> ActionRequest:
    return ActionRequest(
        action=action,
        session_id=DEFAULT_SESSION,
        idempotency_key=f"{action.value}-1",
        args={"target": {"x": 1, "y": 2, "z": 0}},
        policy=CommandPolicy(allow_interrupt=True, max_retries=0),
    )


def test_nothing_mutating_reaches_the_mod_while_the_latch_is_down() -> None:
    engine, sink = _engine(panic=True)

    result = engine.execute(_request(MUTATING))

    assert result.status is ActionStatus.REJECTED
    assert result.reason_code is ReasonCode.PANIC_STOP
    assert sink.sent == [], "a latched stop must reach the mod before the command does"


#: Sorted so the parametrisation is stable whatever order the frozenset yields.
STOPPING_ACTIONS: Final = tuple(sorted(ALWAYS_ALLOWED_ACTIONS, key=lambda name: name.value))


def test_the_set_of_always_allowed_commands_is_exactly_these_three() -> None:
    """The parametrisation below is only as strong as the set it walks."""
    assert {action.value for action in ALWAYS_ALLOWED_ACTIONS} == EXPECTED_ALWAYS_ALLOWED


@pytest.mark.parametrize(
    "action", STOPPING_ACTIONS, ids=[action.value for action in STOPPING_ACTIONS]
)
def test_stopping_disarming_and_cancelling_still_work_while_the_latch_is_down(
    action: ActionName,
) -> None:
    """The lever must not disable the commands that carry the stop out."""
    engine, sink = _engine(panic=True, action=action)

    result = engine.execute(_request(action))

    assert result.reason_code is not ReasonCode.PANIC_STOP
    assert [command.action for command in sink.sent] == [action]


def test_the_same_command_runs_when_the_lever_is_up() -> None:
    """So the refusal above is the lever's doing and not the harness's."""
    engine, sink = _engine(panic=False)

    result = engine.execute(_request(MUTATING))

    assert result.reason_code is not ReasonCode.PANIC_STOP
    assert [command.action for command in sink.sent] == [MUTATING]


# ---------------------------------------------------------------------------
# only the mod's own entries (E12-M03-T008)
# ---------------------------------------------------------------------------

#: Every (ownership, busy) pair, with the answer written out here rather than
#: derived from the production rule. Only a busy action the mod positively owns
#: may be cancelled; ``ambiguous`` and ``none`` are the player's.
OWNERSHIP_CASES: Final[tuple[tuple[ActionOwnership, bool, bool], ...]] = (
    (ActionOwnership.MOD, True, True),
    (ActionOwnership.MOD, False, False),
    (ActionOwnership.MANUAL, True, False),
    (ActionOwnership.MANUAL, False, False),
    (ActionOwnership.AMBIGUOUS, True, False),
    (ActionOwnership.AMBIGUOUS, False, False),
    (ActionOwnership.NONE, True, False),
    (ActionOwnership.NONE, False, False),
)


@pytest.mark.parametrize(
    ("ownership", "busy", "may_cancel"),
    OWNERSHIP_CASES,
    ids=[f"{o.value}-{'busy' if b else 'idle'}" for o, b, _ in OWNERSHIP_CASES],
)
def test_a_stop_cancels_the_running_action_only_on_proof_that_it_is_ours(
    ownership: ActionOwnership, busy: bool, may_cancel: bool
) -> None:
    action = make_action_state(ownership=ownership, busy=busy, type="consume.eat")
    current = _obs(action=action)

    assert may_cancel_running_action(action) is may_cancel
    event = _one(GUARD.evaluate(_obs(), current, _panic()), ReasonCode.PANIC_STOP)
    assert event.cancels_running_action is may_cancel
    assert event.cancels_mod_owned_queue, (
        "clearing the mod's own queue entries never depends on who owns the "
        "action currently running"
    )


def test_four_rules_firing_at_once_cannot_widen_a_cancel_onto_the_players_action() -> None:
    """The union in ``_first_per_reason`` merges authority, never ownership.

    Panic, a takeover, a critical threat and a stalled command all fire on this
    tick, and the character is mid-way through an action nobody can prove is
    the mod's. Not one of the returned events may claim the right to cancel it.
    """
    current = _obs(
        seq=2,
        action=make_action_state(
            ownership=ActionOwnership.AMBIGUOUS, busy=True, type="consume.eat"
        ),
        safety=make_safety(manual_takeover=True, danger_level=DangerLevel.CRITICAL),
        nearby=make_nearby(make_zombie("z1", 1.0, chasing=True)),
    )
    signals = _panic(
        in_flight=(in_flight("c1", last_progress_ms=NOW_MS - 60_000),),
        sidecar_alive=False,
    )

    events = GUARD.evaluate(_obs(), current, signals)

    assert len(events) > 1, "this test is only meaningful when several rules fire"
    offenders = [e.reason_code.value for e in events if e.cancels_running_action]
    assert offenders == [], f"these events would cancel the player's action: {offenders}"


def test_the_stop_does_cancel_a_running_action_the_mod_owns() -> None:
    """The other half, so the rule above is not "never cancel anything"."""
    current = _obs(
        action=make_action_state(ownership=ActionOwnership.MOD, busy=True, type="consume.eat")
    )

    event = _one(GUARD.evaluate(_obs(), current, _panic()), ReasonCode.PANIC_STOP)

    assert event.cancels_running_action


# ---------------------------------------------------------------------------
# the latch, and the exchange directory it lives in
# ---------------------------------------------------------------------------

#: The exchange root as Windows spells it. The shipping platform is Windows and
#: the tests run on Linux, so the path the mod and the sidecar agree on is built
#: here in its Windows shape: a name carrying a separator, a drive letter or a
#: character Win32 rejects would compose into a *different* file there and into
#: a perfectly good one on the machine running this.
WINDOWS_ROOT: Final = PureWindowsPath(r"C:\Users\Ivan\Zomboid\Lua\pz_agent")


def _latch(layout: IpcLayout) -> PanicSwitch:
    """The panic switch as the runtime wires it: the latch file's presence.

    Passed to :class:`ActionEngine` as its ``panic_stop``, so the refusals below
    are decided by a real ``stat`` against a real (and, further down, thoroughly
    broken) directory rather than by a boolean the test made up.
    """

    def engaged() -> bool:
        return layout.panic_stop.is_file()

    return engaged


def test_the_latch_is_one_file_directly_in_the_exchange_root() -> None:
    """Written out for Windows, where a stray separator would relocate it.

    ``panic.stop`` has to be the same file for two processes that compose it
    independently — Lua from ``Ipc.lua``'s table, Python from the layout — so
    the name is pinned as a literal and composed against a Windows root.
    """
    assert PANIC_STOP_FILE == "panic.stop"
    composed = WINDOWS_ROOT / PANIC_STOP_FILE
    assert str(composed) == r"C:\Users\Ivan\Zomboid\Lua\pz_agent\panic.stop"
    assert composed.parent == WINDOWS_ROOT, "the latch must not gain a directory level"
    # `<>:"/\|?*` are the characters Win32 rejects outright; the colon after a
    # drive letter is the only legal one and cannot appear in a bare filename.
    assert not set(PANIC_STOP_FILE) & set('<>:"/\\|?*')


def test_the_layout_owns_the_latch_and_will_not_take_a_neighbour_for_it(tmp_path: Path) -> None:
    """The control for the guard writers use before opening a file."""
    layout = make_layout(tmp_path / "pz_agent")

    assert layout.panic_stop == layout.root / PANIC_STOP_FILE
    assert layout.is_managed_path(layout.panic_stop)
    assert not layout.is_managed_path(layout.root / "panic.stop.evil")
    assert not layout.is_managed_path(layout.root.parent / PANIC_STOP_FILE)


# -- the three wrecks -------------------------------------------------------
#
# Deliberately not `chmod`: this suite runs as root on Linux, where the mode
# bits do not stop a write at all, and Windows ignores them for directories
# outright. Each wreck below fails the same way on both platforms — a directory
# standing where a file must be replaced, a byte-level truncation, torn JSON —
# so a Linux pass here is evidence about Windows rather than about POSIX modes.


def _wreck_state_writes(layout: IpcLayout) -> None:
    """Make every state document impossible to publish.

    ``write_json_atomic`` finishes with :func:`os.replace` onto the target, and
    replacing a *directory* with a file fails on POSIX (``EISDIR``) and on Win32
    (``MoveFileEx`` refuses an existing directory) alike.
    """
    layout.session.mkdir()
    layout.sidecar_heartbeat.mkdir()


def _wreck_journal(layout: IpcLayout) -> None:
    """Leave the ack journal ending in the middle of a record."""
    writer = JournalWriter(layout, layout.command_ack)
    try:
        writer.append({"type": "ack.first", "n": 1})
        writer.append({"type": "ack.torn", "n": 2})
    finally:
        writer.close()
    raw = layout.command_ack.read_bytes()
    layout.command_ack.write_bytes(raw[: -len(b'"n":2}\n')])


def _wreck_snapshot(layout: IpcLayout) -> None:
    """Publish a pointer, then tear both slots it could lead to."""
    SnapshotWriter(layout).publish({"seq": 7, "player": {"hunger": 0.2}})
    for slot in (SnapshotSlot.A, SnapshotSlot.B):
        layout.snapshot_slot(slot).write_text('{"seq": 7, "player": {"hun', encoding="utf-8")


WRECKS: Final = {
    "state_unwritable": _wreck_state_writes,
    "truncated_journal": _wreck_journal,
    "corrupt_snapshot": _wreck_snapshot,
}


def _exchange(tmp_path: Path, *wrecks: str, latched: bool = True) -> IpcLayout:
    layout = make_layout(tmp_path / "pz_agent")
    if latched:
        layout.panic_stop.write_text("", encoding="utf-8")
    for name in wrecks:
        WRECKS[name](layout)
    return layout


def test_the_state_directory_really_cannot_be_written(tmp_path: Path) -> None:
    """Negative control for ``state_unwritable``.

    Without this, "the stop works with an unwritable state directory" would be
    satisfied by a directory that was perfectly writable.
    """
    layout = _exchange(tmp_path, "state_unwritable")

    with pytest.raises(OSError):
        write_json_atomic(layout, layout.session, {"session_id": IPC_SESSION_ID})
    with pytest.raises(OSError):
        write_json_atomic(layout, layout.sidecar_heartbeat, {"peer": "sidecar"})


def test_the_truncated_journal_really_loses_its_last_record(tmp_path: Path) -> None:
    """Negative control for ``truncated_journal``: the tear must be observable.

    The torn record is withheld rather than half-parsed, and the offset stops on
    the newline boundary before it — that is what lets the reader resume if the
    producer ever finishes the line.
    """
    layout = _exchange(tmp_path, "truncated_journal")

    read = JournalReader(layout, layout.command_ack).read()

    assert [payload["type"] for payload in read.payloads] == ["ack.first"]
    assert read.pending_bytes > 0, "the torn tail is still sitting there unconsumed"
    assert layout.command_ack.read_bytes()[read.offset - 1 : read.offset] == b"\n"


def test_the_corrupt_snapshot_really_yields_nothing(tmp_path: Path) -> None:
    """Negative control for ``corrupt_snapshot``.

    Both slots and the fallback path are gone, so the reader must report a miss.
    Returning a partially parsed document would be the fabricated observation
    this project forbids, and a miss is what the guard then has to survive.
    """
    layout = _exchange(tmp_path, "corrupt_snapshot")

    read = SnapshotReader(layout).read()

    assert isinstance(read, SnapshotMiss)
    assert len(read.diagnostics) == 2, f"both slots must be reported: {read.diagnostics}"


ALL_WRECKS: Final = tuple(WRECKS)


@pytest.mark.parametrize(
    "wrecks",
    [(), *[(name,) for name in ALL_WRECKS], ALL_WRECKS],
    ids=["healthy", *ALL_WRECKS, "all_at_once"],
)
def test_the_latch_is_still_readable_with_the_exchange_directory_wrecked(
    tmp_path: Path, wrecks: tuple[str, ...]
) -> None:
    """Reading the latch is a ``stat``; none of the wrecks is on that path."""
    layout = _exchange(tmp_path, *wrecks)

    assert _latch(layout)() is True


def _wrecked_engine(
    layout: IpcLayout, observation: Observation
) -> tuple[ActionEngine, FakeCommandSink]:
    """The production engine, with the latch read out of *layout*."""
    clock = FakeClock()
    sink = FakeCommandSink(clock)
    source = FakeObservationSource(clock)
    source.repeat(observation)
    registry = AdapterRegistry()
    registry.register(StubAdapter(name=MUTATING, verify_after=1))
    engine = ActionEngine(
        registry=registry,
        sink=sink,
        observations=source,
        clock=clock,
        panic_stop=_latch(layout),
        capability_check=lambda _: True,
    )
    return engine, sink


#: Everything else that could be wrong at the same moment, in one observation:
#: the mod has stopped seeing this sidecar, the world is on fire, the player has
#: taken over and the action queue holds something nobody can prove is ours.
def _everything_wrong() -> Observation:
    return _obs(
        seq=9,
        safety=make_safety(
            sidecar_stale=True, manual_takeover=True, danger_level=DangerLevel.CRITICAL
        ),
        action=make_action_state(
            ownership=ActionOwnership.AMBIGUOUS, busy=True, type="consume.eat"
        ),
        nearby=make_nearby(make_zombie("z1", 1.0, chasing=True)),
    )


def test_the_engine_refuses_through_a_wrecked_exchange_directory(tmp_path: Path) -> None:
    """Every subsystem down at once, and the stop still outranks all of them.

    ``_session_abort`` consults the latch before the session id, the save id and
    the staleness flag, so the code that comes back names the stop rather than
    whichever other thing happened to be checked first — which is what the user
    who pressed the key needs to be told.
    """
    layout = _exchange(tmp_path, *ALL_WRECKS)
    engine, sink = _wrecked_engine(layout, _everything_wrong())

    result = engine.execute(_request(MUTATING))

    assert result.status is ActionStatus.REJECTED
    assert result.reason_code is ReasonCode.PANIC_STOP
    assert sink.sent == [], "a latched stop must reach the mod before the command does"


def test_the_same_wrecked_directory_without_the_latch_ships_the_command(
    tmp_path: Path,
) -> None:
    """The control: the refusal above is the latch's doing, not the wreckage's.

    A healthy observation this time, because the point is narrow — with the
    exchange directory in exactly the same three broken states and no
    ``panic.stop`` in it, the command goes out.
    """
    layout = _exchange(tmp_path, *ALL_WRECKS, latched=False)
    engine, sink = _wrecked_engine(layout, _obs())

    result = engine.execute(_request(MUTATING))

    assert _latch(layout)() is False
    assert result.reason_code is not ReasonCode.PANIC_STOP
    assert [command.action for command in sink.sent] == [MUTATING]


def test_the_guard_reaches_the_same_verdict_with_everything_down_at_once() -> None:
    """No game, no link, no queue, no history, no world data — one verdict.

    Compared against the calm baseline field by field rather than re-asserted,
    so a rule that quietly stopped disarming, or stopped naming the commands it
    closes, when the tick is also carrying four other failures shows up here.
    """
    calm = _one(GUARD.evaluate(_obs(), _obs(seq=2), _panic()), ReasonCode.PANIC_STOP)

    besieged = _one(
        GUARD.evaluate(
            None,
            _obs(
                seq=2,
                nearby=None,
                safety=make_safety(
                    sidecar_stale=True, manual_takeover=True, danger_level=DangerLevel.CRITICAL
                ),
                action=make_action_state(),
            ),
            _panic(game_alive=False, sidecar_alive=False, in_flight=()),
        ),
        ReasonCode.PANIC_STOP,
    )

    assert besieged == calm


# ---------------------------------------------------------------------------
# what the stop decision is allowed to depend on
# ---------------------------------------------------------------------------

CORE_SRC: Final = (
    Path(__file__).resolve().parents[2] / "packages" / "pz_agent_core" / "src" / "pz_agent_core"
)

#: What each half of the stop may reach for, written out. A stop that needed the
#: Local Core RPC link, a planner, a provider or a journal would stop working in
#: exactly the situations it exists for, so the closure is bounded by name.
ALLOWED_CLOSURES: Final[dict[str, frozenset[str]]] = {
    "safety.reflex": frozenset({"safety.reflex", "safety.threat", "protocol", "version"}),
    "actions.engine": frozenset(
        {"actions.engine", "actions.adapter", "ipc.clocks", "protocol", "version"}
    ),
}

#: Nothing on the stop's path may talk to a socket, spawn anything or schedule
#: work: each of those is a way for a stop to block on something that is down.
FORBIDDEN_STDLIB: Final[frozenset[str]] = frozenset(
    {"socket", "ssl", "http", "urllib", "subprocess", "asyncio", "multiprocessing", "threading"}
)


def _module_file(dotted: str) -> Path | None:
    direct = CORE_SRC.joinpath(*dotted.split(".")).with_suffix(".py")
    if direct.is_file():
        return direct
    package = CORE_SRC.joinpath(*dotted.split("."), "__init__.py")
    return package if package.is_file() else None


def _imports(dotted: str) -> tuple[set[str], set[str]]:
    """The (in-package, stdlib) module names *dotted* imports, from its source."""
    path = _module_file(dotted)
    assert path is not None, f"{dotted} is not a module under pz_agent_core"
    is_package = path.name == "__init__.py"
    parts = dotted.split(".")
    anchor = parts if is_package else parts[:-1]
    inside: set[str] = set()
    outside: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            outside.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if not node.level:
                outside.add((node.module or "").split(".")[0])
                continue
            base = anchor[: len(anchor) - node.level + 1]
            target = ".".join(base + ([node.module] if node.module else []))
            inside.add(target)
            inside.update(f"{target}.{alias.name}" for alias in node.names)
    return inside, outside


def _closure(root: str) -> tuple[set[str], set[str]]:
    seen: set[str] = set()
    stdlib: set[str] = set()
    pending = [root]
    while pending:
        current = pending.pop()
        if current in seen or _module_file(current) is None:
            continue
        seen.add(current)
        inside, outside = _imports(current)
        stdlib |= outside
        pending.extend(inside)
    return seen, stdlib


@pytest.mark.parametrize("module", sorted(ALLOWED_CLOSURES), ids=sorted(ALLOWED_CLOSURES))
def test_the_stop_needs_no_link_no_planner_and_no_journal(module: str) -> None:
    """Both halves of the stop are reachable with nothing else running."""
    reached, stdlib = _closure(module)

    allowed = ALLOWED_CLOSURES[module]
    unexpected = sorted(
        name
        for name in reached
        if not any(name == ok or name.startswith(f"{ok}.") for ok in allowed)
    )
    assert unexpected == [], f"{module} now reaches {unexpected}"
    assert not stdlib & FORBIDDEN_STDLIB, f"{module} reaches {sorted(stdlib & FORBIDDEN_STDLIB)}"


def test_the_closure_walker_actually_walks() -> None:
    """Control for the two tests above: an empty closure would satisfy them.

    A walker that silently returned nothing — a wrong root, a path that stopped
    resolving — would report every module as clean forever. So would one that
    stopped at the first hop: ``protocol.messages`` is two relative imports and
    a package re-export away from ``actions.engine``.
    """
    reached, stdlib = _closure("actions.engine")

    assert "protocol.messages" in reached, "the walker did not follow the package re-exports"
    assert "ipc.clocks" in reached
    assert {"uuid", "dataclasses"} <= stdlib, "the walker did not collect plain imports"


# ---------------------------------------------------------------------------
# a stop is never queued behind anything (§3.11)
# ---------------------------------------------------------------------------


def _queue(tmp_path: Path) -> CommandQueue:
    return CommandQueue(layout=make_layout(tmp_path / "pz_agent"), session_id=IPC_SESSION_ID)


def test_a_stop_is_written_while_an_ordinary_command_is_refused(tmp_path: Path) -> None:
    """Backpressure holds for everything except the thing that ends it."""
    queue = _queue(tmp_path)
    try:
        occupied = queue.submit(queue.build(MUTATING, idempotency_key="move-1", lease_ms=30_000))
        assert occupied.accepted
        blocked = queue.submit(
            queue.build(ActionName.CONSUME_EAT, idempotency_key="eat-1", lease_ms=30_000)
        )
        assert blocked.rejection is not None
        assert blocked.rejection.reason_code is ReasonCode.QUEUE_REJECTED

        stop = queue.send_stop(idempotency_key="stop-1")

        assert stop.action is ActionName.SAFETY_STOP
        assert queue.in_flight is not None
        assert queue.in_flight.command_id == occupied.command.command_id, (
            "the stop must overtake the in-flight command, not evict it — its "
            "idempotency key is where the terminal ack still has to be filed"
        )
    finally:
        queue.close()


def _acked(command: Command, *, seq: int) -> ActionResult:
    """The mod's terminal answer to *command* — what the cache is filled from."""
    return ActionResult.failure(
        session_id=command.session_id,
        seq=seq,
        command_id=command.command_id,
        action=command.action.value,
        timestamp_ms=NOW_MS,
        reason_code=ReasonCode.POSTCONDITION_MET,
        status=ActionStatus.CANCELLED,
        message="the mod cleared what it owned",
    )


def test_a_stop_is_written_even_when_its_key_already_has_a_terminal_result(
    tmp_path: Path,
) -> None:
    """The cache is genuinely armed, and the stop goes past it anyway.

    An ordinary command reusing a key is answered from the cache, which is
    exactly right: re-executing "eat half a sandwich" costs food that cannot be
    un-eaten. A stop is the opposite trade — the cost of one redundant stop is
    nothing, and the cost of a replayed refusal is an agent that keeps going —
    so ``send_stop`` writes rather than consulting the cache at all.
    """
    layout = make_layout(tmp_path / "pz_agent")
    queue = CommandQueue(layout=layout, session_id=IPC_SESSION_ID, clock=IpcClock())
    try:
        first = queue.send_stop(idempotency_key="stop-1")
        queue.record_ack(_acked(first, seq=0))
        assert "stop-1" in queue.cache, "the cache did not take the ack, so it proves nothing"

        # The control: with the cache in that state, an ordinary command on the
        # same key is answered from it and never reaches the journal.
        ordinary = queue.submit(queue.build(MUTATING, idempotency_key="stop-1", lease_ms=30_000))
        assert ordinary.duplicate
        assert not ordinary.accepted

        second = queue.send_stop(idempotency_key="stop-1")
    finally:
        queue.close()

    assert first.command_id != second.command_id
    assert second.seq == first.seq + 1, "the second stop took the next stream sequence"
    written = [
        json.loads(line)
        for line in layout.command_queue.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    stops = [record["command_id"] for record in written if record.get("action") == "safety.stop"]
    assert stops == [first.command_id, second.command_id], (
        "both stops must be on disk; a stop answered from the cache is a stop "
        "that never reached the mod"
    )


def test_every_stop_reaches_the_journal_as_the_action_the_mod_dispatches_on(
    tmp_path: Path,
) -> None:
    """Read back off disk and matched against the literal wire value.

    ``CommandDispatcher`` in the mod branches on this string. A round trip
    through :class:`~pz_agent_core.protocol.messages.Command` would pass just as
    happily if both halves were renamed together, so the file is parsed as bare
    JSON here and the name is written out.
    """
    layout = make_layout(tmp_path / "pz_agent")
    queue = CommandQueue(layout=layout, session_id=IPC_SESSION_ID, clock=IpcClock())
    try:
        queue.submit(queue.build(MUTATING, idempotency_key="move-1", lease_ms=30_000))
        queue.send_stop(idempotency_key="stop-1")
    finally:
        queue.close()

    lines = [
        json.loads(line)
        for line in layout.command_queue.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    actions = [record["action"] for record in lines if "action" in record]

    assert actions == ["movement.move_to", "safety.stop"]
    stop = lines[-1]
    assert stop["session_id"] == IPC_SESSION_ID
    assert stop["seq"] == 1, "the stop consumed the next stream sequence, leaving no gap"
