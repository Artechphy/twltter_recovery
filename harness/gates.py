"""Three local control points. A passing gate never performs a remote action."""

from __future__ import annotations

from pathlib import Path

from .core import (GATE_KEYS, HarnessError, REPORT_DIR, exact, git, input_ref, load_json,
                   new_json, nonempty, now, policy, report_path, require,
                   required_checks, revision, spec_metadata, tasks,
                   validate_revision, verify_ref, PLACEHOLDER)
from .runner import validate_report

REVIEW_KEYS = {"version", "kind", "task_id", "author", "reviewer", "independent", "round", "decision", "revision", "criteria", "findings", "summary"}


def ready_problems(root: Path, task: dict, ledger: dict, config: dict) -> list[str]:
    reasons = []
    if task["status"] in ("planned", "blocked", "needs_human", "done"):
        reasons.append(f"task status is {task['status']}; ready/active work is required")
    for dependency in task["dependencies"]:
        if ledger[dependency]["status"] != "done":
            reasons.append(f"dependency {dependency} is {ledger[dependency]['status']}")
    for name in config["stages"][task["stage"]]["capabilities"]:
        item = config["capabilities"][name]
        if item["status"] != "available":
            reasons.append(f"capability {name}: {item['status']} — {item['reason']}")
    if task["stage"] != "spec":
        for path in task["specs"]:
            if spec_metadata(root, path)["status"] != "accepted":
                reasons.append(f"behavior spec is not accepted: {path}")
    if any(q["priority"] in ("P0", "P1") for q in task["open_questions"]):
        reasons.append("unresolved P0/P1 questions")
    return reasons


