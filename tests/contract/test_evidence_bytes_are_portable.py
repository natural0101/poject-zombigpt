"""Evidence digests describe the bytes on disk, on every operating system.

This is the defect that made the Windows release gate unreachable. Artefacts
were serialised to a string, hashed as that string, and written with
``Path.write_text`` — which opens in **text mode**, so on Windows every ``\\n``
became ``\\r\\n`` on the way out. The digest therefore described bytes nobody had
written, and every untouched ``result.json`` read back as tampered:

    TamperError: content hash 1dc789a5… does not match the recorded 0c6f7cc9…
    This file was modified after it was written; an edited result is evidence of
    nothing. Re-run the scenario.

``finalize`` refuses on a digest mismatch, by design and correctly, so the gate
could not pass on the one operating system the release is for. Ten of the
twenty-four Windows failures were this, directly or downstream.

**These tests fail on Linux too if the fix is reverted**, which is the property
that matters — a regression test that only fires on the platform nobody develops
on is a regression test that fires after the release. Two mechanisms do that:

* the returned digest is compared against the file, so hashing anything other
  than what was written is caught wherever it runs;
* the writer is handed a document and the file is read back as **bytes** and
  compared to :func:`canonical_json_bytes`, so a text-mode write is caught by
  the CRLF appearing in the comparison rather than by the platform.

The third mechanism is direct: a file deliberately written with CRLF *must* be
rejected, because that is a real edit, and the fix must not have been to make
the verifier tolerant of line endings. Tolerating them would have turned a
correct tamper alarm into one that ignores a class of edits.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest

from pz_agent_cli.livetest import evidence
from pz_agent_cli.livetest.evidence import (
    TamperError,
    canonical_json_bytes,
    read_document,
    sha256_bytes,
    sha256_file,
    write_bytes_atomically,
    write_document,
)

#: Cyrillic, an emoji and a nested structure: ``ensure_ascii=False`` means these
#: reach the file as UTF-8 rather than as escapes, so they are exactly where an
#: encoding difference between platforms would show up.
DOCUMENT: Final[dict[str, Any]] = {
    "scenario_id": "S07_NESTED_INVENTORY",
    "detail": "тестовый сейв — «Мулдро», 42.20 ✔",
    "nested": {"b": [1, 2, 3], "a": {"ключ": "значение"}},
    "float": 1.5,
    "null": None,
}


def test_the_canonical_form_is_bytes_and_ends_in_one_newline() -> None:
    """The exact contract, pinned. Everything else here rests on it."""
    data = canonical_json_bytes(DOCUMENT)

    assert isinstance(data, bytes)
    assert data.endswith(b"\n")
    assert b"\r\n" not in data, "the canonical form must never carry a carriage return"
    assert data == (
        json.dumps(DOCUMENT, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    )


def test_the_canonical_form_is_stable_whatever_the_key_order_was() -> None:
    """Two dictionaries with the same content hash the same. That is the point."""
    reordered = dict(reversed(list(DOCUMENT.items())))

    assert canonical_json_bytes(reordered) == canonical_json_bytes(DOCUMENT)


def test_cyrillic_and_emoji_survive_as_utf8_rather_than_escapes() -> None:
    """A save named in Russian must not change the digest by being re-encoded."""
    data = canonical_json_bytes(DOCUMENT)

    assert "«Мулдро»".encode() in data
    assert b"\\u" not in data, "ensure_ascii=False means no escapes; escapes would be a re-encoding"


def test_the_returned_digest_is_the_digest_of_the_file(tmp_path: Path) -> None:
    """The load-bearing assertion, and the one that fires on any platform.

    ``write_document`` returns what it claims the file hashes to. If it hashes
    a buffer instead, this fails wherever the platform rewrites a byte — and it
    fails on Linux too the moment the returned value stops being read back.
    """
    path = tmp_path / "result.json"

    digest = write_document(path, DOCUMENT, schema=None)

    on_disk, size = sha256_file(path)
    assert digest.sha256 == on_disk
    assert digest.size_bytes == size
    assert digest.size_bytes == len(canonical_json_bytes(DOCUMENT))


def test_a_write_that_changed_the_bytes_is_caught_rather_than_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read-back guard, exercised where a platform cannot exercise it.

    On Linux the buffer and the file are always identical, so removing the
    read-back changes nothing here — which is precisely why the original defect
    lived only on Windows and reached a release candidate. A writer that alters
    the bytes stands in for the platform, so the guard is tested on every OS
    rather than on the one that happened to break.
    """

    def translating(path: Path, data: bytes) -> None:
        path.write_bytes(data.replace(b"\n", b"\r\n"))

    monkeypatch.setattr(evidence, "write_bytes_atomically", translating)

    with pytest.raises(evidence.LiveTestError, match="not the bytes that were written"):
        evidence.write_document(tmp_path / "result.json", DOCUMENT, schema=None)


