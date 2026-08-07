"""The capability off-switch two shipped documents already described.

``CapabilityState.DISABLED_BY_POLICY`` existed. The mod's ``CapabilityRuntime``
guarded on it. ``PermissionEngine`` refused on it with a message written for a
user — *"X is switched off by configuration."* ``docs/COMPATIBILITY.md``, which
ships inside the Windows archive, listed it in the state table as "available,
but configuration forbids it", and ``docs/PROTOCOL.md`` said the same.

No configuration could produce it. ``Capability.disabled_by_policy`` — the only
constructor for the state — was called from three tests and nowhere else, and
``config.toml`` had no key for it, so `validate-config` rejected anything an
operator invented (unknown keys are hard errors here, deliberately).

The cost is concrete rather than theoretical: ``COMPATIBILITY.md`` warns three
rows below that table that ``survival_sleep`` is capped experimental because
"once the character is asleep there is no timed action to interrupt and no queue
entry to cancel, so a panic stop cannot reach them". A reader who takes that
seriously and goes looking for the switch the same page just described had
nothing to write.

This is the same shape as the multiplayer defect — a documented control that was
implemented nowhere — and it is closed the same way: by making the control real
rather than by deleting the row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from pz_agent_cli.app import build_capabilities
from pz_agent_cli.config import KNOWN_CAPABILITIES, load_config
from pz_agent_cli.context import EXIT_FAILURE, EXIT_OK, resolve_workspace
from tests.fixtures.cli_worlds import CliWorld, make_world

#: The capability under test, and it has to be one that is *usable* to begin
#: with. The first draft of this file used ``survival_sleep`` — the one the
#: documents' own warning points a cautious reader at — and every assertion
#: passed before the feature existed, because §12.4 caps it at ``experimental``
#: and ``report.usable`` excludes that state. Switching off something already
#: refused proves nothing at all.
SUBJECT: Final = "eat_percentage"

#: Named only in the prose above: it is what motivates the control, and it is
#: the wrong thing to assert against for the reason just given.
SLEEP: Final = "survival_sleep"


def _configure(world: CliWorld, body: str) -> None:
    path = resolve_workspace(world.ctx).config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _switch_off(*names: str) -> str:
    listed = ", ".join(f'"{name}"' for name in names)
    return f"[safety]\ndisabled_capabilities = [{listed}]\n"


def test_a_switched_off_capability_is_not_usable(tmp_path: Path) -> None:
    """The whole point: the engine's capability check answers False.

    Asserted against the ledger the CLI assembles, because that is the object
    the action engine is handed — a report that still lists the capability and a
    ledger that refuses it is exactly the arrangement wanted, and asserting
    against the report would have missed it.
    """
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)

    _configure(world, "[safety]\ndisabled_capabilities = []\n")
    assert build_capabilities(workspace).usable(SUBJECT) is True, (
        f"{SUBJECT} is not usable on this fixture even before anything switches it "
        "off, so switching it off would prove nothing"
    )

    _configure(world, _switch_off(SUBJECT))
    ledger = build_capabilities(workspace)

    assert ledger.usable(SUBJECT) is False
    assert SUBJECT not in ledger.record().usable
    assert SUBJECT in KNOWN_CAPABILITIES, "the name under test is not one the build knows"


def test_switching_one_off_leaves_the_others_alone(tmp_path: Path) -> None:
    """A control that subtracts more than it was asked to is not one."""
    world = make_world(tmp_path)
    workspace = resolve_workspace(world.ctx)

    _configure(world, "[safety]\ndisabled_capabilities = []\n")
    before = set(build_capabilities(resolve_workspace(world.ctx)).record().usable)

    _configure(world, _switch_off(SUBJECT))
    after = set(build_capabilities(workspace).record().usable)

    assert before - after == {SUBJECT}, (
        f"switching off {SUBJECT} changed the usable set by {before - after}"
    )
    assert after - before == set()


def test_status_says_why_rather_than_dropping_the_name(tmp_path: Path) -> None:
    """A capability that silently stops appearing is indistinguishable from a bug.

    An operator who switched something off months ago and forgot needs the
    record to tell them that is what happened, not to leave a hole.
    """
    world = make_world(tmp_path)
    _configure(world, _switch_off(SUBJECT))

    record = build_capabilities(resolve_workspace(world.ctx)).record()

    assert SUBJECT in record.refused
    assert "config.toml" in record.refused[SUBJECT]


def test_a_name_no_probe_knows_is_an_error_not_an_ignored_line(tmp_path: Path) -> None:
    """The module's own rule for unknown keys, applied to an unknown value.

    A typo that validates leaves the user believing they disabled something and
    leaves the agent free to use it, which is the failure this file exists over.
    """
    world = make_world(tmp_path)
    _configure(world, _switch_off("eat_percentages"))

    world.reset_streams()
    exit_code = world.run("validate-config")

    assert exit_code == EXIT_FAILURE
    said = world.stdout + world.stderr
    assert "eat_percentages" in said
    assert SUBJECT in said, "the refusal does not offer the names that would work"


def test_the_default_switches_nothing_off(tmp_path: Path) -> None:
    """Nothing is disabled for a user who did not ask."""
    world = make_world(tmp_path)
    _configure(world, "[safety]\nmanual_takeover = true\n")

    ledger = build_capabilities(resolve_workspace(world.ctx))

    assert ledger.disabled == frozenset()
    world.reset_streams()
    assert world.run("validate-config") == EXIT_OK, world.stdout + world.stderr


def test_a_configuration_that_does_not_validate_switches_nothing_off(
    tmp_path: Path,
) -> None:
    """Deliberate, and the opposite of what "fail closed" would suggest here.

    Reading the raw table anyway would let a document with a typo *elsewhere in
    it* decide what the agent may do, and a user who cannot see their own file's
    errors is the last one to obey silently. ``validate-config`` reports it.
    """
    world = make_world(tmp_path)
    _configure(world, _switch_off(SUBJECT) + 'max_autonomous_radius = "wide"\n')

    ledger = build_capabilities(resolve_workspace(world.ctx))

    assert ledger.disabled == frozenset()
    world.reset_streams()
    assert world.run("validate-config") == EXIT_FAILURE


def test_the_rendered_document_loads_back_as_what_went_in(tmp_path: Path) -> None:
    """The support bundle carries a rendered copy of the configuration.

    A list rendered as ``"['survival_sleep']"`` parses, validates as the wrong
    type, and makes the bundle's copy disagree with the user's file — which is
    worse than not carrying it, because a reader would debug the wrong document.
    """
    world = make_world(tmp_path)
    _configure(world, _switch_off(SUBJECT))

    workspace = resolve_workspace(world.ctx)
    parsed = load_config(workspace.config_path)
    assert parsed.config is not None, [problem.render() for problem in parsed.errors]

    rendered = tmp_path / "rendered.toml"
    rendered.write_text(parsed.config.to_toml(), encoding="utf-8")
    round_tripped = load_config(rendered)

    assert round_tripped.config is not None, [p.render() for p in round_tripped.errors]
    assert round_tripped.config.get("safety", "disabled_capabilities") == [SUBJECT]
