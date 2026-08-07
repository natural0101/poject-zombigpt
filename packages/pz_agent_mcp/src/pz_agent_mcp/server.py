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

import inspect
import json
from typing import Any, Final

from pz_agent_core.protocol import JsonDict
from pz_agent_core.version import PRODUCT_VERSION

from .idempotency import IdempotencyCache
from .ports import CoreServices
from .resources import ResourceReader
from .router import ToolRouter

__all__ = [
    "SERVER_NAME",
    "McpSdkIncompatible",
    "McpSdkUnavailable",
    "build_server",
    "require_sdk",
    "run_stdio",
]

SERVER_NAME = "pz-agent"


class McpSdkUnavailable(RuntimeError):
    """The ``mcp`` package is not installed in this environment."""

    def __init__(self) -> None:
        super().__init__(
            "the MCP SDK is not installed; install the optional extra with "
            "'pip install pz-agent[mcp]' to run the stdio server"
        )


class McpSdkIncompatible(RuntimeError):
    """The ``mcp`` package is installed and is not a version this build can drive.

    A separate type from :class:`McpSdkUnavailable` because the remedy is
    different — a version constraint rather than an install — and because the
    two were indistinguishable to a user for as long as this failure went
    unnoticed. The message names what was looked for, never a traceback.
    """

    def __init__(self, missing: str) -> None:
        super().__init__(
            f"the installed MCP SDK does not accept {missing}, so this build cannot "
            "register its handlers. pz-agent needs the 2.x server API; pin the extra "
            "with 'pip install \"pz-agent[mcp]\"' from a matching release, or install "
            "'mcp>=2,<3'"
        )


#: Constructor keywords the 2.x ``Server`` takes and 1.x did not. Checked rather
#: than assumed: this is the exact difference that let a 1.x-shaped build install
#: cleanly against 2.0 and then die at the first ``initialize``.
_REQUIRED_SERVER_KEYWORDS: Final = (
    "on_list_tools",
    "on_call_tool",
    "on_list_resources",
    "on_read_resource",
)


def require_sdk() -> Any:
    """Import the SDK's low-level server module, or say plainly what is wrong.

    Two distinct failures, because they have two distinct remedies and reporting
    them as one is what hid this for a whole development cycle.

    Raises:
        McpSdkUnavailable: the optional dependency is missing. Reported as a
            missing *install step* rather than an ImportError traceback,
            because that is what the user has to act on.
        McpSdkIncompatible: it is installed and has the wrong server API. The
            check is a signature inspection rather than a version string
            comparison: a version number is a claim about an API and the
            signature *is* the API, so a fork or a pre-release that keeps the
            contract passes and one that breaks it fails, which is the honest
            way round.
    """
    try:
        # Deferred: the SDK is an optional dependency, and every other module in
        # this package must import cleanly without it.
        from mcp.server import lowlevel  # noqa: PLC0415
    except ImportError as exc:
        raise McpSdkUnavailable() from exc

    accepted = inspect.signature(lowlevel.Server.__init__).parameters
    missing = [keyword for keyword in _REQUIRED_SERVER_KEYWORDS if keyword not in accepted]
    if missing:
        raise McpSdkIncompatible(", ".join(missing))
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
    """Wire a router and a resource reader onto an SDK server object.

    Handlers are passed to the constructor, which is the SDK 2.0 contract. The
    1.x spelling — ``server.list_tools()(handler)``, a decorator factory — was
    what this function used until an end-to-end test caught that it had stopped
    working: ``pyproject.toml`` asked for ``mcp>=1.2`` with no upper bound, 2.0
    resolved, and :class:`mcp.server.lowlevel.Server` has none of those four
    methods any more. The child died with ``AttributeError`` before answering
    ``initialize``.

    What made it survive so long is worth stating, because it is the exact
    substitution ``AGENTS.md`` forbids: ``--describe`` still exited 0 with the
    whole catalogue, because the catalogue is computed without the SDK. A
    catalogue is not a server. Only a test that speaks the protocol over a real
    subprocess could tell the difference, and until there was one, nothing did.

    The handler shape changed with the registration: 2.0 passes a request
    context and a typed params object and expects a typed ``*Result`` back,
    where 1.x passed loose arguments and accepted a bare list.
    """
    lowlevel = require_sdk()
    # Deferred with the rest of the SDK; see require_sdk.
    from mcp import types  # noqa: PLC0415

    router = ToolRouter(services, cache=cache)
    reader = ResourceReader(router)

    async def on_list_tools(_context: Any, _params: Any) -> Any:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=descriptor["name"],
                    description=descriptor["description"],
                    # The SDK's field is `input_schema`; `inputSchema` is its wire
                    # alias and pydantic accepts either. Named by field so the type
                    # checker can see it — the serialised form is `inputSchema`.
                    input_schema=descriptor["inputSchema"],
                )
                for descriptor in router.list_tools()
            ]
        )

    async def on_call_tool(_context: Any, params: Any) -> Any:
        payload = router.call(params.name, params.arguments)
        # is_error stays False even for a refused call. The refusal is the
        # answer: it carries a reason code and a retry flag the client acts on,
        # and marking it a protocol error would strip both and leave the client
        # with a transport failure it cannot distinguish from a dead sidecar.
        return types.CallToolResult(content=_text_content(payload), is_error=False)

    async def on_list_resources(_context: Any, _params: Any) -> Any:
        return types.ListResourcesResult(
            resources=[
                types.Resource(
                    uri=descriptor["uri"],
                    name=descriptor["name"],
                    description=descriptor["description"],
                    mime_type=descriptor["mimeType"],
                )
                for descriptor in reader.list()
            ]
        )

    async def on_read_resource(_context: Any, params: Any) -> Any:
        # An unknown URI, a refused read and a crashing port all come back as the
        # error document: the reason code is what the client acts on, and a
        # transport exception would carry neither it nor the retry flag.
        uri = str(params.uri)
        return types.ReadResourceResult(
            contents=[
                types.TextResourceContents(
                    uri=uri,
                    mime_type="application/json",
                    text=json.dumps(reader.read_payload(uri), ensure_ascii=False),
                )
            ]
        )

    server: Any = lowlevel.Server(
        SERVER_NAME,
        version=PRODUCT_VERSION,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=on_list_resources,
        on_read_resource=on_read_resource,
    )
    return server


async def run_stdio(services: CoreServices) -> None:
    """Serve the boundary over stdio until the client disconnects."""
    # Deferred with the rest of the SDK; see require_sdk.
    from mcp.server.stdio import stdio_server  # noqa: PLC0415

    server = build_server(services)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
