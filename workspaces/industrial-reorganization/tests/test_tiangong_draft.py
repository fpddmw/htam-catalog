"""Preview-only bridge from an indexed source to an explicit native model."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from htam_catalog.tiangong_draft import main, preview_from_files, preview_model_draft
from htam_catalog.tiangong_sync import IngestError, ingest_page, open_db, read_db


VERSION = "01.00.000"


def source_process(source_id, exchanges):
    return {"id": source_id, "version": VERSION, "state_code": 100,
            "process": {"processDataSet": {
                "processInformation": {
                    "dataSetInformation": {
                        "common:UUID": source_id,
                        "name": {"baseName": {"#text": source_id}}},
                    "quantitativeReference": {"referenceToReferenceFlow": "1"}},
                "administrativeInformation": {"publicationAndOwnership": {
                    "common:dataSetVersion": VERSION}},
                "exchanges": {"exchange": [
                    {"@dataSetInternalID": str(position), "exchangeDirection": direction,
                     "referenceToFlowDataSet": {"@refObjectId": flow_id, "@version": flow_version},
                     "meanAmount": "1"}
                    for position, (direction, flow_id, flow_version) in enumerate(exchanges, 1)]}}}}


def native_model():
    return {"lifeCycleModelDataSet": {
        "lifeCycleModelInformation": {
            "dataSetInformation": {"common:UUID": "model-1"},
            "quantitativeReference": {"referenceToReferenceProcess": "2"},
            "technology": {"processes": {"processInstance": [
                {"@dataSetInternalID": "1", "@multiplicationFactor": "1",
                 "referenceToProcess": {"@refObjectId": "child-a", "@version": VERSION},
                 "connections": {"outputExchange": {
                     "@flowUUID": "middle", "@version": VERSION,
                     "downstreamProcess": [
                         {"@id": "2", "@flowUUID": "middle", "@version": VERSION},
                         {"@id": "3", "@flowUUID": "middle", "@version": VERSION}]}}},
                {"@dataSetInternalID": "2", "@multiplicationFactor": "2",
                 "referenceToProcess": {"@refObjectId": "child-b", "@version": VERSION}},
                {"@dataSetInternalID": "3", "@multiplicationFactor": "3",
                 "referenceToProcess": {"@refObjectId": "child-b", "@version": VERSION}},
            ]}}},
        "administrativeInformation": {"publicationAndOwnership": {
            "common:dataSetVersion": VERSION}}}}


class TianGongDraftTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "sources.sqlite"
        with open_db(self.db_path) as db:
            ingest_page(db, "process", [source_process("aggregate", [
                ("Input", "feed", VERSION), ("Output", "product", VERSION)])], "aggregate")
            ingest_page(db, "process", [source_process("child-a", [
                ("Input", "feed", VERSION), ("Output", "middle", VERSION)])], "child-a")
            ingest_page(db, "process", [source_process("child-b", [
                ("Input", "middle", VERSION), ("Output", "product", VERSION)])], "child-b")
        self.model = native_model()
        self.model_file = Path(self.temp.name) / "model.json"
        self.model_file.write_text(json.dumps(self.model), encoding="utf-8")

    def test_duplicate_process_definition_remains_two_instances(self):
        before_model = copy.deepcopy(self.model)
        before_db = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        with read_db(self.db_path) as db:
            draft = preview_model_draft(db, "aggregate", VERSION, self.model)
        self.assertEqual(self.model, before_model)
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).hexdigest(), before_db)
        self.assertEqual(draft["mode"], "preview")
        self.assertEqual(draft["source_process"]["association_to_model"],
                         "explicitly_supplied_unverified")
        self.assertEqual(draft["whole_source_card_preview"]["extracted"]["data"][
            "representations"], [{"id": "whole", "kind": "whole"}])
        instances = draft["candidate_topology"]["instances"]
        self.assertEqual([item["instance_id"] for item in instances], ["1", "2", "3"])
        self.assertEqual([item["process_id"] for item in instances], ["child-a", "child-b", "child-b"])
        self.assertEqual([item["multiplication_factor"] for item in instances], ["1", "2", "3"])
        self.assertTrue(all(item["module_mapping"] is None for item in instances))
        edges = draft["candidate_topology"]["connections"]
        self.assertEqual([item["to_instance"] for item in edges], ["2", "3"])
        self.assertEqual([item["candidate_status"] for item in edges],
                         ["single_candidate_pair", "single_candidate_pair"])
        self.assertEqual(edges[0]["output_exchange_candidates"][0]["exchange_internal_id"], "2")
        self.assertEqual(edges[1]["input_exchange_candidates"][0]["exchange_internal_id"], "1")
        self.assertEqual(draft["boundary_mapping"]["status"], "unresolved")

    def test_native_resulting_process_reference_resolves_exact_association_only(self):
        model = native_model()
        info = model["lifeCycleModelDataSet"]["lifeCycleModelInformation"]["dataSetInformation"]
        info["referenceToResultingProcess"] = {"@type": "process data set",
                                               "@refObjectId": "aggregate", "@version": VERSION}
        with read_db(self.db_path) as db:
            draft = preview_model_draft(db, "aggregate", VERSION, model)
        self.assertEqual(draft["source_process"]["association_to_model"],
                         "native_resulting_process_reference")
        self.assertNotIn("source_process_model_association", draft["unresolved"])
        self.assertIn("child_module_identity", draft["unresolved"])
        info["referenceToResultingProcess"]["@version"] = "02.00.000"
        with read_db(self.db_path) as db:
            conflict = preview_model_draft(db, "aggregate", VERSION, model)
        self.assertEqual(conflict["source_process"]["association_to_model"],
                         "native_resulting_process_conflict")

    def test_missing_child_source_keeps_model_instance_unresolved(self):
        model = native_model()
        instances = model["lifeCycleModelDataSet"]["lifeCycleModelInformation"][
            "technology"]["processes"]["processInstance"]
        instances[1]["referenceToProcess"]["@version"] = "02.00.000"
        with read_db(self.db_path) as db:
            draft = preview_model_draft(db, "aggregate", VERSION, model)
        self.assertFalse(draft["candidate_topology"]["instances"][1]["indexed"])
        self.assertEqual(draft["candidate_topology"]["connections"][0]["candidate_status"],
                         "process_not_indexed")

    def test_ambiguous_exchange_is_not_bound_automatically(self):
        with open_db(self.db_path) as db:
            ingest_page(db, "process", [source_process("child-a", [
                ("Input", "feed", VERSION), ("Output", "middle", VERSION),
                ("Output", "middle", VERSION)])], "child-a-changed")
        with read_db(self.db_path) as db:
            draft = preview_model_draft(db, "aggregate", VERSION, self.model)
        edge = draft["candidate_topology"]["connections"][0]
        self.assertEqual(edge["candidate_status"], "ambiguous_exchange")
        self.assertEqual(len(edge["output_exchange_candidates"]), 2)

    def test_exact_source_is_required_and_cli_does_not_create_workspace(self):
        with read_db(self.db_path) as db:
            with self.assertRaises(IngestError):
                preview_model_draft(db, "aggregate", "02.00.000", self.model)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--db", str(self.db_path), "--process-id", "aggregate",
                         "--version", VERSION, "--model-file", str(self.model_file)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["schema"], "htam.tiangong-curation-draft.v1")
        self.assertEqual(sorted(item.name for item in Path(self.temp.name).iterdir()),
                         ["model.json", "sources.sqlite"])

    def test_model_flow_version_conflict_is_preserved(self):
        model = native_model()
        edge = model["lifeCycleModelDataSet"]["lifeCycleModelInformation"][
            "technology"]["processes"]["processInstance"][0]["connections"]["outputExchange"]
        edge["downstreamProcess"][0]["@version"] = "02.00.000"
        draft = preview_from_files(self.db_path, "aggregate", VERSION, self.model_file)
        self.assertEqual(draft["candidate_topology"]["connections"][0]["candidate_status"],
                         "single_candidate_pair")
        with read_db(self.db_path) as db:
            changed = preview_model_draft(db, "aggregate", VERSION, model)
        self.assertEqual(changed["candidate_topology"]["connections"][0]["candidate_status"],
                         "model_flow_version_conflict")
        self.assertFalse(changed["model"]["structurally_consistent"])


if __name__ == "__main__":
    unittest.main()
