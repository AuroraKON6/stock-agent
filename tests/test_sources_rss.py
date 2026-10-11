from __future__ import annotations

import io
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from stock_agent.sources.rss import _has_doctype_in_prolog, collect_rss


def _feed(body: bytes) -> bytes:
    return body


def _fake_urlopen(payload: bytes):
    @contextmanager
    def _opener(request, timeout=0, context=None):
        yield io.BytesIO(payload)

    return _opener


DOCTYPE_RSS = _feed(
    b"<?xml version='1.0'?>\n"
    b"<!DOCTYPE rss [\n"
    b"  <!ENTITY xxe 'repeat'>\n"
    b"]>\n"
    b"<rss version='2.0'><channel><item><title>t</title></item></channel></rss>"
)

PLAIN_RSS = _feed(
    b"<?xml version='1.0'?>\n"
    b"<rss version='2.0'><channel><title>channel</title>"
    b"<item><title>signal one</title><description>body text</description>"
    b"<link>https://example.com/a</link>"
    b"<pubDate>Mon, 24 Aug 2026 01:02:03 +0800</pubDate>"
    b"<guid>guid-1</guid></item></channel></rss>"
)

DOCTYPE_MENTION_IN_TEXT = _feed(
    b"<?xml version='1.0'?>\n"
    b"<rss version='2.0'><channel>"
    b"<item><title>about &lt;!DOCTYPE html&gt;</title></item></channel></rss>"
)

ATOM_WITH_DTD_TEXT = _feed(
    b"<?xml version='1.0' encoding='utf-8'?>\n"
    b"<feed xmlns='http://www.w3.org/2005/Atom'>"
    b"<entry><title>entry title</title>"
    b"<link href='https://example.com/b' />"
    b"<updated>2026-08-24T01:02:03Z</updated></entry></feed>"
)


class HasDoctypeInPrologTest(unittest.TestCase):
    def test_doctype_after_xml_declaration_detected(self):
        self.assertTrue(_has_doctype_in_prolog(DOCTYPE_RSS))

    def test_doctype_without_xml_declaration_detected(self):
        self.assertTrue(_has_doctype_in_prolog(b"<!DOCTYPE rss SYSTEM \"x.dtd\"><rss/>"))

    def test_case_insensitive_and_whitespace_tolerant(self):
        self.assertTrue(_has_doctype_in_prolog(b"<?xml version='1.0'?>   \n  <!doctype rss><rss/>"))

    def test_plain_feed_not_flagged(self):
        self.assertFalse(_has_doctype_in_prolog(PLAIN_RSS))

    def test_doctype_mention_inside_text_not_flagged(self):
        self.assertFalse(_has_doctype_in_prolog(DOCTYPE_MENTION_IN_TEXT))


class CollectRssDoctypeGuardTest(unittest.TestCase):
    def test_doctype_feed_degrades_to_source_error(self):
        with patch("urllib.request.urlopen", _fake_urlopen(DOCTYPE_RSS)):
            signals = collect_rss(
                [{"name": "wire", "url": "https://example.com/rss", "enabled": True}],
                timeout=1,
            )
        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal.source_type, "source_error")
        self.assertIn("DOCTYPE", signal.summary)
        self.assertEqual(signal.url, "https://example.com/rss")

    def test_plain_rss_still_parses(self):
        with patch("urllib.request.urlopen", _fake_urlopen(PLAIN_RSS)):
            signals = collect_rss(
                [{"name": "wire", "url": "https://example.com/rss", "source_type": "news", "enabled": True}],
                timeout=1,
            )
        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal.source, "wire")
        self.assertEqual(signal.source_type, "news")
        self.assertEqual(signal.title, "signal one")
        self.assertEqual(signal.summary, "body text")
        self.assertEqual(signal.url, "https://example.com/a")
        self.assertIsNotNone(signal.published_at)
        self.assertEqual(signal.payload.get("guid"), "guid-1")

    def test_atom_still_parses(self):
        with patch("urllib.request.urlopen", _fake_urlopen(ATOM_WITH_DTD_TEXT)):
            signals = collect_rss(
                [{"name": "atom-wire", "url": "https://example.com/atom", "source_type": "news", "enabled": True}],
                timeout=1,
            )
        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal.title, "entry title")
        self.assertEqual(signal.url, "https://example.com/b")
        self.assertIsNotNone(signal.published_at)


if __name__ == "__main__":
    unittest.main()
