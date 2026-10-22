"""R08: image provenance — build once, verify forever, never self-reference.

The release-audit Git snapshot cannot exist inside a container image (the
image deliberately ships without ``.git``). This module defines the
replacement chain WITHOUT ever auto-passing the missing-Git case
(GLM_REFACTOR_TASKBOOK R08 stop condition):

    build side (outside the image)
        PROVENANCE.json  = {source commit, dirty flag, stock_agent/ tree
                            sha256, requirements locks sha256, pi package-lock
                            sha256, schema_version, generated_at}
        image binding    = {image digest, provenance_sha256}   <-- EXTERNAL:
                            the digest only exists after the build, so it is
                            written NEXT TO the image, never inside it (the
                            recursive self-digest trap).

    runtime side (inside the image, read-only)
        verify_image_provenance() reads the shipped PROVENANCE.json, the
        externally-injected expected provenance hash, and the observed image
        digest; the chain passes only when all three agree. A missing .git
        NEVER passes on its own — without the binding it is BLOCKED.

Every step is pure computation over passed-in values: no network, no docker
socket, no writes.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

PROVENANCE_SCHEMA_VERSION = "1.0"
PROVENANCE_FILENAME = "PROVENANCE.json"
BINDING_FILENAME = "image_binding.json"

_PROVENANCE_FIELDS = {
    "schema_version", "source_commit", "source_dirty", "stock_agent_tree_sha256",
    "python_locks", "pi_package_lock_sha256", "generated_at",
}


class ProvenanceError(ValueError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path, patterns: Sequence[str]) -> str:
    """Deterministic content hash of a source tree: relative POSIX path +
    file sha256 per file, sorted. Skips __pycache__/pyc; never follows the
    patterns outside the root."""
    entries: List[str] = []
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            try:
                path.resolve().relative_to(root.resolve())
            except ValueError:
                continue
            entries.append("{0}:{1}".format(path.relative_to(root).as_posix(),
                                            _sha256_file(path)))
    combined = "\n".join(sorted(entries))
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def build_provenance(source_root: Path, *, source_commit: str = "",
                     source_dirty: bool = True,
                     python_locks: Optional[Dict[str, str]] = None,
                     pi_package_lock: Optional[Path] = None,
                     generated_at: str = "") -> Dict[str, Any]:
    """Build-side: freeze the full source identity into PROVENANCE.json.

    ``source_commit``/``source_dirty`` come from the build environment's Git
    (the builder HAS .git); the runtime verifier never needs them to be
    re-derivable — it only needs the frozen record to be internally honest
    and externally bound to the image digest.
    """
    root = Path(source_root)
    provenance = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "source_commit": str(source_commit or "unknown"),
        "source_dirty": bool(source_dirty),
        "stock_agent_tree_sha256": _tree_sha256(root, ("stock_agent/**/*.py",)),
        "python_locks": dict(python_locks or {}),
        "pi_package_lock_sha256": (
            _sha256_file(pi_package_lock) if pi_package_lock and Path(pi_package_lock).is_file()
            else ""),
        "generated_at": str(generated_at or ""),
    }
    return provenance


def provenance_sha256(provenance: Dict[str, Any]) -> str:
    """Canonical hash of the provenance record (the value the binding pins)."""
    canonical = json.dumps(provenance, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_image_binding(binding_path: Path, *, image_digest: str,
                        provenance: Dict[str, Any], builder: str = "") -> Dict[str, Any]:
    """Build-side, AFTER the image exists: bind digest <-> provenance hash.

    The binding file lives NEXT TO the image (registry note/build log), never
    inside it — writing the digest into the image would change the digest."""
    digest = str(image_digest or "").strip()
    if not digest.startswith("sha256:"):
        raise ProvenanceError(
            "image digest must be a sha256:<hex> reference; refusing to bind {0!r}".format(digest))
    binding = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "image_digest": digest,
        "provenance_sha256": provenance_sha256(provenance),
        "builder": str(builder or ""),
    }
    path = Path(binding_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(binding, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return binding


def verify_image_provenance(shipped_provenance: Dict[str, Any],
                            expected_provenance_sha256: str,
                            observed_image_digest: str,
                            binding: Dict[str, Any]) -> Dict[str, Any]:
    """Runtime/read-side: verify the full chain. Missing pieces BLOCK — the
    absence of .git never passes anything by itself.

    chain: binding.provenance_sha256 == expected == sha256(shipped record)
           binding.image_digest == observed digest
           record shape is exactly the frozen schema (tampering adds fields).
    """
    problems: List[str] = []
    if not isinstance(shipped_provenance, dict) or \
            set(shipped_provenance) != _PROVENANCE_FIELDS or \
            str(shipped_provenance.get("schema_version")) != PROVENANCE_SCHEMA_VERSION:
        problems.append("shipped provenance record is tampered or malformed")
    expected = str(expected_provenance_sha256 or "")
    if not expected:
        problems.append("expected provenance hash was not injected into the runtime")
    binding_digest = str((binding or {}).get("image_digest") or "")
    binding_hash = str((binding or {}).get("provenance_sha256") or "")
    observed = str(observed_image_digest or "")
    if not problems and expected != provenance_sha256(shipped_provenance):
        problems.append("shipped provenance hash does not match the injected expectation")
    if not problems and binding_hash != expected:
        problems.append("binding pins a different provenance hash")
    if not problems and (not observed.startswith("sha256:")
                         or binding_digest != observed):
        problems.append("binding digest does not match the observed image digest")
    if problems:
        return {"verified": False, "problems": problems,
                "policy": "BLOCKED — missing .git never passes on its own"}
    return {"verified": True, "problems": [],
            "source_commit": shipped_provenance.get("source_commit"),
            "source_dirty": bool(shipped_provenance.get("source_dirty")),
            "provenance_sha256": expected}
