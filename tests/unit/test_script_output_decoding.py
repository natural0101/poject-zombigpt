"""A control-plane script must not decode a child's output with the host's locale.

``subprocess.run(..., text=True)`` decodes the pipe with ``locale.getencoding()``.
That is UTF-8 on this container and cp1252 on a stock Windows runner, and every
file in this repository is UTF-8 with a great deal of Russian in it. So
``scripts/audit_pass.py``, whose ``_path_at`` reads whole files out of git
history, worked here and could not run at all on Windows: ``git show
<sha>:CHANGELOG.md`` raised ``UnicodeDecodeError: 'charmap' codec can't decode
byte 0x81`` inside subprocess's *reader thread*, a hundred times over, and took
the release build red at ``b350572``. Ten tests failed and one errored with a
hundred sub-exceptions — not a check reporting a problem, a gate that could not
execute.

The defect is not Windows-specific and this file does not need Windows to find
it. ``locale.getencoding()`` answers ASCII under ``LC_ALL=C``, so the same
decode raises here, on Linux, in one subprocess:

    LC_ALL=C python -c "subprocess.run(['git','show','HEAD:CHANGELOG.md'],text=True)"
    UnicodeDecodeError: 'ascii' codec can't decode byte 0xe2 in position 262

Which is what the tests below do. They run the real scripts as real processes
under an ASCII locale, so what is asserted is the behaviour that broke rather
than a property of the source. The source is asked too, in one test, because a
new script added next month is easier to catch by shape than by waiting for the
next red Windows build — but that one is the supporting check, not the load
bearing one, and it is proved against a planted call site.

``errors="replace"`` rather than ``strict`` is deliberate and is also asserted:
every reader here searches the text for an ASCII marker, and a gate that dies on
one undecodable byte in one file is a gate that gets switched off.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT))

from scripts._process import ENCODING, ERRORS, run_text  # noqa: E402

SCRIPTS: Final = REPO_ROOT / "scripts"

#: An environment whose preferred encoding is ASCII. ``PYTHONUTF8`` and
#: ``PYTHONCOERCECLOCALE`` are switched off explicitly: either one on its own
#: would rescue the child and make every assertion below vacuous.
_ASCII_LOCALE: Final = {
    "LC_ALL": "C",
    "LANG": "C",
    "LANGUAGE": "C",
    "PYTHONUTF8": "0",
    "PYTHONCOERCECLOCALE": "0",
}

#: A tracked file with bytes outside ASCII in it. The failure needs one, and
#: picking it here means a failure names a file rather than a mystery.
_NON_ASCII_FILE: Final = "CHANGELOG.md"


def _ascii_env() -> dict[str, str]:
    return {**os.environ, **_ASCII_LOCALE}


def _in_ascii_locale(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a python command under an ASCII locale, reading its output as UTF-8."""
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_ascii_env(),
        check=False,
        timeout=600,
    )


# ---------------------------------------------------------------------------
# the failure itself, reproduced here
# ---------------------------------------------------------------------------


def test_the_repository_really_has_bytes_that_ascii_cannot_decode() -> None:
    """Without this the reproduction below would pass for the wrong reason."""
    raw = (REPO_ROOT / _NON_ASCII_FILE).read_bytes()

    assert raw, f"{_NON_ASCII_FILE} is empty"
    with pytest.raises(UnicodeDecodeError):
        raw.decode("ascii")


