#!/usr/bin/env python3
"""Run the project's catalog CLI directly from a source checkout."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from htam_catalog.catalog import main

if __name__ == "__main__":
    raise SystemExit(main())
