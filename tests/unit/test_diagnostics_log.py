"""Bounded logging: rotation, the file-count cap, oversize records, redaction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pz_agent_core.diagnostics.log import (
    MAX_EVENT_LEN,
    MAX_FIELDS,
    MIN_RECORD_BYTES,
    DiagnosticLog,
    DiagnosticsError,
    LogLevel,
    LogLimits,
    LogRecord,
    RotatingWriter,
    iso_from_ms,
    read_records,
)
from pz_agent_core.diagnostics.redaction import USER_DIR_PLACEHOLDER, build_redactor
from tests.fixtures.cli_worlds import StepClock
from tests.fixtures.platform_trees import CYRILLIC_USER

SMALL = LogLimits(max_bytes=512, keep=2, max_record_bytes=256)


def _log(tmp_path: Path, limits: LogLimits = SMALL) -> DiagnosticLog:
    home = tmp_path / "Users" / CYRILLIC_USER
    return DiagnosticLog(
        tmp_path / "logs",
        redactor=build_redactor(user_dir=home / "Zomboid", home_dir=home),
        clock=StepClock(step_ms=0),
        limits=limits,
    )


# ---------------------------------------------------------------------------
# limits
# ---------------------------------------------------------------------------


def test_limits_refuse_a_record_cap_larger_than_the_file_cap() -> None:
    with pytest.raises(DiagnosticsError, match="cannot exceed"):
        LogLimits(max_bytes=100, keep=1, max_record_bytes=200)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_bytes": 0}, "max_bytes must be positive"),
        ({"keep": 0}, "keep must be at least 1"),
        ({"max_record_bytes": 0}, "max_record_bytes must be positive"),
    ],
)
def test_limits_reject_non_positive_bounds(kwargs: dict[str, int], message: str) -> None:
    with pytest.raises(DiagnosticsError, match=message):
        LogLimits(**kwargs)


# ---------------------------------------------------------------------------
# rotation
# ---------------------------------------------------------------------------


def test_rotation_is_counted_and_the_file_count_stays_bounded(tmp_path: Path) -> None:
    writer = RotatingWriter(tmp_path / "trace.jsonl", SMALL)

    for index in range(200):
        writer.write(f"{index:0>200}")

    assert writer.rotations > 0
    # keep=2 means the current file plus two generations, and no more, ever.
    assert len(writer.files()) == 3
    assert not writer.rotated_path(SMALL.keep + 1).exists()
    assert writer.total_bytes() <= SMALL.max_total_bytes


def test_the_oldest_generation_is_the_one_that_is_dropped(tmp_path: Path) -> None:
    writer = RotatingWriter(tmp_path / "trace.jsonl", SMALL)
    writer.write("first" + "-" * 200)
    writer.rotate()
    writer.write("second" + "-" * 200)
    writer.rotate()
    writer.write("third" + "-" * 200)
    writer.rotate()

    assert writer.rotated_path(1).read_text(encoding="utf-8").startswith("third")
    assert writer.rotated_path(2).read_text(encoding="utf-8").startswith("second")
    assert not writer.rotated_path(3).exists()


def test_a_line_over_the_record_cap_is_refused_rather_than_cut(tmp_path: Path) -> None:
    writer = RotatingWriter(tmp_path / "trace.jsonl", SMALL)

    with pytest.raises(DiagnosticsError, match="exceeds max_record_bytes"):
        writer.write("x" * (SMALL.max_record_bytes + 1))

    assert writer.records_written == 0


def test_a_writer_resumes_the_size_of_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * 400 + "\n", encoding="utf-8")

    writer = RotatingWriter(path, SMALL)
    writer.write("y" * 200)

    assert writer.rotations == 1


def test_rotate_rejects_an_index_below_one(tmp_path: Path) -> None:
    writer = RotatingWriter(tmp_path / "trace.jsonl", SMALL)

    with pytest.raises(DiagnosticsError, match="rotation index"):
        writer.rotated_path(0)


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------


def test_both_streams_are_written_from_one_record(tmp_path: Path) -> None:
    log = _log(tmp_path)

    record = log.log(LogLevel.INFO, "action.dispatched", action="consume.eat")

    assert record is not None
    structured = json.loads(log.structured.path.read_text(encoding="utf-8").strip())
    assert structured["event"] == "action.dispatched"
    assert structured["fields"]["action"] == "consume.eat"
    assert structured["timestamp"] == iso_from_ms(record.timestamp_ms)
    assert "action.dispatched" in log.human.path.read_text(encoding="utf-8")


def test_a_path_field_is_redacted_before_it_reaches_either_file(tmp_path: Path) -> None:
    log = _log(tmp_path)
    home = tmp_path / "Users" / CYRILLIC_USER

    log.log(LogLevel.ERROR, "ipc.write_failed", path=str(home / "Zomboid" / "Lua" / "x"))

    for path in log.files():
        text = path.read_text(encoding="utf-8")
        assert CYRILLIC_USER not in text
        assert USER_DIR_PLACEHOLDER in text


def test_an_oversize_record_is_replaced_by_a_marker_naming_what_was_dropped(
    tmp_path: Path,
) -> None:
    log = _log(tmp_path)

    record = log.log(LogLevel.INFO, "observation.snapshot", blob="x" * 4000)

    assert record is not None
    assert record.event == "diagnostics.record_truncated"
    assert record.fields["original_event"] == "observation.snapshot"
    assert record.fields["limit_bytes"] == SMALL.max_record_bytes
    assert log.structured.records_written == 1


def test_fields_beyond_the_cap_are_counted_rather_than_silently_dropped(
    tmp_path: Path,
) -> None:
    log = _log(tmp_path, LogLimits(max_bytes=64 * 1024, keep=1, max_record_bytes=32 * 1024))

    record = log.log(LogLevel.INFO, "wide", **{f"k{i}": i for i in range(MAX_FIELDS + 7)})

    assert record is not None
    assert record.fields["fields_dropped"] == 7
    assert len(record.fields) == MAX_FIELDS + 1


def test_the_marker_fits_even_when_the_event_name_is_as_long_as_it_may_be(
    tmp_path: Path,
) -> None:
    """The oversize path must not itself overflow the cap it is enforcing.

    A marker naming an 80-character event took 294 bytes, so at the smallest
    legal cap the writer raised out of ``log()`` — the caller crashed for
    logging a large field, which is the failure the marker exists to avoid.
    """
    log = _log(tmp_path, LogLimits(max_bytes=4096, keep=1, max_record_bytes=MIN_RECORD_BYTES))

    record = log.log(LogLevel.INFO, "e" * MAX_EVENT_LEN, blob="x" * 4000)

    assert record is not None
    assert record.event == "diagnostics.record_truncated"
    assert str(record.fields["original_event"]).startswith("e")
    written = log.structured.path.read_bytes()
    assert len(written) <= MIN_RECORD_BYTES
    assert json.loads(written)["event"] == "diagnostics.record_truncated"


def test_a_record_cap_too_small_for_the_marker_is_refused_at_construction() -> None:
    with pytest.raises(DiagnosticsError, match="at least"):
        LogLimits(max_bytes=4096, keep=1, max_record_bytes=MIN_RECORD_BYTES - 1)


def test_a_record_below_the_threshold_is_counted_as_suppressed(tmp_path: Path) -> None:
    home = tmp_path / "Users" / CYRILLIC_USER
    log = DiagnosticLog(
        tmp_path / "logs",
        redactor=build_redactor(home_dir=home),
        clock=StepClock(step_ms=0),
        min_level=LogLevel.WARNING,
    )

    assert log.debug("noisy") is None
    assert log.warning("real") is not None
    assert log.suppressed == 1


def test_an_event_name_must_exist_and_stay_short() -> None:
    with pytest.raises(DiagnosticsError, match="must name an event"):
        LogRecord(timestamp_ms=0, level=LogLevel.INFO, event="", fields={})
    with pytest.raises(DiagnosticsError, match="longer than"):
        LogRecord(timestamp_ms=0, level=LogLevel.INFO, event="e" * 200, fields={})


def test_a_log_name_may_not_carry_a_path_separator(tmp_path: Path) -> None:
    with pytest.raises(DiagnosticsError, match="bare stem"):
        DiagnosticLog(tmp_path, name="logs/pz-agent")


def test_the_human_line_never_wraps_onto_a_second_line(tmp_path: Path) -> None:
    log = _log(tmp_path)

    log.log(LogLevel.WARNING, "queue.gap", detail="one\ntwo\nthree")

    body = log.human.path.read_text(encoding="utf-8")
    assert body.count("\n") == 1


# ---------------------------------------------------------------------------
# reading back
# ---------------------------------------------------------------------------


def test_reading_records_reports_a_corrupt_line_and_keeps_the_rest(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.info("first")
    with log.structured.path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    log.info("second")

    records, problems = read_records(log.structured.path)

    assert [record["event"] for record in records] == ["first", "second"]
    assert len(problems) == 1
    assert "not JSON" in problems[0]


def test_reading_a_missing_file_is_a_problem_not_an_exception(tmp_path: Path) -> None:
    records, problems = read_records(tmp_path / "absent.jsonl")

    assert records == ()
    assert "cannot read" in problems[0]


def test_a_read_window_that_starts_mid_record_states_the_cut_it_made(tmp_path: Path) -> None:
    """The fragment the window begins on is the reader's doing, not corruption."""
    log = _log(tmp_path, LogLimits(max_bytes=64 * 1024, keep=1, max_record_bytes=1024))
    for index in range(20):
        log.info("event", index=index, filler="y" * 100)

    records, problems = read_records(log.structured.path, max_bytes=900)

    assert records, "the whole window must not be discarded with the fragment"
    assert all(record["event"] == "event" for record in records)
    assert len(problems) == 1
    assert "were not read" in problems[0]
    assert "not JSON" not in problems[0]


def test_read_records_rejects_a_non_positive_limit(tmp_path: Path) -> None:
    with pytest.raises(DiagnosticsError, match="limit must be positive"):
        read_records(tmp_path / "x.jsonl", limit=0)
