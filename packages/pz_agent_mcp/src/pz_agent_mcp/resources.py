"""The seven read-only views of ``docs/MCP_TOOLS.md``.

Every resource is a projection of something a tool already answers, and it is
implemented by calling that tool rather than by reading the ports again. Two
paths to one view would be two places for the redaction rules to be applied
differently, and the resource path is the one nobody would notice was weaker.

Reads carry the observation sequence they were built from, which is what a
client uses as an ETag: a resource that has not moved reports the same ``seq``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from pz_agent_core.protocol import JsonDict, ReasonCode

from .catalog import RESOURCES, RESOURCES_BY_URI, ResourceSpec
from .envelope import ToolFailure
from .router import ToolRouter

__all__ = ["ResourceReader"]

#: The observation-derived resources name the tool that builds them and the
#: arguments that make it produce the right slice.
_FROM_TOOL: Final[dict[str, tuple[str, JsonDict]]] = {
    "pz://session/current": ("pz_session_status", {}),
    "pz://observation/latest": ("pz_observe_snapshot", {"detail": "full"}),
    "pz://inventory/current": ("pz_observe_inventory", {"scope": "all", "include_nested": True}),
    "pz://plan/current": ("pz_plan_status", {}),
    "pz://diagnostics/recent": ("pz_debug_tail", {}),
}


class ResourceReader:
    """Reads the published resources through the router that owns the rules."""

    def __init__(self, router: ToolRouter) -> None:
        self._router = router
        self._documents: dict[str, Callable[[], JsonDict]] = {
            "pz://capabilities": router.capability_document,
            "pz://safety/status": router.safety_document,
        }
        known = set(_FROM_TOOL) | set(self._documents)
        unhandled = sorted(spec.uri for spec in RESOURCES if spec.uri not in known)
        if unhandled:
            # A listed resource with no reader would be advertised and then fail.
            raise ValueError(f"published resources without a reader: {unhandled}")

    def list(self) -> list[JsonDict]:
        return [spec.descriptor() for spec in RESOURCES]

    def spec(self, uri: str) -> ResourceSpec:
        found = RESOURCES_BY_URI.get(uri)
        if found is None:
            raise ToolFailure(ReasonCode.INVALID_ARGUMENT, f"no such resource: {uri!r}")
        return found

    def read(self, uri: str) -> JsonDict:
        """The resource's current contents.

        Raises:
            ToolFailure: for an unknown URI, or with the domain code the
                underlying tool refused with — a resource read is not a
                privileged path around a refusal.
        """
        spec = self.spec(uri)
        document = self._documents.get(spec.uri)
        if document is not None:
            return document()
        tool, arguments = _FROM_TOOL[spec.uri]
        return self._router.invoke(tool, arguments).data
