"""``pz-agent doctor`` — the ten checks listed in ``docs/COMPATIBILITY.md``.

Each check has a **stable code** and, whenever it is not a pass, remediation
text. That pairing is enforced in :class:`CheckResult` rather than left to
discipline: a check that reports a problem without saying what to do about it
cannot be constructed. The codes are what a bug report cites, so they never
change meaning — a new check takes the next number.

The checks reuse the subsystems that already exist and re-implement none of
them: :mod:`pz_agent_core.platform.discovery` finds the game across every Steam
library, :mod:`pz_agent_core.capabilities` scans the installed Lua read-only,
:mod:`pz_agent_core.session` reads the heartbeats. Doctor's own contribution is
the ordering, the codes and the remediation.

Everything printed or emitted as JSON goes through the workspace redactor, which
is why ``doctor --json`` is the artefact ``docs/TROUBLESHOOTING.md`` tells users
to attach to a public issue.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from pz_agent_core.capabilities import (
    AUTONOMOUS_ATTACK,
    CapabilityReport,
    ReportIOError,
    ScanError,
    ScanResult,
    SymbolIndex,
    lua_root,
    missing_symbols,
    resolve_all,
    save_report,
    scan_lua_tree,
)
from pz_agent_core.diagnostics import Redactor
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.protocol import CapabilityState, JsonDict
from pz_agent_core.session.handshake import SessionDescriptor, SessionError
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer
from pz_agent_core.version import (
    MOD_VERSION,
    PRODUCT_VERSION,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    SUPPORTED_BUILDS,
)

from .context import CliContext, Workspace
from .modinstall import MOD_ID, read_mod_info

#: Locations listed in the JSON report. Discovery bounds its own list; this is
#: the second bound, so a corrupt ``libraryfolders.vdf`` cannot make the report
#: itself unreadable.
MAX_LISTED_PATHS: Final = 16

#: Name of the file each writability check creates and removes.
_PROBE_PREFIX: Final = ".pz-agent-write-test-"


class CheckStatus(StrEnum):
    """Outcome of one check.

    ``INFO`` is separate from ``PASS`` on purpose: "no session is attached" is a
    fact, not a healthy or unhealthy condition, and reporting it as a pass would
    make the summary line claim something the check never established.
    """

    # S105 reads "PASS = ..." as a hardcoded password; it is a verdict name.
    PASS = "pass"  # noqa: S105
    INFO = "info"
    WARN = "warn"
    FAIL = "fail"
    UNKNOWN = "unknown"


#: Statuses that must carry remediation text.
_NEEDS_REMEDIATION: Final = frozenset({CheckStatus.WARN, CheckStatus.FAIL, CheckStatus.UNKNOWN})


class DoctorError(ValueError):
    """A doctor result could not be constructed."""


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One check, its verdict, and what the user should do about it."""

    code: str
    name: str
    title: str
    status: CheckStatus
    detail: str
    remediation: str = ""
    facts: JsonDict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status in _NEEDS_REMEDIATION and not self.remediation:
            raise DoctorError(
                f"{self.code}: a {self.status.value} must tell the user what to do next"
            )

    @property
    def failed(self) -> bool:
        return self.status is CheckStatus.FAIL

    def to_dict(self) -> JsonDict:
        return {
            "code": self.code,
            "name": self.name,
            "title": self.title,
            "status": self.status.value,
            "detail": self.detail,
            "remediation": self.remediation,
            "facts": dict(self.facts),
        }

    def redacted(self, redactor: Redactor) -> CheckResult:
        """A copy with every embedded string put through *redactor*.

        Each check already redacts the paths it composes itself. This second
        pass exists because a detail can also arrive pre-formatted from a
        subsystem that wrapped a path in its own message — the heartbeat
        monitor's "no readable heartbeat: <path>" is the one that caught it — and
        ``doctor --json`` is the artefact users attach to public issues.
        """
        facts: JsonDict = redactor.value(self.facts)
        return CheckResult(
            code=self.code,
            name=self.name,
            title=self.title,
            status=self.status,
            detail=redactor.text(self.detail),
            remediation=redactor.text(self.remediation),
            facts=facts,
        )

    def render_lines(self) -> tuple[str, ...]:
        head = f"[{self.status.value.upper():<7}] {self.code} {self.title}: {self.detail}"
        if not self.remediation:
            return (head,)
        return (head, f"          -> {self.remediation}")


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Every check, plus the capability report the scan produced."""

    checks: tuple[CheckResult, ...]
    generated_at: str
    capabilities: CapabilityReport | None = None
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True when nothing failed. Warnings do not make the environment broken."""
        return not any(check.failed for check in self.checks)

    def summary(self) -> dict[str, int]:
        counts = {status.value: 0 for status in CheckStatus}
        for check in self.checks:
            counts[check.status.value] += 1
        return counts

    def to_dict(self) -> JsonDict:
        return {
            "schema_version": SCHEMA_VERSION,
            "product_version": PRODUCT_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "mod_version": MOD_VERSION,
            "generated_at": self.generated_at,
            "ok": self.ok,
            "summary": self.summary(),
            "checks": [check.to_dict() for check in self.checks],
            "capabilities": None if self.capabilities is None else self.capabilities.to_dict(),
            "notes": list(self.notes),
        }

    def render_lines(self) -> tuple[str, ...]:
        lines: list[str] = []
        for check in self.checks:
            lines.extend(check.render_lines())
        counts = self.summary()
        lines.append("")
        lines.append(
            "pass {pass} · info {info} · warn {warn} · fail {fail} · unknown {unknown}".format(
                **counts
            )
        )
        lines.extend(f"note: {note}" for note in self.notes)
        return tuple(lines)


