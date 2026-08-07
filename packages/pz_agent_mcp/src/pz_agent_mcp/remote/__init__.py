"""The MCP boundary, talking to a core that is in another process.

:class:`~pz_agent_mcp.ports.CoreServices` is seven ports the router calls. When
the sidecar and the MCP server run in one process, those ports are the real
subsystems. They do not: an MCP client launches ``pz-agent-mcp`` as a
subprocess, so the core is always somewhere else.

:mod:`.client` implements every one of those ports over the Local Core RPC link,
and :mod:`.server` answers on the other side by calling the real ones. Between
them sits :mod:`.codec`, which both import — one definition of each record's
JSON shape rather than two that can drift.

See ``docs/CORE_RPC.md`` for the envelope, the token and the descriptor.
"""

from __future__ import annotations

from pz_agent_mcp.remote.methods import ALL_METHODS, Method

__all__ = ["ALL_METHODS", "Method"]
