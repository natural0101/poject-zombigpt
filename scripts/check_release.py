#!/usr/bin/env python3
"""The release gate. Two bars, and it will not confuse them.

``--rc`` certifies a *release candidate*: the archive exists, holds everything
it declares, the executables and the eleven wrappers are in it, and a test
report says the suite passed. Nothing about a game.

``--release`` certifies **v1.0.0**, and asks for the one thing an RC does not
have: ``release/evidence-manifest.json``, holding for every scenario S01..S20 a
PASS and a SHA-256 for each artefact that scenario owes.

That file does not exist in this repository, so ``--release`` fails today. It is
supposed to. Nothing here can be satisfied by building, by testing, or by
reading the code carefully; the only way to make it pass is to run every
scenario in the catalogue inside Project Zomboid and let the runner write what it
observed. A
gate that could be satisfied without the game would certify the one thing this
project cannot check for itself.

Two rules the implementation follows, both of them consequences of the
project's honest-success invariant:

* **A claim is checked against the artefact, never accepted from it.** The
  build manifest says which files are in the ZIP and what they hash to; this
  script opens the ZIP and hashes them. The evidence manifest records a digest
  per artefact; where the evidence tree is present, the file is re-hashed.
* **Every failure is reported, not the first one.** An operator who learns one
  missing thing per run learns to treat the gate as an obstacle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from xml.etree import ElementTree

REPO_ROOT: Final = Path(__file__).resolve().parent.parent

# Every package, not only the two this file names directly. Importing the
# scenario catalogue pulls ``pz_agent_cli.app``, which reaches through the CLI's
# voice layer into ``pz_agent_mcp``; with only two paths set up the gate
# reported "the scenario catalogue could not be imported" and advised running it
# from a checkout — to someone already standing in one.
for _package in ("pz_agent_core", "pz_agent_cli", "pz_agent_mcp", "pz_agent_voice"):
    _source = REPO_ROOT / "packages" / _package / "src"
    if _source.is_dir():
        sys.path.insert(0, str(_source))
sys.path.insert(0, str(REPO_ROOT / "packaging" / "windows"))

import build_rc  # noqa: E402  (path set up above)

from pz_agent_core.version import PRODUCT_VERSION  # noqa: E402  (path set up above)

#: What ``pz_agent_cli.livetest.runner`` stamps on a manifest it wrote.
LIVETEST_MANIFEST_FORMAT: Final = "pz-agent/livetest-manifest/1"

#: The JUnit classname prefix of the suite that proves a second process reaches
#: a served core. ``check_tests`` requires it by name: aggregate counters alone
#: cannot tell a full run from one where this suite never ran.
E2E_MODULE: Final = "tests.contract.test_mcp_subprocess_e2e"

DEFAULT_MANIFEST: Final = REPO_ROOT / "release" / "evidence-manifest.json"

DEFAULT_EVIDENCE_ROOT: Final = REPO_ROOT / "evidence"

#: An evidence manifest is a page of JSON per scenario. Past this it is not one.
MAX_MANIFEST_BYTES: Final = 8 * 1024 * 1024

#: Guard on re-hashing an artefact named by a document on disk.
MAX_ARTEFACT_BYTES: Final = 64 * 1024 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_CHUNK_BYTES: Final = 1024 * 1024

EXIT_OK: Final = 0
EXIT_REFUSED: Final = 1
EXIT_USAGE: Final = 2


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing the gate looked at.

    ``remediation`` is required on a failure for the same reason the config
    validator requires it: a gate that says no without saying what would make it
    say yes gets routed around rather than satisfied.
    """

    check: str
    ok: bool
    detail: str
    remediation: str = ""

    def render(self) -> str:
        mark = "ok  " if self.ok else "FAIL"
        line = f"  [{mark}] {self.check}: {self.detail}"
        if self.ok or not self.remediation:
            return line
        return f"{line}\n         -> {self.remediation}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "ok": self.ok,
            "detail": self.detail,
            "remediation": self.remediation,
        }


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    """Hash a file in bounded chunks, enforcing the cap during the read."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            size += len(chunk)
            if size > MAX_ARTEFACT_BYTES:
                raise ValueError(f"exceeds the {MAX_ARTEFACT_BYTES} byte artefact cap")
            digest.update(chunk)
    return digest.hexdigest(), size


def _read_json(path: Path, *, limit: int) -> dict[str, Any]:
    """Read a JSON object from disk, bounded.

    Raises:
        ValueError: on anything that is not a JSON object within *limit*. The
            caller turns it into a finding; a traceback out of a release gate
            reads as a broken gate rather than a refused release.
    """
    raw = path.read_bytes()
    if len(raw) > limit:
        raise ValueError(f"{len(raw)} bytes exceeds the {limit} byte limit")
    try:
        document: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"is not readable JSON ({exc})") from exc
    if not isinstance(document, dict):
        raise ValueError(f"holds a JSON {type(document).__name__}, not an object")
    return document


# ---------------------------------------------------------------------------
# the candidate: the archive and the test report
# ---------------------------------------------------------------------------


def check_archive(archive: Path) -> list[Finding]:
    """The ZIP exists, and holds exactly what its own manifest says it holds."""
    if not archive.is_file():
        return [
            Finding(
                check="archive",
                ok=False,
                detail=f"{archive} does not exist",
                remediation=(
                    "build it: pyinstaller the two specs in packaging/windows, then "
                    "python packaging/windows/build_rc.py"
                ),
            )
        ]
    try:
        with zipfile.ZipFile(archive) as bundle:
            return _check_archive_contents(archive, bundle)
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        return [
            Finding(
                check="archive",
                ok=False,
                detail=f"{archive.name} could not be read: {exc}",
                remediation="rebuild it with packaging/windows/build_rc.py",
            )
        ]


def _check_archive_contents(archive: Path, bundle: zipfile.ZipFile) -> list[Finding]:
    findings: list[Finding] = []
    names = set(bundle.namelist())
    if build_rc.MANIFEST_NAME not in names:
        return [
            Finding(
                check="archive.manifest",
                ok=False,
                detail=f"{archive.name} has no {build_rc.MANIFEST_NAME}",
                remediation=(
                    "rebuild with packaging/windows/build_rc.py; an archive assembled by "
                    "hand carries no record of what is in it"
                ),
            )
        ]
    raw = bundle.read(build_rc.MANIFEST_NAME)
    try:
        manifest: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [
            Finding(
                check="archive.manifest",
                ok=False,
                detail=f"{build_rc.MANIFEST_NAME} is not readable JSON ({exc})",
                remediation="rebuild with packaging/windows/build_rc.py",
            )
        ]
    if not isinstance(manifest, dict) or manifest.get("format") != build_rc.MANIFEST_FORMAT:
        return [
            Finding(
                check="archive.manifest",
                ok=False,
                detail=(f"{build_rc.MANIFEST_NAME} is not in format {build_rc.MANIFEST_FORMAT!r}"),
                remediation="rebuild with the packaging in this checkout",
            )
        ]

    findings.append(
        Finding(
            check="archive",
            ok=True,
            detail=f"{archive.name}, {len(names)} entr(ies), sha256 {_archive_digest(archive)}",
        )
    )
    findings.append(_completeness(manifest))
    findings.append(_archive_release(manifest))
    findings.append(_evidence_not_claimed(manifest))
    findings.extend(_required_entries(names))
    findings.append(_recorded_digests(bundle, manifest, names))
    return findings


def _archive_release(manifest: dict[str, Any]) -> Finding:
    """The archive must be the release this gate says it certified.

    The headline of a successful run is ``CERTIFIED v1.0.0-rc1``, and that string
    is built from ``build_rc.RELEASE_VERSION`` — the *checkout's* constant. It was
    the one claim in the output derived from neither the artefact nor a check of
    it, in a file whose stated rule is: *"A claim is checked against the artefact,
    never accepted from it."* The archive records its own ``release_version`` and
    nothing read it.

    Scope, stated plainly rather than dressed up: ``docs/control/DECISIONS.md``
    D-012 records that the gate runs in the workflow that built, so in the real
    release path the archive's constant and the checkout's are the same object
    moments apart, and this can never fire there. It is a tightening, not a
    reachable false success — the value is that the headline now says something
    the artefact agrees with, and that an archive examined outside that workflow
    is refused rather than relabelled.
    """
    declared = str(manifest.get("release_version", "")).strip()
    if not declared:
        return Finding(
            check="archive.release",
            ok=False,
            detail=f"{build_rc.MANIFEST_NAME} records no release_version",
            remediation=(
                "rebuild with packaging/windows/build_rc.py; an archive that does not "
                "say which release it is cannot be certified as one"
            ),
        )
    if declared != build_rc.RELEASE_VERSION:
        return Finding(
            check="archive.release",
            ok=False,
            detail=(
                f"the archive says it is release {declared} and this checkout builds "
                f"{build_rc.RELEASE_VERSION}"
            ),
            remediation=(
                "certify the archive this checkout built. Relabelling another one would "
                "put this gate's verdict on an artefact it never examined the build of"
            ),
        )
    return Finding(
        check="archive.release",
        ok=True,
        detail=f"the archive declares release {declared}, which is what this checkout builds",
    )


def _archive_digest(archive: Path) -> str:
    digest, _ = _sha256_file(archive)
    return digest


def _completeness(manifest: dict[str, Any]) -> Finding:
    missing = [
        item
        for component in manifest.get("components", [])
        if isinstance(component, dict)
        for item in component.get("missing", [])
    ]
    if manifest.get("complete") is True and not missing:
        return Finding(
            check="archive.complete",
            ok=True,
            detail="every declared component is in the archive",
        )
    return Finding(
        check="archive.complete",
        ok=False,
        detail=(
            f"the archive declares {len(missing)} missing file(s): "
            f"{', '.join(str(item) for item in missing)}"
        ),
        remediation=(
            "build the missing components and run packaging/windows/build_rc.py again; "
            "the executables need a Windows machine with PyInstaller"
        ),
    )


def _evidence_not_claimed(manifest: dict[str, Any]) -> Finding:
    """A build must not claim live evidence. Only a live run can produce it."""
    if manifest.get("live_evidence_claimed") is False:
        return Finding(
            check="archive.claims",
            ok=True,
            detail="the archive claims no live-test evidence, which is what building one can prove",
        )
    return Finding(
        check="archive.claims",
        ok=False,
        detail="the build manifest claims live evidence",
        remediation=(
            "packaging never observes a game; evidence is written by 'pz-agent live-test' "
            "and checked by --release, not by the build"
        ),
    )


def _required_entries(names: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    absent_bats = [name for name in build_rc.BAT_NAMES if name not in names]
    findings.append(
        Finding(
            check="archive.bat",
            ok=not absent_bats,
            detail=(
                f"all {len(build_rc.BAT_NAMES)} wrappers are at the root"
                if not absent_bats
                else f"missing from the root: {', '.join(absent_bats)}"
            ),
            remediation="the wrappers live in packaging/windows/bat; build_rc.py copies them",
        )
    )
    absent_bins = [name for name in build_rc.BIN_NAMES if f"bin/{name}" not in names]
    findings.append(
        Finding(
            check="archive.bin",
            ok=not absent_bins,
            detail=(
                "both executables are in bin/"
                if not absent_bins
                else f"missing from bin/: {', '.join(absent_bins)}"
            ),
            remediation=(
                "run PyInstaller on packaging/windows/*.spec on Windows, then build_rc.py "
                "--bin-dir with the directory it wrote them to"
            ),
        )
    )
    return findings


def _recorded_digests(
    bundle: zipfile.ZipFile, manifest: dict[str, Any], names: set[str]
) -> Finding:
    """Re-hash what the manifest claims, from the archive itself."""
    entries = [item for item in manifest.get("files", []) if isinstance(item, dict)]
    if not entries:
        return Finding(
            check="archive.digests",
            ok=False,
            detail="the build manifest lists no files",
            remediation="rebuild with packaging/windows/build_rc.py",
        )
    problems: list[str] = []
    for entry in entries:
        path = str(entry.get("path", ""))
        recorded = str(entry.get("sha256", ""))
        if path not in names:
            problems.append(f"{path}: recorded but not in the archive")
            continue
        info = bundle.getinfo(path)
        if info.file_size != entry.get("size_bytes"):
            problems.append(
                f"{path}: {info.file_size} bytes, manifest says {entry.get('size_bytes')}"
            )
            continue
        if _sha256_bytes(bundle.read(path)) != recorded:
            problems.append(f"{path}: content does not match the recorded digest")
    # The other direction: a member the index never recorded. Hashing what the
    # manifest claims proves the claims; it says nothing about a file smuggled
    # in beside them, which no per-entry loop can see.
    recorded_names = {str(entry.get("path", "")) for entry in entries} | {build_rc.MANIFEST_NAME}
    for name in sorted(names - recorded_names):
        problems.append(f"{name}: in the archive but recorded in no manifest entry")
    if problems:
        return Finding(
            check="archive.digests",
            ok=False,
            detail=f"{len(problems)} file(s) disagree with the manifest: {'; '.join(problems[:5])}",
            remediation="rebuild the archive; do not edit one after it has been built",
        )
    return Finding(
        check="archive.digests",
        ok=True,
        detail=f"{len(entries)} file(s) match the digests recorded for them",
    )


def check_tests(report: Path | None) -> list[Finding]:
    """The suite passed — read off a report, never assumed.

    A gate that took "the tests pass" on trust would be certifying the intention
    to run them. The JUnit XML pytest writes is the observable, so it is
    required rather than optional.
    """
    if report is None:
        return [
            Finding(
                check="tests",
                ok=False,
                detail="no test report was given to this gate",
                remediation=(
                    "run the suite and pass its report: "
                    "python -m pytest --junitxml=pytest.xml, then "
                    "check_release.py --rc --junit pytest.xml"
                ),
            )
        ]
    if not report.is_file():
        return [
            Finding(
                check="tests",
                ok=False,
                detail=f"{report} does not exist",
                remediation="run python -m pytest --junitxml=<that path> first",
            )
        ]
    try:
        root = ElementTree.parse(report).getroot()
    except (ElementTree.ParseError, OSError) as exc:
        return [
            Finding(
                check="tests",
                ok=False,
                detail=f"{report.name} is not a readable JUnit report ({exc})",
                remediation="regenerate it with pytest --junitxml",
            )
        ]
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = sum(int(suite.get("tests", "0")) for suite in suites)
    failures = sum(int(suite.get("failures", "0")) for suite in suites)
    errors = sum(int(suite.get("errors", "0")) for suite in suites)
    # JUnit's ``tests`` is everything collected, skips included. Reporting it as
    # "N test(s) passed" overstated the result by however many were skipped, in
    # the one document whose whole job is not to overstate a result.
    skipped = sum(int(suite.get("skipped", "0")) for suite in suites)
    passed = total - failures - errors - skipped
    if not suites or total == 0:
        return [
            Finding(
                check="tests",
                ok=False,
                detail=f"{report.name} reports no tests at all",
                remediation="a report with no tests in it is not evidence that any ran",
            )
        ]
    if failures or errors:
        return [
            Finding(
                check="tests",
                ok=False,
                detail=f"{failures} failure(s) and {errors} error(s) in {total} test(s)",
                remediation="fix them; a release candidate is not cut from a red suite",
            )
        ]
    detail = f"{passed} of {total} test(s) passed, no failures and no errors"
    if skipped:
        detail += f"; {skipped} skipped"
    return [
        Finding(
            check="tests",
            ok=True,
            detail=detail,
        ),
        _e2e_observed(suites),
    ]


def _e2e_observed(suites: Sequence[ElementTree.Element]) -> Finding:
    """The MCP end-to-end suite ran in *this* report, not merely somewhere.

    The aggregate counters above cannot tell a full run from one where the E2E
    suite was deselected or skipped wholesale: a report of six thousand green
    unit tests certifies nothing about whether a packaged client ever reached a
    served core on the platform this gate runs on. So the suite that crosses
    that seam is required by name — its testcases must appear, and none of them
    may be a skip, because the suite has no legitimate skip today and a gate
    that tolerates one is how an all-skip run creeps back in.
    """
    cases = [
        case
        for suite in suites
        for case in suite.iter("testcase")
        if (classname := case.get("classname", "")) == E2E_MODULE
        or classname.startswith(E2E_MODULE + ".")
    ]
    if not cases:
        return Finding(
            check="tests.mcp-e2e",
            ok=False,
            detail=f"no testcase from {E2E_MODULE} appears in the report",
            remediation=(
                "run the full suite on this platform; a report that never "
                "collected tests/contract/test_mcp_subprocess_e2e.py proves "
                "nothing about the served link"
            ),
        )
    e2e_skipped = sum(1 for case in cases if case.find("skipped") is not None)
    if e2e_skipped:
        return Finding(
            check="tests.mcp-e2e",
            ok=False,
            detail=(
                f"{e2e_skipped} of {len(cases)} testcase(s) from {E2E_MODULE} "
                "were skipped rather than run"
            ),
            remediation=(
                "a skipped E2E case observed nothing; make the suite runnable "
                "on this platform before certifying from its report"
            ),
        )
    return Finding(
        check="tests.mcp-e2e",
        ok=True,
        detail=f"{len(cases)} testcase(s) from {E2E_MODULE} ran and passed",
    )


# ---------------------------------------------------------------------------
# the release: live evidence
# ---------------------------------------------------------------------------


def _scenario_ids() -> tuple[tuple[str, ...], Finding | None]:
    """The scenario catalogue, or a finding saying why it could not be read."""
    try:
        from pz_agent_cli.livetest.scenarios import SCENARIO_IDS  # noqa: PLC0415
    except ImportError as exc:
        return (), Finding(
            check="evidence.catalogue",
            ok=False,
            detail=f"the scenario catalogue could not be imported ({exc})",
            remediation=(
                "run this from a checkout of the repository; the gate cannot know what "
                "evidence is owed without the list of scenarios"
            ),
        )
    return tuple(SCENARIO_IDS), None


def check_evidence(manifest_path: Path, evidence_root: Path) -> list[Finding]:
    """The live evidence for v1.0.0: every scenario at PASS, each with hashed artefacts."""
    scenario_ids, problem = _scenario_ids()
    if problem is not None:
        return [problem]
    if not manifest_path.is_file():
        return [
            Finding(
                check="evidence.manifest",
                ok=False,
                detail=f"{manifest_path} does not exist",
                remediation=(
                    "run every scenario in the catalogue against a real Build 42.20 session and "
                    "then "
                    "'pz-agent live-test finalize'. Nothing else produces this file: it is "
                    "the record of what was observed in the game, and no amount of building "
                    "or testing substitutes for it"
                ),
            )
        ]
    try:
        manifest = _read_json(manifest_path, limit=MAX_MANIFEST_BYTES)
    except (OSError, ValueError) as exc:
        return [
            Finding(
                check="evidence.manifest",
                ok=False,
                detail=f"{manifest_path.name} {exc}",
                remediation="rebuild it with 'pz-agent live-test finalize'; do not edit it",
            )
        ]
    if manifest.get("format") != LIVETEST_MANIFEST_FORMAT:
        return [
            Finding(
                check="evidence.manifest",
                ok=False,
                detail=(
                    f"{manifest_path.name} is in format {manifest.get('format')!r}, "
                    f"not {LIVETEST_MANIFEST_FORMAT!r}"
                ),
                remediation="rebuild it with this build's 'pz-agent live-test finalize'",
            )
        ]

    findings: list[Finding] = [
        Finding(
            check="evidence.manifest",
            ok=True,
            detail=(
                f"{manifest_path.name}, commit {manifest.get('commit', '(unrecorded)')}, "
                f"build(s) {', '.join(str(b) for b in manifest.get('game_builds', [])) or '(none)'}"
            ),
        )
    ]
    findings.append(_manifest_version(manifest))
    findings.append(_manifest_components(manifest))
    findings.append(_scenario_commits(manifest))
    findings.append(_game_builds(manifest))
    findings.extend(_scenario_verdicts(manifest, scenario_ids))
    findings.extend(_artefact_digests(manifest, scenario_ids, evidence_root))
    return findings


def _manifest_components(manifest: dict[str, Any]) -> Finding:
    """The mod and schema versions the evidence was produced by.

    This repository's own changelog opens with the rule: *"Five versions move
    independently — product, protocol, schema, mod and the supported build
    range."* The manifest records three of them, and until this check existed the
    gate compared exactly one. ``_manifest_version`` handles ``product_version``;
    ``game_builds`` covers the supported build range. ``mod_version`` and
    ``schema_version`` were written into the evidence and read by nobody.

    They are not decoration. ``MOD_VERSION`` describes the Lua that runs *inside
    the game* and produces every observation these scenarios are judged on, so
    evidence from another mod version is evidence about other in-game code —
    the same argument the commit and the build already make, applied to the
    component that actually did the observing. ``SCHEMA_VERSION`` describes the
    shape of the observation documents, which postconditions read by dotted
    path; a schema that moved can move a field out from under a check that still
    finds something there.

    Compared against this checkout rather than against a release target: neither
    is the number being tagged, so the question is whether the evidence describes
    the code being released, which is what the checkout declares.
    """
    from pz_agent_core.version import MOD_VERSION, SCHEMA_VERSION  # noqa: PLC0415

    expected = {"mod_version": MOD_VERSION, "schema_version": SCHEMA_VERSION}
    missing = sorted(key for key in expected if not str(manifest.get(key, "")).strip())
    if missing:
        return Finding(
            check="evidence.components",
            ok=False,
            detail=f"the manifest records no {', '.join(missing)}",
            remediation=(
                "rebuild the manifest with 'pz-agent live-test finalize'; evidence that "
                "does not say which mod and schema produced it cannot be matched to the "
                "code being released"
            ),
        )

    disagreeing = sorted(
        f"{key} {str(manifest.get(key)).strip()} against {value}"
        for key, value in expected.items()
        if str(manifest.get(key)).strip() != value
    )
    if disagreeing:
        return Finding(
            check="evidence.components",
            ok=False,
            detail="the evidence was produced by " + "; ".join(disagreeing) + " in this checkout",
            remediation=(
                "re-run the scenarios against the mod and schema being released. The mod "
                "is the code that ran inside the game and did the observing, so evidence "
                "from another one is evidence about that one"
            ),
        )
    return Finding(
        check="evidence.components",
        ok=True,
        detail=f"the evidence was produced by mod {MOD_VERSION}, schema {SCHEMA_VERSION}",
    )


def _game_builds(manifest: dict[str, Any]) -> Finding:
    """The evidence must name the game it ran against, and it must be a supported one.

    The runner already states the rule, on the constant it records when nobody
    read a build off a running session: *"Not a guess at the supported build:
    evidence that cannot name the game it ran against closes nothing."* Nothing
    enforced it. ``game_build`` appeared in this file exactly once, interpolated
    into a detail line, and no code anywhere compared the evidence's build to
    ``SUPPORTED_BUILDS``.

    Both halves are reachable. Twenty-one of the twenty-two scenarios declare no
    postcondition about the build at all, so they reach PASS with ``game_build``
    unset and the result records ``(not observed)``. The twenty-second,
    ``S01_INSTALL``, asks only that the field be ``observed`` — measured, it
    passes on ``"41.78"`` and on ``"banana"``. So a full manifest could carry
    ``game_builds: ["(not observed)"]`` and this gate would print it and certify
    ``v1.0.0``.

    ``UNOBSERVED_BUILD`` is imported rather than re-spelled here: a second copy
    of that string is precisely the drift this project keeps finding, and a
    checker that agrees with its own spelling of a constant checks nothing.
    """
    try:
        from pz_agent_cli.livetest.runner import UNOBSERVED_BUILD  # noqa: PLC0415
        from pz_agent_core.version import SUPPORTED_BUILDS  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - the catalogue import fails first
        return Finding(
            check="evidence.game_build",
            ok=False,
            detail=f"the supported builds could not be imported ({exc})",
            remediation="run this from a checkout of the repository",
        )

    builds = [str(entry).strip() for entry in manifest.get("game_builds", []) if str(entry).strip()]
    if not builds:
        return Finding(
            check="evidence.game_build",
            ok=False,
            detail="the manifest names no game build",
            remediation=(
                "re-run the scenarios recording the build the session reported, and "
                "finalize again; evidence that cannot name the game it ran against "
                "closes nothing"
            ),
        )

    unsupported = sorted(build for build in builds if build not in SUPPORTED_BUILDS)
    if unsupported:
        unobserved = [build for build in unsupported if build == UNOBSERVED_BUILD]
        return Finding(
            check="evidence.game_build",
            ok=False,
            detail=(
                f"the evidence names build(s) {', '.join(unsupported)}, and this release "
                f"supports {', '.join(SUPPORTED_BUILDS)}"
            ),
            remediation=(
                "re-run the scenarios against a supported build, recording what the "
                "session reported"
                + (
                    ". At least one result records that no build was read at all, which "
                    "is the runner saying nobody looked"
                    if unobserved
                    else ""
                )
            ),
        )
    return Finding(
        check="evidence.game_build",
        ok=True,
        detail=f"the evidence was produced against build(s) {', '.join(sorted(builds))}",
    )


def _scenario_commits(manifest: dict[str, Any]) -> Finding:
    """Every scenario's evidence must be about the code being released.

    ``_manifest_version`` one function down already states the principle in its
    remediation — *"evidence from a different build is evidence about that
    build"* — and enforces it on the **version string**. That string is a single
    literal that does not move for hundreds of commits, so it cannot tell one
    build from another inside a release series. The commit can, and until this
    check existed nothing compared it: the manifest's ``commit`` was printed in a
    detail line and the per-scenario commits were not in the document at all.

    That gap has teeth because of how the ledger derives a verdict: *PASS if any
    attempt passed*. A scenario that passed at one commit keeps reporting PASS
    after the code moves — deliberately, so a re-run cannot erase a real result —
    and the campaign producing this evidence is expected to move it. ``E14-M04``
    is, in as many words, "record the incompatibilities the run finds, fix each,
    re-run every scenario a fix touches". So the natural end of a week of live
    testing is a manifest whose scenarios passed at several commits, and nothing
    would have said so before ``CERTIFIED v1.0.0``.

    The rule is agreement, not equality with HEAD: ``finalize`` stamps the
    manifest with the commit it was generated at, and an operator who ran every
    scenario without changing code gets one value throughout. A disagreement
    names the scenarios to re-run, which is an instruction rather than a wall.
    """
    manifest_commit = str(manifest.get("commit", "")).strip()
    entries = [item for item in manifest.get("scenarios", []) if isinstance(item, dict)]

    unrecorded = sorted(
        str(entry.get("scenario_id", "?")) for entry in entries if not str(entry.get("commit", ""))
    )
    if not manifest_commit or unrecorded:
        missing = ", ".join(unrecorded) if unrecorded else "the manifest itself"
        return Finding(
            check="evidence.commit",
            ok=False,
            detail=f"no commit is recorded for {missing}",
            remediation=(
                "rebuild the manifest with 'pz-agent live-test finalize'. A manifest "
                "written before the commit was recorded cannot say what code its "
                "evidence is about, and that is the question this check exists to ask"
            ),
        )

    elsewhere = sorted(
        {
            f"{entry.get('scenario_id', '?')} at {str(entry.get('commit'))[:8]}"
            for entry in entries
            if str(entry.get("commit")) != manifest_commit
        }
    )
    if elsewhere:
        return Finding(
            check="evidence.commit",
            ok=False,
            detail=(
                f"the manifest was generated at {manifest_commit[:8]} and "
                f"{len(elsewhere)} scenario(s) passed at other commits: " + "; ".join(elsewhere)
            ),
            remediation=(
                "re-run those scenarios against the code being released: "
                "'pz-agent live-test run --scenario <id>' for each, then finalize again. "
                "A pass recorded against different code is evidence about that code"
            ),
        )
    return Finding(
        check="evidence.commit",
        ok=True,
        detail=(
            f"all {len(entries)} scenario(s) passed at {manifest_commit[:8]}, "
            f"the commit the manifest was generated at"
        ),
    )


def _manifest_version(manifest: dict[str, Any]) -> Finding:
    """The evidence must be about the version being released.

    Three versions have to agree: the one the evidence was produced by, the one
    this checkout declares, and the one being tagged. They do not agree today —
    ``PRODUCT_VERSION`` is still the pre-1.0 number — and bumping it is a
    deliberate last step before a tag rather than something to do in passing.
    """
    declared = str(manifest.get("product_version", ""))
    # Annotated rather than compared directly: both constants are ``Final``
    # literals, and a type checker reads the comparison of two literals that
    # differ as dead code rather than as the check it is.
    checkout: str = PRODUCT_VERSION
    target: str = build_rc.RELEASE_VERSION
    if declared == target and checkout == target:
        return Finding(
            check="evidence.version",
            ok=True,
            detail=f"the evidence was produced by product version {declared}",
        )
    return Finding(
        check="evidence.version",
        ok=False,
        detail=(
            f"the evidence names product version {declared or '(none)'}, this checkout "
            f"declares {PRODUCT_VERSION}, and the release is v{build_rc.RELEASE_VERSION}"
        ),
        remediation=(
            "bump version.py and everything that restates it (scripts/check_versions.py "
            f"lists them) to {build_rc.RELEASE_VERSION}, then re-run the scenarios: "
            "evidence from a different build is evidence about that build"
        ),
    )


def _scenario_verdicts(manifest: dict[str, Any], scenario_ids: Sequence[str]) -> list[Finding]:
    verdicts: dict[str, str] = {}
    for entry in manifest.get("scenarios", []):
        if isinstance(entry, dict):
            verdicts[str(entry.get("scenario_id", ""))] = str(entry.get("state", ""))
    not_passed = [
        f"{scenario_id} is {verdicts.get(scenario_id, 'absent from the manifest')}"
        for scenario_id in scenario_ids
        if verdicts.get(scenario_id) != "PASS"
    ]
    if not_passed:
        return [
            Finding(
                check="evidence.scenarios",
                ok=False,
                detail=f"{len(not_passed)} of {len(scenario_ids)} scenario(s) did not pass: "
                + "; ".join(not_passed),
                remediation=(
                    "run each of them with 'pz-agent live-test run --scenario <id>' until its "
                    "postconditions are observed. A scenario cannot be marked passed by hand"
                ),
            )
        ]
    return [
        Finding(
            check="evidence.scenarios",
            ok=True,
            detail=f"all {len(scenario_ids)} scenarios are PASS",
        )
    ]


def _artefact_digests(
    manifest: dict[str, Any], scenario_ids: Sequence[str], evidence_root: Path
) -> list[Finding]:
    """Every required artefact has a usable digest, and matches it where it is on disk."""
    entries = [item for item in manifest.get("artefacts", []) if isinstance(item, dict)]
    problems: list[str] = []
    counted: dict[str, int] = {scenario_id: 0 for scenario_id in scenario_ids}
    verified = 0
    for entry in entries:
        if not entry.get("required"):
            continue
        scenario_id = str(entry.get("scenario_id", ""))
        path = str(entry.get("path", ""))
        digest = str(entry.get("sha256", ""))
        label = f"{scenario_id}/{path}" if scenario_id else path
        counted[scenario_id] = counted.get(scenario_id, 0) + 1
        if entry.get("present") is not True or entry.get("problem"):
            problems.append(f"{label}: {entry.get('problem') or 'recorded as not present'}")
            continue
        if not _SHA256.match(digest):
            problems.append(f"{label}: {digest!r} is not a SHA-256")
            continue
        if not isinstance(entry.get("size_bytes"), int) or entry.get("size_bytes") == 0:
            problems.append(f"{label}: recorded with no size")
            continue
        verified += _verify_on_disk(evidence_root, path, digest, label, problems)

    empty = [scenario_id for scenario_id in scenario_ids if counted.get(scenario_id, 0) == 0]
    if empty:
        problems.append(f"no required artefact at all for: {', '.join(empty)}")
    if problems:
        return [
            Finding(
                check="evidence.artefacts",
                ok=False,
                detail=f"{len(problems)} problem(s): " + "; ".join(problems[:8]),
                remediation=(
                    "collect what is missing with 'pz-agent live-test collect' and run "
                    "'finalize' again; it refuses to write a manifest with a gap in it"
                ),
            )
        ]
    detail = f"{sum(counted.values())} required artefact(s), each with a SHA-256"
    if evidence_root.is_dir():
        detail += f"; {verified} re-hashed from {evidence_root}"
    else:
        detail += (
            f"; not re-hashed, because {evidence_root} is not on this machine — the digests "
            "were checked as recorded, not against the files"
        )
    return [Finding(check="evidence.artefacts", ok=True, detail=detail)]


def _verify_on_disk(
    evidence_root: Path, path: str, digest: str, label: str, problems: list[str]
) -> int:
    """Re-hash one artefact when the tree is here. Returns 1 when it was checked."""
    if not evidence_root.is_dir():
        return 0
    candidate = evidence_root / path
    if not candidate.is_file():
        problems.append(f"{label}: recorded as present, but not in {evidence_root}")
        return 0
    try:
        actual, _ = _sha256_file(candidate)
    except (OSError, ValueError) as exc:
        problems.append(f"{label}: could not be re-hashed ({exc})")
        return 0
    if actual != digest:
        problems.append(
            f"{label}: on disk it hashes to {actual[:12]}…, the manifest records "
            f"{digest[:12]}… — it was modified after it was written"
        )
        return 0
    return 1


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_release.py",
        description=(
            "Certify a release candidate (--rc) or the release itself (--release). "
            "--release additionally requires live-test evidence, which only a run "
            "inside Project Zomboid can produce."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--rc", action="store_true", help="certify a release candidate")
    mode.add_argument("--release", action="store_true", help="certify the release")
    parser.add_argument(
        "--archive",
        type=Path,
        default=REPO_ROOT / "dist" / build_rc.ARCHIVE_NAME,
        metavar="PATH",
        help="the Windows archive to certify",
    )
    parser.add_argument(
        "--junit",
        type=Path,
        default=None,
        metavar="PATH",
        help="the JUnit report the test suite wrote",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        metavar="PATH",
        help="the live-test evidence manifest (--release only)",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=DEFAULT_EVIDENCE_ROOT,
        metavar="PATH",
        help="the evidence tree the manifest's paths are relative to",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable findings")
    return parser


def run(
    *,
    release: bool,
    archive: Path,
    junit: Path | None,
    manifest: Path,
    evidence_dir: Path,
) -> list[Finding]:
    """Every check for the requested bar, run to the end."""
    findings = check_archive(archive)
    findings.extend(check_tests(junit))
    if release:
        findings.extend(check_evidence(manifest, evidence_dir))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    findings = run(
        release=bool(args.release),
        archive=args.archive,
        junit=args.junit,
        manifest=args.manifest,
        evidence_dir=args.evidence_dir,
    )
    target = (
        f"v{build_rc.RELEASE_VERSION}"
        if args.release
        else f"v{build_rc.RELEASE_VERSION}-{build_rc.RC_TAG}"
    )
    failed = [finding for finding in findings if not finding.ok]
    if args.json:
        print(
            json.dumps(
                {
                    "mode": "release" if args.release else "rc",
                    "target": target,
                    "certified": not failed,
                    "findings": [finding.to_dict() for finding in findings],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_REFUSED if failed else EXIT_OK

    print(f"release gate: {target}")
    for finding in findings:
        print(finding.render())
    if not failed:
        print(f"\nCERTIFIED {target}: {len(findings)} check(s) passed.")
        if not args.release:
            print(
                "This says the artefact is built and the suite is green. It says nothing "
                "about the agent having run inside Project Zomboid; --release is the bar "
                "that asks for that."
            )
        return EXIT_OK
    print(f"\nREFUSED {target}: {len(failed)} of {len(findings)} check(s) failed.")
    for finding in failed:
        print(f"  - {finding.check}: {finding.detail}")
    return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
