"""The support bundle: what it accepts, what it refuses, and what verify reports."""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pz_agent_cli.context import resolve_workspace
from pz_agent_cli.support import build_support_bundle
from pz_agent_core.diagnostics.bundle import (
    MANIFEST_NAME,
    BundleBuilder,
    BundleError,
    verify_bundle,
)
from pz_agent_core.diagnostics.redaction import (
    SECRET_PLACEHOLDER,
    USER_DIR_PLACEHOLDER,
    build_redactor,
    null_redactor,
)
from pz_agent_core.rpc.descriptor import runtime_dir
from pz_agent_core.rpc.token import TOKEN_FILENAME, issue_token
from tests.fixtures.cli_worlds import StepDatetime, make_world
from tests.fixtures.platform_trees import CYRILLIC_USER


def _builder(tmp_path: Path, **kwargs: int) -> BundleBuilder:
    home = tmp_path / "Users" / CYRILLIC_USER
    return BundleBuilder(
        redactor=build_redactor(
            user_dir=home / "Zomboid", home_dir=home, usernames=[CYRILLIC_USER]
        ),
        clock=StepDatetime(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------


def test_a_bundle_lists_every_member_with_its_hash(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    builder.add_json("versions.json", {"product_version": "0.1.0"})
    builder.add_text("notes.txt", "hello\n")

    bundle = builder.build(tmp_path / "out" / "support.zip")

    assert bundle.names == ("versions.json", "notes.txt")
    assert all(len(entry.sha256) == 64 for entry in bundle.entries)
    with zipfile.ZipFile(bundle.path) as archive:
        assert set(archive.namelist()) == {"versions.json", "notes.txt", MANIFEST_NAME}


def test_a_json_member_is_redacted_before_it_is_serialised(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    home = tmp_path / "Users" / CYRILLIC_USER

    builder.add_json("doctor.json", {"user_dir": str(home / "Zomboid"), "token": "abcdef123456"})
    bundle = builder.build(tmp_path / "support.zip")

    with zipfile.ZipFile(bundle.path) as archive:
        body = archive.read("doctor.json").decode("utf-8")
    assert CYRILLIC_USER not in body
    assert USER_DIR_PLACEHOLDER in body
    assert SECRET_PLACEHOLDER in body


def test_only_the_tail_of_a_long_file_is_taken_and_the_omission_is_stated(
    tmp_path: Path,
) -> None:
    log = tmp_path / "pz-agent.log"
    log.write_text("".join(f"line {index}\n" for index in range(4000)), encoding="utf-8")
    builder = _builder(tmp_path)

    entry = builder.add_file("logs/pz-agent.log", log, tail_bytes=512)
    bundle = builder.build(tmp_path / "support.zip")

    assert entry.truncated
    with zipfile.ZipFile(bundle.path) as archive:
        body = archive.read("logs/pz-agent.log").decode("utf-8")
    assert body.startswith("... earlier content omitted")
    assert "line 3999" in body
    assert "line 0\n" not in body


def test_a_file_that_cannot_be_read_is_an_error_not_an_empty_member(tmp_path: Path) -> None:
    builder = _builder(tmp_path)

    with pytest.raises(BundleError, match="cannot read for the bundle"):
        builder.add_file("logs/absent.log", tmp_path / "absent.log")


@pytest.mark.parametrize(
    "name",
    ["/absolute.json", "../escape.json", "nested/../../out.json", "weird name.json", ""],
)
def test_a_member_name_that_could_escape_the_archive_is_refused(tmp_path: Path, name: str) -> None:
    builder = _builder(tmp_path)

    with pytest.raises(BundleError):
        builder.add_text(name, "x")


def test_the_same_member_may_not_be_added_twice(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    builder.add_text("notes.txt", "one")

    with pytest.raises(BundleError, match="already holds a member"):
        builder.add_text("notes.txt", "two")


def test_the_member_count_is_capped(tmp_path: Path) -> None:
    builder = _builder(tmp_path, max_entries=2)
    builder.add_text("a.txt", "a")
    builder.add_text("b.txt", "b")

    with pytest.raises(BundleError, match="at most 2 members"):
        builder.add_text("c.txt", "c")


def test_the_per_member_and_total_byte_caps_are_enforced(tmp_path: Path) -> None:
    builder = _builder(tmp_path, max_entry_bytes=64, max_total_bytes=96)

    with pytest.raises(BundleError, match="exceeds the per-member cap"):
        builder.add_text("big.txt", "x" * 200)
    builder.add_text("a.txt", "x" * 50)
    with pytest.raises(BundleError, match="past its 96 byte cap"):
        builder.add_text("b.txt", "y" * 60)


def test_nothing_is_written_until_build(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    builder.add_text("notes.txt", "hello")

    assert list(tmp_path.glob("*.zip")) == []


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


def test_verify_lists_the_contents_and_reports_clean(tmp_path: Path) -> None:
    home = tmp_path / "Users" / CYRILLIC_USER
    builder = _builder(tmp_path)
    builder.add_json("doctor.json", {"path": str(home / "Zomboid" / "Lua")})
    builder.add_file("logs/pz-agent.log", _write_log(tmp_path, home))
    bundle = builder.build(tmp_path / "support.zip")

    verification = verify_bundle(
        bundle.path,
        redactor=build_redactor(user_dir=home / "Zomboid", home_dir=home),
        forbidden={"home_directory": str(home), "account_name": CYRILLIC_USER},
    )

    assert verification.clean
    assert [entry.name for entry in verification.entries] == [
        "doctor.json",
        "logs/pz-agent.log",
        MANIFEST_NAME,
    ]
    rendered = "\n".join(verification.render_lines())
    assert "doctor.json" in rendered
    assert "clean" in rendered
    assert "nothing matching a home directory" in rendered


def test_verify_flags_a_member_that_reached_the_archive_unredacted(tmp_path: Path) -> None:
    """The case verification exists for: a writer that skipped the redactor."""
    home = tmp_path / "Users" / CYRILLIC_USER
    archive_path = tmp_path / "leaky.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("doctor.json", f'{{"path": "{home}/Zomboid"}}')

    verification = verify_bundle(
        archive_path,
        redactor=build_redactor(user_dir=home / "Zomboid", home_dir=home),
        forbidden={"account_name": CYRILLIC_USER},
    )

    assert not verification.clean
    entry = verification.entries[0]
    assert "user_dir" in entry.findings
    assert "literal:account_name" in entry.findings
    # The report says which literal was found, never what it was: it is printed
    # to the terminal and emitted as JSON.
    rendered = "\n".join(verification.render_lines())
    assert CYRILLIC_USER not in rendered
    assert CYRILLIC_USER not in json.dumps(verification.to_dict(), ensure_ascii=False)
    assert "REVIEW BEFORE SHARING" in rendered


@pytest.mark.parametrize(
    ("member_text", "expected_finding"),
    [
        # Key shapes assembled at runtime so the repository's own secret
        # scanner does not — correctly — flag this file.
        ("2026-08-05 INFO auth with " + "sk-ant-" + "A1b2C3d4E5f6G7h8" + "\n", "anthropic_key"),
        ('{"api_key": "9f8e7d6c5b4a3210"}\n', "credential_assignment"),
    ],
    ids=["bare-key", "labelled-assignment"],
)
def test_verify_of_an_archive_carrying_a_credential_produces_findings(
    tmp_path: Path, member_text: str, expected_finding: str
) -> None:
    """The composed claim of E12-M01-T006, asserted end to end.

    Redactor-flags-credential and findings-reach-VerifiedEntry were each
    tested alone, but never the composition the criterion actually makes:
    ``verify_bundle`` over an archive whose member carries a token or key
    returns non-empty findings. A verifier that scanned members with an empty
    rule list — or dropped credential findings on the way into the entry —
    would have passed both halves and failed the whole.
    """
    archive_path = tmp_path / "leaky.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("logs/pz-agent.log", member_text)

    verification = verify_bundle(archive_path, redactor=null_redactor())

    assert not verification.clean
    entry = verification.entries[0]
    assert entry.findings, "a member carrying a credential must be flagged"
    assert expected_finding in entry.findings
    rendered = "\n".join(verification.render_lines())
    assert "REVIEW BEFORE SHARING" in rendered


def test_verify_reports_a_member_it_could_not_scan(tmp_path: Path) -> None:
    archive_path = tmp_path / "binary.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("blob.bin", b"\xff\xfe\x00binary")

    verification = verify_bundle(archive_path, redactor=null_redactor())

    assert not verification.clean
    assert "not utf-8 text; contents were not scanned" in verification.entries[0].findings


def test_verify_refuses_a_file_that_is_not_an_archive(tmp_path: Path) -> None:
    path = tmp_path / "not.zip"
    path.write_text("plain text", encoding="utf-8")

    with pytest.raises(BundleError, match="not a readable support bundle"):
        verify_bundle(path)


def test_no_member_of_the_real_support_bundle_carries_the_rpc_token(tmp_path: Path) -> None:
    """The whole chain, not the redactor component: E12-M01-T003.

    A real token is issued into the workspace's real runtime directory — the
    place a running sidecar keeps it — a real log is left where ``logs``
    collects from, and the bundle is the one ``pz-agent logs --bundle``
    builds: versions, doctor, capabilities, config, log tails. Then every
    member's bytes are scanned for the token and for its hex. The component
    tests could all stay green while a future member (a doctor field, a new
    tail) started carrying the key; this is the assertion that fails then.
    """
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)
    token = issue_token(runtime_dir(workspace.state_dir))
    workspace.logs_dir.mkdir(parents=True, exist_ok=True)
    (workspace.logs_dir / "pz-agent.log").write_text(
        "2026-08-05 INFO rpc.published family=AF_UNIX\n", encoding="utf-8"
    )

    bundle = build_support_bundle(world.ctx, workspace, tmp_path / "support.zip")

    # Guards against a vacuous pass: the secret is really on disk inside the
    # workspace the bundle was built from, and the bundle really has members,
    # including the log directory the runtime directory lives beside.
    assert (runtime_dir(workspace.state_dir) / TOKEN_FILENAME).read_bytes() == token
    assert {"versions.json", "doctor.json", "logs/pz-agent.log"} <= set(bundle.names)
    spellings = (token, token.hex().encode(), token.hex().upper().encode())
    with zipfile.ZipFile(bundle.path) as archive:
        for name in archive.namelist():
            data = archive.read(name)
            assert data, f"{name}: an empty member scans clean vacuously"
            for spelling in spellings:
                assert spelling not in data, f"{name} carries the RPC token"


def test_the_manifest_records_the_creation_time_and_totals(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    builder.add_text("notes.txt", "hello\n")

    bundle = builder.build(tmp_path / "support.zip")

    assert datetime.fromisoformat(bundle.created_at).tzinfo == UTC
    assert bundle.total_bytes == sum(entry.size for entry in bundle.entries)


def _write_log(tmp_path: Path, home: Path) -> Path:
    log = tmp_path / "pz-agent.log"
    log.write_text(f"2026-08-05 INFO wrote {home / 'Zomboid' / 'Lua'}\n", encoding="utf-8")
    return log
