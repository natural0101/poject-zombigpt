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


@pytest.mark.parametrize("document", INSTRUCTIONS, ids=lambda path: path.name)
def test_every_instruction_document_shows_the_per_scenario_collect(document: Path) -> None:
    text = document.read_text(encoding="utf-8")

    assert PER_SCENARIO in text, (
        f"{document.name} never shows `{PER_SCENARIO}`, so an operator following it "
        "has no reason to collect before the end of the day — and by then "
        "console.txt has been rewritten and the early scenarios have to be run again"
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
