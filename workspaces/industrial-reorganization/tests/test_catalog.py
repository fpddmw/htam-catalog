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

    def test_source_lookup_supports_many_to_many_module_mapping(self):
        def ref(source_id, relation, scope):
            return {"provider": "tiangong-lca", "id": source_id,
                    "resolved_version": "01.00.000", "relation": relation, "scope": scope}
        first = example()
        first["module_id"] = "curated-one"
        first["extracted"]["data"]["external_refs"] = [
            ref("source-a", "partial", "输入输出线索"),
            ref("source-b", "exact", "整体工艺边界")]
        second = example()
        second["module_id"] = "curated-two"
        second["extracted"]["data"]["external_refs"] = [
            ref("source-a", "aggregate_contains", "聚合来源中的子工序线索")]
        self.catalog.save(first, 0)
        self.catalog.save(second, 0)
        uses = self.catalog.uses_source("tiangong-lca", "source-a", "01.00.000")
        self.assertEqual([item["module_id"] for item in uses], ["curated-one", "curated-two"])
        self.assertEqual([item["reference"]["relation"] for item in uses],
                         ["partial", "aggregate_contains"])
        self.assertEqual([item["module_id"] for item in self.catalog.uses_source("tiangong-lca", "source-b")],
                         ["curated-one"])
        self.assertEqual(self.catalog.uses_source("tiangong-lca", "source-a", "02.00.000"), [])
        updated = copy.deepcopy(first)
        updated["revision"] = 2
        updated["local"]["data"]["external_refs"] = [ref("source-b", "exact", "修订后的来源")]
        self.catalog.save(updated, 1)
        uses_old = self.catalog.uses_source("tiangong-lca", "source-a", "01.00.000")
        self.assertEqual([(item["module_id"], item["revision"], item["is_latest"],
                           item["reachable_from_latest"])
                          for item in uses_old], [("curated-one", 1, False, False),
                                                  ("curated-two", 1, True, True)])
        self.assertEqual([(item["revision"], item["is_latest"], item["reachable_from_latest"])
                          for item in self.catalog.uses_source("tiangong-lca", "source-b")],
                         [(1, False, False), (2, True, True)])
        self.assertFalse((self.root / "index").exists())

    def test_flow_lookup_keeps_unversioned_references_visible(self):
        exact = example()
        exact["module_id"] = "exact-flow"
        exact["extracted"]["data"]["interfaces"][0]["flow_refs"] = [
            {"provider": "tiangong-lca", "id": "flow-a", "version": "01.00.000"}]
        unversioned = example()
        unversioned["module_id"] = "unversioned-flow"
        unversioned["extracted"]["data"]["interfaces"][0]["flow_refs"] = [
            {"provider": "tiangong-lca", "id": "flow-a"}]
        self.catalog.save(exact, 0)
        self.catalog.save(unversioned, 0)
        matches = self.catalog.uses_flow("tiangong-lca", "flow-a", "01.00.000")
        self.assertEqual([(item["module_id"], item["version_match"])
                          for item in matches], [("exact-flow", "exact"),
                                                 ("unversioned-flow", "unresolved")])
        self.assertEqual([item["module_id"] for item in self.catalog.uses_flow(
            "tiangong-lca", "flow-a", "02.00.000")], ["unversioned-flow"])
        self.assertTrue(all(item["version_match"] == "not_checked" for item in
                            self.catalog.uses_flow("tiangong-lca", "flow-a")))
        self.assertFalse((self.root / "index").exists())

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

    def test_light_fragment_is_saved_as_v1_and_found_from_its_input(self):
        from htam_catalog.fragments import fragment_card
        spec = {
            "module_id": "located-receiver",
            "name": "构造热量接收片段",
            "source": {"id": "study-a", "kind": "synthetic",
                       "citation": "仅用于软件验证的构造资料",
                       "locator": "section 2 / operation B"},
            "functions": ["接收热量"],
            "inputs": [{"id": "heat", "kind": "energy", "terms": ["热量"]}],
            "unknowns": ["接收工况待核查"],
        }
        card = fragment_card(spec)
        self.assertEqual(card["schema_version"], 1)
        self.assertEqual(card["extracted"]["data"], {})
        self.assertEqual(card["local"]["source_ids"], ["study-a"])
        self.catalog.save(card, 0)
        self.catalog.save(example("demo-heat-recovery"), 0)
        loaded = api.Catalog(self.root).get("located-receiver")
        self.assertEqual(loaded["card"], card)
        self.assertEqual(self.catalog.search(function="接收热量")[0]["module_id"], "located-receiver")
        hits = self.catalog.candidates("located-receiver", "heat")
        self.assertEqual([(hit["from"]["module_id"], hit["to"]["module_id"]) for hit in hits],
                         [("demo-heat-recovery", "located-receiver")])
        self.assertEqual(hits[0]["query"]["direction"], "input")
        self.assertEqual(hits[0]["to"]["sources"][0]["locator"], "section 2 / operation B")
        self.assertEqual(hits[0]["condition_status"], "unknown")
        self.assertIn("接收工况待核查", hits[0]["unknowns"])

    def test_location_paths_keep_repeated_and_pinned_uses_distinct(self):
        self.catalog.save(example(), 0)
        parent = example("demo-conditioning")
        parent["extracted"]["data"]["representations"] = [
            {"id": "twice", "kind": "composite", "children": [
                {"id": "first", "module_id": "demo-dryer", "revision": 1, "representation": "whole"},
                {"id": "second", "module_id": "demo-dryer", "revision": 1, "representation": "whole"}]}]
        self.catalog.save(parent, 0)
        old = self.catalog.locations("demo-dryer", 1)
        self.assertEqual({item["path"] for item in old["occurrences"]
                          if item["root"]["module_id"] == "demo-conditioning"},
                         {"demo-conditioning/first", "demo-conditioning/second"})
        self.assertTrue(all(item["root_representation"] == "twice" for item in old["occurrences"]
                            if item["root"]["module_id"] == "demo-conditioning"))
        newer = example()
        newer["revision"] = 2
        newer["local"]["data"]["name"] = "新修订"
        self.catalog.save(newer, 1)
        self.assertTrue(any(item["path"] == "demo-conditioning/first"
                            for item in self.catalog.locations("demo-dryer", 1)["occurrences"]))
        self.assertFalse(any(item["root"]["module_id"] == "demo-conditioning"
                             for item in self.catalog.locations("demo-dryer", 2)["occurrences"]))

    def test_fragment_cli_preview_and_explicit_save(self):
        spec = {"module_id": "located-input", "name": "构造输入线索",
                "source": {"id": "synthetic-source", "kind": "synthetic",
                           "citation": "软件验证", "locator": "table 1 / row 2"},
                "inputs": ["测试物料"]}
        path = Path(self.tmp.name) / "fragment.json"
        path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        command = [sys.executable, str(SCRIPT), "--workspace", str(self.root),
                   "fragment", "--file", str(path)]
        preview = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertEqual(json.loads(preview.stdout)["result"]["card"]["revision"], 1)
        self.assertEqual(self.catalog.revisions("located-input"), [])
        saved = subprocess.run(command + ["--apply"], capture_output=True, text=True, timeout=20)
        self.assertEqual(saved.returncode, 0, saved.stderr)
        self.assertEqual(self.catalog.locations("located-input")["sources"][0]["locator"],
                         "table 1 / row 2")
        repeated = subprocess.run(command + ["--apply"], capture_output=True, text=True, timeout=20)
        self.assertEqual(json.loads(repeated.stderr)["error"]["code"], "REVISION_CONFLICT")

    def test_fragment_cli_reports_missing_source_id_and_invalid_origin(self):
        spec = {"module_id": "invalid-fragment", "name": "构造片段",
                "source": {"kind": "synthetic", "citation": "软件验证", "locator": "p1"},
                "functions": ["干燥"]}
        path = Path(self.tmp.name) / "invalid-fragment.json"
        for malformed in (spec, {**spec, "source": {**spec["source"], "id": "study"},
                                  "origin": []}):
            path.write_text(json.dumps(malformed, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run([sys.executable, str(SCRIPT), "fragment",
                                     "--file", str(path)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 2)
            error = json.loads(result.stderr)["error"]
            self.assertEqual(error["code"], "INVALID_CARD")
            self.assertNotIn("Traceback", result.stderr)

    def test_candidate_record_keeps_distinct_uses_and_fixed_source_positions(self):
        self.catalog.save(example(), 0)
        self.catalog.save(example("demo-heat-recovery"), 0)
        parent = example("demo-conditioning")
        parent["extracted"]["data"]["representations"] = [
            {"id": "twice", "kind": "composite", "children": [
                {"id": "a", "module_id": "demo-dryer", "revision": 1, "representation": "whole"},
                {"id": "b", "module_id": "demo-dryer", "revision": 1, "representation": "whole"}]}]
        self.catalog.save(parent, 0)
        candidate = {
            "schema_version": 1, "candidate_id": "study-selection", "revision": 1,
            "uses": [
                {"id": "heat-source", "module_id": "demo-heat-recovery",
                 "revision": 1, "representation": "whole"},
                *[{"id": f"dryer-{instance}", "module_id": "demo-dryer",
                   "revision": 1, "representation": "whole",
                   "origin_position": {
                       "root_module_id": "demo-conditioning", "root_revision": 1,
                       "root_representation": "twice",
                       "path": f"demo-conditioning/{instance}"}} for instance in ("a", "b")]],
            "relations": [
                {"id": "heat-supply", "kind": "supply", "basis": "inferred",
                 "reason": "构造的接口线索，实际供热条件待查",
                 "from": {"use_id": "heat-source", "port_id": "heat"},
                 "to": {"use_id": "dryer-a", "port_id": "heat"},
                 "unknowns": ["供需时段与温度待查"]},
                {"id": "second-use", "kind": "insert", "basis": "hypothesis",
                 "reason": "保留第二个独立使用位置",
                 "to": {"use_id": "dryer-b"}}],
            "unknowns": ["工程数量尚未确定"],
        }
        saved = self.catalog.save_candidate(candidate, 0)
        first_bytes = Path(saved["path"]).read_bytes()
        restored = api.Catalog(self.root).get_candidate("study-selection")["candidate"]
        cli = subprocess.run([sys.executable, str(SCRIPT), "--workspace", str(self.root),
                              "candidate-get", "study-selection"],
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout)["result"]["candidate"]["candidate_id"],
                         "study-selection")
        self.assertEqual([item["id"] for item in restored["uses"]],
                         ["heat-source", "dryer-a", "dryer-b"])
        self.assertNotEqual(restored["uses"][1]["origin_position"]["path"],
                            restored["uses"][2]["origin_position"]["path"])
        self.assertEqual(restored["uses"][0]["content_hash"],
                         self.catalog.get("demo-heat-recovery", 1)["content_hash"])
        self.assertEqual(restored["relations"][0]["unknowns"], ["供需时段与温度待查"])
        updated = copy.deepcopy(restored)
        updated["revision"] = 2
        updated["summary"] = "第二次选择"
        self.catalog.save_candidate(updated, 1)
        self.assertEqual(Path(saved["path"]).read_bytes(), first_bytes)
        self.assertEqual(self.catalog.candidate_revisions("study-selection"), [1, 2])
        self.assertEqual(self.catalog.get_candidate("study-selection", 1)["candidate"]["revision"], 1)

    def test_candidate_rejects_false_observation_and_wrong_fixed_position(self):
        self.catalog.save(example(), 0)
        candidate = {"schema_version": 1, "candidate_id": "invalid-evidence", "revision": 1,
                     "uses": [{"id": "use-a", "module_id": "demo-dryer",
                               "revision": 1, "representation": "whole"}],
                     "relations": [{"id": "observed", "kind": "supply",
                                    "basis": "observed", "reason": "声称已经观察",
                                    "from": {"use_id": "use-a"}}],
                     "unknowns": []}
        self.assert_error("INVALID_CARD", self.catalog.save_candidate, candidate, 0)
        candidate["relations"][0]["basis"] = "hypothesis"
        candidate["uses"][0]["origin_position"] = {
            "root_module_id": "demo-dryer", "root_revision": 1,
            "root_representation": "whole", "path": "demo-dryer/nonexistent"}
        self.assert_error("NOT_FOUND", self.catalog.save_candidate, candidate, 0)
        self.assertEqual(self.catalog.candidate_revisions("invalid-evidence"), [])

    def test_term_candidate_reports_explicit_flow_version_conflict(self):
        source = example("demo-dryer")
        target = example("demo-blender")
        output = next(p for p in source["extracted"]["data"]["interfaces"] if p["id"] == "product")
        input_port = next(p for p in target["extracted"]["data"]["interfaces"] if p["direction"] == "input")
        output["flow_refs"] = [{"provider": "tiangong-lca", "id": "same-flow",
                                "version": "01.00.000"}]
        input_port["flow_refs"] = [{"provider": "tiangong-lca", "id": "same-flow",
                                    "version": "02.00.000"}]
        self.catalog.save(source, 0)
        self.catalog.save(target, 0)
        row = self.catalog.candidates("demo-dryer", "product")[0]
        self.assertEqual(row["to"]["module_id"], "demo-blender")
        self.assertEqual(row["checks"][0]["status"], "satisfied")
        self.assertEqual(row["condition_status"], "unknown")
        self.assertEqual(row["match_reasons"]["shared_flow_ids"], [])
        self.assertEqual(row["match_reasons"]["conflicting_flow_refs"], [{
            "provider": "tiangong-lca", "id": "same-flow", "output_version": "01.00.000",
            "input_version": "02.00.000"}])
        self.assertTrue(row["match_reasons"]["shared_terms"])

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
        uses = self.catalog.uses_module("demo-dryer", 1)
        self.assertEqual([(item["parent_module_id"], item["representation"], item["instance"])
                          for item in uses], [("demo-conditioning", "detail", "dryer")])
        self.assertEqual(self.catalog.uses_module("demo-dryer", 2), [])
        self.assertEqual(self.catalog.uses_module("demo-conditioning", 1)[0]["parent_module_id"], "demo-line")
        conditioning = example("demo-conditioning")
        conditioning["revision"] = 2
        conditioning["local"]["data"]["representations"] = copy.deepcopy(
            conditioning["extracted"]["data"]["representations"])
        conditioning["local"]["data"]["representations"][0]["children"][1]["revision"] = 2
        self.catalog.save(conditioning, 1)
        self.assertEqual([(item["parent_revision"], item["parent_is_latest"],
                           item["parent_reachable_from_latest"])
                          for item in self.catalog.uses_module("demo-dryer", 1)], [(1, False, True)])
        self.assertEqual([(item["parent_revision"], item["parent_is_latest"])
                          for item in self.catalog.uses_module("demo-dryer", 2)], [(2, True)])
        self.assertEqual(self.catalog.uses_module("demo-conditioning", 1)[0]["parent_module_id"], "demo-line")
        line = example("demo-line")
        line["revision"] = 2
        line["local"]["data"]["representations"] = copy.deepcopy(
            line["extracted"]["data"]["representations"])
        line["local"]["data"]["representations"][0]["children"][0]["revision"] = 2
        self.catalog.save(line, 1)
        self.assertFalse(self.catalog.uses_module("demo-dryer", 1)[0]["parent_reachable_from_latest"])
        self.assertFalse(self.catalog.uses_module("demo-conditioning", 1)[0]["parent_reachable_from_latest"])

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