def test_the_ambient_encoding_does_not_deliver_the_content() -> None:
    """The control: ``text=True`` with nothing pinning it must still fail.

    Without this, a future Python that made ``text=True`` mean UTF-8 everywhere
    would leave every assertion in this file proving nothing.

    **What "fail" means is not the same on both platforms**, and getting that
    wrong is what made this test itself the one red test on the Windows runner
    after the fix landed (``6794dd4``: ``1 failed, 8585 passed``). It asserted a
    non-zero exit, which is true on POSIX and false on Windows:

    * On POSIX ``_communicate`` decodes on the calling thread, so the
      ``UnicodeDecodeError`` propagates and the process dies.
    * On Windows it decodes in ``_readerthread``. The exception is raised
      *there*, printed by the threading excepthook, and never reaches the
      caller — ``subprocess.run`` returns a ``CompletedProcess`` with
      ``returncode`` 0 and **empty output**. The traceback in the release log
      shows exactly that frame.

    The Windows shape is the more dangerous of the two and is the real reason
    this matters beyond a red build: ``audit_pass._path_at`` would have returned
    ``""`` rather than a file's content, ``_defines("", node)`` answers False on
    an empty string, and the audit would have accused tests that were plainly
    there. Measured rather than reasoned — patching ``_git`` to return an empty
    ``stdout`` for ``git show``, exactly as the reader thread leaves it, makes
    the audit report **82 invalid claims out of 400**, every one of them
    fabricated, with no error anywhere. Under pytest it surfaced at all only
    because pytest turns an unraisable thread exception into one;
    ``scripts/check.sh`` runs the script directly, where the run would simply
    have exited 1 with a list of 82 tasks to go and investigate.

    So what is asserted here is the thing both platforms share: the decode
    raises, and the content does not arrive.
    """
    expected = len((REPO_ROOT / _NON_ASCII_FILE).read_text(encoding="utf-8"))
    ambient = _in_ascii_locale(
        "-c",
        "import subprocess,sys;"
        f"r=subprocess.run(['git','show','HEAD:{_NON_ASCII_FILE}'],"
        "capture_output=True,text=True,check=False);"
        "sys.stderr.write('DELIVERED %d\\n' % len(r.stdout))",
    )

    assert "UnicodeDecodeError" in ambient.stderr, (
        "the ambient decode no longer fails; this file is moot"
    )
    delivered = [
        int(line.split()[1]) for line in ambient.stderr.splitlines() if line.startswith("DELIVERED")
    ]
    assert delivered != [expected], (
        f"the ambient decode delivered all {expected} characters despite raising"
    )


def test_the_pinned_decoder_survives_the_same_locale() -> None:
    """And the fix, over the same bytes in the same environment."""
    pinned = _in_ascii_locale(
        "-c",
        "import sys;sys.path.insert(0,'.');"
        "from scripts._process import run_text;"
        f"r=run_text(['git','show','HEAD:{_NON_ASCII_FILE}']);"
        "print('decoded', len(r.stdout))",
    )

    assert pinned.returncode == 0, pinned.stderr
    assert "decoded" in pinned.stdout


@pytest.mark.parametrize(
    "argv",
    [
        ("scripts/audit_pass.py", "--quiet"),
        ("scripts/check_master_plan.py",),
        ("scripts/master_report.py", "--json"),
    ],
)
def test_each_control_plane_gate_runs_under_an_ascii_locale(argv: tuple[str, ...]) -> None:
    """End to end: the commands ``check.sh`` runs, in the environment that broke one.

    Measured, not assumed: with the fix reverted, only ``audit_pass.py`` fails
    here. It is the one that reads whole *files* out of history, so it is the
    one whose output carries Russian prose. ``check_master_plan.py`` and
    ``master_report.py`` shell out for SHAs and path lists, which are ASCII in
    this repository today — their bug is latent, and a filename with a non-ASCII
    character would wake it. That is precisely why the source check below is
    here as well: a behavioural test can only fire for the sites whose output
    happens to be non-ASCII right now, and "happens to be" is not a property to
    rest a gate on.
    """
    result = _in_ascii_locale(*argv)

    assert "UnicodeDecodeError" not in result.stderr, result.stderr[-2000:]
    assert result.returncode == 0, result.stdout + result.stderr


