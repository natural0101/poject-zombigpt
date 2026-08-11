"""``pz-agent goal`` — submit, inspect and cancel goals from the terminal.

The typed goal channel had two ways in and neither was a keyboard: an MCP tool
call, which needs a language model on the other end of a stdio pipe, and a
Russian phrase, which needs a microphone and a vendor SDK this build does not
ship a binding for. The channel itself, the queue behind it, the deterministic
missions behind that and the Core RPC link to all of it were finished and
tested. A user with a terminal could arm the agent and then had no way to tell
it what to do.

This module is that way in, and it is deliberately the *same* way in: every
submission here is a :class:`~pz_agent_core.goals.GoalRequest` built by the
channel's own constructors and sent over
:class:`~pz_agent_mcp.remote.client.RemoteGoalPort`, the wire
``pz-agent-mcp`` and the voice companion both take. A terminal is not a
privileged caller either — it meets the same fourteen kinds, the same parameter
ranges, the same idempotency rule and the same refusals, and the only thing it
gets that they do not is a message written to be read by a person.

Three verbs, and the two that are missing
-----------------------------------------

``submit``, ``status`` and ``cancel`` are the three, because
:mod:`pz_agent_mcp.remote.methods` carries exactly three goal methods and this
command has no second channel to reach around them with.

There are deliberately **no ``pause`` and ``resume`` verbs**:
:mod:`~pz_agent_mcp.remote.methods` refuses them by design — manual input
already outranks the agent, and suspension belongs to the arbiter. Pausing is
not a thing a user has to ask for: touching the controls *is* the pause (the
loop observes the takeover and parks the goal, which is what ``goal status``
reports under ``paused``), and parking a goal so another may run in front of it
is the needs arbiter's decision, made from an observation rather than from a
command line. A ``pause`` verb here would be a second, slower way to do what the
keyboard already does instantly, and a ``resume`` verb would be this process
asserting that the condition which caused the suspension has passed — which it
cannot observe. What a user can say from here is "stop this goal", and that is
``cancel``.

What is printed, and what is not claimed
----------------------------------------

The channel status carries three tails — ``progress``, ``paused`` and
``report`` — that a port may honestly be unable to answer, and the codec decodes
their absence to ``None`` rather than inventing them
(:mod:`pz_agent_mcp.remote.codec.goals`). ``None`` is therefore two facts in
one: there is no such thing, or nothing said. This command prints
``unreported`` for it and never ``no`` and never ``0``. "The agent is not
paused" and "this build could not tell me whether it is paused" are different
sentences, and only one of them is safe to act on.
"""

from __future__ import annotations

import argparse
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, TypeAlias, get_args, get_type_hints

from pz_agent_core.goals import (
    GOAL_SPECS,
    MAX_GOAL_WALL_MS,
    PARAM_NAMES,
    GoalBudget,
    GoalKind,
    GoalParams,
    GoalRecord,
    GoalRequest,
    LootScope,
    TrainableSkill,
    parse_kind,
    parse_scope,
    parse_skill,
)
from pz_agent_core.protocol import JsonDict
from pz_agent_mcp.ports import GoalChannelStatus
from pz_agent_mcp.remote.client import (
    CoreLink,
    RemoteCoreError,
    RemoteGoalPort,
)

from .context import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, CliContext, Workspace, resolve_workspace
from .output import Printer

__all__ = [
    "GOAL_LINK_SECONDS",
    "GOAL_SUBCOMMANDS",
    "MAX_GOAL_MINUTES",
    "NO_SIDECAR_REMEDY",
    "PARAM_TYPES",
    "UNREPORTED",
    "GoalArgumentError",
    "QueuePlace",
    "add_goal_parser",
    "build_request",
    "goal_port",
    "run_goal",
    "run_goal_cancel",
    "run_goal_status",
    "run_goal_submit",
]

GOAL_SUBCOMMANDS: Final[tuple[str, ...]] = ("submit", "status", "cancel")

#: How long any one of these calls waits for the sidecar. The same bound
#: :func:`~pz_agent_cli.voice.probe_goal_channel` uses, and for the same reason:
#: a person is waiting at a terminal, and each of the three calls is short by
#: construction — ``goal.submit`` returns when the goal has an id and a place in
#: the backlog, not when it has been served, and the other two are reads.
GOAL_LINK_SECONDS: Final = 2.0