def _listed(paths: Sequence[str], workspace: Workspace) -> list[str]:
    """Redact and bound a list of paths for the report."""
    shown = [workspace.redactor.text(path) for path in paths[:MAX_LISTED_PATHS]]
    if len(paths) > MAX_LISTED_PATHS:
        shown.append(f"... and {len(paths) - MAX_LISTED_PATHS} more")
    return shown


def _probe_write(directory: Path, ctx: CliContext) -> str:
    """Create and remove a file in *directory*. Returns "" on success, else why not.

    Statting a directory says nothing about whether this account may write in
    it — an inherited deny ACL or a read-only profile both stat fine — so the
    check writes, which is what the mod will have to do.
    """
    probe = directory / f"{_PROBE_PREFIX}{ctx.clock_ms()}"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with probe.open("wb") as handle:
            handle.write(b"pz-agent write test\n")
    except OSError as exc:
        return f"{exc.strerror or exc}"
    try:
        probe.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - removal fails only where creation did
        return f"created a probe file but could not remove it: {exc.strerror or exc}"
    return ""


def check_game_installation(workspace: Workspace) -> CheckResult:
    install = workspace.discovery.install
    facts: JsonDict = {
        "searched": _listed(install.searched, workspace),
        "libraries": len(install.libraries),
        "problems": _listed(install.problems, workspace),
    }
    if install.found and install.path is not None:
        return CheckResult(
            code="PZD001",
            name="game_installation",
            title="Game installation",
            status=CheckStatus.PASS,
            detail=f"found at {workspace.redact(install.path)}",
            facts={**facts, "path": workspace.redact(install.path)},
        )
    return CheckResult(
        code="PZD001",
        name="game_installation",
        title="Game installation",
        status=CheckStatus.FAIL,
        detail=f"not found; {len(install.searched)} location(s) were searched",
        remediation=(
            "install Project Zomboid through Steam, or — for a GOG or manual copy, which "
            "Steam does not list — set install_dir under [game] in config.toml. The full "
            "'searched' list is in doctor --json and usually shows the wrong assumption."
        ),
        facts=facts,
    )


def check_build(workspace: Workspace) -> CheckResult:
    build = workspace.discovery.build
    facts: JsonDict = {
        "build": build.display,
        "known": build.known,
        "source": workspace.redact(build.source),
        "supported": list(SUPPORTED_BUILDS),
    }
    if not build.known:
        return CheckResult(
            code="PZD002",
            name="build_detected",
            title="Installed build",
            status=CheckStatus.WARN,
            detail="could not be determined from local metadata",
            remediation=(
                "launch the game once so it writes console.txt, then run doctor again. "
                "You can proceed without it: every capability stays available_unverified "
                "rather than being credited to a build nobody checked."
            ),
            facts=facts,
        )
    if not build.matches(SUPPORTED_BUILDS):
        return CheckResult(
            code="PZD002",
            name="build_detected",
            title="Installed build",
            status=CheckStatus.WARN,
            detail=f"{build.display} is outside {', '.join(SUPPORTED_BUILDS)}",
            remediation=(
                "you may continue — a point release usually keeps the API surface — but "
                "re-run doctor so the capability report is rebuilt against this build; "
                "claims carried over from another build are downgraded on load."
            ),
            facts=facts,
        )
    return CheckResult(
        code="PZD002",
        name="build_detected",
        title="Installed build",
        status=CheckStatus.PASS,
        detail=f"{build.display}, read from {workspace.redact(build.source)}",
        facts=facts,
    )


