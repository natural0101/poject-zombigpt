"""The forbidden-pattern gate, and the claims two governing documents make about it.

``AGENTS.md`` and ``CONTRIBUTING.md`` both list what ``scripts/check_forbidden.py``
rejects, and both are binding — AGENTS.md says so in its first line. Neither had
a test. One of the listed rules did not exist: both documents said an empty
exception handler fails the build, and the scanner had no such check, for
exactly the exception style this codebase writes.

So this file does two things. It exercises each rule over a planted snippet,
which is the only way to know a scanner still fires. And it pins the *documents*
to the scanner, so the next rule that gets written into AGENTS.md without being
implemented fails here instead of being believed for months.

The handler rules are the interesting pair, and they are not symmetrical:

* ``except:`` with no type is always wrong here — it swallows
  ``KeyboardInterrupt`` and ``SystemExit`` — so the scanner rejects it.
* ``except SomeError: pass`` is *not* always wrong. This tree has one that falls
  through to a second lookup and several ``except OSError: return`` that
  deliberately trade a diagnostic for a session in flight. A scanner cannot tell
  those from a swallowed failure, so AGENTS.md states it as a review rule and
  says why. This file asserts the scanner leaves them alone, which is the half
  that would otherwise be quietly tightened into a checker nobody trusts.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO_ROOT / "scripts" / "check_forbidden.py"


def _checker() -> Any:
    """The script, imported as a module. It is not on the import path."""
    spec = importlib.util.spec_from_file_location("check_forbidden", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_forbidden"] = module
    spec.loader.exec_module(module)
    return module


def _scan(tmp_path: Path, source: str) -> list[str]:
    """Rule names reported for one planted file."""
    planted = tmp_path / "planted.py"
    planted.write_text(source, encoding="utf-8")
    return [finding.rule for finding in _checker().check_python(planted)]


# ---------------------------------------------------------------------------
# each rule the documents name
# ---------------------------------------------------------------------------


def test_an_untyped_except_is_rejected(tmp_path: Path) -> None:
    """It catches KeyboardInterrupt and SystemExit, which is never intended."""
    kinds = _scan(
        tmp_path, "def read(p):\n    try:\n        return 1\n    except:\n        return 0\n"
    )

    assert "bare-except" in kinds


def test_a_typed_handler_that_falls_through_is_left_alone(tmp_path: Path) -> None:
    """The construct this tree uses on purpose, and the reason the rule is narrow.

    ``read_commit`` tries a loose ref, passes on failure, and reads packed-refs
    next. A scanner that rejected this would be rejecting correct code, and the
    usual result is a check somebody switches off.
    """
    kinds = _scan(
        tmp_path,
        "def read(p):\n"
        "    try:\n"
        "        return loose(p)\n"
        "    except OSError:\n"
        "        pass\n"
        "    return packed(p)\n",
    )

    assert kinds == [], kinds


def test_a_guarded_swallow_with_its_reasoning_is_left_alone(tmp_path: Path) -> None:
    """``except OSError: return`` in a diagnostic writer is deliberate here."""
    kinds = _scan(
        tmp_path,
        "def log(record):\n"
        "    try:\n"
        "        write(record)\n"
        "    except OSError:\n"
        "        # Losing a log line is never a reason to end a session.\n"
        "        return\n",
    )

    assert kinds == [], kinds


def test_a_bare_pass_function_body_is_rejected(tmp_path: Path) -> None:
    kinds = _scan(tmp_path, "def unfinished():\n    pass\n")

    assert "stub-body" in kinds


def test_a_not_implemented_error_is_rejected(tmp_path: Path) -> None:
    kinds = _scan(tmp_path, "def unfinished():\n    raise NotImplementedError\n")

    assert "stub-body" in kinds


def test_a_protocol_method_may_be_empty(tmp_path: Path) -> None:
    """The exemption that makes the rule usable: a Protocol body is the point."""
    kinds = _scan(
        tmp_path,
        "from typing import Protocol\n\n\nclass Port(Protocol):\n    def read(self) -> int: ...\n",
    )

    assert kinds == [], kinds


def test_a_todo_marker_is_rejected(tmp_path: Path) -> None:
    kinds = _scan(tmp_path, "def done():\n    return 1  # TODO: finish this\n")

    assert "stub-marker" in kinds


def test_a_process_memory_primitive_is_rejected_however_it_is_reached(
    tmp_path: Path,
) -> None:
    """The anti-cheat floor: every route to the primitive ends in its name.

    Both spellings — the bare name a ``from ctypes import`` would produce and
    the attribute a ``kernel32.`` handle would — because banning one and not
    the other is a rule with a documented bypass.
    """
    as_name = _scan(tmp_path, "def f(h):\n    return WriteProcessMemory(h, 0, b'', 0)\n")
    as_attr = _scan(tmp_path, "def f(k, h):\n    return k.CreateRemoteThread(h)\n")

    assert "process-tampering" in as_name
    assert "process-tampering" in as_attr


def test_ctypes_outside_the_audited_probe_is_rejected(tmp_path: Path) -> None:
    kinds = _scan(tmp_path, "import ctypes\n")

    assert "ctypes-outside-the-probe" in kinds


@pytest.mark.parametrize(
    "source",
    [
        "def f(s):\n    return eval(s)\n",
        "def f(s):\n    exec(s)\n",
        "def f(s):\n    return compile(s, '<s>', 'exec')\n",
    ],
    ids=["eval", "exec", "compile"],
)
def test_an_arbitrary_code_execution_call_is_rejected(source: str, tmp_path: Path) -> None:
    """The LLM boundary must not be able to reach a code-execution primitive.

    These rules existed and were never exercised: no planted snippet, and the
    real-tree scan filtered them out. A rule nobody has ever seen fire is a
    claim, not a gate.
    """
    kinds = _scan(tmp_path, source)

    assert "banned-call" in kinds


@pytest.mark.parametrize(
    "source",
    [
        "import os\n\n\ndef f(c):\n    return os.system(c)\n",
        "import os\n\n\ndef f(c):\n    return os.popen(c)\n",
        "import pickle\n\n\ndef f(b):\n    return pickle.loads(b)\n",
        "import pickle\n\n\ndef f(fh):\n    return pickle.load(fh)\n",
        "import subprocess\n\n\ndef f(c):\n    return subprocess.getoutput(c)\n",
    ],
    ids=["os.system", "os.popen", "pickle.loads", "pickle.load", "subprocess.getoutput"],
)
def test_a_shell_or_unpickling_attribute_call_is_rejected(source: str, tmp_path: Path) -> None:
    """`pickle.loads` is the one that matters most: the transport's whole
    security argument is that no code path in the shipped tree unpickles."""
    kinds = _scan(tmp_path, source)

    assert "banned-call" in kinds


def test_a_subprocess_call_with_shell_true_is_rejected(tmp_path: Path) -> None:
    kinds = _scan(
        tmp_path, "import subprocess\n\n\ndef f(c):\n    return subprocess.run(c, shell=True)\n"
    )

    assert "banned-call" in kinds


def test_the_shipped_tree_is_clean_under_the_full_rule_set() -> None:
    """The sweep `main()` runs in CI, run here where deleting it fails a test.

    The previous real-tree scan filtered findings to the process-tampering and
    ctypes rules, so the eval/exec/pickle/os.system rules were never exercised
    against the shipped tree by any test — a scanner change that broke them
    would have stayed green. This sweep keeps *no* rule filtered: whatever
    `check_python` can find, the shipped tree must be free of.
    """
    checker = _checker()
    findings = [
        finding
        for shipped in checker._iter_files(checker.SHIPPED_ROOTS, (".py",))
        for finding in checker.check_python(shipped)
    ]

    assert findings == [], [finding.render() for finding in findings]


def test_the_one_audited_ctypes_use_is_the_one_the_allowlist_names() -> None:
    """The exemption is exactly the descriptor's liveness probe, nothing more.

    Scanning the real shipped tree keeps the allowlist honest in both
    directions: an entry for a file with no ``ctypes`` left in it is a hole
    waiting to be used, and a new import anywhere else must fail the gate
    rather than widen it.
    """
    checker = _checker()
    for allowed in checker.CTYPES_ALLOWED_IN:
        source = (REPO_ROOT / allowed).read_text(encoding="utf-8")
        assert "import ctypes" in source, f"{allowed} no longer uses ctypes; drop the entry"
    findings = [
        finding
        for shipped in checker._iter_files(checker.SHIPPED_ROOTS, (".py",))
        for finding in checker.check_python(shipped)
        if finding.rule in {"process-tampering", "ctypes-outside-the-probe"}
    ]
    assert findings == [], [finding.render() for finding in findings]


# ---------------------------------------------------------------------------
# the documents against the scanner
# ---------------------------------------------------------------------------

#: What each governing document promises, and the snippet that proves it. The
#: point is the *pairing*: a rule added to a document without a snippet here has
#: nothing standing behind it, which is the state "no empty except" was in.
_PROMISED: Final[tuple[tuple[str, str], ...]] = (
    ("`TODO`", "x = 1  # TODO: later\n"),
    ("bare `pass`", "def f():\n    pass\n"),
    ("`NotImplementedError`", "def f():\n    raise NotImplementedError\n"),
    (
        "`except:` without an exception type",
        "def f():\n    try:\n        g()\n    except:\n        pass\n",
    ),
)


@pytest.mark.parametrize(("phrase", "source"), _PROMISED, ids=[p for p, _ in _PROMISED])
def test_what_the_documents_promise_is_what_the_scanner_does(
    phrase: str, source: str, tmp_path: Path
) -> None:
    """Both directions: the documents name it, and the scanner rejects it."""
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    naked = phrase.replace("`", "")

    assert naked in agents.replace("`", ""), f"AGENTS.md no longer names {phrase}"
    assert naked in contributing.replace("`", ""), f"CONTRIBUTING.md no longer names {phrase}"
    assert _scan(tmp_path, source) != [], f"both documents promise {phrase} is rejected; it is not"


def test_neither_document_claims_a_swallowed_handler_is_scanned() -> None:
    """The claim that was false for months, asserted absent rather than assumed.

    Both files said an "empty except" or "empty handler" fails the build. The
    wording is what a contributor relies on, so the wording is what is checked:
    if it comes back, this fails, and whoever brings it back has to implement it
    or say why it cannot be implemented.
    """
    for name in ("AGENTS.md", "CONTRIBUTING.md"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8").lower()
        for claim in ("no empty except", "no empty handler"):
            assert claim not in text, (
                f"{name} says {claim!r} again; check_forbidden.py does not do that, and "
                "AGENTS.md explains why it cannot be done honestly"
            )
