#!/usr/bin/env python3
"""Populate a fresh, explicit workspace with documented catalog examples."""
import argparse
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from htam_catalog.catalog import Catalog, CatalogError, atomic_json, read_json, require

ORDER = ["demo-press", "demo-dryer", "demo-blender", "demo-sensitive-receiver",
         "demo-unknown-receiver", "demo-heat-recovery", "demo-conditioning", "demo-line",
         "tiangong-clinker-public-summary"]


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    try:
        catalog = Catalog(args.workspace)
        catalog.init()
        require(not catalog.heads(), "Examples require an empty catalog; choose a fresh workspace", "DEMO_NOT_EMPTY")
        examples = PROJECT / "examples/catalog"
        for module_id in ORDER:
            catalog.save(read_json(examples / (module_id + ".json")), 0)
        index = catalog.reindex()
        summary = {
            "scope": "8 synthetic modules and 1 public identity/title summary; no physical or LCA validation",
            "workspace": str(catalog.root), "module_count": len(index["rows"]),
            "search": catalog.search(function="接收", input_term="矿物固体"),
            "candidates": catalog.candidates("demo-dryer", "product"),
            "hierarchy": catalog.expand("demo-line"),
        }
        atomic_json(catalog.root / "runs/demo-summary.json", summary)
        print(json.dumps({"ok": True, "module_count": summary["module_count"],
                          "report": str(catalog.root / "runs/demo-summary.json"),
                          "condition_states": {r["to"]["module_id"]: r["condition_status"] for r in summary["candidates"]},
                          "hierarchy_nodes": summary["hierarchy"]["nodes"]}, ensure_ascii=False, indent=2))
        return 0
    except (CatalogError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": {"code": getattr(exc, "code", "IO_OR_FORMAT_ERROR"), "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
