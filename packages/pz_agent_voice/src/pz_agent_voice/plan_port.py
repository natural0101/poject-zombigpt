"""The companion's route from a spoken goal to the core, over the Core RPC link.

Until this module existed the voice loop had no route at all. The one shipped
:class:`~pz_agent_mcp.ports.PlanPort` a companion could be built with refused
every submission on the grounds that "this build has no channel that carries a
goal into a running sidecar", which stopped being true the moment
:mod:`pz_agent_mcp.remote.client` landed: the Local Core RPC link described in
``docs/CORE_RPC.md`` carries ``goal.submit``, ``goal.status`` and ``goal.cancel``
between any two processes on the machine. A refusal that survives the reason for
it is a feature the user is told does not exist while the wire for it is running.

Goals, not plans
----------------

``docs/control/DECISIONS.md`` § "The voice companion routes through
``goal.submit``, not ``plan.execute``" is why :func:`services_over_core_rpc`
fills :attr:`~.ports.VoiceServices.goals` and why the session submits through it.
The short version is that a spoken quantity has to survive the crossing:
``plan.execute`` takes a goal *string* and there is nowhere in it to put
``pages=12`` that is not a substring of that string, which would be transcript
text on the wire. The plan port is still wired, because a plan is what a goal is
served by and the companion speaks about the plan records a sidecar pushes in,
but nothing in this package submits one.

**No new port, and no second encoder.** What a spoken goal needs is exactly what
a tool call needs — the same method name, the same params, the same codec, the
same three ways of being told no — so this module builds
:class:`~pz_agent_mcp.remote.client.RemoteGoalPort` through
:meth:`~pz_agent_mcp.remote.client.RemoteCoreServices.from_state_dir` and adds
nothing to it. A voice-shaped copy of that class would be a second spelling of
``goal.submit``, a second place for the goal codec to drift, and — worse — a
second path into the engine that the MCP path's tests do not cover. The
microphone is not a privileged caller (§ "The LLM is not a privileged caller"),
and the cheapest way to keep it unprivileged is to give it no code of its own.

What this module *does* own is the deadline
-------------------------------------------

A tool call that waits ten seconds shows an IDE user a spinner. A spoken command
that waits ten seconds is a companion that has gone silent, and a user who is
already repeating themselves — which, with a fresh idempotency key per
transcript, is how one goal becomes two. So the voice path dials with
:data:`VOICE_PLAN_DEADLINE_SECONDS` rather than the transport's default, and
every constructor here refuses a deadline above
:data:`MAX_VOICE_PLAN_DEADLINE_SECONDS` at wiring time rather than on the first
utterance.

The bound is what turns a wedged core into a sentence. The link raises
:class:`~pz_agent_mcp.remote.client.SidecarUnavailable` when the deadline
passes, :meth:`~pz_agent_voice.session.VoiceSession._start_goal` catches it and
says «Не получилось.», and the user learns the goal did not land. Without the
bound the same core produces no exception, no utterance and no turn — the
companion simply stops answering, which is the one failure mode a voice
interface cannot signal.

The bound is honest about what it bounds, too: ``goal.submit`` returns when the
goal has an id and a place in the backlog, not when it has been served, so the
three seconds are three seconds of *admission* and never of the goal's own
wall-clock budget. A submit that waited for the goal would hold the link for the
whole of that budget, which is precisely the window a stop has to get through.

Stop does not come through here
-------------------------------

:func:`services_over_core_rpc` takes the :class:`~pz_agent_mcp.ports.SessionPort`
the caller already has and passes it through untouched. That is deliberate and
it is the whole reason this module exposes a wiring function at all: arm, disarm
and stop stay on whatever short path the caller built them on — for
``pz-agent voice run`` that is the panic latch in the exchange directory, which
one write reaches whether or not a sidecar is running — while goals, and only
goals, take the link. A stop that had to dial, authenticate and wait for an
answer would fail in precisely the state a user reaches for it.
"""

from __future__ import annotations

# Two of the sentences quoted above are members of :mod:`.phrases` reproduced
# exactly, and short enough that every letter in them has a Latin lookalike.
# Taking the confusable-character rule's suggestion would leave a docstring
# quoting a sentence this build never says.
# ruff: noqa: RUF002
from pathlib import Path
from typing import Final

from pz_agent_mcp.remote.client import RemoteCoreServices

from .ports import GoalPort, PlanPort, SessionPort, VoiceServices

