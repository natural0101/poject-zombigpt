"""Redaction: the Cyrillic home directory, the unknown username, and the caps."""

from __future__ import annotations

import unicodedata
from pathlib import Path
from urllib.parse import quote

import pytest

from pz_agent_core.diagnostics.redaction import (
    ELIDED,
    INSTALL_PLACEHOLDER,
    MAX_DEPTH,
    MAX_ITEMS,
    MAX_STRING_LEN,
    PATH_PLACEHOLDER,
    SECRET_PLACEHOLDER,
    STEAM_ID_PLACEHOLDER,
    TRUNCATION_MARKER,
    USER_DIR_PLACEHOLDER,
    USER_HOME_PLACEHOLDER,
    USERNAME_PLACEHOLDER,
    Redactor,
    build_redactor,
    null_redactor,
)
from tests.fixtures.platform_trees import CYRILLIC_USER


def _cyrillic_redactor(home: Path | None = None) -> tuple[Redactor, Path]:
    profile = home or Path("/home") / CYRILLIC_USER
    redactor = build_redactor(
        user_dir=profile / "Zomboid",
        home_dir=profile,
        install_dir=Path("/games/ProjectZomboid"),
        usernames=[CYRILLIC_USER],
    )
    return redactor, profile


# ---------------------------------------------------------------------------
# the Cyrillic case
# ---------------------------------------------------------------------------


def test_known_zomboid_directory_becomes_its_own_placeholder() -> None:
    redactor, profile = _cyrillic_redactor()

    assert redactor.text(str(profile / "Zomboid" / "logs")) == f"{USER_DIR_PLACEHOLDER}/logs"


@pytest.mark.parametrize(
    "spelling",
    [
        "/home/{user}/Zomboid/x",
        "/HOME/{USER}/Zomboid/x",
        r"C:\Users\{user}\Zomboid\x",
        r"C:\\Users\\{user}\\Zomboid\\x",
        "user={user}",
        "reported by {user} in chat",
    ],
)
def test_no_spelling_of_a_cyrillic_username_survives(spelling: str) -> None:
    redactor, _ = _cyrillic_redactor()
    sample = spelling.format(user=CYRILLIC_USER, USER=CYRILLIC_USER.upper())

    redacted = redactor.text(sample)

    assert CYRILLIC_USER.lower() not in redacted.lower()
    assert CYRILLIC_USER.upper() not in redacted


def test_decomposed_and_percent_encoded_forms_are_matched_too() -> None:
    """The two forms a naive replace on the composed ASCII spelling misses."""
    redactor, profile = _cyrillic_redactor()
    composed = str(profile / "Zomboid" / "x")

    decomposed = redactor.text(unicodedata.normalize("NFD", composed))
    encoded = redactor.text(quote(composed))

    assert decomposed == f"{USER_DIR_PLACEHOLDER}/x"
    assert encoded == f"{USER_DIR_PLACEHOLDER}/x"


def test_an_unknown_account_name_is_still_removed_from_a_profile_path() -> None:
    """The redactor is usually built before the username is known."""
    redactor = null_redactor()

    assert (
        redactor.text(r"C:\Users\Иван\Documents\a.txt")
        == f"{USER_HOME_PLACEHOLDER}\\Documents\\a.txt"
    )
    assert redactor.text("/home/someone/notes.md") == f"{USER_HOME_PLACEHOLDER}/notes.md"


def test_a_short_username_is_not_struck_out_of_an_unrelated_word() -> None:
    redactor = build_redactor(usernames=["ann"])

    assert redactor.text("channel = stable") == "channel = stable"
    assert redactor.text("owner is ann") == f"owner is {USERNAME_PLACEHOLDER}"


# ---------------------------------------------------------------------------
# other rules
# ---------------------------------------------------------------------------


def test_the_install_directory_gets_its_own_placeholder() -> None:
    redactor, _ = _cyrillic_redactor()

    assert redactor.text("/games/ProjectZomboid/media/lua") == f"{INSTALL_PLACEHOLDER}/media/lua"


def test_an_unrelated_absolute_path_keeps_only_its_basename() -> None:
    redactor = null_redactor()

    assert redactor.text("cannot read /var/data/private/report.bin") == (
        f"cannot read {PATH_PLACEHOLDER}/report.bin"
    )


