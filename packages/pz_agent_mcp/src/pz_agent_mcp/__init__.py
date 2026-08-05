"""MCP boundary for pz-agent.

A thin adapter over ``pz_agent_core``: it translates MCP tool calls into core
commands and serialises domain errors back out. Policy decisions are made in
core and are never re-implemented here — duplicating them is how a boundary
ends up disagreeing with the engine it fronts.
"""

from __future__ import annotations

from pz_agent_core.version import PRODUCT_VERSION

__all__ = ["PRODUCT_VERSION"]
