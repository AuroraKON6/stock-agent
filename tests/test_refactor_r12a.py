"""R12a: HTML/plain-text body extraction into the production locator chain,
plus the personal evidence namespace (R10 deferral closed here).

Acceptance from GLM_REFACTOR_TASKBOOK_20261020:
- A55: HTML and plain text excerpt/locate CLOSES over the production chain
  (vault import → visible-text extraction → excerpt → verification → fact).
- A57 (pre-OCR half): a scanned PDF is refused with needs_ocr — no fabricated
  body text, no excerpt.
- A58: negative amounts, units and periods survive excerpting VERBATIM (the
  excerpt is a faithful slice; nothing is normalized away).
- A59: corrupt / oversized / binary documents are classified failures with
  honest statuses — no excerpt, no arbitrary fallback writes.
- Personal namespace: owner-isolated root/index/audit; same verification
  machinery via a config view; institutional projection untouched; personal
  facts never claim institutional admission; cross-namespace addressing is
  refused in BOTH directions.
"""
import sys
import tempfile
import unittest
from pathlib import Path

from stock_agent.config import project_path, project_path_override
from stock_agent.evidence_context import verified_evidence_documents
from stock_agent.evidence_excerpts import (
    EXTRACTOR_VERSION,
    HTML_EXTRACTOR_CHAIN,
    EvidenceExcerptError,
    create_excerpt,
    excerpt_selector,
    verify_excerpt_selection,
)
from stock_agent.evidence_facts import build_fact
from stock_agent.evidence_parsing import PARSER_VERSION, extract_body
from stock_agent.personal_evidence import (
    PERSONAL_EXTERNAL_SEND_POLICY,
    PERSONAL_NAMESPACE,
    PERSONAL_RETENTION_POLICY,
    PersonalEvidenceError,
    import_personal_document,
    personal_import_audit,
    personal_root,
    personal_vault_view,
)
from stock_agent.sources import kimi_adapter  # noqa: F401  (namespace sanity import)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import import_document, vault_config

HTML_BODY = (
    "<!DOCTYPE html><html><head><title>甲公司公告</title>"
    "<style>.x{color:red}</style><script>var a=1;</script></head>"
    "<body><h1>2024 年年度报告</h1>"
    "<p>营业收入为 1,234.56 万元，同比 -7.89%。</p>"
    "<p>研发费用为（300.00）万元。</p>"
    "<table><tr><td>净利润</td><td>-1,234.56 万元</td></tr></table>"
    "</body></html>"
)


class R12aVaultTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root)

    def _import_html(self, html: str = HTML_BODY, *, title="甲公司 2024 年度报告"):
        source = self.root / "incoming"
        source.mkdir(parents=True, exist_ok=True)
        path = source / "report.html"
        path.write_text(html, encoding="utf-8")
        result = import_document(self.config, self.root, content=html, title=title)
        return result

    def _document(self, sha_or_ref):
        documents = verified_evidence_documents(self.config)
        for document in documents.values():
            if document["sha256"] == sha_or_ref[1] or document["document_id"] == sha_or_ref[0]:
                return document
        raise AssertionError("imported document not in verified projection")


