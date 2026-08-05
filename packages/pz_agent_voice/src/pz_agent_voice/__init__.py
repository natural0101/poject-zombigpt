"""Voice companion adapters.

Defines the transport-agnostic ``VoiceAdapter`` protocol plus concrete
implementations. Core never imports this package: speech is an interface to the
agent, not part of its decision loop.
"""

from __future__ import annotations

from pz_agent_core.version import PRODUCT_VERSION

__all__ = ["PRODUCT_VERSION"]