#: What to do when nothing is listening. The link's own sentence names
#: ``pz-agent start``, which attaches a sidecar in OBSERVE; this one names the
#: command that also waits for the game and arms, because a user who is trying
#: to submit a goal wants an agent that may act on it.
NO_SIDECAR_REMEDY: Final = "start the sidecar first: pz-agent play"

#: Printed for a field the answer did not carry. Never ``no``, never ``0``: the
#: module docstring says why, and this constant exists so the JSON and the text
#: rendering cannot come to disagree about which word means "nothing was said".
UNREPORTED: Final = "unreported"

#: How every free-text field reaches the terminal: through the workspace's
#: redactor, whose rules are built once per invocation so a path printed here
#: and the same path written into a support bundle are redacted the same way.
Redact: TypeAlias = Callable[[str], str]

#: Milliseconds in the minute ``--minutes`` is spelled in.
_MS_PER_MINUTE: Final = 60_000

#: The longest ``--minutes`` the channel's own wall-clock ceiling admits. The
#: floor is one, which is a fact about the unit rather than about the channel:
#: :data:`~pz_agent_core.goals.MIN_GOAL_WALL_MS` is a second, and a whole minute
#: clears it comfortably.
MAX_GOAL_MINUTES: Final = MAX_GOAL_WALL_MS // _MS_PER_MINUTE


class GoalArgumentError(ValueError):
    """An argument this command will not turn into a goal.

    Raised before anything is dialled and carries the whole refusal, including
    the valid values: a user who mistyped a kind wants the list, and a list
    printed by the command they already ran is worth more than a pointer to a
    document.
    """


# ---------------------------------------------------------------------------
# --param: the keys, and the type each one is read as
# ---------------------------------------------------------------------------


def _whole(raw: str, *, key: str) -> int:
    try:
        return int(raw, 10)
    except ValueError:
        raise GoalArgumentError(
            f"--param {key} takes a whole number, and {raw!r} is not one"
        ) from None


def _number(raw: str, *, key: str) -> float:
    try:
        return float(raw)
    except ValueError:
        raise GoalArgumentError(f"--param {key} takes a number, and {raw!r} is not one") from None


#: What ``take_all=`` accepts. Closed and short on purpose: a flag that took
#: "yes", "y", "1" and "on" would also take "no" as true the day somebody adds
#: a truthiness check, and this one widens a loot sweep to everything on the
#: shelf.
_FLAGS: Final[Mapping[str, bool]] = MappingProxyType({"true": True, "false": False})


def _flag(raw: str, *, key: str) -> bool:
    value = _FLAGS.get(raw.strip().casefold())
    if value is None:
        raise GoalArgumentError(
            f"--param {key} takes {' or '.join(sorted(_FLAGS))}, and {raw!r} is neither"
        )
    return value


def _text(raw: str, *, key: str) -> str:
    """Carried as typed. The parameter's own validator decides what it means.

    ``categories`` is the only one, and :class:`~pz_agent_core.goals.GoalParams`
    parses every token in it against the closed
    :class:`~pz_agent_core.loot.LootCategory` set at construction — so nothing
    here needs to know what a category is, and nothing here may accidentally
    become a second opinion about it.
    """
    del key
    return raw


def _skill(raw: str, *, key: str) -> TrainableSkill:
    skill = parse_skill(raw)
    if skill is None:
        raise GoalArgumentError(
            f"--param {key} is not a skill this build trains. The skills are: "
            f"{', '.join(sorted(member.value for member in TrainableSkill))}"
        )
    return skill


def _scope(raw: str, *, key: str) -> LootScope:
    scope = parse_scope(raw)
    if scope is None:
        raise GoalArgumentError(
            f"--param {key} is not a scope this build carries. The scopes are: "
            f"{', '.join(sorted(member.value for member in LootScope))}"
        )
    return scope


#: How a typed value is read out of a ``KEY=VALUE`` string, by the *type* the
#: parameter is declared with rather than by its name. One entry per type
#: :class:`~pz_agent_core.goals.GoalParams` uses; a parameter declared with a
#: type absent from here fails :func:`_param_types` at import, which is the
#: point — a new parameter must be given a reader, not silently dropped or
#: silently passed through as a string.
_READERS: Final[Mapping[Any, Callable[..., Any]]] = MappingProxyType(
    {
        int: _whole,
        float: _number,
        bool: _flag,
        str: _text,
        TrainableSkill: _skill,
        LootScope: _scope,
    }
)


