"""``pz-agent status``: the one-shot report against a heartbeat that stopped.

The rest of the one-shot report is covered in ``tests/unit/test_cli_app.py``.
What this file owns is the single question that document cannot answer by
itself: the tier-0 fields — build, armed, mode, player present, active action —
all come out of one heartbeat, and a heartbeat is only a statement about the
moment it was written. A game that crashed while armed leaves that file behind
with ``armed: true`` in it forever.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from pz_agent_cli.context import EXIT_OK
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.protocol import SessionMode
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer
from pz_agent_core.version import MOD_VERSION
from tests.fixtures import DEFAULT_SESSION
from tests.fixtures.cli_worlds import CliWorld, make_world

BUILD: Final = "42.20"

#: Long past the 5 s liveness window, and long enough that no reader could call
#: it a slow frame: this is a game that is not running.
LONG_SILENCE_MS: Final = 3_600_000


def _publish_heartbeat(world: CliWorld, *, armed: bool) -> None:
    """The game's own file, written by the monitor the mod writes it with."""
    assert world.ipc_root is not None
    layout = IpcLayout(world.ipc_root)
    layout.ensure()
    HeartbeatMonitor(layout, clock=lambda: world.clock.now_ms).publish(
        Peer.GAME,
        session_id=DEFAULT_SESSION,
        nonce="nonce-1",
        version=MOD_VERSION,
        build=BUILD,
        player_present=True,
        armed=armed,
        mode=SessionMode.AUTONOMOUS,
        active_action_id="act-7",
    )


def _status(world: CliWorld) -> str:
    world.reset_streams()
    assert world.run("status") == EXIT_OK
    return world.stdout


def test_a_silent_heartbeat_is_not_printed_as_the_state_now(tmp_path: Path) -> None:
    """An hour of silence must not read as an armed agent in the present tense."""
    world = make_world(tmp_path)
    _publish_heartbeat(world, armed=True)
    world.clock.advance(LONG_SILENCE_MS)

    printed = _status(world)

    assert "stale" in printed
    assert "not the state now" in printed
    # The last word is still shown — the evidence is what a user diagnosing a
    # crashed game needs — and it is shown below the sentence that dates it.
    assert printed.index("not the state now") < printed.index("armed")
    assert "armed                  yes" in printed


def test_a_live_heartbeat_carries_no_such_caveat(tmp_path: Path) -> None:
    """The note is about staleness, so a game that is reporting gets none of it."""
    world = make_world(tmp_path)
    world.clock.freeze()
    _publish_heartbeat(world, armed=True)

    printed = _status(world)

    assert "live" in printed
    assert "not the state now" not in printed
    assert "armed                  yes" in printed
