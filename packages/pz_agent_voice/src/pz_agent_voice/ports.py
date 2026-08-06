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

__all__ = [
    "IdFactory",
    "PlanPort",
    "PlanRecord",
    "PlanRequest",
    "SessionPort",
    "SessionSnapshot",
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
