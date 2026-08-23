"""`python -m harness` — same entry point as the `harness` shim."""
from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
