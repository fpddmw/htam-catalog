import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from htam_catalog.tiangong_units import (SupportExport, UnitResolutionError, main,
                                         resolve_reference_unit)


def flow(source_id="flow-1", version="01.00.000", property_id="property-mass"):
    return {"flowDataSet": {
        "flowInformation": {
            "dataSetInformation": {
                "common:UUID": source_id, "name": {"baseName": {"#text": "Material"}}},
            "quantitativeReference": {"referenceToReferenceFlowProperty": 1}},
        "flowProperties": {"flowProperty": {
            "@dataSetInternalID": "1",
            "referenceToFlowPropertyDataSet": {
                "@refObjectId": property_id, "@version": version}}},
        "administrativeInformation": {"publicationAndOwnership": {
            "common:dataSetVersion": version}}}}


def property_row(source_id, group_id, name):
    version = "01.00.000"
    return {"id": source_id, "version": version, "state_code": 100, "json": {
        "flowPropertyDataSet": {
            "flowPropertiesInformation": {
                "dataSetInformation": {"common:UUID": source_id,
                                       "common:name": {"#text": name}},
                "quantitativeReference": {"referenceToReferenceUnitGroup": {
                    "@refObjectId": group_id, "@version": version}}},
            "administrativeInformation": {"publicationAndOwnership": {
                "common:dataSetVersion": version}}}}}


def group_row(source_id, unit_name):
    version = "01.00.000"
    return {"id": source_id, "version": version, "state_code": 100, "json": {
        "unitGroupDataSet": {
            "unitGroupInformation": {
                "dataSetInformation": {"common:UUID": source_id},
                "quantitativeReference": {"referenceToReferenceUnit": "7"}},
            "units": {"unit": [{"@dataSetInternalID": "1", "name": "other"},
                               {"@dataSetInternalID": 7, "name": unit_name}]},
            "administrativeInformation": {"publicationAndOwnership": {
                "common:dataSetVersion": version}}}}}


def write_export(directory, properties, groups):
    directory.mkdir()
    tables = {}
    for kind, rows in (("flowproperties", properties), ("unitgroups", groups)):
        raw = "".join(json.dumps(row) + "\n" for row in rows).encode()
        (directory / f"{kind}.jsonl").write_bytes(raw)
        tables[kind] = {"rows": len(rows), "sha256": hashlib.sha256(raw).hexdigest()}
    (directory / "export-report.json").write_text(json.dumps({
        "schema_version": 1, "command": "dataset support-cache export",
        "status": "completed", "captured_at_utc": "2026-09-28T00:00:00Z",
        "project_ref": "synthetic", "filters": {"state_codes": [100]},
        "tables": tables}), encoding="utf-8")


class TianGongUnitsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.support_dir = Path(self.temp.name) / "support"
        self.properties = [property_row("property-mass", "group-mass", "Mass"),
                           property_row("property-volume", "group-volume", "Volume")]
        self.groups = [group_row("group-mass", "kg"), group_row("group-volume", "m3")]
        write_export(self.support_dir, self.properties, self.groups)

    def test_reference_units_follow_exact_flow_property_and_group(self):
        support = SupportExport(self.support_dir)
        mass = resolve_reference_unit(flow(), support)
        volume = resolve_reference_unit(flow("flow-2", property_id="property-volume"), support)
        self.assertEqual(mass["reference_unit"], {"internal_id": "7", "name": "kg"})
        self.assertEqual(volume["reference_unit"]["name"], "m3")
        self.assertEqual(volume["flow_property"]["name"], "Volume")
        self.assertEqual(mass["support_source"]["project_ref"], "synthetic")

    def test_requested_support_version_must_exist_exactly(self):
        source = flow()
        source["flowDataSet"]["flowProperties"]["flowProperty"][
            "referenceToFlowPropertyDataSet"]["@version"] = "02.00.000"
        with self.assertRaisesRegex(UnitResolutionError, "found 0 published rows") as raised:
            resolve_reference_unit(source, SupportExport(self.support_dir))
        self.assertEqual(raised.exception.code, "support_not_in_export")

    def test_missing_internal_unit_is_diagnosed(self):
        self.groups[0]["json"]["unitGroupDataSet"]["unitGroupInformation"][
            "quantitativeReference"]["referenceToReferenceUnit"] = "8"
        other = Path(self.temp.name) / "missing-unit"
        write_export(other, self.properties, self.groups)
        with self.assertRaises(UnitResolutionError) as raised:
            resolve_reference_unit(flow(), SupportExport(other))
        self.assertEqual(raised.exception.code, "missing_reference")

    def test_export_integrity_and_embedded_identity_are_checked(self):
        (self.support_dir / "flowproperties.jsonl").write_text("{}\n", encoding="utf-8")
        with self.assertRaises(UnitResolutionError) as raised:
            resolve_reference_unit(flow(), SupportExport(self.support_dir))
        self.assertEqual(raised.exception.code, "export_incomplete")
        self.properties[0]["json"]["flowPropertyDataSet"]["flowPropertiesInformation"][
            "dataSetInformation"]["common:UUID"] = "wrong"
        other = Path(self.temp.name) / "wrong-identity"
        write_export(other, self.properties, self.groups)
        with self.assertRaises(UnitResolutionError) as raised:
            resolve_reference_unit(flow(), SupportExport(other))
        self.assertEqual(raised.exception.code, "identity_mismatch")

    def test_cli_reads_saved_flow_without_remote_access(self):
        flow_file = Path(self.temp.name) / "flow.json"
        flow_file.write_text(json.dumps({
            "status": "resolved_remote_flow", "flow_id": "flow-1",
            "resolved_version": "01.00.000", "flow": flow()}), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--flow-file", str(flow_file), "--support-dir", str(self.support_dir)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["reference_unit"]["name"], "kg")

    def test_cli_failure_returns_structured_diagnostic(self):
        flow_file = Path(self.temp.name) / "flow.json"
        flow_file.write_text(json.dumps(flow(property_id="absent")), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--flow-file", str(flow_file), "--support-dir", str(self.support_dir)])
        self.assertEqual(code, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["diagnostic"]["code"], "support_not_in_export")

    @patch("htam_catalog.tiangong_units.remote_get")
    def test_remote_lookup_passes_exact_version(self, get):
        get.return_value = {"flow_id": "flow-1", "resolved_version": "01.00.000",
                            "flow": flow()}
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["--flow-id", "flow-1", "--version", "01.00.000",
                         "--support-dir", str(self.support_dir), "--cli", "mock-cli"])
        self.assertEqual(code, 0)
        get.assert_called_once_with(["mock-cli"], "flow", "flow-1", "01.00.000")


if __name__ == "__main__":
    unittest.main()
