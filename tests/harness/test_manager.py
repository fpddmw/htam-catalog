import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from harness.common import HarnessError, digest
from harness.context import check_evidence, render_context
from harness.discovery import Discovery
from harness.host import fallback_hook, handle_hook
from harness.cli import hooks_config, snapshot
from harness.state import State


def work_patch(title="General work", status="active"):
    return {"title": title, "request": "Explain the existing material", "request_source": "user message A",
            "objective": "Help the user assess available evidence", "status": status,
            "constraints": ["No external writes"], "references": []}


def competing_update(directory, barrier, queue, value):
    state = State(Path(directory))
    barrier.wait()
    try:
        state.update("w", 1, {"next_action": value}, "concurrent update")
        queue.put("ok")
    except HarnessError:
        queue.put("conflict")
    finally:
        state.close()


def interrupted_update(directory):
    state = State(Path(directory))
    state.db.execute("BEGIN IMMEDIATE")
    state.db.execute("UPDATE works SET revision=99 WHERE id='w'")
    state.event("w", "unfinished", "crash test", {})
    os._exit(23)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / "store"
        self.state = State(self.directory)
        self.state.update("w", 0, work_patch(), "original user request")
        (self.root / "result.md").write_text("A scoped inventory")

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def task(self, identifier="t", work="w", revision=1, **extra):
        completion = {"purpose": "A scoped local inventory", "checks": [{"id": "inventory", "kind": "files", "description": "The requested inventory exists", "basis": "Local inventory is the requested deliverable", "inputs": [{"path": "result.md"}]}]}
        return self.state.create_task(identifier, work, revision, {"expected": "Inventory with sources", "purpose": "Answer the requested assessment", "completion": completion, **extra}, "plan")

    def reserve(self, key, task_id, target, source):
        task = self.state.task(task_id)
        work = self.state.get(task['work_id'])
        binding = self.state.binding('main')
        self.state.bind('main', work['id'], 'main', 'fixture', binding['revision'] if binding else 0)
        self.state.bind(target, work['id'], 'executor', 'fixture')
        self.state.decide(work['id'], work['revision'], {
            'action': 'continue', 'reason': 'The requested inventory is still needed',
            'expected_gain': 'Provide the requested evidence', 'tasks': [task_id],
            'assessments': [{'requirement': 'overall', 'state': 'open', 'remaining': ['Requested inventory'],
                'limitations': [], 'supports': [], 'reason': 'No overall result has yet been adopted'}]}, 'fixture decision')
        return self.state.reserve(key, task_id, target, source, {'threadId': target, 'prompt': 'Do the scoped inventory'})

    def test_old_revision_cannot_replace_user_correction(self):
        self.state.update("w", 1, {"status": "paused", "request": "Pause this work"}, "new user request")
        with self.assertRaises(HarnessError):
            self.state.update("w", 1, {"status": "active"}, "old completion")
        self.assertEqual(self.state.get("w")["status"], "paused")
        self.assertEqual(len(self.state.history("w")), 2)

    def test_administrative_update_keeps_current_tasks_valid(self):
        self.task()
        self.state.update("w", 1, {"next_action": "Continue the same task"}, "progress")
        self.assertEqual(self.state.get("w")["revision"], 2)
        self.assertEqual(self.state.get("w")["request_revision"], 1)
        self.reserve("k", "t", "worker", "same request")

    def test_concurrent_writers_detect_conflict(self):
        ctx = multiprocessing.get_context("fork")
        barrier, queue = ctx.Barrier(2), ctx.Queue()
        processes = [ctx.Process(target=competing_update, args=(str(self.directory), barrier, queue, str(i))) for i in range(2)]
        for p in processes:
            p.start()
        for p in processes:
            p.join(10)
            self.assertEqual(p.exitcode, 0)
        self.assertEqual(sorted([queue.get(timeout=1), queue.get(timeout=1)]), ["conflict", "ok"])
        self.assertEqual(self.state.get("w")["revision"], 2)
        self.assertEqual(len(self.state.history("w")), 2)

    def test_process_crash_rolls_back_state_and_event(self):
        p = multiprocessing.get_context("fork").Process(target=interrupted_update, args=(str(self.directory),))
        p.start(); p.join(10)
        self.assertEqual(p.exitcode, 23)
        self.assertEqual(self.state.get("w")["revision"], 1)
        self.assertEqual(len(self.state.history("w")), 1)

    def test_paused_work_does_not_stop_independent_work(self):
        self.state.update("paused", 0, work_patch("Other work", "paused"), "user pause")
        self.task("p", "paused")
        with self.assertRaises(HarnessError):
            self.reserve("k1", "p", "worker-p", "continue")
        self.task()
        self.assertEqual(self.reserve("k2", "t", "worker-w", "continue")["status"], "prepared")

    def test_duplicate_reservation_and_unknown_receipt_not_retried(self):
        self.task()
        self.reserve("k", "t", "worker", "dispatch")
        self.state.settle("k", "unknown", {"reason": "timeout"}, "read result")
        with self.assertRaises(HarnessError):
            self.reserve("another", "t", "worker", "retry")
        self.state.settle("k", "returned", {"readback": "completed"}, "observed current recipient")
        self.assertEqual(self.state.task("t")["status"], "returned")

    def test_tool_receipt_does_not_claim_recipient_started(self):
        self.task()
        self.reserve("k", "t", "worker", "dispatch")
        self.state.settle("k", "sent", {"observed": "tool returned"}, "tool receipt")
        self.assertEqual(self.state.task("t")["status"], "dispatched")
        self.state.settle("k", "started", {"readback": "active"}, "recipient snapshot")
        self.assertEqual(self.state.task("t")["status"], "running")

    def test_new_prompt_blocks_dispatch_until_explicit_reconciliation(self):
        self.state.bind("session", "w", "main", "bind")
        event = {"hook_event_name": "UserPromptSubmit", "session_id": "session", "turn_id": "new-turn", "prompt": "Change direction"}
        response = handle_hook(self.state, event, self.root)
        self.assertIn("Change direction", response["hookSpecificOutput"]["additionalContext"])
        self.task()
        with self.assertRaises(HarnessError):
            self.reserve("k", "t", "worker", "old plan")
        self.state.ack(self.state.pending("w")[0]["id"], "w", 1, "This is a status question; scope unchanged")
        self.reserve("k", "t", "worker", "current plan")

    def test_bound_session_context_ignores_other_recent_work(self):
        self.state.bind("s", "w", "main", "binding")
        self.state.update("recent", 0, work_patch("New unrelated work"), "other request")
        self.assertEqual(render_context(self.state, session="s")["work"]["id"], "w")
        self.assertEqual(render_context(self.state, session="unknown")["binding"], "unbound")

    def test_old_result_retained_but_cannot_be_adopted_as_current(self):
        self.task()
        self.state.update("w", 1, {"request": "Now only review the management design"}, "user changed scope")
        self.state.review_task("t", "returned", {"artifacts": ["old.json"]}, "old worker returned")
        with self.assertRaises(HarnessError):
            self.state.review_task("t", "adopted", {"artifacts": ["old.json"], "answer": "done", "remaining": [], "reviewer": "main", "review_mode": "self"}, "review")
        self.assertEqual(self.state.task("t")["status"], "returned")

    def test_adopting_subtask_does_not_complete_parent(self):
        self.task()
        self.state.review_task("t", "returned", {"artifacts": ["result.md"]}, "result")
        with self.assertRaises(HarnessError):
            self.state.review_task("t", "adopted", {"tests_passed": True}, "numeric check only")
        self.state.review_task("t", "adopted", {"artifacts": ["result.md"], "answer": "A partial inventory", "remaining": ["Explain coverage"], "reviewer": "main", "review_mode": "self"}, "review")
        self.assertEqual(self.state.get("w")["status"], "active")

    def test_judgment_changes_propagate_only_to_dependencies(self):
        p = self.root / "evidence.txt"; p.write_text("original")
        def body(refs):
            return {"claim": "A scoped result", "scope": "local experiment", "evidence": refs}
        self.state.judge("j1", "w", 0, "adopted", body([{"path": "evidence.txt", "sha256": digest(p)}]), "review")
        self.state.judge("j2", "w", 0, "adopted", body([{"judgment": "j1", "revision": 1}]), "review")
        self.state.judge("j3", "w", 0, "adopted", body([{"url": "https://example.org/independent"}]), "review")
        p.write_text("corrected")
        self.assertEqual(len(check_evidence(self.root, self.state, "w")), 2)
        self.assertEqual(self.state.judgment("j3")["status"], "adopted")
        self.task(judgments=["j2"])
        with self.assertRaises(HarnessError):
            self.reserve("k", "t", "worker", "continue")

    def test_host_guard_consumes_one_prepared_dispatch(self):
        self.state.bind("worker", "w", "executor", "bind")
        self.task(); self.reserve("k", "t", "worker", "dispatch")
        e = {"hook_event_name": "PreToolUse", "session_id": "main", "tool_name": "mcp__codex_app__send_message_to_thread", "tool_input": {"threadId": "worker", "prompt": "Do the scoped inventory"}}
        self.assertEqual(handle_hook(self.state, e, self.root), {})
        self.assertEqual(handle_hook(self.state, e, self.root)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.state.dispatch("k")["status"], "claimed")

    def test_host_guard_catches_pause_after_reservation(self):
        self.state.bind("worker", "w", "executor", "bind")
        self.task(); self.reserve("k", "t", "worker", "dispatch")
        self.state.update("w", self.state.get("w")["revision"], {"status": "paused"}, "user pause")
        e = {"hook_event_name": "PreToolUse", "session_id": "main", "tool_name": "mcp__codex_app__send_message_to_thread", "tool_input": {"threadId": "worker", "prompt": "Do the scoped inventory"}}
        self.assertEqual(handle_hook(self.state, e, self.root)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_source_change_between_reservation_and_call_requires_review(self):
        p = self.root / "evidence.txt"; p.write_text("original")
        self.state.judge("j", "w", 0, "adopted", {"claim": "usable", "scope": "test", "evidence": [{"path": "evidence.txt", "sha256": digest(p)}]}, "review")
        self.state.bind("worker", "w", "executor", "bind")
        self.task(judgments=["j"]); self.reserve("k", "t", "worker", "dispatch")
        p.write_text("changed")
        e = {"hook_event_name": "PreToolUse", "session_id": "main", "tool_name": "mcp__codex_app__send_message_to_thread", "tool_input": {"threadId": "worker", "prompt": "Do the scoped inventory"}}
        self.assertEqual(handle_hook(self.state, e, self.root)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.state.judgment("j")["status"], "needs_review")

    def test_cli_rejects_nonexistent_adoption_artifact(self):
        self.task(); self.state.review_task("t", "returned", {"note": "some output"}, "worker")
        review = self.root / "review.json"
        review.write_text(json.dumps({"artifacts": ["not-there.md"], "answer": "done", "remaining": [], "reviewer": "main", "review_mode": "self"}))
        script = Path(__file__).resolve().parents[2] / "scripts/harness.py"
        result = subprocess.run([sys.executable, str(script), "--root", str(self.root), "--store", str(self.directory), "review", "--task", "t", "--disposition", "adopted", "--file", str(review), "--source", "check"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Adoption artifact must exist", result.stderr)
        self.assertEqual(self.state.task("t")["status"], "returned")

    def test_degraded_hook_only_stops_known_managed_dispatch(self):
        self.state.bind("worker", "w", "executor", "bind"); snapshot(self.state)
        e = {"hook_event_name": "PreToolUse", "tool_name": "mcp__codex_app__send_message_to_thread", "tool_input": {"threadId": "worker", "prompt": "Do the scoped inventory"}}
        self.assertEqual(fallback_hook(self.directory, e, "database unavailable")["hookSpecificOutput"]["permissionDecision"], "deny")
        e["tool_name"] = "Bash"; e["tool_input"] = {"command": "read file"}
        self.assertNotIn("hookSpecificOutput", fallback_hook(self.directory, e, "database unavailable"))

    def test_backup_is_recoverable(self):
        p = self.root / "backup.sqlite"; self.state.backup(p)
        db = sqlite3.connect(p)
        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(db.execute("SELECT count(*) FROM works").fetchone()[0], 1)
        db.close()

    def test_hook_configuration_preserves_native_and_windows_arguments(self):
        config = hooks_config(self.root / "space in path", "Example-Distro")
        handler = config["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertIn("'", handler["command"])
        self.assertIn('"', handler["commandWindows"])
        self.assertNotIn("bypass", handler["commandWindows"].lower())


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.directory = self.root / "store"; (self.root / "materials").mkdir()
        self.index = Discovery(self.root, self.directory)
        self.state = State(self.directory)
        self.config = {"roots": ["materials"]}

    def tearDown(self):
        self.index.close(); self.state.close(); self.temp.cleanup()

    def test_ignored_files_renames_and_reading_survive_rebuild(self):
        (self.root / "materials/.gitignore").write_text("*.txt\n*.pdf\n")
        p = self.root / "materials/old.txt"; p.write_text("Catalyst kinetics 10.12345/experiment")
        self.index.scan(self.config)
        result = self.index.find("Catalyst")["results"][0]
        self.state.observation("sha256:" + result["sha"], "reading", {"sections": ["2"]}, "actual reading")
        p.rename(p.with_name("renamed.txt"))
        self.index.scan(self.config)
        self.assertEqual(self.index.find("Catalyst")["results"][0]["path"], "materials/renamed.txt")
        self.index.close(); (self.directory / "index.sqlite").unlink()
        self.index = Discovery(self.root, self.directory); self.index.scan(self.config)
        self.assertEqual(len(self.state.observations("sha256:" + result["sha"])), 1)

    def test_remote_failure_does_not_change_possession_or_reading(self):
        p = self.root / "materials/data.csv"; p.write_text("grid_load,42\n")
        self.index.scan(self.config); key = "sha256:" + digest(p)
        self.state.observation(key, "reading", {"verified_rows": "1"}, "checked")
        self.state.observation(key, "access", {"status": 405}, "remote fetch")
        self.assertEqual(self.index.find("grid_load")["results"][0]["presence"], "present")
        self.assertEqual(len(self.state.observations(key)), 2)

    def test_non_lca_chinese_binary_and_new_version(self):
        (self.root / "materials/计划.md").write_text("课程安排与课时记录")
        (self.root / "materials/report.pdf").write_bytes(b"binary-pdf")
        self.index.scan(self.config)
        self.assertEqual(self.index.find("课程")["results"][0]["text_state"], "complete")
        self.assertEqual(self.index.find("report.pdf")["results"][0]["text_state"], "metadata_only")
        p = self.root / "materials/v2.txt"; p.write_text("课程安排的新版")
        self.index.scan(self.config)
        self.assertEqual(self.index.find("课程")["matching_files"], 2)

    def test_credentials_and_symlinks_are_outside_coverage(self):
        (self.root / "materials/.env").write_text("secret-marker")
        (self.root / "materials/credentials.json").write_text('{"title":"secret-marker"}')
        external = self.root / "outside.txt"; external.write_text("secret-marker")
        (self.root / "materials/link.txt").symlink_to(external)
        report = self.index.scan(self.config)
        self.assertEqual(self.index.find("secret-marker")["matching_files"], 0)
        self.assertFalse(report["symlinks_followed"])

    def test_missing_scope_is_unknown_not_absent(self):
        p = self.root / "materials/kept.txt"; p.write_text("known-evidence")
        self.index.scan(self.config)
        (self.root / "materials").rename(self.root / "temporarily-unmounted")
        report = self.index.scan(self.config)
        self.assertTrue(report["errors"])
        self.assertEqual(self.index.find("known-evidence")["results"][0]["presence"], "unknown")

    def test_deleted_file_and_removed_scope_are_distinct(self):
        p = self.root / "materials/gone.txt"; p.write_text("known-evidence")
        self.index.scan(self.config); p.unlink(); self.index.scan(self.config)
        self.assertEqual(self.index.find("known-evidence")["results"][0]["presence"], "missing")
        self.index.scan({"roots": []})
        self.assertEqual(self.index.find("known-evidence")["matching_files"], 0)

    def test_json_index_exposes_metadata_not_arbitrary_secrets(self):
        p = self.root / "materials/provenance.json"
        p.write_text(json.dumps({"title": "Metadata title", "access_token": "do-not-index", "url": "https://example.org/?secret=signed"}))
        self.index.scan(self.config)
        self.assertEqual(self.index.find("Metadata title")["matching_files"], 1)
        self.assertEqual(self.index.find("do-not-index")["matching_files"], 0)


if __name__ == "__main__":
    unittest.main()
