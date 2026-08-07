"""Local Core RPC: one machine, two processes, no network.

The sidecar owns the session, the observation store and the action engine. The
MCP executable owns none of it and is launched by an MCP client, so everything
it reports it has to ask for. This package is the asking.

Local by construction: a Windows named pipe or a Unix socket, never a TCP port.
There is nothing here to reach from another machine, which is the point — the
system acts on a running game on the user's own computer and an address that
could be dialled from outside would be a new attack surface for no capability.

Read the modules in this order: :mod:`.wire` is the envelope and every reason to
refuse one, :mod:`.token` is the shared secret and the rules around it, and
:mod:`.descriptor` is how a client finds a running server and when it must
decline to.
"""

from __future__ import annotations

from pz_agent_core.rpc.descriptor import (
    DESCRIPTOR_FILENAME,
    DESCRIPTOR_FORMAT,
    FAMILY_PIPE,
    FAMILY_UNIX,
    RUNTIME_DIRNAME,
    DescriptorError,
    RpcDescriptor,
    StaleDescriptor,
    descriptor_path,
    load_descriptor,
    remove_descriptor,
    runtime_dir,
    write_descriptor,
)
from pz_agent_core.rpc.token import (
    MIN_TOKEN_BYTES,
    TOKEN_FILENAME,
    TokenError,
    issue_token,
    read_token,
    revoke_token,
)
from pz_agent_core.rpc.wire import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    ErrorCode,
    ProtocolMismatch,
    RpcError,
    RpcRequest,
    RpcResponse,
    TooLarge,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)

__all__ = [
    "DESCRIPTOR_FILENAME",
    "DESCRIPTOR_FORMAT",
    "FAMILY_PIPE",
    "FAMILY_UNIX",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "MIN_TOKEN_BYTES",
    "RUNTIME_DIRNAME",
    "TOKEN_FILENAME",
    "DescriptorError",
    "ErrorCode",
    "ProtocolMismatch",
    "RpcDescriptor",
    "RpcError",
    "RpcRequest",
    "RpcResponse",
    "StaleDescriptor",
    "TokenError",
    "TooLarge",
    "decode_request",
    "decode_response",
    "descriptor_path",
    "encode_request",
    "encode_response",
    "issue_token",
    "load_descriptor",
    "read_token",
    "remove_descriptor",
    "revoke_token",
    "runtime_dir",
    "write_descriptor",
]
