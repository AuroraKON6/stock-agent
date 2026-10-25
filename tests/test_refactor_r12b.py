"""R12b: text-layer PDF extraction (A56) — pages/tables/fragments with the
explicit-status contract. Real multi-object PDFs are built in-test (flate and
raw streams, escapes, hex strings, TJ kerning, positioned table rows); every
out-of-subset input is an explicit refusal, never a partial success. The
excerpt round-trip runs against a REAL vault import, not a hand-written index.
"""
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import vault_config

from stock_agent.config import project_path_override
from stock_agent.evidence_excerpts import (
    EvidenceExcerptError,
    PDF_EXTRACTOR_CHAIN,
    create_excerpt,
    excerpt_selector,
    verify_excerpt_selection,
)
from stock_agent.evidence_parsing import extract_body
from stock_agent.evidence_vault import import_official_document


def build_pdf(page_streams, *, flate=None, font=True):
    """Assemble a classic-layout PDF: catalog -> pages tree -> page+content.

    ``flate`` is a per-page bool list (None = all raw). Objects carry no xref
    (the parser scans objects tolerantly, like the fixtures real-world files)."""
    flat = [flate[i] if flate is not None and i < len(flate) else False
            for i in range(len(page_streams))]
    parts = [b"%PDF-1.4\n"]
    next_id = 1
    catalog_id = next_id; next_id += 1
    pages_id = next_id; next_id += 1
    page_ids, content_ids = [], []
    for _ in page_streams:
        page_ids.append(next_id); next_id += 1
        content_ids.append(next_id); next_id += 1
    parts.append(b"%d 0 obj\n<< /Type /Catalog /Pages %d 0 R >>\nendobj\n"
                 % (catalog_id, pages_id))
    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    parts.append(b"%d 0 obj\n<< /Type /Pages /Kids [ %s ] /Count %d >>\nendobj\n"
                 % (pages_id, kids, len(page_ids)))
    for index, stream in enumerate(page_streams):
        font_entry = (b" /Resources << /Font << /F1 9 0 R >> >>" if font else b"")
        parts.append(b"%d 0 obj\n<< /Type /Page /Parent %d 0 R /Contents %d 0 R%s >>\nendobj\n"
                     % (page_ids[index], pages_id, content_ids[index], font_entry))
        body = zlib.compress(stream) if flat[index] else stream
        filter_entry = b" /Filter /FlateDecode" if flat[index] else b""
        parts.append(b"%d 0 obj\n<< /Length %d%s >>\nstream\n%s\nendstream\nendobj\n"
                     % (content_ids[index], len(body), filter_entry, body))
    if font:
        parts.append(b"9 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")
    parts.append(b"trailer\n<< /Root %d 0 R >>\n%%EOF" % catalog_id)
    return b"".join(parts)


