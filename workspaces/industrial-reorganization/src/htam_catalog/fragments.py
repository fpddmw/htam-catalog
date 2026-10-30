"""Build a small, source-located process node using the existing v1 card contract."""
from __future__ import annotations

import copy

from .catalog import require, shape, validate_card


def fragment_card(spec):
    """Turn partial, researcher-selected knowledge into a valid v1 card.

    The result can be previewed or saved with the usual immutable revision rules.
    Source positions are literal pointers to the cited material, not inferred
    process boundaries or quantitative inventories.
    """
    shape(spec, {"module_id", "name"},
          {"summary", "source", "sources", "origin", "aliases", "categories",
           "functions", "industries", "inputs", "outputs", "external_refs", "unknowns"},
          "fragment")
    require(("source" in spec) != ("sources" in spec),
            "fragment: supply source or sources")
    sources = [spec["source"]] if "source" in spec else spec["sources"]
    require(isinstance(sources, list) and bool(sources),
            "fragment.sources: at least one source is required")
    for source in sources:
        shape(source, {"id", "kind", "citation", "locator"},
              {"uri", "retrieved_at", "sha256"}, "fragment.source")
        require(isinstance(source["locator"], str) and bool(source["locator"].strip()),
                "fragment source needs a nonempty locator")
    origin = spec.get("origin", "sourced_supplement")
    require(isinstance(origin, str)
            and origin in {"sourced_supplement", "hypothesis", "synthetic"},
            "fragment.origin: expected sourced_supplement, hypothesis or synthetic")
    require(any(spec.get(field) for field in ("aliases", "categories", "functions",
                                              "inputs", "outputs")),
            "fragment: add a function, category, alias, input or output clue")
    interfaces = []
    for key, direction in (("inputs", "input"), ("outputs", "output")):
        value = spec.get(key, [])
        require(isinstance(value, list), f"fragment.{key}: expected a list")
        for number, item in enumerate(value, 1):
            if isinstance(item, str):
                port = {"id": f"{direction}-{number}", "kind": "unspecified",
                        "terms": [item]}
            else:
                require(isinstance(item, dict), f"fragment.{key}: expected text or object")
                port = copy.deepcopy(item)
                require("direction" not in port or port["direction"] == direction,
                        f"fragment.{key}: conflicting interface direction")
                port.setdefault("id", f"{direction}-{number}")
                port.setdefault("kind", "unspecified")
                port.setdefault("terms", [])
            port["direction"] = direction
            interfaces.append(port)
    data = {
        "name": spec["name"],
        "summary": spec.get("summary", spec["name"]),
        "interfaces": interfaces,
        "representations": [{"id": "whole", "kind": "whole"}],
        "unknowns": copy.deepcopy(spec.get("unknowns", [])),
    }
    for field in ("aliases", "categories", "functions", "industries", "external_refs"):
        if field in spec:
            data[field] = copy.deepcopy(spec[field])
    card = {
        "schema_version": 1,
        "module_id": spec["module_id"],
        "revision": 1,
        "sources": copy.deepcopy(sources),
        "extracted": {"origin": "source_extraction", "source_ids": [], "data": {}},
        "local": {"origin": origin,
                  "source_ids": [source["id"] for source in sources],
                  "data": data},
    }
    validate_card(card)
    return card
