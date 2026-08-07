"""The stdio entry point, and the only module that touches the MCP SDK.

The SDK is an optional dependency (``pip install pz-agent[mcp]``). Confining the
import to this file is what lets the rest of the boundary — the catalogue, the
validator, the redaction and every gate — be imported, exercised and tested on
an install that does not have it. That is not a convenience: without the split,
the surface would be untestable anywhere the SDK is absent, which is where most
of the work on it happens.

Nothing here decides anything. It registers four handlers that forward to
:class:`~.router.ToolRouter` and :class:`~.resources.ResourceReader` and
serialises what they return. A refusal comes back as the boundary's error
document inside a normal tool result rather than as a transport error, because
``reason_code`` and ``retryable`` are the point of it and an exception across
the wire carries neither.
"""

from __future__ import annotations

import json
from typing import Any

from pz_agent_core.protocol import JsonDict
from pz_agent_core.version import PRODUCT_VERSION

from .idempotency import IdempotencyCache
from .ports import CoreServices
from .resources import ResourceReader
from .router import ToolRouter

__all__ = ["SERVER_NAME", "McpSdkUnavailable", "build_server", "require_sdk", "run_stdio"]

SERVER_NAME = "pz-agent"


class McpSdkUnavailable(RuntimeError):
    """The ``mcp`` package is not installed in this environment."""

    def __init__(self) -> None:
        super().__init__(
            "the MCP SDK is not installed; install the optional extra with "
            "'pip install pz-agent[mcp]' to run the stdio server"
        )


def require_sdk() -> Any:
    """Import the SDK's low-level server module, or say plainly that it is absent.

    Raises:
        McpSdkUnavailable: when the optional dependency is missing. Reported as
            a missing *install step* rather than an ImportError traceback,
            because that is what the user has to act on.
    """
    try:
        # Deferred: the SDK is an optional dependency, and every other module in
        # this package must import cleanly without it.
        from mcp.server import lowlevel  # noqa: PLC0415
    except ImportError as exc:
        raise McpSdkUnavailable() from exc
    return lowlevel


def _text_content(payload: JsonDict) -> list[Any]:
    """One structured document, as the SDK's content list."""
    # Deferred with the rest of the SDK; see require_sdk.
    from mcp import types  # noqa: PLC0415

    return [types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


def build_server(
    services: CoreServices,
    *,
    cache: IdempotencyCache | None = None,
) -> Any:
    """Wire a router and a resource reader onto an SDK server object."""
    lowlevel = require_sdk()
    # Deferred with the rest of the SDK; see require_sdk.
    from mcp import types  # noqa: PLC0415

    router = ToolRouter(services, cache=cache)
    reader = ResourceReader(router)
    server: Any = lowlevel.Server(SERVER_NAME, version=PRODUCT_VERSION)

    async def list_tools() -> list[Any]:
        return [
            types.Tool(
                name=descriptor["name"],
                description=descriptor["description"],
                # The SDK's field is `input_schema`; `inputSchema` is its wire alias
                # and pydantic accepts either. Named by field so the type checker
                # can see it — the serialised form is `inputSchema` regardless.
                input_schema=descriptor["inputSchema"],
            )
            for descriptor in router.list_tools()
        ]

    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[Any]:
        return _text_content(router.call(name, arguments))

    async def list_resources() -> list[Any]:
        return [
            types.Resource(
                uri=descriptor["uri"],
                name=descriptor["name"],
                description=descriptor["description"],
                mime_type=descriptor["mimeType"],
            )
            for descriptor in reader.list()
        ]

    async def read_resource(uri: Any) -> str:
        # An unknown URI, a refused read and a crashing port all come back as the
        # error document: the reason code is what the client acts on, and a
        # transport exception would carry neither it nor the retry flag.
        return json.dumps(reader.read_payload(str(uri)), ensure_ascii=False)

    # Registered by call rather than by decorator: the SDK's decorators are
    # untyped, and a decorator that erases the handler's signature would hide a
    # mismatch between what the SDK passes and what these functions accept.
    server.list_tools()(list_tools)
    server.call_tool()(call_tool)
    server.list_resources()(list_resources)
    server.read_resource()(read_resource)
    return server


async def run_stdio(services: CoreServices) -> None:
    """Serve the boundary over stdio until the client disconnects."""
    # Deferred with the rest of the SDK; see require_sdk.
    from mcp.server.stdio import stdio_server  # noqa: PLC0415

    server = build_server(services)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
