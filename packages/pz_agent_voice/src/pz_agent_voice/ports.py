"""What the voice loop needs from the rest of the sidecar.

Deliberately not a new set of protocols: these are the *same* ports the MCP
boundary reads through. That is the point. A transcript that has been classified
into a :class:`~.messages.VoiceGoal` is submitted as a
:class:`~pz_agent_core.goals.GoalRequest` through
:class:`~pz_agent_mcp.ports.GoalPort`, exactly like a goal that arrived as a
tool call, and it therefore meets the same validation, the same limits and the
same permission policy. Giving voice its own path to the planner would make the
microphone a privileged caller, which is the one thing § "The LLM is not a
privileged caller" of the working agreement rules out.

The goal channel rather than the plan channel
---------------------------------------------

``docs/control/DECISIONS.md`` § "The voice companion routes through
``goal.submit``, not ``plan.execute``" settles which of the two carries a spoken
command, and the reason is parameters. ``PlanRequest.goal`` is a ``str`` with
nowhere to put a quantity: "прочитай двенадцать страниц" has to arrive as
``pages=12`` and be refused at ``pages=999``, and the only place a
:class:`~pz_agent_mcp.ports.PlanRequest` could carry that is inside the goal
string — which would be transcript text on the wire, the one thing this package
exists to prevent. :class:`~pz_agent_core.goals.GoalParams` carries it as a
range-checked field instead.

:attr:`VoiceServices.plans` is kept because a plan is still what a goal is
*served* by: :meth:`~.session.VoiceSession.report_plan` speaks about the plan
records a sidecar pushes in. Nothing in this package submits one.

Stop uses :meth:`~pz_agent_mcp.ports.SessionPort.stop` for the same reason and
one more: it is the shortest path in the system (§ 6.16), needs no armed state,
and is the only call in this package that must work when everything else is
refusing.

Two ports, two transports, and the asymmetry is the design
----------------------------------------------------------

The same protocols, but deliberately not the same wire. :mod:`.plan_port` puts
:class:`~pz_agent_mcp.ports.GoalPort` on the Local Core RPC link, because a goal
has to arrive in the process that owns the planner, the policy engine and the
action queue, and that process is not the one holding the microphone.
:class:`~pz_agent_mcp.ports.SessionPort` stays wherever its owner put it — for
``pz-agent voice run``, the panic latch in the exchange directory, which one
write reaches whether or not a sidecar is listening. A stop that had to dial a
socket, authenticate and wait for an answer would fail in exactly the state a
user reaches for it, so nothing in this package couples the two.

The three ways a remote port says no are re-exported below rather than looked up
at each call site, and they are three rather than one because collapsing them
misinforms: :class:`~pz_agent_mcp.remote.client.SidecarUnavailable` means
nothing was asked, :class:`~pz_agent_mcp.remote.client.CoreRefused` means the
core was asked and declined, and
:class:`~pz_agent_mcp.remote.client.CoreAnswerUnreadable` means the state is
unknown. The companion says one sentence about all three today; the record and
the log it writes should not.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias

from pz_agent_core.goals import (
    GoalAdmission,
    GoalBudget,
    GoalKind,
    GoalParams,
    GoalRecord,
    GoalRefusal,
    GoalRequest,
    GoalState,
)
from pz_agent_mcp.ports import (
    GoalCancellation,
    GoalChannelStatus,
    GoalPort,
    PlanPort,
    PlanRecord,
    PlanRequest,
    SessionPort,
    SessionSnapshot,
    StopReport,
)
from pz_agent_mcp.remote.client import (
    CoreAnswerUnreadable,
    CoreRefused,
    RemoteCoreError,
    SidecarUnavailable,
)

__all__ = [
    "CoreAnswerUnreadable",
    "CoreRefused",
    "GoalAdmission",
    "GoalBudget",
    "GoalCancellation",
    "GoalChannelStatus",
    "GoalKind",
    "GoalParams",
    "GoalPort",
    "GoalRecord",
    "GoalRefusal",
    "GoalRequest",
    "GoalState",
    "IdFactory",
    "PlanPort",
    "PlanRecord",
    "PlanRequest",
    "RemoteCoreError",
    "SessionPort",
    "SessionSnapshot",
    "SidecarUnavailable",
    "StopReport",
    "VoiceServices",
]

#: Mints the idempotency key for one goal submission. Injected so a test can
#: predict it and so a replayed transcript cannot silently become two goals.
IdFactory: TypeAlias = Callable[[], str]


@dataclass(frozen=True, slots=True)
class VoiceServices:
    """The three ports the companion drives, in one injectable bundle.

    ``goals`` is optional for the same reason
    :attr:`~pz_agent_mcp.ports.CoreServices.goals` is, and the default is not a
    convenience: a bundle assembled without the channel — an embedder supplying
    its own ports, or a build wired before the Core RPC link existed — says so
    by leaving this ``None``, and :meth:`~.session.VoiceSession.handle` answers a
    spoken goal with the sentence :mod:`.phrases` keeps for
    :attr:`~pz_agent_core.protocol.ReasonCode.CAPABILITY_UNAVAILABLE`, which
    names exactly that. The alternative, a default port that accepted goals and
    dropped them, is the fabricated success this project does not ship.
    """

    session: SessionPort
    plans: PlanPort
    goals: GoalPort | None = None
