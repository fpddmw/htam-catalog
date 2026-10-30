"""Card conversion tests use a temporary source index and catalog workspace."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from htam_catalog.catalog import Catalog
from htam_catalog.tiangong_cards import build, module_id, projected_card
from htam_catalog.tiangong_sync import ingest_page, open_db
from test_tiangong_sync import flow_row, process_row


class TianGongCardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "projection.sqlite"
        self.workspace = Path(self.temp.name) / "cards"
        self.db = open_db(self.db_path)
        self.addCleanup(self.db.close)
        ingest_page(self.db, "process", [process_row()], "source-page")
        ingest_page(self.db, "flow", [flow_row()], "flow-page")

    def test_preview_is_read_only_and_keeps_uncertainty(self):
        report = build(self.db_path, self.workspace, limit=1, show_card=True)
        self.assertEqual((report["created"], report["failed"]), (1, 0))
        self.assertFalse(self.workspace.exists())
        self.assertEqual(report["sample_card"]["module_id"], module_id("process-1", "01.00.000"))
        row = self.db.execute("SELECT * FROM processes").fetchone()
        card = projected_card(self.db, row)
        self.assertEqual(card["extracted"]["data"]["functions"], [])
        self.assertEqual(card["extracted"]["data"]["representations"],
                         [{"id": "whole", "kind": "whole"}])
        self.assertTrue(all(port["kind"] == "unspecified" for port in
                            card["extracted"]["data"]["interfaces"]))
        self.assertEqual(card["extracted"]["data"]["parameters"]["source_exchanges"][1]
                         ["resulting_amount"], "1.5")

    def test_apply_is_idempotent_and_refresh_keeps_local_edits(self):
        first = build(self.db_path, self.workspace, limit=1, apply=True)
        self.assertEqual((first["created"], first["failed"]), (1, 0))
        self.assertEqual(build(self.db_path, self.workspace, limit=1, apply=True)["unchanged"], 1)
        mid = module_id("process-1", "01.00.000")
        catalog = Catalog(self.workspace)
        card = copy.deepcopy(catalog.get(mid)["card"])
        card["revision"] = 2
        card["local"]["data"]["name"] = "本地整理名称"
        catalog.save(card, 1)
        updated = process_row(name="Source name updated")
        updated["process"]["processDataSet"]["exchanges"]["exchange"].append({
            "@dataSetInternalID": "3", "exchangeDirection": "Output",
            "referenceToFlowDataSet": {"@refObjectId": "flow-extra", "@version": "01.00.000"},
            "meanAmount": "0.5"})
        ingest_page(self.db, "process", [updated], "new-page")
        preview = build(self.db_path, self.workspace, limit=1)
        self.assertEqual(preview["refreshed"], 1)
        change = preview["items"][0]["change"]
        self.assertEqual(change["locally_overridden_fields"], ["name"])
        self.assertEqual(len(change["changed_interface_ids"]["added"]), 1)
        self.assertTrue({"name", "interfaces", "parameters"} <= set(change["changed_source_fields"]))
        self.assertEqual(catalog.get(mid)["card"]["revision"], 2)
        changed = build(self.db_path, self.workspace, limit=1, apply=True)
        self.assertEqual((changed["refreshed"], changed["failed"]), (1, 0))
        self.assertEqual(changed["items"][0]["change"], change)
        latest = catalog.get(mid)
        self.assertEqual(latest["card"]["revision"], 3)
        self.assertEqual(latest["effective"]["name"], "本地整理名称")
        self.assertEqual(latest["card"]["extracted"]["data"]["name"], "Source name updated")

    def test_unspecified_interfaces_match_only_by_flow_identity(self):
        consumer = process_row(source_id="process-2", name="Consumer")
        exchange = consumer["process"]["processDataSet"]["exchanges"]["exchange"][0]
        exchange["referenceToFlowDataSet"]["@refObjectId"] = "flow-1"
        ingest_page(self.db, "process", [consumer], "consumer-page")
        build(self.db_path, self.workspace, limit=2, apply=True)
        catalog = Catalog(self.workspace)
        source = catalog.get(module_id("process-1", "01.00.000"))["effective"]
        output = next(p["id"] for p in source["interfaces"] if p["direction"] == "output")
        candidates = catalog.candidates(module_id("process-1", "01.00.000"), output)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["to"]["module_id"], module_id("process-2", "01.00.000"))
        self.assertEqual(candidates[0]["condition_status"], "unknown")
        self.assertEqual(candidates[0]["match_reasons"]["shared_flow_refs"], [{
            "provider": "tiangong-lca", "id": "flow-1", "output_version": "01.00.000",
            "input_version": "01.00.000", "version_match": "exact"}])

        # Refresh the same source process: an explicit different version no longer
        # provides a flow-identity connection for an unclassified exchange.
        exchange["referenceToFlowDataSet"]["@version"] = "02.00.000"
        ingest_page(self.db, "process", [consumer], "consumer-new-version")
        self.assertEqual(build(self.db_path, self.workspace, limit=2, apply=True)["refreshed"], 1)
        self.assertEqual(catalog.candidates(module_id("process-1", "01.00.000"), output), [])

        # A missing version remains a possible connection with uncertainty shown.
        del exchange["referenceToFlowDataSet"]["@version"]
        ingest_page(self.db, "process", [consumer], "consumer-unversioned")
        self.assertEqual(build(self.db_path, self.workspace, limit=2, apply=True)["refreshed"], 1)
        candidates = catalog.candidates(module_id("process-1", "01.00.000"), output)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["match_reasons"]["shared_flow_refs"][0]["version_match"],
                         "unresolved")
        self.assertIsNone(candidates[0]["match_reasons"]["shared_flow_refs"][0]["input_version"])
        self.assertIn("连接流的版本至少有一端未明确", candidates[0]["unknowns"])

    def test_elementary_flow_is_not_a_recombination_link(self):
        consumer = process_row(source_id="process-2", name="Consumer")
        exchange = consumer["process"]["processDataSet"]["exchanges"]["exchange"][0]
        exchange["referenceToFlowDataSet"]["@refObjectId"] = "flow-1"
        ingest_page(self.db, "process", [consumer], "consumer-page")
        ingest_page(self.db, "flow", [flow_row(kind="Elementary flow")], "flow-page")
        build(self.db_path, self.workspace, limit=2, apply=True)
        catalog = Catalog(self.workspace)
        source = catalog.get(module_id("process-1", "01.00.000"))["effective"]
        output = next(p["id"] for p in source["interfaces"] if p["direction"] == "output")
        self.assertEqual(catalog.candidates(module_id("process-1", "01.00.000"), output), [])


if __name__ == "__main__":
    unittest.main()
