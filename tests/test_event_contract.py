"""GLM-02 event timing and price unit contract: separate times, never zero-fill."""
import unittest

from stock_agent.event_contract import (
    build_event_timing,
    build_price_observation,
    convert_cny_amount,
    event_identity,
)


class EventTimingTests(unittest.TestCase):
    def test_three_times_are_kept_separate(self):
        timing = build_event_timing(
            source_time="2026-10-10T18:00:00+08:00",
            observed_at="2026-10-10T18:05:00+08:00",
            fetched_at="2026-10-10T18:30:00+08:00",
        )
        self.assertEqual(timing["source_time"], "2026-10-10T18:00:00+08:00")
        self.assertEqual(timing["observed_at"], "2026-10-10T18:05:00+08:00")
        self.assertEqual(timing["fetched_at"], "2026-10-10T18:30:00+08:00")
        self.assertEqual(timing["source_time_status"], "valid")
        self.assertEqual(timing["observed_at_status"], "valid")
        self.assertTrue(timing["ordering_valid"])

    def test_missing_source_time_is_not_filled_with_fetch_time(self):
        timing = build_event_timing(
            source_time=None,
            observed_at=None,
            fetched_at="2026-10-10T18:30:00+08:00",
        )
        self.assertIsNone(timing["source_time"])
        self.assertEqual(timing["source_time_status"], "missing")
        self.assertEqual(timing["observed_at_status"], "missing")

    def test_future_source_time_is_flagged_and_preserved(self):
        timing = build_event_timing(
            source_time="2027-01-01T00:00:00+08:00",
            observed_at=None,
            fetched_at="2026-10-10T18:30:00+08:00",
        )
        self.assertEqual(timing["source_time_status"], "future")
        self.assertEqual(timing["source_time"], "2027-01-01T00:00:00+08:00")
        self.assertFalse(timing["ordering_valid"])

    def test_unparseable_source_time_is_flagged(self):
        timing = build_event_timing(
            source_time="not-a-time",
            observed_at=None,
            fetched_at="2026-10-10T18:30:00+08:00",
        )
        self.assertEqual(timing["source_time_status"], "unparseable")
        self.assertEqual(timing["source_time"], "not-a-time")

    def test_naive_timestamp_needs_explicit_default_tz(self):
        timing = build_event_timing(
            source_time="2026-10-10T18:00:00",
            observed_at=None,
            fetched_at="2026-10-10T18:30:00+08:00",
        )
        self.assertEqual(timing["source_time_status"], "unparseable")
        timing = build_event_timing(
            source_time="2026-10-10T18:00:00",
            observed_at=None,
            fetched_at="2026-10-10T18:30:00+08:00",
            default_tz="Asia/Shanghai",
        )
        self.assertEqual(timing["source_time_status"], "valid")
        self.assertTrue(timing["source_time"].endswith("+08:00"))
        self.assertEqual(timing["source_time_tz"], "assumed")

    def test_observed_before_source_flags_ordering(self):
        timing = build_event_timing(
            source_time="2026-10-10T18:00:00+08:00",
            observed_at="2026-10-10T17:00:00+08:00",
            fetched_at="2026-10-10T18:30:00+08:00",
        )
        self.assertFalse(timing["ordering_valid"])
        self.assertEqual(timing["ordering_violation"], "observed_before_source")

    def test_fetched_at_is_required(self):
        with self.assertRaises(ValueError):
            build_event_timing(source_time=None, observed_at=None, fetched_at="")


class PriceObservationTests(unittest.TestCase):
    def test_price_observation_records_units_explicitly(self):
        observation = build_price_observation(
            symbol="600519",
            value=1500.5,
            unit="cny",
            adjusted="none",
            observed_at="2026-10-10T15:00:00+08:00",
            source="eastmoney",
        )
        self.assertEqual(observation["value"], 1500.5)
        self.assertEqual(observation["unit"], "cny")
        self.assertEqual(observation["currency"], "cny")
        self.assertEqual(observation["adjusted"], "none")
        self.assertEqual(observation["status"], "valid")

    def test_missing_price_is_none_not_zero(self):
        observation = build_price_observation(
            symbol="600519",
            value=None,
            unit="cny",
            adjusted="none",
            observed_at="2026-10-10T15:00:00+08:00",
            source="eastmoney",
        )
        self.assertIsNone(observation["value"])
        self.assertEqual(observation["status"], "missing")

    def test_unit_is_mandatory_and_whitelisted(self):
        with self.assertRaises(ValueError):
            build_price_observation(
                symbol="600519", value=1.0, unit="yuan",
                adjusted="none", observed_at="2026-10-10T15:00:00+08:00", source="x",
            )

    def test_adjustment_must_be_explicit(self):
        with self.assertRaises(ValueError):
            build_price_observation(
                symbol="600519", value=1.0, unit="cny",
                adjusted=None, observed_at="2026-10-10T15:00:00+08:00", source="x",
            )

    def test_future_observation_is_flagged_not_dropped(self):
        observation = build_price_observation(
            symbol="600519", value=1.0, unit="cny", adjusted="none",
            observed_at="2027-01-01T00:00:00+08:00",
            fetched_at="2026-10-10T18:30:00+08:00", source="eastmoney",
        )
        self.assertEqual(observation["status"], "future_observation")
        self.assertEqual(observation["value"], 1.0)

    def test_convert_cny_amount_is_deterministic(self):
        self.assertEqual(convert_cny_amount(1.5, "cny_yi", "cny"), 150000000.0)
        self.assertEqual(convert_cny_amount(2.0, "cny_wan", "cny"), 20000.0)
        self.assertEqual(convert_cny_amount(5.0, "cny", "cny"), 5.0)
        with self.assertRaises(ValueError):
            convert_cny_amount(1.0, "usd", "cny")


class EventIdentityTests(unittest.TestCase):
    def test_identity_is_stable_and_version_sensitive(self):
        kwargs = dict(source_id="eastmoney_filing", url="https://x.example/a",
                      title="某公司公告")
        first = event_identity(**kwargs)
        self.assertEqual(first, event_identity(**kwargs))
        revised = event_identity(**kwargs, content_version="2")
        self.assertNotEqual(first, revised)
        other_title = event_identity(source_id="eastmoney_filing",
                                     url="https://x.example/a", title="更正：某公司公告")
        self.assertNotEqual(first, other_title)

    def test_identity_requires_source_and_title(self):
        with self.assertRaises(ValueError):
            event_identity(source_id="", url="https://x.example/a", title="t")


if __name__ == "__main__":
    unittest.main()
