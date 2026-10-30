#!/usr/bin/env python3
"""Run from any directory; the repository is inferred from this file."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
