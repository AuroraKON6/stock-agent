"""DF-02: Dify service-credential registry — fail-closed validation, zero plaintext."""
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.dify_access import (
    SCOPES,
    DifyAccessError,
    dify_config,
    ensure_case_scope,
    ensure_scope,
    load_service_credentials,
    verify_service_credential,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

TOKEN = "unit-test-service-token-alpha"
OTHER_TOKEN = "unit-test-service-token-beta"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _entry(**overrides):
    entry = {
        "credential_id": "cred-test-1",
        "token_sha256": _digest(TOKEN),
        "scopes": ["read_research"],
        "case_scope": ["case-alpha"],
        "active": True,
        "created_at": "2026-10-13T00:00:00+08:00",
        "expires_at": "",
    }
    entry.update(overrides)
    return entry


def _write_registry(root: Path, entries) -> Path:
    path = root / "config" / "dify" / "service_credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": "1.0", "credentials": list(entries)},
                               ensure_ascii=False), encoding="utf-8")
    return path


class RegistryLoadingTests(unittest.TestCase):
    def setUp(self):
        _tmp = tempfile.TemporaryDirectory()
        self.addCleanup(_tmp.cleanup)
        self.root = Path(_tmp.name)
        self._override = project_path_override(lambda value: self.root / value)
        self._override.__enter__()
        self.addCleanup(self._override.__exit__, None, None, None)

    def _config(self, **extra):
        raw = {"enabled": True}
        raw.update(extra)
        return {"dify": raw}

    def test_missing_registry_file_is_empty_and_verify_denies(self):
        registry = load_service_credentials(self._config())
        self.assertEqual(registry["credentials"], [])
        with self.assertRaises(DifyAccessError) as caught:
            verify_service_credential(registry, TOKEN)
        self.assertEqual(caught.exception.reason, "credential_unknown")

    def test_valid_token_verifies_and_returns_identity(self):
        _write_registry(self.root, [_entry(scopes=sorted(SCOPES))])
        identity = verify_service_credential(load_service_credentials(self._config()), TOKEN)
        self.assertEqual(identity["credential_id"], "cred-test-1")
        self.assertEqual(set(identity["scopes"]), SCOPES)
        self.assertEqual(identity["case_scope"], ["case-alpha"])

    def test_wrong_token_inactive_and_expired_reasons(self):
        base = datetime.now().astimezone()
        _write_registry(self.root, [_entry()])
        registry = load_service_credentials(self._config())
        with self.assertRaises(DifyAccessError) as caught:
            verify_service_credential(registry, OTHER_TOKEN)
        self.assertEqual(caught.exception.reason, "credential_unknown")
        inactive_registry = {"credentials": [_entry(credential_id="cred-off", active=False)]}
        with self.assertRaises(DifyAccessError) as caught:
            verify_service_credential(inactive_registry, TOKEN)
        self.assertEqual(caught.exception.reason, "credential_inactive")
        expired_registry = {"credentials": [_entry(
            credential_id="cred-old",
            expires_at=(base - timedelta(minutes=1)).isoformat())]}
        with self.assertRaises(DifyAccessError) as caught:
            verify_service_credential(expired_registry, TOKEN)
        self.assertEqual(caught.exception.reason, "credential_expired")

    def test_registry_validation_rejects_unknown_fields_bad_hash_bad_scope(self):
        _write_registry(self.root, [_entry(surprise=1)])
        with self.assertRaises(ValueError):
            load_service_credentials(self._config())
        _write_registry(self.root, [_entry(token_sha256="not-a-hash")])
        with self.assertRaises(ValueError):
            load_service_credentials(self._config())
        _write_registry(self.root, [_entry(scopes=["admin"])])
        with self.assertRaises(ValueError):
            load_service_credentials(self._config())
        _write_registry(self.root, [_entry(case_scope=[])])
        with self.assertRaises(ValueError):
            load_service_credentials(self._config())

    def test_scope_and_case_scope_gates(self):
        identity = {"credential_id": "c", "scopes": ["read_research"], "case_scope": ["case-alpha"]}
        ensure_scope(identity, "read_research")  # granted
        with self.assertRaises(DifyAccessError) as caught:
            ensure_scope(identity, "create_research")
        self.assertEqual(caught.exception.reason, "scope_not_granted")
        ensure_case_scope(identity, "case-alpha")
        ensure_case_scope({"credential_id": "c", "scopes": [], "case_scope": ["*"]}, "any-case")
        with self.assertRaises(DifyAccessError) as caught:
            ensure_case_scope(identity, "case-beta")
        self.assertEqual(caught.exception.reason, "case_out_of_scope")


class DifyConfigTests(unittest.TestCase):
    def test_defaults_disabled_and_rejects_unknown_keys(self):
        settings = dify_config({})
        self.assertFalse(settings["enabled"])
        self.assertEqual(settings["approved_workflow_versions"], [])
        self.assertEqual(settings["poll"]["max_polls"], 40)
        with self.assertRaises(ValueError):
            dify_config({"dify": {"oops": 1}})
        with self.assertRaises(ValueError):
            dify_config({"dify": {"poll": {"max_polls": 0}}})
        with self.assertRaises(ValueError):
            dify_config({"dify": {"approved_workflow_versions": [1]}})


class ExampleRegistryTests(unittest.TestCase):
    def test_shipped_example_validates_and_carries_no_plaintext_token(self):
        example = Path(__file__).resolve().parents[1] / "config" / "dify.service_credentials.example.json"
        document = json.loads(example.read_text(encoding="utf-8"))
        text = example.read_text(encoding="utf-8")
        self.assertNotIn("example-service-token-REPLACE-BEFORE-USE", text,
                         "the placeholder token's plaintext must not ride along in the file")
        _tmp = tempfile.TemporaryDirectory()
        self.addCleanup(_tmp.cleanup)
        root = Path(_tmp.name)
        with project_path_override(lambda value: root / value):
            target = root / "config" / "dify" / "service_credentials.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            registry = load_service_credentials({"dify": {"enabled": True}})
        self.assertEqual(len(registry["credentials"]), 1)
        entry = registry["credentials"][0]
        self.assertEqual(entry["token_sha256"], _digest("example-service-token-REPLACE-BEFORE-USE"))
        # The placeholder token DOES verify — proving the hash math — but it is
        # documented as replace-before-use, and the file carries no plaintext.
        identity = verify_service_credential(registry, "example-service-token-REPLACE-BEFORE-USE")
        self.assertEqual(identity["credential_id"], "dify-phase1-orchestrator-EXAMPLE")


if __name__ == "__main__":
    unittest.main()
