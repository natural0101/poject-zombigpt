"""Whole-file writes: the scratch file, the guard, and what a failure leaves.

§3.5 requires the temporary file to carry the writer's pid and a serial. That
is not decoration: a shared scratch name lets one writer publish another
writer's half-written bytes through ``os.replace``, which is precisely the tear
the atomic write exists to prevent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pz_agent_core.ipc import atomic
from pz_agent_core.ipc.atomic import (
    MAX_DOCUMENT_BYTES,
    DocumentError,
    IpcPathError,
    read_json_document,
    write_json_atomic,
)
from tests.fixtures.ipc_builders import make_layout


def _scratch_files(root: Path) -> list[Path]:
    return sorted(path for path in root.iterdir() if ".tmp" in path.name)


def test_a_document_round_trips_and_leaves_no_scratch_file(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    written = write_json_atomic(layout, layout.session, {"session_id": "s", "n": 1})

    assert read_json_document(layout.session) == {"session_id": "s", "n": 1}
    assert written == layout.session.stat().st_size
    assert _scratch_files(layout.root) == []


def test_successive_writes_do_not_share_a_scratch_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = make_layout(tmp_path)
    names: list[str] = []
    original = atomic.temp_name

    def spy(name: str, pid: int, seq: int) -> str:
        chosen = original(name, pid, seq)
        names.append(chosen)
        return chosen

    monkeypatch.setattr(atomic, "temp_name", spy)
    write_json_atomic(layout, layout.session, {"n": 1})
    write_json_atomic(layout, layout.session, {"n": 2})

    assert len(set(names)) == 2
    assert all(name.startswith("session.json.tmp.") for name in names)
    assert read_json_document(layout.session) == {"n": 2}


def test_a_failed_write_takes_its_scratch_file_with_it(tmp_path: Path) -> None:
    """Unique names are only bounded if the failures clean up after themselves."""
    layout = make_layout(tmp_path)
    layout.session.mkdir()  # the target cannot be replaced by a file

    with pytest.raises(OSError):
        write_json_atomic(layout, layout.session, {"n": 1})
    assert _scratch_files(layout.root) == []


def test_an_unmanaged_target_is_refused(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    with pytest.raises(IpcPathError):
        write_json_atomic(layout, layout.root / "anything.json", {"n": 1})
    assert not (layout.root / "anything.json").exists()


def test_an_oversized_document_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    with pytest.raises(DocumentError, match="exceeds"):
        write_json_atomic(layout, layout.session, {"blob": "x" * (MAX_DOCUMENT_BYTES + 1)})
    assert not layout.session.exists()
    assert _scratch_files(layout.root) == []


def test_a_truncated_document_is_not_half_accepted(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.session.write_text('{"session_id": "s", "n": ', encoding="utf-8")
    with pytest.raises(DocumentError, match="malformed JSON"):
        read_json_document(layout.session)


def test_a_json_array_is_not_a_document(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.session.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(DocumentError, match="expected a JSON object"):
        read_json_document(layout.session)


def test_a_missing_document_is_an_error_not_an_empty_one(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    with pytest.raises(DocumentError):
        read_json_document(layout.capabilities)
