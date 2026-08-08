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
    canonical_json_bytes,
    collect_files,
    digest_entry,
    read_document,
    sha256_file,
    sha256_text,
    write_document,
)
from pz_agent_core.diagnostics.redaction import null_redactor
from tests.fixtures.platform_trees import CYRILLIC_USER

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
        path.write_bytes(canonical_json_bytes(document))

        digest, size = sha256_file(path)

        # `read_bytes`, not `read_text`: text mode would decode through the
        # locale encoding and translate newlines, so the round trip would be
        # testing the platform rather than the canonical form.
        raw = path.read_bytes()
        assert digest == sha256_text(canonical_json(json.loads(raw.decode("utf-8"))))
        assert size == len(raw)

    def test_non_ascii_survives_the_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "doc.json"
        path.write_bytes(canonical_json_bytes({"user": "Пользователь"}))

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

    def test_a_path_outside_the_tree_is_shown_redacted_rather_than_hidden_or_raw(
        self, layout: EvidenceLayout, tmp_path: Path
    ) -> None:
        """Reconciled from ``== outside.as_posix()``: shown, but never the account name.

        The previous pin recorded the raw absolute spelling, and that spelling
        flows into manifest artefact entries — a committed, published document
        — carrying the profile directory and with it the account name of
        whoever ran the suite (E12-M01-T008). The two halves of the old
        behaviour that were worth keeping are kept and asserted separately:
        an outside-tree path is still *visible* as one (it is not silently
        relativised into a fake in-tree path, and not an exception), and the
        basename still says which file it was. What changed is the spelling:
        the floor redactor's placeholders replace the directories that name a
        machine and a person, which is not a lie about where the file is —
        "an absolute path elsewhere, ending in thing.log" is exactly what the
        placeholder form states.
        """
        outside = tmp_path / "Users" / CYRILLIC_USER / "elsewhere" / "thing.log"

        shown = layout.relative(outside)

        assert shown == null_redactor().text(outside.as_posix())
        assert shown != outside.as_posix(), "the raw absolute spelling reached the manifest"
        assert CYRILLIC_USER not in shown
        assert shown.endswith("thing.log"), "the basename is the diagnostic half; keep it"
        # Still visibly not an in-tree relative path: the placeholder marks it.
        assert "<" in shown and ">" in shown


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
        path.write_bytes(canonical_json_bytes({"status": "PASS"}))

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

    def test_a_collection_report_never_carries_the_account_name(
        self, layout: EvidenceLayout, tmp_path: Path
    ) -> None:
        """The evidence half of E12-M01-T008: ``collected.json`` is committed.

        The realistic sources — the game console, the IPC journals — live
        under the user's profile, and this report is written verbatim into
        the evidence tree, so a ``source`` or skip line spelled absolutely
        publishes the account name. Both shapes are exercised: a source that
        copies (its spelling lands in ``copied``) and one that is absent (its
        spelling lands in ``skipped``), and the sweep runs over the whole
        serialised report. The skip line must still name the file — the
        basename is the diagnostic part and the redactor keeps it.
        """
        profile = tmp_path / "Users" / CYRILLIC_USER / "Zomboid"
        profile.mkdir(parents=True)
        console = profile / "console.txt"
        console.write_text("a lua error", encoding="utf-8")
        absent = profile / "queue.jsonl"

        report = collect_files(
            [
                (console, layout.logs_dir(SCENARIO) / "console.txt"),
                (absent, layout.journals_dir(SCENARIO) / "queue.jsonl"),
            ],
            scenario_id=SCENARIO,
        )

        assert len(report.copied) == 1, report.skipped
        assert report.copied[0].source.endswith("console.txt")
        assert any("queue.jsonl" in line and "not found" in line for line in report.skipped)
        document = json.dumps(report.to_dict(), ensure_ascii=False)
        assert CYRILLIC_USER not in document, document


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

    def test_no_manifest_entry_for_an_outside_tree_file_carries_the_account_name(
        self, layout: EvidenceLayout, tmp_path: Path
    ) -> None:
        """The manifest half of E12-M01-T008, observed over a real entry.

        ``digest_entry`` is what the manifest is assembled from, and its
        ``path`` field is the one place an outside-tree artefact's absolute
        spelling — profile directory, account name and all — used to land.
        The sweep is over the entry's whole serialised form, not just the
        path field, so a second field starting to carry the spelling would
        fail here too.
        """
        outside = tmp_path / "Users" / CYRILLIC_USER / "Zomboid" / "console.txt"
        outside.parent.mkdir(parents=True)
        outside.write_text("a lua error", encoding="utf-8")

        entry = digest_entry(layout, scenario_id=SCENARIO, kind="log", path=outside, required=False)

        assert entry.present, "the file exists; the entry must still hash it"
        assert entry.path.endswith("console.txt")
        document = json.dumps(entry.to_dict(), ensure_ascii=False)
        assert CYRILLIC_USER not in document, document


def test_both_shipped_schemas_are_valid_draft_2020_12() -> None:
    """check_schemas.py scans schemas/; these live under evidence/ and still must compile."""
    validator = pytest.importorskip("jsonschema").Draft202012Validator

    for path in sorted(SCHEMA_SOURCE.glob("*.schema.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        validator.check_schema(document)
        assert "$id" in document, path.name
        assert "title" in document, path.name
