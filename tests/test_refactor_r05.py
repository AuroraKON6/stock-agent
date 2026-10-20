"""R05: deterministic fact calculators + claim/receipt separation.

Acceptance scenarios from GLM_REFACTOR_TASKBOOK_20261020 (A26–A29), plus the
supporting invariants: whitelisted operations only (free-form formulas are
prose), Decimal recomputation with explicit rounding, NaN/Infinity rejected
at every door, missing values never zero, unit consistency enforced, and the
claim-vs-verified-receipt split. All offline; vault fixtures in temp roots.
"""
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

from stock_agent.config import project_path, project_path_override
from stock_agent.evidence_context import verified_evidence_documents
from stock_agent.evidence_excerpts import create_excerpt
from stock_agent.evidence_facts import build_fact, convert_amount_to_cny
from stock_agent.fact_calculators import (
    FactCalculationError,
    execute_operation,
)
from stock_agent.facts_service import (
    FactVerificationError,
    attach_verified_receipt,
    verify_computed_fact,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import import_document, vault_config


class R05FactFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root)
        self.body = "\ufeff标题\n营业收入为 1.2 亿元。\n上年营业收入为 1.0 亿元。\n研发费用为 3000 万元。"
        self.ref, self.sha = import_document(self.config, self.root, content=self.body,
                                             title="Fixture annual report")
        document = verified_evidence_documents(self.config)[self.sha]
        self.text = project_path(document["object_path"]).read_bytes().decode("utf-8-sig")

    def selector_for(self, needle: str) -> dict:
        excerpt = create_excerpt(self.config, self.ref,
                                 self.text.index(needle),
                                 self.text.index("。", self.text.index(needle)) + 1)
        return {
            "document_id": excerpt["document_id"],
            "document_sha256": excerpt["document_sha256"],
            "extractor_version": excerpt["extractor_version"],
            "source_text_sha256": excerpt["source_text_sha256"],
            "start": excerpt["start"], "end": excerpt["end"],
            "excerpt_sha256": excerpt["excerpt_sha256"],
        }

    def amount(self, statement: str, value, unit: str, *, period: str = "",
               needle: str = "营业收入", computed=None) -> dict:
        return build_fact(
            self.config, self.selector_for(needle), fact_type="amount",
            statement=statement, value=value, unit=unit, period=period,
            computed=computed)


