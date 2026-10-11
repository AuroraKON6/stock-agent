import json
import unittest
from unittest.mock import patch

from stock_agent.llm_agent import _call_llm


class _DripResponse:
    def __init__(self):
        self._chunks = [b"{"]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read1(self, _size):
        return self._chunks.pop(0) if self._chunks else b""


class _BufferedResponse:
    def __init__(self, body):
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


class LlmWallClockV23Tests(unittest.TestCase):
    def test_drip_response_cannot_extend_total_deadline(self):
        with patch("stock_agent.llm_agent.urllib.request.urlopen", return_value=_DripResponse()), patch(
            "stock_agent.llm_agent.time.monotonic", side_effect=[0.0, 0.0, 1.1]
        ):
            with self.assertRaisesRegex(TimeoutError, "wall-clock timeout"):
                _call_llm("https://example.test/v1", "model", "key", 1, 0.0, "prompt")

    def test_fully_buffered_test_response_remains_supported(self):
        body = {
            "choices": [{"finish_reason": "stop", "message": {"content": '{"results": []}'}}],
            "usage": {},
        }
        with patch("stock_agent.llm_agent.urllib.request.urlopen", return_value=_BufferedResponse(body)):
            response = _call_llm("https://example.test/v1", "model", "key", 1, 0.0, "prompt")
        self.assertEqual(str(response), '{"results": []}')


if __name__ == "__main__":
    unittest.main()
