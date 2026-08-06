"""The Windows packaging directory: a launcher, an installer and an uninstaller.

A package rather than two loose scripts so that the module has one unambiguous
name — ``installer.pz_agent_installer`` — however it is reached: imported by a
test from the repository root, or type-checked by path. Nothing here is
importable *from* the shipped packages, and nothing here imports them: an
installer runs before the thing it installs exists.

``INSTALL.md`` is the user-facing half, including the unsigned-binary warning
Windows will show and what to verify instead of a signature.
"""

from __future__ import annotations
