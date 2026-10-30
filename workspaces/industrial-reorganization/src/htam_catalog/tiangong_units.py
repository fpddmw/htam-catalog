"""Resolve a TianGong flow's reference unit from a completed support export."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from .tiangong_sync import IngestError, dataset, identity, nested, remote_get, text_value


class UnitResolutionError(ValueError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(detail)


def _object(value, path):
    if not isinstance(value, dict):
        raise UnitResolutionError("invalid_record", f"{path}: expected object")
    return value


def _required_text(value, path):
    if not isinstance(value, str) or not value.strip():
        raise UnitResolutionError("missing_reference", f"{path}: missing text")
    return value.strip()


def _internal_id(value, path):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise UnitResolutionError("missing_reference", f"{path}: missing internal ID")
    return str(value).strip()


def _single(items, internal_id, path):
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise UnitResolutionError("missing_reference", f"{path}: missing list")
    matches = [item for item in items if isinstance(item, dict)
               and str(item.get("@dataSetInternalID")) == internal_id]
    if len(matches) != 1:
        code = "ambiguous_reference" if matches else "missing_reference"
        raise UnitResolutionError(code, f"{path}: expected one internal ID {internal_id}, found {len(matches)}")
    return matches[0]


def _reference(value, path):
    ref = _object(value, path)
    return (_required_text(ref.get("@refObjectId"), f"{path}.@refObjectId"),
            _required_text(ref.get("@version"), f"{path}.@version"))


def _dataset_identity(payload, kind):
    root = "flowPropertyDataSet" if kind == "flowproperties" else "unitGroupDataSet"
    section = "flowPropertiesInformation" if kind == "flowproperties" else "unitGroupInformation"
    data = _object(_object(payload, root).get(root), root)
    found_id = _required_text(nested(data, section, "dataSetInformation", "common:UUID"),
                              f"{root}.{section}.dataSetInformation.common:UUID")
    version = _required_text(nested(data, "administrativeInformation",
                                    "publicationAndOwnership", "common:dataSetVersion"),
                             f"{root}.administrativeInformation.publicationAndOwnership.common:dataSetVersion")
    return data, found_id, version


class SupportExport:
    """Exact public-state lookup from the CLI's completed JSONL export."""

    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        report_path = self.directory / "export-report.json"
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UnitResolutionError("export_incomplete", f"cannot read {report_path}") from exc
        if (not isinstance(report, dict) or report.get("status") != "completed"
                or report.get("command") != "dataset support-cache export"
                or report.get("schema_version") != 1):
            raise UnitResolutionError("export_incomplete", "unsupported or incomplete support export report")
        states = nested(report, "filters", "state_codes")
        if not isinstance(states, list) or 100 not in states:
            raise UnitResolutionError("export_scope", "support export does not include published state 100")
        self.report = report

    def get(self, kind, source_id, version):
        if kind not in ("flowproperties", "unitgroups"):
            raise ValueError(f"unsupported support kind: {kind}")
        path = self.directory / f"{kind}.jsonl"
        table = _object(nested(self.report, "tables", kind), f"export-report.tables.{kind}")
        expected_hash = _required_text(table.get("sha256"), f"export-report.tables.{kind}.sha256")
        expected_count = table.get("rows")
        if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count < 0:
            raise UnitResolutionError("export_incomplete", f"export-report.tables.{kind}.rows: invalid count")
        digest = hashlib.sha256()
        count = 0
        matches = []
        try:
            with path.open("rb") as rows:
                for line in rows:
                    digest.update(line)
                    count += 1
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise UnitResolutionError("export_incomplete", f"{path}:{count}: invalid row")
                    if row.get("id") == source_id and row.get("version") == version:
                        matches.append(row)
        except (OSError, json.JSONDecodeError) as exc:
            raise UnitResolutionError("export_incomplete", f"cannot read valid JSONL from {path}") from exc
        if count != expected_count or digest.hexdigest() != expected_hash:
            raise UnitResolutionError("export_incomplete", f"{path}: count or checksum disagrees with report")
        published = [row for row in matches if row.get("state_code") == 100]
        if len(published) != 1:
            code = "ambiguous_reference" if len(published) > 1 else "support_not_in_export"
            raise UnitResolutionError(code, f"{kind} {source_id}@{version}: found {len(published)} published rows in export")
        row = published[0]
        payload = _object(row.get("json"), f"{kind} {source_id}@{version}.json")
        data, embedded_id, embedded_version = _dataset_identity(payload, kind)
        if (embedded_id, embedded_version) != (source_id, version):
            raise UnitResolutionError("identity_mismatch",
                                      f"{kind} {source_id}@{version}: embedded identity differs")
        return data


