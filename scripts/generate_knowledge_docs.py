#!/usr/bin/env python3
"""Regenerate the three docs the gameplay knowledge corpus renders.

``docs/BEHAVIOR_REFERENCE.md``, ``docs/BUILD42_MECHANICS_SOURCES.md`` and
``docs/GAMEPLAY_AGENT_GUIDE_RU.md`` all open with "DO NOT EDIT BY HAND": every
byte below their headers is a rendering of ``knowledge/gameplay/*.yaml``. This
is the generator those headers promise, in the same shape as
``generate_playbook.py`` — because a document that claims to be generated and
has no gate is a document that drifts silently.

    scripts/generate_knowledge_docs.py            rewrite the three files
    scripts/generate_knowledge_docs.py --check    exit 1 if any would change

``--check`` is what ``scripts/check.sh`` runs, so a corpus edited without a
regenerate fails the gate rather than shipping stale prose. The corpus itself
is validated separately (``tests/contract/test_knowledge_corpus.py`` and the
loader's own honesty gates); this script refuses to render a corpus the loader
refuses.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages" / "pz_agent_core" / "src"))

from pz_agent_core.knowledge import (  # noqa: E402
    GENERATED_DOCS,
    CorpusError,
    docs_in_sync,
    load_corpus,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift and exit 1 instead of rewriting",
    )
    arguments = parser.parse_args()

    try:
        corpus = load_corpus(REPO_ROOT)
    except CorpusError as refused:
        print(f"the corpus refuses to load: {refused}", file=sys.stderr)
        return 1

    docs_root = REPO_ROOT / "docs"
    stale = docs_in_sync(corpus, docs_root)
    if arguments.check:
        if stale:
            print(
                "generated knowledge docs are out of sync with the corpus: "
                + ", ".join(stale)
                + "; run scripts/generate_knowledge_docs.py",
                file=sys.stderr,
            )
            return 1
        print(f"knowledge docs are up to date ({len(GENERATED_DOCS)} files).")
        return 0

    for name, render in GENERATED_DOCS:
        (docs_root / name).write_text(render(corpus), encoding="utf-8")
    print(f"wrote {len(GENERATED_DOCS)} generated docs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
