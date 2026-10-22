"""R08: image provenance chain + candidate Docker/compose/lockfile contracts.

O-layer coverage: the provenance build/bind/verify chain (tamper rejection,
missing-evidence blocking — a missing .git NEVER passes on its own, A39's
digest-consistency stop condition), the lockfile platform sanity (no
Windows-only packages in the Linux lock), the Dockerfile digest pins (real
digests fetched from the Docker Hub registry API, never fabricated), the
.dockerignore hard exclusions, and the compose digest refusal. The C-layer
build/run (A34/A37/A38/A40) stays NOT_RUN: the docker engine was unreachable
from the authoring session — see REFACTOR_R08 §3 precheck evidence.
"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from stock_agent.release_provenance import (
    BINDING_FILENAME,
    PROVENANCE_FILENAME,
    PROVENANCE_SCHEMA_VERSION,
    ProvenanceError,
    build_provenance,
    provenance_sha256,
    verify_image_provenance,
    write_image_binding,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}\s*$")


class ProvenanceChainTests(unittest.TestCase):
    def _record(self, **overrides):
        record = build_provenance(
            ROOT,
            source_commit="abc123",
            source_dirty=False,
            python_locks={"requirements.lock.txt": "f" * 64},
            pi_package_lock=ROOT / "pi_runtime" / "package-lock.json",
            generated_at="2026-10-22T12:00:00+08:00")
        record.update(overrides)
        return record

    def test_full_chain_verifies(self):
        record = self._record()
        digest = "sha256:" + "a" * 64
        with tempfile.TemporaryDirectory() as tmp:
            binding = write_image_binding(Path(tmp) / BINDING_FILENAME,
                                          image_digest=digest, provenance=record)
            verdict = verify_image_provenance(
                record, provenance_sha256(record), digest, binding)
        self.assertTrue(verdict["verified"], verdict)
        self.assertEqual(verdict["source_commit"], "abc123")
        self.assertFalse(verdict["source_dirty"])

    def test_tampered_record_rejected(self):
        record = self._record()
        digest = "sha256:" + "a" * 64
        tampered = dict(record, extra_field="smuggled")
        verdict = verify_image_provenance(
            tampered, provenance_sha256(record), digest,
            {"image_digest": digest, "provenance_sha256": provenance_sha256(record)})
        self.assertFalse(verdict["verified"])
        self.assertTrue(any("tampered" in item for item in verdict["problems"]))

    def test_missing_expectation_or_binding_blocks_never_autopasses(self):
        record = self._record()
        digest = "sha256:" + "a" * 64
        # No expectation injected, no binding at all: BLOCKED — this is the
        # "image without .git" case and it must never pass on its own.
        verdict = verify_image_provenance(record, "", digest, {})
        self.assertFalse(verdict["verified"])
        self.assertIn("never passes", verdict["policy"])

    def test_digest_mismatch_rejected(self):
        record = self._record()
        binding = {"image_digest": "sha256:" + "b" * 64,
                   "provenance_sha256": provenance_sha256(record)}
        verdict = verify_image_provenance(
            record, provenance_sha256(record), "sha256:" + "c" * 64, binding)
        self.assertFalse(verdict["verified"])

    def test_binding_refuses_non_sha256_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ProvenanceError):
                write_image_binding(Path(tmp) / BINDING_FILENAME,
                                    image_digest="latest", provenance=self._record())

    def test_tree_hash_changes_with_content(self):
        base = build_provenance(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp)
            (other / "stock_agent").mkdir()
            (other / "stock_agent" / "x.py").write_text("print(1)\n", encoding="utf-8")
            different = build_provenance(other)
        self.assertNotEqual(base["stock_agent_tree_sha256"],
                            different["stock_agent_tree_sha256"])


class LockfileContractTests(unittest.TestCase):
    def test_linux_lock_exists_hashed_and_windows_free(self):
        path = ROOT / "requirements.linux-py312.lock.txt"
        self.assertTrue(path.is_file(), "R08 deliverable: linux lockfile")
        text = path.read_text(encoding="utf-8")
        self.assertIn("--python-platform linux", text.splitlines()[1])
        self.assertIn("--python-version 3.12", text.splitlines()[1])
        # A39: lock entries carry hashes; no Windows-only wheels leak in.
        self.assertGreater(text.count("--hash=sha256:"), 25)
        for windows_only in ("pywin32", "pywinpty", "win32", "colorama=="):
            self.assertNotIn(windows_only, text)

    def test_pyproject_requires_python_matches_baseline(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('requires-python = ">=3.11"', text)


class ImageContractTests(unittest.TestCase):
    """Static contract checks over the candidate Dockerfile/compose/ignore —
    the C-layer build/run acceptance (A34/A37/A38/A40) stays NOT_RUN."""

    def test_base_images_digest_pinned(self):
        text = (ROOT / "deploy" / "docker" / "Dockerfile").read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("ARG PYTHON_IMAGE=") or line.startswith("ARG NODE_IMAGE="):
                self.assertRegex(line, _DIGEST_RE,
                                 "floating base image is an A39 stop condition")
        self.assertNotIn(":latest", text)

    def test_dockerfile_has_no_runtime_install(self):
        text = (ROOT / "deploy" / "docker" / "Dockerfile").read_text(encoding="utf-8")
        runtime_part = text.split("runtime: node base", 1)[1]
        # The RUN in the runtime stage only creates the user/dirs.
        pip_hits = re.findall(r"pip install|npm ci|npm install", runtime_part)
        self.assertEqual(pip_hits, [], "A38: no dependency installs at runtime")
        self.assertIn("USER app", runtime_part)
        self.assertIn("APP_TEMP_ROOT", text)

    def test_dockerignore_hard_exclusions(self):
        text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for forbidden in (".git", ".venv", "config/*.local.json", "data/",
                          "reports/", "logs/", "pi_runtime/node_modules", ".env"):
            self.assertIn(forbidden, text)

    def test_compose_refuses_floating_image(self):
        text = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("STOCK_AGENT_TAG:?", text,
                      "compose must fail closed until a digest is pinned")
        self.assertIn("read_only: true", text)
        self.assertIn("cap_drop", text)

    def test_build_wrapper_enforces_pins_and_binding(self):
        text = (ROOT / "scripts" / "build_image.sh").read_text(encoding="utf-8")
        self.assertIn("@sha256:", text)
        self.assertIn("write_image_binding", text)
        self.assertIn("allow-dirty", text)


class ReleaseAuditProvenanceBranchTests(unittest.TestCase):
    def test_no_git_blocks_and_provenance_path_wiring_exists(self):
        from stock_agent import release_audit
        source = Path(release_audit.__file__).read_text(encoding="utf-8")
        self.assertIn("_image_provenance_snapshot", source)
        self.assertIn("NEVER passes on its own", source)
        # And the snapshot helper itself fails closed on missing evidence.
        with tempfile.TemporaryDirectory() as tmp:
            verdict = release_audit._image_provenance_snapshot(Path(tmp))
        self.assertFalse(verdict["verified"])
        self.assertTrue(verdict["problems"])

    def test_linux_lock_in_audit_candidates(self):
        from stock_agent import release_audit
        self.assertIn("requirements.linux-py312.lock.txt",
                      release_audit.PYTHON_LOCK_CANDIDATES)


if __name__ == "__main__":
    unittest.main()
