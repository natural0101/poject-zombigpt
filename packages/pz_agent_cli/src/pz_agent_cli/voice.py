"""``pz-agent voice`` — the microphone's way in, and the two ports behind it.

:mod:`pz_agent_voice` was finished, tested and unreachable. Nothing imported it:
not this package, not the MCP server, and neither console script arrived at it.
Russian voice control is a headline feature of this build and there was no way
for a user to start it, which is a defect no test inside that package could see —
its suite is green against a companion nobody constructs.

This module is the seam, and it owns three decisions.

**Stop takes the mod's own latch, not the control channel.** ``panic.stop`` in
the exchange directory is what :func:`PZAgent.Runtime.tick` reads on every
heartbeat tick and what
:meth:`~pz_agent_cli.runtime.SidecarLoop.panic_engaged` reads on every tick and
before every submission. One write puts both of them under it. Routing a spoken
«стоп» through ``sidecar.control.json`` instead would make the stop wait for the
sidecar's next tick *and then* for the mod's, and it would stop nothing at all
while no sidecar is running — which is precisely the state a user reaches for the
stop word in. :meth:`ExchangeSessionPort.stop` is therefore one guarded path and
one write, with every read it needs happening *after* the write.

**A spoken goal takes the Core RPC link, and the stop does not.** This module
used to ship a plan port that refused every submission on the grounds that no
channel carried a goal from a second process into the running sidecar. That
stopped being true when the Local Core RPC link landed, and a refusal that
outlives its reason is a feature the user is told does not exist while the wire
for it runs. :func:`voice_services` now builds the goal route with
:func:`~pz_agent_voice.plan_port.services_over_core_rpc` over the same state
directory the supervisor is given, so a spoken goal arrives in the process that
owns the planner, the policy engine and the action queue — by the same method,
the same codec and the same refusals a tool call uses, which is what keeps the
microphone from being a privileged caller. The one thing that does *not* take
the link is the stop, for the reason above: a goal needs a sidecar, and a stop
has to work when nothing is running.

**Nothing claims the channel is up without asking it.** ``voice check`` dials
:meth:`~pz_agent_mcp.remote.client.RemoteGoalPort.status` before it says
anything about where a goal would go, and reports the link's own sentence —
which names ``pz-agent start`` — when there is nothing there. A descriptor on
disk and a live pid are both cheaper and both say "routed" about a sidecar that
has stopped answering, which is exactly the report this build removed.

**The fake is not selectable.** :data:`~pz_agent_cli.config.SUPPORTED_VOICE_ADAPTERS`
does not list it and :func:`select_adapter` does not construct it under any
setting. An adapter that answers scripted transcripts is a complete test double
and a silent failure in a shipped configuration: the user would hear nothing, say
«стоп» to nothing, and be told the companion is running.

Credentials are named by ``voice.api_key_env`` and read from the environment,
never from the configuration file — the same rule the planner's providers hold,
enforced by the same :func:`~pz_agent_core.planner.providers.ensure_env_name` at
validation time.
"""

from __future__ import annotations

# Two of the sentences quoted below are members of :mod:`pz_agent_voice.phrases`
# reproduced exactly, and short enough that every letter in them has a Latin
# lookalike. Taking the confusable-character rule's suggestion would leave a
# docstring quoting a sentence this build never says.
# ruff: noqa: RUF002
import argparse
import asyncio
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, TypeAlias

from pz_agent_core.capabilities import MAX_NOTES
from pz_agent_core.diagnostics import DiagnosticLog, LogLevel
from pz_agent_core.ipc.atomic import guard_managed
from pz_agent_core.ipc.clocks import Clock
from pz_agent_core.ipc.layout import IpcLayout
from pz_agent_core.planner.providers import CredentialUnavailable, key_from_env
from pz_agent_core.protocol import DangerLevel, JsonDict, ReasonCode, SessionMode
from pz_agent_core.session.heartbeat import HeartbeatMonitor, Peer
from pz_agent_core.version import PROTOCOL_VERSION
from pz_agent_mcp.ports import SessionSnapshot, StopReport
from pz_agent_mcp.remote.client import (
    SIDECAR_REMEDY,
    CoreLink,
    RemoteCoreError,
    RemoteGoalPort,
    SidecarUnavailable,
)
from pz_agent_voice import (
    DEFAULT_VOICE_CONFIG,
    TeamONVoiceAdapter,
    VoiceAdapter,
    VoiceCompanion,
    VoiceConfig,
    VoiceServices,
    VoiceSession,
    classify,
)
from pz_agent_voice.adapters.teamon import (
    TEAMON_SDK_MODULE,
    TeamONClient,
    TeamONSdkUnavailable,
    require_teamon_sdk,
)
from pz_agent_voice.messages import VoiceGoal, VoiceInput, VoiceIntent
from pz_agent_voice.plan_port import services_over_core_rpc

from .config import ADAPTER_NONE, ADAPTER_TEAMON, AgentConfig, load_config
from .context import EXIT_FAILURE, EXIT_OK, CliContext, Workspace, resolve_workspace
from .output import Printer
from .supervisor import SidecarSupervisor, _read_json, _write_json

