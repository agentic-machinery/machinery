"""Module entry point for ``python -m lu_machinery.cli_health``."""

from __future__ import annotations

from .check import main

if __name__ == "__main__":
    raise SystemExit(main())
