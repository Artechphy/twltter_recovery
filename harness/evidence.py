"""Portable, explicit report bundles. Transport integrity is not approval."""

from __future__ import annotations

import base64
import binascii
from pathlib import Path

from .core import (REPORT_DIR, canonical, exact, input_ref, load_json, new_json,
                   parse_json, report_path, require, sha_bytes, verify_ref)

KINDS = {"gate", "checks", "review", "owner-decision", "delivery"}
MAX_BYTES = 32 * 1024 * 1024


def canonical_report(root: Path, path: str) -> str:
    return report_path(root, path).relative_to(root).as_posix()


def check_links(root: Path, parsed: dict, decoded: dict) -> None:
    for record in parsed.values():
        refs = record.get("inputs", []) if record["kind"] == "gate" else []
        require(isinstance(refs, list), "invalid bundled references")
        for ref in refs:
            exact(ref, {"path", "sha256"}, "bundled reference")
            path = canonical_report(root, ref["path"])
            require(path in decoded and sha_bytes(decoded[path]) == ref["sha256"], "bundle has missing/modified referenced evidence")
        if record["kind"] == "delivery":
            require(canonical_report(root, record["merge_gate"]) in decoded, "bundle has no delivery merge gate")


def references(root: Path, record: dict) -> list[dict]:
    require(isinstance(record, dict) and record.get("kind") in KINDS, "bundle roots/entries must be harness evidence records")
    refs = record.get("inputs", []) if record["kind"] == "gate" else []
    require(isinstance(refs, list), "invalid evidence references")
    for ref in refs:
        exact(ref, {"path", "sha256"}, "evidence reference")
        report_path(root, ref["path"])
    if record["kind"] == "delivery":
        report_path(root, record["merge_gate"])
        refs = [*refs, input_ref(root, record["merge_gate"])]
    return refs


def pack(root: Path, roots: list[str], output: str) -> dict:
    roots = [canonical_report(root, p) for p in roots]
    require(bool(roots) and len(set(roots)) == len(roots), "select distinct evidence roots")
    require(not report_path(root, output).exists(), f"refusing to overwrite: {output}")
    records: dict[str, bytes] = {}
    queue = list(roots)
    versions = set()
    while queue:
        path = canonical_report(root, queue.pop())
        if path in records:
            continue
        file = report_path(root, path)
        record = load_json(root, path)
        for ref in references(root, record):
            verify_ref(root, ref)
            queue.append(ref["path"])
        records[path] = file.read_bytes()
        if record.get("kind") in ("gate", "checks") and "revision" in record:
            versions.add(canonical([record["task_id"], record["phase"], record["revision"]]))
    # A minimal passing input list must not omit a known same-version failure.
    for file in sorted((root / REPORT_DIR).rglob("*.json")):
        path = file.relative_to(root).as_posix()
        record = load_json(root, path)
        if not isinstance(record, dict) or record.get("kind") != "checks":
            continue
        identity = canonical([record.get("task_id"), record.get("phase"), record.get("revision")])
        failed = record.get("status") == "FAIL" or any(c.get("status") == "FAIL" for c in record.get("checks", []) if isinstance(c, dict))
        if identity in versions and failed:
            records[path] = file.read_bytes()
    require(sum(map(len, records.values())) <= MAX_BYTES, "evidence bundle exceeds 32 MiB")
    check_links(root, {p: parse_json(b.decode("utf-8"), p) for p, b in records.items()}, records)
    entries = [{"path": path, "sha256": sha_bytes(data), "data": base64.b64encode(data).decode("ascii")}
               for path, data in sorted(records.items())]
    obj = {"version": 1, "kind": "evidence-bundle", "roots": roots, "entries": entries}
    new_json(root, output, obj, report=True)
    return {"status": "PACKED", "bundle": output, "sha256": sha_bytes(report_path(root, output).read_bytes()),
            "files": len(entries), "gate_result": "NOT_EVALUATED"}


def unpack(root: Path, bundle: str, expected_sha256: str | None = None) -> dict:
    file = report_path(root, bundle)
    require(file.stat().st_size <= MAX_BYTES * 2, "evidence bundle exceeds size limit")
    if expected_sha256 is not None:
        require(sha_bytes(file.read_bytes()) == expected_sha256, "bundle SHA256 mismatch")
    obj = exact(load_json(root, bundle), {"version", "kind", "roots", "entries"}, "evidence bundle")
    require(type(obj["version"]) is int and obj["version"] == 1 and obj["kind"] == "evidence-bundle", "unsupported evidence bundle")
    require(isinstance(obj["roots"], list) and bool(obj["roots"]) and all(isinstance(p, str) for p in obj["roots"])
            and len(set(obj["roots"])) == len(obj["roots"]), "invalid bundle roots")
    require(isinstance(obj["entries"], list) and bool(obj["entries"]), "empty evidence bundle")
    decoded: dict[str, bytes] = {}
    parsed = {}
    for entry in obj["entries"]:
        exact(entry, {"path", "sha256", "data"}, "bundle entry")
        path = entry["path"]
        target = report_path(root, path)
        require(path == target.relative_to(root).as_posix(), "bundle entry path must be canonical")
        require(target != file and path not in decoded, "duplicate/self-referencing bundle path")
        require(isinstance(entry["data"], str), "invalid encoded evidence")
        try:
            data = base64.b64decode(entry["data"], validate=True)
            record = parse_json(data.decode("utf-8"), path)
        except (ValueError, UnicodeError, binascii.Error) as exc:
            raise ValueError(f"invalid evidence encoding: {path}") from exc
        require(sha_bytes(data) == entry["sha256"], f"evidence digest mismatch: {path}")
        require(isinstance(record, dict) and record.get("kind") in KINDS, "invalid evidence record in bundle")
        if target.exists():
            require(target.is_file() and target.read_bytes() == data, f"refusing to overwrite different evidence: {path}")
        decoded[path], parsed[path] = data, record
    require(sum(map(len, decoded.values())) <= MAX_BYTES, "decoded bundle exceeds 32 MiB")
    require(set(obj["roots"]) <= set(decoded), "bundle is missing a root record")
    # Resolve references against the bundle itself before writing anything.
    check_links(root, parsed, decoded)
    written = 0
    for path, data in decoded.items():
        target = report_path(root, path)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
        written += 1
    return {"status": "RESTORED", "files": len(decoded), "written": written, "gate_result": "NOT_EVALUATED"}