def _param_types() -> Mapping[str, Any]:
    """Every ``--param`` key, and the type its value is read as.

    Derived from :class:`~pz_agent_core.goals.GoalParams`' own annotations
    rather than restated: a hand-written table here would be a second
    declaration of the parameter surface, and the copy that drifts is always the
    one on the side that only refuses.

    Raises:
        RuntimeError: a parameter is not declared ``T | None`` for exactly one
            ``T``, or that ``T`` has no reader above. Raised at import, the way
            :mod:`pz_agent_core.goals.model` checks its own tables, so a
            parameter this command could not read fails the build rather than a
            user's first attempt to pass it.
    """
    hints = get_type_hints(GoalParams)
    resolved: dict[str, Any] = {}
    for name in PARAM_NAMES:
        declared = [option for option in get_args(hints[name]) if option is not type(None)]
        if len(declared) != 1 or declared[0] not in _READERS:
            raise RuntimeError(
                f"goal parameter {name!r} is declared {hints[name]!r}, which this command "
                "has no way to read from a command line"
            )
        resolved[name] = declared[0]
    return MappingProxyType(resolved)


PARAM_TYPES: Final[Mapping[str, Any]] = _param_types()


def _read_params(pairs: Sequence[str]) -> GoalParams:
    """The ``--param KEY=VALUE`` arguments, as the channel's own parameters.

    Every key is checked against the parameter surface before any value is
    read, an unknown one is refused with the whole list, and a repeated one is
    refused rather than resolved: two values for one parameter is a user who
    believes they set something they did not.

    Which parameters a *kind* accepts is not decided here. That is
    :data:`~pz_agent_core.goals.GOAL_SPECS`' business and
    :class:`~pz_agent_core.goals.GoalRequest` enforces it, so this function
    answers only "is this a parameter, and is this a value of its type".

    Raises:
        GoalArgumentError: a pair is not ``KEY=VALUE``, names something that is
            not a parameter, repeats a parameter, or carries a value its
            parameter's type will not read.
    """
    values: dict[str, Any] = {}
    for pair in pairs:
        key, separator, raw = pair.partition("=")
        if not separator:
            raise GoalArgumentError(f"--param takes KEY=VALUE, and {pair!r} has no '='")
        key = key.strip()
        if key not in PARAM_TYPES:
            raise GoalArgumentError(
                f"--param {key!r} is not a goal parameter. The parameters are: "
                f"{', '.join(sorted(PARAM_TYPES))}"
            )
        if key in values:
            raise GoalArgumentError(f"--param {key} was given twice; it takes one value")
        values[key] = _READERS[PARAM_TYPES[key]](raw, key=key)
    try:
        return GoalParams(**values)
    except ValueError as refused:
        # The channel's own message, quoting the number it rejected — which is
        # the user's own value, typed by them, printed back to their terminal.
        raise GoalArgumentError(
            f"that parameter is not one the channel accepts: {refused}"
        ) from None


def _read_kind(raw: str) -> GoalKind:
    """The kind, through the channel's own door.

    :func:`~pz_agent_core.goals.parse_kind` bounds and folds the token and
    answers a member or nothing. Nothing is refused *with the list*: this is a
    command line, the reader is the person who typed it, and the fourteen goals
    this build carries are a closed set worth printing.

    Raises:
        GoalArgumentError: *raw* is not one of them.
    """
    kind = parse_kind(raw)
    if kind is None:
        raise GoalArgumentError(
            f"{raw!r} is not one of the {len(GoalKind)} goals this build carries. They are: "
            f"{', '.join(sorted(member.value for member in GoalKind))}"
        )
    return kind


def _read_budget(kind: GoalKind, minutes: int | None) -> GoalBudget | None:
    """The budget ``--minutes`` asks for, or none at all.

    ``None`` travels as a missing budget and means "the kind's declared
    default", which is a statement rather than a gap — the same thing
    :class:`~pz_agent_core.goals.GoalRequest` means by it.

    When minutes *are* given, all three bounds are sent, the way
    :meth:`~pz_agent_voice.session.VoiceSession._budget` sends them: the wall
    clock is the user's, and the step count and the pending time to live stay
    the kind's, because a command line has no phrasing for either and inventing
    one would be a number nobody chose.

    Raises:
        GoalArgumentError: the minutes are outside what the channel's own
            :class:`~pz_agent_core.goals.GoalBudget` admits.
    """
    if minutes is None:
        return None
    declared = GOAL_SPECS[kind].budget
    try:
        return GoalBudget(
            max_wall_ms=minutes * _MS_PER_MINUTE,
            max_steps=declared.max_steps,
            pending_ttl_ms=declared.pending_ttl_ms,
        )
    except ValueError:
        raise GoalArgumentError(
            f"--minutes must be between 1 and {MAX_GOAL_MINUTES}, and {minutes} is not"
        ) from None


