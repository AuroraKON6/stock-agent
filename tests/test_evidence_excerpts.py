"""Exact source lineage with real temporary vault producers; no model/network."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from stock_agent.config import project_path, project_path_override
from stock_agent.evidence_context import verified_evidence_documents
from stock_agent.evidence_vault import import_official_document
from stock_agent.evidence_excerpts import (
    EvidenceExcerptError, bind_summary_draft, create_excerpt, excerpt_selector, verify_excerpt_selection,
)
from stock_agent.research_worker import build_safe_research_context, build_user_prompt
from stock_agent.task_supply import build_task_spec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import import_document, vault_config


class EvidenceExcerptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root)
        self.body = "\ufeffHeading\n\u6536\u5165\u4e0b\u964d\u3002\nBODY_SENTINEL_NOT_FOR_EGRESS\nFooter"
        self.ref, self.sha = import_document(self.config, self.root, content=self.body, title="Fixture title")
        document = verified_evidence_documents(self.config)[self.sha]
        self.text = project_path(document["object_path"]).read_bytes().decode("utf-8-sig")
        self.start = self.text.index("\u6536")

    def excerpt(self):
        return create_excerpt(self.config, self.ref, self.start, self.start + 5)

    def test_exact_unicode_offsets_and_source_hash(self):
        excerpt = self.excerpt()
        self.assertEqual(excerpt["text"], self.text[self.start:self.start + 5])
        self.assertEqual(excerpt["document_sha256"], self.sha)
        self.assertEqual(excerpt["source_text_sha256"], hashlib.sha256(self.text.encode()).hexdigest())
        self.assertEqual(excerpt["excerpt_sha256"], hashlib.sha256(excerpt["text"].encode()).hexdigest())
        self.assertEqual((excerpt["line_start"], excerpt["line_end"]), (2, 2))
        self.assertEqual(excerpt["scope"], "local_review_only")
        self.assertFalse(excerpt["external_eligible"])
        self.assertNotIn("object_path", excerpt)

    def test_boundaries_fail_without_clamping_or_truncation(self):
        for start, end in ((-1, 5), (5, 5), (6, 4), (False, 3), (0, True), (0, 5000), (0, 99), ("0", 5)):
            with self.subTest(start=start, end=end), self.assertRaises(EvidenceExcerptError):
                create_excerpt(self.config, self.ref, start, end)

    def test_generation_is_stable_and_read_only(self):
        before = {str(p): p.read_bytes() for p in (self.root / "vault").rglob("*") if p.is_file()}
        first = self.excerpt()
        self.assertEqual(first, self.excerpt())
        verify_excerpt_selection(self.config, excerpt_selector(first))
        after = {str(p): p.read_bytes() for p in (self.root / "vault").rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_selector_tampering_rejected(self):
        selector = excerpt_selector(self.excerpt())
        for field, value in (("document_sha256", "0" * 64), ("excerpt_sha256", "0" * 64),
                             ("source_text_sha256", "0" * 64), ("extractor_version", "another-parser"),
                             ("end", self.start + 6), ("external_eligible", True)):
            changed = dict(selector, **{field: value})
            with self.subTest(field=field), self.assertRaises(EvidenceExcerptError):
                verify_excerpt_selection(self.config, changed)

    def test_missing_object_fails_closed(self):
        document = verified_evidence_documents(self.config)[self.sha]
        project_path(document["object_path"]).unlink()
        with self.assertRaises(EvidenceExcerptError):
            self.excerpt()

    def test_rehashes_actual_bytes_after_verification(self):
        documents = verified_evidence_documents(self.config)
        path = project_path(documents[self.sha]["object_path"])
        data = path.read_bytes()
        path.write_bytes(data[:-1] + b"X")
        with patch("stock_agent.evidence_excerpts.verified_evidence_documents", return_value=documents):
            with self.assertRaisesRegex(EvidenceExcerptError, "bytes changed"):
                self.excerpt()

    def test_unsupported_html_has_no_title_fallback(self):
        ref, _ = import_document(self.config, self.root, content="<!doctype html><html><body>Fixture</body></html>", title="HTML")
        with self.assertRaisesRegex(EvidenceExcerptError, "unsupported format"):
            create_excerpt(self.config, ref, 0, 5)

    def test_unsupported_bytes_and_size_limit_with_real_vault(self):
        for body, reason in ((b"Bad UTF8: \xff", "strict UTF-8"),
                             (b"Plain text\x00control", "control characters"),
                             (b"x" * (2 * 1024 * 1024 + 1), "size limit")):
            with self.subTest(reason=reason):
                path = self.root / "incoming-binary.txt"
                path.write_bytes(body)
                result = import_official_document(
                    self.config, str(path), source_url="https://www.cninfo.com.cn/fixture",
                    title="Byte boundary fixture", published_at="2026-09-20T18:00:00+08:00",
                    actor="test-evidence-collector", actor_role="information_analyst",
                )
                document = result.get("document") or result
                with self.assertRaisesRegex(EvidenceExcerptError, reason):
                    create_excerpt(self.config, document["document_id"], 0, 5)

    def test_cross_document_selection_cannot_relabel_source(self):
        ref, _ = import_document(self.config, self.root, content="Different document", title="Other")
        selector = excerpt_selector(self.excerpt())
        selector["document_id"] = ref
        with self.assertRaises(EvidenceExcerptError):
            verify_excerpt_selection(self.config, selector)

    def test_summary_invalid_shapes_fail_closed(self):
        selector = excerpt_selector(self.excerpt())
        for selectors, text, kind in (([], "Draft", "human_judgment"),
                                     ([selector] * 6, "Draft", "human_judgment"),
                                     ([selector], " " * 3, "human_judgment"),
                                     ([selector], "x" * 4001, "human_judgment"),
                                     ([selector], None, "human_judgment"),
                                     ([selector], "Draft", [])):
            with self.subTest(kind=kind), self.assertRaises(EvidenceExcerptError):
                bind_summary_draft(self.config, selectors, text, kind)

    def test_title_digest_matches_delivered_truncated_metadata(self):
        ref, _ = import_document(self.config, self.root, content="Long title fixture", title="T" * 1200)
        spec = build_task_spec(research_question="Fixture research", symbols=["600519"], config=self.config)
        safe, _ = build_safe_research_context(spec, self.config)
        summary = safe["evidence_summaries"][ref]
        self.assertEqual(summary["summary_sha256"], hashlib.sha256(summary["summary"].encode()).hexdigest())

    def test_summary_versions_bind_sources_but_do_not_certify_claims(self):
        selector = excerpt_selector(self.excerpt())
        first = bind_summary_draft(self.config, [selector], "Revenue increased (unsupported fixture claim)", "model_inference")
        second = bind_summary_draft(self.config, [selector], "Revenue decreased", "human_judgment")
        self.assertNotEqual(first["summary_id"], second["summary_id"])
        self.assertFalse(first["semantic_support_verified"])
        self.assertEqual(first["review_status"], "unreviewed")
        self.assertFalse(first["external_eligible"])
        self.assertEqual(first["source_excerpt_ids"], [self.excerpt()["excerpt_id"]])
        self.assertEqual(first, bind_summary_draft(self.config, [selector], first["text"], "model_inference"))
        with self.assertRaises(EvidenceExcerptError):
            bind_summary_draft(self.config, [selector], "not a quote", "source_statement")

    def test_worker_retains_selection_locally_not_in_outbound_context(self):
        spec = build_task_spec(research_question="Fixture research", symbols=["600519"], config=self.config)
        body = self.text
        start = body.index("BODY_SENTINEL")
        excerpt = create_excerpt(self.config, self.ref, start, start + len("BODY_SENTINEL_NOT_FOR_EGRESS"))
        spec["public_context"]["evidence_summaries"][self.ref]["source_selection"] = excerpt_selector(excerpt)
        safe, delivery = build_safe_research_context(spec, self.config)
        self.assertEqual(delivery["local_excerpt_records"][self.ref], excerpt)
        prompt = build_user_prompt({"task_id": "fixture", "input_version": 1, "input_payload": spec}, {}, safe)
        self.assertNotIn("BODY_SENTINEL_NOT_FOR_EGRESS", prompt)
        self.assertNotIn("local_excerpt_records", prompt)
        self.assertEqual(safe["evidence_summaries"][self.ref]["summary_scope"], "title_metadata_only_not_body_analysis")
        broken = copy.deepcopy(spec)
        broken["public_context"]["evidence_summaries"][self.ref]["source_selection"]["excerpt_sha256"] = "0" * 64
        safe, receipt = build_safe_research_context(broken, self.config)
        self.assertEqual(safe["evidence_summaries"], {})
        self.assertIn(self.ref + ":invalid_source_selection", receipt["dropped_evidence"])

    def test_cli_emits_local_record_and_no_vault_write(self):
        from stock_agent.cli import main
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config), encoding="utf-8")
        ledger = self.root / "vault/events.jsonl"
        before = ledger.read_bytes()
        out = io.StringIO()
        with patch.object(sys, "argv", ["stock_agent", "evidence-excerpt", "--config", str(path),
                                      "--document-id", self.ref, "--start", str(self.start), "--end", str(self.start + 5),
                                      "--summary", "Local draft"]), contextlib.redirect_stdout(out):
            main()
        result = json.loads(out.getvalue())
        self.assertEqual(result["excerpt"], self.excerpt())
        self.assertFalse(result["summary_draft"]["external_eligible"])
        self.assertEqual(ledger.read_bytes(), before)
