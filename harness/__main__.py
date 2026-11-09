"""Run with python3 -m harness. All network and platform writes are external."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import (HarnessError, IDENTIFIER, STAGES, canonical, git, lint,
                   load_json, new_json, policy, require, revision, tasks,
                   validate_task, evidence_version)
from .gates import gate, ready_problems
from .runner import run_checks


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="X project development harness (Python standard library; local only)")
    cli.add_argument("--root", type=Path, default=Path.cwd(), help="explicit project root (default: cwd)")
    cli.add_argument("--github-actions", action="store_true", help="explicitly validate GitHub event/Git context; never infer approval")
    commands = cli.add_subparsers(dest="command", required=True)
    lint_cmd = commands.add_parser("lint", help="validate policy, task DAG, specs and local Markdown links")
    lint_cmd.add_argument("--structure-only", action="store_true", help="do not audit historical closure evidence; gates still require it")
    commands.add_parser("status", aliases=["doctor"], help="read-only capabilities, stage checks and next work")
    ci_context = commands.add_parser("ci-context", help="verify native GitHub event/Git identity without approving a task")
    ci_context.add_argument("--output", help="optional new JSON under .harness/reports/")
    new = commands.add_parser("new-task", help="create a planned task without overwriting")
    new.add_argument("id")
    new.add_argument("--title", required=True)
    new.add_argument("--stage", choices=STAGES, default="spec")
    new.add_argument("--spec", required=True, help="existing behavior spec relative to root")
    snapshot = commands.add_parser("snapshot", help="print current revision for external review records")
    snapshot.add_argument("--task", required=True)
    review = commands.add_parser("review-template", help="create a NOT_REVIEWED record for a separate reviewer")
    review.add_argument("--task", required=True)
    review.add_argument("--output", required=True)
    run = commands.add_parser("run", help="explicitly execute registered argv checks and save evidence")
    run.add_argument("--task", required=True)
    run.add_argument("--phase", choices=("merge", "close"), default="merge")
    run.add_argument("--check", action="append", dest="checks", help="optional subset; gate still enforces policy minimums")
    run.add_argument("--output", required=True, help="new JSON under .harness/reports/")
    check_gate = commands.add_parser("gate", help="evaluate readiness, merge candidate or delivery; never merge/deploy")
    check_gate.add_argument("--task", required=True)
    check_gate.add_argument("--phase", choices=("ready", "merge", "close"), required=True)
    check_gate.add_argument("--report", action="append", default=[])
    check_gate.add_argument("--review")
    check_gate.add_argument("--approval")
    check_gate.add_argument("--delivery")
    check_gate.add_argument("--output")
    evidence = commands.add_parser("evidence", help="explicitly pack/restore report chains; never approve them")
    operations = evidence.add_subparsers(dest="operation", required=True)
    pack = operations.add_parser("pack")
    pack.add_argument("--report", action="append", required=True)
    pack.add_argument("--output", required=True)
    unpack = operations.add_parser("unpack")
    unpack.add_argument("--bundle", required=True)
    unpack.add_argument("--sha256", help="expected bundle digest from an authorized transport")
    return cli


def execute(args: argparse.Namespace) -> dict:
    root = args.root.resolve()
    require(root.is_dir(), f"root does not exist: {root}")
    if args.command == "evidence":
        from .evidence import pack, unpack
        if args.operation == "pack":
            return pack(root, args.report, args.output)
        return unpack(root, args.bundle, args.sha256)
    if args.command == "lint":
        if args.github_actions:
            from .ci import github_context
            github_context(root, policy(root)["base_ref"])
        return lint(root, structure_only=args.structure_only)
    config = policy(root)
    context = None
    if args.github_actions or args.command == "ci-context":
        from .ci import github_context
        context = github_context(root, config["base_ref"])
    if args.command == "ci-context":
        result = {"status": "VERIFIED", "context": context["revision"], "provenance": context["provenance"],
                  "gate_result": "NOT_EVALUATED"}
        if args.output:
            new_json(root, args.output, result, report=True)
        return result
    ledger = tasks(root, config)
    if args.command in ("status", "doctor"):
        top = git(root, "rev-parse", "--show-toplevel")
        return {"status": "INFO", "scope": "harness-bootstrap; product stages are not verified",
                "git": "initialized" if top and Path(top).resolve() == root else "not_initialized",
                "remote_configured": bool(git(root, "remote")) if top and Path(top).resolve() == root else False,
                "capabilities": config["capabilities"],
                "checks": {name: "configured" if check["argv"] is not None else "NOT_CONFIGURED" for name, check in config["checks"].items()},
                "stages": config["stages"],
                "tasks": [{"id": task["id"], "title": task["title"], "stage": task["stage"], "status": task["status"], "blockers": ready_problems(root, task, ledger, config)} for task in ledger.values()]}
    if args.command == "new-task":
        require(bool(IDENTIFIER.fullmatch(args.id)), "task ID must use uppercase letters/digits and hyphens, such as X-100")
        require(args.id not in ledger, f"task already exists: {args.id}")
        template = load_json(root, "docs/templates/task.json")
        template.update(id=args.id, title=args.title, stage=args.stage, specs=[args.spec])
        validate_task(root, template, config, args.id)
        path = f"docs/tasks/{args.id}.json"
        new_json(root, path, template)
        return {"status": "CREATED", "path": path, "task_status": "planned"}
    if args.command in ("snapshot", "review-template"):
        require(args.task in ledger, f"unknown task: {args.task}")
        task = ledger[args.task]
        current = revision(root, task, config, context)
        if args.command == "snapshot":
            return current
        obj = {"version": evidence_version(current), "kind": "review", "task_id": task["id"], "author": task["author"],
               "reviewer": task["reviewer"], "independent": False, "round": 1, "decision": "not_reviewed",
               "revision": current, "criteria": {item["id"]: "NOT_RUN" for item in task["acceptance"]},
               "findings": [], "summary": "NOT_REVIEWED: separate reviewer must inspect goal, code and evidence."}
        if context is not None:
            obj["provenance"] = context["provenance"]
        new_json(root, args.output, obj, report=True)
        return {"status": "CREATED", "path": args.output, "decision": "not_reviewed"}
    if args.command == "run":
        report = run_checks(root, args.task, args.phase, args.output, args.checks, context)
        return {"status": report["status"], "report": args.output, "workspace_changed": report["revision"] != report["finished_revision"],
                "checks": [{key: check[key] for key in ("check_id", "status", "reason", "returncode", "test_counts")} for check in report["checks"]]}
    return gate(root, args.task, args.phase, args.report, args.review, args.approval, args.delivery, args.output, context)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = execute(args)
        print(canonical(result).decode(), end="")
        return 2 if result.get("status") == "BLOCKED" else 1 if result.get("status") == "FAIL" else 0
    except (HarnessError, OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"status": "FAIL", "error": "INTERRUPTED"}), file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
