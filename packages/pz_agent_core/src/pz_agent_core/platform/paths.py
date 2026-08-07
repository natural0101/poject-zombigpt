"""Two kinds of path, and the one function that converts between them.

A path in this project is one of two things, and conflating them is what broke
the Windows build:

**A system path** is where a file actually is. It uses the native separator,
because that is what the operating system takes: ``C:\\Users\\Иван\\Zomboid`` on
Windows, ``/home/ivan/Zomboid`` elsewhere. Anything handed to :mod:`os`, to
:class:`pathlib.Path`, or to a subprocess is one of these, and it is never
rewritten.

**A portable path** is a name *for* a file, recorded so that two machines agree.
It appears in the evidence manifest, the installer manifest, the archive index,
the JSON protocol and every document check — and it always uses ``/``. Not
because ``/`` is better, but because a manifest written on Windows and verified
on Linux has to compare equal, and ``pz-agent\\config.toml`` never equals
``pz-agent/config.toml``.

The separator carries no information a reader wants. Which file it is does.
:func:`portable_relative_path` is where the one becomes the other, and having a
single function means the rule is applied rather than remembered.
"""

from __future__ import annotations

from pathlib import PurePath

__all__ = ["PathNotUnderRoot", "portable_posix", "portable_relative_path"]


class PathNotUnderRoot(ValueError):
    """A path was asked to be recorded relative to a root it is not inside.

    Raised rather than falling back to the absolute path. A manifest entry
    silently holding ``C:\\Users\\Иван\\…`` instead of ``mod/media/x.lua`` is
    both a leak of the account name and an entry no other machine can match, and
    the caller is the only one that can decide what to do about it.
    """


def _matched(path: PurePath | str, root: PurePath | str) -> tuple[PurePath, PurePath]:
    """*path* and *root* as :class:`PurePath` objects of the same flavour.

    Deliberately not ``PurePath(path)`` on both. ``PurePath`` builds the
    *running platform's* flavour, so on Linux it turns a
    :class:`PureWindowsPath` into a :class:`PurePosixPath` whose first part is
    still ``C:\\`` — the separators are already normalised, and the drive keeps
    a trailing backslash it should not have. That coercion is what made this
    module untestable: with the flavour thrown away before ``as_posix`` runs,
    ``as_posix()`` and ``str()`` return the same thing on Linux, so removing the
    one that matters left every Linux test green and broke Windows only.
    Preserving the flavour is what lets a Windows shape be checked anywhere.
    """
    if isinstance(path, PurePath):
        return path, (root if isinstance(root, PurePath) else type(path)(root))
    if isinstance(root, PurePath):
        return type(root)(path), root
    return PurePath(path), PurePath(root)


def portable_relative_path(path: PurePath | str, root: PurePath | str) -> str:
    """*path* relative to *root*, with ``/`` separators, for recording.

    Use for anything that will be compared, hashed or read on another machine:
    manifest entries, archive indexes, protocol documents, document checks.
    Never for opening a file — that is what the original path is for.

    Raises:
        PathNotUnderRoot: when *path* is not inside *root*.
    """
    candidate, base = _matched(path, root)
    try:
        return candidate.relative_to(base).as_posix()
    except ValueError as exc:
        raise PathNotUnderRoot(f"{candidate} is not under {base}") from exc


def portable_posix(path: PurePath | str) -> str:
    """A whole path with ``/`` separators, for a value that has no root.

    The drive and the leading separator are kept — this is a rendering, not a
    relativisation. Use it where a full path has to appear in a document and
    there is nothing to make it relative to; prefer
    :func:`portable_relative_path` whenever there is.
    """
    return (path if isinstance(path, PurePath) else PurePath(path)).as_posix()