def effective_risk(root: Path, task: dict, config: dict) -> str:
    changed = git(root, "diff", "--name-only", config["base_ref"], "--")
    if changed is not None:
        for path in changed.splitlines():
            if any(path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/") for prefix in config["high_risk_paths"]):
                return "high"
    return task["risk"]


def validate_review(root: Path, path: str, task: dict, current: dict) -> None:
    report_path(root, path)
    obj = exact(load_json(root, path), REVIEW_KEYS, path)
    require(type(obj["version"]) is int and obj["version"] == 1 and obj["kind"] == "review", f"{path}: invalid review format")
    require(obj["task_id"] == task["id"] and validate_revision(obj["revision"]) == current, f"{path}: wrong task or stale review")
    require(obj["author"] == task["author"] and obj["reviewer"] == task["reviewer"], f"{path}: identities do not match task")
    require(task["author"].strip().casefold() != task["reviewer"].strip().casefold() and obj["independent"] is True, f"{path}: independent review is required; self review is not accepted")
    require(type(obj["round"]) is int and 1 <= obj["round"] <= 3, f"{path}: review round exceeds automatic limit; needs-human")
    require(obj["decision"] in ("not_reviewed", "approved", "changes_requested", "needs_human"), f"{path}: invalid decision")
    nonempty(obj["summary"], f"{path}.summary")
    require(not PLACEHOLDER.search(obj["summary"]), f"{path}: review contains template placeholders")
    exact(obj["criteria"], {condition["id"] for condition in task["acceptance"]}, f"{path}.criteria")
    require(all(value in ("PASS", "FAIL", "NOT_RUN") for value in obj["criteria"].values()), f"{path}: invalid criterion status")
    require(isinstance(obj["findings"], list), f"{path}: findings must be an array")
    ids = set()
    blocking = False
    for item in obj["findings"]:
        exact(item, {"id", "priority", "blocking", "status", "summary", "evidence"}, f"{path}.finding")
        name = nonempty(item["id"], "finding.id")
        require(name not in ids, f"{path}: duplicate finding ID")
        ids.add(name)
        require(item["priority"] in ("P0", "P1", "P2", "P3") and type(item["blocking"]) is bool, f"{path}: invalid priority/blocking flag")
        require(item["status"] in ("open", "resolved", "disputed"), f"{path}: invalid finding status")
        nonempty(item["summary"], "finding.summary")
        nonempty(item["evidence"], "finding.evidence")
        if item["status"] == "disputed":
            raise HarnessError(f"{path}: disputed finding {name}; needs-human")
        blocking |= item["status"] != "resolved" and (item["blocking"] or item["priority"] in ("P0", "P1"))
    require(obj["decision"] == "approved", f"{path}: {obj['decision']}" + ("; needs-human (third round)" if obj["round"] == 3 else ""))
    require(not blocking, f"{path}: unresolved blocking findings")
    require(all(value == "PASS" for value in obj["criteria"].values()), f"{path}: acceptance criteria were not all verified")


def validate_approval(root: Path, path: str, task: dict, current: dict) -> None:
    report_path(root, path)
    obj = exact(load_json(root, path), {"version", "kind", "task_id", "revision", "decision", "approver", "source"}, path)
    require(type(obj["version"]) is int and obj["version"] == 1 and obj["kind"] == "owner-decision", f"{path}: invalid owner decision")
    require(obj["task_id"] == task["id"] and validate_revision(obj["revision"]) == current, f"{path}: wrong task or stale owner decision")
    require(obj["decision"] == "approved", f"{path}: owner has not approved")
    actor = nonempty(obj["approver"], "approver").strip().casefold()
    require(actor not in (task["author"].strip().casefold(), task["reviewer"].strip().casefold()), f"{path}: owner decision must come from the project owner")
    require(not PLACEHOLDER.search(nonempty(obj["source"], "decision source")), f"{path}: missing actual decision source")


def collect_checks(root: Path, paths: list[str], task: dict, phase: str, current: dict, config: dict) -> None:
    require(bool(paths), "missing check evidence")
    require(len(paths) == len(set(paths)), "duplicate check report paths")
    results = {}
    for path in paths:
        report = validate_report(root, path, task["id"], phase, current, config)
        require(report["status"] == "PASS", f"{path}: checks are {report['status']}")
        for check in report["checks"]:
            name = check["check_id"]
            require(name not in results, f"ambiguous duplicate result for {name}")
            results[name] = check
    needed = set(required_checks(task, config, phase))
    require(needed <= set(results), f"missing required checks: {sorted(needed-set(results))}")
    # Keep failed attempts visible. A retry of the same bytes must not erase failure.
    for file in sorted((root / REPORT_DIR).rglob("*.json")):
        rel = file.relative_to(root).as_posix()
        old = load_json(root, rel)
        if not isinstance(old, dict) or old.get("kind") != "checks":
            continue
        if old.get("task_id") != task["id"] or old.get("phase") != phase or old.get("revision") != current:
            continue
        for check in old.get("checks", []):
            if isinstance(check, dict) and check.get("check_id") in needed and check.get("status") == "FAIL":
                raise HarnessError(f"same-version failure for {check['check_id']} in {rel}; retry success cannot erase it")


def validate_delivery(root: Path, path: str, task: dict, current: dict) -> list[dict]:
    report_path(root, path)
    delivery = exact(load_json(root, path), {"version", "kind", "task_id", "merge_commit", "merge_gate", "strategy", "source"}, path)
    require(type(delivery["version"]) is int and delivery["version"] == 1 and delivery["kind"] == "delivery", f"{path}: invalid delivery record")
    require(delivery["task_id"] == task["id"] and delivery["merge_commit"] == current["head"], f"{path}: delivery does not describe current merged commit")
    nonempty(delivery["source"], "merge fact source")
    require(not PLACEHOLDER.search(delivery["source"]), "merge fact source is a template placeholder")
    require(delivery["strategy"] in ("merge", "fast-forward"), "automatic close only verifies merge/fast-forward ancestry; squash/rebase requires a future platform attestation adapter")
    report_path(root, delivery["merge_gate"])
    accepted = exact(load_json(root, delivery["merge_gate"]), GATE_KEYS, "accepted merge gate")
    require(type(accepted["version"]) is int and accepted["version"] == 1 and accepted["kind"] == "gate" and accepted["task_id"] == task["id"] and accepted["phase"] == "merge" and accepted["status"] == "PASS" and accepted["reasons"] == [], "delivery requires a saved passing merge gate for this task")
    old = validate_revision(accepted["revision"])
    require(old["head"] is not None and old["head"] != old["base"] and old["dirty"] is False, "accepted merge gate was not bound to a committed PR candidate")
    require(isinstance(accepted["inputs"], list) and bool(accepted["inputs"]), "accepted merge gate has no input evidence")
    for ref in accepted["inputs"]:
        verify_ref(root, ref)
    require(git(root, "merge-base", "--is-ancestor", old["head"], current["head"]) is not None, "reviewed candidate is not an ancestor of merged commit")
    require(current["head"] == current["base"] and current["branch"] == current["base_ref"].rsplit("/", 1)[-1], "close must run on the configured target branch at the actual merged commit")
    return [input_ref(root, delivery["merge_gate"]), *accepted["inputs"]]


def gate(root: Path, task_id: str, phase: str, reports: list[str], review: str | None = None,
         approval: str | None = None, delivery: str | None = None, output: str | None = None) -> dict:
    if output:
        require(not report_path(root, output).exists(), f"refusing to overwrite: {output}")
    config = policy(root)
    ledger = tasks(root, config)
    require(task_id in ledger, f"unknown task: {task_id}")
    task = ledger[task_id]
    current = revision(root, task, config)
    reasons = ready_problems(root, task, ledger, config)
    inputs = []
    if phase != "ready":
        if current["head"] is None or current["base"] is None:
            reasons.append("Git HEAD/base is unavailable; local checks cannot prove merge or delivery")
        if current["dirty"] is not False:
            reasons.append("merge/close requires a clean committed worktree")
        try:
            collect_checks(root, reports, task, phase, current, config)
            inputs.extend(input_ref(root, path) for path in reports)
        except HarnessError as exc:
            reasons.append(str(exc))
        if phase == "merge":
            if current["head"] is not None and current["head"] == current["base"]:
                reasons.append("candidate equals base; no separate PR candidate")
            try:
                require(review is not None, "missing independent review")
                validate_review(root, review, task, current)
                inputs.append(input_ref(root, review))
            except HarnessError as exc:
                reasons.append(str(exc))
            if effective_risk(root, task, config) in ("medium", "high"):
                try:
                    require(approval is not None, "medium/high risk requires an actual project-owner decision")
                    validate_approval(root, approval, task, current)
                    inputs.append(input_ref(root, approval))
                except HarnessError as exc:
                    reasons.append(str(exc))
        else:
            try:
                require(delivery is not None, "missing delivery record and merged-version verification")
                inputs.extend(validate_delivery(root, delivery, task, current))
                inputs.append(input_ref(root, delivery))
            except HarnessError as exc:
                reasons.append(str(exc))
    result = {"version": 1, "kind": "gate", "task_id": task_id, "phase": phase,
              "status": "BLOCKED" if reasons else "PASS", "created_at": now(),
              "revision": current, "reasons": reasons, "inputs": inputs}
    if output:
        new_json(root, output, result, report=True)
    return result
