import json
import contextlib
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from htam_catalog.tiangong_sync import (add_selected, ingest_page, main, matching_connections,
                                         open_db, query_processes, read_export, remote_get,
                                         remote_search, refs_from_search, selected_refs,
                                         retry_failed_rows, search, status_report,
                                         source_changes, source_detail, sync_cli, sync_flow_references,
                                         sync_selected_flow_references)


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

    def test_missing_flow_reference_keeps_process_and_records_issue(self):
        row = process_row()
        del row["process"]["processDataSet"]["exchanges"]["exchange"][0]["referenceToFlowDataSet"]["@refObjectId"]
        counts = ingest_page(self.db, "process", [row], "page")
        self.assertEqual((counts["inserted"], counts["failed"]), (1, 0))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0], 1)
        self.assertEqual(status_report(self.db)["quality"]["skipped_exchange_items"], 1)

    @patch("htam_catalog.tiangong_sync.cli_ids")
    def test_failed_remote_row_retries_by_identity(self, fetch):
        bad = process_row()
        bad["process"]["processDataSet"]["processInformation"]["dataSetInformation"]["common:UUID"] = "wrong"
        ingest_page(self.db, "process", [bad], "tiangong:process:100:offset=0")
        fetch.return_value = [process_row()]
        result = retry_failed_rows(self.db, "process", ["mock-cli"])
        self.assertEqual((result["requested_ids"], result["remaining_failures"]), (1, 0))
        self.assertEqual(fetch.call_args.args[2], ["process-1"])

    def test_flow_name_becomes_searchable_without_reclassifying_exchange(self):
        ingest_page(self.db, "process", [process_row()], "process-page")
        ingest_page(self.db, "flow", [flow_row()], "flow-page")
        self.assertEqual(search(self.db, "Recovered heat")[0]["id"], "process-1")
        self.assertEqual(search(self.db, "flow-1")[0]["id"], "process-1")
        self.assertEqual(self.db.execute("SELECT flow_type FROM flows").fetchone()[0], "Product flow")
        self.assertEqual(status_report(self.db)["quality"]["exchanges_without_exact_flow_record"], 1)
        self.assertEqual(status_report(self.db)["quality"]["flow_id_absent"], 1)

    def test_facets_and_connection_status_use_source_fields(self):
        source = process_row()
        info = source["process"]["processDataSet"]["processInformation"]
        info["time"] = {"common:referenceYear": 2023}
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
        self.assertEqual(search(self.db, "Metals")[0]["reference_year"], "2023")
        self.assertEqual(query_processes(self.db, industry_code="24", location="CN",
                                         reference_year="2023", output_flow="flow-1")[0]["id"],
                         "process-1")
        self.assertEqual(query_processes(self.db, industry_code="24", input_flow="flow-1"), [])
        self.assertEqual(matching_connections(self.db, "process-1", "01.00.000")[0]["status"],
                         "flow_type_unresolved")
        ingest_page(self.db, "flow", [flow_row()], "flow")
        self.assertEqual(matching_connections(self.db, "process-1", "01.00.000")[0]["status"],
                         "shared_technosphere_flow")
        consumer_exchange = consumer["process"]["processDataSet"]["exchanges"]["exchange"][0]
        consumer_exchange["referenceToFlowDataSet"]["@version"] = "02.00.000"
        ingest_page(self.db, "process", [consumer], "consumer-new-version")
        self.assertEqual(matching_connections(self.db, "process-1", "01.00.000")[0]["status"],
                         "version_conflict")
        del consumer_exchange["referenceToFlowDataSet"]["@version"]
        ingest_page(self.db, "process", [consumer], "consumer-unversioned")
        self.assertEqual(matching_connections(self.db, "process-1", "01.00.000")[0]["status"],
                         "version_unresolved")
        ingest_page(self.db, "flow", [flow_row(kind="Elementary flow")], "flow")
        self.assertEqual(matching_connections(self.db, "process-1", "01.00.000")[0]["status"],
                         "environmental_exchange")

    def test_search_and_connections_can_page_through_stable_results(self):
        ingest_page(self.db, "process", [process_row()], "source")
        for source_id in ("process-2", "process-3"):
            consumer = process_row(source_id=source_id, name="Consumer")
            input_ref = consumer["process"]["processDataSet"]["exchanges"]["exchange"][0][
                "referenceToFlowDataSet"]
            input_ref["@refObjectId"] = "flow-1"
            if source_id == "process-3":
                input_ref["@version"] = "02.00.000"
                consumer["process"]["processDataSet"]["exchanges"]["exchange"][1][
                    "referenceToFlowDataSet"]["@version"] = "02.00.000"
            ingest_page(self.db, "process", [consumer], source_id)
        ingest_page(self.db, "flow", [flow_row()], "flow")
        self.assertEqual([row["id"] for row in search(self.db, "Consumer", limit=1)],
                         ["process-2"])
        self.assertEqual([row["id"] for row in search(self.db, "Consumer", limit=1, offset=1)],
                         ["process-3"])
        self.assertEqual([row["id"] for row in query_processes(self.db, input_flow="flow-1")],
                         ["process-2", "process-3"])
        self.assertEqual([row["id"] for row in query_processes(
            self.db, input_flow="flow-1", input_flow_version="01.00.000")], ["process-2"])
        self.assertEqual([row["id"] for row in query_processes(
            self.db, input_flow="flow-1", input_flow_version="02.00.000")], ["process-3"])
        self.assertEqual([row["id"] for row in query_processes(
            self.db, output_flow="flow-1", output_flow_version="02.00.000")], ["process-3"])
        with self.assertRaisesRegex(ValueError, "flow version requires a flow ID"):
            query_processes(self.db, input_flow_version="01.00.000")
        first = matching_connections(self.db, "process-1", "01.00.000", limit=1)
        second = matching_connections(self.db, "process-1", "01.00.000", limit=1, offset=1)
        self.assertEqual([row["receiving_process_id"] for row in first + second],
                         ["process-2", "process-3"])
        self.assertEqual(matching_connections(self.db, "process-1", "01.00.000",
                                              limit=1, offset=2), [])
        with self.assertRaisesRegex(ValueError, "indexed process .* not found"):
            matching_connections(self.db, "process-1", "02.00.000")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--db", str(Path(self.temp.name) / "catalog.sqlite"),
                                   "matches", "--process-id", "process-1", "--version", "01.00.000",
                                   "--limit", "1", "--offset", "1"]), 0)
        self.assertEqual(json.loads(output.getvalue())[0]["receiving_process_id"], "process-3")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--db", str(Path(self.temp.name) / "catalog.sqlite"),
                                   "query", "--input-flow", "flow-1",
                                   "--input-flow-version", "02.00.000"]), 0)
        self.assertEqual([row["id"] for row in json.loads(output.getvalue())], ["process-3"])

    def test_read_only_commands_do_not_create_a_missing_index(self):
        missing = Path(self.temp.name) / "uncreated" / "source.sqlite"
        commands = [["search", "heat"], ["query"],
                    ["matches", "--process-id", "process-1", "--version", "01.00.000"],
                    ["status"], ["issues"], ["failures"]]
        for command in commands:
            with self.subTest(command=command):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["--db", str(missing), *command]), 1)
                self.assertFalse(missing.parent.exists())

    def test_saved_cli_export_shape(self):
        path = Path(self.temp.name) / "export.json"
        path.write_text(json.dumps({"status": "listed_remote_processes", "rows": [process_row()]}))
        self.assertEqual(len(read_export(path, "process")), 1)
        self.assertEqual(ingest_page(self.db, "process", read_export(path, "process"), str(path))["inserted"], 1)
        path.write_text(json.dumps({"status": "resolved_remote_process", "process_id": "process-2",
                                    "resolved_version": "01.00.000", "process": process_row("process-2")["process"]}))
        self.assertEqual(ingest_page(self.db, "process", read_export(path, "process"), str(path))["inserted"], 1)

    def test_existing_projection_schema_is_migrated_before_reimport(self):
        old_path = Path(self.temp.name) / "old.sqlite"
        with sqlite3.connect(old_path) as old:
            old.execute("""CREATE TABLE processes (id TEXT,version TEXT,content_hash TEXT,
              name TEXT,process_type TEXT,reference_exchange_id TEXT,state_code INTEGER,
              modified_at TEXT,source_locator TEXT,synced_at TEXT,PRIMARY KEY(id,version))""")
            old.execute("""CREATE TABLE exchanges (process_id TEXT,process_version TEXT,
              internal_id TEXT,direction TEXT,flow_id TEXT,flow_version TEXT,amount TEXT,
              is_reference INTEGER,PRIMARY KEY(process_id,process_version,internal_id))""")
        with open_db(old_path) as migrated:
            self.assertEqual(ingest_page(migrated, "process", [process_row()], "migration")["inserted"], 1)
            columns = {row[1] for row in migrated.execute("PRAGMA table_info(exchanges)")}
            self.assertTrue({"mean_amount", "resulting_amount"} <= columns)
            self.assertEqual(migrated.execute("SELECT projection_version FROM processes").fetchone()[0], 3)

    @patch("htam_catalog.tiangong_sync.cli_flow_ids")
    def test_only_referenced_flow_ids_are_queued_and_fetched(self, fetch):
        ingest_page(self.db, "process", [process_row()], "process")
        ingest_page(self.db, "flow", [flow_row()], "flow")
        fetch.return_value = [flow_row("flow-input", "Electricity")]
        result = sync_flow_references(self.db, ["mock-cli"], batch_size=10)
        self.assertEqual(result["requested_ids"], 1)
        self.assertEqual(fetch.call_args.args[1], ["flow-input"])
        self.assertEqual(status_report(self.db)["flow_lookup_queue"], {"found": 1})
        self.assertEqual(status_report(self.db)["quality"]["exchanges_without_exact_flow_record"], 0)

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

    @patch("htam_catalog.tiangong_sync.subprocess.run")
    def test_remote_search_wraps_official_request_without_writing_index(self, run):
        def respond(command, **_kwargs):
            self.assertEqual(command[:3], ["mock-cli", "search", "process"])
            request = json.loads(Path(command[command.index("--input") + 1]).read_text())
            self.assertEqual(request, {"query": "waste heat", "filter": {"region": "CN"}})
            hit = {"id": "p1", "version": "01.00.000", "json": process_row("p1")["process"]}
            return type("Result", (), {"returncode": 0, "stdout": json.dumps({"data": [hit]})})()
        run.side_effect = respond
        result = remote_search(["mock-cli"], "process", "waste heat", {"region": "CN"})
        self.assertEqual((result["count"], result["data"][0]["id"]), (1, "p1"))
        saved_path = Path(self.temp.name) / "search.json"
        with patch("htam_catalog.tiangong_sync.remote_search", return_value=result):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["remote-search", "--kind", "process", "--query", "waste heat",
                                       "--out", str(saved_path)]), 0)
        self.assertEqual(json.loads(saved_path.read_text())["data"], result["data"])
        self.assertEqual(json.loads(output.getvalue())["count"], 1)
        self.assertEqual(json.loads(output.getvalue())["hits"][0], {
            "position": 1, "id": "p1", "version": "01.00.000", "name": "Steel heat treatment"})

    @patch("htam_catalog.tiangong_sync.subprocess.run")
    def test_remote_get_requires_exact_returned_version(self, run):
        source = process_row()
        payload = {"status": "resolved_remote_process", "process_id": "process-1",
                   "resolved_version": "01.00.000", "process": source["process"]}
        run.return_value = type("Result", (), {"returncode": 0, "stdout": json.dumps(payload)})()
        with self.assertRaisesRegex(ValueError, "resolved as"):
            remote_get(["mock-cli"], "process", "process-1", "00.00.001")
        self.assertEqual(remote_get(["mock-cli"], "process", "process-1")["resolved_version"],
                         "01.00.000")

    @patch("htam_catalog.tiangong_sync.remote_get")
    def test_selected_ids_are_idempotent_and_failures_stay_isolated(self, get):
        def respond(_cli, _kind, source_id, version):
            if source_id == "missing":
                raise ValueError("not found")
            row = process_row(source_id)
            return {"process_id": source_id, "resolved_version": row["version"],
                    "state_code": 100, "process": row["process"]}
        get.side_effect = respond
        refs = selected_refs(["process-1", "process-1", "missing"])
        self.assertEqual(len(refs), 2)
        first = add_selected(self.db, "process", refs, ["mock-cli"])
        self.assertEqual((first["inserted"], first["failed"]), (1, 1))
        second = add_selected(self.db, "process", refs, ["mock-cli"])
        self.assertEqual((second["unchanged"], second["failed"]), (1, 1))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM processes").fetchone()[0], 1)

    def test_reference_file_preserves_requested_versions(self):
        path = Path(self.temp.name) / "refs.json"
        path.write_text(json.dumps(["process-1", {"id": "process-2", "version": "01.00.000"}]))
        self.assertEqual(selected_refs(refs_file=path), [
            {"id": "process-1", "version": None},
            {"id": "process-2", "version": "01.00.000"}])

    @patch("htam_catalog.tiangong_sync.remote_get")
    def test_picking_search_results_fetches_only_exact_selected_refs(self, get):
        path = Path(self.temp.name) / "search.json"
        path.write_text(json.dumps({"kind": "process", "data": [
            {"id": "process-1", "version": "01.00.000"},
            {"id": "process-2", "version": "01.00.000"}]}))
        refs = refs_from_search(path, [2, 2], "process")
        self.assertEqual(refs, [{"id": "process-2", "version": "01.00.000"}])
        with self.assertRaisesRegex(ValueError, "out of range"):
            refs_from_search(path, [3], "process")
        row = process_row("process-2")
        get.return_value = {"process_id": row["id"], "resolved_version": row["version"],
                            "process": row["process"]}
        selected_db = Path(self.temp.name) / "picked.sqlite"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--db", str(selected_db), "add-ids", "--kind", "process",
                         "--search-file", str(path), "--pick", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["inserted"], 1)
        self.assertEqual(get.call_args.args[2:], ("process-2", "01.00.000"))
        with sqlite3.connect(selected_db) as db:
            self.assertEqual(db.execute("SELECT id FROM processes").fetchone()[0], "process-2")

    @patch("htam_catalog.tiangong_sync.remote_get")
    def test_add_ids_command_works_from_a_fresh_workspace(self, get):
        row = flow_row()
        get.return_value = {"flow_id": row["id"], "resolved_version": row["version"],
                            "flow": row["flow"]}
        path = Path(self.temp.name) / "selected.sqlite"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--db", str(path), "add-ids", "--kind", "flow", "--id", "flow-1"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["inserted"], 1)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("SELECT id FROM flows").fetchone()[0], "flow-1")

    @patch("htam_catalog.tiangong_sync.remote_get")
    def test_scoped_flow_lookup_uses_only_selected_process_references(self, get):
        ingest_page(self.db, "process", [process_row(), process_row("process-2", flow_id="other-flow")], "source")
        def respond(_cli, kind, source_id, version):
            self.assertEqual(kind, "flow")
            row = flow_row(source_id)
            self.assertEqual(version, row["version"])
            return {"flow_id": source_id, "resolved_version": row["version"], "flow": row["flow"]}
        get.side_effect = respond
        result = sync_selected_flow_references(self.db, ["mock-cli"], "process-1", "01.00.000")
        self.assertEqual(result["requested"], 2)
        self.assertEqual({item["id"] for item in result["items"]}, {"flow-input", "flow-1"})
        self.assertEqual(sync_selected_flow_references(self.db, ["mock-cli"], "process-1")["requested"], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM flows").fetchone()[0], 2)

    def test_source_detail_shows_exact_indexed_facts_without_creating_a_card(self):
        ingest_page(self.db, "process", [process_row()], "selected-source")
        ingest_page(self.db, "flow", [flow_row()], "selected-flow")
        detail = source_detail(self.db, "process", "process-1", "01.00.000")
        self.assertEqual(detail["process"]["name"], "Steel heat treatment")
        self.assertEqual(detail["exchanges"][1]["flow_name"], "Recovered heat")
        self.assertEqual(detail["exchanges"][0]["flow_name"], None)
        self.assertEqual(detail["issues"], [])
        self.assertEqual(source_detail(self.db, "flow", "flow-1", "01.00.000")["flow"]["flow_type"],
                         "Product flow")
        self.db.commit()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--db", str(Path(self.temp.name) / "catalog.sqlite"),
                                   "source-detail", "--kind", "process", "--id", "process-1",
                                   "--version", "01.00.000"]), 0)
        self.assertEqual(json.loads(output.getvalue())["exchanges"][1]["flow_name"], "Recovered heat")
        missing = Path(self.temp.name) / "missing.sqlite"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(missing), "source-detail", "--kind", "process",
                                   "--id", "process-1", "--version", "01.00.000"]), 1)
        self.assertFalse(missing.exists())

    def test_changes_query_reports_observations_without_claiming_module_impact(self):
        ingest_page(self.db, "process", [process_row()], "initial-process")
        ingest_page(self.db, "process", [process_row(name="Changed name")], "updated-process")
        ingest_page(self.db, "flow", [flow_row()], "initial-flow")
        ingest_page(self.db, "flow", [flow_row(name="Changed flow")], "updated-flow")
        process = source_changes(self.db, kind="process", source_id="process-1", version="01.00.000")
        self.assertEqual(len(process), 1)
        self.assertNotEqual(process[0]["previous_hash"], process[0]["current_hash"])
        self.assertEqual(process[0]["source_locator"], "updated-process#id=process-1@01.00.000")
        self.assertEqual(len(source_changes(self.db, kind="flow")), 1)
        self.assertEqual(source_changes(self.db, source_id="missing"), [])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--db", str(Path(self.temp.name) / "catalog.sqlite"),
                                   "changes", "--kind", "process", "--id", "process-1"]), 0)
        self.assertEqual(json.loads(output.getvalue()), process)


if __name__ == "__main__":
    unittest.main()