def build_request(kind_token: str, *, params: Sequence[str], minutes: int | None) -> GoalRequest:
    """One submission, built and validated before anything is dialled.

    The idempotency key is minted here and is fresh for every invocation. That
    is the honest reading of a command line: a user who runs the command twice
    has asked for the goal twice, and a key derived from the arguments would
    make the second run a silent no-op reporting the first run's goal.

    Raises:
        GoalArgumentError: for every way the arguments are not a goal — an
            unknown kind, an unknown or repeated parameter, a value of the
            wrong type or outside its range, minutes the channel will not
            carry, or a parameter set this kind does not take.
    """
    kind = _read_kind(kind_token)
    params_value = _read_params(params)
    budget = _read_budget(kind, minutes)
    try:
        return GoalRequest(
            kind=kind,
            idempotency_key=f"cli.{uuid.uuid4().hex}",
            params=params_value,
            budget=budget,
        )
    except ValueError as refused:
        # The request type's own sentence, which names the kind and the
        # parameters it requires or forbids and never quotes a value.
        raise GoalArgumentError(f"{refused}") from None


# ---------------------------------------------------------------------------
# the link
# ---------------------------------------------------------------------------


def goal_port(workspace: Workspace, *, deadline: float = GOAL_LINK_SECONDS) -> RemoteGoalPort:
    """The goal channel of the sidecar whose state lives in this workspace.

    Constructing it reads nothing and dials nothing —
    :class:`~pz_agent_mcp.remote.client.CoreLink` resolves the descriptor and
    the token on every call — so this cannot fail on a machine with no sidecar,
    and the failure arrives where it can be reported: at the call.
    """
    return RemoteGoalPort(CoreLink(state_dir=workspace.state_dir, deadline=deadline))


def _unreachable(printer: Printer, workspace: Workspace, failure: RemoteCoreError) -> int:
    """Report a link that did not answer, and change nothing.

    The link's own sentence is relayed rather than rewritten: it names what was
    missing, and this process knows strictly less about the failure than the
    layer that raised it. It goes through the workspace's redactor because it
    names files, and it is printed to a terminal and pasted into bug reports.
    """
    printer.error(f"{workspace.redactor.text(str(failure))}. {NO_SIDECAR_REMEDY}")
    return EXIT_FAILURE


# ---------------------------------------------------------------------------
# rendering: what a record and a channel look like to a person
# ---------------------------------------------------------------------------


def _param_value(params: GoalParams, name: str) -> Any:
    """One supplied parameter, as JSON carries it.

    The two enum-valued parameters travel as their values, never as their
    members: a ``StrEnum`` member *is* a string and would serialise
    indistinguishably, which is exactly how a name or an ordinal ends up on a
    wire instead of the value the protocol pins.
    """
    value = getattr(params, name)
    if isinstance(value, TrainableSkill | LootScope):
        return value.value
    return value


def _params_line(params: GoalParams) -> str:
    """The parameters that were supplied, and nothing about the ones that were not."""
    supplied = sorted(params.present())
    if not supplied:
        return "(none)"
    return " ".join(f"{name}={_param_value(params, name)}" for name in supplied)


def _record_dict(record: GoalRecord, *, redact: Redact) -> JsonDict:
    """One goal, as the JSON this command prints.

    ``key_digest`` is deliberately not published, the way
    :class:`~pz_agent_mcp.router.ToolRouter` does not publish it: it is
    value-free by construction and buys a reader nothing the goal id does not.

    ``detail`` goes through the redactor although the channel assembles it from
    constants and identifiers it minted. That is the rule this package holds
    everywhere rather than a suspicion about this one field: a document printed
    to a terminal is a document pasted into a bug report, and the place to make
    an exception is not the one command that talks to another process.
    """
    return {
        "goal_id": record.goal_id,
        "kind": record.kind.value,
        "state": record.state.value,
        "params": {
            name: _param_value(record.params, name) for name in sorted(record.params.present())
        },
        "budget": {
            "max_wall_ms": record.budget.max_wall_ms,
            "max_steps": record.budget.max_steps,
            "pending_ttl_ms": record.budget.pending_ttl_ms,
        },
        "sequence": record.sequence,
        "submitted_at_ms": record.submitted_at_ms,
        "started_at_ms": record.started_at_ms,
        "finished_at_ms": record.finished_at_ms,
        "steps_used": record.steps_used,
        "reason_code": None if record.reason_code is None else record.reason_code.value,
        "evidence_keys": list(record.evidence_keys),
        "detail": redact(record.detail),
        "suspended_by": record.suspended_by,
        "suspensions": record.suspensions,
    }