class A55ProductionClosureTests(R12aVaultTestCase):
    def test_html_excerpt_round_trip_with_version_chain(self):
        ref, sha = self._import_html()
        document = self._document((ref, sha))
        self.assertEqual(document["content_type"], "text/html")
        # The visible text exists and tags/scripts/styles are gone.
        from stock_agent.evidence_excerpts import _document_text

        text, version = _document_text(self.config, document)
        self.assertEqual(version, HTML_EXTRACTOR_CHAIN)
        self.assertIn("营业收入为 1,234.56 万元", text)
        self.assertIn("研发费用为（300.00）万元", text)
        self.assertNotIn("<p>", text)
        self.assertNotIn("var a=1", text)
        self.assertNotIn("color:red", text)
        # Excerpt closes the chain: create → verify replays the pipeline.
        start = text.index("营业收入")
        end = text.index("。", start) + 1
        excerpt = create_excerpt(self.config, document["document_id"], start, end)
        self.assertEqual(excerpt["extractor_version"], HTML_EXTRACTOR_CHAIN)
        self.assertIn("1,234.56", excerpt["text"])
        verified = verify_excerpt_selection(self.config, excerpt_selector(excerpt))
        self.assertEqual(verified["excerpt_id"], excerpt["excerpt_id"])
        self.assertGreaterEqual(excerpt["line_start"], 1)

    def test_html_fact_binds_to_located_excerpt(self):
        ref, sha = self._import_html()
        document = self._document((ref, sha))
        from stock_agent.evidence_excerpts import _document_text

        text, _ = _document_text(self.config, document)
        start = text.index("研发费用为")
        end = text.index("。", start) + 1
        excerpt = create_excerpt(self.config, document["document_id"], start, end)
        fact = build_fact(self.config, excerpt_selector(excerpt),
                          fact_type="statement",
                          statement="研发费用为（300.00）万元。")
        self.assertTrue(fact["fact_id"].startswith("fact:sha256:"))
        self.assertEqual(fact["excerpt_sha256"], excerpt["excerpt_sha256"])
        self.assertEqual(fact["parser_version"], HTML_EXTRACTOR_CHAIN)

    def test_plain_text_selectors_stay_byte_compatible(self):
        body = "\ufeff标题\n营业收入为 1.2 亿元。\n"
        ref, sha = import_document(self.config, self.root, content=body,
                                   title="Fixture plain report")
        document = self._document((ref, sha))
        self.assertEqual(document["content_type"], "text/plain")
        text = project_path(document["object_path"]).read_bytes().decode("utf-8-sig")
        start = text.index("营业收入")
        end = text.index("。") + 1
        excerpt = create_excerpt(self.config, document["document_id"], start, end)
        # The historical extractor version — old selectors still verify.
        self.assertEqual(excerpt["extractor_version"], EXTRACTOR_VERSION)
        verified = verify_excerpt_selection(self.config, excerpt_selector(excerpt))
        self.assertEqual(verified["excerpt_id"], excerpt["excerpt_id"])


class A57A59RefusalTests(R12aVaultTestCase):
    def test_a57_scanned_pdf_refused_needs_ocr_no_excerpt(self):
        # A minimal PDF without /Font or BT text operators = scanned shape.
        scanned = b"%PDF-1.4\n%<binary image only>\n%%EOF\n"
        source = self.root / "incoming"
        source.mkdir(parents=True, exist_ok=True)
        path = source / "scan.pdf"
        path.write_bytes(scanned)
        parsed = extract_body(scanned)
        self.assertEqual(parsed["status"], "needs_ocr")
        ref, sha = import_document(self.config, self.root, content=scanned.decode("latin-1"),
                                   title="扫描件")
        document = self._document((ref, sha))
        with self.assertRaises(EvidenceExcerptError) as caught:
            create_excerpt(self.config, document["document_id"], 0, 10)
        # PDFs are refused by format here; a scan additionally reports
        # needs_ocr from the extraction pipeline — either way, NO excerpt.
        self.assertIn(str(caught.exception),
                      {"unsupported format: only retained text/plain and text/html are supported",
                       "body extraction refused: needs_ocr"})

    def test_a59_binary_garbage_refused_with_status(self):
        garbage = bytes(range(256)) * 4  # definitely not UTF-8
        parsed = extract_body(garbage)
        self.assertIn(parsed["status"], {"not_utf8", "unsupported_content"})
        ref, sha = import_document(self.config, self.root,
                                   content=garbage.decode("latin-1"), title="乱码")
        document = self._document((ref, sha))
        with self.assertRaises(EvidenceExcerptError):
            create_excerpt(self.config, document["document_id"], 0, 10)

    def test_a59_oversize_refused(self):
        parsed = extract_body(b"a" * (2 * 1024 * 1024 + 1))
        self.assertEqual(parsed["status"], "too_large")

    def test_a59_empty_document_refused(self):
        parsed = extract_body(b"")
        self.assertEqual(parsed["status"], "empty")


class A58FidelityTests(R12aVaultTestCase):
    def test_negative_amounts_units_periods_survive_verbatim(self):
        ref, sha = self._import_html()
        document = self._document((ref, sha))
        from stock_agent.evidence_excerpts import _document_text

        text, _ = _document_text(self.config, document)
        for fragment in ("1,234.56 万元", "-7.89%", "（300.00）万元",
                         "-1,234.56 万元", "2024 年年度报告"):
            with self.subTest(fragment=fragment):
                start = text.index(fragment)
                excerpt = create_excerpt(self.config, document["document_id"],
                                         start, start + len(fragment))
                # The excerpt is a FAITHFUL slice: signs, units, parentheses
                # and period text pass through untouched.
                self.assertEqual(excerpt["text"], fragment)
                verified = verify_excerpt_selection(self.config, excerpt_selector(excerpt))
                self.assertEqual(verified["text"], fragment)


