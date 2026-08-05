#!/usr/bin/env python3
"""Validate that every document in ``schemas/`` is itself a legal JSON Schema.

This is distinct from the contract tests, which validate *payloads* against the
schemas. Here we only assert the schemas compile — a broken ``$ref`` or a typo
in a keyword would otherwise silently make a validator accept everything.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO_ROOT / "schemas"


def main() -> int:
    try:
        # Deferred: jsonschema lives in the 'dev' extra, and this script must
        # produce a useful message rather than an ImportError without it.
        from jsonschema import Draft202012Validator  # noqa: PLC0415
    except ImportError:
        print("jsonschema is not installed; install the 'dev' extra to run this check")
        return 1

    schemas = sorted(SCHEMA_DIR.glob("*.schema.json"))
    if not schemas:
        print(f"no schemas found under {SCHEMA_DIR}")
        return 1

    problems: list[str] = []
    for path in schemas:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{path.name}: invalid JSON at line {exc.lineno}: {exc.msg}")
            continue
        try:
            Draft202012Validator.check_schema(document)
        except Exception as exc:
            problems.append(f"{path.name}: not a valid schema: {exc}")
            continue
        if "$id" not in document:
            problems.append(f"{path.name}: missing $id")
        if "title" not in document:
            problems.append(f"{path.name}: missing title")

    if problems:
        print(f"{len(problems)} schema problem(s):")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(f"{len(schemas)} schema(s) valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