def test_the_file_holds_exactly_the_canonical_bytes(tmp_path: Path) -> None:
    """Read back as bytes, so a text-mode write is visible here and not on CI."""
    path = tmp_path / "result.json"

    write_document(path, DOCUMENT, schema=None)

    assert path.read_bytes() == canonical_json_bytes(DOCUMENT)
    assert b"\r\n" not in path.read_bytes(), "written in text mode: the newlines were translated"


def test_a_document_written_then_read_verifies_against_its_own_digest(tmp_path: Path) -> None:
    """The sequence a scenario performs, and the one that was failing."""
    path = tmp_path / "result.json"
    digest = write_document(path, DOCUMENT, schema=None)

    read_back = read_document(path, expected_sha256=digest.sha256)

    assert read_back == DOCUMENT


def test_a_file_edited_to_crlf_is_still_refused(tmp_path: Path) -> None:
    """The fix must not have been to make the verifier tolerant of line endings.

    Rewriting every newline is an edit. A verifier that shrugged at it would
    accept a class of modification silently, which is a worse defect than the
    one being fixed — so this asserts the alarm still fires.
    """
    path = tmp_path / "result.json"
    digest = write_document(path, DOCUMENT, schema=None)
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))

    with pytest.raises(TamperError):
        read_document(path, expected_sha256=digest.sha256)


def test_a_document_edited_in_place_is_refused(tmp_path: Path) -> None:
    """The case the digest exists for: a FAIL edited into a PASS."""
    path = tmp_path / "result.json"
    digest = write_document(path, DOCUMENT, schema=None)
    path.write_bytes(path.read_bytes().replace(b"S07_NESTED_INVENTORY", b"S07_NESTED_INVENTORYX"))

    with pytest.raises(TamperError):
        read_document(path, expected_sha256=digest.sha256)


def test_writing_is_all_or_nothing(tmp_path: Path) -> None:
    """A half-written artefact is indistinguishable from a tampered one."""
    path = tmp_path / "result.json"
    write_bytes_atomically(path, b"first\n")

    write_bytes_atomically(path, canonical_json_bytes(DOCUMENT))

    assert path.read_bytes() == canonical_json_bytes(DOCUMENT)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["result.json"], (
        "a temporary file was left behind for finalize to find and hash"
    )


def test_the_digest_does_not_depend_on_how_the_document_was_built(tmp_path: Path) -> None:
    """Two runs on two machines must agree, which is what the manifest asserts.

    Built by parsing the canonical form rather than by copying the literal, so
    a round trip through JSON — which is what a second machine does — is what
    is being compared.
    """
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    write_document(first, DOCUMENT, schema=None)

    reparsed = json.loads(first.read_bytes().decode("utf-8"))
    write_document(second, reparsed, schema=None)

    assert first.read_bytes() == second.read_bytes()
    assert sha256_file(first)[0] == sha256_file(second)[0]
    assert sha256_bytes(first.read_bytes()) == sha256_file(second)[0]
