"""Real temporary Git histories distinguish merge candidates from wrong checkouts."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from ci.verify_checkout import verify_event, verify_manifest


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="public-ci-git-")
        self.root = Path(self.temp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "CI Fixture")
        self.git("config", "user.email", "ci@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        (self.root / "base").write_text("base")
        self.git("add", "base")
        self.git("commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("switch", "-c", "feature")
        (self.root / "feature").write_text("feature")
        self.git("add", "feature")
        self.git("commit", "-m", "feature")
        self.head = self.git("rev-parse", "HEAD")
        self.git("switch", "main")
        self.git("merge", "--no-ff", "feature", "-m", "merge candidate")
        self.merge = self.git("rev-parse", "HEAD")
        self.event = {"pull_request": {"base": {"sha": self.base}, "head": {"sha": self.head}}}

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.root), *args], check=True,
            capture_output=True, text=True, timeout=15
        ).stdout.strip()

    def test_real_pr_merge_candidate(self):
        self.assertEqual(verify_event(self.root, "pull_request", self.event, self.merge)["mode"], "pr_merge_candidate")

    def test_pr_head_is_not_a_merge_candidate(self):
        self.git("checkout", self.head)
        with self.assertRaisesRegex(ValueError, "merge candidate"):
            verify_event(self.root, "pull_request", self.event, self.head)

    def test_wrong_base_parent_fails(self):
        self.event["pull_request"]["base"]["sha"] = self.head
        with self.assertRaisesRegex(ValueError, "merge candidate"):
            verify_event(self.root, "pull_request", self.event, self.merge)

    def test_wrong_event_checkout_fails(self):
        with self.assertRaisesRegex(ValueError, "event SHA"):
            verify_event(self.root, "pull_request", self.event, self.head)

    def test_push_preserves_prior_history(self):
        event = {"before": self.base, "after": self.merge}
        self.assertEqual(verify_event(self.root, "push", event, self.merge)["mode"], "push_ancestry")

    def test_push_after_and_missing_history_fail(self):
        for event in [{"before": self.base, "after": self.head}, {"before": "0" * 40, "after": self.merge}]:
            with self.subTest(event=event), self.assertRaises(ValueError):
                verify_event(self.root, "push", event, self.merge)

    def test_malformed_sha_and_unsupported_event_fail(self):
        with self.assertRaises(ValueError):
            verify_event(self.root, "pull_request", self.event, "main; echo bad")
        with self.assertRaises(ValueError):
            verify_event(self.root, "untrusted", {}, self.merge)

    def test_manifest_matches_tracked_files(self):
        (self.root / "PUBLISH_MANIFEST.json").write_text(json.dumps({"files": ["base", "feature", "PUBLISH_MANIFEST.json"]}))
        self.git("add", "PUBLISH_MANIFEST.json")
        self.assertEqual(verify_manifest(self.root), 3)

    def test_manifest_rejects_extra_or_missing_files(self):
        (self.root / "PUBLISH_MANIFEST.json").write_text(json.dumps({"files": ["base", "PUBLISH_MANIFEST.json"]}))
        self.git("add", "PUBLISH_MANIFEST.json")
        with self.assertRaisesRegex(ValueError, "publication list"):
            verify_manifest(self.root)


if __name__ == "__main__":
    unittest.main()
