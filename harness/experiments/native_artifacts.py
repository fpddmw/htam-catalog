"""Pure bounded native-output contracts and byte-preserving artifact validation.

Model output is data. This module performs no filesystem, network or execution IO.
Invalid frozen configuration raises HarnessError; invalid model data returns a
failed validation with no accepted files. Unexpected programming errors propagate.
"""
import hashlib
import json
import math

from ..common import HarnessError


PROTOCOL = 'htam.native-output/v2'
LEGACY_PROTOCOL = 'htam.native-output/v1'
MAX_FILES = 16
MAX_BYTES = 32768
MAX_FILE_BYTES = 16384
MAX_DEPTH = 16
MAX_NODES = 4096
MAX_SCHEMA_NODES = 256
MAX_SCHEMA_DEPTH = 10
TYPES = ('object', 'array', 'string', 'number', 'integer', 'boolean', 'null')
DEFAULT_SCHEMA = {'type': 'object', 'additionalProperties': False,
    'properties': {'value': {'type': ['string', 'number', 'boolean', 'null']}}, 'required': ['value']}


class _OutputError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _bad(code):
    raise _OutputError(code)


def _config(ok, message):
    if not ok:
        raise HarnessError(message, 'INVALID_OUTPUT_CONTRACT')


def _bound(value, default, ceiling, name, minimum=1):
    value = default if value is None else value
    _config(type(value) is int and minimum <= value <= ceiling, 'Invalid bounded ' + name)
    return value


def _text_bytes(text, fail):
    if '\x00' in text:
        fail('TEXT_NUL')
    try:
        return text.encode('utf-8', errors='strict')
    except UnicodeEncodeError:
        fail('INVALID_UTF8')


def _path(name, fail):
    if not isinstance(name, str) or not name:
        fail('INVALID_ARTIFACT_PATH')
    encoded = _text_bytes(name, fail)
    if len(encoded) > 240 or '\\' in name or name.startswith('/') or any(ord(c) < 32 or c in '<>:"|?*' for c in name):
        fail('INVALID_ARTIFACT_PATH')
    devices = {'con', 'prn', 'aux', 'nul', 'conin$', 'conout$'}
    devices.update(prefix + suffix for prefix in ('com', 'lpt') for suffix in '123456789¹²³')
    for segment in name.split('/'):
        if not segment or segment in ('.', '..') or segment.endswith(('.', ' ')) or segment.split('.')[0].rstrip(' .').casefold() in devices:
            fail('INVALID_ARTIFACT_PATH')
    return name


def _names(names, fail, *, max_files=MAX_FILES):
    if type(max_files) is not int or max_files < 1:
        raise ValueError('A finite positive file count is required')
    if len(names) > max_files:
        fail('ARTIFACT_COUNT_LIMIT')
    folded = set()
    for name in names:
        _path(name, fail)
        key = name.casefold()
        if key in folded:
            fail('ARTIFACT_NAME_COLLISION')
        folded.add(key)
    for name in folded:
        if any('/'.join(name.split('/')[:n]) in folded for n in range(1, len(name.split('/')))):
            fail('ARTIFACT_NAME_COLLISION')


def _config_fail(code):
    raise HarnessError(code, 'INVALID_OUTPUT_CONTRACT')


