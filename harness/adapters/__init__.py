"""Thin, read-only native adapter bridge; no SQLite, model send or authorization logic.

An explicit Windows node.exe + windows_interop=True lets WSL use the same public
Windows Codex installation. No installation, daemon, private state or PATH edit.
The long-lived Node CodexAdapter API is the mutation integration point.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


class AdapterInvocationError(RuntimeError):
    def __init__(self, code, message, facts=None):
        super().__init__(message)
        self.code, self.facts = code, facts or {}


def _windows_path(path):
    result = subprocess.run(["wslpath", "-w", str(path)], text=True, capture_output=True, check=True, timeout=5)
    return result.stdout.strip()


def observe(operation, *, host_id, codex_command="codex", node_command="node", process_cwd=None,
            windows_interop=False, thread_id=None, turn_id=None, client_message_id=None,
            connect=False, max_pages=3, page_size=20, timeout_ms=15000,
            anchor_turn_id=None, anchor_item_id=None, anchor_content_sha256=None,
            max_item_pages=3, item_page_size=20, max_turns=30, max_items=200, max_messages=64,
            max_text_bytes=16384, max_total_text_bytes=65536, max_requests=20, deadline_ms=30000):
    """Return a versioned public observation for an exact target.

    Scope and coverage remain in the response. A timeout is UNKNOWN, with no
    retry. This function neither persists facts nor claims model-task adoption.
    For live mutation streams, use codex.mjs with management guard + factSink.
    """
    if operation not in {"version", "capabilities", "read", "reconcile", "timeline"}:
        raise AdapterInvocationError("UNSUPPORTED", "The Python bridge is read-only")
    if not host_id:
        raise AdapterInvocationError("INVALID", "An explicit host identity is required")
    if operation in {"read", "reconcile", "timeline"} and not thread_id:
        raise AdapterInvocationError("INVALID", "An exact thread identity is required")
    if operation == "reconcile" and not (turn_id or client_message_id):
        raise AdapterInvocationError("INVALID", "An exact turn or client message identity is required")
    if not 1 <= max_pages <= 10 or not 1 <= page_size <= 100 or not 1 <= timeout_ms <= 60000:
        raise AdapterInvocationError("INVALID", "Read bounds are outside the supported limits")
    if operation == 'timeline':
        if (anchor_item_id is not None or anchor_content_sha256 is not None) and anchor_turn_id is None:
            raise AdapterInvocationError('INVALID', 'Timeline item/content anchor needs its exact turn')
        if anchor_content_sha256 is not None and anchor_item_id is None:
            raise AdapterInvocationError('INVALID', 'Timeline content anchor needs its exact item')
        for name, value, low, high in (('page_size', page_size, 1, 50), ('max_item_pages', max_item_pages, 1, 10),
                ('item_page_size', item_page_size, 1, 100), ('max_turns', max_turns, 1, 100), ('max_items', max_items, 1, 1000),
                ('max_messages', max_messages, 1, 128), ('max_text_bytes', max_text_bytes, 0, 65536),
                ('max_total_text_bytes', max_total_text_bytes, 0, 262144), ('max_requests', max_requests, 1, 100), ('deadline_ms', deadline_ms, 1, 60000)):
            if type(value) is not int or not low <= value <= high:
                raise AdapterInvocationError('INVALID', name + ' is outside its finite bound')
    module = Path(__file__).with_name("cli.mjs").resolve()
    cwd = str(Path(process_cwd).resolve()) if process_cwd is not None else str(module.parent)
    module_arg = str(module)
    if windows_interop:
        if os.name == "nt":
            raise AdapterInvocationError("INVALID", "windows_interop is only for a WSL caller")
        module_arg, cwd = _windows_path(module), _windows_path(cwd)
    args = [str(node_command), module_arg, operation, "--host", str(host_id),
            "--codex", str(codex_command), "--process-cwd", cwd, "--timeout-ms", str(timeout_ms)]
    for key, value in (("thread", thread_id), ("turn", turn_id), ("client-message-id", client_message_id)):
        if value is not None:
            args += ["--" + key, str(value)]
    if connect:
        args += ["--connect"]
    if operation == "reconcile":
        args += ["--max-pages", str(max_pages), "--page-size", str(page_size)]
    if operation == 'timeline':
        for key, value in (('anchor-turn', anchor_turn_id), ('anchor-item', anchor_item_id), ('anchor-content-sha256', anchor_content_sha256),
                ('max-pages', max_pages), ('page-size', page_size), ('max-item-pages', max_item_pages), ('item-page-size', item_page_size),
                ('max-turns', max_turns), ('max-items', max_items), ('max-messages', max_messages), ('max-text-bytes', max_text_bytes),
                ('max-total-text-bytes', max_total_text_bytes), ('max-requests', max_requests), ('deadline-ms', deadline_ms)):
            if value is not None:
                args += ['--' + key, str(value)]
    # The envelope timeout includes all bounded RPC pages and cleanup. No shell.
    budget = 20 + (max_pages + 4) * timeout_ms / 1000
    if operation == 'timeline':
        budget = 20 + (deadline_ms + 2 * timeout_ms) / 1000
    try:
        completed = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=budget)
    except subprocess.TimeoutExpired as exc:
        raise AdapterInvocationError("UNKNOWN", "Adapter invocation timed out; no retry was performed",
                                     {"cleanup_confirmed": False, "no_automatic_retry": True}) from exc
    except OSError as exc:
        raise AdapterInvocationError("UNAVAILABLE", "The configured Node runtime could not be launched",
                                     {"errno": exc.errno}) from exc
    if len(completed.stdout) > 4 * 1024 * 1024:
        raise AdapterInvocationError("PROTOCOL_ERROR", "Adapter response exceeded the bridge bound")
    try:
        result = json.loads(completed.stdout)
    except (ValueError, TypeError) as exc:
        raise AdapterInvocationError("PROTOCOL_ERROR", "Adapter did not return a JSON envelope") from exc
    if not isinstance(result, dict):
        raise AdapterInvocationError("PROTOCOL_ERROR", "Adapter response is not a JSON envelope")
    # A missing native CLI is a useful version observation, not a bridge crash.
    if completed.returncode or (result.get("error") and result.get("operation") != "version"):
        error = result.get("error", {})
        raise AdapterInvocationError(error.get("code", "ADAPTER_FAILURE"), error.get("message", "Native observation failed"), result)
    return {**result, "bridge": {"platform": os.name, "windows_interop": windows_interop,
                                  "read_only": True, "facts_persisted": False}}
