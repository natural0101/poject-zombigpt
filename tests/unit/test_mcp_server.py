"""The SDK boundary: absent here, and the whole surface still works without it.

The ``mcp`` package is an optional dependency and is not installed in this
environment. These tests pin the two things that follow from that: nothing
outside :mod:`pz_agent_mcp.server` imports it, and the one module that does says
plainly what is missing rather than raising an ImportError at a caller who
cannot read it.
"""

from __future__ import annotations

import importlib
import sys

import pytest

from pz_agent_mcp import server
from pz_agent_mcp.server import McpSdkUnavailable, build_server, require_sdk
from tests.fixtures.mcp_doubles import Doubles

#: Every module of the package except the SDK entry point.
SDK_FREE_MODULES = (
    "pz_agent_mcp",
    "pz_agent_mcp.catalog",
    "pz_agent_mcp.envelope",
    "pz_agent_mcp.idempotency",
    "pz_agent_mcp.ports",
    "pz_agent_mcp.resources",
    "pz_agent_mcp.router",
    "pz_agent_mcp.scrub",
    "pz_agent_mcp.validation",
)

SDK_INSTALLED = importlib.util.find_spec("mcp") is not None


@pytest.mark.parametrize("name", SDK_FREE_MODULES)
def test_every_module_but_the_entry_point_imports_without_the_sdk(name: str) -> None:
    module = importlib.import_module(name)

    assert module is not None


def test_the_package_is_importable_with_mcp_blocked_outright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Stronger than "it happens not to be installed": even with the name
    # poisoned, importing the surface must not touch it.
    monkeypatch.setitem(sys.modules, "mcp", None)
    for name in SDK_FREE_MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)

    for name in SDK_FREE_MODULES:
        assert importlib.import_module(name) is not None


@pytest.mark.skipif(SDK_INSTALLED, reason="the SDK is installed in this environment")
def test_the_missing_sdk_is_reported_as_an_install_step() -> None:
    with pytest.raises(McpSdkUnavailable) as caught:
        require_sdk()

    assert "pz-agent[mcp]" in str(caught.value)


@pytest.mark.skipif(SDK_INSTALLED, reason="the SDK is installed in this environment")
def test_building_a_server_without_the_sdk_fails_with_the_same_message() -> None:
    with pytest.raises(McpSdkUnavailable):
        build_server(Doubles().services)


def test_the_server_name_is_stable() -> None:
    # It is what a client's configuration file names; changing it silently
    # would orphan every existing configuration.
    assert server.SERVER_NAME == "pz-agent"
