"""GLM-03 fact binding: typed facts bound to verified excerpts, deterministic math."""
import sys
import tempfile
import unittest
from pathlib import Path

from stock_agent.config import project_path, project_path_override
from stock_agent.evidence_context import verified_evidence_documents
from stock_agent.evidence_excerpts import create_excerpt
from stock_agent.evidence_facts import build_fact, convert_amount_to_cny

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _vault_fixture import import_document, vault_config


class EvidenceFactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(project_path_override(lambda value: self.root / value))
        self.config = vault_config(self.root)
        self.body = "\ufeff标题\n营业收入为 1.2 亿元。\n研发费用为 3000 万元。"
        self.ref, self.sha = import_document(self.config, self.root, content=self.body,
                                             title="Fixture annual report")
        document = verified_evidence_documents(self.config)[self.sha]
        self.text = project_path(document["object_path"]).read_bytes().decode("utf-8-sig")
        self.excerpt = create_excerpt(self.config, self.ref,
                                      self.text.index("营业收入"),
                                      self.text.index("。") + 1)
        self.selector = {
            "document_id": self.excerpt["document_id"],
            "document_sha256": self.excerpt["document_sha256"],
            "extractor_version": self.excerpt["extractor_version"],
            "source_text_sha256": self.excerpt["source_text_sha256"],
            "start": self.excerpt["start"], "end": self.excerpt["end"],
            "excerpt_sha256": self.excerpt["excerpt_sha256"],
        }

    def test_statement_fact_binds_to_verified_excerpt(self):
        fact = build_fact(self.config, self.selector, fact_type="statement",
                          statement="公司披露营业收入为 1.2 亿元。")
        self.assertTrue(fact["fact_id"].startswith("fact:sha256:"))
        self.assertEqual(fact["fact_type"], "statement")
        self.assertEqual(fact["excerpt_sha256"], self.selector["excerpt_sha256"])
        self.assertEqual(fact["document_sha256"], self.sha)
        self.assertEqual(fact["parser_version"], self.excerpt["extractor_version"])
        self.assertEqual(fact["review_status"], "unreviewed")
        self.assertFalse(fact["external_eligible"])
        self.assertEqual(fact["scope"], "local_review_only")

    def test_amount_fact_requires_numeric_value_and_unit(self):
        fact = build_fact(self.config, self.selector, fact_type="amount",
                          statement="营业收入", value=1.2, unit="cny_yi")
        self.assertEqual(fact["value"], 1.2)
        self.assertEqual(fact["unit"], "cny_yi")
        self.assertIsNone(fact["computation"])
        with self.assertRaises(ValueError):
            build_fact(self.config, self.selector, fact_type="amount",
                       statement="营业收入", value=None, unit="cny_yi")
        with self.assertRaises(ValueError):
            build_fact(self.config, self.selector, fact_type="amount",
                       statement="营业收入", value=1.2, unit="usd")

    def test_computed_fact_records_formula_and_inputs(self):
        base = build_fact(self.config, self.selector, fact_type="amount",
                          statement="营业收入", value=1.2, unit="cny_yi")
        derived_selector = dict(self.selector)
        fact = build_fact(
            self.config, derived_selector, fact_type="amount",
            statement="营业收入（元）", value=120000000.0, unit="cny",
            computed={"operation": "convert_unit",
                      "input_fact_ids": [base["fact_id"]],
                      "params": {"to_unit": "cny"}},
        )
        self.assertEqual(fact["computation"]["operation"], "convert_unit")
        self.assertEqual(fact["computation"]["params"], {"to_unit": "cny"})
        self.assertEqual(fact["computation"]["input_fact_ids"], [base["fact_id"]])
        self.assertEqual(fact["computation"]["converted_by"], "deterministic_code")
        # R05: free-form formula strings are prose, never executable claims.
        with self.assertRaises(ValueError):
            build_fact(
                self.config, dict(self.selector), fact_type="amount",
                statement="营业收入（元）", value=120000000.0, unit="cny",
                computed={"formula": "value_in_yi * 100000000",
                          "input_fact_ids": [base["fact_id"]]},
            )

    def test_unknown_fact_type_rejected(self):
        with self.assertRaises(ValueError):
            build_fact(self.config, self.selector, fact_type="opinion",
                      statement="我觉得会涨")

    def test_tampered_selector_rejected(self):
        tampered = dict(self.selector)
        tampered["excerpt_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            build_fact(self.config, tampered, fact_type="statement", statement="x")

    def test_statement_length_capped(self):
        with self.assertRaises(ValueError):
            build_fact(self.config, self.selector, fact_type="statement",
                      statement="长" * 2001)

    def test_convert_amount_to_cny(self):
        self.assertEqual(convert_amount_to_cny(1.5, "cny_yi"), 150000000.0)
        self.assertEqual(convert_amount_to_cny(2.0, "cny_wan"), 20000.0)
        self.assertEqual(convert_amount_to_cny(5.0, "cny"), 5.0)
        with self.assertRaises(ValueError):
            convert_amount_to_cny(1.0, "usd")


if __name__ == "__main__":
    unittest.main()