def test_a_url_is_not_mistaken_for_a_path() -> None:
    redactor = null_redactor()
    url = "see https://github.com/natural0101/poject-zombigpt/issues"

    assert redactor.text(url) == url


def test_a_placeholder_is_not_re_redacted() -> None:
    """Rules run in sequence, so an earlier rule's output must survive later ones."""
    redactor, profile = _cyrillic_redactor()

    once = redactor.text(str(profile / "Zomboid" / "logs" / "pz-agent.log"))

    assert redactor.text(once) == once


#: Assembled rather than written out: the repository's own secret scanner walks
#: every tracked text file, and a literal PEM header here would be a finding —
#: correctly, which is the behaviour under test.
PEM_HEADER = "-----BEGIN " + "PRIVATE KEY" + "-----"


@pytest.mark.parametrize(
    ("sample", "placeholder"),
    [
        ("steamid=76561198012345678", SECRET_PLACEHOLDER),
        ("owner 76561198012345678 joined", STEAM_ID_PLACEHOLDER),
        ("Authorization: Bearer abcdefghijklmnop", SECRET_PLACEHOLDER),
        ('{"api_key": "abc123def456"}', SECRET_PLACEHOLDER),
        (PEM_HEADER, SECRET_PLACEHOLDER),
    ],
)
def test_credential_shapes_are_struck_out(sample: str, placeholder: str) -> None:
    assert placeholder in null_redactor().text(sample)


def test_a_key_shaped_token_is_removed_even_without_a_label() -> None:
    # Built at runtime so this source file does not itself contain a key shape.
    token = "sk-ant-" + "A1b2C3d4E5f6G7h8"

    assert null_redactor().text(f"value {token}") == f"value {SECRET_PLACEHOLDER}"


# ---------------------------------------------------------------------------
# bounds
# ---------------------------------------------------------------------------


def test_a_long_string_is_truncated_with_a_marker() -> None:
    redacted = null_redactor().text("x" * (MAX_STRING_LEN * 2))

    assert redacted.endswith(TRUNCATION_MARKER)
    assert len(redacted) == MAX_STRING_LEN + len(TRUNCATION_MARKER)


def test_recursion_stops_at_the_depth_cap() -> None:
    document: dict[str, object] = {"leaf": "value"}
    for _ in range(MAX_DEPTH + 4):
        document = {"nested": document}

    redacted = null_redactor().value(document)

    depth = 0
    node: object = redacted
    while isinstance(node, dict) and "nested" in node:
        node = node["nested"]
        depth += 1
    assert node == ELIDED
    assert depth == MAX_DEPTH


def test_a_wide_list_is_elided_with_a_count_rather_than_dropped() -> None:
    redacted = null_redactor().value(list(range(MAX_ITEMS + 5)))

    assert isinstance(redacted, list)
    assert len(redacted) == MAX_ITEMS + 1
    assert redacted[-1] == f"{ELIDED} 5 more item(s)"


def test_a_wide_mapping_reports_how_many_keys_it_dropped() -> None:
    redacted = null_redactor().value({f"k{index}": index for index in range(MAX_ITEMS + 3)})

    assert isinstance(redacted, dict)
    assert redacted[ELIDED] == "3 more key(s)"


def test_a_secret_named_key_has_its_value_struck_out_whatever_it_looks_like() -> None:
    """In a structured document the key and the value never form one span."""
    redacted = null_redactor().value({"api_key": "plain-looking", "steam_id": 12345, "seq": 4})

    assert redacted == {"api_key": SECRET_PLACEHOLDER, "steam_id": SECRET_PLACEHOLDER, "seq": 4}


def test_mapping_keys_are_redacted_as_well_as_values() -> None:
    redactor, profile = _cyrillic_redactor()

    redacted = redactor.value({str(profile / "Zomboid" / "a"): "ok"})

    assert isinstance(redacted, dict)
    assert list(redacted) == [f"{USER_DIR_PLACEHOLDER}/a"]


def test_findings_names_the_rules_that_still_match() -> None:
    redactor, profile = _cyrillic_redactor()

    assert redactor.findings("clean text") == ()
    assert "user_dir" in redactor.findings(str(profile / "Zomboid"))
