#!/usr/bin/env python3
"""Preview or build TianGong-backed process cards from the local source index."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from htam_catalog.tiangong_cards import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
