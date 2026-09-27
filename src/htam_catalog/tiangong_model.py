"""Read-only inspection of native TianGong lifecyclemodel JSON.

This extracts identities and links needed for catalog mapping. It does not
resolve process datasets, evaluate parameters, or calculate inventories.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .catalog import CatalogError, digest, require


def _object(value, path):
    require(isinstance(value, dict), f"{path}: expected object", "INVALID_MODEL")
    return value


def _text(value):
    return value if isinstance(value, str) and value.strip() else None


def _items(value, path):
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    require(isinstance(value, list), f"{path}: expected object or array", "INVALID_MODEL")
    require(all(isinstance(item, dict) for item in value), f"{path}: array must contain objects", "INVALID_MODEL")
    return value


def _factor(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return str(amount) if amount.is_finite() else None


def inspect_tiangong_model(payload):
    """Summarize an actual json_ordered lifecyclemodel without changing it."""
    root = _object(payload, "model")
    if "json_ordered" in root:
        root = _object(root["json_ordered"], "json_ordered")
    model = _object(root.get("lifeCycleModelDataSet"), "lifeCycleModelDataSet")
    info = _object(model.get("lifeCycleModelInformation"), "lifeCycleModelInformation")
    identity = _object(info.get("dataSetInformation"), "dataSetInformation")
    admin = _object(model.get("administrativeInformation"), "administrativeInformation")
    ownership = _object(admin.get("publicationAndOwnership"), "publicationAndOwnership")
    technology = _object(info.get("technology"), "technology")
    processes = _object(technology.get("processes"), "technology.processes")
    instances_raw = _items(processes.get("processInstance"), "processInstance")
    diagnostics = []
    if not instances_raw:
        diagnostics.append({"code": "NO_PROCESS_INSTANCES", "path": "technology.processes.processInstance"})
    model_id = _text(identity.get("common:UUID"))
    model_version = _text(ownership.get("common:dataSetVersion"))
    if model_id is None:
        diagnostics.append({"code": "MISSING_MODEL_ID", "path": "dataSetInformation.common:UUID"})
    if model_version is None:
        diagnostics.append({"code": "MISSING_MODEL_VERSION", "path": "publicationAndOwnership.common:dataSetVersion"})

    instances = []
    seen = set()
    for position, raw in enumerate(instances_raw):
        path = f"processInstance[{position}]"
        instance_id = _text(raw.get("@dataSetInternalID"))
        if instance_id is None:
            diagnostics.append({"code": "MISSING_INSTANCE_ID", "path": path})
        elif instance_id in seen:
            diagnostics.append({"code": "DUPLICATE_INSTANCE_ID", "path": path, "value": instance_id})
        seen.add(instance_id)
        ref = _object(raw.get("referenceToProcess"), path + ".referenceToProcess")
        process_id = _text(ref.get("@refObjectId"))
        version = _text(ref.get("@version"))
        if process_id is None:
            diagnostics.append({"code": "MISSING_PROCESS_ID", "path": path + ".referenceToProcess"})
        if version is None:
            diagnostics.append({"code": "UNPINNED_PROCESS_REFERENCE", "path": path + ".referenceToProcess"})
        factor = _factor(raw.get("@multiplicationFactor"))
        if factor is None:
            diagnostics.append({"code": "INVALID_MULTIPLICATION_FACTOR", "path": path})
        instances.append({
            "instance_id": instance_id,
            "process_id": process_id,
            "requested_version": version,
            "multiplication_factor": factor,
            "parameters_present": "parameters" in raw,
        })

    connections = []
    for position, raw in enumerate(instances_raw):
        for edge_position, edge in enumerate(_items(_object(raw.get("connections", {}),
                                                       f"processInstance[{position}].connections").get("outputExchange"),
                                                  f"processInstance[{position}].connections.outputExchange")):
            path = f"processInstance[{position}].connections.outputExchange[{edge_position}]"
            from_flow = _text(edge.get("@flowUUID"))
            targets = _items(edge.get("downstreamProcess"), path + ".downstreamProcess")
            if not targets:
                diagnostics.append({"code": "MISSING_DOWNSTREAM_PROCESS", "path": path})
            for target in targets:
                to_id = _text(target.get("@id"))
                to_flow = _text(target.get("@flowUUID"))
                if to_id not in seen or to_id is None:
                    diagnostics.append({"code": "UNRESOLVED_DOWNSTREAM_INSTANCE", "path": path, "value": to_id})
                if from_flow is None or to_flow is None:
                    diagnostics.append({"code": "MISSING_CONNECTION_FLOW", "path": path})
                elif from_flow != to_flow:
                    diagnostics.append({"code": "CONNECTION_FLOW_MISMATCH", "path": path})
                connections.append({"from_instance": instances[position]["instance_id"],
                                    "to_instance": to_id, "from_flow_id": from_flow,
                                    "to_flow_id": to_flow})
    quantitative = _object(info.get("quantitativeReference", {}), "quantitativeReference")
    reference_id = _text(quantitative.get("referenceToReferenceProcess"))
    if reference_id is None:
        diagnostics.append({"code": "MISSING_REFERENCE_INSTANCE", "path": "quantitativeReference"})
    elif reference_id not in seen:
        diagnostics.append({"code": "UNRESOLVED_REFERENCE_PROCESS", "path": "quantitativeReference", "value": reference_id})
    return {
        "model_id": model_id,
        "requested_version": model_version,
        "content_hash": digest(payload),
        "reference_instance_id": reference_id,
        "instances": instances,
        "connections": connections,
        "diagnostics": diagnostics,
        "structurally_consistent": not diagnostics,
        "scope": "Structural inspection only; process records and actual returned versions unresolved",
    }
