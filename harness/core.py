"""Validation, repository identity and append-only local evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPORT_DIR = ".harness/reports"
STAGES = ("spec", "contract", "backend", "frontend", "integration", "acceptance")
TASK_STATES = ("planned", "ready", "in_progress", "in_review", "blocked", "needs_human", "done")
ACTIVE_STATES = ("in_progress", "in_review")
SPEC_STATES = ("draft", "accepted", "deprecated")
IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")
PLACEHOLDER = re.compile(r"\b(?:TODO|TBD)\b|\{\{.*?\}\}|<[A-Za-z_][A-Za-z0-9_ /.-]*>|待填写|填写此处", re.I)
SPEC_META = re.compile(r"<!--\s*harness-spec:\s*(\{.*?\})\s*-->", re.S)
SPEC_HEADINGS = ("目标", "参与者与权限", "前置条件", "主流程", "异常与边界", "状态与一致性", "验收场景", "未决问题", "非目标")
GENERATED_NAMES = {".git", ".agent-state", "__pycache__", "node_modules", ".venv", ".next", ".pytest_cache", ".DS_Store"}
GENERATED_PREFIXES = (REPORT_DIR, "artifacts/harness-bootstrap", "dist", "build", "target")
TASK_KEYS = {"version", "id", "title", "stage", "status", "risk", "author", "reviewer", "goal", "scope", "non_goals", "specs", "dependencies", "acceptance", "checks", "open_questions", "rollback", "closure"}
REVISION_KEYS = {"head", "base", "base_ref", "branch", "dirty", "tree_sha256", "task_sha256", "policy_sha256"}
GATE_KEYS = {"version", "kind", "task_id", "phase", "status", "created_at", "revision", "reasons", "inputs"}


class HarnessError(Exception):
    """An actionable input error; deliberately not a traceback in the CLI."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise HarnessError(message)


def exact(obj: Any, keys: set[str], label: str) -> dict:
    require(isinstance(obj, dict), f"{label}: expected an object")
    require(set(obj) == keys, f"{label}: missing={sorted(keys-set(obj))}, unknown={sorted(set(obj)-keys)}")
    return obj


def nonempty(value: Any, label: str) -> str:
    require(isinstance(value, str) and bool(value.strip()), f"{label}: expected nonempty text")
    return value


def string_list(value: Any, label: str, *, empty: bool = True) -> list[str]:
    require(isinstance(value, list), f"{label}: expected an array")
    require(empty or bool(value), f"{label}: cannot be empty")
    for item in value:
        nonempty(item, label)
    require(len(value) == len(set(value)), f"{label}: duplicate values")
    return value


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in obj, f"JSON: duplicate key {key!r}")
        obj[key] = value
    return obj


def parse_json(data: str, label: str) -> Any:
    try:
        return json.loads(data, object_pairs_hook=_no_duplicate_keys,
                          parse_constant=lambda value: (_ for _ in ()).throw(HarnessError(f"{label}: invalid number {value}")))
    except (ValueError, UnicodeError) as exc:
        raise HarnessError(f"{label}: invalid JSON: {exc}") from exc


def safe_path(root: Path, relative: Any, *, exists: bool = False) -> Path:
    nonempty(relative, "path")
    path = Path(relative)
    require(not path.is_absolute() and ".." not in path.parts and str(path) != ".", f"path must be relative and inside root: {relative}")
    candidate = root / path
    for part in [candidate, *candidate.parents]:
        if part == root:
            break
        require(not part.is_symlink(), f"symlink is not an allowed input/output: {relative}")
    require(candidate.resolve().is_relative_to(root), f"path escapes root: {relative}")
    if exists:
        require(candidate.is_file(), f"missing regular file: {relative}")
    return candidate


def load_json(root: Path, path: str) -> Any:
    file = safe_path(root, path, exists=True)
    try:
        return parse_json(file.read_text(encoding="utf-8"), path)
    except (OSError, UnicodeError) as exc:
        raise HarnessError(f"{path}: cannot read JSON: {exc}") from exc


def report_path(root: Path, path: str) -> Path:
    file = safe_path(root, path)
    require(file.is_relative_to(root / REPORT_DIR), f"evidence must live under {REPORT_DIR}/")
    require(file.suffix == ".json", "evidence filename must end in .json so failure history remains discoverable")
    return file


