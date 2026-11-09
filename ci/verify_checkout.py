"""Check public file scope and the actual GitHub event checkout; never merge."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=15
    )
    if result.returncode:
        raise ValueError(f"Git verification failed: {args[0]}")
    return result.stdout.strip()


def sha(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40,64}", value):
        raise ValueError("Expected a complete commit SHA")
    return value


def verify_event(root: Path, name: str, event: dict, expected_sha: str) -> dict:
    expected = sha(expected_sha)
    if git(root, "rev-parse", "HEAD") != expected:
        raise ValueError("Checkout does not match the event SHA")
    result = {"event": name, "checkout_sha": expected}
    if name == "pull_request":
        base = sha(event["pull_request"]["base"]["sha"])
        head = sha(event["pull_request"]["head"]["sha"])
        parents = git(root, "show", "-s", "--format=%P", "HEAD").split()
        if parents != [base, head]:
            raise ValueError("Checkout is not the expected PR merge candidate")
        git(root, "merge-base", "--is-ancestor", base, expected)
        git(root, "merge-base", "--is-ancestor", head, expected)
        result.update(mode="pr_merge_candidate", base_sha=base, head_sha=head)
    elif name == "push":
        before = sha(event["before"])
        after = sha(event["after"])
        if after != expected or set(before) == {"0"}:
            raise ValueError("Push does not describe an existing target history")
        git(root, "merge-base", "--is-ancestor", before, after)
        result.update(mode="push_ancestry", before_sha=before)
    elif name == "workflow_dispatch":
        result["mode"] = "manual_checkout_only"
    else:
        raise ValueError("Unsupported workflow event")
    return result


def verify_manifest(root: Path) -> int:
    manifest = json.loads((root / "PUBLISH_MANIFEST.json").read_text())
    files = manifest["files"]
    if not isinstance(files, list) or not all(isinstance(p, str) for p in files):
        raise ValueError("Manifest must contain a file list")
    if len(set(files)) != len(files):
        raise ValueError("Manifest contains duplicate paths")
    tracked = set(git(root, "ls-files", "-z").split("\0")) - {""}
    if set(files) != tracked:
        raise ValueError("Tracked files differ from the public publication list")
    for relative in files:
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink() or not path.is_file():
            raise ValueError("Manifest path is not a regular file inside the repository")
    return len(files)


def main() -> int:
    root = Path.cwd()
    try:
        result = verify_event(
            root,
            os.environ["GITHUB_EVENT_NAME"],
            json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()),
            os.environ["GITHUB_SHA"],
        )
        result.update(status="PASS", published_files=verify_manifest(root) if (root / "PUBLISH_MANIFEST.json").exists() else None)
    except (ValueError, KeyError, OSError, TypeError) as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc)}))
        return 1
    output = root / ".harness/reports/github-checkout.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
