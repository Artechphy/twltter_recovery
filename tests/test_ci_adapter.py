"""Synthetic approvals below exercise contracts; they approve no real task."""

from __future__ import annotations

import base64
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

from test_harness import Workspace
from harness.ci import github_context
from harness.core import HarnessError, canonical, lint, revision, tasks
from harness.evidence import pack, unpack
from harness.gates import gate
from harness.runner import run_checks, validate_report


class CIWorkspace(Workspace):
    def setUp(self):
        super().setUp()
        self.config["checks"]["test/pass"] = self.check(
            "import os,sys; print('synthetic fixture check'); sys.exit(int(os.environ.get('HX_FIXTURE_FAIL','0')))"
        )
        self.save_config()

    def prepare_pr(self):
        self.init_git()
        self.base = self.git("rev-parse", "main")
        (self.root / "protected").mkdir()
        (self.root / "protected/rule.txt").write_text("A reviewed protected-path change.\n")
        self.commit("protected fixture change")
        self.source = self.git("rev-parse", "HEAD")
        self.git("remote", "add", "origin", "https://github.com/example/harness-ci.git")
        self.git("checkout", "--detach", self.base)
        self.git("merge", "--no-ff", "feature/fixture", "-m", "synthetic PR merge candidate")
        self.tested = self.git("rev-parse", "HEAD")
        self.git("branch", "-D", "main")
        self.event = {"repository": {"full_name": "example/harness-ci"}, "number": 7,
                      "pull_request": {"base": {"ref": "main", "sha": self.base},
                                       "head": {"sha": self.source, "repo": {"full_name": "example/harness-ci"}}}}
        self.env = {"GITHUB_ACTIONS": "true", "GITHUB_WORKSPACE": str(self.root),
                    "GITHUB_REPOSITORY": "example/harness-ci", "GITHUB_EVENT_NAME": "pull_request",
                    "GITHUB_REF": "refs/pull/7/merge", "GITHUB_SHA": self.tested,
                    "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_EVENT_PATH": str(self.root / ".harness/reports/event.json")}
        self.write(".harness/reports/event.json", self.event)
        self.context = github_context(self.root, "main", self.env)

    def ci_review(self, context=None):
        context = context or self.context
        current = revision(self.root, self.task, self.config, context)
        self.write(".harness/reports/review.json", {
            "version": 2, "kind": "review", "task_id": self.task["id"], "author": self.task["author"],
            "reviewer": self.task["reviewer"], "independent": True, "round": 1, "decision": "approved",
            "revision": current, "provenance": context["provenance"], "criteria": {"AC-1": "PASS"},
            "findings": [], "summary": "Synthetic contract fixture review; no real project approval."
        })
        self.write(".harness/reports/owner.json", {
            "version": 2, "kind": "owner-decision", "task_id": self.task["id"], "revision": current,
            "provenance": context["provenance"], "decision": "approved", "approver": "fixture-owner",
            "source": "Synthetic contract fixture decision; not a real project-owner approval."
        })

    def prepared_merge(self):
        self.prepare_pr()
        self.assertEqual(run_checks(self.root, self.task["id"], "merge", ".harness/reports/pr.json", context=self.context)["status"], "PASS")
        self.ci_review()
        result = gate(self.root, self.task["id"], "merge", [".harness/reports/pr.json"],
                      ".harness/reports/review.json", ".harness/reports/owner.json",
                      output=".harness/reports/merge.json", context=self.context)
        self.assertEqual(result["status"], "PASS", result)

    def actual_merge(self):
        self.git("checkout", "-b", "main", self.base)
        self.git("merge", "--no-ff", "feature/fixture", "-m", "actual fixture merge after CI")
        self.merged = self.git("rev-parse", "HEAD")
        self.assertNotEqual(self.merged, self.tested)
        self.env.update(GITHUB_EVENT_NAME="push", GITHUB_REF="refs/heads/main", GITHUB_SHA=self.merged,
                        GITHUB_RUN_ID="43")
        self.write(".harness/reports/event.json", {"repository": {"full_name": "example/harness-ci"},
                   "ref": "refs/heads/main", "before": self.base, "after": self.merged})
        self.git("checkout", "--detach", self.merged)
        self.git("branch", "-D", "main")
        self.push_context = github_context(self.root, "main", self.env)

    def close(self, context=None):
        context = context or self.push_context
        self.assertEqual(run_checks(self.root, self.task["id"], "close", ".harness/reports/close.json", context=context)["status"], "PASS")
        self.write(".harness/reports/delivery.json", {"version": 1, "kind": "delivery", "task_id": self.task["id"],
                   "merge_commit": self.git("rev-parse", "HEAD"), "merge_gate": ".harness/reports/merge.json",
                   "strategy": "merge", "source": "Actual Git merge in a synthetic contract fixture."})
        return gate(self.root, self.task["id"], "close", [".harness/reports/close.json"],
                    delivery=".harness/reports/delivery.json", output=".harness/reports/closed.json", context=context)