def _status_dict(status: GoalChannelStatus, *, redact: Redact) -> JsonDict:
    """The whole answer, with the three tails left as ``null`` when unreported.

    ``null`` rather than the word: JSON has a value for "nothing was said" and
    a consumer branching on it must not have to match a string. The word is the
    text rendering's business.
    """
    return {
        "active": None if status.active is None else _record_dict(status.active, redact=redact),
        "pending": [_record_dict(record, redact=redact) for record in status.pending],
        "named": None if status.named is None else _record_dict(status.named, redact=redact),
        "progress": None
        if status.progress is None
        else {"phase": status.progress.phase, "counters": dict(status.progress.counters)},
        "paused": None
        if status.paused is None
        else {
            "goal_id": status.paused.goal_id,
            "kind": status.paused.kind,
            "reason": redact(status.paused.reason),
            "paused_at_ms": status.paused.paused_at_ms,
        },
        "report": None if status.report is None else dict(status.report),
    }


def _progress_line(status: GoalChannelStatus) -> str:
    """The deterministic drive's phase, or the word for "nothing was said"."""
    if status.progress is None:
        return UNREPORTED
    counters = " ".join(
        f"{name}={value}" for name, value in sorted(status.progress.counters.items())
    )
    return f"{status.progress.phase} {counters}".strip()


def _paused_line(status: GoalChannelStatus, workspace: Workspace) -> str:
    """The takeover marker, or the word — never "no".

    The marker's ``reason`` is the loop's own sentence and is redacted like
    every other free text this command prints, rather than trusted on the
    strength of who wrote it.
    """
    paused = status.paused
    if paused is None:
        return UNREPORTED
    return (
        f"{paused.kind} {paused.goal_id} was parked at {paused.paused_at_ms}ms "
        f"({workspace.redactor.text(paused.reason)})"
    )


def _print_record(printer: Printer, record: GoalRecord, workspace: Workspace) -> None:
    """One goal in full, for ``goal status GOAL_ID``."""
    printer.field("goal", record.goal_id)
    printer.field("kind", record.kind.value)
    printer.field("state", record.state.value)
    printer.field("params", _params_line(record.params))
    printer.field("steps", f"{record.steps_used} of {record.budget.max_steps}")
    printer.field("submitted at", f"{record.submitted_at_ms}ms")
    if record.started_at_ms is not None:
        printer.field("started at", f"{record.started_at_ms}ms")
    if record.finished_at_ms is not None:
        printer.field("finished at", f"{record.finished_at_ms}ms")
    if record.reason_code is not None:
        printer.field("reason", record.reason_code.value)
    if record.evidence_keys:
        printer.field("evidence", ", ".join(record.evidence_keys))
    # Printed only when there is something to say. "Suspended: no" would be a
    # claim built from a default the wire may never have carried, which is the
    # thing this command does not do.
    if record.suspensions:
        printer.field("suspensions", str(record.suspensions))
    if record.suspended_by is not None:
        printer.field("stepped aside for", record.suspended_by)
    if record.detail:
        printer.field("detail", workspace.redactor.text(record.detail))


def _summary(record: GoalRecord) -> str:
    return f"{record.kind.value} [{record.state.value}] {record.goal_id}"


# ---------------------------------------------------------------------------
# where a submitted goal stands, observed rather than assumed
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueuePlace:
    """Where the channel says a goal stands, right after it was admitted.

    Read from a second ``goal.status`` call rather than inferred from the
    admission: the admission carries the record, and a record's ``sequence`` is
    the order it was admitted in, not its place in a backlog that other goals
    have left and entered. A position printed from the sequence would be a
    number that looks like a queue position and is not one.

    ``position`` is ``None`` when the channel could not be asked or no longer
    holds the goal in its backlog, and the renderer says so rather than
    printing a zero.
    """

    active: bool
    position: int | None
    waiting: int
    detail: str

    def to_dict(self) -> JsonDict:
        return {
            "active": self.active,
            "position": self.position,
            "waiting": self.waiting,
            "detail": self.detail,
        }


