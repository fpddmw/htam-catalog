"""Exact selection from official lifecyclemodel search, without remote writes."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from htam_catalog.catalog import digest, read_json
from htam_catalog.tiangong_model_fetch import (main, matching_model_candidates,
                                               save_selected, search_for_resulting_process,
                                               search_model, select_model)
from htam_catalog.tiangong_sync import IngestError


class ExactModelSearchTests(unittest.TestCase):
    def setUp(self):
        self.model = read_json(PROJECT / "examples/tiangong/synthetic-lifecyclemodel.json")
        self.model_id = "00000000-0000-4000-8000-000000000100"
        self.version = "00.00.001"
        self.ref = {"@type": "process data set", "@refObjectId": "aggregate",
                    "@version": "01.00.000"}
        self.info = self.model["lifeCycleModelDataSet"]["lifeCycleModelInformation"]["dataSetInformation"]

    def response(self, *extra):
        return {"data": [{"id": self.model_id, "version": self.version,
                          "json": self.model, "total_count": 1 + len(extra)}, *extra]}

    def test_exact_selection_skips_decoys_and_checks_native_resulting_process(self):
        self.info["referenceToResultingProcess"] = self.ref
        decoy = {"id": "other", "version": self.version, "json": {}}
        payload, summary = select_model(self.response(decoy), self.model_id, self.version,
                                        "aggregate", "01.00.000")
        self.assertIs(payload, self.model)
        self.assertEqual(summary["content_hash"], digest(self.model))
        self.assertEqual(summary["exact_resulting_process_reference"]["id"], "aggregate")
        self.assertEqual(summary["instance_count"], 2)

    def test_rejects_missing_duplicate_truncated_or_inconsistent_identity(self):
        with self.assertRaisesRegex(IngestError, "found 0"):
            select_model(self.response(), self.model_id, "99.00.000")
        with self.assertRaisesRegex(IngestError, "found 0"):
            select_model([], self.model_id, self.version)
        duplicate = {"id": self.model_id, "version": self.version, "json": self.model}
        with self.assertRaisesRegex(IngestError, "found 2"):
            select_model(self.response(duplicate), self.model_id, self.version)
        response = self.response()
        response["data"][0]["total_count"] = 2
        with self.assertRaisesRegex(IngestError, "incomplete"):
            select_model(response, self.model_id, self.version)
        response = self.response()
        response["data"][0]["json"] = {"json_ordered": self.model}
        response["data"][0]["id"] = "other"
        with self.assertRaisesRegex(IngestError, "disagree"):
            select_model(response, "other", self.version)

    def test_resulting_process_can_be_array_and_mismatch_is_refused(self):
        self.info["referenceToResultingProcess"] = [self.ref]
        _, summary = select_model(self.response(), self.model_id, self.version,
                                  "aggregate", "01.00.000")
        self.assertTrue(summary["exact_resulting_process_reference"]["locator"].endswith("[0]"))
        with self.assertRaisesRegex(IngestError, "does not reference"):
            select_model(self.response(), self.model_id, self.version,
                         "different", "01.00.000")
        self.info.pop("referenceToResultingProcess")
        with self.assertRaisesRegex(IngestError, "does not reference"):
            select_model(self.response(), self.model_id, self.version,
                         "aggregate", "01.00.000")

    def test_cli_uses_supported_search_and_offline_result_saves_exact_raw_model(self):
        with patch("htam_catalog.tiangong_model_fetch.remote_json") as run:
            run.return_value = self.response()
            result = search_model(["tiangong-lca"], self.model_id)
            self.assertEqual(result["data"][0]["id"], self.model_id)
            command = run.call_args.args[0]
            self.assertEqual(command[:3], ["tiangong-lca", "search", "lifecyclemodel"])
        with tempfile.TemporaryDirectory() as directory:
            search_file = Path(directory) / "response.json"
            output_file = Path(directory) / "model.json"
            search_file.write_text(json.dumps(self.response()), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--search-file", str(search_file), "--id", self.model_id,
                                       "--version", self.version, "--out", str(output_file)]), 0)
            self.assertEqual(read_json(output_file), self.model)
            self.assertEqual(json.loads(output.getvalue())["file_status"], "saved")
            self.assertEqual(save_selected(output_file, self.model), "unchanged")
            output_file.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(IngestError, "differs"):
                save_selected(output_file, self.model)

    def test_discovery_uses_exact_native_filter_and_reports_candidates_only(self):
        self.info["referenceToResultingProcess"] = self.ref
        requests = []
        def capture(command, label):
            requests.append(read_json(command[command.index("--input") + 1]))
            return self.response()
        with patch("htam_catalog.tiangong_model_fetch.remote_json") as run:
            run.side_effect = capture
            search_for_resulting_process(["tiangong-lca"], "aggregate", "01.00.000",
                                         "Aggregate process")
            command = run.call_args.args[0]
            self.assertEqual(command[:3], ["tiangong-lca", "search", "lifecyclemodel"])
        self.assertEqual(requests[0]["filter"]["lifeCycleModelDataSet"]["lifeCycleModelInformation"]
                         ["dataSetInformation"]["referenceToResultingProcess"], self.ref)
        result = matching_model_candidates(self.response(), "aggregate", "01.00.000",
                                           "Aggregate process")
        self.assertEqual(result["status"], "candidates_found")
        self.assertEqual(result["coverage"], "hybrid_retrieval_only")
        self.assertEqual(result["candidates"][0]["model_id"], self.model_id)
        self.assertEqual(matching_model_candidates([], "aggregate", "01.00.000", "query")["status"],
                         "no_candidates_returned")
        partial = self.response()
        partial["data"][0]["total_count"] = 3
        self.assertEqual(matching_model_candidates(partial, "aggregate", "01.00.000",
                                                   "query")["coverage"], "partial_page")
        with self.assertRaisesRegex(IngestError, "different resulting process"):
            matching_model_candidates(self.response(), "other", "01.00.000", "query")

    def test_discovery_cli_uses_existing_index_name_without_writing_index(self):
        self.info["referenceToResultingProcess"] = self.ref
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "source.sqlite"
            import sqlite3
            with sqlite3.connect(db) as connection:
                connection.execute("CREATE TABLE processes (id TEXT, version TEXT, name TEXT)")
                connection.execute("INSERT INTO processes VALUES (?,?,?)",
                                   ("aggregate", "01.00.000", "Aggregate process"))
            before = db.read_bytes()
            with patch("htam_catalog.tiangong_model_fetch.search_for_resulting_process",
                       return_value=self.response()) as search:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    code = main(["--find-for-process", "--db", str(db),
                                 "--process-id", "aggregate", "--process-version", "01.00.000"])
            self.assertEqual(code, 0)
            self.assertEqual(search.call_args.args[3], "Aggregate process")
            self.assertEqual(db.read_bytes(), before)
            self.assertEqual(json.loads(output.getvalue())["returned_count"], 1)


if __name__ == "__main__":
    unittest.main()
