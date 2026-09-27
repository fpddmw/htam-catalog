"""Behavior tests; all writes use temporary, isolated workspaces."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
EXAMPLES = PROJECT / "examples/catalog"
SCRIPT = PROJECT / "scripts/catalog.py"
sys.path.insert(0, str(PROJECT / "src"))
from htam_catalog import catalog as api
ORDER = ["demo-press", "demo-dryer", "demo-blender", "demo-sensitive-receiver",
         "demo-unknown-receiver", "demo-heat-recovery", "demo-conditioning", "demo-line",
         "tiangong-clinker-public-summary"]


def example(module_id="demo-dryer"):
    return api.read_json(EXAMPLES / (module_id + ".json"))


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.catalog = api.Catalog(self.root)
        self.catalog.init()

    def populate(self):
        for module_id in ORDER:
            self.catalog.save(example(module_id), 0)

    def assert_error(self, code, function, *args):
        with self.assertRaises(api.CatalogError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def test_roundtrip_partial_knowledge_and_many_external_refs(self):
        card = example("tiangong-clinker-public-summary")
        card["local"]["data"] = {"external_refs": card["extracted"]["data"]["external_refs"] + [
            {"provider": "test-provider", "id": "synthetic-other", "relation": "candidate", "scope": "Software test only"}]}
        self.catalog.save(card, 0)
        loaded = api.Catalog(self.root).get(card["module_id"])
        self.assertEqual(loaded["card"], card)
        self.assertEqual(len(loaded["effective"]["external_refs"]), 2)
        self.assertEqual(loaded["effective"]["interfaces"], [])
        self.assertTrue(loaded["effective"]["unknowns"])
        self.assertEqual(loaded["field_origins"]["external_refs"]["origin"], "hypothesis")

    def test_revision_conflict_preserves_history(self):
        card = example()
        saved = self.catalog.save(card, 0)
        first_bytes = Path(saved["path"]).read_bytes()
        card["revision"] = 2
        card["local"]["data"]["name"] = "新名称"
        self.assert_error("REVISION_CONFLICT", self.catalog.save, card, 0)
        self.catalog.save(card, 1)
        self.assertEqual(Path(saved["path"]).read_bytes(), first_bytes)
        self.assertEqual(self.catalog.get("demo-dryer", 1)["effective"]["name"], "构造干燥模块")
        self.assertEqual(self.catalog.get("demo-dryer")["effective"]["name"], "新名称")
        self.assert_error("NOT_FOUND", self.catalog.get, "demo-dryer", 99)

    def test_refresh_preserves_local_layer_and_reports_masked_fields(self):
        card = example()
        card["local"]["data"] = {"name": "本地研究命名", "unknowns": ["保留本地问题"]}
        self.catalog.save(card, 0)
        self.catalog.reindex()
        extraction = copy.deepcopy(card["extracted"])
        extraction["data"]["name"] = "来源新名称"
        extraction["data"]["aliases"] = ["new-source-alias"]
        result = self.catalog.refresh("demo-dryer", {"extracted": extraction, "sources": []}, 1)
        updated = self.catalog.get("demo-dryer")
        self.assertEqual(updated["card"]["local"], card["local"])
        self.assertEqual(updated["effective"]["name"], "本地研究命名")
        self.assertEqual(result["change"]["locally_overridden_fields"], ["name"])
        self.assertTrue(result["change"]["review_required"])
        self.assertEqual(self.catalog.search("new-source-alias")[0]["revision"], 2)
        self.assertEqual(self.catalog.get("demo-dryer", 1)["card"], card)

    def test_refresh_citation_identity_conflict_is_explicit(self):
        card = example()
        self.catalog.save(card, 0)
        sources = copy.deepcopy(card["sources"])
        sources[0]["citation"] = "Changed source under same identity"
        self.assert_error("SOURCE_CONFLICT", self.catalog.refresh, "demo-dryer",
                          {"extracted": card["extracted"], "sources": sources}, 1)
        self.assertEqual(self.catalog.revisions("demo-dryer"), [1])

    def test_search_alias_function_interface_and_unknowns(self):
        self.populate()
        self.assertEqual(self.catalog.search("DRYER")[0]["module_id"], "demo-dryer")
        result = self.catalog.search(function="接收", input_term="矿物固体")
        self.assertEqual(len(result), 3)
        self.assertTrue(all(len(row["match_reasons"]) == 2 for row in result))
        self.assertEqual(self.catalog.search(category="公用工程", output_term="热量")[0]["module_id"], "demo-heat-recovery")
        self.assertEqual(self.catalog.search(input_term="配制物料"), [])
        partial = self.catalog.search("phosphogypsum")[0]
        self.assertTrue(partial["data"]["unknowns"])

    def test_candidates_report_pass_fail_unknown_separately(self):
        self.populate()
        rows = self.catalog.candidates("demo-dryer", "product")
        states = {r["to"]["module_id"]: r["condition_status"] for r in rows}
        self.assertEqual(states, {"demo-blender": "satisfied", "demo-sensitive-receiver": "not_satisfied",
                                  "demo-unknown-receiver": "unknown"})
        unknown = next(r for r in rows if r["to"]["module_id"] == "demo-unknown-receiver")
        self.assertEqual(unknown["checks"][0]["reason"], "missing_value")
        self.assertIn("unassessed", unknown["scope"])

    def test_zero_missing_value_units_and_basis(self):
        output = example()["extracted"]["data"]["interfaces"][1]
        target = example("demo-blender")["extracted"]["data"]["interfaces"][0]
        output["properties"][0]["value"] = 0
        self.assertEqual(api.check_requirements(output, target)[0]["status"], "satisfied")
        output["properties"][0]["basis"] = "dry-mass"
        self.assertEqual(api.check_requirements(output, target)[0]["reason"], "unit_or_basis_mismatch")
        output["properties"][0]["value"] = None
        self.assertEqual(api.check_requirements(output, target)[0]["reason"], "missing_value")
        output["properties"][0].update(value=0, unit="", basis="")
        target["requirements"][0].update(unit="", basis="")
        self.assertEqual(api.check_requirements(output, target)[0]["reason"], "missing_unit_or_basis")

    def test_no_declared_requirements_is_unknown(self):
        self.populate()
        results = self.catalog.candidates("demo-heat-recovery", "heat")
        self.assertTrue(results)
        self.assertTrue(all(row["condition_status"] == "unknown" for row in results))

    def test_three_levels_and_explicit_representation_selection(self):
        self.populate()
        tree = self.catalog.expand("demo-line")
        self.assertEqual(tree["status"], "complete")
        inner = tree["tree"]["children"][0]["children"]
        self.assertEqual([n["path"] for n in inner], ["demo-line/conditioning/press", "demo-line/conditioning/dryer"])
        self.assertEqual(self.catalog.expand("demo-conditioning", representation="whole")["tree"]["children"], [])
        limited = self.catalog.expand("demo-line", depth=1)
        self.assertEqual(limited["status"], "partial")
        self.assertEqual(limited["diagnostics"][0]["code"], "DEPTH_LIMIT")
        self.assertEqual(self.catalog.expand("demo-line", max_nodes=2)["status"], "partial")

    def test_child_revision_is_pinned_after_new_revision(self):
        self.populate()
        dryer = example(); dryer["revision"] = 2; dryer["local"]["data"]["name"] = "新版干燥"
        self.catalog.save(dryer, 1)
        expanded = self.catalog.expand("demo-conditioning")
        dryer_node = expanded["tree"]["children"][1]
        self.assertEqual(dryer_node["revision"], 1)
        self.assertEqual(dryer_node["name"], "构造干燥模块")

    def test_repeated_definition_keeps_distinct_instance_paths(self):
        self.catalog.save(example(), 0)
        card = example("demo-conditioning")
        card["extracted"]["data"]["representations"] = [{"id": "two", "kind": "composite", "children": [
            {"id": "a", "module_id": "demo-dryer", "revision": 1, "representation": "whole"},
            {"id": "b", "module_id": "demo-dryer", "revision": 1, "representation": "whole"}]}]
        self.catalog.save(card, 0)
        children = self.catalog.expand("demo-conditioning")["tree"]["children"]
        self.assertEqual([x["path"] for x in children], ["demo-conditioning/a", "demo-conditioning/b"])

    def test_unresolved_reference_self_cycle_and_bad_port_rejected(self):
        card = example("demo-conditioning")
        self.assert_error("NOT_FOUND", self.catalog.save, card, 0)
        self.assertEqual(self.catalog.revisions(card["module_id"]), [])
        self.catalog.save(example("demo-press"), 0); self.catalog.save(example(), 0)
        card["extracted"]["data"]["representations"][0]["connections"][0]["to"]["port"] = "missing"
        self.assert_error("INVALID_CARD", self.catalog.save, card, 0)
        card = example("demo-conditioning")
        card["extracted"]["data"]["representations"][0]["children"][0]["module_id"] = card["module_id"]
        self.assert_error("CONTAINMENT_CYCLE", self.catalog.save, card, 0)

    def test_index_missing_stale_and_corrupt_are_rebuilt(self):
        self.catalog.save(example(), 0)
        first = self.catalog.search("dryer")
        index = self.root / "index/catalog.json"
        self.assertTrue(index.exists())
        index.write_text("broken", encoding="utf-8")
        self.assertEqual(self.catalog.search("dryer"), first)
        self.catalog.save(example("demo-heat-recovery"), 0)
        self.assertEqual(len(self.catalog.search()), 2)
        index.unlink()
        self.assertEqual(len(self.catalog.reindex()["rows"]), 2)

    def test_changed_source_file_is_not_hidden_by_cache(self):
        result = self.catalog.save(example(), 0)
        self.catalog.reindex()
        path = Path(result["path"])
        altered = api.read_json(path)
        altered["card"]["extracted"]["data"]["name"] = "Untracked edit"
        path.write_text(json.dumps(altered), encoding="utf-8")
        self.assert_error("INTEGRITY_ERROR", self.catalog.search)

    def test_writer_lock_and_atomic_write_failure_leave_history_intact(self):
        self.catalog.save(example(), 0)
        next_card = example(); next_card["revision"] = 2
        with self.catalog.lock():
            self.assert_error("WORKSPACE_BUSY", self.catalog.save, next_card, 1)
        from unittest.mock import patch
        with patch.object(api.os, "replace", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                self.catalog.save(next_card, 1)
            with self.assertRaises(OSError):
                self.catalog.save(example("demo-blender"), 0)
        self.assertEqual(self.catalog.revisions("demo-dryer"), [1])
        self.assertFalse((self.root / ".catalog.lock").exists())
        self.assertEqual(list((self.root / "modules/demo-dryer").glob(".pending-*")), [])
        self.assertEqual([r["module_id"] for r in self.catalog.search()], ["demo-dryer"])

    def test_validation_rejects_ambiguous_or_unsafe_records(self):
        for change in [lambda c: c.update(module_id="../escape"), lambda c: c.update(revision=True),
                       lambda c: c["extracted"].update(source_ids=["missing"]),
                       lambda c: c["extracted"].update(origin=[]),
                       lambda c: c["extracted"]["data"]["interfaces"][0].update(direction=[]),
                       lambda c: c["extracted"]["data"].update(interfcaes=[]),
                       lambda c: c["extracted"]["data"]["interfaces"].append(c["extracted"]["data"]["interfaces"][0]),
                       lambda c: c["extracted"]["data"]["interfaces"][1]["properties"][0].update(value=float("nan"))]:
            card = example(); change(card)
            self.assert_error("INVALID_CARD", api.validate_card, card)
        bad = Path(self.tmp.name) / "duplicate.json"
        bad.write_text('{"name":"a","name":"b"}', encoding="utf-8")
        self.assert_error("INVALID_JSON", api.read_json, bad)

    def test_recovery_detects_a_containment_cycle_in_imported_history(self):
        self.populate()
        # Simulate externally restored, hash-consistent records with invalid containment.
        path = self.root / "modules/demo-conditioning/00000001.json"
        envelope = api.read_json(path)
        rep = envelope["card"]["extracted"]["data"]["representations"][0]
        rep["children"][0].update(module_id="demo-line", representation="detail")
        envelope["content_hash"] = api.digest(envelope["card"])
        api.atomic_json(path, envelope)
        result = self.catalog.expand("demo-line")
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any(d["code"] == "CONTAINMENT_CYCLE" for d in result["diagnostics"]))

    def test_cli_portable_from_another_directory(self):
        installed = Path(self.tmp.name) / "installed/scripts/catalog.py"
        installed.parent.mkdir(parents=True)
        shutil.copyfile(SCRIPT, installed)
        shutil.copytree(PROJECT / "src/htam_catalog", installed.parents[1] / "src/htam_catalog",
                        ignore=shutil.ignore_patterns("__pycache__"))
        def run(*args):
            proc = subprocess.run([sys.executable, str(installed), "--workspace", str(self.root), *args],
                                  cwd=self.tmp.name, capture_output=True, encoding="utf-8", timeout=20)
            return proc, json.loads(proc.stdout or proc.stderr)
        proc, data = run("save", "--file", str(EXAMPLES / "demo-dryer.json"), "--expect-revision", "0")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["result"]["revision"], 1)
        proc, data = run("search", "--function", "干燥")
        self.assertEqual(data["result"][0]["module_id"], "demo-dryer")
        proc, data = run("get", "../escape")
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(data["ok"])


if __name__ == "__main__":
    unittest.main()