def _place_of(goal_id: str, status: GoalChannelStatus) -> QueuePlace:
    """Where *goal_id* stands in *status*."""
    waiting = len(status.pending)
    if status.active is not None and status.active.goal_id == goal_id:
        return QueuePlace(
            active=True,
            position=0,
            waiting=waiting,
            detail=f"the agent is serving it now, with {waiting} waiting behind it",
        )
    for index, record in enumerate(status.pending, start=1):
        if record.goal_id == goal_id:
            return QueuePlace(
                active=False,
                position=index,
                waiting=waiting,
                detail=f"{index} of {waiting} waiting",
            )
    return QueuePlace(
        active=False,
        position=None,
        waiting=waiting,
        detail=(
            "the channel is not holding it open — it has already ended, or the "
            "answer arrived after it did"
        ),
    )


def _place_after_submit(port: RemoteGoalPort, goal_id: str, *, redact: Redact) -> QueuePlace:
    """Ask the channel where the goal it just admitted stands.

    One read-only call, bounded like every other. A link that dies between the
    submit and this call is not a failed submission — the goal is in — so the
    failure is reported as an unknown position rather than as a failed command.
    The link's sentence names files, so it goes through the redactor like every
    other line here before it reaches a terminal.
    """
    try:
        return _place_of(goal_id, port.status())
    except RemoteCoreError as unreachable:
        return QueuePlace(
            active=False,
            position=None,
            waiting=0,
            detail=redact(
                f"the goal was admitted; its place in the queue was not readable ({unreachable})"
            ),
        )


# ---------------------------------------------------------------------------
# the commands
# ---------------------------------------------------------------------------


