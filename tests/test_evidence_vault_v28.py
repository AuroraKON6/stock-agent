from __future__ import annotations

import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path

from stock_agent.config import project_path_override
from stock_agent.evidence_vault import (
    EvidenceVaultError,
    activate_production_archive,
    archive_policy_fingerprint,
    attach_evidence_vault_status,
    evidence_status_for_signal,
    fetch_official_document,
    import_official_document,
    link_evidence_to_signal,
    load_evidence_vault,
    record_archive_review,
    revoke_production_archive,
    validate_evidence_vault_policy,
    validate_official_url,
    verify_evidence_vault,
)
from stock_agent.models import AnalyzedSignal
from stock_agent.llm_governance import prepare_llm_batch
from stock_agent.research_workpapers import load_research_workpapers, sync_research_workpapers


def _config(*, backend="local_content_addressed_shadow", evidence_ref="", immutable=False):
    return {
        "official_evidence_vault": {
            "enabled": True,
            "enforce": True,
            "events_path": "data/evidence_vault/events.jsonl",
            "index_path": "data/evidence_vault/index.json",
            "objects_dir": "data/evidence_vault/objects/sha256",
            "quarantine_dir": "data/evidence_vault/quarantine/sha256",
            "report_dir": "reports/evidence_vault",
            "allowed_official_domains": [
                "bse.cn", "cninfo.com.cn", "csrc.gov.cn", "sac.net.cn", "sse.com.cn", "szse.cn"
            ],
            "allowed_content_types": [
                "application/json", "application/pdf", "application/xml", "text/html", "text/plain", "text/xml"
            ],
            "require_https": True,
            "resolve_public_ip_on_fetch": True,
            "allow_network_fetch": False,
            "auto_capture_official_signals": False,
            "max_document_bytes": 1024 * 1024,
            "reject_challenge_pages": True,
            "require_published_at_for_llm": True,
            "require_retained_before_external_llm_for": ["announcement", "filing", "financial"],
            "retention_years": 5,
            "capture_actor_id": "collector.service",
            "capture_actor_role": "information_analyst",
            "automatic_approval": False,
            "automatic_publication": False,
            "automatic_trading": False,
            "production_archive": {
                "backend": backend,
                "evidence_ref": evidence_ref,
                "external_immutability_verified": immutable,
                "required_review_roles": ["compliance_officer", "information_security_officer"],
                "activation_roles": ["data_steward", "research_coordinator"],
            },
        },
        "research_operations": {
            "enabled": True,
            "enforce_roster": True,
            "roster_path": "config/roster.json",
            "restrictions_path": "config/restrictions.json",
        },
        "llm_governance": {
            "enabled": True,
            "enforce": True,
            "external_provider": True,
            "default_classification": "internal",
            "allowed_external_classifications": ["public"],
            "public_source_types": ["filing", "announcement", "financial", "news"],
            "trusted_public_sources": ["cninfo"],
            "trusted_public_domains": ["cninfo.com.cn"],
            "block_secrets": True,
            "block_personal_data": True,
            "block_nonpublic_information": True,
            "block_prompt_injection": True,
            "require_evidence_refs": True,
            "audit_enabled": True,
            "audit_path": "data/llm_governance/audit",
            "exception_path": "data/llm_governance/exceptions",
            "report_path": "reports/llm_governance",
        },
    }


def _signal(signal_id="sig-v28", source_type="filing", url="https://static.cninfo.com.cn/finalpage/test.pdf"):
    return AnalyzedSignal(
        signal_id=signal_id,
        source="cninfo",
        source_type=source_type,
        title="测试公司公告",
        summary="公开公告摘要",
        url=url,
        published_at="2026-08-20T08:00:00+08:00",
        collected_at="2026-08-20T08:01:00+08:00",
        symbols=["688888"],
        payload={},
    )


class _FakeResponse:
    def __init__(self, data, url, content_type="application/pdf"):
        self.data = data
        self.url = url
        self.status = 200
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(data))
        self.closed = False

    def geturl(self):
        return self.url

    def read(self, limit):
        return self.data[:limit]

    def close(self):
        self.closed = True


class _FakeOpener:
    def __init__(self, response):
        self.response = response
        self.request = None

    def open(self, request, timeout=0):
        self.request = request
        return self.response


