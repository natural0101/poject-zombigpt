"""Redaction, applied while a record is written rather than afterwards.

The invariant: nothing that leaves :class:`Redactor` carries the user's home
directory, their Windows account name, an absolute path, a Steam id or anything
shaped like a credential. Redaction is a *write* step — every writer in this
package takes a redactor and runs the record through it before the bytes reach
a file — because a post-processing pass only redacts the copies somebody
remembered to run it over, and the file on disk is what gets attached to a
public issue.

Two things make this more than a string replace.

**Case and normalisation.** A Windows account name is rendered by different
subsystems as ``Пользователь``, ``ПОЛЬЗОВАТЕЛЬ`` and in either NFC or NFD, and
a path may arrive with forward slashes, backslashes, doubled backslashes (the
JSON and VDF forms) or percent-encoded. All of those are generated from each
known literal, and matching is case-insensitive over Unicode rather than over
ASCII.

**The username is usually not known.** A record can name a path this process
never learnt about. So the literals are only the first layer: a user-profile
pattern catches ``…/Users/<anyone>`` with a class that does not assume Latin
letters — including the spaces of ``C:\\Users\\John Smith`` and the ``\\\\?\\``
prefix of a long path — and a final pair of rules reduces any surviving absolute
or UNC path to its basename.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

#: The ``Zomboid`` user directory.
USER_DIR_PLACEHOLDER: Final = "<ZOMBOID>"

#: The user's profile directory (``C:\\Users\\<name>``, ``/home/<name>``).
USER_HOME_PLACEHOLDER: Final = "<USER_HOME>"

#: The game installation directory.
INSTALL_PLACEHOLDER: Final = "<GAME_INSTALL>"

#: The account name on its own, wherever it appears outside a path.
USERNAME_PLACEHOLDER: Final = "<USER>"

#: Any other absolute path. The basename is kept: "which file" is diagnostic,
#: "where the user keeps their files" is not.
PATH_PLACEHOLDER: Final = "<PATH>"

# The name matches the security linter's "hardcoded password" heuristic; the
# value is what replaces a credential, which is the opposite of one.
SECRET_PLACEHOLDER: Final = "<REDACTED>"  # noqa: S105

STEAM_ID_PLACEHOLDER: Final = "<STEAM_ID>"

#: Marker appended to a value that was longer than the cap.
TRUNCATION_MARKER: Final = "…<TRUNCATED>"

#: Longest string kept in a record. A diagnostic field is a sentence or a path,
#: never a file, and an unbounded one turns a bounded log into an unbounded one.
MAX_STRING_LEN: Final = 2048

#: Redaction runs over at most this many characters of any single value. A
#: caller that hands over a megabyte gets the head of it, redacted, and a
#: marker — never a regex pass over the whole thing.
MAX_SCAN_CHARS: Final = 64 * 1024

#: How deep :meth:`Redactor.value` recurses before it stops descending.
#: Sized for the deepest document this package writes, which is a tier-1
#: observation diff: every level of the snapshot is wrapped in a ``nested``
#: object, so the diff of a nested inventory is about twice as deep as the
#: inventory. A cap that merely fitted an observation would elide the changed
#: value out of a diff and make the trace unreplayable.
MAX_DEPTH: Final = 16

#: How many entries of one mapping or sequence survive redaction.
MAX_ITEMS: Final = 128

#: Placeholder for a branch cut off by :data:`MAX_DEPTH` or :data:`MAX_ITEMS`.
ELIDED: Final = "<ELIDED>"

#: Characters that may precede a path for it to be recognised as one. Anything
#: else — a word character, ``>`` closing a placeholder, a URL's ``:`` — means
#: the slash is part of something that is not a filesystem path.
_PATH_LEAD: Final = r"(?<![^\s\"'(\[=,])"

#: Windows' extended-length prefix. Long paths (blueprint §14.8) arrive as
#: ``\\?\C:\Users\...``; without this the leading backslashes fail
#: :data:`_PATH_LEAD` and the whole path survives the generic rules.
_LONG_PREFIX: Final = r"(?:\\\\[?.]\\)?"

#: Characters one path segment is made of: explicitly *not* ``\w``, so a
#: Cyrillic or CJK account name is matched by the same rule as an ASCII one.
_SEG_CHARS: Final = r"[^\s\"'<>|*?\r\n\\/]"

#: A word a segment may continue over a space into. ``:`` is excluded so the
#: continuation cannot run into the drive letter of the *next* path in the same
#: sentence and swallow it unredacted.
_SEG_WORD: Final = r"[^\s\"'<>|*?\r\n\\/:]+"

#: One path segment. ``C:\Users\John Smith\Zomboid`` is an ordinary Windows
#: profile (blueprint §14.8 requires spaces to work), and a segment that stopped
#: at the first space would replace ``C:\Users\John`` and leave the surname in
#: the output. The spanning form is taken only when a separator or the end of
#: the value follows, and it spans at most four words, so the rule can neither
#: eat a whole sentence following a path nor backtrack unboundedly.
_SEGMENT: Final = rf"(?:{_SEG_CHARS}+(?:[ ]+{_SEG_WORD}){{1,4}}(?=[\\/]|$)|{_SEG_CHARS}+)"


class RedactionError(ValueError):
    """A redactor could not be built from the inputs given."""


@dataclass(frozen=True, slots=True)
class RedactionRule:
    """One pattern and what replaces it, with a label for the audit report."""

    label: str
    pattern: re.Pattern[str]
    replacement: str

    def apply(self, text: str) -> str:
        return self.pattern.sub(self.replacement, text)

    def matches(self, text: str) -> bool:
        return self.pattern.search(text) is not None


def _normalisations(literal: str) -> set[str]:
    """Every Unicode normalisation of *literal* that a filesystem may hand back.

    Windows and Python's own path handling do not agree on NFC/NFD for non-ASCII
    names, so a redactor built from one form has to match the other.
    """
    return {
        literal,
        unicodedata.normalize("NFC", literal),
        unicodedata.normalize("NFD", literal),
    }


def _literal_variants(literal: str) -> set[str]:
    """Every spelling of *literal* that could appear in a record."""
    variants: set[str] = set()
    for base in _normalisations(literal):
        for separated in (base, base.replace("\\", "/"), base.replace("/", "\\")):
            variants.add(separated)
            variants.add(separated.replace("\\", "\\\\"))
            variants.add(quote(separated, safe=":/"))
    return {variant for variant in variants if variant}


def _literal_rule(
    label: str, literal: str, placeholder: str, *, word: bool = False
) -> RedactionRule:
    """A rule matching every spelling of *literal*.

    ``word=True`` guards the match with word boundaries, which is what keeps a
    short account name from being struck out of the middle of an unrelated word.
    """
    variants = sorted(_literal_variants(literal), key=len, reverse=True)
    body = "|".join(re.escape(variant) for variant in variants)
    prefix, suffix = (r"(?<!\w)", r"(?!\w)") if word else ("", "")
    return RedactionRule(
        label=label,
        pattern=re.compile(f"{prefix}(?:{body}){suffix}", re.IGNORECASE),
        replacement=placeholder,
    )


#: Field names whose *value* is a credential whatever it looks like. Used by
#: :meth:`Redactor.value` on mapping keys, where the text rules cannot help: the
#: key and the value are separate strings and never form one ``token=...`` span.
SECRET_KEY_RE: Final = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|passwd|steam_?id|authorization|credential)"
)

#: Credential shapes. Matched before the path rules so a token that happens to
#: contain a slash is struck out whole rather than half-rewritten.
SECRET_RULES: Final[tuple[RedactionRule, ...]] = (
    RedactionRule(
        label="private_key",
        pattern=re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
        replacement=SECRET_PLACEHOLDER,
    ),
    RedactionRule(
        label="anthropic_key",
        pattern=re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
        replacement=SECRET_PLACEHOLDER,
    ),
    RedactionRule(
        label="openai_key",
        pattern=re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
        replacement=SECRET_PLACEHOLDER,
    ),
    RedactionRule(
        label="github_token",
        pattern=re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
        replacement=SECRET_PLACEHOLDER,
    ),
    RedactionRule(
        label="aws_key_id",
        pattern=re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        replacement=SECRET_PLACEHOLDER,
    ),
    RedactionRule(
        label="bearer_token",
        pattern=re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}"),
        replacement=f"Bearer {SECRET_PLACEHOLDER}",
    ),
    RedactionRule(
        label="credential_assignment",
        pattern=re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd|steam_?id|authorization)\b"
            r"(\"?\s*[:=]\s*\"?)[^\s\"',}\]]+"
        ),
        replacement=rf"\1\2{SECRET_PLACEHOLDER}",
    ),
    RedactionRule(
        label="steam_id64",
        pattern=re.compile(r"\b7656119\d{10}\b"),
        replacement=STEAM_ID_PLACEHOLDER,
    ),
)

#: Path shapes that survive when no literal matched, in the order they run.
GENERIC_PATH_RULES: Final[tuple[RedactionRule, ...]] = (
    RedactionRule(
        label="windows_user_profile",
        pattern=re.compile(rf"(?i){_PATH_LEAD}{_LONG_PREFIX}[A-Za-z]:[\\/]+Users[\\/]+{_SEGMENT}"),
        replacement=USER_HOME_PLACEHOLDER,
    ),
    RedactionRule(
        label="posix_user_profile",
        pattern=re.compile(rf"{_PATH_LEAD}/(?:home|Users)/{_SEGMENT}"),
        replacement=USER_HOME_PLACEHOLDER,
    ),
    RedactionRule(
        label="absolute_path",
        pattern=re.compile(
            rf"{_PATH_LEAD}{_LONG_PREFIX}(?:[A-Za-z]:[\\/]|/)(?:{_SEGMENT}[\\/])*({_SEGMENT})"
        ),
        replacement=rf"{PATH_PLACEHOLDER}/\1",
    ),
    # A UNC path has no drive letter and its leading backslashes fail the lead
    # guard, so it reaches this point untouched. The server and the share name a
    # machine and a person as surely as a home directory does; only the basename
    # is kept, exactly as for a local path.
    RedactionRule(
        label="unc_path",
        pattern=re.compile(
            rf"{_PATH_LEAD}\\\\{_SEGMENT}[\\/]{_SEGMENT}(?:[\\/]{_SEGMENT})*[\\/]({_SEGMENT})"
        ),
        replacement=rf"{PATH_PLACEHOLDER}/\1",
    ),
    RedactionRule(
        label="unc_share",
        pattern=re.compile(rf"{_PATH_LEAD}\\\\{_SEGMENT}[\\/]{_SEGMENT}"),
        replacement=PATH_PLACEHOLDER,
    ),
)


class Redactor:
    """Applies a fixed rule list to strings and to whole JSON documents.

    Rules run in order and each sees the output of the last, so the specific
    literals (the ``Zomboid`` directory, the install, the home directory) get
    their own placeholder before the generic path rule reduces whatever is left
    to a basename.
    """

    def __init__(self, rules: Sequence[RedactionRule]) -> None:
        self._rules = tuple(rules)

    @property
    def rules(self) -> tuple[RedactionRule, ...]:
        return self._rules

    def text(self, value: str) -> str:
        """Redact one string, then bound its length.

        Truncation happens last on purpose: a redacted string holds nothing
        secret, so cutting it cannot expose the head of a token the way cutting
        the raw value could.
        """
        scanned = value if len(value) <= MAX_SCAN_CHARS else value[:MAX_SCAN_CHARS]
        for rule in self._rules:
            scanned = rule.apply(scanned)
        if len(value) > MAX_SCAN_CHARS or len(scanned) > MAX_STRING_LEN:
            return scanned[:MAX_STRING_LEN] + TRUNCATION_MARKER
        return scanned

    def path(self, value: Path) -> str:
        """Redact a path. Convenience for the many callers that hold one."""
        return self.text(str(value))

    def value(self, value: Any, *, depth: int = 0) -> Any:
        """Redact a JSON-shaped value recursively, bounded in depth and width.

        Mapping *keys* are redacted as well: a record that keys a dictionary by
        filename would otherwise leak through the one part of the document a
        value-only walk never looks at.
        """
        if depth >= MAX_DEPTH:
            return ELIDED
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
            return value
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for key, item in value.items():
                if len(out) >= MAX_ITEMS:
                    out[ELIDED] = f"{len(value) - MAX_ITEMS} more key(s)"
                    break
                name = self.text(str(key))
                # In a structured document the name and the value are two
                # separate strings, so the ``token=...`` text rule never sees
                # them together. The key is therefore what marks the value.
                if SECRET_KEY_RE.search(name):
                    out[name] = SECRET_PLACEHOLDER
                    continue
                out[name] = self.value(item, depth=depth + 1)
            return out
        if isinstance(value, (list, tuple)):
            items = [self.value(item, depth=depth + 1) for item in list(value)[:MAX_ITEMS]]
            if len(value) > MAX_ITEMS:
                items.append(f"{ELIDED} {len(value) - MAX_ITEMS} more item(s)")
            return items
        # An unexpected type is stringified rather than dropped: losing the fact
        # that a field was present is worse for diagnosis than an odd rendering.
        return self.text(repr(value))

    def findings(self, text: str) -> tuple[str, ...]:
        """Labels of every rule that still matches *text*.

        Used by the support-bundle verifier, which re-scans the finished archive
        instead of trusting that every writer went through this class.
        """
        return tuple(rule.label for rule in self._rules if rule.matches(text))


def build_redactor(
    *,
    user_dir: Path | None = None,
    home_dir: Path | None = None,
    install_dir: Path | None = None,
    usernames: Iterable[str] = (),
    extra_literals: Mapping[str, str] | None = None,
) -> Redactor:
    """Assemble a redactor for one machine.

    Every argument is optional because ``doctor`` runs before any of them is
    necessarily known, and a redactor that refused to exist without them would
    mean the first records — the ones describing a broken installation — were
    the unredacted ones.
    """
    literal_rules: list[tuple[int, RedactionRule]] = []

    def _add(label: str, literal: str, placeholder: str, *, word: bool = False) -> None:
        if not literal:
            return
        literal_rules.append((len(literal), _literal_rule(label, literal, placeholder, word=word)))

    if user_dir is not None:
        _add("user_dir", str(user_dir), USER_DIR_PLACEHOLDER)
    if install_dir is not None:
        _add("install_dir", str(install_dir), INSTALL_PLACEHOLDER)
    if home_dir is not None:
        _add("home_dir", str(home_dir), USER_HOME_PLACEHOLDER)
    for name, placeholder in (extra_literals or {}).items():
        _add(f"literal:{name}", name, placeholder)

    # Longest literal first: the Zomboid directory is a child of the home
    # directory, and matching the parent first would leave "<USER_HOME>/Zomboid"
    # where the more specific placeholder belongs.
    literal_rules.sort(key=lambda pair: pair[0], reverse=True)
    rules = [rule for _, rule in literal_rules]
    rules.extend(SECRET_RULES)
    rules.extend(GENERIC_PATH_RULES)

    # The bare account name is struck out *after* the path rules, not before.
    # Replacing it first turns "C:\Users\Пользователь\Zomboid" into a string the
    # user-profile pattern no longer recognises, and the path then survives as
    # far as the generic rule — which keeps a basename that is still the name.
    for username in usernames:
        if username:
            rules.append(_literal_rule("username", username, USERNAME_PLACEHOLDER, word=True))
    return Redactor(rules)


def null_redactor() -> Redactor:
    """A redactor that still strips secrets and absolute paths, knowing no literals.

    This is the floor, not an opt-out: there is no code path in this package
    that writes a record without passing it through a redactor.
    """
    return build_redactor()