def check_user_directory(workspace: Workspace) -> CheckResult:
    user_dir = workspace.discovery.user_dir
    facts: JsonDict = {
        "searched": _listed(user_dir.searched, workspace),
        "source": user_dir.source,
    }
    if user_dir.found and user_dir.path is not None:
        return CheckResult(
            code="PZD003",
            name="user_directory",
            title="Zomboid user directory",
            status=CheckStatus.PASS,
            detail=f"{workspace.redact(user_dir.path)} (found via {user_dir.source})",
            facts={**facts, "path": workspace.redact(user_dir.path)},
        )
    return CheckResult(
        code="PZD003",
        name="user_directory",
        title="Zomboid user directory",
        status=CheckStatus.FAIL,
        detail="not found",
        remediation=(
            "launch the game once so it creates %USERPROFILE%\\Zomboid, or — if you run "
            "with -cachedir or moved the profile onto OneDrive — set user_dir under "
            "[game] in config.toml. A non-ASCII account name is supported and is not "
            "the cause."
        ),
        facts=facts,
    )


def check_permissions(ctx: CliContext, workspace: Workspace) -> CheckResult:
    user_dir = workspace.discovery.user_dir.path
    if user_dir is None:
        return CheckResult(
            code="PZD004",
            name="directory_permissions",
            title="Directory permissions",
            status=CheckStatus.UNKNOWN,
            detail="no Zomboid directory to test",
            remediation="fix PZD003 first; permissions cannot be tested on a directory "
            "that has not been located",
        )
    problem = _probe_write(user_dir, ctx)
    if problem:
        return CheckResult(
            code="PZD004",
            name="directory_permissions",
            title="Directory permissions",
            status=CheckStatus.FAIL,
            detail=f"cannot write in {workspace.redact(user_dir)}: {problem}",
            remediation=(
                "grant your own account write access to the Zomboid directory. Do not "
                "run pz-agent or the game as administrator — the mod writes as you, and "
                "elevating creates files you then cannot read."
            ),
            facts={"path": workspace.redact(user_dir)},
        )
    return CheckResult(
        code="PZD004",
        name="directory_permissions",
        title="Directory permissions",
        status=CheckStatus.PASS,
        detail=f"writable: {workspace.redact(user_dir)}",
        facts={"path": workspace.redact(user_dir)},
    )


def check_mod_installed(workspace: Workspace) -> CheckResult:
    mods_dir = workspace.mods_dir
    if mods_dir is None:
        return CheckResult(
            code="PZD005",
            name="mod_installed",
            title="Bridge mod",
            status=CheckStatus.UNKNOWN,
            detail="no Zomboid directory, so the mods folder could not be inspected",
            remediation="fix PZD003, then run pz-agent install-mod",
        )
    destination = mods_dir / MOD_ID
    info, problem = read_mod_info(destination)
    if info is None:
        return CheckResult(
            code="PZD005",
            name="mod_installed",
            title="Bridge mod",
            status=CheckStatus.FAIL,
            detail=problem or f"not installed at {workspace.redact(destination)}",
            remediation=(
                "run pz-agent install-mod. Installing copies files; it does not tick the "
                "box — enable 'PZ Agent Bridge' in the game's Mods menu and reload the "
                "save afterwards, because enabling a mod does not affect a loaded game."
            ),
            facts={"path": workspace.redact(destination)},
        )
    facts: JsonDict = {
        "path": workspace.redact(destination),
        "mod_version": info.get("modversion", ""),
        "expected_version": MOD_VERSION,
        "pz_version": info.get("pzversion", ""),
    }
    if info.get("modversion") != MOD_VERSION:
        return CheckResult(
            code="PZD005",
            name="mod_installed",
            title="Bridge mod",
            status=CheckStatus.WARN,
            detail=f"installed version {info.get('modversion', '?')} != {MOD_VERSION}",
            remediation=(
                "run pz-agent install-mod again to replace it; a mod and a sidecar from "
                "different releases are not guaranteed to speak the same protocol"
            ),
            facts=facts,
        )
    return CheckResult(
        code="PZD005",
        name="mod_installed",
        title="Bridge mod",
        status=CheckStatus.PASS,
        detail=f"{MOD_ID} {MOD_VERSION} at {workspace.redact(destination)}",
        facts=facts,
    )


