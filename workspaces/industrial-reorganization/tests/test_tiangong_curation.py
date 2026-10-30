"""Evidence-backed curation of an exact native model into a recursive card."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from htam_catalog.catalog import Catalog, CatalogError
from htam_catalog.tiangong_cards import projected_card
from htam_catalog.tiangong_curation import curate, decision_template, main
from htam_catalog.tiangong_draft import preview_from_files
from htam_catalog.tiangong_sync import ingest_page, open_db, read_db
from htam_catalog.tiangong_whole import (curate as curate_whole,
                                        decision_template as whole_template, source_card)
from test_tiangong_draft import VERSION, native_model, source_process


class CurationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "sources.sqlite"
        self.model_file = self.root / "model.json"
        self.decision_file = self.root / "decision.json"
        self.workspace = self.root / "catalog"
        self.catalog = Catalog(self.workspace)
        self.catalog.init()
        with open_db(self.db_path) as db:
            ingest_page(db, "process", [source_process("aggregate", [
                ("Input", "feed", VERSION), ("Output", "product", VERSION)])], "aggregate")
            ingest_page(db, "process", [source_process("child-a", [
                ("Input", "feed", VERSION), ("Output", "middle", VERSION)])], "child-a")
            ingest_page(db, "process", [source_process("child-b", [
                ("Input", "middle", VERSION), ("Output", "product", VERSION)])], "child-b")
        self.model_file.write_text(json.dumps(native_model()), encoding="utf-8")
        with read_db(self.db_path) as db:
            self.children = {}
            for process_id in ("child-a", "child-b"):
                row = db.execute("SELECT * FROM processes WHERE id=?", (process_id,)).fetchone()
                card = projected_card(db, row)
                self.catalog.save(card, 0)
                self.children[process_id] = card
        self.draft = preview_from_files(self.db_path, "aggregate", VERSION, self.model_file)
        self.decision = self.make_decision()
        self.write_decision()

    def evidence(self, source_id, locator, note):
        return [{"source_id": source_id, "locator": locator, "note": note}]

    def port(self, process_id, direction):
        card = self.children[process_id]
        return next(item["id"] for item in card["extracted"]["data"]["interfaces"]
                    if item["direction"] == direction)

    def make_decision(self):
        draft = self.draft
        model_source = "model-" + draft["model"]["content_hash"][:24]
        report_id = "study-1"
        instances = []
        for native in draft["candidate_topology"]["instances"]:
            card = self.children[native["process_id"]]
            instances.append({"instance_id": native["instance_id"],
                              "module_id": card["module_id"], "revision": 1,
                              "representation": "whole",
                              "evidence": self.evidence(model_source,
                                                        "technology.processes.processInstance",
                                                        "Native instance references this child source")})
        connections = []
        for index, edge in enumerate(draft["candidate_topology"]["connections"]):
            connections.append({"index": index,
                                "from_port": self.port("child-a", "output"),
                                "to_port": self.port("child-b", "input"),
                                "from_exchange_internal_id": "2",
                                "to_exchange_internal_id": "1",
                                "evidence": self.evidence(model_source,
                                                          "technology.processes.processInstance[0].connections",
                                                          "Native connection and exchanges were checked")})
        source_ports = draft["whole_source_card_preview"]["extracted"]["data"]["interfaces"]
        bindings = []
        for source_port in source_ports:
            is_input = source_port["direction"] == "input"
            bindings.append({"source_port_id": source_port["id"],
                             "instance_id": "1" if is_input else "2",
                             "port_id": self.port("child-a" if is_input else "child-b",
                                                  source_port["direction"]),
                             "exchange_internal_id": "1" if is_input else "2",
                             "evidence": self.evidence(report_id, "boundary table",
                                                       "Report assigns the aggregate boundary to this instance")})
        return {"schema": "htam.tiangong-curation-decision.v1",
                "source_process": {k: draft["source_process"][k]
                                   for k in ("id", "version", "content_hash")},
                "model": {k: draft["model"][k] for k in ("id", "version", "content_hash")},
                "module": {"id": "curated-aggregate", "name": "Synthetic aggregate",
                           "summary": "Two steps with repeated use of the second source process.",
                           "source_relation": "partial", "source_scope": "Observed aggregate boundary",
                           "unknowns": ["Exchange quantity basis remains unverified"],
                           "evidence": self.evidence(report_id, "method section",
                                                     "Report describes the module boundary")},
                "evidence_sources": [{"id": report_id, "kind": "synthetic",
                                      "citation": "Synthetic fixture report"}],
                "association_evidence": self.evidence(report_id, "model table",
                                                      "Report associates aggregate and model"),
                "instances": instances, "connections": connections,
                "boundary_bindings": bindings}

    def write_decision(self):
        self.decision_file.write_text(json.dumps(self.decision), encoding="utf-8")

    def run_curate(self, revision=0, apply=False):
        return curate(self.db_path, "aggregate", VERSION, self.model_file,
                      self.decision_file, self.workspace, revision, apply)

    def test_preview_apply_reopen_and_revision(self):
        preview = self.run_curate()
        self.assertEqual(preview["mode"], "preview")
        self.assertEqual(self.catalog.revisions("curated-aggregate"), [])
        applied = self.run_curate(apply=True)
        self.assertEqual(applied["saved"]["revision"], 1)
        loaded = self.catalog.get("curated-aggregate")
        reps = loaded["effective"]["representations"]
        self.assertEqual(len(reps), 2)
        composite = reps[1]
        self.assertEqual([item["id"] for item in composite["children"]], ["1", "2", "3"])
        self.assertEqual(len(composite["connections"]), 2)
        self.assertEqual(len(composite["boundary_bindings"]), 2)
        self.assertEqual(loaded["effective"]["parameters"]["curation_evidence"]["native_instance_factors"],
                         {"1": "1", "2": "2", "3": "3"})
        self.decision["module"]["summary"] = "Revised description from the same source."
        self.write_decision()
        updated = self.run_curate(revision=1, apply=True)
        self.assertEqual(updated["saved"]["revision"], 2)
        self.assertEqual(self.catalog.get("curated-aggregate", 1)["effective"]["summary"],
                         "Two steps with repeated use of the second source process.")

    def test_stale_source_or_model_and_missing_evidence_are_rejected(self):
        self.decision["source_process"]["content_hash"] = "wrong"
        self.write_decision()
        with self.assertRaisesRegex(CatalogError, "content hash changed"):
            self.run_curate()
        self.decision = self.make_decision()
        self.decision["association_evidence"] = []
        self.write_decision()
        with self.assertRaisesRegex(CatalogError, "evidence is required"):
            self.run_curate()
        self.assertEqual(self.catalog.revisions("curated-aggregate"), [])

    def test_wrong_exchange_port_and_missing_boundary_are_rejected(self):
        self.decision["connections"][0]["from_port"] = self.port("child-a", "input")
        self.write_decision()
        with self.assertRaises(CatalogError):
            self.run_curate()
        self.decision = self.make_decision()
        self.decision["boundary_bindings"].pop()
        self.write_decision()
        with self.assertRaisesRegex(CatalogError, "Every source boundary port"):
            self.run_curate()

    def test_child_source_change_requires_a_current_pinned_card(self):
        with open_db(self.db_path) as db:
            changed = source_process("child-a", [
                ("Input", "feed", VERSION), ("Output", "middle", VERSION)])
            changed["process"]["processDataSet"]["exchanges"]["exchange"][1]["meanAmount"] = "2"
            ingest_page(db, "process", [changed], "child-a-new")
        with self.assertRaisesRegex(CatalogError, "current exact native process content"):
            self.run_curate()

    def test_cli_template_leaves_semantic_fields_open(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--db", str(self.db_path), "--process-id", "aggregate",
                         "--version", VERSION, "--model-file", str(self.model_file),
                         "--template"])
        self.assertEqual(code, 0)
        template = json.loads(output.getvalue())
        self.assertIsNone(template["module"]["id"])
        self.assertEqual([item["instance_id"] for item in template["instances"]],
                         ["1", "2", "3"])
        self.assertEqual(self.catalog.revisions("curated-aggregate"), [])

    def test_native_association_prefills_its_evidence_and_conflict_blocks_curation(self):
        model = native_model()
        info = model["lifeCycleModelDataSet"]["lifeCycleModelInformation"]["dataSetInformation"]
        info["referenceToResultingProcess"] = {"@type": "process data set",
                                               "@refObjectId": "aggregate", "@version": VERSION}
        self.model_file.write_text(json.dumps(model), encoding="utf-8")
        linked = preview_from_files(self.db_path, "aggregate", VERSION, self.model_file)
        template = decision_template(linked)
        self.assertEqual(template["association_evidence"][0]["locator"],
                         linked["source_process"]["association_locator"])
        self.assertIsNone(template["module"]["id"])
        info["referenceToResultingProcess"]["@refObjectId"] = "other-aggregate"
        self.model_file.write_text(json.dumps(model), encoding="utf-8")
        conflict = preview_from_files(self.db_path, "aggregate", VERSION, self.model_file)
        self.assertEqual(conflict["source_process"]["association_to_model"],
                         "native_resulting_process_conflict")
        self.decision["model"]["content_hash"] = conflict["model"]["content_hash"]
        self.write_decision()
        with self.assertRaisesRegex(CatalogError, "different resulting process"):
            self.run_curate()

    def test_parent_source_change_is_reviewed_with_new_decision(self):
        self.run_curate(apply=True)
        with open_db(self.db_path) as db:
            changed = source_process("aggregate", [
                ("Input", "feed", VERSION), ("Output", "product", VERSION)])
            changed["process"]["processDataSet"]["exchanges"]["exchange"][0]["meanAmount"] = "4"
            ingest_page(db, "process", [changed], "aggregate-new")
        fresh = preview_from_files(self.db_path, "aggregate", VERSION, self.model_file)
        self.decision["source_process"]["content_hash"] = fresh["source_process"]["content_hash"]
        self.write_decision()
        preview = self.run_curate(revision=1)
        self.assertTrue(preview["source_change"]["review_required"])
        self.assertEqual(preview["card"]["extracted"],
                         fresh["whole_source_card_preview"]["extracted"])

    def test_composite_can_pin_a_curated_whole_child_revision(self):
        source = source_card(self.db_path, "child-a", VERSION)
        child_decision = whole_template(source)
        child_decision["module"] = {
            "id": source["module_id"], "name": "Reviewed child A",
            "summary": "A sourced first step.", "source_relation": "partial",
            "source_scope": "First step in the synthetic route", "unknowns": [],
            "evidence": self.evidence("study-1", "step A", "Defines the first step")}
        child_decision["evidence_sources"] = [
            {"id": "study-1", "kind": "synthetic", "citation": "Synthetic fixture report"}]
        child_file = self.root / "child-decision.json"
        child_file.write_text(json.dumps(child_decision), encoding="utf-8")
        curate_whole(self.db_path, "child-a", VERSION, child_file, self.workspace,
                     expected_revision=1, apply=True)
        self.decision["instances"][0]["revision"] = 2
        self.write_decision()
        result = self.run_curate(apply=True)
        self.assertEqual(result["card"]["local"]["data"]["representations"][1]["children"][0]["revision"], 2)


if __name__ == "__main__":
    unittest.main()
