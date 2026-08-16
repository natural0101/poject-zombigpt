"""Each instruction document must show the per-scenario collect, because waiting loses it.

``finalize`` requires the declared logs of every scenario, passes included, and
those files do not survive the day: ``console.txt`` is rewritten each time the
game launches and the session trace rotates. Logs gathered in the evening are
missing the early scenarios' entirely, and the only remedy is to run those
scenarios again — hours, on a machine this repository cannot reach.

That instruction existed in exactly one of the three documents an operator
follows, and nothing checked it there either. Measured by planting: cutting
``## 4a`` out of ``LOCAL_AGENT_PROMPT.md`` left the whole contract suite green.
The playbook and the handoff each named ``collect-evidence.bat`` only in a
command table, with no word about when to run it.

**What this checks, and what it does not.** The anchor is the per-scenario form
of the command — ``collect-evidence.bat --scenario`` — because that is
mechanical, language-independent (one of the three documents is in Russian) and
is the form the instruction is actionable in. It proves each document shows the
per-scenario call. It does not prove the document explains *why*, which is prose
a reviewer has to read. Anchoring on the explanation would mean inventing a
phrase in two languages that only this file looks for, and a magic string is a
guard that fails the next time someone rewrites a sentence.

The document set is imported from the one place that defines it rather than
listed here. Two guards in this repository have already been scoped to the files
a defect was found in rather than to the fact, and both were caught only after
the fact went unguarded somewhere else.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
WRAPPER: Final = REPO_ROOT / "packaging" / "windows" / "bat" / "collect-evidence.bat"

#: The one definition of "documents an operator follows as instructions".
handoff_tests = importlib.import_module("tests.contract.test_handoff_instructions_match_the_run")
INSTRUCTIONS: Final = handoff_tests.INSTRUCTIONS

#: The per-scenario spelling. Taken as one string rather than as a command plus
#: a flag, because a document that names the wrapper in one paragraph and the
#: flag in another has not told anybody to run them together.
PER_SCENARIO: Final = "collect-evidence.bat --scenario"

#: The phrase each document uses for the half of the rule that the command alone
#: cannot carry: logs are owed by the scenarios that *passed*, not only by the
#: ones that failed.
#:
#: A per-document table because the three documents are not in one language, and
#: its completeness is asserted against the derived instruction set below rather
#: than trusted — a mapping that silently missed a document would be this
#: guard's third scoping failure.
#:
#: The first version of this file checked only :data:`PER_SCENARIO`, and that was
#: not enough: ``LOCAL_AGENT_PROMPT.md`` spells the command twice, once in the
#: timing rule and once in "what to do after a FAIL". Deleting the timing rule
#: left the other occurrence, and the guard stayed green — found by deleting the
#: section rather than by replacing every occurrence of the string, which is what
#: the original plant did and why it looked convincing.
ALSO_FOR_PASSES: Final = {
    "LOCAL_AGENT_PROMPT.md": "включая прошедшие",
    "LIVE_TEST_PLAYBOOK.md": "passes included",
    "LOCAL_GAME_HANDOFF.md": "passes included",
}


def test_the_phrase_table_covers_every_instruction_document() -> None:
    """A document with no entry would be checked for the command and nothing else."""
    assert {path.name for path in INSTRUCTIONS} == set(ALSO_FOR_PASSES)


@pytest.mark.parametrize("document", INSTRUCTIONS, ids=lambda path: path.name)
def test_every_instruction_document_shows_the_per_scenario_collect(document: Path) -> None:
    text = document.read_text(encoding="utf-8")

    assert PER_SCENARIO in text, (
        f"{document.name} never shows `{PER_SCENARIO}`, so an operator following it "
        "has no reason to collect before the end of the day — and by then "
        "console.txt has been rewritten and the early scenarios have to be run again"
    )
    owed = ALSO_FOR_PASSES[document.name]
    assert owed in text, (
        f"{document.name} shows the per-scenario command without saying that the "
        f"scenarios which passed owe their logs too («{owed}»). Collecting "
        "only after a failure loses every passing scenario's console.txt, and "
        "finalize refuses the run for exactly those"
    )


def test_the_wrapper_really_takes_that_flag() -> None:
    """The other half: an instruction is only worth checking if it runs.

    Without this the three tests above could agree on a spelling the wrapper
    does not accept, and every operator following them would meet a usage error
    instead of a collection.
    """
    wrapper = WRAPPER.read_text(encoding="utf-8")

    assert "--scenario" in wrapper, f"{WRAPPER.name} does not accept --scenario"


def test_at_least_one_document_says_why_waiting_loses_the_logs() -> None:
    """The reason has to survive somewhere, even though prose cannot be pinned.

    Deliberately weaker than the checks above and deliberately present: if every
    document ended up showing the flag with no explanation anywhere, an operator
    would read it as a convenience rather than as the difference between a
    usable session and a repeated one. ``console.txt`` is the file whose
    rewriting is the whole reason, so its name is the anchor.
    """
    explaining = [
        document.name
        for document in INSTRUCTIONS
        if "console.txt" in document.read_text(encoding="utf-8")
    ]

    assert explaining, (
        "no instruction document mentions console.txt, so nothing explains why "
        "collecting late loses the early scenarios"
    )