def add_goal_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``goal`` group and its three subcommands."""
    group = subparsers.add_parser(
        "goal",
        help="submit, inspect and cancel goals",
        description=(
            "Submits to the goal channel of a running sidecar over the local Core RPC "
            "link, which is the same channel an MCP tool call and a spoken goal take. "
            "There is no pause verb: touching the controls is the pause, and the "
            "arbiter owns suspension. Use cancel to stop a goal."
        ),
    )
    inner = group.add_subparsers(dest="goal_command", metavar="SUBCOMMAND")

    submit = inner.add_parser(
        "submit",
        help="send one goal to the running sidecar",
        description=(
            "KIND is one of: " + ", ".join(sorted(member.value for member in GoalKind)) + "."
        ),
    )
    submit.add_argument("kind", metavar="KIND", help="the goal to submit")
    submit.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        dest="params",
        help="one goal parameter; repeat for more. Keys: " + ", ".join(sorted(PARAM_TYPES)),
    )
    submit.add_argument(
        "--minutes",
        type=int,
        default=None,
        metavar="N",
        help=(
            f"wall-clock budget for this goal, 1 to {MAX_GOAL_MINUTES}; "
            "the kind's own default is used when this is left out"
        ),
    )

    status = inner.add_parser(
        "status",
        help="the active goal and the backlog, or one goal in detail",
    )
    status.add_argument(
        "goal_id",
        nargs="?",
        default=None,
        metavar="GOAL_ID",
        help="a goal to report in detail; the whole channel when left out",
    )

    cancel = inner.add_parser("cancel", help="ask the channel to end a goal")
    cancel.add_argument(
        "goal_id", nargs="?", default=None, metavar="GOAL_ID", help="the goal to cancel"
    )
    cancel.add_argument(
        "--all",
        action="store_true",
        dest="all_goals",
        help="cancel every goal the channel is holding open right now",
    )

    for parser in (submit, status, cancel):
        parser.add_argument("--json", action="store_true")

    # The group's own parser, so the no-subcommand branch can print the help a
    # user needs instead of a sentence describing it.
    group.set_defaults(goal_parser=group)


def run_goal(ctx: CliContext, args: argparse.Namespace) -> int:
    """Dispatch a parsed ``goal`` invocation."""
    printer = Printer(ctx.stdout, ctx.stderr)
    subcommand = getattr(args, "goal_command", None)
    if subcommand is None:
        group: argparse.ArgumentParser | None = getattr(args, "goal_parser", None)
        if group is not None:
            group.print_help(ctx.stdout)
        else:
            # A namespace built by hand rather than by the parser above. It
            # reaches here from a caller inside this repository, and the honest
            # answer is the same one, minus the argparse listing.
            printer.error(f"goal needs a subcommand: {', '.join(GOAL_SUBCOMMANDS)}")
        return EXIT_USAGE
    as_json = bool(args.json)
    if subcommand == "submit":
        return run_goal_submit(
            ctx, kind=args.kind, params=args.params, minutes=args.minutes, as_json=as_json
        )
    if subcommand == "status":
        return run_goal_status(ctx, goal_id=args.goal_id, as_json=as_json)
    if subcommand == "cancel":
        return run_goal_cancel(
            ctx, goal_id=args.goal_id, every=bool(args.all_goals), as_json=as_json
        )
    # Unreachable through the parser: every choice it accepts is handled above.
    raise AssertionError(f"unrouted goal subcommand: {subcommand!r}")


def _refuse(printer: Printer, refusal: str, *, as_json: bool) -> int:
    """Report an argument this command will not turn into a goal."""
    if as_json:
        printer.json({"submitted": False, "detail": refusal})
    else:
        printer.error(refusal)
    return EXIT_USAGE


def run_goal_submit(
    ctx: CliContext,
    *,
    kind: str,
    params: Sequence[str],
    minutes: int | None,
    as_json: bool,
) -> int:
    """Handler for ``pz-agent goal submit``.

    Everything that can be refused locally is refused before the link is
    dialled. That order matters on the machine this runs on: a mistyped kind
    would otherwise come back as "the sidecar is not running", which is a true
    sentence about the wrong problem.

    The channel's own refusal — the queue is full, the session is disarmed, the
    kind is not one this sidecar serves — arrives *inside* the admission rather
    than as an exception, and is printed as what it is: the goal did not land,
    and here is the sentence the channel wrote about why.
    """
    printer = Printer(ctx.stdout, ctx.stderr)
    try:
        request = build_request(kind, params=params, minutes=minutes)
    except GoalArgumentError as refused:
        return _refuse(printer, str(refused), as_json=as_json)

    workspace = resolve_workspace(ctx)
    port = goal_port(workspace)
    try:
        admission = port.submit(request)
    except RemoteCoreError as unreachable:
        # All three kinds share one report here — no core, a core that refused
        # the method, an answer this build cannot read — because the user-facing
        # fact is identical: the goal did not land, and nothing on this machine
        # changed. Which one it was is in the link's own sentence, relayed.
        return _unreachable(printer, workspace, unreachable)

    refusal = admission.refusal
    if refusal is not None:
        detail = workspace.redactor.text(refusal.message)
        if as_json:
            printer.json(
                {
                    "submitted": False,
                    "reason_code": refusal.reason_code.value,
                    "detail": detail,
                    "active_goal_id": refusal.active_goal_id,
                }
            )
        else:
            printer.error(f"the channel did not admit the goal: {detail}")
        return EXIT_FAILURE

    goal = admission.goal
    if goal is None:
        # Unreachable through GoalAdmission, whose own invariant is that it
        # carries exactly one of the two; stated rather than assumed so a
        # regression upstream is a refusal here and not an attribute error.
        raise AssertionError("the channel admitted neither a goal nor a refusal")

    place = _place_after_submit(port, goal.goal_id, redact=workspace.redactor.text)
    if as_json:
        printer.json(
            {
                "submitted": True,
                "duplicate": admission.duplicate,
                "goal": _record_dict(goal, redact=workspace.redactor.text),
                "place": place.to_dict(),
            }
        )
        return EXIT_OK
    printer.heading("pz-agent goal submit")
    printer.field("goal", goal.goal_id)
    printer.field("kind", goal.kind.value)
    printer.field("state", goal.state.value)
    printer.field("queue", place.detail)
    if admission.duplicate:
        printer.field("note", "this goal was already in the channel; nothing new was submitted")
    return EXIT_OK


def run_goal_status(ctx: CliContext, *, goal_id: str | None, as_json: bool) -> int:
    """Handler for ``pz-agent goal status``.

    With no id this is a question about the channel: what is being served, what
    is waiting, and what the answer could say about progress and a takeover.
    With an id it is a question about one goal, and an id the channel does not
    know is reported as exactly that rather than as "no goal is running" — the
    two are different answers and only one of them means the goal is gone.
    """
    printer = Printer(ctx.stdout, ctx.stderr)
    workspace = resolve_workspace(ctx)
    try:
        status = goal_port(workspace).status(goal_id)
    except RemoteCoreError as unreachable:
        return _unreachable(printer, workspace, unreachable)

    if goal_id is not None and status.named is None:
        detail = f"the channel is not holding a goal with the id {goal_id}"
        if as_json:
            printer.json({"known": False, "goal_id": goal_id, "detail": detail})
        else:
            printer.error(detail)
        return EXIT_FAILURE

    if as_json:
        printer.json(_status_dict(status, redact=workspace.redactor.text))
        return EXIT_OK

    if status.named is not None:
        printer.heading("pz-agent goal status")
        _print_record(printer, status.named, workspace)
        printer.field("progress", _progress_line(status))
        printer.field("paused", _paused_line(status, workspace))
        printer.field("report", UNREPORTED if status.report is None else "available with --json")
        return EXIT_OK

    printer.heading("pz-agent goal status")
    if status.active is None:
        printer.field("active", "no goal is active")
    else:
        printer.field("active", _summary(status.active))
        printer.field("progress", _progress_line(status))
    printer.field("paused", _paused_line(status, workspace))
    if not status.pending:
        printer.field("waiting", "nothing is waiting")
    else:
        printer.field("waiting", f"{len(status.pending)} goal(s), oldest first")
        for index, record in enumerate(status.pending, start=1):
            marker = (
                "" if record.suspended_by is None else f" (stepped aside for {record.suspended_by})"
            )
            printer.field(f"  {index}", f"{_summary(record)}{marker}")
    return EXIT_OK


def _cancel_one(port: RemoteGoalPort, goal_id: str) -> JsonDict:
    """One ``goal.cancel``, as the document both renderings are built from."""
    cancellation = port.cancel(goal_id)
    goal = cancellation.goal
    return {
        "goal_id": goal_id,
        "requested": cancellation.requested,
        "known": goal is not None,
        "state": None if goal is None else goal.state.value,
    }


def _cancel_line(outcome: Mapping[str, Any]) -> str:
    """What one cancellation says to a person.

    ``requested`` is not "cancelled", and the wording keeps the difference: the
    channel applies a cancellation on its next tick, so an accepted request is
    routinely reported against a goal that is still running.
    """
    if not outcome["known"]:
        return "the channel is not holding a goal with this id"
    if not outcome["requested"]:
        return f"nothing to cancel; it had already ended ({outcome['state']})"
    return f"cancellation requested; the goal is {outcome['state']} until the channel applies it"


def run_goal_cancel(ctx: CliContext, *, goal_id: str | None, every: bool, as_json: bool) -> int:
    """Handler for ``pz-agent goal cancel``.

    ``--all`` iterates the ids one ``goal.status`` answer named, and nothing
    else: the loop is bounded by that snapshot, which the codec has already
    bounded to the backlog a channel remembers. It deliberately does not
    re-read the channel and chase goals that arrived while it was cancelling —
    that is an unbounded loop, and the honest report of it is the one below,
    which says what it was holding when it looked.
    """
    printer = Printer(ctx.stdout, ctx.stderr)
    if (goal_id is None) == (not every):
        printer.error("cancel takes either a goal id or --all, and exactly one of them")
        return EXIT_USAGE

    workspace = resolve_workspace(ctx)
    port = goal_port(workspace)
    try:
        targets: list[str] = [goal_id] if goal_id is not None else _open_ids(port)
        outcomes = [_cancel_one(port, target) for target in targets]
    except RemoteCoreError as unreachable:
        return _unreachable(printer, workspace, unreachable)

    if as_json:
        printer.json({"cancelled": outcomes})
        return EXIT_OK
    printer.heading("pz-agent goal cancel")
    if not outcomes:
        printer.field("channel", "no goal is open, so there was nothing to cancel")
        return EXIT_OK
    for outcome in outcomes:
        printer.field(str(outcome["goal_id"]), _cancel_line(outcome))
    return EXIT_OK


def _open_ids(port: RemoteGoalPort) -> list[str]:
    """Every goal the channel is holding open, in the order it reported them.

    The active goal first, then the backlog oldest first. One snapshot: the
    list is what the channel held when it was asked, and the bound on its
    length is the codec's own cap on a backlog.
    """
    status = port.status()
    active: tuple[GoalRecord, ...] = () if status.active is None else (status.active,)
    return [record.goal_id for record in (*active, *status.pending)]