__all__ = [
    "GOALS_ROUTED",
    "GOAL_CHANNEL_PROBE_SECONDS",
    "VOICE_RECORD_NAME",
    "VOICE_SUBCOMMANDS",
    "ChannelProbe",
    "ExchangeSessionPort",
    "GoalChannelReading",
    "PhraseReading",
    "VoiceRecord",
    "VoiceRecordError",
    "VoiceRefused",
    "VoiceStatus",
    "adapter_name",
    "add_voice_parser",
    "build_companion",
    "collect_voice_status",
    "probe_goal_channel",
    "publish_voice_record",
    "read_phrase",
    "read_voice_record",
    "run_voice",
    "run_voice_check",
    "run_voice_run",
    "select_adapter",
    "voice_services",
]

#: Where a running companion records itself, beside the capability, planner and
#: memory records and for the same reason: ``status`` must be able to say what is
#: in force without re-deriving it and reaching a second opinion.
VOICE_RECORD_NAME: Final = "sidecar.voice.json"

VOICE_SUBCOMMANDS: Final[tuple[str, ...]] = ("run", "check")

#: What the panic latch says about who asked. The mod never parses this file —
#: any non-empty content is a stop, which is the only reading that fails safe —
#: so the content is for the human reading the exchange directory afterwards.
_PANIC_SOURCE: Final = "voice"


class VoiceRefused(ValueError):
    """A voice command will not start, and this says exactly what is missing."""


class VoiceRecordError(ValueError):
    """A voice record could not be read or written as a whole document."""


# ---------------------------------------------------------------------------
# the ports
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExchangeSessionPort:
    """:class:`~pz_agent_mcp.ports.SessionPort` over the exchange directory.

    Every fact it reports is read from a file the mod or the sidecar wrote this
    second, and every fact it cannot read is reported as unknown rather than as
    fine. That asymmetry is the whole of this class: a session port that answered
    "armed, connected" because the heartbeat was missing would have the companion
    submitting work into a game that is not running.
    """

    layout: IpcLayout
    supervisor: SidecarSupervisor
    clock: Clock

    @property
    def monitor(self) -> HeartbeatMonitor:
        return HeartbeatMonitor(self.layout, clock=self.clock)

    def status(self) -> SessionSnapshot:
        """What the mod last published about itself.

        The game's heartbeat is the reading, not ``session.json``: the session
        file says which session was opened, and the heartbeat says whether
        anything is still on it. A stale or absent heartbeat is ``connected =
        False``, which is what makes the companion say «Нет связи с игрой» rather
        than start work against a world nobody is reporting on.
        """
        game = self.monitor.liveness(Peer.GAME)
        sidecar = self.monitor.liveness(Peer.SIDECAR)
        beat = game.heartbeat
        if beat is None:
            return SessionSnapshot(
                session_id="",
                mode=SessionMode.OBSERVE,
                armed=False,
                connected=False,
                protocol_version=PROTOCOL_VERSION,
                sidecar_heartbeat_ok=sidecar.alive,
            )
        return SessionSnapshot(
            session_id=beat.session_id,
            mode=beat.mode if beat.mode is not None else SessionMode.OBSERVE,
            # A heartbeat that does not carry the field has not said it is armed,
            # and an omission is not a claim: unknown reads as disarmed here for
            # the same reason it does everywhere else in this build.
            armed=bool(beat.armed),
            connected=game.alive,
            protocol_version=beat.protocol_version,
            build=beat.build,
            danger_level=beat.danger_level if beat.danger_level is not None else DangerLevel.NONE,
            game_heartbeat_ok=game.alive,
            sidecar_heartbeat_ok=sidecar.alive,
            active_action_id=beat.active_action_id,
        )

    def arm(self, mode: SessionMode, *, confirm_backup: bool) -> SessionSnapshot:
        """Refused: arming is not something a room can say.

        §7.7 puts a verified backup and an explicit user request between a save
        and an armed agent. A microphone in a room with a television is not an
        explicit user request, and nothing in the vocabulary
        :mod:`pz_agent_voice.intent` recognises even reaches here — this method
        exists because the port protocol has it, and it refuses because the one
        way to arm this build is ``pz-agent arm``.

        Raises:
            VoiceRefused: always.
        """
        raise VoiceRefused(
            f"arming in {mode.value} is not a spoken command; run 'pz-agent arm' — "
            "granting authority to act is an explicit request, not a phrase overheard"
        )

    def disarm(self) -> SessionSnapshot:
        """Ask the running sidecar to return to ``OBSERVE``.

        The same control request ``pz-agent disarm`` publishes, through the same
        channel, so there is one route into the loop's arming state rather than
        two. It takes authority away only, which is why it is offered here at all
        while :meth:`arm` is not.
        """
        self.supervisor.disarm()
        return self.status()

    def stop(self) -> StopReport:
        """Latch the panic stop, and answer once it is verifiably down.

        The write is first and everything else is after it. The mod reads this
        file as a level on every heartbeat tick and stops whatever it is running;
        the sidecar reads it on every tick and before every submission, disarms,
        and closes in-flight work as lost. One write reaches both, and it reaches
        them whether or not a sidecar is running — which the control channel does
        not.

        The file is written whole and not atomically, because the mod never
        parses it: any non-empty content is a stop, so a torn write is still a
        stop. Atomicity would buy nothing and cost an fsync on the one path in
        this build that must not wait for a disk to settle.

        Returning is a claim that the *request* is in force — verified by the
        file's own size — and not that the game has already stopped. That second
        fact is not observable synchronously from this process: the mod applies
        the latch on its next heartbeat tick and clears the file afterwards. The
        report says what was observed, and ``cleared`` stays 0 because the count
        of mod-owned queue entries belongs to the mod, which is where it is
        recorded; inventing one here would be a number nobody measured.

        Raises:
            OSError: when the latch could not be written, which is the one case
                the companion must not say «Остановился.» about.
        """
        latch = guard_managed(self.layout, self.layout.panic_stop)
        latch.write_text(
            f'{{"requested_at_ms":{self.clock()},"source":"{_PANIC_SOURCE}",'
            f'"reason":"{ReasonCode.PANIC_STOP.value}"}}\n',
            encoding="utf-8",
        )
        if latch.stat().st_size == 0:
            raise OSError(f"the panic latch at {latch.name} was written empty, so it stops nothing")
        # Read only now: a heartbeat read between the decision and the write
        # would be a file read inside the latency this whole path exists to keep
        # short, and its answer is no less true a microsecond later.
        snapshot = self.status()
        return StopReport(cleared=0, disarmed=not snapshot.armed, mode=snapshot.mode)