def new_json(root: Path, path: str, value: Any, *, report: bool = False) -> None:
    file = report_path(root, path) if report else safe_path(root, path)
    require(not file.exists(), f"refusing to overwrite: {path}")
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("x", encoding="utf-8") as stream:
        stream.write(canonical(value).decode())


def policy(root: Path) -> dict:
    obj = exact(load_json(root, "harness/policy.json"), {"version", "base_ref", "capabilities", "checks", "stages", "high_risk_paths"}, "policy")
    require(type(obj["version"]) is int and obj["version"] == 1, "policy.version: expected 1")
    require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", nonempty(obj["base_ref"], "base_ref"))) and ".." not in obj["base_ref"], "invalid base_ref")
    require(isinstance(obj["capabilities"], dict), "capabilities: expected object")
    for name, capability in obj["capabilities"].items():
        nonempty(name, "capability ID")
        exact(capability, {"status", "reason"}, f"capability {name}")
        require(capability["status"] in ("available", "not_configured", "deferred"), f"capability {name}: invalid status")
        nonempty(capability["reason"], f"capability {name}.reason")
    require(isinstance(obj["checks"], dict) and bool(obj["checks"]), "checks: cannot be empty")
    for name, check in obj["checks"].items():
        require(bool(re.fullmatch(r"[a-z][a-z0-9-]*/[a-z][a-z0-9-]*", name)), f"invalid check ID: {name}")
        exact(check, {"argv", "timeout_seconds", "description", "result_format"}, f"check {name}")
        if check["argv"] is not None:
            require(isinstance(check["argv"], list) and bool(check["argv"]), f"check {name}.argv: expected nonempty array")
            for arg in check["argv"]:
                nonempty(arg, f"check {name}.argv")
        require(type(check["timeout_seconds"]) is int and 0 < check["timeout_seconds"] <= 3600, f"check {name}: invalid timeout")
        require(check["result_format"] in ("exit-code", "unittest"), f"check {name}: invalid result_format")
        nonempty(check["description"], f"check {name}.description")
    exact(obj["stages"], set(STAGES), "stages")
    for stage, settings in obj["stages"].items():
        exact(settings, {"capabilities", "merge", "close"}, stage)
        string_list(settings["capabilities"], f"{stage}.capabilities")
        require(set(settings["capabilities"]) <= set(obj["capabilities"]), f"{stage}: unknown capability")
        for phase in ("merge", "close"):
            string_list(settings[phase], f"{stage}.{phase}", empty=False)
            require(set(settings[phase]) <= set(obj["checks"]), f"{stage}.{phase}: unknown check")
    string_list(obj["high_risk_paths"], "high_risk_paths")
    for path in obj["high_risk_paths"]:
        safe_path(root, path)
    return obj


