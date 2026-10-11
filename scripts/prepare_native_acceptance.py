"""Build/verify a source-only bundle. Never execute native tests on this host."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "native_acceptance_manifest.json"
CONFIG_PATH = "config/watchlist.local.json"
TEST_FILES = (
    "tests/test_research_workbench_ops.py",
    "tests/test_z002_run_data_boundaries.py",
    "tests/test_z003_run_identity_recovery.py",
)
REQUIRED_FILES = (
    "AGENTS.md", "requirements.lock.txt", "requirements-optional.txt",
    "scripts/prepare_native_acceptance.py", "docs/NATIVE_ACCEPTANCE_RUNBOOK.md",
    "scripts/start_research_workbench.ps1", "scripts/stop_research_workbench.ps1",
    "scripts/create_research_workbench_shortcuts.ps1",
    "scripts/start_agent.ps1", "scripts/stop_agent.ps1", "scripts/run_once.ps1",
    "scripts/watch.ps1", "scripts/run_daily_research.ps1",
    "scripts/go_live_check.ps1", "scripts/migrate_to_pi_agent.ps1",
    *TEST_FILES,
)
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_FILES = 512


class BundleError(ValueError):
    pass


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode()


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def synthetic_config():
    return {
        "project_name": "SYNTHETIC_NATIVE_ACCEPTANCE_ONLY", "timezone": "Asia/Shanghai",
        "sources": [], "symbols": [], "topics": [],
        "llm_agent": {"enabled": False}, "pi_agent": {"enabled": False},
        "llm_governance": {"enabled": True, "enforce": True},
        "research_operations": {"enforce_roster": True},
        "official_evidence_vault": {"allow_network_fetch": False, "automatic_approval": False},
        "storage": {"sqlite_path": "data/synthetic_native.sqlite3"},
    }


def _safe_name(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name:
        return False
    for part in path.parts:
        if part in {".", ".."} or part.rstrip(" .") != part:
            return False
        if re.fullmatch(r"(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part):
            return False
    return True


def _source_allowed(name):
    return _safe_name(name) and (
        name in REQUIRED_FILES or (
            name.startswith("stock_agent/") and name.endswith(".py")
            and all(not part.startswith(".") and part != "__pycache__"
                    for part in PurePosixPath(name).parts)
        )
    )


def _no_reparse(path):
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise BundleError("reparse point not permitted in bundle source/output")


def _read_source(root, name):
    path = root / name
    _no_reparse(path)
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise BundleError("source is not a regular in-root file: " + name)
    with path.open("rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise BundleError("source exceeds size bound: " + name)
    return data


def _scan_selected(name, data):
    # Reuse existing redacted credential rules, without scanning any local secrets.
    from stock_agent.release_controls import SECRET_PATTERNS, _is_placeholder_secret
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise BundleError("bundle source is not UTF-8: " + name) from exc
    for rule, pattern, group in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            if not _is_placeholder_secret(match.group(group)):
                raise BundleError("credential-like content blocked: " + name + " (" + rule + ")")


def _source_names(root):
    names = set(REQUIRED_FILES)
    pending = [root / "stock_agent"]
    visited = 0
    while pending:
        directory = pending.pop()
        visited += 1
        _no_reparse(directory)
        for child in directory.iterdir():
            if child.name.startswith(".") or child.name == "__pycache__":
                continue
            _no_reparse(child)
            if child.is_dir():
                pending.append(child)
            elif child.suffix == ".py":
                names.add(child.relative_to(root).as_posix())
            if len(names) + len(pending) + visited > MAX_FILES:
                raise BundleError("too many bundle sources")
    return sorted(names)


def prepare_bundle(root: Path, output: Path):
    root, output = Path(root).absolute(), Path(output).absolute()
    _no_reparse(root)
    _no_reparse(output.parent)
    files = {}
    total = 0
    for name in _source_names(root):
        if not _source_allowed(name):
            raise BundleError("source outside allowlist: " + name)
        data = _read_source(root, name)
        _scan_selected(name, data)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise BundleError("bundle exceeds total size bound")
        files[name] = data
    # Deliberately never read config/watchlist.local.json from the real checkout.
    files[CONFIG_PATH] = _json_bytes(synthetic_config())
    manifest = {
        "schema_version": "1.0", "kind": "native_acceptance_source_bundle",
        "status": "PREPARED", "native_execution": "NOT_RUN",
        "requires_disposable_windows_vm": True, "test_files": list(TEST_FILES),
        "files": [{"path": name, "sha256": _hash(data), "size": len(data),
                   "origin": "synthetic_generated" if name == CONFIG_PATH else "source_allowlist"}
                  for name, data in sorted(files.items())],
    }
    files[MANIFEST] = _json_bytes(manifest)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, data)
    payload = buffer.getvalue()
    with output.open("xb") as handle:
        handle.write(payload)
    return {"status": "PREPARED", "native_execution": "NOT_RUN", "path": str(output),
            "sha256": _hash(payload), "files": len(manifest["files"]), "bytes": len(payload)}


def verify_bundle(path: Path, expected_sha256: str):
    """Verify in place without extraction. The digest must arrive separately."""
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        raise BundleError("a separately recorded SHA-256 is required")
    with Path(path).open("rb") as handle:
        payload = handle.read(MAX_TOTAL_BYTES + 1)
    if len(payload) > MAX_TOTAL_BYTES or _hash(payload) != expected_sha256:
        raise BundleError("bundle SHA-256/size mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(entries) > MAX_FILES or len(names) != len(set(n.casefold() for n in names)):
                raise BundleError("duplicate or excessive archive entries")
            if MANIFEST not in names or sum(e.file_size for e in entries) > MAX_TOTAL_BYTES:
                raise BundleError("missing manifest or oversized archive")
            for entry in entries:
                name = entry.filename
                if (not _safe_name(name) or entry.is_dir() or entry.file_size > MAX_FILE_BYTES
                        or stat.S_ISLNK(entry.external_attr >> 16)
                        or (name not in {MANIFEST, CONFIG_PATH} and not _source_allowed(name))):
                    raise BundleError("unsafe or non-allowlisted archive entry")
            manifest = json.loads(archive.read(MANIFEST))
            if (manifest.get("schema_version") != "1.0" or manifest.get("status") != "PREPARED"
                    or manifest.get("native_execution") != "NOT_RUN"
                    or manifest.get("kind") != "native_acceptance_source_bundle"
                    or manifest.get("requires_disposable_windows_vm") is not True
                    or manifest.get("test_files") != list(TEST_FILES)):
                raise BundleError("invalid bundle manifest contract")
            indexed = {}
            for item in manifest["files"]:
                name = item["path"]
                if name in indexed or not _safe_name(name) or name == MANIFEST:
                    raise BundleError("invalid or duplicate manifest file")
                indexed[name] = item
            if set(indexed) != set(names) - {MANIFEST} or not set(REQUIRED_FILES) <= set(indexed):
                raise BundleError("manifest membership mismatch")
            for name, item in indexed.items():
                data = archive.read(name)
                origin = "synthetic_generated" if name == CONFIG_PATH else "source_allowlist"
                if (type(item["size"]) is not int or item["size"] != len(data)
                        or item["sha256"] != _hash(data) or item.get("origin") != origin):
                    raise BundleError("manifest byte/hash mismatch: " + name)
            if archive.read(CONFIG_PATH) != _json_bytes(synthetic_config()):
                raise BundleError("configuration is not the empty synthetic baseline")
    except (zipfile.BadZipFile, json.JSONDecodeError, KeyError, TypeError, AttributeError, UnicodeError) as exc:
        raise BundleError("malformed bundle or manifest") from exc
    return {"status": "VERIFIED", "native_execution": "NOT_RUN", "files": len(indexed),
            "sha256": expected_sha256}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path)
    mode.add_argument("--verify", type=Path)
    parser.add_argument("--sha256", default="")
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT))
    try:
        result = verify_bundle(args.verify, args.sha256) if args.verify else prepare_bundle(ROOT, args.output)
    except (BundleError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
