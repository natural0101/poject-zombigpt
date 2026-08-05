"""The ``pz-agent`` command-line interface.

This is the only package permitted to write to stdout — everything else
communicates through structured results and the diagnostics logger.
"""

from __future__ import annotations

from pz_agent_core.version import PRODUCT_VERSION

__all__ = ["PRODUCT_VERSION"]
