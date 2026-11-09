"""Explicit GitHub Actions event adapter; no network or approval authority."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re

from .core import HarnessError, exact, git, require

CONTEXT_KEYS = {"provider", "repository", "source_repository", "event", "target_ref",
                "source_head", "base_head", "tested_head", "previous_head", "pull_request"}
PROVENANCE_KEYS = {"provider", "repository", "event", "run_id", "run_attempt"}


def commit(value: object) -> str:
    require(isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{40,64}", value)), "CI requires complete commit SHAs")
    return value


def repository(value: object) -> str:
    require(isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value)), "invalid CI repository")
    return value


def parents(root: Path, sha: str) -> list[str]:
    raw = git(root, "cat-file", "-p", sha)
    require(raw is not None, "CI commit object is unavailable")
    return [line[7:] for line in raw.split("\n\n", 1)[0].splitlines() if line.startswith("parent ")]


def validate_context(value: object) -> dict:
    obj = exact(value, CONTEXT_KEYS, "CI revision context")
    require(obj["provider"] == "github-actions", "unsupported CI provider")
    repository(obj["repository"])
    repository(obj["source_repository"])
    require(obj["event"] in ("pull_request", "push", "workflow_dispatch"), "unsupported CI event")
    require(isinstance(obj["target_ref"], str) and obj["target_ref"].startswith("refs/heads/")
            and ".." not in obj["target_ref"] and bool(re.fullmatch(r"refs/heads/[A-Za-z0-9_./-]+", obj["target_ref"])), "invalid CI target ref")
    for name in ("source_head", "base_head", "tested_head"):
        commit(obj[name])
    if obj["previous_head"] is not None:
        commit(obj["previous_head"])
    if obj["event"] == "pull_request":
        require(type(obj["pull_request"]) is int and obj["pull_request"] > 0 and obj["previous_head"] is None, "invalid PR context")
    else:
        require(obj["pull_request"] is None and obj["source_head"] == obj["tested_head"] == obj["base_head"], "invalid target checkout context")
    return obj


def validate_provenance(value: object, context: dict) -> dict:
    obj = exact(value, PROVENANCE_KEYS, "CI execution provenance")
    require(all(obj[k] == context[k] for k in ("provider", "repository", "event")), "CI provenance does not match revision")
    require(all(type(obj[k]) is int and obj[k] > 0 for k in ("run_id", "run_attempt")), "invalid CI run identity")
    return obj


def github_context(root: Path, base_ref: str, environ: dict | None = None) -> dict:
    env = os.environ if environ is None else environ
    require(env.get("GITHUB_ACTIONS") == "true", "--github-actions requires a GitHub Actions environment")
    workspace = env.get("GITHUB_WORKSPACE")
    require(isinstance(workspace, str) and bool(workspace) and Path(workspace).resolve() == root.resolve()
            and git(root, "rev-parse", "--show-toplevel") == str(root.resolve()), "CI workspace does not match project root")
    repo = repository(env.get("GITHUB_REPOSITORY"))
    origin = git(root, "remote", "get-url", "origin")
    require(origin in (f"https://github.com/{repo}", f"https://github.com/{repo}.git", f"git@github.com:{repo}.git"), "CI origin does not match repository")
    try:
        event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        require(event["repository"]["full_name"] == repo, "event repository mismatch")
        name = env["GITHUB_EVENT_NAME"]
        tested = commit(env["GITHUB_SHA"])
        require(git(root, "rev-parse", "HEAD") == tested, "checkout does not match GITHUB_SHA")
        expected_branch = base_ref.removeprefix("refs/remotes/origin/").removeprefix("refs/heads/").removeprefix("origin/")
        context = {"provider": "github-actions", "repository": repo, "source_repository": repo,
                   "event": name, "target_ref": env["GITHUB_REF"], "source_head": tested,
                   "base_head": tested, "tested_head": tested, "previous_head": None, "pull_request": None}
        if name == "pull_request":
            pr = event["pull_request"]
            base, head = commit(pr["base"]["sha"]), commit(pr["head"]["sha"])
            number = event["number"]
            require(type(number) is int and number > 0 and env["GITHUB_REF"] == f"refs/pull/{number}/merge", "PR must check out its merge ref")
            require(parents(root, tested) == [base, head], "PR checkout has incorrect base/head parents")
            context.update(source_repository=repository(pr["head"]["repo"]["full_name"]),
                           target_ref="refs/heads/" + pr["base"]["ref"], source_head=head,
                           base_head=base, pull_request=number)
            for parent in (base, head):
                require(git(root, "merge-base", "--is-ancestor", parent, tested) is not None, "CI history is incomplete; use fetch-depth: 0")
        elif name == "push":
            require(commit(event["after"]) == tested and event["ref"] == env["GITHUB_REF"], "push event does not describe checkout")
            before = commit(event["before"])
            if set(before) != {"0"}:
                require(git(root, "merge-base", "--is-ancestor", before, tested) is not None, "push history is incomplete or unrelated; use fetch-depth: 0")
                context["previous_head"] = before
        else:
            require(name == "workflow_dispatch", "unsupported GitHub event")
        validate_context(context)
        require(context["target_ref"] == "refs/heads/" + expected_branch, "CI target does not match policy base_ref")
        provenance = {"provider": "github-actions", "repository": repo, "event": name,
                      "run_id": int(env["GITHUB_RUN_ID"]), "run_attempt": int(env["GITHUB_RUN_ATTEMPT"])}
        validate_provenance(provenance, context)
        return {"revision": context, "provenance": provenance}
    except (KeyError, ValueError, TypeError, OSError) as exc:
        raise HarnessError("invalid or incomplete GitHub Actions event data") from exc
