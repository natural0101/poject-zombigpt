"""pz-agent core: domain types and business logic.

This package imports no MCP framework, no UI toolkit and no LLM SDK, and has no
third-party runtime dependencies at all. That constraint is what makes
``provider = "none"`` a real, testable configuration rather than a promise.
"""

from __future__ import annotations

from .version import (
    MOD_INFO_PZVERSION,
    MOD_VERSION,
    PRODUCT_VERSION,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    SUPPORTED_BUILDS,
    TARGET_BUILD,
)

__all__ = [
    "MOD_INFO_PZVERSION",
    "MOD_VERSION",
    "PRODUCT_VERSION",
    "PROTOCOL_VERSION",
    "SCHEMA_VERSION",
    "SUPPORTED_BUILDS",
    "TARGET_BUILD",
]
