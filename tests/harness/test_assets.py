import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness.assets import AssetRegistry
from harness.common import HarnessError, digest
from harness.state import State


def asset_body(path="source.txt", version="1", dependencies=None, **extra):
    return {"version": version, "type": "lci", "title": "A scoped material inventory",
            "description": "Source-derived quantities; use only in the declared scope",
            "provenance": [{"path": path}], "uses": [{"purpose": "conditional research",
                "state": "conditional", "scope": "Named source and boundary only", "evidence": [{"path": path}]}],
            "limitations": ["Regional representativeness unverified"], "depends_on": dependencies or [], **extra}


def competing_asset_update(directory, root, barrier, queue, version):
    state = State(Path(directory), root=Path(root))
    try:
        barrier.wait()
        AssetRegistry(state).put("inventory", 1, asset_body(version=version), "concurrent explicit update")
        queue.put("ok")
    except HarnessError:
        queue.put("conflict")
    finally:
        state.close()


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / "store"
        self.state = State(self.directory, root=self.root)
        self.registry = AssetRegistry(self.state)
        for name in ("source.txt", "independent.txt", "review.txt"):
            (self.root / name).write_text("Captured content for " + name)
        self.make_work("w")
        self.make_work("other")

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def make_work(self, identifier):
        return self.state.update(identifier, 0, {"title": "Material " + identifier,
            "request": "Develop scoped LCA data", "request_source": "Original user request",
            "objective": "An applicable data package", "status": "active"}, "work fixture")

    def test_snapshot_identity_versions_and_explicit_lifecycle_history(self):
        initial = self.registry.put("inventory", 0, asset_body(domain={"reference_mass": 1, "unit": "kg"}), "source capture")
        self.assertEqual(initial["revision"], 1)
        self.assertEqual(initial["body"]["provenance"][0]["sha256"], digest(self.root / "source.txt"))
        self.assertEqual(initial["body"]["domain"]["unit"], "kg")
        self.assertEqual(initial["validity"]["remote_state"], "not_rechecked")
        self.assertEqual(initial["validity"]["state"], "captured_local_evidence_matches")
        self.assertEqual(self.registry.put("inventory", 1, initial["body"], "idempotent explicit capture")["revision"], 1)
        changed = json.loads(json.dumps(initial["body"]))
        changed["description"] = "Changed quantities require a new version"
        with self.assertRaisesRegex(HarnessError, "immutable"):
            self.registry.put("inventory", 1, changed, "attempted overwrite")
        changed["version"] = "2"
        second = self.registry.put("inventory", 1, changed, "revised evidence")
        self.assertEqual(second["revision"], 2)
        historical = self.registry.get("inventory", "1")
        self.assertEqual(historical["sha256"], initial["sha256"])
        self.assertEqual(historical["body"], initial["body"])
        self.assertFalse(historical["is_current"])
        revised = self.registry.revise("inventory", 2, {"status": "retracted"}, "source interpretation withdrawn", version="1")
        self.assertEqual(revised["annotations"]["status"], "retracted")
        self.assertEqual(revised["body"], initial["body"])
        self.assertEqual(revised["sha256"], initial["sha256"])
        self.assertEqual(self.registry.get("inventory")["annotations"], {})
        self.assertEqual([r["revision"] for r in self.registry.history("inventory")], [1, 2, 3])
        with self.assertRaisesRegex(HarnessError, "Stale"):
            self.registry.revise("inventory", 2, {"status": "retired"}, "stale lifecycle change")

    def test_concurrent_version_update_preserves_winner_and_both_sources(self):
        self.registry.put("inventory", 0, asset_body(), "initial source")
        ctx = multiprocessing.get_context("fork")
        barrier, queue = ctx.Barrier(2), ctx.Queue()
        processes = [ctx.Process(target=competing_asset_update,
            args=(str(self.directory), str(self.root), barrier, queue, version)) for version in ("2a", "2b")]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sorted([queue.get(timeout=1), queue.get(timeout=1)]), ["conflict", "ok"])
        self.assertEqual(self.registry.get("inventory")["revision"], 2)
        self.assertEqual(self.registry.get("inventory", "1")["source"], "initial source")
        self.assertEqual(len(self.registry.history("inventory")), 2)

    def test_local_change_propagates_only_to_declared_dependencies(self):
        self.registry.put("source", 0, asset_body(), "captured source")
        self.registry.put("inventory", 0, asset_body(path="review.txt", dependencies=[{"id": "source", "version": "1"}]), "compiled inventory")
        self.registry.put("model", 0, asset_body(path="review.txt", dependencies=[{"id": "inventory", "version": "1"}]), "model assembly")
        self.registry.put("independent", 0, asset_body(path="independent.txt"), "independent evidence")
        self.registry.use("w", "overall", "model", "1", "Research only; no project adoption yet", "actual reuse")
        (self.root / "source.txt").write_text("Changed source facts")
        self.assertEqual(self.registry.get("model")["validity"]["state"], "needs_review")
        self.assertEqual(self.registry.get("independent")["validity"]["state"], "captured_local_evidence_matches")
        impact = self.registry.impact("source", "1")
        self.assertEqual({x["id"] for x in impact["assets"]}, {"source", "inventory", "model"})
        self.assertEqual(impact["works"], ["w"])
        self.assertEqual(impact["uses"][0]["applicability"], "needs_review")
        self.assertNotIn("assessments", self.state.get("w"))

    def test_new_upstream_version_keeps_old_pin_and_reports_review(self):
        first = self.registry.put("source", 0, asset_body(), "first source")
        self.registry.put("inventory", 0, asset_body(path="review.txt", dependencies=[{"id": "source", "version": "1"}]), "scoped compilation")
        self.registry.put("source", 1, asset_body(version="2"), "second source")
        item = self.registry.get("inventory")
        self.assertEqual(item["body"]["depends_on"], [{"id": "source", "version": "1"}])
        self.assertEqual(item["validity"]["state"], "needs_review")
        self.assertIn("dependency_has_new_version", [x["kind"] for x in item["validity"]["issues"]])
        self.assertEqual(self.registry.get("source", "1")["sha256"], first["sha256"])
        self.assertIn("inventory", {x["id"] for x in self.registry.impact("source")["assets"]})
        self.assertNotIn("inventory", {x["id"] for x in self.registry.impact("source", "2")["assets"]})

    def test_reuse_and_scoped_adoption_do_not_transfer_between_materials(self):
        self.registry.put("inventory", 0, asset_body(), "captured inventory")
        purpose = {"description": "Adopt source-only quantities", "state": "conditional",
            "scope": {"work": "w", "requirement": "overall"}, "limitations": ["No plant representativeness claim"],
            "evidence": [{"path": "review.txt"}]}
        adopted = self.registry.use("w", "overall", "inventory", "1", purpose, "main scientific review")
        self.assertEqual(adopted["applicability"], "scoped_adoption")
        with self.assertRaisesRegex(HarnessError, "exact work"):
            self.registry.use("other", "overall", "inventory", "1", purpose, "copying adoption is invalid")
        reused = self.registry.use("other", "overall", "inventory", "1", "Inspect applicability in another material", "separate material")
        self.assertEqual(reused["applicability"], "unreviewed")
        self.assertEqual(reused["state"], "reused")
        self.assertNotIn("assessments", self.state.get("w"))
        self.assertNotIn("assessments", self.state.get("other"))
        self.assertEqual(len(self.registry.list(work="other")[0]["work_usage"]), 1)
        self.assertEqual(self.registry.list(query="scoped material")[0]["id"], "inventory")

    def test_reuse_evidence_and_requirement_change_are_visible(self):
        self.registry.put("inventory", 0, asset_body(), "captured inventory")
        self.registry.use("w", "overall", "inventory", "1", {"description": "Precise local use", "state": "adopted",
            "scope": {"work": "w", "requirement": "overall"}, "limitations": [],
            "evidence": [{"path": "review.txt"}]}, "adoption")
        (self.root / "review.txt").write_text("review changed")
        use = self.registry.get("inventory")["usage"][0]
        self.assertEqual(use["applicability"], "needs_review")
        self.assertEqual(use["issues"][0]["kind"], "file_changed")
        self.state.update("w", 1, {"objective": "A corrected research question"}, "new user question")
        use = self.registry.get("inventory")["usage"][0]
        self.assertIn("work_request_changed", {x["kind"] for x in use["issues"]})

    def test_reference_validation_and_provenance_scope(self):
        with self.assertRaisesRegex(HarnessError, "Unknown asset"):
            self.registry.put("inventory", 0, asset_body(dependencies=[{"id": "missing", "version": "1"}]), "bad dependency")
        self.assertEqual(self.registry.list(), [])
        bad = asset_body()
        bad["provenance"][0]["sha256"] = "f" * 64
        with self.assertRaisesRegex(HarnessError, "content changed"):
            self.registry.put("inventory", 0, bad, "mismatched provenance")
        with self.assertRaisesRegex(HarnessError, "outside"):
            self.registry.put("inventory", 0, asset_body(path="../outside.txt"), "out of scope")
        self.registry.put("inventory", 0, asset_body(), "source")
        with self.assertRaisesRegex(HarnessError, "Unknown work"):
            self.registry.use("missing", "overall", "inventory", "1", "Reuse", "fixture")
        with self.assertRaisesRegex(HarnessError, "Unknown requirement"):
            self.registry.use("w", "missing", "inventory", "1", "Reuse", "fixture")
        with self.assertRaisesRegex(HarnessError, "Unknown asset version"):
            self.registry.use("w", "overall", "inventory", "2", "Reuse", "fixture")
        with self.assertRaisesRegex(HarnessError, "Exact asset-use version"):
            self.registry.use("w", "overall", "inventory", None, "Reuse", "fixture")

    def test_positive_use_claims_require_scope_and_exact_evidence(self):
        body = asset_body()
        body["uses"][0].pop("evidence")
        with self.assertRaisesRegex(HarnessError, "exact local evidence"):
            self.registry.put("inventory", 0, body, "unsupported positive claim")
        body["uses"][0]["state"] = "unreviewed"
        self.registry.put("inventory", 0, body, "explicitly unreviewed")
        with self.assertRaisesRegex(HarnessError, "exact local evidence"):
            self.registry.use("w", "overall", "inventory", "1", {"description": "adopt", "state": "adopted",
                "scope": {"work": "w", "requirement": "overall"}, "limitations": []}, "missing review")

    def test_readonly_legacy_state_returns_no_assets_without_creating_tables(self):
        before = {r[0] for r in self.state.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        readonly = State(self.directory, root=self.root, readonly=True)
        try:
            registry = AssetRegistry(readonly)
            self.assertEqual(registry.list(), [])
            self.assertEqual(registry.impact("not-registered")["assets"], [])
            with self.assertRaisesRegex(HarnessError, "Unknown asset"):
                registry.get("not-registered")
            with self.assertRaisesRegex(HarnessError, "Read-only"):
                registry.put("inventory", 0, asset_body(), "unauthorized write")
        finally:
            readonly.close()
        after = {r[0] for r in self.state.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(before, after)

    def test_readonly_current_registry_checks_evidence_without_writing(self):
        self.registry.put("inventory", 0, asset_body(), "source")
        readonly = State(self.directory, root=self.root, readonly=True)
        try:
            registry = AssetRegistry(readonly)
            self.assertEqual(registry.get("inventory")["validity"]["state"], "captured_local_evidence_matches")
            (self.root / "source.txt").unlink()
            self.assertEqual(registry.get("inventory")["validity"]["state"], "needs_review")
            with self.assertRaisesRegex(HarnessError, "Read-only"):
                registry.use("w", "overall", "inventory", "1", "reuse", "attempt")
            self.assertEqual(readonly.db.total_changes, 0)
        finally:
            readonly.close()

    def test_asset_registration_participates_in_outer_transaction(self):
        with self.assertRaisesRegex(HarnessError, "outer rollback"):
            with self.state.transaction():
                self.registry.put("inventory", 0, asset_body(), "source")
                raise HarnessError("outer rollback")
        self.assertEqual(self.registry.list(), [])
        self.assertNotIn("asset_version_registered", [x["kind"] for x in self.state.history("w")])

    def test_work_view_keeps_scoped_usage_of_a_historical_version(self):
        self.registry.put("inventory", 0, asset_body(), "first version")
        self.registry.use("w", "overall", "inventory", "1", "Original scoped use", "reuse")
        self.registry.put("inventory", 1, asset_body(version="2"), "new version")
        listed = self.registry.list(work="w")
        self.assertEqual(listed[0]["version"], "2")
        self.assertEqual(listed[0]["work_usage"][0]["version"], "1")
        self.assertEqual(listed[0]["work_usage"][0]["applicability"], "needs_review")

    def test_context_summary_is_bounded_metadata_without_file_validation(self):
        for identifier in ("a", "b", "c"):
            self.registry.put(identifier, 0, asset_body(), "source")
            self.registry.use("w", "overall", identifier, "1", "Original scoped use", "reuse")
        with patch("harness.assets.digest", side_effect=AssertionError("Context must not hash files")), \
             patch("harness.assets.local_path", side_effect=AssertionError("Context must not read files")):
            summary = self.registry.summary("w", limit=2)
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["omitted"], 1)
        self.assertEqual(summary["items"][0]["recorded_validity"], "not_rechecked")
        self.assertEqual(summary["items"][0]["work_usage"][0]["work"], "w")
        self.assertEqual(self.registry.summary("other")["count"], 0)
        with self.assertRaisesRegex(HarnessError, "limit"):
            self.registry.summary("w", limit=-1)

    def test_later_reuse_disposition_preserves_history_without_old_adoption(self):
        self.registry.put("inventory", 0, asset_body(), "source")
        self.registry.use("w", "overall", "inventory", "1", {"description": "Source-only compilation", "state": "conditional",
            "scope": {"work": "w", "requirement": "overall"}, "limitations": ["Source boundary only"],
            "evidence": [{"path": "review.txt"}]}, "initial scoped review")
        self.registry.use("w", "overall", "inventory", "1", {"description": "Source-only compilation", "state": "rejected"},
            "review found an applicability mismatch")
        uses = self.registry.get("inventory")["usage"]
        self.assertFalse(uses[0]["current"])
        self.assertEqual(uses[0]["applicability"], "historical_use")
        self.assertEqual(uses[0]["state"], "conditional")
        self.assertTrue(uses[1]["current"])
        self.assertEqual(uses[1]["applicability"], "rejected")

    def test_metadata_keeps_exact_work_pin_and_revision_without_file_reads(self):
        first = self.registry.put("inventory", 0, asset_body(), "first source")
        description = "Source-only compilation"
        self.registry.use("w", "overall", "inventory", "1", {"description": description, "state": "conditional",
            "scope": {"work": "w", "requirement": "overall"}, "limitations": ["Source boundary"],
            "evidence": [{"path": "review.txt"}]}, "initial review")
        self.registry.use("w", "overall", "inventory", "1", {"description": description, "state": "rejected"}, "later review")
        self.registry.put("inventory", 1, asset_body(version="2"), "new source")
        self.registry.revise("inventory", 2, {"status": "retired", "uses": [{"purpose": "conditional research",
            "state": "unavailable", "scope": "withdrawn scope", "evidence": []}]}, "withdraw original use", version="1")
        (self.root / "source.txt").unlink()
        with patch("harness.assets.digest", side_effect=AssertionError("Metadata must not hash files")), \
             patch("harness.assets.local_path", side_effect=AssertionError("Metadata must not inspect files")), \
             patch.object(self.registry, "get", side_effect=AssertionError("Metadata must not validate get")), \
             patch.object(self.registry, "_usage", side_effect=AssertionError("Metadata must not validate scoped uses")), \
             patch.object(self.registry, "_evidence_issues", side_effect=AssertionError("Metadata must not validate evidence")):
            scoped = self.registry.metadata("w")
            global_view = self.registry.metadata()
            unrelated = self.registry.metadata("other")
        self.assertEqual([a["version"] for a in scoped], ["1"])
        self.assertEqual(scoped[0]["current_version"], "2")
        self.assertEqual(scoped[0]["sha256"], first["sha256"])
        self.assertEqual(scoped[0]["version_revision"], 3)
        self.assertEqual(scoped[0]["annotations"]["status"], "retired")
        self.assertEqual(scoped[0]["annotations"]["uses"][0]["state"], "unavailable")
        self.assertEqual(scoped[0]["validity"]["state"], "not_rechecked")
        self.assertEqual([u["current"] for u in scoped[0]["work_usage"]], [False, True])
        self.assertEqual(scoped[0]["work_usage"][1]["state"], "rejected")
        self.assertEqual(scoped[0]["work_usage"][0]["scope"], {"work": "w", "requirement": "overall"})
        self.assertTrue(all(u["applicability"] == "not_rechecked" for u in scoped[0]["usage"]))
        self.assertEqual(global_view[0]["version"], "2")
        self.assertEqual(global_view[0]["annotations"], {})
        self.assertEqual(unrelated, [])


if __name__ == "__main__":
    unittest.main()