class ActionsIdentityTests(CIWorkspace):
    def test_detached_pr_roles_and_cli_snapshot(self):
        self.prepare_pr()
        self.assertIsNone(revision(self.root, self.task, self.config)["base"])
        current = revision(self.root, self.task, self.config, self.context)
        self.assertIsNone(current["branch"])
        self.assertEqual(current["base"], self.base)
        self.assertEqual(current["head"], self.tested)
        self.assertEqual(current["context"]["source_head"], self.source)
        result = subprocess.run([sys.executable, "-m", "harness", "--github-actions", "snapshot", "--task", self.task["id"]],
                                cwd=self.root, env={**os.environ, **self.env}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), current)

    def test_mismatched_workspace_repository_checkout_and_target_rejected(self):
        self.prepare_pr()
        for key, value in [("GITHUB_WORKSPACE", str(self.root.parent)), ("GITHUB_REPOSITORY", "other/repo"),
                           ("GITHUB_SHA", self.source), ("GITHUB_REF", "refs/heads/main"), ("GITHUB_WORKSPACE", "")]:
            with self.subTest(key=key), self.assertRaises(HarnessError):
                github_context(self.root, "main", {**self.env, key: value})
        with self.assertRaisesRegex(HarnessError, "target"):
            github_context(self.root, "release", self.env)

    def test_wrong_parents_and_incomplete_history_are_rejected(self):
        self.prepare_pr()
        event = copy.deepcopy(self.event)
        event["pull_request"]["base"]["sha"] = self.source
        self.write(".harness/reports/event.json", event)
        with self.assertRaisesRegex(HarnessError, "parents"):
            github_context(self.root, "main", self.env)
        self.write(".harness/reports/event.json", self.event)
        (self.root / ".git/shallow").write_text(self.tested + "\n")
        with self.assertRaisesRegex(HarnessError, "fetch-depth"):
            github_context(self.root, "main", self.env)

    def test_environment_is_opt_in(self):
        self.prepare_pr()
        with patch.dict(os.environ, self.env):
            self.assertNotIn("context", revision(self.root, self.task, self.config))
        with self.assertRaises(HarnessError):
            github_context(self.root, "main", {})


class ActionsGateTests(CIWorkspace):
    def test_ci_python_path_is_portable_but_registered_arguments_are_not(self):
        self.prepare_pr()
        report = run_checks(self.root, self.task["id"], "merge", ".harness/reports/pr.json", context=self.context)
        current = revision(self.root, self.task, self.config, self.context)
        with patch("harness.runner.sys.executable", "/different-host/bin/python3"):
            self.assertEqual(validate_report(self.root, ".harness/reports/pr.json", self.task["id"], "merge", current, self.config)["status"], "PASS")
        report["checks"][0]["argv"][-1] = "print('unregistered command')"
        self.write(".harness/reports/altered.json", report)
        with self.assertRaisesRegex(HarnessError, "command metadata"):
            validate_report(self.root, ".harness/reports/altered.json", self.task["id"], "merge", current, self.config)

    def test_real_core_merge_close_and_v2_history(self):
        self.prepared_merge()
        self.actual_merge()
        result = self.close()
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["version"], 2)
        self.assertIsNone(result["revision"]["branch"])
        self.task.update(status="done", closure=".harness/reports/closed.json")
        self.save_task()
        self.assertEqual(lint(self.root)["status"], "PASS")

    def test_missing_review_and_protected_path_owner_still_block(self):
        self.prepare_pr()
        run_checks(self.root, self.task["id"], "merge", ".harness/reports/pr.json", context=self.context)
        self.ci_review()
        missing_review = gate(self.root, self.task["id"], "merge", [".harness/reports/pr.json"], context=self.context)
        self.assert_blocked(missing_review, "missing independent review")
        missing_owner = gate(self.root, self.task["id"], "merge", [".harness/reports/pr.json"], ".harness/reports/review.json", context=self.context)
        self.assert_blocked(missing_owner, "actual project-owner decision")

    def test_new_run_attempt_does_not_erase_same_version_failure(self):
        self.prepare_pr()
        with patch.dict(os.environ, {"HX_FIXTURE_FAIL": "1"}):
            failed = run_checks(self.root, self.task["id"], "merge", ".harness/reports/fail.json", context=self.context)
        self.assertEqual(failed["status"], "FAIL")
        context = copy.deepcopy(self.context)
        context["provenance"].update(run_id=99, run_attempt=2)
        good = run_checks(self.root, self.task["id"], "merge", ".harness/reports/pass.json", context=context)
        self.assertEqual(good["status"], "PASS")
        self.assertEqual(failed["revision"], good["revision"])
        self.ci_review(context)
        result = gate(self.root, self.task["id"], "merge", [".harness/reports/pass.json"],
                      ".harness/reports/review.json", ".harness/reports/owner.json", context=context)
        self.assert_blocked(result, "same-version failure")

    def test_pr_run_cannot_close_task(self):
        self.prepared_merge()
        self.assert_blocked(self.close(self.context), "actual target push")

    def test_post_merge_unreviewed_content_is_rejected(self):
        self.prepared_merge()
        self.actual_merge()
        (self.root / "extra.txt").write_text("An unreviewed post-merge edit.")
        self.commit("unreviewed extra change")
        self.env["GITHUB_SHA"] = self.git("rev-parse", "HEAD")
        event = {"repository": {"full_name": "example/harness-ci"}, "ref": "refs/heads/main",
                 "before": self.base, "after": self.env["GITHUB_SHA"]}
        self.write(".harness/reports/event.json", event)
        context = github_context(self.root, "main", self.env)
        self.assert_blocked(self.close(context), "content differs")


