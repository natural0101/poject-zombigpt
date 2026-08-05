"""Tolerant reader for Valve's KeyValues ("VDF") text format.

The only document this project needs is Steam's ``libraryfolders.vdf``, and it
only needs the library paths out of it. So the parser is permissive about
everything a Steam client varies — CRLF, tab or space indentation, ``//``
comments, unquoted tokens, platform conditionals such as ``[$WIN32]`` — and
strict about structure. An unbalanced brace raises instead of returning the
tree parsed so far, because a truncated tree is indistinguishable from "this
user has fewer Steam libraries", and silently discovering fewer libraries is
exactly how a game install goes missing.

Depth and token count are bounded: ``pz-agent doctor`` parses whatever file is
on disk, and a corrupt one must not be able to exhaust memory.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Final

#: A VDF value is either a leaf string or a nested object.
VdfValue = str | dict[str, "VdfValue"]

VdfObject = dict[str, VdfValue]

#: Steam's own writer never nests more than a handful of levels; the cap is
#: generous enough that a legitimate file is never rejected.
MAX_VDF_DEPTH: Final = 32

MAX_VDF_TOKENS: Final = 200_000

_KIND_TEXT: Final = "text"
_KIND_OPEN: Final = "open"
_KIND_CLOSE: Final = "close"

_WHITESPACE: Final = " \t\r\n\f\v"
_BARE_TERMINATORS: Final = ' \t\r\n\f\v"{}'

#: Valve escapes only these inside quoted strings. Anything else after a
#: backslash is taken literally, which is what Steam's own reader does.
_ESCAPES: Final[Mapping[str, str]] = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "\\": "\\",
    '"': '"',
}


class VdfError(ValueError):
    """A VDF document is structurally invalid or exceeds a bound."""


def _tokenize(text: str, *, max_tokens: int) -> Iterator[tuple[str, str]]:
    """Yield ``(kind, value)`` pairs.

    Braces are reported as their own kinds rather than as text so that a
    quoted ``"{"`` — legal as a key or value — can never be mistaken for the
    start of an object.
    """
    index = 0
    length = len(text)
    emitted = 0
    while index < length:
        char = text[index]
        if char in _WHITESPACE:
            index += 1
            continue
        if char == "/" and text.startswith("//", index):
            newline = text.find("\n", index)
            index = length if newline < 0 else newline + 1
            continue
        emitted += 1
        if emitted > max_tokens:
            raise VdfError(f"VDF document has more than {max_tokens} tokens")
        if char == "{":
            index += 1
            yield (_KIND_OPEN, char)
            continue
        if char == "}":
            index += 1
            yield (_KIND_CLOSE, char)
            continue
        if char == '"':
            index += 1
            parts: list[str] = []
            while index < length and text[index] != '"':
                current = text[index]
                if current == "\\" and index + 1 < length:
                    following = text[index + 1]
                    parts.append(_ESCAPES.get(following, following))
                    index += 2
                    continue
                parts.append(current)
                index += 1
            if index >= length:
                raise VdfError("unterminated quoted string")
            index += 1
            yield (_KIND_TEXT, "".join(parts))
            continue
        start = index
        while index < length and text[index] not in _BARE_TERMINATORS:
            index += 1
        yield (_KIND_TEXT, text[start:index])


def _is_conditional(token: str) -> bool:
    """True for a platform conditional like ``[$WIN32]``.

    Valve appends these after a statement. Treated as a key they would swallow
    the next token and shift every following pair by one, so they are dropped.
    """
    return len(token) >= 2 and token.startswith("[") and token.endswith("]")


def parse_vdf(
    text: str,
    *,
    max_depth: int = MAX_VDF_DEPTH,
    max_tokens: int = MAX_VDF_TOKENS,
) -> VdfObject:
    """Parse a VDF document into nested dictionaries.

    Duplicate keys resolve last-wins, matching Steam's reader.

    Raises:
        VdfError: on unbalanced braces, an unterminated string, a key without a
            value, or a document past ``max_depth``/``max_tokens``.
    """
    root: VdfObject = {}
    stack: list[VdfObject] = [root]
    pending: str | None = None

    for kind, value in _tokenize(text, max_tokens=max_tokens):
        if kind == _KIND_OPEN:
            if pending is None:
                raise VdfError("'{' does not follow a key")
            if len(stack) >= max_depth:
                raise VdfError(f"VDF nesting deeper than {max_depth} levels")
            child: VdfObject = {}
            stack[-1][pending] = child
            stack.append(child)
            pending = None
        elif kind == _KIND_CLOSE:
            if pending is not None:
                raise VdfError(f"key {pending!r} has no value")
            if len(stack) == 1:
                raise VdfError("'}' without a matching '{'")
            stack.pop()
        elif pending is None:
            if _is_conditional(value):
                continue
            pending = value
        else:
            stack[-1][pending] = value
            pending = None

    if pending is not None:
        raise VdfError(f"key {pending!r} has no value")
    if len(stack) != 1:
        raise VdfError("'{' without a matching '}'")
    return root


def as_object(value: VdfValue | None) -> VdfObject | None:
    """Narrow a value to a nested object, or None when it is a leaf."""
    return value if isinstance(value, dict) else None


def as_text(value: VdfValue | None) -> str | None:
    """Narrow a value to a leaf string, or None when it is an object."""
    return value if isinstance(value, str) else None


def get_ci(obj: Mapping[str, VdfValue], key: str) -> VdfValue | None:
    """Look a key up ignoring case.

    Steam treats KeyValues keys case-insensitively and has shipped both
    ``libraryfolders`` and ``LibraryFolders`` as the root key of the same file.
    """
    direct = obj.get(key)
    if direct is not None:
        return direct
    wanted = key.lower()
    for candidate, value in obj.items():
        if candidate.lower() == wanted:
            return value
    return None
