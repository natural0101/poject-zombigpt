"""The evidence tree: canonical bytes, digests, schema validation, collection.

The digest is only worth anything if the same document always serialises to the
same bytes, so that is pinned first. Everything after it depends on it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pz_agent_cli.livetest.evidence import (
    GITKEEP_NAME,
    MAX_ARTEFACT_BYTES,
    DocumentInvalidError,
    EvidenceLayout,
    LiveTestError,
    SchemaUnavailableError,
    TamperError,
    canonical_json,
    collect_files,
    digest_entry,
    read_document,
    sha256_file,
    sha256_text,
    write_document,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_SOURCE = REPO_ROOT / "evidence" / "schema"
SCENARIO = "S07_NESTED_INVENTORY"


@pytest.fixture
def layout(tmp_path: Path) -> EvidenceLayout:
    built = EvidenceLayout(tmp_path / "evidence")
    built.ensure_tree([SCENARIO, "S04_MOVE"])
    for schema in SCHEMA_SOURCE.glob("*.json"):
        (built.schema_dir / schema.name).write_bytes(schema.read_bytes())
    return built


class TestCanonicalBytes:
    def test_key_order_does_not_change_the_digest(self) -> None:
        first = canonical_json({"b": 1, "a": 2})
        second = canonical_json({"a": 2, "b": 1})

        assert first == second
        assert sha256_text(first) == sha256_text(second)

    def test_a_document_read_back_reproduces_its_digest(self, tmp_path: Path) -> None:
        """Without this, every tamper check would be a coin flip on whitespace."""
        document = {"scenario": SCENARIO, "values": [1, 2, 3], "nested": {"x": None}}
        path = tmp_path / "doc.json"
        path.write_text(canonical_json(document), encoding="utf-8")

        digest, _ = sha256_file(path)

        assert digest == sha256_text(canonical_json(json.loads(path.read_text())))

    def test_non_ascii_survives_the_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "doc.json"
        path.write_text(canonical_json({"user": "Пользователь"}), encoding="utf-8")

        assert json.loads(path.read_text(encoding="utf-8"))["user"] == "Пользователь"


class TestHashing:
    def test_the_size_cap_is_enforced_while_reading(self, tmp_path: Path) -> None:
        path = tmp_path / "big.log"
        path.write_bytes(b"x" * 4096)

        with pytest.raises(LiveTestError, match="byte artefact cap"):
            sha256_file(path, limit=1024)

    def test_an_unreadable_file_is_a_refusal_not_an_oserror(self, tmp_path: Path) -> None:
        with pytest.raises(LiveTestError, match="cannot be read"):
            sha256_file(tmp_path / "absent.log")

    def test_the_default_cap_is_generous_but_finite(self) -> None:
        assert 0 < MAX_ARTEFACT_BYTES <= 128 * 1024 * 1024


class TestLayout:
    def test_the_tree_carries_a_gitkeep_in_every_directory(self, layout: EvidenceLayout) -> None:
        for directory in (
            layout.scenario_dir(SCENARIO),
            layout.logs_dir(SCENARIO),
            layout.journals_dir(SCENARIO),
            layout.snapshots_dir(SCENARIO),
            layout.screenshots_dir(SCENARIO),
            layout.attempts_dir(SCENARIO),
        ):
            assert (directory / GITKEEP_NAME).is_file(), directory

    def test_ensure_tree_is_idempotent_and_destroys_nothing(self, layout: EvidenceLayout) -> None:
        """prepare runs again after a failed session; it must not clear the evidence."""
        survivor = layout.logs_dir(SCENARIO) / "console.txt"
        survivor.write_text("a lua error", encoding="utf-8")

        created = layout.ensure_tree([SCENARIO, "S04_MOVE"])

        assert created == ()
        assert survivor.read_text(encoding="utf-8") == "a lua error"

    def test_attempt_paths_are_zero_padded_and_distinct(self, layout: EvidenceLayout) -> None:
        assert layout.attempt_result_path(SCENARIO, 1).name == "result.001.json"
        assert layout.attempt_result_path(SCENARIO, 12).name == "result.012.json"

    def test_relative_renders_manifest_paths_as_posix(self, layout: EvidenceLayout) -> None:
        assert layout.relative(layout.result_path(SCENARIO)) == f"{SCENARIO}/result.json"

    def test_a_path_outside_the_tree_is_shown_rather_than_hidden(
        self, layout: EvidenceLayout, tmp_path: Path
    ) -> None:
        outside = tmp_path / "elsewhere" / "thing.log"

        assert layout.relative(outside) == outside.as_posix()


class TestValidatedWriting:
    def test_an_invalid_document_is_never_written(self, layout: EvidenceLayout) -> None:
        destination = layout.result_path(SCENARIO)

        with pytest.raises(DocumentInvalidError):
            write_document(destination, {"format": "wrong"}, schema=layout.result_schema)

        assert not destination.exists()

    def test_a_missing_schema_refuses_rather_than_writing_unchecked(
        self, layout: EvidenceLayout
    ) -> None:
        """An unvalidated artefact is indistinguishable from a checked one."""
        layout.result_schema.unlink()
        destination = layout.result_path(SCENARIO)

        with pytest.raises(SchemaUnavailableError, match="missing"):
            write_document(destination, {"anything": True}, schema=layout.result_schema)

        assert not destination.exists()

    def test_the_returned_digest_matches_the_bytes_on_disk(
        self, layout: EvidenceLayout, tmp_path: Path
    ) -> None:
        path = tmp_path / "state.json"

        digest = write_document(path, {"a": 1}, schema=None)

        assert sha256_file(path)[0] == digest.sha256
        assert digest.size_bytes == len(path.read_bytes())


class TestReadingBack:
    def test_a_modified_document_is_reported_as_tampering(self, tmp_path: Path) -> None:
        path = tmp_path / "result.json"
        digest = write_document(path, {"status": "FAIL"}, schema=None)
        path.write_text(canonical_json({"status": "PASS"}), encoding="utf-8")

        with pytest.raises(TamperError, match="modified after it was written"):
            read_document(path, expected_sha256=digest.sha256)

    def test_an_unmodified_document_reads_back(self, tmp_path: Path) -> None:
        path = tmp_path / "result.json"
        digest = write_document(path, {"status": "FAIL"}, schema=None)

        assert read_document(path, expected_sha256=digest.sha256) == {"status": "FAIL"}

    def test_a_json_array_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "result.json"
        path.write_text("[1, 2]", encoding="utf-8")

        with pytest.raises(LiveTestError, match="must be a JSON object"):
            read_document(path)

    def test_a_missing_file_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(LiveTestError, match="does not exist"):
            read_document(tmp_path / "nope.json")


class TestCollection:
    def test_a_copied_file_is_hashed_where_it_landed(
        self, layout: EvidenceLayout, tmp_path: Path
    ) -> None:
        source = tmp_path / "console.txt"
        source.write_text("ERROR: attempt to index a nil value", encoding="utf-8")
        destination = layout.logs_dir(SCENARIO) / "console.txt"

        report = collect_files([(source, destination)], scenario_id=SCENARIO)

        assert len(report.copied) == 1
        assert report.copied[0].sha256 == sha256_file(destination)[0]
        assert destination.read_text(encoding="utf-8").startswith("ERROR")

    def test_a_missing_source_is_skipped_by_name(
        self, layout: EvidenceLayout, tmp_path: Path
    ) -> None:
        """Naming the absent file is the most useful line a failed collection has."""
        absent = tmp_path / "console.txt"

        report = collect_files(
            [(absent, layout.logs_dir(SCENARIO) / "console.txt")], scenario_id=SCENARIO
        )

        assert report.copied == ()
        assert any("console.txt" in entry and "not found" in entry for entry in report.skipped)

    def test_the_file_count_is_bounded(self, layout: EvidenceLayout, tmp_path: Path) -> None:
        pairs = []
        for index in range(5):
            source = tmp_path / f"log{index}.txt"
            source.write_text(str(index), encoding="utf-8")
            pairs.append((source, layout.logs_dir(SCENARIO) / source.name))

        report = collect_files(pairs, scenario_id=SCENARIO, limit=2)

        assert len(report.copied) == 2
        assert len(report.skipped) == 3
        assert all("collection cap" in entry for entry in report.skipped)


class TestManifestEntries:
    def test_a_present_file_is_hashed(self, layout: EvidenceLayout) -> None:
        path = layout.logs_dir(SCENARIO) / "console.txt"
        path.write_text("content", encoding="utf-8")

        entry = digest_entry(layout, scenario_id=SCENARIO, kind="log", path=path, required=True)

        assert entry.present
        assert entry.sha256 == sha256_text("content")
        assert entry.problem == ""

    def test_an_empty_required_file_is_not_evidence(self, layout: EvidenceLayout) -> None:
        """An empty log means nothing was collected, not that nothing went wrong."""
        path = layout.logs_dir(SCENARIO) / "console.txt"
        path.write_text("", encoding="utf-8")

        entry = digest_entry(layout, scenario_id=SCENARIO, kind="log", path=path, required=True)

        assert not entry.present
        assert entry.problem == "empty"

    def test_a_missing_file_records_why(self, layout: EvidenceLayout) -> None:
        entry = digest_entry(
            layout,
            scenario_id=SCENARIO,
            kind="log",
            path=layout.logs_dir(SCENARIO) / "absent.txt",
            required=True,
        )

        assert not entry.present
        assert entry.problem == "missing"
        assert entry.path.endswith("logs/absent.txt")


def test_both_shipped_schemas_are_valid_draft_2020_12() -> None:
    """check_schemas.py scans schemas/; these live under evidence/ and still must compile."""
    validator = pytest.importorskip("jsonschema").Draft202012Validator

    for path in sorted(SCHEMA_SOURCE.glob("*.schema.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        validator.check_schema(document)
        assert "$id" in document, path.name
        assert "title" in document, path.name