def test_an_audit_reading_empty_git_output_accuses_the_innocent() -> None:
    """The Windows shape, held as a fact rather than as a paragraph.

    ``git show`` returning nothing is what a reader-thread decode failure leaves
    behind, and it is indistinguishable — to every line of ``audit_pass`` — from
    a file that is genuinely empty. This pins the consequence so the number in
    the docstring above is checked rather than remembered, and so that anyone
    tempted to give ``_path_at`` a "just return what we got" fallback sees what
    that costs.
    """
    import yaml  # noqa: PLC0415
    from scripts import audit_pass  # noqa: PLC0415

    document = yaml.safe_load(
        (REPO_ROOT / "docs" / "control" / "MASTER_PLAN.yaml").read_text(encoding="utf-8")
    )
    real = audit_pass._git

    def content_lost(*arguments: str) -> subprocess.CompletedProcess[str]:
        result = real(*arguments)
        if arguments and arguments[0] == "show":
            result.stdout = ""
        return result

    audit_pass._git = content_lost
    try:
        invalid = [verdict for verdict in audit_pass.audit(document) if not verdict.valid]
    finally:
        audit_pass._git = real

    assert invalid, "losing every file's content produced no accusation at all"
    assert len(invalid) > 50, f"only {len(invalid)} accusation(s); the simulation is not biting"
    # And with the content arriving, none of them stands.
    assert [v for v in audit_pass.audit(document) if not v.valid] == []


# ---------------------------------------------------------------------------
# the decoder itself
# ---------------------------------------------------------------------------


def test_the_decoder_replaces_an_undecodable_byte_instead_of_raising() -> None:
    """A gate that dies on one byte is a gate that gets switched off."""
    assert ENCODING == "utf-8"
    assert ERRORS == "replace"

    result = run_text(
        [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'ok\\xff\\xfedone')"],
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("ok")
    assert result.stdout.endswith("done")
    assert "�" in result.stdout, "the bad bytes vanished rather than being marked"


def test_the_decoder_reads_utf8_whatever_the_host_prefers() -> None:
    written = "не ASCII — ни байта"
    # The child writes the bytes itself, from an ASCII-only source line. Passing
    # the text on argv instead fails earlier and for another reason: an ASCII
    # locale cannot decode a non-ASCII *command line* either, which is an input
    # problem and not the output problem this file is about.
    program = (
        f"import sys;sys.stdout.buffer.write({written.encode('utf-8')!r});sys.stdout.buffer.flush()"
    )
    result = run_text([sys.executable, "-c", program], cwd=REPO_ROOT, env=_ascii_env())

    assert result.returncode == 0, result.stderr
    assert result.stdout == written


# ---------------------------------------------------------------------------
# and no script goes back to the ambient encoding
# ---------------------------------------------------------------------------


def _ambient_call_sites(source: str) -> list[int]:
    """Line numbers of text-mode subprocess calls that name no encoding."""
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if ast.unparse(node.func) not in {
            "subprocess.run",
            "subprocess.Popen",
            "subprocess.check_output",
        }:
            continue
        keywords = {keyword.arg for keyword in node.keywords}
        if ("text" in keywords or "universal_newlines" in keywords) and "encoding" not in keywords:
            found.append(node.lineno)
    return found


def test_no_script_decodes_a_child_with_the_hosts_locale() -> None:
    offenders = {
        path.relative_to(REPO_ROOT).as_posix(): lines
        for path in sorted(SCRIPTS.glob("*.py"))
        if (lines := _ambient_call_sites(path.read_text(encoding="utf-8")))
    }

    assert offenders == {}


def test_that_check_can_see_a_call_site(tmp_path: Path) -> None:
    """The pattern, tested against itself in both directions.

    A source check that matches nothing is indistinguishable from a clean tree,
    and this repository has already retracted two documents written on the
    strength of what a pattern could not see.
    """
    ambient = "import subprocess\nsubprocess.run(['git'], capture_output=True, text=True)\n"
    pinned = (
        "import subprocess\n"
        "subprocess.run(['git'], capture_output=True, text=True, encoding='utf-8')\n"
    )

    assert _ambient_call_sites(ambient) == [2]
    assert _ambient_call_sites(pinned) == []


def test_every_script_that_runs_a_child_was_actually_examined() -> None:
    """Counts the sites, so "no offenders" cannot mean "nothing was read"."""
    total = sum(
        1
        for path in SCRIPTS.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) in {"subprocess.run", "subprocess.Popen"}
    )

    assert total >= 9, f"only {total} subprocess call(s) found in scripts/"
