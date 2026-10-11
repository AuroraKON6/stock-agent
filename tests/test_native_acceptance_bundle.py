"""Source-only packaging tests: temporary inputs, no native process launches."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.prepare_native_acceptance import (
    BundleError, CONFIG_PATH, MANIFEST, REQUIRED_FILES, _json_bytes,
    _safe_name, prepare_bundle, synthetic_config, verify_bundle,
)


class NativeAcceptanceBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "synthetic workspace"
        self.root.mkdir()
        for name in (*REQUIRED_FILES, "stock_agent/__init__.py", "stock_agent/sources/example.py"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# synthetic fixture\n", encoding="utf-8")
        self.output = Path(temporary.name) / "bundle.zip"

    def build(self):
        return prepare_bundle(self.root, self.output)

    def rewrite(self, mutate):
        with zipfile.ZipFile(self.output) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        mutate(entries)
        target = self.output.with_name("mutated.zip")
        with zipfile.ZipFile(target, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        return target, hashlib.sha256(target.read_bytes()).hexdigest()

    def test_only_allowlisted_source_and_generated_config(self):
        private_names = (".env.local", "config/watchlist.local.json", "config/roster.json",
                         "data/evidence_vault/events.jsonl", "logs/audit.log", ".git/config",
                         ".venv312/pyvenv.cfg", "reports/private.txt", "stock_agent/local.env")
        for name in private_names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("PRIVATE_FIXTURE_DO_NOT_COPY", encoding="utf-8")
        result = self.build()
        verified = verify_bundle(self.output, result["sha256"])
        self.assertEqual(verified["native_execution"], "NOT_RUN")
        self.assertEqual(verified["status"], "VERIFIED")
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(json.loads(archive.read(CONFIG_PATH)), synthetic_config())
            for name in archive.namelist():
                self.assertNotIn(b"PRIVATE_FIXTURE_DO_NOT_COPY", archive.read(name))
            self.assertFalse(set(private_names) - {CONFIG_PATH} & set(archive.namelist()))
            manifest = json.loads(archive.read(MANIFEST))
            self.assertTrue(manifest["requires_disposable_windows_vm"])

    def test_deterministic_read_only_build_refuses_overwrite(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        first = self.build()
        other = prepare_bundle(self.root, self.output.with_name("other.zip"))
        self.assertEqual(first["sha256"], other["sha256"])
        with self.assertRaises(FileExistsError):
            self.build()
        after = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_external_digest_and_member_hash_both_required(self):
        result = self.build()
        with self.assertRaises(BundleError):
            verify_bundle(self.output, "0" * 64)
        path, digest = self.rewrite(lambda files: files.update({"stock_agent/__init__.py": b"changed"}))
        with self.assertRaisesRegex(BundleError, "hash mismatch"):
            verify_bundle(path, digest)
        self.assertEqual(verify_bundle(self.output, result["sha256"])["status"], "VERIFIED")

    def test_extra_file_and_traversal_rejected_even_with_new_zip_hash(self):
        self.build()
        for name in ("../escape.txt", "data/events.jsonl", "C:/outside.py", "stock_agent/../outside.py"):
            with self.subTest(name=name):
                path, digest = self.rewrite(lambda files: files.update({name: b"extra"}))
                with self.assertRaises(BundleError):
                    verify_bundle(path, digest)

    def test_malformed_manifest_rejected(self):
        self.build()
        for body in (b"not json", b"[]", b"{}"):
            with self.subTest(body=body):
                path, digest = self.rewrite(lambda files: files.update({MANIFEST: body}))
                with self.assertRaises(BundleError):
                    verify_bundle(path, digest)

    def test_manifest_cannot_assert_tests_passed(self):
        self.build()
        def change(files):
            manifest = json.loads(files[MANIFEST])
            manifest["native_execution"] = "PASS"
            files[MANIFEST] = _json_bytes(manifest)
        path, digest = self.rewrite(change)
        with self.assertRaisesRegex(BundleError, "contract"):
            verify_bundle(path, digest)

    def test_local_config_cannot_replace_synthetic_even_with_rehashed_manifest(self):
        self.build()
        def change(files):
            config = copy.deepcopy(synthetic_config())
            config["llm_agent"]["enabled"] = True
            files[CONFIG_PATH] = _json_bytes(config)
            manifest = json.loads(files[MANIFEST])
            entry = next(e for e in manifest["files"] if e["path"] == CONFIG_PATH)
            entry.update(size=len(files[CONFIG_PATH]), sha256=hashlib.sha256(files[CONFIG_PATH]).hexdigest())
            files[MANIFEST] = _json_bytes(manifest)
        path, digest = self.rewrite(change)
        with self.assertRaisesRegex(BundleError, "synthetic"):
            verify_bundle(path, digest)

    def test_credential_looking_source_refuses_package_without_exposing_value(self):
        token = "sk-" + "7Km9Yx3Rz8Vp2Nd6Wq4Ls5Ht"
        (self.root / "stock_agent/__init__.py").write_text("value = '" + token + "'", encoding="utf-8")
        with self.assertRaises(BundleError) as caught:
            self.build()
        self.assertNotIn(token, str(caught.exception))
        self.assertIn("credential-like", str(caught.exception))
        self.assertFalse(self.output.exists())

    def test_missing_required_source_and_size_limits_fail_before_output(self):
        source = self.root / REQUIRED_FILES[0]
        source.unlink()
        with self.assertRaises(OSError):
            self.build()
        self.assertFalse(self.output.exists())
        source.write_text("oversize", encoding="utf-8")
        with patch("scripts.prepare_native_acceptance.MAX_FILE_BYTES", 3):
            with self.assertRaisesRegex(BundleError, "size"):
                self.build()
        self.assertFalse(self.output.exists())

    def test_reparse_points_are_rejected_without_following_them(self):
        original = Path.lstat
        target = self.root / "stock_agent"
        def mocked_lstat(path):
            result = original(path)
            if path == target:
                from types import SimpleNamespace
                return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
            return result
        with patch.object(Path, "lstat", mocked_lstat):
            with self.assertRaisesRegex(BundleError, "reparse"):
                self.build()
        self.assertFalse(self.output.exists())

    def test_path_names_are_portable_and_not_windows_devices(self):
        for name in ("../bad", "/root.py", "C:/root.py", "a//b", "a/./b", "a/CON.py",
                     "stock_agent/NUL", "a/end. ", "a\\b", ""):
            with self.subTest(name=name):
                self.assertFalse(_safe_name(name))
        self.assertTrue(_safe_name("stock_agent/sources/local_jsonl.py"))
