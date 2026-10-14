"""Allow invoking One Agent Harness via `python -m harness` on Linux, macOS, and Windows."""

from __future__ import annotations

import sys

from harness.cli import main

if __name__ == "__main__":
    sys.exit(main())