class WhitelistTests(R05FactFixture):
    def test_free_form_formula_is_prose_not_a_claim(self):
        with self.assertRaises(ValueError) as caught:
            self.amount("营业收入（元）", 120000000.0, "cny",
                        computed={"formula": "a*100000000",
                                  "input_fact_ids": ["fact:sha256:" + "0" * 64]})
        self.assertIn("operation", str(caught.exception))

    def test_unknown_operation_rejected(self):
        with self.assertRaises(FactCalculationError):
            execute_operation("eval_magic", inputs=[])

    def test_amount_fact_rejects_nan_and_infinity(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.amount("坏值", bad, "cny_yi")
        with self.assertRaises(ValueError):
            convert_amount_to_cny(float("nan"), "cny_yi")


class A27UnitConversionTests(R05FactFixture):
    def test_yi_wan_yuan_conversion_exact_with_scope_preserved(self):
        yi = self.amount("营业收入", 1.2, "cny_yi", period="2024FY")
        receipt = execute_operation(
            "convert_unit", inputs=[yi], params={"to_unit": "cny"})
        self.assertEqual(receipt["result"]["decimal_text"], "120000000.000000")
        self.assertEqual(receipt["result"]["unit"], "cny")
        self.assertEqual(receipt["inputs"][0]["period"], "2024FY",
                         "conversion preserves the period/口径, never resets it")
        wan = execute_operation("convert_unit", inputs=[yi], params={"to_unit": "cny_wan"})
        self.assertEqual(wan["result"]["value"], 12000.0)
        # Legacy helper stays consistent (exact power-of-ten).
        self.assertEqual(convert_amount_to_cny(1.2, "cny_yi"), 120000000.0)

    def test_conversion_to_unknown_unit_rejected(self):
        yi = self.amount("营业收入", 1.2, "cny_yi")
        with self.assertRaises(FactCalculationError):
            execute_operation("convert_unit", inputs=[yi], params={"to_unit": "usd"})


class A28NumericHygieneTests(R05FactFixture):
    def test_missing_input_value_is_an_error_not_zero(self):
        base = {"fact_id": "fact:sha256:x", "fact_type": "amount",
                "value": None, "unit": "cny_yi", "period": "2024FY"}
        other = self.amount("营收", 1.2, "cny_yi", period="2024FY")
        with self.assertRaises(FactCalculationError) as caught:
            execute_operation("sum", inputs=[base, other])
        self.assertIn("missing is not zero", str(caught.exception))

    def test_division_by_zero_rejected_not_infinity(self):
        a = self.amount("本期", 1.2, "cny_yi", period="2024FY")
        zero = self.amount("上年", 0.0, "cny_yi", period="2024FY")
        for operation in ("ratio", "yoy_growth"):
            with self.subTest(operation=operation):
                with self.assertRaises(FactCalculationError) as caught:
                    execute_operation(operation, inputs=[a, zero])
                self.assertIn("zero", str(caught.exception))

    def test_nan_and_infinity_inputs_rejected_by_calculators(self):
        for bad in (float("nan"), float("inf")):
            with self.subTest(value=bad):
                poison = {"fact_id": "fact:sha256:p", "value": bad,
                          "unit": "cny_yi", "period": "2024FY"}
                with self.assertRaises(FactCalculationError):
                    execute_operation("sum", inputs=[poison])

    def test_decimal_string_inputs_are_accepted_and_finite_checked(self):
        from_str = {"fact_id": "fact:sha256:s", "value": "1.25",
                    "unit": "cny_yi", "period": "2024FY"}
        receipt = execute_operation("convert_unit", inputs=[from_str],
                                    params={"to_unit": "cny_wan"})
        self.assertEqual(receipt["result"]["value"], 12500.0)
        poison = {"fact_id": "fact:sha256:t", "value": "NaN",
                  "unit": "cny_yi", "period": "2024FY"}
        with self.assertRaises(FactCalculationError):
            execute_operation("convert_unit", inputs=[poison], params={"to_unit": "cny"})


class A29PeriodMixingTests(R05FactFixture):
    def test_mixed_periods_block_comparable_operations_by_default(self):
        current = self.amount("本期营收", 1.2, "cny_yi", period="2024FY", needle="营业收入")
        prior = self.amount("上年营收", 1.0, "cny_yi", period="2023FY", needle="上年营业收入")
        with self.assertRaises(FactCalculationError) as caught:
            execute_operation("yoy_growth", inputs=[current, prior])
        self.assertIn("periods", str(caught.exception))

    def test_explicit_allow_records_the_exception_in_the_receipt(self):
        current = self.amount("本期营收", 1.2, "cny_yi", period="2024FY", needle="营业收入")
        prior = self.amount("上年营收", 1.0, "cny_yi", period="2023FY", needle="上年营业收入")
        receipt = execute_operation("yoy_growth", inputs=[current, prior],
                                    allow_mixed_periods=True)
        self.assertTrue(receipt["allow_mixed_periods"],
                        "an allowed exception must be auditable, not silent")
        self.assertEqual(receipt["result"]["value"], 0.2)
        self.assertEqual(receipt["input_periods"], ["2023FY", "2024FY"])

    def test_same_period_comparable_operations_pass(self):
        a = self.amount("营收一", 1.2, "cny_yi", period="2024FY")
        b = self.amount("营收二", 1.0, "cny_yi", period="2024FY", needle="上年营业收入")
        receipt = execute_operation("difference", inputs=[a, b])
        self.assertEqual(receipt["result"]["value"], 0.2)
        self.assertEqual(receipt["result"]["unit"], "cny_yi")

    def test_unit_mismatch_blocks_comparable_operations(self):
        yi = self.amount("营收", 1.2, "cny_yi", period="2024FY")
        wan = self.amount("研发费用", 3000.0, "cny_wan", period="2024FY", needle="研发费用")
        with self.assertRaises(FactCalculationError):
            execute_operation("sum", inputs=[yi, wan])


class A26ClaimVerificationTests(R05FactFixture):
    def _derived(self, base: dict, *, claimed, unit="cny", period="2024FY",
                 operation="convert_unit", params=None, needle="营业收入") -> dict:
        return self.amount(
            "营业收入（元）", claimed, unit, period=period, needle=needle,
            computed={"operation": operation,
                      "input_fact_ids": [base["fact_id"]],
                      "params": params or {"to_unit": "cny"}})

    def test_fake_input_fact_id_refuses_verification(self):
        base = self.amount("营业收入", 1.2, "cny_yi")
        forged = dict(self._derived(base, claimed=120000000.0))
        forged["computation"] = {
            "operation": "convert_unit",
            "input_fact_ids": ["fact:sha256:" + "f" * 64],
            "params": {"to_unit": "cny"},
        }
        verdict = verify_computed_fact(forged, {base["fact_id"]: base})
        self.assertFalse(verdict["verified"])
        self.assertEqual(verdict["reason"], "unknown_input_fact")
        with self.assertRaises(FactVerificationError):
            attach_verified_receipt(forged, verdict)

    def test_value_mismatch_refuses_and_claims_no_receipt(self):
        base = self.amount("营业收入", 1.2, "cny_yi")
        wrong = self._derived(base, claimed=999000000.0)
        verdict = verify_computed_fact(wrong, {base["fact_id"]: base})
        self.assertFalse(verdict["verified"])
        self.assertEqual(verdict["reason"], "value_mismatch")
        self.assertEqual(verdict["recomputed"], 120000000.0)
        self.assertNotIn("verified_computation", wrong)

    def test_happy_path_attaches_verified_receipt(self):
        base = self.amount("营业收入", 1.2, "cny_yi", period="2024FY")
        derived = self._derived(base, claimed=120000000.0)
        verdict = verify_computed_fact(derived, {base["fact_id"]: base})
        self.assertTrue(verdict["verified"], verdict)
        updated = attach_verified_receipt(derived, verdict)
        receipt = updated["verified_computation"]
        self.assertTrue(updated["computation_verified"])
        self.assertEqual(receipt["operation"], "convert_unit")
        self.assertEqual(receipt["computed_with"], "decimal")
        self.assertEqual(receipt["rounding"]["mode"], "ROUND_HALF_UP")
        self.assertEqual(receipt["inputs"][0]["fact_id"], base["fact_id"])
        # The receipt keeps the actual inputs so the chain is re-auditable.
        self.assertEqual(receipt["inputs"][0]["value"], 1.2)
        self.assertEqual(receipt["inputs"][0]["unit"], "cny_yi")

    def test_self_reference_and_unit_mismatch_refuse(self):
        base = self.amount("营业收入", 1.2, "cny_yi")
        self_referring = self._derived(base, claimed=120000000.0)
        self_referring["computation"]["input_fact_ids"] = [self_referring["fact_id"]]
        verdict = verify_computed_fact(
            self_referring,
            {base["fact_id"]: base, self_referring["fact_id"]: self_referring})
        self.assertEqual(verdict["reason"], "self_referential_input")
        wrong_unit = self._derived(base, claimed=120000000.0, unit="cny_wan")
        verdict2 = verify_computed_fact(wrong_unit, {base["fact_id"]: base})
        self.assertEqual(verdict2["reason"], "unit_mismatch")

    def test_plain_fact_without_claim_cannot_be_verified(self):
        plain = self.amount("营业收入", 1.2, "cny_yi")
        verdict = verify_computed_fact(plain, {})
        self.assertFalse(verdict["verified"])
        self.assertEqual(verdict["reason"], "no_computation_claim")


class PeriodFieldTests(R05FactFixture):
    def test_period_recorded_and_validated(self):
        fact = self.amount("营业收入", 1.2, "cny_yi", period="2024Q3")
        self.assertEqual(fact["period"], "2024Q3")
        with self.assertRaises(ValueError):
            self.amount("营业收入", 1.2, "cny_yi", period="x" * 41)

    def test_period_participates_in_fact_id(self):
        a = self.amount("营业收入", 1.2, "cny_yi", period="2024FY")
        b = self.amount("营业收入", 1.2, "cny_yi", period="2023FY")
        self.assertNotEqual(a["fact_id"], b["fact_id"],
                            "same number over a different period is a different fact")


if __name__ == "__main__":
    unittest.main()