class EvidenceVaultV28Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.override = project_path_override(lambda value: self.root / str(value))
        self.override.__enter__()
        self.config = _config()
        (self.root / "config").mkdir(parents=True)
        roster = {
            "principals": [
                {
                    "actor_id": "collector.service", "display_name": "Collector", "principal_type": "service",
                    "active": True, "roles": ["information_analyst"], "information_side": "public",
                    "coverage_entities": ["*"], "max_active_cases": 20,
                },
                {
                    "actor_id": "compliance.human", "display_name": "Compliance", "principal_type": "human",
                    "active": True, "roles": ["compliance_officer"], "information_side": "wall",
                    "authorizations": ["compliance_authorized"], "coverage_entities": ["*"], "max_active_cases": 20,
                },
                {
                    "actor_id": "security.human", "display_name": "Security", "principal_type": "human",
                    "active": True, "roles": ["information_security_officer"], "information_side": "wall",
                    "coverage_entities": ["*"], "max_active_cases": 20,
                },
                {
                    "actor_id": "data.human", "display_name": "Data", "principal_type": "human",
                    "active": True, "roles": ["data_steward"], "information_side": "public",
                    "coverage_entities": ["*"], "max_active_cases": 20,
                },
                {
                    "actor_id": "coordinator.human", "display_name": "Coordinator", "principal_type": "human",
                    "active": True, "roles": ["research_coordinator"], "information_side": "public",
                    "coverage_entities": ["*"], "max_active_cases": 20,
                },
            ]
        }
        (self.root / "config" / "roster.json").write_text(json.dumps(roster), encoding="utf-8")
        (self.root / "config" / "restrictions.json").write_text(json.dumps({"entries": []}), encoding="utf-8")

    def tearDown(self):
        self.override.__exit__(None, None, None)
        self.temp.cleanup()

    def _pdf(self, name="notice.pdf", data=b"%PDF-1.7\npublic filing\n%%EOF"):
        path = self.root / name
        path.write_bytes(data)
        return path

    def _import(self, *, signal_ids=None, published_at="2026-08-20T08:00:00+08:00", path=None, url=None):
        return import_official_document(
            self.config,
            str(path or self._pdf()),
            source_url=url or "https://static.cninfo.com.cn/finalpage/test.pdf",
            title="测试公司公告",
            published_at=published_at,
            actor="collector.service",
            actor_role="information_analyst",
            signal_ids=signal_ids or ["sig-v28"],
            issuer="测试公司",
            symbol="688888",
        )

    def _enable_workpapers(self):
        self.config["research_workpapers"] = {
            "enabled": True,
            "enforce": True,
            "events_path": "data/research_workpapers/events.jsonl",
            "projection_path": "data/research_workpapers/latest.json",
            "report_dir": "reports/research_workpapers",
            "retention_years": 5,
            "require_official_primary_for": ["P0", "P1"],
            "require_exact_evidence_refs": True,
            "require_market_governance_pass": True,
            "require_distinct_human_roles": True,
            "require_independent_challenge": True,
            "archive_backend": "local_append_only_shadow",
            "production_archive_approved": False,
            "production_archive_evidence_ref": "",
            "automatic_approval": False,
            "automatic_assignment": False,
            "automatic_publication": False,
            "automatic_trading": False,
        }
        self.config["pi_agent"] = {"compiled_skills_enabled": False}

    def test_manual_import_writes_content_addressed_object_chain_and_reports(self):
        result = self._import()
        document = result["document"]
        vault = load_evidence_vault(self.config)
        self.assertFalse(result["duplicate"])
        self.assertEqual(vault["integrity_status"], "OK")
        self.assertEqual(vault["event_count"], 1)
        self.assertEqual(document["status"], "captured")
        self.assertTrue(document["object_locator"].startswith("evidence-vault:sha256:"))
        self.assertTrue((self.root / document["object_path"]).is_file())
        self.assertTrue(Path(result["evidence_vault_latest_path"]).is_file())
        self.assertFalse(vault["archive_control"]["ready"])

    def test_repeat_import_is_idempotent(self):
        first = self._import()
        second = self._import(path=self._pdf())
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(load_evidence_vault(self.config)["event_count"], 1)

    def test_same_bytes_new_signal_appends_linked_capture(self):
        path = self._pdf()
        self._import(path=path, signal_ids=["sig-a"])
        self._import(path=path, signal_ids=["sig-b"])
        vault = load_evidence_vault(self.config)
        document = next(iter(vault["documents"].values()))
        self.assertEqual(vault["event_count"], 2)
        self.assertEqual(set(document["signal_ids"]), {"sig-a", "sig-b"})

    def test_nonofficial_and_http_urls_are_rejected(self):
        with self.assertRaisesRegex(EvidenceVaultError, "allowlist"):
            self._import(url="https://example.com/notice.pdf")
        with self.assertRaisesRegex(EvidenceVaultError, "HTTPS"):
            self._import(url="http://www.cninfo.com.cn/notice.pdf")

    def test_private_dns_target_is_rejected_before_fetch(self):
        resolver = lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))]
        with self.assertRaisesRegex(EvidenceVaultError, "non-public"):
            validate_official_url("https://www.cninfo.com.cn/test.pdf", self.config["official_evidence_vault"], resolver=resolver)

    def test_challenge_page_is_quarantined_and_not_llm_eligible(self):
        html = self.root / "challenge.html"
        html.write_text("<html><body>Verify you are human CAPTCHA</body></html>", encoding="utf-8")
        result = self._import(path=html)
        self.assertEqual(result["document"]["status"], "quarantined")
        signal = _signal()
        status = evidence_status_for_signal(signal, self.config)
        self.assertEqual(status["status"], "QUARANTINED")
        self.assertFalse(status["llm_eligible"])

    def test_invalid_publication_time_retains_but_blocks_llm(self):
        self._import(published_at="")
        status = evidence_status_for_signal(_signal(), self.config)
        self.assertEqual(status["status"], "INCOMPLETE_METADATA")
        self.assertFalse(status["llm_eligible"])

    def test_tampered_object_fails_technical_verification(self):
        result = self._import()
        (self.root / result["document"]["object_path"]).write_bytes(b"tampered")
        verification = verify_evidence_vault(self.config)
        self.assertEqual(verification["technical_status"], "FAIL")
        self.assertEqual(verification["tampered_object_count"], 1)

    def test_tampered_event_chain_fails_closed(self):
        self._import()
        path = self.root / "data" / "evidence_vault" / "events.jsonl"
        event = json.loads(path.read_text(encoding="utf-8"))
        event["payload"]["title"] = "tampered"
        path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        verification = verify_evidence_vault(self.config)
        self.assertEqual(verification["integrity_status"], "FAIL")
        self.assertEqual(verification["technical_status"], "FAIL")

    def test_filing_requires_retained_evidence_but_news_does_not(self):
        filing = evidence_status_for_signal(_signal(), self.config)
        news = evidence_status_for_signal(_signal(source_type="news"), self.config)
        self.assertEqual(filing["status"], "MISSING")
        self.assertFalse(filing["llm_eligible"])
        self.assertEqual(news["status"], "NOT_REQUIRED")
        self.assertTrue(news["llm_eligible"])

    def test_attached_status_contains_only_public_locator_not_local_path(self):
        self._import()
        signal = _signal()
        signals, stats = attach_evidence_vault_status([signal], self.config)
        payload = signals[0].payload["official_evidence_vault"]
        serialized = json.dumps(payload)
        self.assertTrue(payload["llm_eligible"])
        self.assertEqual(stats["evidence_vault_llm_eligible"], 1)
        self.assertIn("evidence-vault:sha256:", serialized)
        self.assertNotIn(str(self.root), serialized)
        self.assertFalse(payload["raw_document_sent_to_external_llm"])

    def test_llm_preflight_denies_required_filing_without_retained_original(self):
        signal = _signal()
        signals, _ = attach_evidence_vault_status([signal], self.config)
        approved, payloads, stats = prepare_llm_batch(signals, self.config, "signal_llm")
        self.assertEqual(approved, [])
        self.assertEqual(payloads, {})
        self.assertEqual(stats["llm_governance_denied"], 1)
        decision = signal.llm_governance["signal_llm"]
        self.assertEqual(decision["official_evidence_status"], "MISSING")
        self.assertFalse(decision["raw_document_sent_to_external_llm"])

    def test_llm_preflight_allows_filing_after_retained_original(self):
        self._import()
        signal = _signal()
        signals, _ = attach_evidence_vault_status([signal], self.config)
        approved, payloads, stats = prepare_llm_batch(signals, self.config, "signal_llm")
        self.assertEqual([item.signal_id for item in approved], ["sig-v28"])
        self.assertIn("sig-v28", payloads)
        self.assertEqual(stats["llm_governance_approved"], 1)
        self.assertNotIn("object_path", json.dumps(payloads))
        self.assertNotIn(str(self.root), json.dumps(payloads))

    def test_llm_preflight_does_not_require_vault_for_news(self):
        signal = _signal(source_type="news")
        signals, _ = attach_evidence_vault_status([signal], self.config)
        approved, _, stats = prepare_llm_batch(signals, self.config, "signal_llm")
        self.assertEqual([item.signal_id for item in approved], ["sig-v28"])
        self.assertEqual(stats["llm_governance_approved"], 1)

    def test_workpaper_uses_retained_raw_hash_and_vault_locator(self):
        self._enable_workpapers()
        imported = self._import()
        signal = _signal().to_dict()
        case = {
            "case_id": "case-v28",
            "title": "测试公司公告",
            "entity": "688888",
            "topic": "公告",
            "catalyst_path": "filing",
            "top_priority": "P1",
            "status": "active",
            "signal_ids": ["sig-v28"],
            "created_at": "2026-08-20T08:01:00+08:00",
        }
        sync_research_workpapers(self.config, cases=[case], signals=[signal], runtime_cases=[])
        source = load_research_workpapers(self.config)["workpapers"][0]["source_records"][0]
        self.assertTrue(source["original_document_retained"])
        self.assertEqual(source["content_sha256"], imported["document"]["sha256"])
        self.assertEqual(source["vault_document_id"], imported["document"]["document_id"])
        self.assertTrue(source["source_locator"].startswith("evidence-vault:sha256:"))
        self.assertEqual(source["authority_tier"], "official_disclosure_platform")
        self.assertFalse(source["raw_document_sent_to_external_llm"])

    def test_explicit_link_adds_signal_without_changing_object(self):
        result = self._import(signal_ids=["sig-a"])
        linked = link_evidence_to_signal(
            self.config,
            document_id=result["document"]["document_id"],
            signal_id="sig-b",
            actor="collector.service",
            actor_role="information_analyst",
            notes="same official filing",
        )
        self.assertFalse(linked["duplicate"])
        self.assertIn("sig-b", linked["document"]["signal_ids"])

    def test_unregistered_capture_actor_is_rejected(self):
        with self.assertRaisesRegex(EvidenceVaultError, "not active"):
            import_official_document(
                self.config,
                str(self._pdf()),
                source_url="https://static.cninfo.com.cn/finalpage/test.pdf",
                title="公告",
                published_at="2026-08-20",
                actor="made-up-user",
                actor_role="information_analyst",
            )

    def test_network_fetch_requires_explicit_enablement(self):
        with self.assertRaisesRegex(EvidenceVaultError, "disabled"):
            fetch_official_document(
                self.config,
                source_url="https://static.cninfo.com.cn/finalpage/test.pdf",
                title="公告",
                published_at="2026-08-20",
                actor="collector.service",
                actor_role="information_analyst",
            )

    def test_controlled_network_fetch_records_transport_binding(self):
        self.config["official_evidence_vault"]["allow_network_fetch"] = True
        response = _FakeResponse(b"%PDF-1.7\npublic\n%%EOF", "https://static.cninfo.com.cn/finalpage/test.pdf")
        opener = _FakeOpener(response)
        resolver = lambda *args, **kwargs: [(2, 1, 6, "", ("203.0.113.10", 443))]
        # Documentation ranges are not globally routable according to ipaddress,
        # so use an ordinary public resolver result for this isolated transport test.
        resolver = lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))]
        result = fetch_official_document(
            self.config,
            source_url="https://static.cninfo.com.cn/finalpage/test.pdf",
            title="公告",
            published_at="2026-08-20",
            actor="collector.service",
            actor_role="information_analyst",
            signal_ids=["sig-v28"],
            opener=opener,
            resolver=resolver,
        )
        self.assertEqual(result["document"]["capture_mode"], "network_fetch")
        self.assertEqual(result["document"]["source_binding_status"], "https_transport_verified")
        self.assertTrue(response.closed)
        self.assertNotIn("Authorization", opener.request.headers)

    def test_policy_rejects_unreviewed_domain_and_automatic_approval(self):
        self.config["official_evidence_vault"]["allowed_official_domains"].append("example.com")
        self.config["official_evidence_vault"]["automatic_approval"] = True
        errors = validate_evidence_vault_policy(self.config)
        self.assertTrue(any("unreviewed" in item for item in errors))
        self.assertTrue(any("automatic" in item for item in errors))

    def test_local_shadow_archive_can_never_be_activated(self):
        with self.assertRaisesRegex(EvidenceVaultError, "local"):
            activate_production_archive(
                self.config,
                actor="data.human",
                actor_role="data_steward",
                notes="activate",
            )

    def test_external_archive_requires_two_distinct_reviews_and_activation(self):
        self.config = _config(backend="approved_external_worm", evidence_ref="contract:archive:2026", immutable=True)
        record_archive_review(
            self.config,
            decision="approve",
            actor="compliance.human",
            actor_role="compliance_officer",
            notes="compliance controls reviewed",
            evidence_refs=["contract:archive:2026"],
        )
        with self.assertRaisesRegex(EvidenceVaultError, "incomplete"):
            activate_production_archive(
                self.config, actor="data.human", actor_role="data_steward", notes="activate"
            )
        record_archive_review(
            self.config,
            decision="approve",
            actor="security.human",
            actor_role="information_security_officer",
            notes="immutability controls reviewed",
            evidence_refs=["assessment:archive:2026"],
        )
        result = activate_production_archive(
            self.config,
            actor="data.human",
            actor_role="data_steward",
            notes="approved archive connected",
        )
        self.assertTrue(result["vault"]["archive_control"]["ready"])
        self.assertTrue(verify_evidence_vault(self.config)["production_archive_ready"])

    def test_service_account_cannot_approve_archive(self):
        self.config = _config(backend="approved_external_worm", evidence_ref="contract:archive:2026", immutable=True)
        with self.assertRaisesRegex(EvidenceVaultError, "real human"):
            record_archive_review(
                self.config,
                decision="approve",
                actor="collector.service",
                actor_role="compliance_officer",
                notes="not allowed",
                evidence_refs=["contract:archive:2026"],
            )

    def test_archive_config_change_invalidates_reviews_and_activation(self):
        self.config = _config(backend="approved_external_worm", evidence_ref="contract:a", immutable=True)
        old_fingerprint = archive_policy_fingerprint(self.config)
        record_archive_review(
            self.config, decision="approve", actor="compliance.human", actor_role="compliance_officer",
            notes="review", evidence_refs=["contract:a"],
        )
        record_archive_review(
            self.config, decision="approve", actor="security.human", actor_role="information_security_officer",
            notes="review", evidence_refs=["assessment:a"],
        )
        activate_production_archive(
            self.config, actor="data.human", actor_role="data_steward", notes="activate"
        )
        self.config["official_evidence_vault"]["production_archive"]["evidence_ref"] = "contract:b"
        vault = load_evidence_vault(self.config)
        self.assertNotEqual(old_fingerprint, archive_policy_fingerprint(self.config))
        self.assertFalse(vault["archive_control"]["ready"])
        self.assertEqual(vault["archive_control"]["approved_review_count"], 0)

    def test_archive_revocation_fails_closed(self):
        self.config = _config(backend="approved_external_worm", evidence_ref="contract:a", immutable=True)
        record_archive_review(
            self.config, decision="approve", actor="compliance.human", actor_role="compliance_officer",
            notes="review", evidence_refs=["contract:a"],
        )
        record_archive_review(
            self.config, decision="approve", actor="security.human", actor_role="information_security_officer",
            notes="review", evidence_refs=["assessment:a"],
        )
        activate_production_archive(
            self.config, actor="data.human", actor_role="data_steward", notes="activate"
        )
        result = revoke_production_archive(
            self.config, actor="security.human", actor_role="information_security_officer", notes="control incident"
        )
        self.assertFalse(result["vault"]["archive_control"]["ready"])
        self.assertFalse(result["vault"]["archive_control"]["activation"]["active"])


if __name__ == "__main__":
    unittest.main()