def check_heartbeat(ctx: CliContext, workspace: Workspace) -> CheckResult:
    if workspace.ipc_root is None:
        return CheckResult(
            code="PZD006",
            name="game_heartbeat",
            title="Game heartbeat",
            status=CheckStatus.UNKNOWN,
            detail="no exchange directory to read",
            remediation="fix PZD003; without the Zomboid directory there is nowhere for "
            "the mod to write a heartbeat",
        )
    monitor = HeartbeatMonitor(IpcLayout(workspace.ipc_root), clock=ctx.clock_ms)
    liveness = monitor.liveness(Peer.GAME)
    beat = liveness.heartbeat
    facts: JsonDict = {"detail": liveness.detail, "alive": liveness.alive}
    if beat is not None:
        facts.update(
            {
                "build": beat.build or "",
                "armed": beat.armed,
                "mode": None if beat.mode is None else beat.mode.value,
                "player_present": beat.player_present,
                "age_ms": beat.age_ms(ctx.clock_ms()),
            }
        )
    if liveness.alive and beat is not None:
        return CheckResult(
            code="PZD006",
            name="game_heartbeat",
            title="Game heartbeat",
            status=CheckStatus.PASS,
            detail=f"live, {beat.age_ms(ctx.clock_ms())} ms old, build {beat.build or 'unknown'}",
            facts=facts,
        )
    return CheckResult(
        code="PZD006",
        name="game_heartbeat",
        title="Game heartbeat",
        status=CheckStatus.WARN,
        detail=liveness.detail,
        remediation=(
            "this is expected when the game is closed. If it is running and you still "
            "see this, the three causes are: the mod is installed but not enabled in the "
            "Mods menu; no save is loaded, and the mod writes a heartbeat only once a "
            "character exists; or the game crashed and left the files behind (PZD009)."
        ),
        facts=facts,
    )


def check_ipc_writable(ctx: CliContext, workspace: Workspace) -> CheckResult:
    if workspace.ipc_root is None:
        return CheckResult(
            code="PZD007",
            name="ipc_writable",
            title="Exchange directory",
            status=CheckStatus.UNKNOWN,
            detail="no Zomboid directory, so the exchange directory has no location",
            remediation="fix PZD003 first",
        )
    existed = workspace.ipc_root.is_dir()
    problem = _probe_write(workspace.ipc_root, ctx)
    facts: JsonDict = {"path": workspace.redact(workspace.ipc_root), "existed": existed}
    if problem:
        return CheckResult(
            code="PZD007",
            name="ipc_writable",
            title="Exchange directory",
            status=CheckStatus.FAIL,
            detail=f"cannot write in {workspace.redact(workspace.ipc_root)}: {problem}",
            remediation=(
                "the mod and the sidecar exchange files here; without write access "
                "nothing can connect. Check the permissions on the Zomboid\\Lua folder, "
                "and that no backup or antivirus tool has it locked."
            ),
            facts=facts,
        )
    created = "" if existed else " (created now)"
    return CheckResult(
        code="PZD007",
        name="ipc_writable",
        title="Exchange directory",
        status=CheckStatus.PASS,
        detail=f"writable: {workspace.redact(workspace.ipc_root)}{created}",
        facts=facts,
    )


def check_capabilities(
    workspace: Workspace,
) -> tuple[CheckResult, CapabilityReport | None, tuple[str, ...]]:
    """Scan the installed Lua and resolve every declared probe.

    Returns the check, the report and any notes. The report is written to the
    workspace so the rest of the system loads the same document rather than
    re-scanning — and so a build change downgrades it on load.
    """
    install = workspace.discovery.install.path
    if install is None:
        return (
            CheckResult(
                code="PZD008",
                name="timed_actions",
                title="Timed actions",
                status=CheckStatus.UNKNOWN,
                detail="no installation to scan",
                remediation="fix PZD001; capabilities are established by reading the "
                "installed game, never assumed from a version number",
            ),
            None,
            (),
        )
    try:
        scan = scan_lua_tree(lua_root(install))
    except ScanError as exc:
        return (
            CheckResult(
                code="PZD008",
                name="timed_actions",
                title="Timed actions",
                status=CheckStatus.FAIL,
                detail=f"the Lua tree could not be scanned ({exc})",
                remediation=(
                    "verify the game files through Steam; media/lua is missing or "
                    "unreadable, and no capability can be established without it"
                ),
            ),
            None,
            (),
        )
    index = scan.index()
    build = workspace.discovery.build.raw or SUPPORTED_BUILDS[0]
    report = resolve_all(index, build=build)
    notes = _persist_report(workspace, report)
    return _capability_check(workspace, scan, index, report), report, notes