#: Where a spoken goal goes, in one sentence. One constant rather than one
#: phrasing per call site, so the line ``voice run`` prints and the reason a
#: record carries cannot drift apart.
GOALS_ROUTED: Final = (
    "a spoken goal is submitted to the running sidecar over the Core RPC link, where it meets "
    "the same planner, policy engine and limits a tool call meets; with no sidecar running the "
    "companion says the goal did not land, and «стоп» reaches the game either way"
)

#: How long ``voice check`` waits for the goal channel to answer before it
#: reports it unreachable. Short on purpose: this command answers a question
#: about a phrase and a user is waiting at a terminal for it. The call it bounds
#: is a read — ``goal.status`` reports what the channel holds and changes
#: nothing — so an expiry leaves nothing half-done.
GOAL_CHANNEL_PROBE_SECONDS: Final = 2.0


def voice_services(workspace: Workspace, *, clock: Clock) -> VoiceServices:
    """The ports the companion drives: the exchange directory, and the link.

    Two transports, deliberately. Arm, disarm and stop stay on
    :class:`ExchangeSessionPort`, where one write reaches the mod whether or not
    a sidecar is running. Goals take
    :func:`~pz_agent_voice.plan_port.services_over_core_rpc` over
    ``workspace.state_dir`` — the directory :class:`SidecarSupervisor` is handed
    here and the one a running sidecar publishes its RPC descriptor and token
    into, so there is no second path to configure and nothing to keep in step.

    Nothing is dialled by this function. The link resolves the descriptor and
    the token on every call, so the companion may be started before the sidecar,
    a sidecar that restarts underneath it stays reachable, and "the sidecar is
    not running" arrives as a refused goal the user is told about rather than as
    a companion that never started and therefore never heard «стоп».

    Raises:
        VoiceRefused: when there is no exchange directory to drive, which is the
            same machine on which the sidecar has nothing to attach to.
    """
    ipc_root = workspace.ipc_root
    if ipc_root is None:
        raise VoiceRefused(
            "no Zomboid directory was found, so there is no exchange directory to "
            "speak to. Run 'pz-agent doctor' and read PZD003"
        )
    return services_over_core_rpc(
        ExchangeSessionPort(
            layout=IpcLayout(ipc_root),
            supervisor=SidecarSupervisor(workspace.state_dir, clock=clock),
            clock=clock,
        ),
        workspace.state_dir,
    )


@dataclass(frozen=True, slots=True)
class GoalChannelReading:
    """What the goal channel answered when this process asked it.

    ``reachable`` is observed rather than inferred: something accepted a
    connection, authenticated it and answered ``goal.status``. The cheaper
    checks were all available — a descriptor on disk, a process id that is still
    alive — and every one of them reports a routed channel about a sidecar that
    has stopped answering, which is the claim this reading exists to stop making.
    """

    reachable: bool
    detail: str

    def to_dict(self) -> JsonDict:
        return {"reachable": self.reachable, "detail": self.detail}


#: Asks the goal channel about itself, once, when something needs the answer.
#: A callable rather than a reading so that :func:`read_phrase` can stay a pure
#: function of the words for every phrase that is not a goal — a stop must be
#: answerable on a machine with no sidecar, no game and no network at all.
ChannelProbe: TypeAlias = Callable[[], GoalChannelReading]


def probe_goal_channel(
    state_dir: Path, *, deadline: float = GOAL_CHANNEL_PROBE_SECONDS
) -> GoalChannelReading:
    """Ask the goal channel at *state_dir* whether it is there.

    One read-only call. ``goal.status`` submits nothing, admits nothing and
    cancels nothing, so this is safe to run against a live armed session by a
    user who only wants to know whether a spoken goal would land.

    Every way of not reaching the channel is reported as unreachable carrying
    the link's own sentence, which names the file that was missing and the
    command that starts a sidecar. Nothing here rewrites those: this process
    knows strictly less about the failure than the layer that raised it.
    """
    port = RemoteGoalPort(CoreLink(state_dir=state_dir, deadline=deadline))
    try:
        status = port.status()
    except SidecarUnavailable as absent:
        # Its message already ends in SIDECAR_REMEDY, which names 'pz-agent start'.
        return GoalChannelReading(reachable=False, detail=str(absent))
    except RemoteCoreError as unreadable:
        # A refusal or an unreadable answer. Something is there and this build
        # cannot use it, which is not a routed channel either.
        return GoalChannelReading(reachable=False, detail=f"{unreadable}; {SIDECAR_REMEDY}")
    active = "one goal active" if status.active is not None else "no goal active"
    return GoalChannelReading(
        reachable=True,
        detail=(
            f"the channel answered: {active} and {len(status.pending)} waiting, so a goal "
            "spoken now is submitted to it"
        ),
    )


