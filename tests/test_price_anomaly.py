"""Tests for price anomaly detection in signal analysis."""
import unittest

from stock_agent.analysis import analyze_one, _score_price_anomaly
from stock_agent.models import RawSignal, now_iso


TOPICS_CONFIG = [
    {
        "name": "AI算力",
        "symbols": ["浪潮信息", "中科曙光"],
        "keywords": ["算力", "服务器"],
        "risk_keywords": ["订单取消"],
    }
]

PRIORITY_RULES = {
    "p0_keywords": ["停牌"],
    "p1_keywords": ["订单取消"],
    "positive_keywords": ["订单"],
    "negative_keywords": ["取消"],
    "review_keywords": [],
    "min_priority_for_alert": "P1",
    "price_rules": {
        "pct_change_abs_threshold": 3.0,
        "main_net_inflow_threshold": 100000000,
        "main_net_outflow_threshold": -100000000,
        "priority_for_single_price_anomaly": "P2",
        "priority_for_multi_price_anomaly": "P1",
    },
}


def _make_price_signal(title, pct_change=None, main_net_inflow=None, symbols=None):
    payload = {}
    if pct_change is not None:
        payload["pct_change"] = pct_change
    if main_net_inflow is not None:
        payload["main_net_inflow"] = main_net_inflow
    if symbols:
        payload["name"] = symbols[0]
    return RawSignal(
        source="eastmoney",
        source_type="price",
        title=title,
        summary="行情快照",
        url="",
        published_at=now_iso(),
        payload=payload,
    )


class TestPriceAnomalyNormal(unittest.TestCase):
    """Normal price movement — no anomaly, stays P3."""

    def test_small_change_no_anomaly(self):
        signal = _make_price_signal("浪潮信息行情", pct_change=0.5, main_net_inflow=10000000)
        config = {"topics": TOPICS_CONFIG, "priority_rules": PRIORITY_RULES}
        result = analyze_one(signal, config)
        self.assertEqual(result.priority, "P3")
        anomaly_reasons = [r for r in result.reasons if "异常" in r or "显著" in r]
        self.assertEqual(len(anomaly_reasons), 0)


class TestPriceAnomalySingle(unittest.TestCase):
    """Single anomaly → P2."""

    def test_large_pct_up(self):
        signal = _make_price_signal("浪潮信息行情", pct_change=4.5)
        config = {"topics": TOPICS_CONFIG, "priority_rules": PRIORITY_RULES}
        result = analyze_one(signal, config)
        self.assertEqual(result.priority, "P2")
        self.assertTrue(any("显著上涨" in r for r in result.reasons))

    def test_large_pct_down(self):
        signal = _make_price_signal("浪潮信息行情", pct_change=-3.8)
        config = {"topics": TOPICS_CONFIG, "priority_rules": PRIORITY_RULES}
        result = analyze_one(signal, config)
        self.assertEqual(result.priority, "P2")
        self.assertTrue(any("显著下跌" in r for r in result.reasons))

    def test_large_inflow(self):
        signal = _make_price_signal("浪潮信息行情", main_net_inflow=150000000)
        config = {"topics": TOPICS_CONFIG, "priority_rules": PRIORITY_RULES}
        result = analyze_one(signal, config)
        self.assertEqual(result.priority, "P2")
        self.assertTrue(any("显著流入" in r for r in result.reasons))

    def test_large_outflow(self):
        signal = _make_price_signal("浪潮信息行情", main_net_inflow=-200000000)
        config = {"topics": TOPICS_CONFIG, "priority_rules": PRIORITY_RULES}
        result = analyze_one(signal, config)
        self.assertEqual(result.priority, "P2")
        self.assertTrue(any("显著流出" in r for r in result.reasons))


class TestPriceAnomalyMulti(unittest.TestCase):
    """Multi anomaly + topic match → P1."""

    def test_pct_and_inflow_with_topic(self):
        signal = _make_price_signal("中科曙光行情", pct_change=5.0, main_net_inflow=300000000)
        config = {"topics": TOPICS_CONFIG, "priority_rules": PRIORITY_RULES}
        result = analyze_one(signal, config)
        self.assertEqual(result.priority, "P1")
        self.assertTrue(any("多指标异常" in r for r in result.reasons))

    def test_pct_and_outflow_with_topic(self):
        signal = _make_price_signal("中科曙光行情", pct_change=-4.0, main_net_inflow=-150000000)
        config = {"topics": TOPICS_CONFIG, "priority_rules": PRIORITY_RULES}
        result = analyze_one(signal, config)
        self.assertEqual(result.priority, "P1")

    def test_multi_without_topic_stays_p2(self):
        signal = _make_price_signal("某无名股行情", pct_change=5.0, main_net_inflow=300000000)
        config = {"topics": TOPICS_CONFIG, "priority_rules": PRIORITY_RULES}
        result = analyze_one(signal, config)
        # No topic match, so single anomaly scoring
        self.assertEqual(result.priority, "P2")


class TestPriceAnomalySafeDefaults(unittest.TestCase):
    """Safe defaults when price_rules is missing from config."""

    def test_missing_price_rules_uses_defaults(self):
        signal = _make_price_signal("浪潮信息行情", pct_change=4.0)
        rules = {
            "p0_keywords": [],
            "p1_keywords": [],
            "positive_keywords": [],
            "negative_keywords": [],
            "review_keywords": [],
            "min_priority_for_alert": "P1",
            # No price_rules key
        }
        config = {"topics": TOPICS_CONFIG, "priority_rules": rules}
        result = analyze_one(signal, config)
        self.assertEqual(result.priority, "P2")
        self.assertTrue(any("显著上涨" in r for r in result.reasons))

    def test_empty_price_rules_uses_defaults(self):
        signal = _make_price_signal("浪潮信息行情", main_net_inflow=-200000000)
        rules = {
            "p0_keywords": [],
            "p1_keywords": [],
            "positive_keywords": [],
            "negative_keywords": [],
            "review_keywords": [],
            "min_priority_for_alert": "P1",
            "price_rules": {},
        }
        config = {"topics": TOPICS_CONFIG, "priority_rules": rules}
        result = analyze_one(signal, config)
        self.assertEqual(result.priority, "P2")

    def test_non_price_signal_ignores_anomaly(self):
        signal = RawSignal(
            source="manual",
            source_type="news",
            title="浪潮信息新闻",
            summary="普通新闻",
            url="http://example.com",
            published_at=now_iso(),
            payload={"pct_change": 10.0, "main_net_inflow": 999000000},
        )
        config = {"topics": TOPICS_CONFIG, "priority_rules": PRIORITY_RULES}
        result = analyze_one(signal, config)
        # news source_type should not trigger price anomaly
        self.assertNotEqual(result.priority, "P1")


if __name__ == "__main__":
    unittest.main()