def _persist_report(workspace: Workspace, report: CapabilityReport) -> tuple[str, ...]:
    install = workspace.discovery.install.path
    protected = (install,) if install is not None else ()
    try:
        save_report(workspace.capability_report_path, report, protected_roots=protected)
    except (ReportIOError, ScanError) as exc:
        return (f"the capability report could not be written: {workspace.redactor.text(str(exc))}",)
    return ()


def _capability_check(
    workspace: Workspace,
    scan: ScanResult,
    index: SymbolIndex,
    report: CapabilityReport,
) -> CheckResult:
    gaps = missing_symbols(index)
    # autonomous_attack is unsupported by design and is listed so the report is
    # explicit about it; counting it as a gap would make a healthy install look
    # broken on every run.
    unsupported = sorted(
        capability.name
        for capability in report.capabilities
        if capability.state is CapabilityState.UNSUPPORTED and capability.name != AUTONOMOUS_ATTACK
    )
    facts: JsonDict = {
        "files_scanned": scan.files_scanned,
        "symbols": len(index),
        "truncated": bool(scan.truncation_reasons),
        "usable": list(report.usable_names()),
        "unsupported": unsupported,
        "missing_symbols": {name: list(symbols) for name, symbols in gaps.items()},
        "report_path": workspace.redact(workspace.capability_report_path),
    }
    if unsupported:
        return CheckResult(
            code="PZD008",
            name="timed_actions",
            title="Timed actions",
            status=CheckStatus.FAIL,
            detail=f"no verified API for: {', '.join(unsupported)}",
            remediation=(
                "verify the game files through Steam and re-run doctor. If they are "
                "intact, this build moved or renamed the classes those actions need, and "
                "the matching commands stay unavailable rather than failing halfway."
            ),
            facts=facts,
        )
    if scan.truncation_reasons:
        return CheckResult(
            code="PZD008",
            name="timed_actions",
            title="Timed actions",
            status=CheckStatus.WARN,
            detail=f"scan hit a bound: {scan.truncation_reasons[0]}",
            remediation=(
                "the symbol index is incomplete, so a capability may be reported "
                "unsupported that is merely unseen; re-run doctor after checking that "
                "media/lua holds only the game's own files"
            ),
            facts=facts,
        )
    return CheckResult(
        code="PZD008",
        name="timed_actions",
        title="Timed actions",
        status=CheckStatus.PASS,
        detail=(
            f"{len(index)} symbols from {scan.files_scanned} files; "
            f"{len(report.usable_names())} capability(ies) available, none verified until "
            "a live probe runs"
        ),
        facts=facts,
    )


def check_stale_files(ctx: CliContext, workspace: Workspace) -> CheckResult:
    if workspace.ipc_root is None or not workspace.ipc_root.is_dir():
        return CheckResult(
            code="PZD009",
            name="conflicting_files",
            title="Leftover exchange files",
            status=CheckStatus.PASS,
            detail="no exchange directory, so nothing can be left over in it",
        )
    layout = IpcLayout(workspace.ipc_root)
    findings: list[str] = []
    if layout.panic_stop.is_file():
        findings.append("a panic-stop sentinel is present")
    monitor = HeartbeatMonitor(layout, clock=ctx.clock_ms)
    game_alive = monitor.liveness(Peer.GAME).alive
    if layout.session.is_file() and not game_alive:
        findings.append("session.json is present while the game heartbeat is not")
    if layout.sidecar_lock.is_file() and not monitor.liveness(Peer.SIDECAR).alive:
        findings.append("a sidecar lock file is present with no live sidecar")
    unmanaged = [
        entry.name
        for entry in sorted(workspace.ipc_root.iterdir())
        if not layout.is_managed_path(entry) and not entry.name.startswith(_PROBE_PREFIX)
    ]
    if unmanaged:
        findings.append(f"files the layout does not own: {', '.join(unmanaged[:8])}")
    facts: JsonDict = {"findings": findings, "path": workspace.redact(workspace.ipc_root)}
    if not findings:
        return CheckResult(
            code="PZD009",
            name="conflicting_files",
            title="Leftover exchange files",
            status=CheckStatus.PASS,
            detail="nothing left over in the exchange directory",
            facts=facts,
        )
    return CheckResult(
        code="PZD009",
        name="conflicting_files",
        title="Leftover exchange files",
        status=CheckStatus.WARN,
        detail="; ".join(findings),
        remediation=(
            "close the game, then delete the files named above from the exchange "
            "directory. A previous run's session file looks exactly like a live one to "
            "anything that only checks whether it exists, which is why it is called out "
            "rather than cleaned up automatically."
        ),
        facts=facts,
    )


