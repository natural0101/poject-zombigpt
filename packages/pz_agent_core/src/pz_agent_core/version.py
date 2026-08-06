"""Version source of truth.

Five versions are tracked separately because they move at different speeds
(docs/blueprint/11_REPO_STRUCTURE.md §11.9). The release gate in
``scripts/check_versions.py`` fails if they drift apart from the places that
restate them: ``pyproject.toml``, ``pz-mod/42/mod.info`` and the JSON schemas.
"""

from __future__ import annotations

from typing import Final

#: Version of the shipped product as a whole (matches ``pyproject.toml``).
PRODUCT_VERSION: Final = "0.1.0"

#: Wire protocol spoken between the Lua mod and the sidecar. Major bumps break
#: compatibility; the mod refuses a session whose major differs.
PROTOCOL_VERSION: Final = "1.1"

#: Version stamped into every observation/action-result payload. Tied to the
#: JSON Schema documents under ``schemas/``.
SCHEMA_VERSION: Final = "1.0"

#: Version of the Lua mod (matches ``pz-mod/42/mod.info``).
MOD_VERSION: Final = "0.1.0"

#: Game builds this release has been designed against. The doctor command warns
#: — it does not hard-fail — when the detected build is outside this set, because
#: a point release usually keeps the API surface we rely on.
SUPPORTED_BUILDS: Final = ("42.20",)

#: Build the compatibility probes were authored against.
TARGET_BUILD: Final = "42.20"


def protocol_major(version: str) -> int:
    """Return the major component of a ``MAJOR.MINOR`` protocol version.

    Raises:
        ValueError: if *version* is not a dotted numeric version.
    """
    head, _, _ = version.partition(".")
    if not head.isdigit():
        raise ValueError(f"malformed protocol version: {version!r}")
    return int(head)


def protocol_compatible(remote_version: str) -> bool:
    """True when *remote_version* can be spoken by this build.

    Compatibility is major-only: a peer on ``1.7`` talks to our ``1.0`` because
    minor versions are additive by contract, while ``2.0`` is rejected.
    """
    try:
        return protocol_major(remote_version) == protocol_major(PROTOCOL_VERSION)
    except ValueError:
        return False