def build_companion(
    adapter: VoiceAdapter,
    services: VoiceServices,
    *,
    clock: Clock,
    config: VoiceConfig = DEFAULT_VOICE_CONFIG,
) -> VoiceCompanion:
    """The companion ``voice run`` drives, assembled from parts that already exist.

    Separate from :func:`run_voice` because the seam test drives exactly this
    assembly over a fake adapter and a fake mod: a companion built one way here
    and another way in a test proves nothing about what a user starts.
    """
    session = VoiceSession(
        services,
        clock=clock,
        # A fresh key per submission, so a transcript the recogniser repeated
        # cannot become two plans on the far side of the link.
        ids=lambda: uuid.uuid4().hex,
        config=config,
    )
    return VoiceCompanion(adapter, session, clock=clock)


# ---------------------------------------------------------------------------
# which adapter, and whether it can be built at all
# ---------------------------------------------------------------------------


#: What ``status`` calls each adapter class. Keyed by the class rather than by
#: the configured word, so the record says what was *constructed*: an adapter
#: this table does not know prints its own class name instead of borrowing the
#: name of the one the user asked for.
_ADAPTER_NAMES: Final[dict[type, str]] = {TeamONVoiceAdapter: ADAPTER_TEAMON}


def adapter_name(adapter: VoiceAdapter) -> str:
    """The name ``status`` prints for the adapter that is actually listening."""
    return _ADAPTER_NAMES.get(type(adapter), type(adapter).__name__)


def select_adapter(
    config: AgentConfig,
    *,
    env: Mapping[str, str],
    clock: Clock,
    client: TeamONClient | None = None,
) -> VoiceAdapter:
    """The adapter ``voice.adapter`` names, or a refusal saying what is missing.

    Never a fallback. An adapter that was configured and cannot be built is a
    session that must not start: falling back to a fake would leave a user
    talking to a test double, and falling back to "none" would leave them talking
    to nothing while the command reported success.

    *client* is the integrator's seam, and the reason it exists is
    :mod:`pz_agent_voice.adapters.teamon`: the vendor SDK is not installed here
    and its surface is unverified, so this build states the three methods it
    needs as :class:`~pz_agent_voice.adapters.teamon.TeamONClient` and refuses to
    invent calls into a package it has never seen. Whoever has the SDK writes
    that binding and passes it in; until then ``voice run`` refuses with the step
    that is missing rather than guessing at a vendor function name.

    Raises:
        VoiceRefused: for every setting under which no adapter can be built.
    """
    name = str(config.get("voice", "adapter"))
    if name == ADAPTER_NONE:
        raise VoiceRefused(
            'voice.adapter is "none", so there is nothing to listen with. Set it to '
            f'"{ADAPTER_TEAMON}" in config.toml'
        )
    if name != ADAPTER_TEAMON:
        # Unreachable through a validated configuration, which is exactly why it
        # is checked: this is the branch that keeps a hand-built config — or a
        # test double — from selecting an adapter the product does not ship.
        raise VoiceRefused(
            f"voice.adapter is {name!r}, which is not an adapter this build constructs; "
            f'the shipped choices are "{ADAPTER_TEAMON}" and "{ADAPTER_NONE}"'
        )
    variable = str(config.get("voice", "api_key_env"))
    try:
        # Asked before the session starts rather than at the first utterance, for
        # the reason ``resolve_provider`` asks the planner's providers: finding
        # out that the key is missing from a microphone that has gone quiet is
        # the worst place in the system to find it out. The value is checked and
        # dropped — nothing in this build has anywhere to send it, and the client
        # an integrator supplies authenticates with it itself.
        key_from_env(variable, environ=env)
    except CredentialUnavailable as absent:
        raise VoiceRefused(f"{absent} Nothing was started") from absent
    except ValueError as invalid:
        # Deliberately not quoting the offending value the way the validator
        # does. A setting that fails this check is most often a pasted key, and
        # this message is printed to a terminal and pasted into bug reports.
        # ``validate-config`` catches the same thing earlier and by name, so
        # reaching here means a document nothing validated.
        raise VoiceRefused(
            "voice.api_key_env must be the name of an environment variable — upper "
            "case, letters, digits and underscores. It names the variable holding "
            "the key; a key written into config.toml is a key in every copy of it"
        ) from invalid
    if client is None:
        raise VoiceRefused(_no_teamon_client())
    return TeamONVoiceAdapter(client, clock=clock)


def _no_teamon_client() -> str:
    """Why no TeamON adapter was built, distinguishing the two causes.

    A missing SDK is an install step. An SDK that is present is a different
    situation entirely — the vendor's surface has still never been verified from
    this repository, and binding it to
    :class:`~pz_agent_voice.adapters.teamon.TeamONClient` is three methods that
    only somebody with the SDK can write correctly. Telling a user who installed
    it that it is not installed would send them to fix the wrong thing.
    """
    try:
        require_teamon_sdk()
    except TeamONSdkUnavailable as absent:
        return f"{absent}. Nothing was started"
    return (
        f"the {TEAMON_SDK_MODULE!r} package is installed, but this build has no verified "
        "binding from it to TeamONClient: no call into the vendor's surface has been "
        "confirmed from this repository, so none is invented. Supply a TeamONClient — "
        "transcripts(), synthesize(request), interrupt(utterance_id), documented in "
        "pz_agent_voice/adapters/teamon.py. Nothing was started"
    )


