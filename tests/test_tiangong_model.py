"""Read-only checks against a synthetic native TianGong model fixture."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT / "examples/tiangong/synthetic-lifecyclemodel.json"
sys.path.insert(0, str(PROJECT / "src"))

from htam_catalog.catalog import digest, read_json
from htam_catalog.tiangong_model import inspect_tiangong_model


class TiangongModelInspectionTests(unittest.TestCase):
    def test_extracts_pinned_instances_and_connection_without_mutating_model(self):
        payload = read_json(FIXTURE)
        before = copy.deepcopy(payload)
        result = inspect_tiangong_model(payload)
        self.assertEqual(payload, before)
        self.assertTrue(result["structurally_consistent"])
        self.assertEqual(result["content_hash"], digest(payload))
        self.assertEqual(result["model_id"], "00000000-0000-4000-8000-000000000100")
        self.assertEqual(result["requested_version"], "00.00.001")
        self.assertEqual(result["reference_instance_id"], "2")
        self.assertEqual([i["instance_id"] for i in result["instances"]], ["1", "2"])
        self.assertEqual([i["requested_version"] for i in result["instances"]], ["00.00.001"] * 2)
        self.assertEqual(result["connections"][0]["to_instance"], "2")

    def test_surfaces_unpinned_version_and_broken_connection(self):
        payload = read_json(FIXTURE)
        raw = payload["lifeCycleModelDataSet"]["lifeCycleModelInformation"]["technology"]["processes"]["processInstance"]
        del raw[0]["referenceToProcess"]["@version"]
        target = raw[0]["connections"]["outputExchange"]["downstreamProcess"]
        target["@id"] = "missing"
        target["@flowUUID"] = "different-flow"
        result = inspect_tiangong_model(payload)
        self.assertFalse(result["structurally_consistent"])
        self.assertEqual({d["code"] for d in result["diagnostics"]},
                         {"UNPINNED_PROCESS_REFERENCE", "UNRESOLVED_DOWNSTREAM_INSTANCE", "CONNECTION_FLOW_MISMATCH"})

    def test_cli_inspects_singleton_instance_and_reports_parameter_presence(self):
        payload = read_json(FIXTURE)
        info = payload["lifeCycleModelDataSet"]["lifeCycleModelInformation"]
        raw = info["technology"]["processes"]["processInstance"]
        raw[0].pop("connections")
        raw[0]["parameters"] = {"example": "not evaluated"}
        info["technology"]["processes"]["processInstance"] = raw[0]
        info["quantitativeReference"]["referenceToReferenceProcess"] = "1"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            path.write_text(json.dumps({"json_ordered": payload}), encoding="utf-8")
            command = [sys.executable, str(PROJECT / "scripts/catalog.py"),
                       "inspect-tiangong-model", "--file", str(path)]
            proc = subprocess.run(command, cwd=directory, capture_output=True,
                                  encoding="utf-8", timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)["result"]
        self.assertTrue(result["structurally_consistent"])
        self.assertEqual(len(result["instances"]), 1)
        self.assertTrue(result["instances"][0]["parameters_present"])
        self.assertEqual(result["connections"], [])


if __name__ == "__main__":
    unittest.main()
