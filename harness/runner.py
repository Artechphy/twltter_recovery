"""Explicit, bounded command execution. No task text is executed as shell code."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import uuid
from pathlib import Path

from .core import (exact, load_json, new_json, now, policy,
                   report_path, require, required_checks, revision, tasks,
                   validate_revision)

CHECK_KEYS = {"check_id", "argv", "timeout_seconds", "result_format", "status", "reason", "returncode", "timed_out", "started_at", "finished_at", "stdout", "stderr", "test_counts"}
REPORT_KEYS = {"version", "kind", "run_id", "task_id", "phase", "status", "revision", "finished_revision", "started_at", "finished_at", "checks"}


def command(check: dict) -> list[str] | None:
    if check["argv"] is None:
        return None
    return [sys.executable if item == "{python}" else item for item in check["argv"]]


def unittest_counts(output: str) -> dict | None:
    matches = list(re.finditer(r"^Ran (\d+) tests? in .+$", output, re.M))
    if len(matches) != 1:
        return None
    total = int(matches[0].group(1))
    tail = output[matches[0].end():]
    counters = {key: int(value) for key, value in re.findall(r"(skipped|failures|errors|expected failures|unexpected successes)=(\d+)", tail)}
    return {"discovered": total, "executed": total - counters.get("skipped", 0),
            "skipped": counters.get("skipped", 0), "failures": counters.get("failures", 0),
            "errors": counters.get("errors", 0), "expected_failures": counters.get("expected failures", 0),
            "unexpected_successes": counters.get("unexpected successes", 0)}


def unittest_passed(output: str, counts: dict | None) -> bool:
    return (counts is not None and counts["executed"] > 0
            and not any(counts[key] for key in ("failures", "errors", "expected_failures", "unexpected_successes"))
            and re.search(r"\nOK(?: \([^\r\n]*\))?\s*\Z", output) is not None)


def run_one(root: Path, name: str, check: dict) -> dict:
    argv = command(check)
    result = {"check_id": name, "argv": argv, "timeout_seconds": check["timeout_seconds"],
              "result_format": check["result_format"], "status": "BLOCKED", "reason": "NOT_CONFIGURED",
              "returncode": None, "timed_out": False, "started_at": now(), "finished_at": None,
              "stdout": "", "stderr": "", "test_counts": None}
    if argv is None:
        result["finished_at"] = now()
        return result
    try:
        process = subprocess.Popen(argv, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace", start_new_session=(os.name == "posix"))
        try:
            stdout, stderr = process.communicate(timeout=check["timeout_seconds"])
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            result["timed_out"] = isinstance(exc, subprocess.TimeoutExpired)
            result["reason"] = "TIMEOUT" if result["timed_out"] else "INTERRUPTED"
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                stdout, stderr = process.communicate()
            result.update(status="FAIL", stdout=stdout, stderr=stderr, returncode=process.returncode)
        else:
            result.update(status="PASS" if process.returncode == 0 else "FAIL", reason="COMPLETED" if process.returncode == 0 else "NONZERO_EXIT",
                          returncode=process.returncode, stdout=stdout, stderr=stderr)
            if check["result_format"] == "unittest":
                combined = stdout + "\n" + stderr
                counts = unittest_counts(combined)
                result["test_counts"] = counts
                if not unittest_passed(combined, counts):
                    result.update(status="FAIL", reason="NO_VALID_TEST_PASS")
    except OSError as exc:
        result.update(reason="TOOL_UNAVAILABLE", stderr=str(exc))
    result["finished_at"] = now()
    return result


def run_checks(root: Path, task_id: str, phase: str, output: str, selected: list[str] | None = None) -> dict:
    file = report_path(root, output)
    require(not file.exists(), f"refusing to overwrite: {output}")
    config = policy(root)
    ledger = tasks(root, config)
    require(task_id in ledger, f"unknown task: {task_id}")
    task = ledger[task_id]
    names = selected if selected is not None else required_checks(task, config, phase)
    require(bool(names) and len(names) == len(set(names)), "select at least one unique check")
    require(set(names) <= set(config["checks"]), "unknown check ID")
    before = revision(root, task, config)
    report = {"version": 1, "kind": "checks", "run_id": uuid.uuid4().hex, "task_id": task_id, "phase": phase,
              "status": "PASS", "revision": before, "finished_revision": None, "started_at": now(), "finished_at": None, "checks": []}
    for name in names:
        result = run_one(root, name, config["checks"][name])
        report["checks"].append(result)
        if result["reason"] == "INTERRUPTED":
            break
    after = revision(root, task, config)
    report["finished_revision"] = after
    if before != after:
        report["status"] = "FAIL"
    elif any(item["status"] == "FAIL" for item in report["checks"]):
        report["status"] = "FAIL"
    elif any(item["status"] == "BLOCKED" for item in report["checks"]):
        report["status"] = "BLOCKED"
    report["finished_at"] = now()
    new_json(root, output, report, report=True)
    return report


def validate_report(root: Path, path: str, task_id: str, phase: str, current: dict, config: dict | None) -> dict:
    report_path(root, path)
    obj = exact(load_json(root, path), REPORT_KEYS, path)
    require(type(obj["version"]) is int and obj["version"] == 1 and obj["kind"] == "checks", f"{path}: not a v1 check report")
    require(isinstance(obj["run_id"], str) and bool(re.fullmatch(r"[0-9a-f]{32}", obj["run_id"])), f"{path}: invalid run ID")
    require(obj["task_id"] == task_id and obj["phase"] == phase, f"{path}: wrong task or phase")
    require(obj["status"] in ("PASS", "FAIL", "BLOCKED"), f"{path}: invalid report status")
    for key in ("started_at", "finished_at"):
        require(isinstance(obj[key], str) and bool(obj[key]), f"{path}: invalid timestamp")
    require(validate_revision(obj["revision"]) == current and validate_revision(obj["finished_revision"]) == current, f"{path}: stale evidence or workspace changed during execution")
    require(isinstance(obj["checks"], list) and bool(obj["checks"]), f"{path}: empty check report")
    ids = []
    for item in obj["checks"]:
        exact(item, CHECK_KEYS, f"{path}.check")
        name = item["check_id"]
        require(isinstance(name, str) and bool(name) and (config is None or name in config["checks"]), f"{path}: unknown check identity")
        ids.append(name)
        require(type(item["timeout_seconds"]) is int and 0 < item["timeout_seconds"] <= 3600, f"{path}: invalid timeout")
        require(item["result_format"] in ("exit-code", "unittest"), f"{path}: invalid result format")
        require(item["argv"] is None or (isinstance(item["argv"], list) and bool(item["argv"]) and all(isinstance(arg, str) and bool(arg.strip()) for arg in item["argv"])), f"{path}: invalid command argv")
        definition = config["checks"][name] if config is not None else item
        require(item["argv"] == command(definition) and item["timeout_seconds"] == definition["timeout_seconds"] and item["result_format"] == definition["result_format"], f"{path}: command metadata does not match registered check {name}")
        require(item["status"] in ("PASS", "FAIL", "BLOCKED") and type(item["timed_out"]) is bool, f"{path}: invalid check status")
        require(item["returncode"] is None or type(item["returncode"]) is int, f"{path}: invalid return code")
        for field in ("started_at", "finished_at", "reason", "stdout", "stderr"):
            require(isinstance(item[field], str), f"{path}: invalid {field}")
        if item["status"] == "PASS":
            require(item["returncode"] == 0 and item["timed_out"] is False and item["reason"] == "COMPLETED" and definition["argv"] is not None, f"{path}: invalid PASS for {name}")
        if definition["result_format"] == "unittest" and definition["argv"] is not None:
            counts = unittest_counts(item["stdout"] + "\n" + item["stderr"])
            require(item["test_counts"] == counts, f"{path}: inconsistent test counts")
            if item["status"] == "PASS":
                require(unittest_passed(item["stdout"] + "\n" + item["stderr"], counts), f"{path}: no actual test pass")
        else:
            require(item["test_counts"] is None, f"{path}: unexpected test counts")
    require(len(ids) == len(set(ids)), f"{path}: duplicate check result")
    expected = "FAIL" if any(item["status"] == "FAIL" for item in obj["checks"]) else "BLOCKED" if any(item["status"] == "BLOCKED" for item in obj["checks"]) else "PASS"
    require(obj["status"] == expected, f"{path}: inconsistent report status")
    return obj
