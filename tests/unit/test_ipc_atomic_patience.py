"""The atomic publish survives a Windows reader holding the target, briefly.

On POSIX ``os.replace`` succeeds under any open handle, so the seam cannot be
driven by a real reader here. On Windows it cannot be *avoided*: the mod reads
every published document, antivirus and indexer sweeps open every fresh file,
and ``os.replace`` onto a file held open without delete sharing raises
``PermissionError`` (WinError 32) — which is exactly what the 2026-08-08 live
session on Build 42.20.2 produced mid-publish. The writer now retries the
replace with a bounded budget, so a reader mid-poll is a brief wait; a reader
that never lets go is the module's own :class:`SharingViolationError`, not a
bare crash. The seam is pinned directly by making ``os.replace`` speak the
Windows refusal for a controlled number of attempts.

Whatever the failure, the scratch file must not be leaked silently: every
failed publish removes it with the same patience, and when even removal is
refused, the raised error names the leaked path so someone can find it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pz_agent_core.ipc import atomic
from pz_agent_core.ipc.atomic import (
    DocumentError,
    SharingViolationError,
    read_json_document,
    write_json_atomic,
)
from tests.fixtures.ipc_builders import make_layout

DOCUMENT = {"session_id": "s", "n": 1, "надпись": "буквы"}


def _scratch_files(root: Path) -> list[Path]:
    return sorted(path for path in root.iterdir() if ".tmp" in path.name)


def _windows_refusal() -> PermissionError:
    """The WinError-32 shape the live session produced: errno + message."""
    return PermissionError(32, "The process cannot access the file")


class TestAReaderMidPollIsWaitedOut:
    def test_three_refusals_then_success_publishes_the_document(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = make_layout(tmp_path)
        real_replace = os.replace
        refusals = {"left": 3}

        def windows_shaped(source: str | Path, target: str | Path) -> None:
            if refusals["left"] > 0:
                refusals["left"] -= 1
                raise _windows_refusal()
            real_replace(source, target)

        monkeypatch.setattr(os, "replace", windows_shaped)
        monkeypatch.setattr(atomic, "_REPLACE_PAUSE_SECONDS", 0.001)

        written = write_json_atomic(layout, layout.session, DOCUMENT)

        assert refusals["left"] == 0, "the Windows refusals were never consumed"
        assert read_json_document(layout.session) == DOCUMENT
        assert written == layout.session.stat().st_size
        assert _scratch_files(layout.root) == [], "the waited-out publish leaked its scratch file"


class TestAReaderThatNeverLetsGoIsANamedError:
    def test_the_error_names_the_target_and_the_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = make_layout(tmp_path)
        attempts = {"n": 0}

        def always_held(source: str | Path, target: str | Path) -> None:
            attempts["n"] += 1
            raise _windows_refusal()

        monkeypatch.setattr(os, "replace", always_held)
        monkeypatch.setattr(atomic, "_REPLACE_PAUSE_SECONDS", 0.001)

        with pytest.raises(SharingViolationError, match="held it open past the") as caught:
            write_json_atomic(layout, layout.session, DOCUMENT)

        assert "session.json" in str(caught.value), "the refusal must name the target"
        assert attempts["n"] == atomic._REPLACE_ATTEMPTS, "the retry budget was not honoured"
        # The failure took its scratch file with it, and the target — which a
        # reader may be depending on — was never created half-way.
        assert _scratch_files(layout.root) == []
        assert not layout.session.exists()

    def test_the_refusal_is_still_an_oserror_for_existing_callers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The write path has always raised ``OSError``; the type keeps that.

        A caller that guarded a publish with ``except OSError`` before this
        module learnt patience must keep catching the exhausted-budget refusal.
        """
        layout = make_layout(tmp_path)

        def always_held(source: str | Path, target: str | Path) -> None:
            raise _windows_refusal()

        monkeypatch.setattr(os, "replace", always_held)
        monkeypatch.setattr(atomic, "_REPLACE_PAUSE_SECONDS", 0.001)

        with pytest.raises(OSError):
            write_json_atomic(layout, layout.session, DOCUMENT)


