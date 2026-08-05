"""Shared fixtures.

The repository is a multi-package source tree, so tests import from
``packages/*/src`` via the ``pythonpath`` setting in ``pyproject.toml``. This
module adds the fixtures that more than one test package needs.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def schema_dir(repo_root: Path) -> Path:
    return repo_root / "schemas"


@pytest.fixture(scope="session")
def schemas(schema_dir: Path) -> dict[str, dict[str, Any]]:
    """Every schema document, keyed by file stem (e.g. ``observation.schema``)."""
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(schema_dir.glob("*.schema.json"))
    }


@pytest.fixture
def session_id() -> str:
    """A deterministic-looking but valid UUID string for one test."""
    return str(uuid.UUID(int=0x5E5510))


@pytest.fixture
def command_id() -> str:
    return str(uuid.UUID(int=0xC0DE))


@pytest.fixture
def tmp_ipc_dir(tmp_path: Path) -> Iterator[Path]:
    """An isolated stand-in for ``~/Zomboid/Lua/pz_agent/``."""
    ipc = tmp_path / "pz_agent"
    ipc.mkdir()
    (ipc / "logs").mkdir()
    yield ipc
