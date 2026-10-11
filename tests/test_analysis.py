import unittest

from stock_agent.analysis import analyze_one
from stock_agent.models import RawSignal


class AnalysisTests(unittest.TestCase):
    def test_high_priority_rumor_needs_review(self):
        config = {
            "topics": [
                {
                    "name": "AI算力",
                    "symbols": ["浪潮信息"],
                    "keywords": ["算力", "服务器"],
                    "risk_keywords": ["订单取消"],
                }
            ],
            "priority_rules": {
                "p0_keywords": ["停牌"],
                "p1_keywords": ["订单取消"],
                "positive_keywords": ["订单"],
                "negative_keywords": ["取消"],
                "review_keywords": ["网传"],
            },
        }
        signal = RawSignal(
            source="manual",
            source_type="social",
            title="网传浪潮信息订单取消",
            summary="消息未证实，需要核验。",
        )
        analyzed = analyze_one(signal, config)
        self.assertEqual(analyzed.priority, "P1")
        self.assertTrue(analyzed.needs_review)
        self.assertIn("AI算力", analyzed.topics)
        self.assertIn("浪潮信息", analyzed.symbols)


if __name__ == "__main__":
    unittest.main()