def validate_task(root: Path, obj: Any, config: dict, label: str) -> dict:
    exact(obj, TASK_KEYS, label)
    require(type(obj["version"]) is int and obj["version"] == 1, f"{label}: version must be 1")
    require(isinstance(obj["id"], str) and bool(IDENTIFIER.fullmatch(obj["id"])), f"{label}: invalid task ID")
    require(obj["stage"] in STAGES, f"{label}: invalid stage")
    require(obj["status"] in TASK_STATES, f"{label}: invalid status")
    require(obj["risk"] in ("low", "medium", "high"), f"{label}: invalid risk")
    for field in ("title", "author", "reviewer", "goal", "rollback"):
        nonempty(obj[field], f"{label}.{field}")
    for field in ("scope", "specs"):
        string_list(obj[field], f"{label}.{field}", empty=False)
    for field in ("non_goals", "dependencies"):
        string_list(obj[field], f"{label}.{field}")
    for spec in obj["specs"]:
        safe_path(root, spec, exists=True)
    require(isinstance(obj["acceptance"], list) and bool(obj["acceptance"]), f"{label}: acceptance cannot be empty")
    ids = []
    for condition in obj["acceptance"]:
        exact(condition, {"id", "statement"}, f"{label}.acceptance")
        ids.append(nonempty(condition["id"], "acceptance.id"))
        nonempty(condition["statement"], "acceptance.statement")
    require(len(ids) == len(set(ids)), f"{label}: duplicate acceptance ID")
    exact(obj["checks"], {"merge", "close"}, f"{label}.checks")
    for phase, checks in obj["checks"].items():
        string_list(checks, f"{label}.checks.{phase}")
        require(set(checks) <= set(config["checks"]), f"{label}: unknown check")
    require(isinstance(obj["open_questions"], list), f"{label}: open_questions must be an array")
    questions = []
    for question in obj["open_questions"]:
        exact(question, {"id", "priority", "question"}, f"{label}.open_questions")
        questions.append(nonempty(question["id"], "question.id"))
        require(question["priority"] in ("P0", "P1", "P2"), f"{label}: invalid question priority")
        nonempty(question["question"], "question.question")
    require(len(questions) == len(set(questions)), f"{label}: duplicate question ID")
    require(obj["closure"] is None or isinstance(obj["closure"], str), f"{label}: invalid closure")
    if obj["status"] == "done":
        require(obj["closure"] is not None, f"{label}: done requires a close gate report")
        report_path(root, obj["closure"])
        closure = exact(load_json(root, obj["closure"]), GATE_KEYS, f"{label}.closure")
        require(type(closure["version"]) is int and closure["version"] == 1 and closure["kind"] == "gate" and closure["phase"] == "close" and closure["status"] == "PASS" and closure["task_id"] == obj["id"] and closure["reasons"] == [], f"{label}: invalid close gate report")
        closed_revision = validate_revision(closure["revision"])
        require(closed_revision["head"] is not None and closed_revision["head"] == closed_revision["base"] and closed_revision["dirty"] is False, f"{label}: close report lacks merged Git identity")
        require(isinstance(closure["inputs"], list) and bool(closure["inputs"]), f"{label}: close report lacks evidence")
        close_checks_found = False
        for ref in closure["inputs"]:
            verify_ref(root, ref)
            record = load_json(root, ref["path"])
            if isinstance(record, dict) and record.get("kind") == "checks" and record.get("phase") == "close":
                # Historical delivery remains valid when later policy/commands evolve.
                from .runner import validate_report
                historical = validate_report(root, ref["path"], obj["id"], "close", closed_revision, None)
                require(historical["status"] == "PASS", f"{label}: close checks did not pass")
                close_checks_found = True
        require(close_checks_found, f"{label}: close report lacks actual merged-version checks")
    if obj["status"] not in ("planned", "blocked"):
        actionable = {key: obj[key] for key in ("title", "author", "reviewer", "goal", "scope", "acceptance", "rollback")}
        require(not PLACEHOLDER.search(json.dumps(actionable, ensure_ascii=False)), f"{label}: ready/active task contains template placeholders")
        require(not any(q["priority"] in ("P0", "P1") for q in obj["open_questions"]), f"{label}: unresolved P0/P1 question in ready/active task")
    return obj