__all__ = [
    "MAX_VOICE_PLAN_DEADLINE_SECONDS",
    "VOICE_PLAN_DEADLINE_SECONDS",
    "core_goal_port",
    "core_plan_port",
    "services_over_core_rpc",
]

#: How long a spoken goal waits for the core before the companion says it did
#: not land. Three seconds is the far side of what a person reads as an answer
#: rather than as silence, and the call it bounds is short by construction:
#: ``goal.submit`` returns when the goal has an id, not when it has finished, so
#: the core is not doing the goal's work inside this window.
VOICE_PLAN_DEADLINE_SECONDS: Final = 3.0

#: The ceiling a caller may raise the deadline to. Written as its own number
#: rather than borrowed from
#: :data:`~pz_agent_core.rpc.transport.DEFAULT_DEADLINE_SECONDS`, and pinned
#: against it by a test: the voice path may be made as patient as the MCP path
#: and no more, and a transport default that moves should fail there rather than
#: silently drag a spoken turn out with it.
MAX_VOICE_PLAN_DEADLINE_SECONDS: Final = 10.0


def _linked_ports(state_dir: Path, deadline: float) -> tuple[PlanPort, GoalPort]:
    """The two remote ports for the sidecar whose state lives in *state_dir*.

    One :class:`~pz_agent_mcp.remote.client.CoreLink` behind both, because they
    address one sidecar and a second link would be a second place for the
    deadline to be chosen.

    Raises:
        ValueError: *deadline* is not positive or exceeds
            :data:`MAX_VOICE_PLAN_DEADLINE_SECONDS`. Checked here rather than at
            the first utterance because a deadline is wiring, and a wiring
            mistake that only shows up when somebody speaks is one that ships.
        RuntimeError: the bundle came back without a goal channel. Unreachable
            through :class:`~pz_agent_mcp.remote.client.RemoteCoreServices`,
            which always fills the field — the field is optional for embedders
            assembling their own ports, not for this link — and stated rather
            than assumed so that a regression upstream fails at wiring time
            instead of arriving as a companion that hears goals and drops them.
    """
    if not 0 < deadline <= MAX_VOICE_PLAN_DEADLINE_SECONDS:
        raise ValueError(
            f"a spoken goal may wait at most {MAX_VOICE_PLAN_DEADLINE_SECONDS:g}s "
            f"for the core, got {deadline}"
        )
    services = RemoteCoreServices.from_state_dir(state_dir, deadline=deadline)
    goals = services.goals
    if goals is None:
        raise RuntimeError(
            "the Core RPC bundle was assembled without a goal channel, and the voice "
            "companion has nowhere else to put a spoken goal"
        )
    return services.plans, goals


def core_plan_port(state_dir: Path, *, deadline: float = VOICE_PLAN_DEADLINE_SECONDS) -> PlanPort:
    """The plan port for the sidecar whose state lives in *state_dir*.

    Constructing this reads nothing and connects to nothing —
    :class:`~pz_agent_mcp.remote.client.CoreLink` resolves the descriptor and the
    token on every call — so a companion may be started before the sidecar is,
    and a sidecar that restarts underneath it stays reachable. "The sidecar is
    not running" arrives as a refused goal the user is told about, not as a
    companion that failed to start and therefore never heard «стоп».

    Raises:
        ValueError: as :func:`_linked_ports`.
    """
    return _linked_ports(state_dir, deadline)[0]


def core_goal_port(state_dir: Path, *, deadline: float = VOICE_PLAN_DEADLINE_SECONDS) -> GoalPort:
    """The typed goal channel for that same sidecar.

    This is the port a spoken command actually travels through. Like
    :func:`core_plan_port` it resolves nothing at construction, so the ordering
    of ``pz-agent voice run`` and ``pz-agent start`` does not matter.

    Raises:
        ValueError: as :func:`_linked_ports`.
    """
    return _linked_ports(state_dir, deadline)[1]


def services_over_core_rpc(
    session: SessionPort,
    state_dir: Path,
    *,
    deadline: float = VOICE_PLAN_DEADLINE_SECONDS,
) -> VoiceServices:
    """*session* as it stands, plus goals routed over the link at *state_dir*.

    The session port is passed through by identity and is not wrapped, adapted
    or re-implemented: whatever transport the caller gave arm, disarm and stop
    is the transport they keep. Nothing in the bundle this returns can make the
    stop lever depend on the RPC link being up.

    Raises:
        ValueError: as :func:`_linked_ports`.
    """
    plans, goals = _linked_ports(state_dir, deadline)
    return VoiceServices(session=session, plans=plans, goals=goals)
