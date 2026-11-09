"""Contract tests for local metadata, execution and gates; no remote services."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from harness.core import (HarnessError, SPEC_HEADINGS, canonical, lint, load_json,
                          new_json, policy, revision, safe_path, tasks, tree_digest)
from harness.gates import gate
from harness.runner import run_checks, validate_report

SOURCE = Path(__file__).resolve().parents[1]


class Workspace(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="x-harness-test-")
        self.root = Path(self.temporary.name).resolve()
        shutil.copytree(SOURCE / "harness", self.root / "harness", ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / "docs/specs").mkdir(parents=True)
        (self.root / "docs/tasks").mkdir()
        (self.root / "docs/templates").mkdir()
        shutil.copyfile(SOURCE / "docs/templates/task.json", self.root / "docs/templates/task.json")
        (self.root / "README.md").write_text("# Fixture\n[spec](docs/specs/SPEC-TEST.md)\n")
        (self.root / "AGENTS.md").write_text("# Fixture roles\nSeparate implementation and review.\n")
        (self.root / ".gitignore").write_text(".harness/reports/\n__pycache__/\n")
        spec = '<!-- harness-spec: {"id":"SPEC-TEST","status":"accepted"} -->\n# Fixture\n'
        spec += "".join(f"\n## {heading}\nExplicit fixture behavior for gate verification.\n" for heading in SPEC_HEADINGS)
        (self.root / "docs/specs/SPEC-TEST.md").write_text(spec)
        self.config = json.loads((SOURCE / "harness/policy.json").read_text())
        self.config["checks"] = {
            "test/pass": self.check("print('fixture result')"),
            "test/fail": self.check("import sys; print('actual failure',file=sys.stderr); sys.exit(7)"),
            "test/missing": {**self.check(""), "argv": ["nonexistent-x-harness-executable-4379"]},
            "test/deferred": {**self.check(""), "argv": None},
        }
        for stage in self.config["stages"].values():
            stage.update(capabilities=[], merge=["test/pass"], close=["test/pass"])
        self.config["high_risk_paths"] = ["protected"]
        self.save_config()
        self.task = json.loads((SOURCE / "docs/templates/task.json").read_text())
        self.task.update(id="TEST-001", title="Verify fixture behavior", status="ready", risk="low", author="implementer",
                         reviewer="reviewer", goal="Verify one committed candidate with separate review and post-merge evidence.",
                         scope=["Local fixture behavior"], non_goals=["Remote actions"], specs=["docs/specs/SPEC-TEST.md"],
                         acceptance=[{"id": "AC-1", "statement": "One visible fixture result with valid version evidence."}],
                         rollback="Revert the fixture change and rerun the registered check.")
        self.save_task()

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def check(source, *, timeout=10, result_format="exit-code"):
        return {"argv": ["{python}", "-c", source], "timeout_seconds": timeout,
                "result_format": result_format, "description": "Deterministic local fixture command."}

    def write(self, relative, value):
        file = self.root / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(canonical(value))

    def save_config(self):
        self.write("harness/policy.json", self.config)

    def save_task(self):
        self.write(f"docs/tasks/{self.task['id']}.json", self.task)

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, message="fixture commit"):
        self.git("add", ".")
        self.git("commit", "-m", message)

    def init_git(self):
        self.git("init", "-b", "main")
        self.git("config", "--local", "user.name", "Harness Fixture")
        self.git("config", "--local", "user.email", "fixture@example.invalid")
        self.git("config", "--local", "commit.gpgsign", "false")
        self.commit("fixture base")
        self.git("checkout", "-b", "feature/fixture")
        (self.root / "product.txt").write_text("A distinct candidate result.\n")
        self.commit("fixture candidate")

    def snapshot(self):
        return revision(self.root, self.task, self.config)

    def report(self, path=".harness/reports/run.json", phase="merge", checks=None):
        return run_checks(self.root, self.task["id"], phase, path, checks)

    def review(self, path=".harness/reports/review.json", **updates):
        record = {"version": 1, "kind": "review", "task_id": self.task["id"], "author": self.task["author"],
                  "reviewer": self.task["reviewer"], "independent": True, "round": 1, "decision": "approved",
                  "revision": self.snapshot(), "criteria": {"AC-1": "PASS"}, "findings": [],
                  "summary": "Fixture review verifies the explicitly registered visible result."}
        record.update(updates)
        self.write(path, record)
        return path

    def merge_gate(self, **kwargs):
        return gate(self.root, self.task["id"], "merge", [".harness/reports/run.json"],
                    kwargs.pop("review", ".harness/reports/review.json"), **kwargs)

    def prepare_merge(self):
        self.init_git()
        self.assertEqual(self.report()["status"], "PASS")
        self.review()

    def assert_blocked(self, result, fragment):
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertIn(fragment, " | ".join(result["reasons"]))

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "harness", "--root", str(self.root), *args],
                              cwd=SOURCE, capture_output=True, text=True, timeout=30)


class ValidationTests(Workspace):
    def test_valid_fixture_lint(self):
        self.assertEqual(lint(self.root)["status"], "PASS")

    def test_local_link_must_exist(self):
        (self.root / "README.md").write_text("[missing](docs/missing.md)")
        with self.assertRaisesRegex(HarnessError, "broken link"):
            lint(self.root)

    def test_missing_and_cyclic_dependencies(self):
        self.task["dependencies"] = ["ABSENT-001"]
        self.save_task()
        with self.assertRaisesRegex(HarnessError, "missing dependency"):
            tasks(self.root, self.config)
        second = copy.deepcopy(self.task)
        second.update(id="TEST-002", dependencies=["TEST-001"])
        self.write("docs/tasks/TEST-002.json", second)
        self.task["dependencies"] = ["TEST-002"]
        self.save_task()
        with self.assertRaisesRegex(HarnessError, "cycle"):
            tasks(self.root, self.config)

    def test_duplicate_task_id_is_rejected(self):
        self.write("docs/tasks/OTHER-001.json", self.task)
        with self.assertRaisesRegex(HarnessError, "filename|duplicate"):
            tasks(self.root, self.config)

    def test_ready_placeholder_and_unresolved_question(self):
        self.task["goal"] = "TODO define goal"
        self.save_task()
        with self.assertRaisesRegex(HarnessError, "placeholders"):
            tasks(self.root, self.config)
        self.task["goal"] = "A complete fixture goal."
        self.task["open_questions"] = [{"id": "Q1", "priority": "P1", "question": "Unresolved product policy"}]
        self.save_task()
        with self.assertRaisesRegex(HarnessError, "P0/P1"):
            tasks(self.root, self.config)

    def test_unknown_fields_and_invalid_types(self):
        for mutate in (lambda t: t.update(approved=True), lambda t: t.update(version=True), lambda t: t.update(checks=[]), lambda t: t.update(stage="release")):
            with self.subTest(mutate=mutate):
                task = copy.deepcopy(self.task)
                mutate(task)
                self.write("docs/tasks/TEST-001.json", task)
                with self.assertRaises(HarnessError):
                    tasks(self.root, self.config)

    def test_wip_one_is_checked(self):
        self.task["status"] = "in_progress"
        self.save_task()
        other = copy.deepcopy(self.task)
        other["id"] = "TEST-002"
        self.write("docs/tasks/TEST-002.json", other)
        with self.assertRaisesRegex(HarnessError, "WIP=1"):
            tasks(self.root, self.config)

    def test_done_requires_substantive_close_evidence(self):
        self.task.update(status="done", closure=".harness/reports/fake-close.json")
        self.write(self.task["closure"], {"kind": "gate", "phase": "close", "status": "PASS", "task_id": self.task["id"]})
        self.save_task()
        with self.assertRaises(HarnessError):
            tasks(self.root, self.config)

    def test_json_duplicate_keys_and_nonfinite_numbers(self):
        file = self.root / "bad.json"
        for data in ('{"id":1,"id":2}', '{"value":NaN}'):
            file.write_text(data)
            with self.assertRaises(HarnessError):
                load_json(self.root, "bad.json")

    def test_paths_and_symlinks_do_not_escape(self):
        for path in ("../escape.json", str(self.root / "absolute.json"), "."):
            with self.subTest(path=path), self.assertRaises(HarnessError):
                safe_path(self.root, path)
        (self.root / "alias").symlink_to(self.root / "docs", target_is_directory=True)
        with self.assertRaisesRegex(HarnessError, "symlink"):
            safe_path(self.root, "alias/tasks/new.json")

    def test_new_output_never_overwrites(self):
        new_json(self.root, "new.json", {"value": 1})
        with self.assertRaisesRegex(HarnessError, "overwrite"):
            new_json(self.root, "new.json", {"value": 2})
        self.assertEqual(load_json(self.root, "new.json"), {"value": 1})

    def test_evidence_requires_json_suffix_for_failure_history(self):
        with self.assertRaisesRegex(HarnessError, "json"):
            new_json(self.root, ".harness/reports/hidden-failure.txt", {"kind": "checks", "status": "FAIL"}, report=True)

    def test_fingerprint_tracks_add_delete_mode_and_ignores_reports(self):
        initial = tree_digest(self.root)
        (self.root / "new.py").write_text("print('new')\n")
        added = tree_digest(self.root)
        self.assertNotEqual(initial, added)
        os.chmod(self.root / "new.py", 0o755)
        self.assertNotEqual(added, tree_digest(self.root))
        (self.root / "new.py").unlink()
        self.assertEqual(initial, tree_digest(self.root))
        new_json(self.root, ".harness/reports/local.json", {"kind": "fixture"}, report=True)
        self.assertEqual(initial, tree_digest(self.root))

    def test_git_status_error_is_unknown_instead_of_clean(self):
        def response(root, *args):
            if args == ("rev-parse", "--show-toplevel"):
                return str(self.root)
            if args == ("rev-parse", "--verify", "HEAD"):
                return "1" * 40
            if args[0] == "rev-parse":
                return "2" * 40
            if args[0] == "symbolic-ref":
                return "feature/fixture"
            return None
        with patch("harness.core.git", side_effect=response):
            self.assertIsNone(self.snapshot()["dirty"])


class ExecutionTests(Workspace):
    def test_success_and_nonzero_store_actual_output(self):
        report = self.report(checks=["test/pass", "test/fail"])
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["checks"][0]["stdout"], "fixture result\n")
        self.assertEqual(report["checks"][1]["returncode"], 7)
        self.assertIn("actual failure", report["checks"][1]["stderr"])

    def test_missing_command_and_tool_are_blocked(self):
        report = self.report(checks=["test/deferred", "test/missing"])
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual([x["reason"] for x in report["checks"]], ["NOT_CONFIGURED", "TOOL_UNAVAILABLE"])

    def test_timeout_is_failed_and_retains_output(self):
        self.config["checks"]["test/slow"] = self.check("import time; print('started',flush=True); time.sleep(30)", timeout=1)
        self.save_config()
        report = self.report(checks=["test/slow"])
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["checks"][0]["reason"], "TIMEOUT")
        self.assertTrue(report["checks"][0]["timed_out"])
        self.assertIn("started", report["checks"][0]["stdout"])

    def test_no_tests_and_all_skipped_are_not_passes(self):
        for total, suffix in ((0, "OK"), (2, "OK (skipped=2)"), (1, "OK (expected failures=1)")):
            with self.subTest(total=total, suffix=suffix):
                text = f"Ran {total} tests in 0.01s\\n\\n{suffix}"
                self.config["checks"]["test/empty"] = self.check(f"import sys; sys.stderr.write('{text}\\n')", result_format="unittest")
                self.save_config()
                report = self.report(path=f".harness/reports/empty-{total}-{len(suffix)}.json", checks=["test/empty"])
                self.assertEqual(report["checks"][0]["status"], "FAIL")

    def test_unittest_counts_reflect_executed_and_skipped(self):
        source = "import sys; sys.stderr.write('Ran 3 tests in 0.01s\\n\\nOK (skipped=1)\\n')"
        self.config["checks"]["test/unit"] = self.check(source, result_format="unittest")
        self.save_config()
        report = self.report(checks=["test/unit"])
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["checks"][0]["test_counts"]["executed"], 2)
        self.assertEqual(report["checks"][0]["test_counts"]["skipped"], 1)

    def test_unittest_summary_without_final_success_is_not_pass(self):
        self.config["checks"]["test/truncated"] = self.check("import sys; sys.stderr.write('Ran 3 tests in 0.01s\\n')", result_format="unittest")
        self.save_config()
        report = self.report(checks=["test/truncated"])
        self.assertEqual(report["status"], "FAIL")

    def test_source_changed_during_check_invalidates_evidence(self):
        self.config["checks"]["test/change"] = self.check("from pathlib import Path; Path('changed.txt').write_text('changed')")
        self.save_config()
        report = self.report(checks=["test/change"])
        self.assertEqual(report["status"], "FAIL")
        self.assertNotEqual(report["revision"], report["finished_revision"])

    def test_existing_output_rejected_before_command_runs(self):
        self.write(".harness/reports/run.json", {"preserved": True})
        self.config["checks"]["test/change"] = self.check("from pathlib import Path; Path('should-not-exist').touch()")
        self.save_config()
        with self.assertRaisesRegex(HarnessError, "overwrite"):
            self.report(checks=["test/change"])
        self.assertFalse((self.root / "should-not-exist").exists())

    def test_task_text_does_not_become_shell_code(self):
        self.task["title"] = "$(touch injected); `touch injected2`"
        self.save_task()
        self.assertEqual(self.report()["status"], "PASS")
        self.assertFalse((self.root / "injected").exists())
        self.assertFalse((self.root / "injected2").exists())


class GateTests(Workspace):
    def test_valid_ready_and_merge_candidate(self):
        self.assertEqual(gate(self.root, "TEST-001", "ready", [])["status"], "PASS")
        self.prepare_merge()
        self.assertEqual(self.merge_gate()["status"], "PASS")

    def test_no_git_cannot_pass_merge(self):
        self.report()
        self.review()
        self.assert_blocked(self.merge_gate(), "Git HEAD/base")

    def test_missing_reports_and_required_checks(self):
        self.prepare_merge()
        self.assert_blocked(gate(self.root, "TEST-001", "merge", [], ".harness/reports/review.json"), "missing check evidence")
        self.config["stages"]["spec"]["merge"].append("test/fail")
        self.save_config()
        self.commit()
        self.report(path=".harness/reports/subset.json", checks=["test/pass"])
        self.review(path=".harness/reports/new-review.json")
        result = gate(self.root, "TEST-001", "merge", [".harness/reports/subset.json"], ".harness/reports/new-review.json")
        self.assert_blocked(result, "missing required checks")

    def test_self_review_and_unverified_criteria(self):
        self.task["reviewer"] = self.task["author"]
        self.save_task()
        self.prepare_merge()
        self.assert_blocked(self.merge_gate(), "self review")
        self.task["reviewer"] = "another-reviewer"
        self.save_task()
        self.commit()
        self.report(path=".harness/reports/new-run.json")
        self.review(criteria={"AC-1": "NOT_RUN"})
        result = gate(self.root, "TEST-001", "merge", [".harness/reports/new-run.json"], ".harness/reports/review.json")
        self.assert_blocked(result, "not all verified")

    def test_blocking_findings_cannot_be_downgraded(self):
        self.prepare_merge()
        finding = {"id": "F1", "priority": "P1", "blocking": False, "status": "open", "summary": "Wrong result", "evidence": "Concrete fixture trigger"}
        self.review(findings=[finding])
        self.assert_blocked(self.merge_gate(), "blocking findings")

    def test_disputed_and_over_limit_reviews_need_owner(self):
        self.prepare_merge()
        self.review(round=4)
        self.assert_blocked(self.merge_gate(), "needs-human")
        self.review(round=3, decision="changes_requested")
        self.assert_blocked(self.merge_gate(), "third round")
        finding = {"id": "F1", "priority": "P2", "blocking": False, "status": "disputed", "summary": "Dispute", "evidence": "Trigger"}
        self.review(findings=[finding])
        self.assert_blocked(self.merge_gate(), "disputed")

    def test_source_task_policy_and_commit_drift(self):
        self.prepare_merge()
        for relative in ("product.txt", "docs/tasks/TEST-001.json", "harness/policy.json"):
            with self.subTest(relative=relative):
                file = self.root / relative
                original = file.read_bytes()
                file.write_bytes(original + b"\n")
                self.assert_blocked(self.merge_gate(), "stale")
                file.write_bytes(original)
        self.git("commit", "--allow-empty", "-m", "new head, same tree")
        self.assert_blocked(self.merge_gate(), "stale")

    def test_base_drift_invalidates_checks_and_review(self):
        self.prepare_merge()
        self.git("checkout", "main")
        self.git("commit", "--allow-empty", "-m", "target advanced")
        self.git("checkout", "feature/fixture")
        self.assert_blocked(self.merge_gate(), "stale")

    def test_command_metadata_and_unknown_report_fields_rejected(self):
        self.prepare_merge()
        file = self.root / ".harness/reports/run.json"
        original = json.loads(file.read_text())
        bad = copy.deepcopy(original)
        bad["checks"][0]["argv"] = ["echo", "fake"]
        self.write(".harness/reports/run.json", bad)
        self.assert_blocked(self.merge_gate(), "command metadata")
        bad = copy.deepcopy(original)
        bad["approved"] = True
        self.write(".harness/reports/run.json", bad)
        self.assert_blocked(self.merge_gate(), "unknown")

    def test_same_version_failure_is_not_erased_by_retry(self):
        self.prepare_merge()
        # Same registered argv, controlled external environment: one failure then success.
        external = Path(self.temporary.name).parent / (self.root.name + "-external-marker")
        source = f"from pathlib import Path; import sys; sys.exit(0 if Path({str(external)!r}).exists() else 9)"
        self.config["checks"]["test/pass"] = self.check(source)
        self.save_config()
        self.commit()
        try:
            self.assertEqual(self.report(path=".harness/reports/failed.json")["status"], "FAIL")
            external.touch()
            self.assertEqual(self.report(path=".harness/reports/retry.json")["status"], "PASS")
            self.review()
            result = gate(self.root, "TEST-001", "merge", [".harness/reports/retry.json"], ".harness/reports/review.json")
            self.assert_blocked(result, "same-version failure")
        finally:
            external.unlink(missing_ok=True)

    def test_policy_minimum_cannot_be_removed_by_task(self):
        self.prepare_merge()
        self.task["checks"]["merge"] = []
        self.assert_blocked(gate(self.root, "TEST-001", "merge", [], ".harness/reports/review.json"), "missing check evidence")

    def test_deferred_dependency_and_draft_spec_block_business(self):
        self.task["stage"] = "backend"
        self.save_task()
        self.config["stages"]["backend"]["capabilities"] = ["database-contract"]
        self.save_config()
        self.assert_blocked(gate(self.root, "TEST-001", "ready", []), "deferred")
        self.config["stages"]["backend"]["capabilities"] = []
        self.save_config()
        path = self.root / "docs/specs/SPEC-TEST.md"
        path.write_text(path.read_text().replace('"accepted"', '"draft"'))
        self.assert_blocked(gate(self.root, "TEST-001", "ready", []), "not accepted")

    def test_changed_protected_path_requires_actual_owner_record(self):
        self.init_git()
        (self.root / "protected").mkdir()
        (self.root / "protected/policy.txt").write_text("a permission change\n")
        self.commit()
        self.report()
        self.review()
        self.assert_blocked(self.merge_gate(), "project-owner decision")
        approval = {"version": 1, "kind": "owner-decision", "task_id": "TEST-001", "revision": self.snapshot(),
                    "decision": "approved", "approver": "fixture-owner", "source": "Synthetic owner decision for this isolated test."}
        self.write(".harness/reports/owner.json", approval)
        self.assertEqual(self.merge_gate(approval=".harness/reports/owner.json")["status"], "PASS")


class LifecycleAndCLITests(Workspace):
    def test_merge_then_new_close_evidence_can_close(self):
        self.prepare_merge()
        result = self.merge_gate(output=".harness/reports/merge-gate.json")
        self.assertEqual(result["status"], "PASS")
        self.git("checkout", "main")
        self.git("merge", "--no-ff", "feature/fixture", "-m", "merge fixture")
        self.report(path=".harness/reports/close-run.json", phase="close")
        self.write(".harness/reports/delivery.json", {"version": 1, "kind": "delivery", "task_id": "TEST-001",
                   "merge_commit": self.git("rev-parse", "HEAD"), "merge_gate": ".harness/reports/merge-gate.json",
                   "strategy": "merge", "source": "Local isolated fixture merge actually executed."})
        result = gate(self.root, "TEST-001", "close", [".harness/reports/close-run.json"],
                      delivery=".harness/reports/delivery.json", output=".harness/reports/closed.json")
        self.assertEqual(result["status"], "PASS", result)
        self.task.update(status="done", closure=".harness/reports/closed.json")
        self.save_task()
        self.assertEqual(tasks(self.root, self.config)["TEST-001"]["status"], "done")

    def test_close_cannot_reuse_pr_report_or_skip_merge_fact(self):
        self.prepare_merge()
        result = gate(self.root, "TEST-001", "close", [".harness/reports/run.json"])
        self.assert_blocked(result, "wrong task or phase")
        self.assertIn("missing delivery", " | ".join(result["reasons"]))

    def test_cli_status_is_read_only_and_reports_capabilities(self):
        before = tree_digest(self.root)
        result = self.cli("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["git"], "not_initialized")
        self.assertEqual(data["checks"]["test/deferred"], "NOT_CONFIGURED")
        self.assertEqual(before, tree_digest(self.root))

    def test_cli_new_task_defaults_planned_and_preserves_existing(self):
        args = ("new-task", "TEST-100", "--title", "New task", "--spec", "docs/specs/SPEC-TEST.md")
        result = self.cli(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        file = self.root / "docs/tasks/TEST-100.json"
        before = file.read_bytes()
        self.assertEqual(json.loads(before)["status"], "planned")
        self.assertNotEqual(self.cli(*args).returncode, 0)
        self.assertEqual(before, file.read_bytes())

    def test_cli_malformed_json_exits_nonzero_without_traceback(self):
        (self.root / "harness/policy.json").write_text('{"version":')
        result = self.cli("lint")
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid JSON", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_cli_run_and_gate_have_distinct_exit_codes(self):
        success = self.cli("run", "--task", "TEST-001", "--output", ".harness/reports/success.json")
        self.assertEqual(success.returncode, 0, success.stderr)
        blocked = self.cli("run", "--task", "TEST-001", "--check", "test/deferred", "--output", ".harness/reports/blocked.json")
        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        failed = self.cli("run", "--task", "TEST-001", "--check", "test/fail", "--output", ".harness/reports/failed.json")
        self.assertEqual(failed.returncode, 1, failed.stderr)


if __name__ == "__main__":
    unittest.main()
