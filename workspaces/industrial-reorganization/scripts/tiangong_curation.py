#!/usr/bin/env python3
"""Run evidence-backed TianGong process-to-module curation."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from htam_catalog.tiangong_curation import main


if __name__ == "__main__":
    raise SystemExit(main())
