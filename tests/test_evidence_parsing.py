"""GLM-03 body extraction statuses: explicit NEEDS_OCR, no fake extraction."""
import unittest

from stock_agent.evidence_parsing import PARSER_VERSION, extract_body


class BodyExtractionTests(unittest.TestCase):
    def test_utf8_body_extracted_with_parser_version(self):
        result = extract_body("营业收入下降。BODY_SENTINEL".encode("utf-8"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["parser_version"], PARSER_VERSION)
        self.assertEqual(result["text"], "营业收入下降。BODY_SENTINEL")
        self.assertEqual(result["char_count"], len("营业收入下降。BODY_SENTINEL"))
        self.assertEqual(result["byte_size"], len("营业收入下降。BODY_SENTINEL".encode("utf-8")))

    def test_bom_is_stripped(self):
        result = extract_body(b"\xef\xbb\xbfheading")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["text"], "heading")

    def test_empty_body_is_explicit_empty(self):
        result = extract_body(b"")
        self.assertEqual(result["status"], "empty")
        self.assertNotIn("text", result)

    def test_non_utf8_is_rejected_without_lossy_decode(self):
        result = extract_body(b"\xd3\xd0\xcf\xde\xb9\xab\xcb\xbe")
        self.assertEqual(result["status"], "not_utf8")
        self.assertNotIn("text", result)

    def test_oversize_body_rejected_without_partial_text(self):
        result = extract_body(b"a" * (2 * 1024 * 1024 + 1))
        self.assertEqual(result["status"], "too_large")
        self.assertNotIn("text", result)

    def test_scanned_pdf_reports_needs_ocr(self):
        scanned = b"%PDF-1.7\n1 0 obj\n<</Type/XObject/Subtype/Image/Width 1/Height 1>>\nendobj\ntrailer\n<<>>"
        result = extract_body(scanned)
        self.assertEqual(result["status"], "needs_ocr")
        self.assertNotIn("text", result)
        self.assertEqual(result["detected_format"], "pdf")

    def test_pdf_with_text_layer_is_unsupported_not_ocr(self):
        with_text = b"%PDF-1.5\n<</Font<</F1 1 0 R>>>>\nBT /F1 12 Tf (x) Tj ET"
        result = extract_body(with_text)
        self.assertEqual(result["status"], "unsupported_format")
        self.assertEqual(result["detected_format"], "pdf")
        self.assertIn("text layer", result["reason"])

    def test_html_is_unsupported_for_now(self):
        result = extract_body(b"<!doctype html><html><body>x</body></html>")
        self.assertEqual(result["status"], "unsupported_format")
        self.assertEqual(result["detected_format"], "html")

    def test_control_characters_rejected(self):
        result = extract_body(b"abc\x00def")
        self.assertEqual(result["status"], "unsupported_content")
        self.assertNotIn("text", result)

    def test_result_is_json_serializable_snapshot(self):
        import json
        payload = json.dumps(extract_body("收入".encode("utf-8")), ensure_ascii=False)
        self.assertIn("ok", payload)


if __name__ == "__main__":
    unittest.main()
