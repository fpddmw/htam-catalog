import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from htam_catalog.tiangong_sync import (ingest_page, matching_connections, open_db, read_export, search,
                                         status_report,
                                         sync_cli)


def process_row(source_id="process-1", name="Steel heat treatment", flow_id="flow-1"):
    return {
        "id": source_id, "version": "01.00.000", "state_code": 100,
        "modified_at": "2026-09-27T00:00:00Z",
        "process": {"processDataSet": {
            "processInformation": {
                "dataSetInformation": {"common:UUID": source_id,
                                       "name": {"baseName": {"#text": name}}},
                "quantitativeReference": {"referenceToReferenceFlow": "2"}},
            "modellingAndValidation": {"LCIMethodAndAllocation":
                                        {"typeOfDataSet": "Unit process, single operation"}},
            "administrativeInformation": {"publicationAndOwnership":
                                          {"common:dataSetVersion": "01.00.000"}},
            "exchanges": {"exchange": [
                {"@dataSetInternalID": "1", "exchangeDirection": "Input",
                 "referenceToFlowDataSet": {"@refObjectId": "flow-input", "@version": "01.00.000"},
                 "meanAmount": "2"},
                {"@dataSetInternalID": "2", "exchangeDirection": "Output",
                 "referenceToFlowDataSet": {"@refObjectId": flow_id, "@version": "01.00.000"},
                 "meanAmount": "1", "resultingAmount": "1.5"}]}}}}


def flow_row(source_id="flow-1", name="Recovered heat", kind="Product flow"):
    return {"id": source_id, "version": "01.00.000", "state_code": 100,
            "flow": {"flowDataSet": {
                "flowInformation": {"dataSetInformation": {
                    "common:UUID": source_id,
                    "name": {"baseName": [{"#text": "Heat", "@xml:lang": "en"},
                                          {"#text": name, "@xml:lang": "zh"}]}}},
                "modellingAndValidation": {"LCIMethod": {"typeOfDataSet": kind}},
                "administrativeInformation": {"publicationAndOwnership":
                                              {"common:dataSetVersion": "01.00.000"}}}}}


class TianGongSyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = open_db(Path(self.temp.name) / "catalog.sqlite")
        self.addCleanup(self.db.close)

    def test_batch_is_idempotent_and_content_change_replaces_projection(self):
        first = process_row()
        self.assertEqual(ingest_page(self.db, "process", [first], "page")['inserted'], 1)
        self.assertEqual(ingest_page(self.db, "process", [first], "page")['unchanged'], 1)
        changed = process_row(name="Steel heat treatment revised", flow_id="flow-2")
        self.assertEqual(ingest_page(self.db, "process", [changed], "page")['updated'], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM processes").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM source_changes").fetchone()[0], 1)
        self.assertEqual([row[0] for row in self.db.execute(
            "SELECT flow_id FROM exchanges WHERE direction='Output'")], ["flow-2"])
        self.assertEqual(self.db.execute(
            "SELECT mean_amount,resulting_amount FROM exchanges WHERE direction='Output'").fetchone()[:],
            ("1", "1.5"))

    def test_bad_row_is_isolated_and_retry_clears_failure(self):
        good = process_row()
        bad = process_row(source_id="process-2")
        bad["process"]["processDataSet"]["processInformation"]["dataSetInformation"]["common:UUID"] = "wrong-id"
        counts = ingest_page(self.db, "process", [bad, good], "page")
        self.assertEqual((counts["inserted"], counts["failed"]), (1, 1))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM ingest_failures").fetchone()[0], 1)
        ingest_page(self.db, "process", [process_row(source_id="process-2")], "page")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM ingest_failures").fetchone()[0], 0)

    def test_flow_name_becomes_searchable_without_reclassifying_exchange(self):
        ingest_page(self.db, "process", [process_row()], "process-page")
        ingest_page(self.db, "flow", [flow_row()], "flow-page")
        self.assertEqual(search(self.db, "Recovered heat")[0]["id"], "process-1")
        self.assertEqual(search(self.db, "flow-1")[0]["id"], "process-1")
        self.assertEqual(self.db.execute("SELECT flow_type FROM flows").fetchone()[0], "Product flow")
        self.assertEqual(status_report(self.db)["quality"]["exchanges_without_exact_flow_record"], 1)

    def test_facets_and_connection_status_use_source_fields(self):
        source = process_row()
        info = source["process"]["processDataSet"]["processInformation"]
        info["time"] = {"common:referenceYear": "2023"}
        info["geography"] = {"locationOfOperationSupplyOrProduction": {"@location": "CN"}}
        info["dataSetInformation"]["classificationInformation"] = {
            "common:classification": {"common:class": [
                {"#text": "Manufacturing", "@classId": "C", "@level": "0"},
                {"#text": "Metals", "@classId": "24", "@level": "1"}]}}
        info["technology"] = {"technologyDescriptionAndIncludedProcesses": {"#text": "Furnace treatment"}}
        ingest_page(self.db, "process", [source], "source")
        consumer = process_row("process-2")
        consumer["process"]["processDataSet"]["exchanges"]["exchange"][0]["referenceToFlowDataSet"]["@refObjectId"] = "flow-1"
        ingest_page(self.db, "process", [consumer], "consumer")
        self.assertEqual(search(self.db, "Metals")[0]["id"], "process-1")
        self.assertEqual(search(self.db, "Furnace")[0]["id"], "process-1")
        self.assertEqual(search(self.db, "24")[0]["id"], "process-1")
        self.assertEqual(matching_connections(self.db, "process-1", "01.00.000")[0]["status"],
                         "flow_type_unresolved")
        ingest_page(self.db, "flow", [flow_row()], "flow")
        self.assertEqual(matching_connections(self.db, "process-1", "01.00.000")[0]["status"],
                         "shared_technosphere_flow")
        ingest_page(self.db, "flow", [flow_row(kind="Elementary flow")], "flow")
        self.assertEqual(matching_connections(self.db, "process-1", "01.00.000")[0]["status"],
                         "environmental_exchange")

    def test_saved_cli_export_shape(self):
        path = Path(self.temp.name) / "export.json"
        path.write_text(json.dumps({"status": "listed_remote_processes", "rows": [process_row()]}))
        self.assertEqual(len(read_export(path, "process")), 1)
        self.assertEqual(ingest_page(self.db, "process", read_export(path, "process"), str(path))["inserted"], 1)

    @patch("htam_catalog.tiangong_sync.subprocess.run")
    def test_paged_sync_resumes_from_committed_offset(self, run):
        def result(command, **_kwargs):
            offset = int(command[command.index("--offset") + 1])
            rows = [process_row("process-1"), process_row("process-2")] if offset == 0 else []
            return type("Result", (), {"returncode": 0, "stdout": json.dumps(
                {"status": "listed_remote_processes", "rows": rows})})()
        run.side_effect = result
        first = sync_cli(self.db, "process", ["mock-cli"], page_size=2, max_pages=1)
        self.assertEqual((first["status"], first["next_offset"]), ("running", 2))
        second = sync_cli(self.db, "process", ["mock-cli"], page_size=2, resume=True)
        self.assertEqual((second["status"], second["next_offset"]), ("complete", 2))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM processes").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