class TestEveryFailurePathTakesTheScratchFileWithIt:
    def test_an_unserialisable_document_is_a_typed_refusal_with_no_scratch_file(
        self, tmp_path: Path
    ) -> None:
        layout = make_layout(tmp_path)

        with pytest.raises(DocumentError, match="cannot be serialised"):
            write_json_atomic(layout, layout.session, {"handle": object()})

        assert _scratch_files(layout.root) == []
        assert not layout.session.exists()

    def test_a_circular_document_is_the_same_typed_refusal(self, tmp_path: Path) -> None:
        layout = make_layout(tmp_path)
        loop: dict[str, object] = {}
        loop["self"] = loop

        with pytest.raises(DocumentError, match="cannot be serialised"):
            write_json_atomic(layout, layout.session, loop)

        assert _scratch_files(layout.root) == []

    def test_a_failed_fsync_removes_the_scratch_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = make_layout(tmp_path)

        def broken_fsync(fd: int) -> None:
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(os, "fsync", broken_fsync)

        with pytest.raises(OSError, match="Input/output error"):
            write_json_atomic(layout, layout.session, DOCUMENT)

        assert _scratch_files(layout.root) == []
        assert not layout.session.exists()


class TestABlockedRemovalNamesTheLeakedPath:
    def test_the_error_names_the_scratch_file_that_stayed_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When cleanup itself is refused, the module says what it leaked.

        The one thing worse than a leaked scratch file is a leaked scratch file
        nobody knows about: the raised error must carry the path, and the
        refusal that caused the cleanup must stay reachable on the chain.
        """
        layout = make_layout(tmp_path)

        def always_held(source: str | Path, target: str | Path) -> None:
            raise _windows_refusal()

        def never_removed(self: Path, missing_ok: bool = False) -> None:
            raise _windows_refusal()

        monkeypatch.setattr(os, "replace", always_held)
        monkeypatch.setattr(Path, "unlink", never_removed)
        monkeypatch.setattr(atomic, "_REPLACE_PAUSE_SECONDS", 0.001)

        with pytest.raises(SharingViolationError, match="left behind") as caught:
            write_json_atomic(layout, layout.session, DOCUMENT)

        leaked = _scratch_files(layout.root)
        assert len(leaked) == 1, "the blocked removal must actually have left the file"
        assert str(leaked[0]) in str(caught.value), "the error must name the leaked scratch path"
        # The replace refusal that started the cleanup is on the chain, so the
        # leak does not silently replace the reason the publish failed. It
        # sits behind the unlink's own PermissionError, hence the walk.
        chain: list[BaseException] = []
        cursor: BaseException | None = caught.value.__context__
        while cursor is not None and len(chain) < 8:
            chain.append(cursor)
            cursor = cursor.__context__
        assert any(
            isinstance(err, SharingViolationError) and "held it open past the" in str(err)
            for err in chain
        ), f"the original replace refusal fell off the chain: {chain!r}"


class TestAWriterMidPublishIsWaitedOutByTheReader:
    """The read side of the same contention, with its own smaller budget.

    On Windows the game holds ``observation.snapshot.pointer`` (and the
    heartbeat files) open across its own truncate-in-place writes, and a poll
    landing inside that hold draws ``PermissionError`` from the open — which
    used to be folded into :class:`DocumentError`, turning a locked file into
    apparent corruption. The read now retries the refusal within a budget kept
    deliberately far under the write side's: the caller is a ~125ms tick loop,
    and 40ms worst case stays inside the tick where half a second would eat
    four of them.
    """

    def test_two_refusals_then_success_returns_the_document(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = make_layout(tmp_path)
        write_json_atomic(layout, layout.session, DOCUMENT)
        real_read_text = Path.read_text
        refusals = {"left": 2}

        def held_then_released(self: Path, *args: object, **kwargs: object) -> str:
            if self == layout.session and refusals["left"] > 0:
                refusals["left"] -= 1
                raise _windows_refusal()
            return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", held_then_released)
        monkeypatch.setattr(atomic, "_READ_PAUSE_SECONDS", 0.001)

        assert read_json_document(layout.session) == DOCUMENT
        assert refusals["left"] == 0, "the Windows refusals were never consumed"

    def test_a_refused_stat_is_retried_the_same_way(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hold can land on the stat before the open; same patience."""
        layout = make_layout(tmp_path)
        write_json_atomic(layout, layout.session, DOCUMENT)
        real_stat = Path.stat
        refusals = {"left": 2}

        def held_then_released(self: Path, **kwargs: object) -> os.stat_result:
            if self == layout.session and refusals["left"] > 0:
                refusals["left"] -= 1
                raise _windows_refusal()
            return real_stat(self, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "stat", held_then_released)
        monkeypatch.setattr(atomic, "_READ_PAUSE_SECONDS", 0.001)

        assert read_json_document(layout.session) == DOCUMENT
        assert refusals["left"] == 0


