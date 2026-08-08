"""One thing every place that parses JSON off a wire or a disk has to do first.

``json.loads`` is not safe to hand an arbitrary byte string. Two of its failure
modes are not the ``JSONDecodeError`` a caller guards for:

* **Depth.** It recurses once per nesting level, so a document of a thousand
  open brackets — a few kilobytes, well under any byte cap — overflows the
  interpreter with a ``RecursionError``. Catching that after the fact is not a
  recovery: it fires with the stack already spent. The only safe move is to
  measure depth on the raw bytes *before* parsing and refuse past a bound, so
  the parser is never handed a document that could take it there.

* **Numbers.** A bare integer literal of more than a few thousand digits makes
  CPython's integer-string-conversion ceiling raise a plain ``ValueError`` —
  not a ``JSONDecodeError`` — from inside ``json.loads``. A caller that catches
  only ``JSONDecodeError`` lets it through raw.

Both the RPC envelope and the IPC journal read bytes that another process (the
MCP client) or another program (the mod, writing the journal) produced, so both
have to guard both modes. This module is the shared primitive: a pure,
non-recursive depth scan and the fact — stated once — that the widened
``except`` a caller writes must reach ``ValueError``, never only its subclass.
"""

from __future__ import annotations

from typing import Final

_QUOTE: Final = 0x22
_BACKSLASH: Final = 0x5C
_OPENERS: Final = frozenset({0x5B, 0x7B})  # '[' and '{'
_CLOSERS: Final = frozenset({0x5D, 0x7D})  # ']' and '}'


def deepest_nesting(data: bytes) -> int:
    """The deepest array/object nesting in *data*, measured without recursing.

    Brackets and braces outside a string move a counter; a string is skipped
    with its backslash escapes honoured, so a ``"`` after a ``\\`` does not
    close it. The result is the maximum depth reached, which a caller compares
    against its own bound before letting ``json.loads`` see the bytes at all.
    """
    depth = 0
    deepest = 0
    in_string = False
    escaped = False
    for byte in data:
        if in_string:
            if escaped:
                escaped = False
            elif byte == _BACKSLASH:
                escaped = True
            elif byte == _QUOTE:
                in_string = False
            continue
        if byte == _QUOTE:
            in_string = True
        elif byte in _OPENERS:
            depth += 1
            deepest = max(deepest, depth)
        elif byte in _CLOSERS and depth > 0:
            depth -= 1
    return deepest
