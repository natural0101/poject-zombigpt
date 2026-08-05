"""Host-machine concerns: where the game lives, and how a save is backed up.

Everything in this package is a pure function of arguments it is handed. The
process environment, the user's home directory and the wall clock are all
injected, which is what lets Windows-shaped path detection and a real backup
round-trip be tested on a Linux CI machine against a temporary tree.
"""

from __future__ import annotations

from .backup import (
    DEFAULT_MAX_BACKUP_BYTES,
    DEFAULT_MAX_BACKUP_FILES,
    MAX_BACKUP_DIRS,
    MAX_MANIFEST_BYTES,
    BackupCorruptError,
    BackupError,
    BackupFile,
    BackupManager,
    BackupNotFoundError,
    BackupRecord,
    BackupTooLargeError,
    GameRunningError,
    RestoreResult,
    SaveNotFoundError,
)
from .discovery import (
    GAME_DIR_NAMES,
    MAX_LIBRARIES,
    MAX_METADATA_BYTES,
    MAX_PROBLEMS,
    BuildInfo,
    Discovery,
    DiscoveryContext,
    DiscoveryError,
    InstallLocation,
    SteamLibraries,
    SteamLibrary,
    UserDirectory,
    detect_build,
    discover,
    find_install,
    find_steam_libraries,
    find_user_directory,
    steam_root_candidates,
)
from .vdf import VdfError, VdfObject, VdfValue, parse_vdf

__all__ = [
    "DEFAULT_MAX_BACKUP_BYTES",
    "DEFAULT_MAX_BACKUP_FILES",
    "GAME_DIR_NAMES",
    "MAX_BACKUP_DIRS",
    "MAX_LIBRARIES",
    "MAX_MANIFEST_BYTES",
    "MAX_METADATA_BYTES",
    "MAX_PROBLEMS",
    "BackupCorruptError",
    "BackupError",
    "BackupFile",
    "BackupManager",
    "BackupNotFoundError",
    "BackupRecord",
    "BackupTooLargeError",
    "BuildInfo",
    "Discovery",
    "DiscoveryContext",
    "DiscoveryError",
    "GameRunningError",
    "InstallLocation",
    "RestoreResult",
    "SaveNotFoundError",
    "SteamLibraries",
    "SteamLibrary",
    "UserDirectory",
    "VdfError",
    "VdfObject",
    "VdfValue",
    "detect_build",
    "discover",
    "find_install",
    "find_steam_libraries",
    "find_user_directory",
    "parse_vdf",
    "steam_root_candidates",
]
