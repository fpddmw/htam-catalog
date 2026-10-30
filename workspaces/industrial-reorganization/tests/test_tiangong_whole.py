"""Curation of a single exact TianGong source as a whole module."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from htam_catalog.catalog import Catalog, CatalogError
from htam_catalog.tiangong_sync import ingest_page, open_db
from htam_catalog.tiangong_whole import curate, decision_template, source_card
from test_tiangong_draft import VERSION, source_process


class WholeCurationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "sources.sqlite"
        self.decision_file = self.root / "decision.json"
        self.catalog = Catalog(self.root / "catalog")
        self.catalog.init()
        with open_db(self.db_path) as db:
            ingest_page(db, "process", [source_process("step", [
                ("Input", "feed", VERSION), ("Output", "product", VERSION)])], "step")
        self.source = source_card(self.db_path, "step", VERSION)
        self.decision = decision_template(self.source)
        self.decision["module"] = {
            "id": "curated-step", "name": "Synthetic treatment step",
            "summary": "Turns feed into product under the stated synthetic conditions.",
            "source_relation": "partial", "source_scope": "Only the documented boundary",
            "unknowns": ["Temperature dependence is unknown"],
            "functions": ["treatment"],
            "evidence": [{"source_id": "study", "locator": "method section",
                          "note": "Describes step function and boundary"}],
            "interface_overrides": [{
                "port_id": self.source["extracted"]["data"]["interfaces"][0]["id"],
                "kind": "material", "terms": ["feed"],
                "evidence": [{"source_id": "study", "locator": "flow table",
                              "note": "Identifies feed as material"}]}],
        }
        self.decision["evidence_sources"] = [{"id": "study", "kind": "synthetic",
                                                 "citation": "Synthetic treatment report"}]
        self.write()

    def write(self):
        self.decision_file.write_text(json.dumps(self.decision), encoding="utf-8")

    def run_curate(self, revision=0, apply=False):
        return curate(self.db_path, "step", VERSION, self.decision_file,
                      self.catalog.root, revision, apply)

    def test_preview_apply_and_update_preserve_source_projection(self):
        preview = self.run_curate()
        self.assertEqual(preview["mode"], "preview")
        self.assertEqual(self.catalog.revisions("curated-step"), [])
        self.run_curate(apply=True)
        loaded = self.catalog.get("curated-step")
        self.assertEqual(loaded["effective"]["interfaces"][0]["kind"], "material")
        self.assertEqual(loaded["effective"]["interfaces"][1]["kind"], "unspecified")
        self.assertEqual(loaded["card"]["extracted"], self.source["extracted"])
        self.decision["module"]["summary"] = "Updated technical description."
        self.write()
        self.run_curate(revision=1, apply=True)
        self.assertEqual(self.catalog.get("curated-step", 1)["effective"]["summary"],
                         "Turns feed into product under the stated synthetic conditions.")

    def test_stale_source_and_missing_evidence_are_rejected(self):
        self.decision["source_process"]["content_hash"] = "stale"
        self.write()
        with self.assertRaisesRegex(CatalogError, "content hash changed"):
            self.run_curate()
        self.decision = decision_template(self.source)
        self.decision["module"] = {"id": "bad", "name": "Bad", "summary": "Bad",
                                   "source_relation": "partial", "source_scope": "Bad",
                                   "unknowns": [], "evidence": []}
        self.write()
        with self.assertRaisesRegex(CatalogError, "evidence is required"):
            self.run_curate()

    def test_source_change_is_reviewable_in_next_revision(self):
        self.run_curate(apply=True)
        with open_db(self.db_path) as db:
            changed = source_process("step", [
                ("Input", "feed", VERSION), ("Output", "product", VERSION)])
            changed["process"]["processDataSet"]["exchanges"]["exchange"][0]["meanAmount"] = "7"
            ingest_page(db, "process", [changed], "step-changed")
        fresh = source_card(self.db_path, "step", VERSION)
        self.decision["source_process"] = decision_template(fresh)["source_process"]
        self.write()
        preview = self.run_curate(revision=1)
        self.assertTrue(preview["source_change"]["review_required"])
        self.assertEqual(preview["card"]["extracted"], fresh["extracted"])
        self.run_curate(revision=1, apply=True)
        loaded = self.catalog.get("curated-step")
        self.assertEqual(loaded["effective"]["parameters"]["source_exchanges"][0]["mean_amount"], "7")


if __name__ == "__main__":
    unittest.main()