def _unwrap_flow(value):
    value = _object(value, "flow")
    if "flowDataSet" in value:
        payload = value
        envelope_id = envelope_version = None
    elif isinstance(value.get("flow"), dict):
        if value.get("status") not in (None, "resolved_remote_flow"):
            raise UnitResolutionError("invalid_record", "flow: CLI get response is not resolved")
        payload = value["flow"]
        envelope_id = value.get("flow_id", value.get("id"))
        envelope_version = value.get("resolved_version", value.get("version"))
    else:
        raise UnitResolutionError("invalid_record", "flow: expected TIDAS data set or CLI get response")
    try:
        data = dataset(payload, "flow")
        source_id, version, name = identity(data, "flow")
    except IngestError as exc:
        raise UnitResolutionError("invalid_record", str(exc)) from exc
    if envelope_id is not None and envelope_id != source_id:
        raise UnitResolutionError("identity_mismatch", "flow: envelope ID differs from TIDAS payload")
    if envelope_version is not None and envelope_version != version:
        raise UnitResolutionError("identity_mismatch", "flow: envelope version differs from TIDAS payload")
    return data, source_id, version, name


def resolve_reference_unit(flow, support):
    """Return the exact reference-property/unit chain; no physical conversion."""
    data, flow_id, flow_version, flow_name = _unwrap_flow(flow)
    internal_id = _internal_id(
        nested(data, "flowInformation", "quantitativeReference", "referenceToReferenceFlowProperty"),
        "flow.flowInformation.quantitativeReference.referenceToReferenceFlowProperty")
    flow_property = _single(nested(data, "flowProperties", "flowProperty"), internal_id,
                            "flow.flowProperties.flowProperty")
    property_id, property_version = _reference(
        flow_property.get("referenceToFlowPropertyDataSet"),
        "flow.flowProperties.flowProperty.referenceToFlowPropertyDataSet")
    property_data = support.get("flowproperties", property_id, property_version)
    group_id, group_version = _reference(
        nested(property_data, "flowPropertiesInformation", "quantitativeReference",
               "referenceToReferenceUnitGroup"),
        "flowProperty.flowPropertiesInformation.quantitativeReference.referenceToReferenceUnitGroup")
    group_data = support.get("unitgroups", group_id, group_version)
    unit_id = _internal_id(
        nested(group_data, "unitGroupInformation", "quantitativeReference", "referenceToReferenceUnit"),
        "unitGroup.unitGroupInformation.quantitativeReference.referenceToReferenceUnit")
    unit = _single(nested(group_data, "units", "unit"), unit_id, "unitGroup.units.unit")
    unit_name = _required_text(unit.get("name"), "unitGroup.units.unit.name")
    property_name = text_value(nested(property_data, "flowPropertiesInformation",
                                      "dataSetInformation", "common:name"))
    return {
        "status": "resolved",
        "flow": {"id": flow_id, "version": flow_version, "name": flow_name,
                 "reference_flow_property_internal_id": internal_id},
        "flow_property": {"id": property_id, "version": property_version, "name": property_name},
        "unit_group": {"id": group_id, "version": group_version},
        "reference_unit": {"internal_id": unit_id, "name": unit_name},
        "support_source": {"kind": "tiangong-cli-support-export",
                           "captured_at_utc": support.report.get("captured_at_utc"),
                           "project_ref": support.report.get("project_ref"),
                           "directory": str(support.directory)},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--flow-file", help="Saved flow get JSON or TIDAS flow data set")
    source.add_argument("--flow-id", help="Exact TianGong flow ID for read-only flow get")
    parser.add_argument("--version", help="Required with --flow-id; exact version, no latest fallback")
    parser.add_argument("--support-dir", required=True,
                        help="Completed dataset support-cache export directory")
    parser.add_argument("--cli", nargs="+", default=["tiangong-lca"],
                        help="Official CLI executable for --flow-id")
    args = parser.parse_args(argv)
    try:
        if args.flow_id:
            if not args.version:
                raise UnitResolutionError("version_required", "--version is required with --flow-id")
            flow = remote_get(args.cli, "flow", args.flow_id, args.version)
        else:
            if args.version:
                raise UnitResolutionError("invalid_options", "--version requires --flow-id")
            flow = json.loads(Path(args.flow_file).read_text(encoding="utf-8-sig"))
        result = resolve_reference_unit(flow, SupportExport(args.support_dir))
    except (UnitResolutionError, IngestError, OSError, json.JSONDecodeError) as exc:
        code = exc.code if isinstance(exc, UnitResolutionError) else "read_failed"
        print(json.dumps({"status": "unresolved", "diagnostic": {"code": code, "detail": str(exc)}},
                         ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
