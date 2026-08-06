"""Configuration validation: the documented sample, typos, and out-of-range values."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from pz_agent_cli.config import (
    CODE_NOT_ALLOWED,
    CODE_NOT_FOUND,
    CODE_OUT_OF_RANGE,
    CODE_PARSE_ERROR,
    CODE_TOO_LARGE,
    CODE_TYPE_MISMATCH,
    CODE_UNKNOWN_KEY,
    CODE_UNKNOWN_TABLE,
    MAX_AUTONOMOUS_RADIUS,
    MAX_CONFIG_BYTES,
    SCHEMA,
    ConfigError,
    ConfigProblem,
    default_config,
    load_config,
)
from pz_agent_cli.context import EXIT_FAILURE, EXIT_OK
from pz_agent_core.protocol import SessionMode
from tests.conftest import REPO_ROOT
from tests.fixtures.cli_worlds import make_world


def _quickstart_sample() -> str:
    """The TOML block from docs/QUICKSTART.md, read rather than copied.

    A hand-copied sample asserts that *a* configuration validates; it goes on
    passing after the documented one has drifted away from the schema, which is
    the failure this test exists to catch.
    """
    text = (REPO_ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    blocks: list[str] = re.findall(r"```toml\n(.*?)```", text, flags=re.DOTALL)
    assert blocks, "docs/QUICKSTART.md no longer carries a toml sample"
    sample = blocks[0]
    assert "[safety]" in sample, "the first toml block is no longer the configuration sample"
    return sample


#: The sample in docs/QUICKSTART.md. If the documented file stops validating,
#: one of the two is wrong and this test is where that shows up.
QUICKSTART_SAMPLE = _quickstart_sample()


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# the documented file
# ---------------------------------------------------------------------------


def test_the_quickstart_sample_validates(tmp_path: Path) -> None:
    validation = load_config(_write(tmp_path, QUICKSTART_SAMPLE))

    assert validation.ok
    assert validation.errors == ()
    assert validation.config is not None
    assert validation.config.default_mode is SessionMode.OBSERVE


def test_an_empty_file_takes_every_documented_default(tmp_path: Path) -> None:
    validation = load_config(_write(tmp_path, ""))

    assert validation.ok
    assert validation.config is not None
    assert validation.config.to_dict() == default_config().to_dict()


def test_the_optional_directory_keys_default_to_absent(tmp_path: Path) -> None:
    validation = load_config(_write(tmp_path, QUICKSTART_SAMPLE))

    assert validation.config is not None
    assert validation.config.install_dir is None
    assert validation.config.user_dir is None


def test_an_explicit_install_directory_is_kept(tmp_path: Path) -> None:
    validation = load_config(_write(tmp_path, '[game]\ninstall_dir = "D:/Games/ProjectZomboid"\n'))

    assert validation.ok
    assert validation.config is not None
    assert validation.config.install_dir == Path("D:/Games/ProjectZomboid")


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


def test_an_unknown_key_is_an_error_and_suggests_the_real_one(tmp_path: Path) -> None:
    """A typo in a safety setting must not read as "off"."""
    validation = load_config(_write(tmp_path, "[safety]\nmanual_takover = false\n"))

    assert not validation.ok
    problem = validation.errors[0]
    assert problem.code == CODE_UNKNOWN_KEY
    assert problem.path == "safety.manual_takover"
    assert "manual_takeover" in problem.detail
    assert validation.config is None


def test_an_unknown_table_is_an_error_not_a_warning(tmp_path: Path) -> None:
    validation = load_config(_write(tmp_path, "[saftey]\nmanual_takeover = false\n"))

    assert validation.errors[0].code == CODE_UNKNOWN_TABLE
    assert "silently discards every setting inside it" in validation.errors[0].remediation


def test_a_typo_does_not_silently_keep_the_default(tmp_path: Path) -> None:
    validation = load_config(_write(tmp_path, "[safety]\nmanual_takover = false\n"))

    # The point of the rule: nothing usable is produced, so nothing can start
    # with a safety setting the user believes they changed.
    assert validation.config is None


def test_a_value_over_the_product_limit_is_out_of_range(tmp_path: Path) -> None:
    validation = load_config(
        _write(tmp_path, f"[safety]\nmax_autonomous_radius = {MAX_AUTONOMOUS_RADIUS + 1}\n")
    )

    problem = validation.errors[0]
    assert problem.code == CODE_OUT_OF_RANGE
    assert problem.path == "safety.max_autonomous_radius"
    assert "cannot be raised by configuration" in problem.remediation


def test_a_value_below_the_minimum_is_out_of_range(tmp_path: Path) -> None:
    validation = load_config(_write(tmp_path, "[planner]\nmax_steps = 0\n"))

    assert validation.errors[0].code == CODE_OUT_OF_RANGE
    assert validation.errors[0].path == "planner.max_steps"


@pytest.mark.parametrize(
    ("body", "path"),
    [
        ("[safety]\nmanual_takeover = 1\n", "safety.manual_takeover"),
        ('[planner]\nmax_steps = "eight"\n', "planner.max_steps"),
        ("[game]\nchannel = 3\n", "game.channel"),
    ],
)
def test_a_wrong_type_is_reported_with_its_key(tmp_path: Path, body: str, path: str) -> None:
    validation = load_config(_write(tmp_path, body))

    assert validation.errors[0].code == CODE_TYPE_MISMATCH
    assert validation.errors[0].path == path


def test_a_value_outside_the_allowed_set_is_refused(tmp_path: Path) -> None:
    validation = load_config(_write(tmp_path, '[game]\nchannel = "beta"\n'))

    assert validation.errors[0].code == CODE_NOT_ALLOWED
    assert "stable" in validation.errors[0].remediation


def test_a_provider_this_build_cannot_run_is_refused(tmp_path: Path) -> None:
    validation = load_config(_write(tmp_path, '[planner]\nprovider = "openai"\n'))

    assert validation.errors[0].path == "planner.provider"
    assert validation.errors[0].code == CODE_NOT_ALLOWED


def test_a_hotkey_combination_is_refused_with_what_is_supported(tmp_path: Path) -> None:
    validation = load_config(_write(tmp_path, '[safety]\npanic_hotkey = "Ctrl+Shift+F12"\n'))

    assert validation.errors[0].path == "safety.panic_hotkey"
    assert "combinations are not supported" in validation.errors[0].remediation


def test_a_table_written_as_a_scalar_is_reported(tmp_path: Path) -> None:
    validation = load_config(_write(tmp_path, 'safety = "on"\n'))

    assert validation.errors[0].code == CODE_TYPE_MISMATCH
    assert validation.errors[0].path == "safety"


def test_broken_toml_names_the_syntax_error(tmp_path: Path) -> None:
    validation = load_config(_write(tmp_path, "[safety\nmanual_takeover = true\n"))

    assert validation.errors[0].code == CODE_PARSE_ERROR


def test_a_missing_file_is_an_error_rather_than_silent_defaults(tmp_path: Path) -> None:
    validation = load_config(tmp_path / "absent.toml")

    assert validation.errors[0].code == CODE_NOT_FOUND
    assert "docs/QUICKSTART.md" in validation.errors[0].remediation


def test_an_oversized_file_is_refused_before_it_is_parsed(tmp_path: Path) -> None:
    validation = load_config(_write(tmp_path, "# " + "x" * (MAX_CONFIG_BYTES + 10)))

    assert validation.errors[0].code == CODE_TOO_LARGE


def test_every_error_carries_remediation() -> None:
    with pytest.raises(ConfigError, match="must say what to do about it"):
        ConfigProblem(path="safety.x", code=CODE_UNKNOWN_KEY, detail="typo", remediation="")


# ---------------------------------------------------------------------------
# warnings
# ---------------------------------------------------------------------------


def test_settings_that_validate_but_deserve_a_word_are_warnings(tmp_path: Path) -> None:
    validation = load_config(
        _write(
            tmp_path,
            "[safety]\nallow_multiplayer = true\n[session]\nrequire_backup = false\n",
        )
    )

    assert validation.ok
    paths = {problem.path for problem in validation.warnings}
    assert paths == {"safety.allow_multiplayer", "session.require_backup"}


def test_an_expected_build_outside_the_supported_set_warns(tmp_path: Path) -> None:
    validation = load_config(_write(tmp_path, '[game]\nexpected_build = "41.78"\n'))

    assert validation.ok
    assert any(problem.path == "game.expected_build" for problem in validation.warnings)


# ---------------------------------------------------------------------------
# through the CLI
# ---------------------------------------------------------------------------


def test_validate_config_exits_non_zero_and_names_the_key(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    config = _write(tmp_path, "[safety]\nmax_autonomous_radius = 500\n")

    exit_code = world.run("--config", str(config), "validate-config")

    assert exit_code == EXIT_FAILURE
    assert "safety.max_autonomous_radius" in world.stderr
    assert "the agent will not start" in world.stdout


def test_validate_config_json_reports_the_problems_machine_readably(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    config = _write(tmp_path, "[safety]\nmanual_takover = false\n")

    exit_code = world.run("--config", str(config), "validate-config", "--json")

    assert exit_code == EXIT_FAILURE
    document = json.loads(world.stdout)
    assert document["ok"] is False
    assert document["errors"][0]["code"] == CODE_UNKNOWN_KEY
    assert document["config"] is None


def test_validate_config_accepts_the_documented_sample(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    config = _write(tmp_path, QUICKSTART_SAMPLE)

    assert world.run("--config", str(config), "validate-config") == EXIT_OK
    assert "configuration is valid" in world.stdout


def test_the_schema_covers_every_table_the_sample_uses() -> None:
    """Guards the other direction: a key added to the docs but not to the schema."""
    sample_tables = {
        line.strip("[]") for line in QUICKSTART_SAMPLE.splitlines() if line.startswith("[")
    }

    assert sample_tables <= set(SCHEMA)