class TestAWriterThatNeverLetsGoIsTheTypedContentionError:
    def test_persistent_refusal_is_a_sharing_violation_not_a_document_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Contention must stay distinguishable from corruption.

        A caller shown :class:`DocumentError` treats the file as corrupt and
        degrades to a miss silently; :class:`SharingViolationError` says the
        document is merely locked, so the caller can say so and poll again.
        """
        layout = make_layout(tmp_path)
        write_json_atomic(layout, layout.session, DOCUMENT)
        attempts = {"n": 0}

        def always_held(self: Path, *args: object, **kwargs: object) -> str:
            attempts["n"] += 1
            raise _windows_refusal()

        monkeypatch.setattr(Path, "read_text", always_held)
        monkeypatch.setattr(atomic, "_READ_PAUSE_SECONDS", 0.001)

        with pytest.raises(SharingViolationError, match="held it open past the") as caught:
            read_json_document(layout.session)

        assert "session.json" in str(caught.value), "the refusal must name the file"
        assert attempts["n"] == atomic._READ_ATTEMPTS, "the read budget was not honoured"
        assert not isinstance(caught.value, DocumentError)

    def test_the_read_budget_is_smaller_than_the_write_budget(self) -> None:
        """The reader lives in a ~125ms tick loop; the writer does not.

        Pinned as an inequality so neither budget can silently grow past the
        other: the worst-case read wait must stay under one tick, while the
        write side may spend half a second because a publish happens off the
        poll path.
        """
        read_budget = atomic._READ_ATTEMPTS * atomic._READ_PAUSE_SECONDS
        write_budget = atomic._REPLACE_ATTEMPTS * atomic._REPLACE_PAUSE_SECONDS
        assert read_budget < write_budget
        assert read_budget <= 0.125, "the read budget must fit inside one tick"

    def test_the_refusal_is_still_an_oserror_for_existing_callers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = make_layout(tmp_path)
        write_json_atomic(layout, layout.session, DOCUMENT)

        def always_held(self: Path, *args: object, **kwargs: object) -> str:
            raise _windows_refusal()

        monkeypatch.setattr(Path, "read_text", always_held)
        monkeypatch.setattr(atomic, "_READ_PAUSE_SECONDS", 0.001)

        with pytest.raises(OSError):
            read_json_document(layout.session)


class TestGenuineDocumentFailuresAreUntouchedByThePatience:
    def test_a_missing_file_is_still_a_document_error_with_no_retry_wait(
        self, tmp_path: Path
    ) -> None:
        """Absence is not contention: it must fail fast, first attempt."""
        layout = make_layout(tmp_path)

        with pytest.raises(DocumentError):
            read_json_document(layout.session)

    def test_a_torn_file_is_still_a_document_error(self, tmp_path: Path) -> None:
        layout = make_layout(tmp_path)
        layout.session.write_text('{"seq": 1, "trunc', encoding="utf-8")

        with pytest.raises(DocumentError, match="malformed JSON"):
            read_json_document(layout.session)

    def test_a_non_permission_oserror_is_a_document_error_on_the_first_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Disk trouble is not a hold that resolves itself; no retries."""
        layout = make_layout(tmp_path)
        write_json_atomic(layout, layout.session, DOCUMENT)
        attempts = {"n": 0}

        def broken_disk(self: Path, *args: object, **kwargs: object) -> str:
            attempts["n"] += 1
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(Path, "read_text", broken_disk)

        with pytest.raises(DocumentError, match="Input/output error"):
            read_json_document(layout.session)

        assert attempts["n"] == 1, "a non-contention failure must not be retried"


class TestTheHappyPathIsByteIdentical:
    def test_bytes_on_disk_match_the_canonical_encoding(self, tmp_path: Path) -> None:
        """The patience must change failure behaviour only, never the bytes.

        The expected bytes are spelled out here exactly as the writer has
        always produced them — compact separators, sorted keys, ASCII escapes,
        UTF-8 — so a regression in the encoding shows up as a byte diff, not as
        a subtly different but still parseable document.
        """
        layout = make_layout(tmp_path)

        written = write_json_atomic(layout, layout.session, DOCUMENT)

        expected = json.dumps(
            DOCUMENT, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        assert layout.session.read_bytes() == expected
        assert written == len(expected)
        assert _scratch_files(layout.root) == []