class PersonalNamespaceTests(R12aVaultTestCase):
    def test_import_isolates_roots_and_records_policy_audit(self):
        result = import_personal_document(
            self.config, "lead-analyst", str(self._html_path()),
            source_url="https://www.cninfo.com.cn/new/disclosure/detail?plate=szse",
            title="个人研究用公告", published_at="2026-10-22T18:00:00+08:00")
        personal_doc = result["document"]
        # Isolated root: personal objects live under the owner's hashed root.
        personal_doc_path = project_path(str(personal_doc["object_path"]))
        self.assertIn(str(personal_root(self.config, "lead-analyst")),
                      str(personal_doc_path))
        # Policy/audit record carries the R10 fields.
        audit = personal_import_audit(self.config, "lead-analyst")
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["namespace"], PERSONAL_NAMESPACE)
        self.assertEqual(audit[0]["owner"], "lead-analyst")
        self.assertEqual(audit[0]["retention_policy"], PERSONAL_RETENTION_POLICY)
        self.assertFalse(audit[0]["external_send_allowed"])
        self.assertEqual(audit[0]["institutional_admission"], "NOT_REQUESTED")

    def test_personal_documents_invisible_to_institutional_projection(self):
        import_personal_document(
            self.config, "lead-analyst", str(self._html_path()),
            source_url="https://www.cninfo.com.cn/x", title="个人公告",
            published_at="2026-10-22T18:00:00+08:00")
        institutional = verified_evidence_documents(self.config)
        self.assertEqual(institutional, {},
                         "the institutional projection must not see personal imports")

    def test_personal_excerpt_and_fact_close_within_namespace(self):
        result = import_personal_document(
            self.config, "lead-analyst", str(self._html_path()),
            source_url="https://www.cninfo.com.cn/x", title="个人公告",
            published_at="2026-10-22T18:00:00+08:00")
        document = result["document"]
        view = personal_vault_view(self.config, "lead-analyst")
        from stock_agent.evidence_excerpts import _document_text

        text, version = _document_text(view, document)
        self.assertEqual(version, HTML_EXTRACTOR_CHAIN)
        start = text.index("营业收入")
        end = text.index("。", start) + 1
        excerpt = create_excerpt(view, document["document_id"], start, end)
        verified = verify_excerpt_selection(view, excerpt_selector(excerpt))
        self.assertEqual(verified["excerpt_id"], excerpt["excerpt_id"])
        fact = build_fact(view, excerpt_selector(excerpt), fact_type="statement",
                          statement="营业收入为 1,234.56 万元。")
        self.assertTrue(fact["fact_id"].startswith("fact:sha256:"))

    def test_cross_namespace_addressing_refused_both_ways(self):
        personal = import_personal_document(
            self.config, "lead-analyst", str(self._html_path()),
            source_url="https://www.cninfo.com.cn/x", title="个人公告",
            published_at="2026-10-22T18:00:00+08:00")["document"]
        institutional_ref, institutional_sha = import_document(
            self.config, self.root, content="机构侧文本", title="机构公告")
        institutional_document = self._document((institutional_ref, institutional_sha))
        # Personal doc id under the INSTITUTIONAL config: invisible.
        with self.assertRaises(EvidenceExcerptError):
            create_excerpt(self.config, personal["document_id"], 0, 5)
        # Institutional doc id under the PERSONAL view: invisible.
        view = personal_vault_view(self.config, "lead-analyst")
        with self.assertRaises(EvidenceExcerptError):
            create_excerpt(view, institutional_document["document_id"], 0, 5)

    def test_owner_isolation_between_personal_roots(self):
        import_personal_document(
            self.config, "lead-analyst", str(self._html_path()),
            source_url="https://www.cninfo.com.cn/x", title="A 的公告",
            published_at="2026-10-22T18:00:00+08:00")
        other_view = personal_vault_view(self.config, "other-analyst")
        from stock_agent.evidence_excerpts import _document_text

        # The other owner sees nothing: an empty projection has no document.
        self.assertEqual(verified_evidence_documents(other_view), {})

    def test_automated_owner_rejected(self):
        with self.assertRaises(PersonalEvidenceError):
            import_personal_document(
                self.config, "dify:cred-1", str(self._html_path()),
                source_url="https://x", title="t", published_at="2026-10-22T00:00:00+08:00")

    # helper ------------------------------------------------------------- #

    def _html_path(self) -> Path:
        source = self.root / "personal_incoming"
        source.mkdir(parents=True, exist_ok=True)
        path = source / "report.html"
        if not path.exists():
            path.write_text(HTML_BODY, encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