def tasks(root: Path, config: dict) -> dict[str, dict]:
    result = {}
    for file in sorted((root / "docs/tasks").glob("*.json")):
        rel = file.relative_to(root).as_posix()
        task = validate_task(root, load_json(root, rel), config, rel)
        require(task["id"] not in result, f"duplicate task ID: {task['id']}")
        require(file.stem == task["id"], f"{rel}: filename must match task ID")
        result[task["id"]] = task
    for name, task in result.items():
        require(set(task["dependencies"]) <= set(result), f"{name}: missing dependency {set(task['dependencies'])-set(result)}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        require(name not in visiting, f"task dependency cycle at {name}")
        if name in visited:
            return
        visiting.add(name)
        for dependency in result[name]["dependencies"]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in result:
        visit(name)
    require(sum(t["status"] in ACTIVE_STATES for t in result.values()) <= 1, "WIP=1: more than one active implementation/review task")
    return result


def spec_metadata(root: Path, path: str) -> dict:
    text = safe_path(root, path, exists=True).read_text(encoding="utf-8")
    match = SPEC_META.search(text)
    require(match is not None, f"{path}: missing harness-spec metadata")
    obj = exact(parse_json(match.group(1), path), {"id", "status"}, path)
    require(bool(IDENTIFIER.fullmatch(nonempty(obj["id"], "spec ID"))), f"{path}: invalid spec ID")
    require(obj["status"] in SPEC_STATES, f"{path}: invalid spec status")
    for heading in SPEC_HEADINGS:
        section = re.search(r"^## " + re.escape(heading) + r"\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
        require(section is not None and bool(section.group(1).strip()), f"{path}: missing/empty section {heading}")
    if obj["status"] == "accepted":
        require(not PLACEHOLDER.search(text), f"{path}: accepted spec contains placeholders")
    return obj


def lint(root: Path) -> dict:
    config = policy(root)
    ledger = tasks(root, config)
    spec_ids = set()
    specs = list((root / "docs/specs").glob("*.md"))
    require(bool(specs), "docs/specs: at least one behavior spec is required")
    for file in specs:
        meta = spec_metadata(root, file.relative_to(root).as_posix())
        require(meta["id"] not in spec_ids, f"duplicate spec ID: {meta['id']}")
        spec_ids.add(meta["id"])
    for task in ledger.values():
        for path in task["specs"]:
            spec_metadata(root, path)
    documents = [root / "README.md", root / "AGENTS.md", *(root / "docs").rglob("*.md"), *(root / ".github").rglob("*.md")]
    for file in documents:
        require(file.is_file() and not file.is_symlink(), f"missing regular document: {file.relative_to(root)}")
        text = file.read_text(encoding="utf-8")
        for link in re.findall(r"\]\(([^)\s]+)(?:\s+[^)]*)?\)", text):
            if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", link) or link.startswith("#"):
                continue
            target = (file.parent / link.split("#")[0]).resolve()
            require(target.is_relative_to(root), f"{file.relative_to(root)}: link escapes root: {link}")
            require(target.exists(), f"{file.relative_to(root)}: broken link: {link}")
    return {"status": "PASS", "tasks": len(ledger), "specs": len(specs), "documents": len(documents)}


def excluded(path: Path) -> bool:
    rel = path.as_posix()
    return any(part in GENERATED_NAMES for part in path.parts) or any(rel == prefix or rel.startswith(prefix + "/") for prefix in GENERATED_PREFIXES)


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        dirs[:] = sorted(name for name in dirs if not excluded((parent / name).relative_to(root)))
        for name in dirs:
            require(not (parent / name).is_symlink(), f"fingerprint: symlink directory {(parent/name).relative_to(root)}")
        for name in sorted(files):
            file = parent / name
            rel = file.relative_to(root)
            if excluded(rel):
                continue
            mode = file.lstat().st_mode
            require(stat.S_ISREG(mode), f"fingerprint: expected regular file: {rel}")
            digest.update(canonical([rel.as_posix(), bool(mode & 0o111), sha_bytes(file.read_bytes())]))
    return digest.hexdigest()


def git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def revision(root: Path, task: dict, config: dict) -> dict:
    top = git(root, "rev-parse", "--show-toplevel")
    initialized = top is not None and Path(top).resolve() == root
    worktree_status = git(root, "status", "--porcelain", "--untracked-files=all") if initialized else None
    return {
        "head": git(root, "rev-parse", "--verify", "HEAD") if initialized else None,
        "base": git(root, "rev-parse", "--verify", config["base_ref"] + "^{commit}") if initialized else None,
        "base_ref": config["base_ref"],
        "branch": git(root, "symbolic-ref", "--quiet", "--short", "HEAD") if initialized else None,
        "dirty": None if worktree_status is None else bool(worktree_status),
        "tree_sha256": tree_digest(root),
        "task_sha256": sha_bytes(canonical(task)),
        "policy_sha256": sha_bytes(canonical(config)),
    }


def validate_revision(obj: Any) -> dict:
    exact(obj, REVISION_KEYS, "revision")
    for key in ("tree_sha256", "task_sha256", "policy_sha256"):
        require(isinstance(obj[key], str) and bool(re.fullmatch(r"[0-9a-f]{64}", obj[key])), f"revision.{key}: invalid hash")
    for key in ("head", "base"):
        require(obj[key] is None or (isinstance(obj[key], str) and bool(re.fullmatch(r"[0-9a-f]{40,64}", obj[key]))), f"revision.{key}: invalid commit")
    for key in ("base_ref", "branch"):
        require((obj[key] is None and key == "branch") or (isinstance(obj[key], str) and bool(obj[key])), f"revision.{key}: invalid ref")
    require(obj["dirty"] is None or type(obj["dirty"]) is bool, "revision.dirty: invalid state")
    return obj


def required_checks(task: dict, config: dict, phase: str) -> list[str]:
    return sorted(set(config["stages"][task["stage"]][phase]) | set(task["checks"][phase]))


def input_ref(root: Path, path: str) -> dict:
    return {"path": path, "sha256": sha_bytes(safe_path(root, path, exists=True).read_bytes())}


def verify_ref(root: Path, ref: Any) -> None:
    exact(ref, {"path", "sha256"}, "input reference")
    report_path(root, ref["path"])
    require(input_ref(root, ref["path"]) == ref, f"evidence was modified: {ref['path']}")
