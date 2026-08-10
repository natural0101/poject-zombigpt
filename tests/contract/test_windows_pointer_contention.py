"""Two processes contending on the snapshot exchange, the way the live game does.

The 2026-08-08 session on Build 42.20.2 had the mod and the sidecar sharing
``observation.snapshot.pointer`` and the two slot files across process
boundaries: the mod rewrites them truncate-in-place at a few hertz (Kahlua has
no rename, so the Lua writer is *not* the atomic writer — a reader really can
catch a half-written file), while the sidecar polls at tick rate. This soak
reproduces that shape with a real child process speaking the mod's protocol
faithfully — plain ``open('w')`` rewrites of slot a/b alternating, the pointer
written last, an occasional deliberately torn slot left visible for a beat —
against the real :class:`SnapshotReader` polling from this process.

What it can and cannot prove: on POSIX an open handle never refuses a read, so
the ``PermissionError`` paths themselves are pinned by the monkeypatched unit
tests in ``tests/unit/test_ipc_atomic_patience.py`` and
``tests/unit/test_ipc_snapshot.py``. What runs here on any OS is everything
else the contention feeds: torn documents served mid-write, pointer and slot
racing each other, and the freshness rule that no accepted snapshot may ever
step backwards.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from pz_agent_core.ipc.layout import (
    SNAPSHOT_POINTER_FILE,
    SNAPSHOT_SLOT_A_FILE,
    SNAPSHOT_SLOT_B_FILE,
    IpcLayout,
)
from pz_agent_core.ipc.snapshot import SnapshotRead, SnapshotReader

pytestmark = pytest.mark.contract

#: How long the child keeps publishing. Long enough that the two processes
#: cross paths hundreds of times, short enough that the whole test stays well
#: under fifteen seconds with process start-up and teardown around it.
SOAK_SECONDS = 6.0

#: The mod publishes at a few hertz; the sidecar polls faster. Both rates are
#: from the live session's order of magnitude, not tuned to avoid collisions.
WRITER_HZ = 10.0
READER_PAUSE_SECONDS = 0.05

#: Every this-many publishes the child first leaves a torn prefix of the slot
#: on disk for a few milliseconds before completing it — the exact residue a
#: truncate-in-place writer shows a concurrent reader.
TORN_EVERY = 7

#: The parent's own hard ceiling on the poll loop, so a child that wedges
#: cannot hold the test open until the global pytest timeout.
PARENT_DEADLINE_SECONDS = 20.0

#: The mod-shaped writer. Deliberately free of pz_agent_core imports: the real
#: counterpart is Lua, and the point is that the reader survives a peer that
#: shares nothing with it but the file protocol. Slot first, pointer last —
#: the pointer is the commit — and every write is a plain truncating rewrite.
WRITER_SCRIPT = textwrap.dedent(
    """
    import json
    import sys
    import time

    root, duration, hz, torn_every = (
        sys.argv[1],
        float(sys.argv[2]),
        float(sys.argv[3]),
        int(sys.argv[4]),
    )
    slots = {
        "a": root + "/observation.snapshot.a.json",
        "b": root + "/observation.snapshot.b.json",
    }
    pointer = root + "/observation.snapshot.pointer"
    deadline = time.monotonic() + duration
    seq = 0
    slot = "b"
    while time.monotonic() < deadline:
        seq += 1
        slot = "a" if slot == "b" else "b"
        body = json.dumps({"seq": seq, "full": True, "player": {"present": True, "tick": seq}})
        if torn_every and seq % torn_every == 0:
            # The residue of dying mid-write: a truncating rewrite that stops
            # partway, left on disk long enough for the poller to see it.
            with open(slots[slot], "w", encoding="utf-8") as handle:
                handle.write(body[: len(body) // 2])
            time.sleep(0.004)
        with open(slots[slot], "w", encoding="utf-8") as handle:
            handle.write(body)
        with open(pointer, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"slot": slot, "seq": seq, "written_at_ms": int(time.time() * 1000)}
                )
            )
        time.sleep(1.0 / hz)
    print(seq)
    """
)


def test_the_reader_survives_a_live_mod_shaped_writer(tmp_path: Path) -> None:
    exchange = tmp_path / "exchange"
    layout = IpcLayout(exchange)
    layout.ensure()
    script = tmp_path / "mod_writer.py"
    script.write_text(WRITER_SCRIPT, encoding="utf-8")

    reader = SnapshotReader(layout)
    accepted: list[int] = []
    miss_diagnostics: list[str] = []
    polls = 0

    child = subprocess.Popen(
        [
            sys.executable,
            str(script),
            str(exchange),
            str(SOAK_SECONDS),
            str(WRITER_HZ),
            str(TORN_EVERY),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + PARENT_DEADLINE_SECONDS
        while child.poll() is None and time.monotonic() < deadline:
            polls += 1
            try:
                result = reader.read()
            except Exception as exc:  # the pin *is* "never raises", so catch-all is the point
                pytest.fail(f"the reader crashed mid-soak on poll {polls}: {exc!r}")
            if isinstance(result, SnapshotRead):
                accepted.append(result.seq)
            else:
                miss_diagnostics.extend(result.diagnostics)
            time.sleep(READER_PAUSE_SECONDS)
        stdout, stderr = child.communicate(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)

    # The child exits cleanly, and its last line is how many snapshots it
    # published — the ground truth the reader's view is checked against.
    assert child.returncode == 0, f"the writer died: {stderr}"
    published = int(stdout.strip())
    assert published >= 20, f"the soak barely ran: {published} publishes"

    # The reader actually saw the world moving, not one lucky snapshot.
    assert len(accepted) >= 10, (
        f"only {len(accepted)} accepted reads over {polls} polls; misses: {miss_diagnostics[-5:]}"
    )

    # Freshness is monotonic: no accepted snapshot may carry a seq below one
    # already accepted, whatever mix of tears and fallbacks produced it.
    assert accepted == sorted(accepted), f"accepted sequence went backwards: {accepted}"

    # Once the writer has stopped, the last publish is complete on disk and
    # nothing is torn: the very next poll must serve exactly the final seq.
    final = reader.read()
    assert isinstance(final, SnapshotRead), f"final poll missed: {final}"
    assert final.seq == published

    # Torn slots and half-written pointers were misses or fallbacks, never
    # accepted documents — so every accepted seq is one the child published.
    assert all(1 <= seq <= published for seq in accepted)

    # The exchange directory holds only the protocol's own files: no scratch
    # files, no leftovers. (The mod-shaped writer never uses temp files, and
    # the reader must not have created any.)
    leftovers = sorted(entry.name for entry in exchange.iterdir())
    expected = sorted([SNAPSHOT_SLOT_A_FILE, SNAPSHOT_SLOT_B_FILE, SNAPSHOT_POINTER_FILE, "logs"])
    assert leftovers == expected, f"unexpected residue in the exchange dir: {leftovers}"
