"""Property fuzz over the journal *reader* boundary (Agent J).

The journal is fed by the mod writing to disk, so its on-disk state is whatever
a process crashing mid-write, a partial fsync, or a corrupted sector can leave:
a torn final line, a duplicated newline, garbage bytes, non-UTF8, a line past
every cap, a valid-JSON line that is not a record shape. ``test_ipc_journal``
pins each of those as a hand-written example; this module asserts the same
contract as a *property* over a seeded corpus, because the value of a fuzz is
finding the one on-disk state nobody thought to write by hand.

The single invariant every property here rests on is journal.py's own promise
(§3.5): reading a damaged journal yields the boundary's *documented* outcome — a
valid record delivered, a corrupt line turned into one diagnostic and skipped, a
reserved marker dropped, a torn tail withheld until whole — and **never a bare
exception through to the caller**, and always terminates in bounded work and
bounded memory. A round-trip property guards the other direction: a corpus the
real writer produced reads back field-for-field.

Determinism is load-bearing: every corpus comes from ``random.Random(_SEED)`` so
a failure names a reproducible file, never a lucky draw. Every per-record label
is a function of its index, not of the RNG, so a mismatch after a round trip
points at the exact record rather than at a value the reader might legitimately
have reordered.

Two escapes fell out of this fuzz — a JSON line nested past ~1000 deep, or an
integer past 4300 digits, once left the reader as a ``RecursionError`` / bare
``ValueError`` instead of a skipped 'corrupt record'. Both are now fixed (a
depth scan before parsing and a widened ``except``), each with its own
regression test at the bottom. The broad corpora still keep their generated
lines under those two boundaries, so their properties cover the general shape
while the two tests pin the exact edges.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest

from pz_agent_core.ipc.atomic import encode_json_line
from pz_agent_core.ipc.journal import (
    HEADER_TYPE,
    MAX_LINE_BYTES,
    MAX_READ_BYTES,
    RESERVED_TYPES,
    ROTATED_TYPE,
    JournalReader,
    JournalWriter,
)
from pz_agent_core.ipc.layout import IpcLayout
from tests.fixtures.ipc_builders import make_layout

#: A fixed literal seed. The corpus must be byte-identical on every run so a
#: failure is a reproducible on-disk state — never the global RNG, never a clock.
_SEED = 20260808

#: Byte sequences that are not valid UTF-8: a lone BOM tail, high bytes with no
#: lead, and a truncated two-byte sequence (``café`` cut after its lead byte).
#: Each decodes to a ``UnicodeDecodeError``, which the reader must turn into a
#: diagnostic rather than let escape.
_NON_UTF8: tuple[bytes, ...] = (
    b"\xff\xfe\x00",
    b"\x80\x81\x82",
    b"\xc3\x28",
    b"payload-\xff-tail",
)


def _lay_down(root: Path, body: bytes) -> tuple[IpcLayout, Path]:
    """A journal with a real header, then *body* appended raw after it.

    Every fuzz corpus is prefixed with a header the real writer produced,
    because the reader refuses a file whose first line is not a header — the
    corruption under test belongs in the *body*, which is exactly where a mod
    dying mid-stream leaves it. The body is written in binary so a case can
    inject bytes no text encoder would emit.
    """
    layout = make_layout(root)
    path = layout.command_ack
    JournalWriter(layout, path).close()  # writes only the header line, then closes it
    with path.open("ab") as handle:
        handle.write(body)
    return layout, path


def _varied_payload(rng: random.Random, index: int) -> dict[str, Any]:
    """A record whose shape varies with the RNG but whose identity is the index.

    The label is derived from *index* alone so that, after a round trip, a
    mismatch identifies the exact record. The scalar choices (bool, exact binary
    fractions, ASCII-escapable unicode) all survive ``json`` unchanged, so
    equality after re-reading is a real field-for-field check.
    """
    return {
        "seq": index,
        "label": f"rec-{index:04d}",
        "flag": rng.choice([True, False]),
        "count": rng.randint(-1000, 1000),
        "ratio": rng.choice([0.0, 0.5, 1.5, -2.25]),
        "note": rng.choice(["ok", "", "unicode-cafe-ü", "line\twith\ttabs"]),
        "tags": [rng.randint(0, 9) for _ in range(rng.randint(0, 4))],
        "nested": {"depth": rng.randint(0, 3), "kind": rng.choice(["a", "b", "c"])},
        "maybe": rng.choice([None, 1, "two"]),
    }


def _corrupt_body(rng: random.Random, index: int) -> tuple[bytes, list[dict[str, Any]], int]:
    """One fuzzed body plus the reader's contracted reading of it.

    Returns the raw bytes to append after the header, the payloads that MUST be
    delivered in order, and the number of diagnostics the reader MUST emit.
    Every category is a real on-disk state: a valid record, an empty or
    duplicated newline, non-JSON garbage, a valid JSON scalar/array (right
    syntax, wrong shape), a forged structural marker, and non-UTF8 bytes. Each
    line is newline-terminated, so the whole body is committed — the truncated
    tail is a separate property. Encoding the expected outcome per category is
    what lets the property assert the *documented* result, not merely "no crash".
    """
    parts: list[bytes] = []
    expected_records: list[dict[str, Any]] = []
    expected_diagnostics = 0
    for line_index in range(rng.randint(3, 12)):
        kind = rng.choice(["record", "record", "blank", "garbage", "scalar", "reserved", "nonutf8"])
        if kind == "record":
            payload = {"seq": index * 100 + line_index, "label": f"r-{index}-{line_index}"}
            parts.append(encode_json_line(payload).encode("utf-8"))
            expected_records.append(payload)
        elif kind == "blank":
            parts.append(rng.choice([b"\n", b"   \n", b"\t\n", b"\n\n"]))
        elif kind == "garbage":
            parts.append(rng.choice([b"{not json}\n", b"]]nope\n", b"<html>\n", b"{,,}\n", b"x\n"]))
            expected_diagnostics += 1
        elif kind == "scalar":
            parts.append(rng.choice([b"42\n", b'"a string"\n', b"[1,2,3]\n", b"true\n", b"null\n"]))
            expected_diagnostics += 1
        elif kind == "reserved":
            # A forged rotation/header marker in the *data* stream must be
            # dropped silently: rotation is keyed off file headers, not off a
            # line a producer can write, so this never triggers replanning.
            marker = {"type": rng.choice([HEADER_TYPE, ROTATED_TYPE]), "serial": line_index}
            parts.append(encode_json_line(marker).encode("utf-8"))
        else:  # nonutf8
            parts.append(rng.choice(_NON_UTF8) + b"\n")
            expected_diagnostics += 1
    return b"".join(parts), expected_records, expected_diagnostics


def _random_line(rng: random.Random) -> bytes:
    """A short run of arbitrary bytes.

    Capped at 200 bytes on purpose. Deep nesting and absurd integers each have
    their own regression test below (both now skipped as corrupt records rather
    than crashing); this broad property keeps its lines short so it tests
    termination and the no-bare-exception promise across arbitrary bytes rather
    than re-deriving those two edges.
    """
    length = rng.randint(0, 200)
    return bytes(rng.randint(0, 255) for _ in range(length))


# ---------------------------------------------------------------------------
# Round trip: what the real writer wrote is what the reader hands back.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count", [1, 2, 5, 20, 200])
def test_a_valid_corpus_reads_back_field_for_field(tmp_path: Path, count: int) -> None:
    """A corpus the real writer produced round-trips exactly and in order.

    This is the control the corruption properties lean on: without it, a reader
    that dropped or reordered even healthy records could still pass every
    "did not crash" assertion below.
    """
    rng = random.Random(_SEED + count)
    layout = make_layout(tmp_path)
    writer = JournalWriter(layout, layout.command_ack)
    expected = [_varied_payload(rng, index) for index in range(count)]
    for payload in expected:
        writer.append(payload)
    writer.close()

    read = JournalReader(layout, layout.command_ack).read()

    assert [record.payload for record in read.records] == expected
    assert read.diagnostics == ()
    assert not read.rotated
    assert read.offset == writer.size
    assert read.pending_bytes == 0


# ---------------------------------------------------------------------------
# Corruption: the documented outcome, never a bare exception.
# ---------------------------------------------------------------------------


def test_mixed_corruption_yields_the_documented_outcome(tmp_path: Path) -> None:
    """PROPERTY. Interleaved valid/corrupt/empty/reserved lines read as promised.

    Every valid record is delivered in order, every corrupt line becomes exactly
    one diagnostic and is skipped, every reserved marker is dropped silently, and
    the offset lands on the file's end — so nothing after a bad line is held
    hostage.

    Defect classes this catches: (a) a bare ``JSONDecodeError`` /
    ``UnicodeDecodeError`` escaping to the caller instead of a diagnostic;
    (b) a corrupt line stalling the reader so later records are never delivered;
    (c) a reserved structural marker leaking to the consumer as a record.
    """
    rng = random.Random(_SEED)
    for case in range(200):
        body, expected_records, expected_diagnostics = _corrupt_body(rng, case)
        layout, path = _lay_down(tmp_path / f"mix-{case}", body)

        read = JournalReader(layout, path).read()

        assert [record.payload for record in read.records] == expected_records
        assert len(read.diagnostics) == expected_diagnostics
        for record in read.records:
            assert isinstance(record.payload, dict)
            assert record.payload.get("type") not in RESERVED_TYPES
        assert read.offset == path.stat().st_size
        assert read.pending_bytes == 0


def test_an_oversized_complete_line_is_reported_and_skipped(tmp_path: Path) -> None:
    """A committed line past MAX_LINE_BYTES is a producer that wedged mid-record.

    The reader must skip it with a diagnostic and keep the records on either
    side, never parse the whole thing in as a record.

    Defect class: an oversized line delivered as data, or one that swallows the
    records that follow it.
    """
    before = {"seq": 0, "label": "before"}
    after = {"seq": 1, "label": "after"}
    oversized = b"x" * (MAX_LINE_BYTES + 64) + b"\n"
    body = (
        encode_json_line(before).encode("utf-8")
        + oversized
        + encode_json_line(after).encode("utf-8")
    )
    layout, path = _lay_down(tmp_path, body)

    read = JournalReader(layout, path).read()

    assert [record.payload for record in read.records] == [before, after]
    assert any("exceeds" in diagnostic.detail for diagnostic in read.diagnostics)


def test_non_utf8_bytes_are_reported_not_raised(tmp_path: Path) -> None:
    """Non-UTF8 bytes between two valid records are a diagnostic, never a crash.

    Defect class: a ``UnicodeDecodeError`` on undecodable bytes reaching the
    caller instead of being caught and reported.
    """
    before = {"seq": 0, "label": "before"}
    after = {"seq": 1, "label": "after"}
    body = (
        encode_json_line(before).encode("utf-8")
        + b"\xc3\x28 not utf8\n"
        + encode_json_line(after).encode("utf-8")
    )
    layout, path = _lay_down(tmp_path, body)

    read = JournalReader(layout, path).read()

    assert [record.payload for record in read.records] == [before, after]
    assert any("corrupt record" in diagnostic.detail for diagnostic in read.diagnostics)


def test_a_truncated_final_line_is_withheld_then_completes(tmp_path: Path) -> None:
    """PROPERTY. Wherever the write was cut, the torn tail is withheld then healed.

    For an arbitrary sever point before the final record's newline, the reader
    delivers only the complete records and leaves the offset before the tail
    (§3.5); once the rest of the line and its newline land, the record reads
    back whole — never half parsed, never advanced past uncommitted bytes.

    Defect class: a torn final record delivered as a partial/corrupt record, or
    the reader's offset moving past bytes that were never committed.
    """
    rng = random.Random(_SEED + 7)
    for case in range(50):
        complete = [{"seq": i, "label": f"c-{case}-{i}"} for i in range(rng.randint(1, 4))]
        body = b"".join(encode_json_line(payload).encode("utf-8") for payload in complete)
        final = {"seq": 99, "label": f"tail-{case}"}
        final_line = encode_json_line(final).encode("utf-8")
        # Sever before the newline: the tail is genuinely uncommitted.
        cut = rng.randint(1, len(final_line) - 1)
        layout, path = _lay_down(tmp_path / f"trunc-{case}", body + final_line[:cut])

        reader = JournalReader(layout, path)
        first = reader.read()
        assert [record.payload for record in first.records] == complete
        assert first.pending_bytes > 0
        offset_before = reader.offset

        with path.open("ab") as handle:
            handle.write(final_line[cut:])
        second = reader.read()

        assert [record.payload for record in second.records] == [final]
        assert reader.offset > offset_before


# ---------------------------------------------------------------------------
# Termination and boundedness: garbage in, a bounded answer out.
# ---------------------------------------------------------------------------


def test_arbitrary_bytes_never_raise_and_leave_a_consistent_position(tmp_path: Path) -> None:
    """PROPERTY. Over random binary bodies the reader gives a bounded answer.

    ``read()`` returns a poll rather than an exception, its offset stays within
    ``[0, filesize]``, every delivered record is a non-reserved dict, and a
    second read with nothing appended returns nothing new.

    Defect classes this catches: any unhandled exception reaching the caller;
    an offset that runs off the end of the file; a bogus re-delivery of records
    already consumed.
    """
    rng = random.Random(_SEED + 11)
    for case in range(300):
        lines = [_random_line(rng) for _ in range(rng.randint(0, 10))]
        body = b"\n".join(lines) + (b"\n" if rng.random() < 0.7 else b"")
        layout, path = _lay_down(tmp_path / f"rand-{case}", body)

        reader = JournalReader(layout, path)
        read = reader.read()

        size = path.stat().st_size
        assert 0 <= read.offset <= size
        for record in read.records:
            assert isinstance(record.payload, dict)
            assert record.payload.get("type") not in RESERVED_TYPES
        # Nothing new was written, so a re-poll must not re-deliver or advance.
        assert reader.read().records == ()


def test_a_huge_unterminated_body_drains_in_bounded_polls(tmp_path: Path) -> None:
    """A never-terminated line larger than one read must not hang or hoard memory.

    A corrupt producer can leave a run of bytes with no newline that is larger
    than any legitimate record and larger than one ``read`` syscall. Each poll
    consumes at most ``MAX_READ_BYTES`` and reports the oversized unterminated
    run, so the file drains in a bounded number of polls and the reader never
    pulls the whole thing into memory at once.

    Defect class: a corrupt length / an endless unterminated line making the
    reader loop forever or allocate unboundedly.
    """
    body = b"x" * (3 * MAX_READ_BYTES + 500)  # no newline anywhere in the run
    layout, path = _lay_down(tmp_path, body)
    reader = JournalReader(layout, path)

    max_polls = 3 * MAX_READ_BYTES // MAX_LINE_BYTES + 5
    saw_oversized = False
    last_offset = -1
    polls = 0
    while polls < max_polls:
        read = reader.read()
        if any("unterminated" in diagnostic.detail for diagnostic in read.diagnostics):
            saw_oversized = True
        if reader.offset == last_offset:
            break
        last_offset = reader.offset
        polls += 1

    assert saw_oversized
    # The reader consumed everything except the sub-cap tail it is right to hold.
    assert reader.offset >= path.stat().st_size - MAX_LINE_BYTES
    assert polls < max_polls


def test_reading_a_fuzzed_file_is_deterministic(tmp_path: Path) -> None:
    """Two fresh readers over the same bytes must agree exactly.

    Defect class: hidden nondeterminism (dict/set iteration order, ambient
    state) in the parse, which would make a diagnostic or a record order depend
    on something other than the file.
    """
    body, _, _ = _corrupt_body(random.Random(_SEED + 3), 123)
    layout, path = _lay_down(tmp_path, body)

    first = JournalReader(layout, path).read()
    second = JournalReader(layout, path).read()

    assert [record.payload for record in first.records] == [
        record.payload for record in second.records
    ]
    assert [diagnostic.detail for diagnostic in first.diagnostics] == [
        diagnostic.detail for diagnostic in second.diagnostics
    ]
    assert first.offset == second.offset


# ---------------------------------------------------------------------------
# The two escapes the fuzz found, now fixed in journal.py and pinned here as
# regression tests with their exact, minimal reproductions.
# ---------------------------------------------------------------------------


def test_deeply_nested_json_is_skipped_not_a_recursion_error(tmp_path: Path) -> None:
    """The escape closed: deep nesting is a skipped record, not a crash.

    A complete line nested a thousand deep — a few kilobytes, under
    ``MAX_LINE_BYTES`` — used to make ``json.loads`` raise a ``RecursionError``
    that ``_parse_lines`` did not catch, so it propagated out of ``read()``
    where §3.5 promises a 'corrupt record' skip. Depth is now measured on the
    raw bytes before parsing (``MAX_RECORD_DEPTH``), so the line is skipped with
    a diagnostic and reading continues.
    """
    line = b"[" * 1000 + b"]" * 1000 + b"\n"
    layout, path = _lay_down(tmp_path, line)
    read = JournalReader(layout, path).read()
    assert read.records == ()
    assert any("corrupt record" in d.detail for d in read.diagnostics)


def test_an_oversized_integer_literal_is_skipped_not_a_value_error(tmp_path: Path) -> None:
    """The second escape closed: an absurd integer is a skipped record.

    A complete line that is a JSON integer of 4301 digits tripped CPython's
    integer-string-conversion ceiling with a plain ``ValueError`` — not the
    ``JSONDecodeError`` the guard caught — so ``read()`` propagated it raw. The
    guard now catches the broader ``ValueError`` and records the skip.
    """
    line = b"9" * 4301 + b"\n"
    layout, path = _lay_down(tmp_path, line)
    read = JournalReader(layout, path).read()
    assert read.records == ()
    assert any("corrupt record" in d.detail for d in read.diagnostics)
