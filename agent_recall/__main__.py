"""``python -m agent_recall`` — same entry point as the ``agent-recall`` script."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
