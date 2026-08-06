"""Console-script entry point for ``pz-agent``.

The only place in the package that touches :func:`sys.exit`. Everything below
returns an exit code, which is what lets the tests drive the real commands in
this process instead of through a subprocess.
"""

from __future__ import annotations

import sys

from .app import main


def cli() -> None:
    """Run the CLI and exit with its code."""
    sys.exit(main())


if __name__ == "__main__":
    cli()
