"""What the voice loop needs from the rest of the sidecar.

Deliberately not a new set of protocols: these are the *same* ports the MCP
boundary reads through. That is the point. A transcript that has been classified
into a :class:`~.messages.VoiceGoal` is submitted as a
:class:`~pz_agent_mcp.ports.PlanRequest` through
:class:`~pz_agent_mcp.ports.PlanPort`, exactly like a plan that arrived as a
tool call, and it therefore meets the same validation, the same limits and the
same permission policy. Giving voice its own path to the planner would make the
microphone a privileged caller, which is the one thing § "The LLM is not a
privileged caller" of the working agreement rules out.

Stop uses :meth:`~pz_agent_mcp.ports.SessionPort.stop` for the same reason and
one more: it is the shortest path in the system (§ 6.16), needs no armed state,
and is the only call in this package that must work when everything else is
refusing.

Two ports, two transports, and the asymmetry is the design
----------------------------------------------------------

The same protocols, but deliberately not the same wire. :mod:`.plan_port` puts
:class:`~pz_agent_mcp.ports.PlanPort` on the Local Core RPC link, because a goal
has to arrive in the process that owns the planner, the policy engine and the
action queue, and that process is not the one holding the microphone.
:class:`~pz_agent_mcp.ports.SessionPort` stays wherever its owner put it — for
``pz-agent voice run``, the panic latch in the exchange directory, which one
write reaches whether or not a sidecar is listening. A stop that had to dial a
socket, authenticate and wait for an answer would fail in exactly the state a
user reaches for it, so nothing in this package couples the two.

The three ways a plan port says no are re-exported below rather than looked up
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

from pz_agent_mcp.ports import (
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

#: Mints the idempotency key for one plan submission. Injected so a test can
#: predict it and so a replayed transcript cannot silently become two plans.
IdFactory: TypeAlias = Callable[[], str]


@dataclass(frozen=True, slots=True)
class VoiceServices:
    """The two ports the companion drives, in one injectable bundle."""

    session: SessionPort
    plans: PlanPort