def _schema(value, depth_limit, *, legacy=False):
    nodes = 0

    def visit(node, depth):
        nonlocal nodes
        nodes += 1
        _config(nodes <= MAX_SCHEMA_NODES and depth <= min(depth_limit, MAX_DEPTH if legacy else MAX_SCHEMA_DEPTH),
            'Schema depth/node limit exceeded')
        _config(isinstance(node, dict) and 'type' in node and len(node) <= 8, 'Each schema node needs a bounded structural type')
        types = node['type'] if isinstance(node['type'], list) else [node['type']]
        _config(0 < len(types) <= len(TYPES) and all(isinstance(t, str) and t in TYPES for t in types) and len(set(types)) == len(types), 'Invalid schema types')
        allowed = {'type'}
        if 'object' in types:
            allowed.update(('properties', 'required', 'additionalProperties'))
        if 'array' in types:
            allowed.update(('items', 'maxItems'))
        if 'string' in types:
            allowed.add('maxLength')
        _config(set(node) <= allowed, 'Unsupported or nonstructural schema keyword')
        result = {'type': [t for t in TYPES if t in types] if isinstance(node['type'], list) else types[0]}
        if 'object' in types:
            props = node.get('properties', {})
            required = node.get('required', [])
            _config(isinstance(props, dict) and len(props) <= MAX_SCHEMA_NODES and node.get('additionalProperties', False) is False, 'Only closed finite objects are supported')
            _config(isinstance(required, list) and len(required) <= len(props) and all(isinstance(k, str) for k in required) and
                len(set(required)) == len(required) and set(required) <= set(props), 'Invalid required property names')
            if not legacy:
                _config(set(required) == set(props), 'Strict native schema requires every object property in required; use an explicit nullable type for optional data')
            _config(all(isinstance(k, str) for k in props), 'Property names must be text')
            for name in props:
                _config(len(_text_bytes(name, _config_fail)) <= MAX_FILE_BYTES, 'Property name too long')
            result.update(properties={name: visit(props[name], depth + 1) for name in sorted(props)},
                required=sorted(required), additionalProperties=False)
        if 'array' in types:
            _config('items' in node, 'Array items schema required')
            result.update(items=visit(node['items'], depth + 1),
                maxItems=_bound(node.get('maxItems'), 256, 256, 'array items', 0))
        if 'string' in types:
            result['maxLength'] = _bound(node.get('maxLength'), MAX_BYTES, MAX_BYTES, 'string length', 0)
        return result

    normalized = visit(value, 1)
    if not legacy:
        _config(normalized['type'] == 'object', 'Strict native schema root must be a single object type')
    _config(len(json.dumps(normalized, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) <= MAX_BYTES,
        'Schema byte limit exceeded')
    return normalized


def _baseline(value):
    _config(isinstance(value, dict) and value, 'Generation requires a finite baseline manifest')
    _names(value, _config_fail)
    result = {}
    for name in sorted(value):
        item = value[name]
        _config(isinstance(item, dict) and set(item) == {'sha256', 'bytes'}, 'Invalid baseline manifest record')
        _config(isinstance(item['sha256'], str) and len(item['sha256']) == 64 and
            all(c in '0123456789abcdef' for c in item['sha256']), 'Invalid baseline content hash')
        _config(type(item['bytes']) is int and 0 <= item['bytes'] <= MAX_BYTES, 'Invalid baseline byte count')
        result[name] = {'sha256': item['sha256'], 'bytes': item['bytes']}
    return result


def freeze_output_contract(spec=None, *, generation=False, baseline_manifest=None, editable=()):
    """Normalize a closed JSON or named-text protocol; never derives schema from E."""
    return _freeze_output_contract(spec, generation=generation, baseline_manifest=baseline_manifest, editable=editable)


def _freeze_output_contract(spec=None, *, generation=False, baseline_manifest=None, editable=(), legacy=False):
    # The legacy branch is read/recovery-only; no new request uses it.
    protocol = LEGACY_PROTOCOL if legacy else PROTOCOL
    _config(type(generation) is bool, 'Generation flag must be explicit boolean')
    spec = {} if spec is None else spec
    _config(isinstance(spec, dict), 'Native output contract must be an object')
    if 'protocol' in spec:
        _config(spec['protocol'] == protocol and spec.get('generation') is generation, 'Normalized output mode differs; old contracts require a new freeze for sending')
        derived = {'protocol', 'generation', 'baseline_manifest', 'editable', 'required_files', 'optional_null_files'}
        if spec.get('kind') == 'text_artifacts':
            derived.add('schema')
        normalized = _freeze_output_contract({k: v for k, v in spec.items() if k not in derived},
            generation=generation, baseline_manifest=baseline_manifest, editable=editable, legacy=legacy)
        _config(normalized == spec, 'Normalized output contract changed or contains extra fields')
        return normalized
    kind = spec.get('kind', 'text_artifacts' if generation else 'json')
    _config(kind in ('json', 'text_artifacts') and (not generation or kind == 'text_artifacts'), 'Unsupported native output kind')
    shared = {'kind', 'max_bytes', 'max_depth', 'max_nodes'}
    _config(set(spec) <= shared | ({'artifact', 'schema'} if kind == 'json' else {'files', 'max_total_bytes'}),
        'Unknown native output setting')
    result = {'protocol': protocol, 'kind': kind, 'generation': generation,
        'max_bytes': _bound(spec.get('max_bytes'), MAX_BYTES, MAX_BYTES, 'final bytes'),
        'max_depth': _bound(spec.get('max_depth'), 8, MAX_DEPTH, 'JSON depth'),
        'max_nodes': _bound(spec.get('max_nodes'), 2048, MAX_NODES, 'JSON nodes')}
    if kind == 'json':
        _config(baseline_manifest is None and not editable, 'JSON output cannot merge a baseline')
        result['artifact'] = _path(spec.get('artifact', 'result.json'), _config_fail)
        result['schema'] = _schema(spec.get('schema', DEFAULT_SCHEMA), result['max_depth'], legacy=legacy)
        return result
    total = _bound(spec.get('max_total_bytes'), MAX_BYTES, MAX_BYTES, 'artifact total bytes')
    if generation:
        baseline = _baseline(baseline_manifest)
        _config(isinstance(editable, (list, tuple)) and 0 < len(editable) <= MAX_FILES and all(isinstance(k, str) for k in editable) and
            len(set(editable)) == len(editable) and set(editable) <= set(baseline), 'Exact nonempty editable baseline names required')
        _config(sum(item['bytes'] for item in baseline.values()) <= total, 'Baseline exceeds final bundle byte limit')
        expected = set(editable)
        files = spec.get('files', {name: {} for name in editable})
        _config(isinstance(files, dict) and set(files) == expected, 'Generation file names must equal editable names')
        result.update(baseline_manifest=baseline, editable=sorted(editable))
    else:
        _config(baseline_manifest is None and not editable, 'Baseline merge requires generation mode')
        files = spec.get('files')
        _config(isinstance(files, dict) and files, 'Explicit named-text files required')
    _names(files, _config_fail)
    bounds = {}
    for name in sorted(files):
        item = files[name]
        _config(isinstance(item, dict) and set(item) <= {'max_bytes'}, 'Invalid named-text constraint')
        bounds[name] = {'max_bytes': _bound(item.get('max_bytes'), MAX_FILE_BYTES, MAX_FILE_BYTES, 'file bytes')}
        if generation:
            _config(result['baseline_manifest'][name]['bytes'] <= bounds[name]['max_bytes'], 'Editable baseline exceeds text byte limit')
    result.update(files=bounds, required_files=[] if generation else sorted(bounds), max_total_bytes=total)
    if not legacy:
        result['optional_null_files'] = sorted(bounds) if generation else []
    result['schema'] = _schema({'type': 'object', 'properties': {'files': {'type': 'object',
        'properties': {name: {'type': ['string', 'null'] if generation and not legacy else 'string',
            'maxLength': bound['max_bytes']} for name, bound in bounds.items()},
        'required': result['required_files'] if legacy else sorted(bounds), 'additionalProperties': False}},
        'required': ['files'], 'additionalProperties': False}, result['max_depth'], legacy=legacy)
    return result


def _verified_contract(contract):
    _config(isinstance(contract, dict) and contract.get('protocol') in (PROTOCOL, LEGACY_PROTOCOL) and type(contract.get('generation')) is bool,
        'A normalized frozen native output contract is required')
    legacy = contract['protocol'] == LEGACY_PROTOCOL
    derived = {'protocol', 'generation', 'baseline_manifest', 'editable', 'required_files', 'optional_null_files'}
    if contract.get('kind') == 'text_artifacts':
        derived.add('schema')
    config = {k: v for k, v in contract.items() if k not in derived}
    normalized = _freeze_output_contract(config, generation=contract['generation'],
        baseline_manifest=contract.get('baseline_manifest'), editable=contract.get('editable', ()), legacy=legacy)
    _config(normalized == contract, 'Frozen native output contract has changed or is not normalized')
    return normalized


def _base_files(base_files, contract):
    if not contract['generation']:
        _config(base_files is None, 'Base files are only accepted in generation mode')
        return {}
    baseline = contract['baseline_manifest']
    if not isinstance(base_files, dict) or set(base_files) != set(baseline):
        raise HarnessError('Frozen baseline files unavailable', 'BASE_MANIFEST_MISMATCH')
    for name, content in base_files.items():
        if type(content) is not bytes or {'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)} != baseline[name]:
            raise HarnessError('Frozen baseline content differs', 'BASE_MANIFEST_MISMATCH')
        if name in contract['editable']:
            try:
                _text_bytes(content.decode('utf-8', errors='strict'), _bad)
            except (UnicodeDecodeError, _OutputError) as error:
                raise HarnessError('Editable baseline is not valid UTF-8 text', 'BASE_MANIFEST_MISMATCH') from error
    return dict(base_files)


def _parse(text, contract):
    # Reject excessive nesting before the recursive standard JSON decoder runs.
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '[{':
            depth += 1
            if depth > contract['max_depth']:
                _bad('DEPTH_LIMIT')
        elif char in ']}':
            depth -= 1

    def pairs(items):
        obj = {}
        for name, value in items:
            if name in obj:
                _bad('DUPLICATE_KEY')
            obj[name] = value
        return obj

    def integer(value):
        if len(value.lstrip('-')) > 1024:
            _bad('NUMBER_LIMIT')
        return int(value)

    value = json.loads(text, object_pairs_hook=pairs, parse_constant=lambda _: _bad('NONFINITE_NUMBER'), parse_int=integer)
    nodes = 0

    def walk(item, level):
        nonlocal nodes
        nodes += 1
        if nodes > contract['max_nodes']:
            _bad('NODE_LIMIT')
        if level > contract['max_depth']:
            _bad('DEPTH_LIMIT')
        if isinstance(item, str):
            _text_bytes(item, _bad)
        elif isinstance(item, float) and not math.isfinite(item):
            _bad('NONFINITE_NUMBER')
        elif isinstance(item, dict):
            for name, child in item.items():
                walk(name, level + 1)
                walk(child, level + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, level + 1)
    walk(value, 1)
    return value


def _validate_schema(value, schema):
    types = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
    matches = {'object': isinstance(value, dict), 'array': isinstance(value, list), 'string': isinstance(value, str),
        'number': type(value) in (int, float), 'integer': type(value) is int or type(value) is float and value.is_integer(),
        'boolean': type(value) is bool, 'null': value is None}
    if not any(matches[name] for name in types):
        _bad('SCHEMA_MISMATCH')
    if isinstance(value, dict):
        if not set(schema['required']) <= set(value) or not set(value) <= set(schema['properties']):
            _bad('SCHEMA_MISMATCH')
        for name, child in value.items():
            _validate_schema(child, schema['properties'][name])
    elif isinstance(value, list):
        if len(value) > schema['maxItems']:
            _bad('SCHEMA_MISMATCH')
        for child in value:
            _validate_schema(child, schema['items'])
    elif isinstance(value, str) and len(value) > schema['maxLength']:
        _bad('SCHEMA_MISMATCH')


def validate_output(text: str, contract, *, base_files: dict[str, bytes] | None = None):
    """Return accepted exact bytes, or a known invalid-model result without IO."""
    if not isinstance(text, str):
        raise TypeError('Model output text must be str')
    contract = _verified_contract(contract)
    baseline = _base_files(base_files, contract)
    raw_hash = None
    try:
        try:
            raw = text.encode('utf-8', errors='strict')
        except UnicodeEncodeError:
            _bad('INVALID_UTF8')
        raw_hash = hashlib.sha256(raw).hexdigest()
        if '\x00' in text:
            _bad('TEXT_NUL')
        if len(raw) > contract['max_bytes']:
            _bad('RAW_BYTES_LIMIT')
        parsed = _parse(text, contract)
        if contract['kind'] == 'text_artifacts':
            if isinstance(parsed, dict) and isinstance(parsed.get('files'), dict):
                _names(parsed['files'], _bad)
                if not set(parsed['files']) <= set(contract['files']):
                    _bad('UNKNOWN_ARTIFACT')
        _validate_schema(parsed, contract['schema'])
        if contract['kind'] == 'json':
            files = {contract['artifact']: raw}
            model_names = set(files)
        else:
            # Only the contract's predeclared nullable file slots mean omission.
            # JSON output bypasses this branch and always retains its raw bytes.
            additions = {name: _text_bytes(value, _bad) for name, value in parsed['files'].items()
                if not (value is None and name in contract.get('optional_null_files', []))}
            for name, content in additions.items():
                if len(content) > contract['files'][name]['max_bytes']:
                    _bad('ARTIFACT_BYTES_LIMIT')
            files = {**baseline, **additions}
            if sum(map(len, files.values())) > contract['max_total_bytes']:
                _bad('TOTAL_BYTES_LIMIT')
            model_names = set(additions)
        manifest = {name: {'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content),
            'origin': 'model' if name in model_names else 'baseline'} for name, content in sorted(files.items())}
        return {'valid': True, 'error_code': None, 'files': files, 'manifest': manifest, 'raw_text_sha256': raw_hash}
    except (_OutputError, json.JSONDecodeError) as error:
        return {'valid': False, 'error_code': error.code if isinstance(error, _OutputError) else 'INVALID_JSON',
            'files': {}, 'manifest': {}, 'raw_text_sha256': raw_hash}
