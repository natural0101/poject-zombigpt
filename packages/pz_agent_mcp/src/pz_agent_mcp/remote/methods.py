"""The closed set of Core RPC method names, in one place.

Both halves of the link read this module. That is the point: a method name
spelled in the client and again in the server is two spellings that can drift,
and the drift shows up as ``UNKNOWN_METHOD`` at runtime on a user's machine
rather than as a failure here.

Names are ``<group>.<verb>``, matching the port they reach. The grouping is not
decorative: seven of the groups are the seven ports of
:class:`~pz_agent_mcp.ports.CoreServices`, and the eighth — ``goal`` — is the
typed goal channel of :mod:`pz_agent_core.goals`, which
:class:`~pz_agent_mcp.remote.server.CoreRouter` is handed alongside that bundle.
A method that belonged to neither would be a capability the boundary had grown
on its own.
"""

from __future__ import annotations

from typing import Final

__all__ = ["ALL_METHODS", "Method"]


class Method:
    """Every method the router answers.

    A class of constants rather than an enum: these are wire strings, they are
    compared against arbitrary input, and an enum would invite ``Method(value)``
    at the boundary — which raises on unknown input where the router needs to
    answer ``UNKNOWN_METHOD``.
    """

    SESSION_STATUS: Final = "session.status"
    SESSION_ARM: Final = "session.arm"
    SESSION_DISARM: Final = "session.disarm"
    SESSION_STOP: Final = "session.stop"

    OBSERVATION_LATEST: Final = "observation.latest"

    CAPABILITIES_REPORT: Final = "capabilities.report"

    ACTION_SUBMIT: Final = "action.submit"
    ACTION_STATUS: Final = "action.status"

    PLAN_EXECUTE: Final = "plan.execute"
    PLAN_CURRENT: Final = "plan.current"

    # The typed goal channel. Three verbs and no more: a goal is submitted,
    # asked after, and asked to stop. There is no "list" and no "activate" —
    # activation is the channel's own decision and a listing would be a second,
    # unbounded read of a queue whose whole point is that it stays small.
    GOAL_SUBMIT: Final = "goal.submit"
    GOAL_STATUS: Final = "goal.status"
    GOAL_CANCEL: Final = "goal.cancel"

    MEMORY_QUERY: Final = "memory.query"

    DIAGNOSTICS_DOCTOR: Final = "diagnostics.doctor"
    DIAGNOSTICS_TAIL: Final = "diagnostics.tail"


#: Used by the router to reject an unknown name, and by the test that asserts
#: the router implements exactly this set — neither less (a name a client can
#: call and nothing answers) nor more (a method reachable but not declared).
ALL_METHODS: Final = frozenset(
    value
    for name, value in vars(Method).items()
    if not name.startswith("_") and isinstance(value, str)
)
