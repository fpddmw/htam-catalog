#!/usr/bin/env python3
"""Run the TianGong batch sync directly from this source checkout."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from htam_catalog.tiangong_sync import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