def check_active_session(ctx: CliContext, workspace: Workspace) -> CheckResult:
    if workspace.ipc_root is None:
        return CheckResult(
            code="PZD010",
            name="active_session",
            title="Active session",
            status=CheckStatus.UNKNOWN,
            detail="no exchange directory to read",
            remediation="fix PZD003 first",
        )
    layout = IpcLayout(workspace.ipc_root)
    if not layout.session.is_file():
        return CheckResult(
            code="PZD010",
            name="active_session",
            title="Active session",
            status=CheckStatus.INFO,
            detail="no session file; nothing is attached",
        )
    descriptor, problem = _read_session(layout)
    if descriptor is None:
        return CheckResult(
            code="PZD010",
            name="active_session",
            title="Active session",
            status=CheckStatus.WARN,
            detail=problem,
            remediation=(
                "delete session.json from the exchange directory while the game is "
                "closed; an unreadable session file blocks a new handshake"
            ),
        )
    monitor = HeartbeatMonitor(layout, clock=ctx.clock_ms)
    beat = monitor.read(Peer.GAME)
    facts: JsonDict = {
        "session_id": descriptor.session_id,
        "mode": descriptor.mode.value,
        "generation": descriptor.generation,
        "save_id": workspace.redactor.text(descriptor.save_id or ""),
        "heartbeat_session": "" if beat is None else beat.session_id,
    }
    if beat is not None and beat.session_id == descriptor.session_id:
        return CheckResult(
            code="PZD010",
            name="active_session",
            title="Active session",
            status=CheckStatus.PASS,
            detail=f"session {descriptor.session_id} in {descriptor.mode.value}",
            facts=facts,
        )
    return CheckResult(
        code="PZD010",
        name="active_session",
        title="Active session",
        status=CheckStatus.WARN,
        detail="a session file exists but the game is not reporting that session",
        remediation=(
            "the game is closed, or this file is from a previous run. Neither side "
            "re-arms itself on recovery: start the sidecar again and arm deliberately."
        ),
        facts=facts,
    )


def _read_session(layout: IpcLayout) -> tuple[SessionDescriptor | None, str]:
    try:
        payload = json.loads(layout.session.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"session.json cannot be read ({exc})"
    if not isinstance(payload, dict):
        return None, "session.json does not hold a JSON object"
    try:
        return SessionDescriptor.from_dict(payload), ""
    except SessionError as exc:
        return None, f"session.json is malformed ({exc})"


def run_checks(ctx: CliContext, workspace: Workspace) -> DoctorReport:
    """Run every check in the order ``docs/COMPATIBILITY.md`` lists them."""
    capability_check, report, notes = check_capabilities(workspace)
    raw = (
        check_game_installation(workspace),
        check_build(workspace),
        check_user_directory(workspace),
        check_permissions(ctx, workspace),
        check_mod_installed(workspace),
        check_heartbeat(ctx, workspace),
        check_ipc_writable(ctx, workspace),
        capability_check,
        check_stale_files(ctx, workspace),
        check_active_session(ctx, workspace),
    )
    return DoctorReport(
        checks=tuple(check.redacted(workspace.redactor) for check in raw),
        generated_at=ctx.now().isoformat(),
        capabilities=report,
        notes=notes,
    )


def environment_facts(workspace: Workspace) -> JsonDict:
    """Version and location summary, shared by ``status`` and the support bundle."""
    return {
        "product_version": PRODUCT_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "mod_version": MOD_VERSION,
        "supported_builds": list(SUPPORTED_BUILDS),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "install_dir": workspace.redact(workspace.install_dir),
        "user_dir": workspace.redact(workspace.user_dir),
        "state_dir": workspace.redact(workspace.state_dir),
    }