class PdfExtractionTests(unittest.TestCase):
    def test_two_pages_text_hashes_and_join(self):
        pdf = build_pdf([
            b"BT /F1 12 Tf 72 720 Td (Revenue 2024: \\(5,318\\) million) Tj 0 -20 Td [(net)-80( profit)] TJ 0 -20 Td <48656C6C6F> Tj ET",
            b"BT /F1 12 Tf 72 720 Td (second page) Tj ET",
        ], flate=[True, False])
        result = extract_body(pdf)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["parser_version"], "pdf-text-v1")
        self.assertEqual(result["page_count"], 2)
        page1, page2 = result["pages"]
        self.assertEqual(page1["text"],
                         "Revenue 2024: (5,318) million\nnet profit\nHello")
        self.assertEqual(page2["text"], "second page")
        self.assertTrue(page1["text_sha256"] != page2["text_sha256"])
        self.assertEqual(page1["row_count"], 3)
        self.assertEqual(result["text"],
                         "Revenue 2024: (5,318) million\nnet profit\nHello\nsecond page")
        self.assertEqual(result["notes"]["page_order"], "pages_tree")
        self.assertEqual(result["notes"]["encoding"], "winansi-cp1252")

    def test_escapes_octal_and_tj_kerning(self):
        pdf = build_pdf([
            b"BT /F1 12 Tf 72 720 Td (\\101\\102\\103 back\\\\slash quote\\'x) Tj 0 -20 Td [(Ker)-40(ned words)] TJ ET",
        ])
        result = extract_body(pdf)
        self.assertEqual(result["status"], "ok")
        self.assertIn("ABC back\\slash quote'x", result["text"])
        self.assertIn("Kerned words", result["text"])

    def test_table_like_flag_on_aligned_rows_only(self):
        table_page = (
            b"BT /F1 10 Tf "
            b"1 0 0 1 72 700 Tm (Item) Tj 1 0 0 1 250 700 Tm (2024) Tj "
            b"1 0 0 1 72 680 Tm (Revenue) Tj 1 0 0 1 250 680 Tm (5318) Tj "
            b"1 0 0 1 72 660 Tm (Profit) Tj 1 0 0 1 250 660 Tm (1120) Tj ET")
        flagged = extract_body(build_pdf([table_page]))
        self.assertTrue(flagged["pages"][0]["table_like"])
        self.assertEqual(flagged["pages"][0]["text"],
                         "Item 2024\nRevenue 5318\nProfit 1120")
        prose = extract_body(build_pdf([
            b"BT /F1 10 Tf 1 0 0 1 72 700 Tm (one long prose paragraph) Tj "
            b"1 0 0 1 72 680 Tm (another line) Tj ET"]))
        self.assertFalse(prose["pages"][0]["table_like"])

    def test_raw_fallback_for_bare_fragment_and_object_scan(self):
        fragment = b"%PDF-1.5\n<</Font<</F1 1 0 R>>>>\nBT /F1 12 Tf (x) Tj ET"
        result = extract_body(fragment)
        self.assertEqual((result["status"], result["text"]), ("ok", "x"))
        self.assertEqual(result["notes"]["page_order"], "raw_fallback")
        # pages present but no /Pages tree: object-number order + honest note
        pdf = (b"%PDF-1.4\n"
               b"7 0 obj\n<< /Type /Page /Contents 8 0 R >>\nendobj\n"
               b"8 0 obj\n<< /Length 44 >>\nstream\nBT /F1 12 Tf (later object) Tj ET\nendstream\nendobj\n"
               b"3 0 obj\n<< /Type /Page /Contents 4 0 R >>\nendobj\n"
               b"4 0 obj\n<< /Length 48 >>\nstream\nBT /F1 12 Tf (earlier object) Tj ET\nendstream\nendobj\n")
        scanned = extract_body(pdf)
        self.assertEqual(scanned["status"], "ok")
        self.assertEqual(scanned["page_count"], 2)
        self.assertEqual(scanned["notes"]["page_order"], "object_scan")
        self.assertEqual(scanned["text"], "earlier object\nlater object")

    def test_negative_inputs_get_explicit_refusals(self):
        scanned = b"%PDF-1.7\n1 0 obj\n<</Type/XObject/Subtype/Image/Width 1/Height 1>>\nendobj\ntrailer\n<<>>"
        self.assertEqual(extract_body(scanned)["status"], "needs_ocr")
        encrypted = build_pdf([b"BT /F1 12 Tf (x) Tj ET"]) + b"\n/Encrypt 9 0 R"
        self.assertEqual(extract_body(encrypted)["status"], "encrypted")
        objstm = build_pdf([b"BT /F1 12 Tf (x) Tj ET"]) + b"\n20 0 obj\n<< /Type /ObjStm >>\nendobj"
        self.assertEqual(extract_body(objstm)["status"], "unsupported_pdf_feature")
        cid = build_pdf([b"BT /F1 12 Tf (x) Tj ET"]) + b"\n21 0 obj\n<< /Subtype /Type0 >>\nendobj"
        self.assertEqual(extract_body(cid)["status"], "unsupported_pdf_feature")
        dct = (b"%PDF-1.4\n1 0 obj\n<< /Type /Page /Contents 2 0 R >>\nendobj\n"
               b"2 0 obj\n<< /Length 3 /Filter /DCTDecode >>\nstream\nabc\nendstream\nendobj\n"
               b"9 0 obj\n<< /Type /Font /Subtype /Type1 >>\nendobj\n")
        self.assertEqual(extract_body(dct)["status"], "unsupported_pdf_feature")
        corrupt = (b"%PDF-1.4\n1 0 obj\n<< /Type /Page /Contents 2 0 R >>\nendobj\n"
                   b"2 0 obj\n<< /Length 9 /Filter /FlateDecode >>\nstream\nnotzlib!!\nendstream\nendobj\n"
                   b"9 0 obj\n<< /Type /Font /Subtype /Type1 >>\nendobj\n")
        self.assertEqual(extract_body(corrupt)["status"], "corrupt")
        empty_text = build_pdf([b"BT /F1 12 Tf ET"])
        self.assertEqual(extract_body(empty_text)["status"], "empty")


class PdfExcerptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root, {})

    def import_pdf(self, pdf_bytes, *, title="PDF fixture"):
        path = self.root / "incoming.pdf"
        path.write_bytes(pdf_bytes)
        result = import_official_document(
            self.config, str(path), source_url="https://www.cninfo.com.cn/fixture-pdf",
            title=title, published_at="2026-09-20T18:00:00+08:00",
            actor="test-evidence-collector", actor_role="information_analyst",
            content_type="application/pdf")
        return (result.get("document") or result)["document_id"]

    def test_excerpt_round_trip_on_pdf_locator_chain(self):
        pdf = build_pdf([
            b"BT /F1 12 Tf 72 720 Td (Company revenue for 2024 grew steadily) Tj "
            b"0 -20 Td (driven by overseas expansion) Tj ET",
            b"BT /F1 12 Tf 72 720 Td (second page content) Tj ET"])
        document_id = self.import_pdf(pdf)
        excerpt = create_excerpt(self.config, document_id, 0, 7)
        self.assertEqual(excerpt["extractor_version"], PDF_EXTRACTOR_CHAIN)
        self.assertEqual(excerpt["text"], "Company revenue for 2024 grew steadily"[:7])
        verified = verify_excerpt_selection(self.config, excerpt_selector(excerpt))
        self.assertEqual(verified["excerpt_id"], excerpt["excerpt_id"])

    def test_tampered_selector_refused(self):
        document_id = self.import_pdf(build_pdf(
            [b"BT /F1 12 Tf 72 720 Td (stable original sentence) Tj ET"]))
        excerpt = create_excerpt(self.config, document_id, 0, 6)
        selector = excerpt_selector(excerpt)
        selector["start"] = 1
        with self.assertRaises(EvidenceExcerptError):
            verify_excerpt_selection(self.config, selector)

    def test_scanned_pdf_excerpt_refused_with_needs_ocr(self):
        scanned = b"%PDF-1.7\n1 0 obj\n<</Type/XObject/Subtype/Image/Width 1/Height 1>>\nendobj\ntrailer\n<<>>"
        document_id = self.import_pdf(scanned, title="scanned fixture")
        with self.assertRaisesRegex(EvidenceExcerptError, "needs_ocr"):
            create_excerpt(self.config, document_id, 0, 5)


if __name__ == "__main__":
    unittest.main()