class EvidenceTransportTests(CIWorkspace):
    def v1_closed(self):
        self.prepare_merge()
        result = self.merge_gate(output=".harness/reports/merge.json")
        self.assertEqual(result["status"], "PASS", result)
        self.git("checkout", "main")
        self.git("merge", "--ff-only", "feature/fixture")
        run_checks(self.root, self.task["id"], "close", ".harness/reports/close.json")
        self.write(".harness/reports/delivery.json", {"version": 1, "kind": "delivery", "task_id": self.task["id"],
                   "merge_commit": self.git("rev-parse", "HEAD"), "merge_gate": ".harness/reports/merge.json",
                   "strategy": "fast-forward", "source": "Actual local Git fast-forward in synthetic fixture."})
        result = gate(self.root, self.task["id"], "close", [".harness/reports/close.json"],
                      delivery=".harness/reports/delivery.json", output=".harness/reports/closed.json")
        self.assertEqual(result["status"], "PASS", result)
        self.task.update(status="done", closure=".harness/reports/closed.json")
        self.save_task()

    def clean_copy(self, parent):
        dest = (Path(parent) / "workspace").resolve()
        shutil.copytree(self.root, dest, ignore=shutil.ignore_patterns(".git", ".harness", "__pycache__"))
        (dest / ".harness/reports").mkdir(parents=True)
        shutil.copyfile(self.root / ".harness/reports/bundle.json", dest / ".harness/reports/incoming.json")
        return dest

    def test_v1_bundle_restores_before_cli_loads_done_tasks(self):
        self.v1_closed()
        result = pack(self.root, [".harness/reports/closed.json"], ".harness/reports/bundle.json")
        self.assertEqual(result["gate_result"], "NOT_EVALUATED")
        with tempfile.TemporaryDirectory() as parent:
            dest = self.clean_copy(parent)
            (dest / "README.md").write_text("[closure](.harness/reports/closed.json)\n")
            with self.assertRaisesRegex(HarnessError, "missing regular file"):
                lint(dest)
            structure = lint(dest, structure_only=True)
            self.assertEqual(structure["closure_evidence"], "NOT_CHECKED")
            self.assertEqual(structure["scope"], "structure-only")
            restore = subprocess.run([sys.executable, "-m", "harness", "--root", str(dest), "evidence", "unpack", "--bundle", ".harness/reports/incoming.json", "--sha256", result["sha256"]],
                                     cwd=self.root, capture_output=True, text=True)
            self.assertEqual(restore.returncode, 0, restore.stderr)
            self.assertEqual(lint(dest)["status"], "PASS")
            self.assertEqual(unpack(dest, ".harness/reports/incoming.json")["written"], 0)

    def test_v2_close_chain_roundtrip(self):
        self.prepared_merge()
        self.actual_merge()
        self.assertEqual(self.close()["status"], "PASS")
        self.task.update(status="done", closure=".harness/reports/closed.json")
        self.save_task()
        pack(self.root, [".harness/reports/closed.json"], ".harness/reports/bundle.json")
        with tempfile.TemporaryDirectory() as parent:
            dest = self.clean_copy(parent)
            unpack(dest, ".harness/reports/incoming.json")
            self.assertEqual(lint(dest)["status"], "PASS")

    def test_legacy_relative_reference_spelling_preserves_raw_bytes(self):
        self.v1_closed()
        path = self.root / self.task["closure"]
        record = json.loads(path.read_text())
        for ref in record["inputs"]:
            ref["path"] = "./" + ref["path"]
        path.write_bytes(canonical(record))
        original = path.read_bytes()
        pack(self.root, ["./" + self.task["closure"]], ".harness/reports/bundle.json")
        with tempfile.TemporaryDirectory() as parent:
            dest = self.clean_copy(parent)
            unpack(dest, ".harness/reports/incoming.json")
            self.assertEqual((dest / self.task["closure"]).read_bytes(), original)
            self.assertEqual(lint(dest)["status"], "PASS")

    def test_tampered_digest_missing_reference_and_unsafe_path_rejected(self):
        self.v1_closed()
        pack(self.root, [".harness/reports/closed.json"], ".harness/reports/bundle.json")
        original = json.loads((self.root / ".harness/reports/bundle.json").read_text())
        variants = []
        bad = copy.deepcopy(original); bad["entries"][0]["data"] = base64.b64encode(b'{}').decode(); variants.append(bad)
        bad = copy.deepcopy(original); bad["entries"][0]["path"] = "../escape.json"; variants.append(bad)
        bad = copy.deepcopy(original); bad["entries"][0]["path"] = ".harness/reports//alias.json"; variants.append(bad)
        bad = copy.deepcopy(original); bad["entries"].append(bad["entries"][0]); variants.append(bad)
        bad = copy.deepcopy(original); bad["entries"] = [e for e in bad["entries"] if e["path"] != ".harness/reports/close.json"]; variants.append(bad)
        for i, bad in enumerate(variants):
            with self.subTest(case=i), tempfile.TemporaryDirectory() as parent:
                dest = self.clean_copy(parent)
                (dest / ".harness/reports/incoming.json").write_bytes(canonical(bad))
                with self.assertRaises(HarnessError):
                    unpack(dest, ".harness/reports/incoming.json")
                self.assertFalse((dest / ".harness/reports/closed.json").exists())

    def test_conflict_and_wrong_transport_digest_preflight(self):
        self.v1_closed()
        pack(self.root, [".harness/reports/closed.json"], ".harness/reports/bundle.json")
        with tempfile.TemporaryDirectory() as parent:
            dest = self.clean_copy(parent)
            with self.assertRaisesRegex(HarnessError, "bundle SHA256"):
                unpack(dest, ".harness/reports/incoming.json", "0" * 64)
            conflict = dest / ".harness/reports/review.json"; conflict.write_text('{"existing":"different"}')
            with self.assertRaisesRegex(HarnessError, "overwrite different"):
                unpack(dest, ".harness/reports/incoming.json")
            self.assertFalse((dest / ".harness/reports/closed.json").exists())
            self.assertEqual(conflict.read_text(), '{"existing":"different"}')

    def test_structure_mode_never_changes_gate_audit(self):
        self.v1_closed()
        (self.root / self.task["closure"]).unlink()
        self.assertEqual(lint(self.root, structure_only=True)["status"], "PASS")
        with self.assertRaisesRegex(HarnessError, "missing regular file"):
            tasks(self.root, self.config)
        with self.assertRaises(HarnessError):
            gate(self.root, self.task["id"], "ready", [])

    def test_pack_preserves_known_same_version_failures(self):
        self.prepare_merge()
        self.assertEqual(self.merge_gate(output=".harness/reports/merge.json")["status"], "PASS")
        with patch.dict(os.environ, {"HX_FIXTURE_FAIL": "1"}):
            self.assertEqual(self.report(".harness/reports/fail.json")["status"], "FAIL")
        result = pack(self.root, [".harness/reports/merge.json"], ".harness/reports/bundle.json")
        bundle = json.loads((self.root / result["bundle"]).read_text())
        self.assertIn(".harness/reports/fail.json", [e["path"] for e in bundle["entries"]])