# ---------------------------------------------------------------------------
# what status reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VoiceRecord:
    """What a companion recorded about itself, as ``status`` reads it.

    ``adapter`` is read off the object that was constructed rather than off the
    setting that asked for it, the way the planner record's ``wired`` is: a
    ``voice run`` that ended up on something other than the configured adapter
    would say so here instead of looking identical to one that did not.

    ``goals_routed`` says whether the companion that wrote this record had a
    route for a spoken goal at all. This build writes ``True``: goals take the
    Core RPC link. It is a field rather than a constant in the renderer because
    it is a fact about the process that ran — a record left by the build that
    had no such route reads ``False``, and ``status`` says so instead of
    describing a route that companion never had. It is not a claim that a
    sidecar is up; that is a question only :func:`probe_goal_channel` answers,
    and only at the moment it is asked.

    ``running`` is written when the companion starts and rewritten when it exits,
    including on a failure. A process killed outright leaves it True with nothing
    to correct it, which is why ``pid`` and ``started_at_ms`` are recorded and
    printed: they are what tells a user that the companion they are looking at is
    one that died last Tuesday.
    """

    running: bool
    adapter: str
    detail: str
    pid: int = 0
    started_at_ms: int = 0
    goals_routed: bool = False
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise VoiceRecordError("a voice record must say how it reached the state it names")
        if self.pid < 0:
            raise VoiceRecordError(f"a voice record's pid must be non-negative, got {self.pid}")
        if self.started_at_ms < 0:
            raise VoiceRecordError("a voice record's started_at_ms must be non-negative")

    def to_dict(self) -> JsonDict:
        return {
            "running": self.running,
            "adapter": self.adapter,
            "detail": self.detail,
            "pid": self.pid,
            "started_at_ms": self.started_at_ms,
            "goals_routed": self.goals_routed,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> VoiceRecord:
        """Parse a record this process did not necessarily write.

        Raises:
            VoiceRecordError: for anything malformed, rather than reading
                whichever field survived as the whole answer.
        """
        return cls(
            running=_flag(payload, "running"),
            adapter=_text(payload.get("adapter", ""), "adapter"),
            detail=_text(payload.get("detail", ""), "detail"),
            pid=_count(payload.get("pid", 0), "pid"),
            started_at_ms=_count(payload.get("started_at_ms", 0), "started_at_ms"),
            goals_routed=_flag(payload, "goals_routed", default=False),
            notes=_notes(payload.get("notes", [])),
        )


def _flag(payload: Mapping[str, Any], name: str, *, default: bool | None = None) -> bool:
    value = payload.get(name, default)
    if not isinstance(value, bool):
        raise VoiceRecordError(f"a voice record's {name} must be true or false")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise VoiceRecordError(f"a voice record's {field_name} must be a string")
    return value


def _count(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VoiceRecordError(f"a voice record's {field_name} must be a non-negative integer")
    return value


def _notes(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, list):
        raise VoiceRecordError("a voice record's notes must be an array")
    return tuple(_text(item, "note") for item in value)[:MAX_NOTES]


def read_voice_record(path: Path) -> VoiceRecord | None:
    """The record at *path*, or None when no companion has written one there.

    None is a different statement from "voice is not running", and ``status``
    reports it as one: nothing has ever started a companion in this state
    directory, which is the ordinary state of a machine where voice has not been
    tried rather than a companion that stopped.
    """
    payload = _read_json(path)
    if payload is None:
        return None
    try:
        return VoiceRecord.from_dict(payload)
    except VoiceRecordError:
        return None


def publish_voice_record(path: Path, record: VoiceRecord) -> VoiceRecord:
    """Write *record* where ``status`` reads it. Returns what was written.

    A record that cannot be written is noted rather than raised, the way the
    capability, planner and memory records treat the same failure: losing the
    status file is not a reason to stop listening for «стоп».
    """
    try:
        _write_json(path, record.to_dict())
    except OSError as exc:
        note = f"the voice record could not be written ({exc.strerror or exc})"
        return replace(record, notes=(*record.notes, note)[-MAX_NOTES:])
    return record


@dataclass(frozen=True, slots=True)
class VoiceStatus:
    """Everything ``pz-agent status`` says about voice.

    Two sources, deliberately kept apart. ``enabled`` and ``adapter`` are the
    configuration — what the user asked for — and the record is what a companion
    actually did about it. Merging them would hide the case worth seeing: a
    configuration that names TeamON and a state directory in which nothing has
    ever managed to start one.
    """

    enabled: bool
    adapter: str
    detail: str
    record: VoiceRecord | None = None

    def to_dict(self) -> JsonDict:
        return {
            "enabled": self.enabled,
            "adapter": self.adapter,
            "detail": self.detail,
            "record": None if self.record is None else self.record.to_dict(),
        }


def collect_voice_status(ctx: CliContext, workspace: Workspace) -> VoiceStatus:
    """What voice is configured to do here, and what the last companion did.

    The configuration is read rather than recorded because it is a fact about
    *now* — the file the next ``voice run`` will read — while the record is a
    fact about a process. A document that does not validate reports the defaults,
    which is what that ``voice run`` would refuse on, so the two agree.
    """
    validation = load_config(workspace.config_path)
    config = validation.config
    record = read_voice_record(workspace.state_dir / VOICE_RECORD_NAME)
    if config is None:
        return VoiceStatus(
            enabled=False,
            adapter=ADAPTER_NONE,
            detail=(
                f"the configuration did not validate ({len(validation.errors)} problem(s)), so "
                "nothing here is in force; run 'pz-agent validate-config'"
            ),
            record=record,
        )
    enabled = bool(config.get("voice", "enabled"))
    adapter = str(config.get("voice", "adapter"))
    if not enabled:
        detail = "voice.enabled is false in config.toml, so 'pz-agent voice run' refuses"
    elif adapter == ADAPTER_NONE:
        detail = 'voice.enabled is true and voice.adapter is "none", so nothing would listen'
    else:
        detail = (
            "'pz-agent voice run' starts the companion; the sidecar does not start it, "
            "and nothing listens until you do"
        )
    return VoiceStatus(enabled=enabled, adapter=adapter, detail=detail, record=record)


# ---------------------------------------------------------------------------
# voice check: what a phrase resolves to, without acting
# ---------------------------------------------------------------------------


#: What each intent would cause, in one line. A closed table for the same reason
#: :mod:`pz_agent_voice.phrases` is one — except that these are printed to a
#: terminal rather than spoken, which is why they may explain themselves at
#: length and the spoken ones may not.
_EFFECTS: Final[dict[VoiceIntent, str]] = {
    VoiceIntent.STOP: (
        "stops the agent, from any state, with no wake word and without waiting for the "
        "recogniser to settle"
    ),
    VoiceIntent.GOAL: "asks the agent to start work, over the running sidecar's goal channel",
    VoiceIntent.STATUS: "asks the companion to say what the session is",
    VoiceIntent.AFFIRM: "answers an outstanding question; on its own it does nothing",
    VoiceIntent.DENY: "declines an outstanding question; on its own it does nothing",
    VoiceIntent.WAKE: "opens a wake session, so the next phrase needs no wake word",
    VoiceIntent.AMBIGUOUS: "matches more than one goal, so the companion asks which you meant",
    VoiceIntent.UNKNOWN: "nothing; the companion stays quiet rather than guessing",
}


@dataclass(frozen=True, slots=True)
class PhraseReading:
    """What one phrase resolves to, and what would happen if it were heard."""

    words: tuple[str, ...]
    intent: VoiceIntent
    goal: VoiceGoal | None
    woke: bool
    effect: str
    notes: tuple[str, ...] = ()
    channel: GoalChannelReading | None = None

    @property
    def recognised(self) -> bool:
        """True when the phrase matched something the companion acts on."""
        return self.intent is not VoiceIntent.UNKNOWN

    def to_dict(self) -> JsonDict:
        return {
            "words": list(self.words),
            "intent": self.intent.value,
            "goal": None if self.goal is None else self.goal.value,
            "woke": self.woke,
            "recognised": self.recognised,
            "effect": self.effect,
            "notes": list(self.notes),
            "goal_channel": None if self.channel is None else self.channel.to_dict(),
        }


def _needs_a_wake_word(intent: VoiceIntent, *, config: VoiceConfig, woke: bool) -> bool:
    """Whether the missing wake word is what stands between this phrase and acting.

    Not said about a stop, which is honoured without one from any state, and not
    said about a phrase that matched nothing: telling a user that «бармаглот»
    would have needed a wake word implies it would have done something with one.
    """
    if intent in (VoiceIntent.STOP, VoiceIntent.UNKNOWN):
        return False
    return config.require_wake_word and not woke


def read_phrase(
    phrase: str,
    *,
    config: VoiceConfig = DEFAULT_VOICE_CONFIG,
    channel: ChannelProbe | None = None,
) -> PhraseReading:
    """Resolve *phrase* exactly as a heard transcript would resolve.

    Through :func:`~pz_agent_voice.intent.classify` and nothing else, because a
    second matcher here would answer a question about itself: the whole use of
    this command is finding out why «стоп» was not recognised, and an answer from
    code the session does not run is worse than no answer.

    The phrase is treated as a final transcript at full confidence — a typed one
    is neither a guess nor half-heard — so what this reports is what the session
    would do with the words, not with the recogniser's opinion of them.

    *channel* is asked only when the phrase resolves to a goal, which is the one
    intent whose effect depends on another process being there. A stop is
    answered without it, and so is a word that matched nothing: dialling a socket
    to explain «бармаглот» would make this command fail on the machines it is
    most often run on. Left at ``None`` the reading simply says nothing about the
    channel, which is what a caller that did not ask has a right to.
    """
    raw = VoiceInput(transcript=phrase, at_ms=0, final=True, confidence=1.0)
    match = classify(raw, wake_words=config.wake_words)
    notes: list[str] = []
    if _needs_a_wake_word(match.intent, config=config, woke=match.woke):
        notes.append(
            "no wake word in this phrase: it is acted on only inside an open wake session. "
            f"The wake words are {', '.join(sorted(config.wake_words))}"
        )
    if match.intent is VoiceIntent.AMBIGUOUS and match.goals:
        notes.append(
            "it matched " + ", ".join(goal.value for goal in match.goals) + ", so nothing starts"
        )
    return PhraseReading(
        words=raw.words(),
        intent=match.intent,
        goal=match.goal,
        woke=match.woke,
        effect=_EFFECTS[match.intent],
        notes=tuple(notes),
        channel=None if channel is None or match.intent is not VoiceIntent.GOAL else channel(),
    )


# ---------------------------------------------------------------------------
# the commands
# ---------------------------------------------------------------------------


def add_voice_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``voice`` group and its two subcommands."""
    group = subparsers.add_parser(
        "voice",
        help="run the Russian voice companion, or ask what a phrase resolves to",
        description=(
            "The companion listens through the adapter named by [voice] in config.toml and "
            "runs in this terminal until you stop it. 'voice check' answers what a phrase "
            "would do without a microphone, a game or a session."
        ),
    )
    inner = group.add_subparsers(dest="voice_command", metavar="SUBCOMMAND")

    run = inner.add_parser("run", help="start the companion against the configured adapter")

    check = inner.add_parser(
        "check", help="print the intent a phrase resolves to, without acting on it"
    )
    check.add_argument(
        "phrase",
        nargs="+",
        metavar="WORD",
        help="the phrase to resolve, e.g. 'pz-agent voice check стоп'",
    )

    for parser in (run, check):
        parser.add_argument("--json", action="store_true")


def run_voice(ctx: CliContext, args: argparse.Namespace) -> int:
    """Dispatch a parsed ``voice`` invocation."""
    printer = Printer(ctx.stdout, ctx.stderr)
    subcommand = getattr(args, "voice_command", None)
    if subcommand is None:
        printer.error(f"voice needs a subcommand: {', '.join(VOICE_SUBCOMMANDS)}")
        return EXIT_FAILURE
    as_json = bool(args.json)
    if subcommand == "check":
        return run_voice_check(ctx, phrase=" ".join(args.phrase), as_json=as_json)
    if subcommand == "run":
        return run_voice_run(ctx, as_json=as_json)
    # Unreachable through the parser: every choice it accepts is handled above.
    raise AssertionError(f"unrouted voice subcommand: {subcommand!r}")


def _goal_channel_probe(ctx: CliContext) -> ChannelProbe:
    """Dial the goal channel for this machine, if a goal is what was said.

    The workspace is resolved inside the closure rather than around it, so a
    ``voice check стоп`` on a machine with no game and no profile still answers
    without running discovery, and only a goal pays for the lookup.

    The link's sentences name files rather than paths, but they are printed and
    pasted into bug reports, so they go through the workspace's redactor for the
    same reason every other line this command prints does.
    """

    def probe() -> GoalChannelReading:
        workspace = resolve_workspace(ctx)
        reading = probe_goal_channel(workspace.state_dir)
        return replace(reading, detail=workspace.redactor.text(reading.detail))

    return probe


def run_voice_check(ctx: CliContext, *, phrase: str, as_json: bool) -> int:
    """Handler for ``pz-agent voice check``.

    Prints the word tokens rather than the phrase as typed. They are what the
    matcher compares — the answer to "why was that not recognised" is usually
    visible in them — and they are already bounded and stripped of everything a
    terminal would act on, which the raw argument is not.

    A phrase that resolves to a goal also gets the goal channel asked about
    itself, and the answer is printed whichever way it came out. The exit code
    stays a statement about *recognition* — that is the question a script asks
    this command, and an unreachable sidecar is not the phrase's fault — so the
    channel is reported in a field of its own rather than folded into the status.
    """
    printer = Printer(ctx.stdout, ctx.stderr)
    reading = read_phrase(phrase, channel=_goal_channel_probe(ctx))
    if as_json:
        printer.json(reading.to_dict())
        return EXIT_OK if reading.recognised else EXIT_FAILURE
    printer.heading("pz-agent voice check")
    printer.field("words", " ".join(reading.words) or "(nothing that matches a word)")
    if reading.recognised:
        printer.field("intent", reading.intent.value)
    else:
        printer.field("intent", "nothing matched")
    if reading.goal is not None:
        printer.field("goal", reading.goal.value)
    printer.field("wake word", "heard" if reading.woke else "not in this phrase")
    printer.field("effect", reading.effect)
    if reading.channel is not None:
        state = "reachable" if reading.channel.reachable else "UNREACHABLE"
        printer.field("goal channel", f"{state} — {reading.channel.detail}")
    for note in reading.notes:
        printer.field("note", note)
    return EXIT_OK if reading.recognised else EXIT_FAILURE


def run_voice_run(ctx: CliContext, *, as_json: bool, client: TeamONClient | None = None) -> int:
    """Handler for ``pz-agent voice run``.

    Refuses before it starts anything, and says which of the several ways it
    refused: the configuration did not validate, voice is switched off, the
    adapter is "none", the key is not in the environment, or the adapter cannot
    be constructed. Every one of them leaves the machine exactly as it was —
    starting a companion that cannot hear is worse than not starting one, because
    the user then says «стоп» to something that is not listening.
    """
    printer = Printer(ctx.stdout, ctx.stderr)
    workspace = resolve_workspace(ctx)
    validation = load_config(workspace.config_path)
    if validation.config is None:
        return _refuse(
            printer,
            workspace,
            as_json=as_json,
            detail=(
                f"the configuration has {len(validation.errors)} problem(s), so the companion "
                "was not started; run 'pz-agent validate-config' to see them"
            ),
        )
    config = validation.config
    if not bool(config.get("voice", "enabled")):
        return _refuse(
            printer,
            workspace,
            as_json=as_json,
            detail=(
                "voice.enabled is false in config.toml, so the companion was not started. "
                "Set it to true to run it"
            ),
        )
    try:
        adapter = select_adapter(config, env=ctx.env, clock=ctx.clock_ms, client=client)
        services = voice_services(workspace, clock=ctx.clock_ms)
    except VoiceRefused as refusal:
        return _refuse(printer, workspace, as_json=as_json, detail=str(refusal))

    record_path = workspace.state_dir / VOICE_RECORD_NAME
    name = adapter_name(adapter)
    # Read off the adapter that was constructed rather than off the setting that
    # asked for it: this is the field that would show a companion listening on
    # something other than what config.toml names.
    started_at_ms = ctx.clock_ms()
    companion = build_companion(adapter, services, clock=ctx.clock_ms)
    publish_voice_record(
        record_path,
        VoiceRecord(
            running=True,
            adapter=name,
            detail=f"a companion is listening on {name}",
            pid=os.getpid(),
            started_at_ms=started_at_ms,
            goals_routed=True,
        ),
    )
    log = _companion_log(ctx, workspace)
    _log_safely(log, LogLevel.INFO, "voice.start", adapter=name, goals_routed=True)
    if not as_json:
        printer.line(f"listening on {name}. Say «стоп» to stop the agent; Ctrl-C to stop this.")
        printer.field("goals", GOALS_ROUTED)
    ended, code = _serve(companion)
    _log_session(log, companion, adapter=name, ended=workspace.redactor.text(ended))
    publish_voice_record(
        record_path,
        VoiceRecord(
            running=False,
            adapter=name,
            detail=workspace.redactor.text(ended),
            pid=os.getpid(),
            started_at_ms=started_at_ms,
            goals_routed=True,
        ),
    )
    message = workspace.redactor.text(ended)
    if as_json:
        printer.json(
            {
                "started": True,
                "adapter": name,
                "detail": message,
                "goals_routed": True,
                "ok": code == EXIT_OK,
            }
        )
    elif code == EXIT_OK:
        printer.line(message)
    else:
        printer.error(message)
    return code


def _companion_log(ctx: CliContext, workspace: Workspace) -> DiagnosticLog | None:
    """The companion's own log, or None when the directory will not take one.

    ``docs/LOCAL_DEBUG_MAP.md`` sends an operator to ``logs/`` for both voice
    symptoms it lists — a Russian phrase not recognised, and «стоп» heard while
    the character kept going — and the companion had never written a byte there.
    Its turn history and its synthesiser failures live in two bounded rings
    inside a process that then exits, and :attr:`VoiceCompanion.speech_failures`
    says in its own docstring that they are kept because "the companion went
    quiet" with nothing recorded is what a support bundle cannot explain. The
    bundle never saw them.

    Written under the same name as the sidecar's log, in the same directory, so
    ``pz-agent logs`` and the support bundle pick it up with no second path to
    know about. The two processes append to one rotating file, which is what an
    operator wants when the question is "did the stop I said reach the sidecar":
    both halves of that are then in one place, in order.
    """
    try:
        workspace.logs_dir.mkdir(parents=True, exist_ok=True)
        return DiagnosticLog(workspace.logs_dir, redactor=workspace.redactor, clock=ctx.clock_ms)
    except OSError:
        return None


def _log_safely(log: DiagnosticLog | None, level: LogLevel, event: str, **fields: object) -> None:
    """Write one record, and never let logging be why the companion stopped."""
    if log is None:
        return
    try:
        log.log(level, event, **fields)
    except OSError:
        return


def _log_session(
    log: DiagnosticLog | None, companion: VoiceCompanion, *, adapter: str, ended: str
) -> None:
    """Everything the companion retained, written once at the end.

    Intents and outcomes, never transcripts. :class:`VoiceTurn` carries the
    *understood* intent and what it caused, and that is the half worth keeping:
    it answers "was «стоп» recognised" and "did the stop reach the sidecar"
    without putting a microphone's contents into a file designed to be attached
    to a public issue.
    """
    if log is None:
        return
    for turn in companion.turns:
        _log_safely(
            log,
            LogLevel.INFO,
            "voice.turn",
            intent=turn.intent.value if hasattr(turn.intent, "value") else str(turn.intent),
            state=turn.state.value if hasattr(turn.state, "value") else str(turn.state),
            detail=turn.detail,
            stopped=turn.stop is not None,
        )
    for failure in companion.speech_failures:
        _log_safely(log, LogLevel.WARNING, "voice.speech_failed", detail=failure)
    _log_safely(
        log,
        LogLevel.INFO,
        "voice.finished",
        adapter=adapter,
        detail=ended,
        turns=len(companion.turns),
    )


def _serve(companion: VoiceCompanion) -> tuple[str, int]:
    """Run the companion until its stream ends, and say how that happened.

    Three endings, and the record has to be corrected on all of them — a record
    left saying "listening" by a companion that is not is exactly the disagreement
    between ``status`` and the world that this seam exists to prevent.

    ``KeyboardInterrupt`` is an ending rather than a failure: Ctrl-C is how a user
    stops a foreground command. A backend that raised is a failure, and it is
    reported as one rather than escaping as a traceback: the user's next question
    is whether anything is still listening, and the answer to that is this
    function's return value and the record written from it.
    """
    try:
        asyncio.run(companion.run())
    except KeyboardInterrupt:
        return "the companion stopped: interrupted from the terminal", EXIT_OK
    except Exception as failure:
        return (
            f"the companion stopped: {type(failure).__name__}: {failure}. Nothing is listening now",
            EXIT_FAILURE,
        )
    return "the companion stopped: the adapter's transcript stream ended", EXIT_OK


def _refuse(printer: Printer, workspace: Workspace, *, as_json: bool, detail: str) -> int:
    """Report a refusal and change nothing."""
    message = workspace.redactor.text(detail)
    if as_json:
        printer.json({"started": False, "detail": message})
    else:
        printer.error(message)
    return EXIT_FAILURE
