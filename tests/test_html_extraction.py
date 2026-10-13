"""GLM-03: controlled local HTML text extraction (stdlib only, no network)."""
import unittest

from stock_agent.evidence_parsing import PARSER_VERSION, extract_body


class HtmlExtractionTests(unittest.TestCase):
    def test_html_text_extracted_scripts_and_styles_dropped(self):
        html = ("<html><head><style>.x{color:red}</style>"
                "<script>alert('BODY_SENTINEL_NOT_EGRESS')</script></head>"
                "<body><h1>年度报告</h1><p>营业收入&nbsp;1,234 万元。</p>"
                "<table><tr><td>净利润</td><td>500 万元</td></tr></table></body></html>")
        result = extract_body(html.encode("utf-8"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["parser_version"], PARSER_VERSION)
        self.assertEqual(result["detected_format"], "html")
        self.assertIn("年度报告", result["text"])
        self.assertIn("营业收入", result["text"])
        self.assertIn("1,234", result["text"])          # entity decoded
        self.assertIn("净利润", result["text"])
        self.assertNotIn("color:red", result["text"])    # style dropped
        self.assertNotIn("alert", result["text"])        # script dropped
        self.assertLessEqual(result["char_count"], len(result["text"]))

    def test_html_size_cap_rejected_without_partial_text(self):
        huge = b"<html><body>" + b"a" * (2 * 1024 * 1024 + 1) + b"</body></html>"
        result = extract_body(huge)
        self.assertEqual(result["status"], "too_large")
        self.assertNotIn("text", result)

    def test_html_whitespace_normalized(self):
        result = extract_body(b"<p>first</p>\n\n   <p>second</p>")
        self.assertEqual(result["status"], "ok")
        # Runs of whitespace collapse to single spaces; text stays readable.
        self.assertNotIn("\n", result["text"])
        self.assertIn("first", result["text"])
        self.assertIn("second", result["text"])

    def test_empty_html_is_empty(self):
        result = extract_body(b"<html><body></body></html>")
        self.assertEqual(result["status"], "empty")

    def test_html_without_tags_still_text(self):
        result = extract_body(b"<!doctype html>plain text body")
        self.assertEqual(result["status"], "ok")
        self.assertIn("plain text body", result["text"])

    def test_chinese_entities_and_attributes_do_not_leak(self):
        html = '<p title="&#x6CDB;&#x6EA2;" lang="zh">正文&#20013;&#25991;</p>'
        result = extract_body(html.encode("utf-8"))
        self.assertEqual(result["status"], "ok")
        self.assertIn("正文中文", result["text"])
        self.assertNotIn("title=", result["text"])


if __name__ == "__main__":
    unittest.main()
