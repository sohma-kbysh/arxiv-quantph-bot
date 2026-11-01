from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import unittest
from unittest.mock import patch

import arxiv_bot
from scripts import audit_classification as audit


GENRES = [{"id": gid, "name": gid, "description": gid, "keywords": []}
          for gid in ("qit", "qec", "other")]
CHAIN = [{"provider": "gemini", "model": name, "name": name}
         for name in ("model-a", "model-b")]
CONFIG = {
    "genres": GENRES,
    "gemini_model_primary": "model-a",
    "gemini_model_secondary": "model-b",
    "external_arxiv_queries": {
        "cs.IT": {"allow_all_genres": True, "excluded_genres": ["other"]},
    },
}


def log_row(**changes) -> dict:
    return {
        "id": "2610.00001",
        "posted_at": "2026-10-30T01:00:00Z",
        "title": "Information transmission theorem",
        "abstract_en": "We prove a quantum information theorem.",
        "primary": "cs.IT",
        "categories": ["cs.IT"],
        "source_feed": "cs.IT",
        "announce_type": "external",
        "classification_context": "external",
        "genre_ids": ["qit"],
        **changes,
    }


class ClassificationAuditTests(unittest.TestCase):
    def run_audit(self, rows, *, normal=None, external=None):
        with (
            patch.object(arxiv_bot, "classifier_key_present", return_value=True),
            patch.object(arxiv_bot, "classifier_dead", return_value=False),
            patch.object(arxiv_bot, "classify_llm_batch",
                         return_value=normal or [["qit"]]) as normal_call,
            patch.object(arxiv_bot, "classify_external_llm_batch",
                         side_effect=external) as external_call,
        ):
            results = audit.audit_rows(rows, CONFIG, GENRES, CHAIN, 5)
        return results, normal_call, external_call

    def test_external_paper_keeps_production_labels_instead_of_other(self):
        results, normal, external = self.run_audit(
            [log_row()], external=[[["qit"]]])
        self.assertEqual(results[0]["genre_ids"], ["qit"])
        normal.assert_not_called()
        self.assertEqual(external.call_args.args[3], "cs.IT")
        self.assertEqual(external.call_args.args[5]["model"], "model-a")

    def test_legacy_external_announce_restores_source_context(self):
        row = log_row()
        row.pop("source_feed")
        row.pop("classification_context")
        results, normal, _ = self.run_audit(
            [row], external=[[["qit"]]])
        self.assertEqual(results[0]["genre_ids"], ["qit"])
        normal.assert_not_called()

    def test_cross_list_uses_normal_context_even_with_external_primary(self):
        row = log_row(source_feed="quant-ph", announce_type="cross",
                      classification_context="quant-ph",
                      categories=["cs.IT", "quant-ph"])
        results, normal, external = self.run_audit([row])
        self.assertEqual(results[0]["genre_ids"], ["qit"])
        normal.assert_called_once()
        external.assert_not_called()

    def test_external_rejection_requires_the_production_skip_consensus(self):
        results, normal, external = self.run_audit(
            [log_row()], external=[[["skip"]], [["skip"]]])
        self.assertEqual(results[0]["genre_ids"], [])
        self.assertEqual(external.call_count, 2)
        normal.assert_not_called()

    def test_single_external_skip_remains_unclassified(self):
        results, _, _ = self.run_audit(
            [log_row()], external=[[["skip"]], [[]]])
        self.assertIsNone(results[0])

    def test_missing_external_policy_does_not_use_normal_classifier(self):
        with redirect_stderr(StringIO()):
            results, normal, external = self.run_audit(
                [log_row(source_feed="cs.CC", primary="cs.CC")])
        self.assertIsNone(results[0])
        normal.assert_not_called()
        external.assert_not_called()

    def test_audit_is_read_only_and_label_order_is_not_a_difference(self):
        output = StringIO()
        row = log_row(genre_ids=["qit", "qec"])
        with (
            patch.object(audit.sys, "argv", [
                "audit_classification.py", "--date", "2026-10-30"]),
            patch.object(arxiv_bot, "load_json", side_effect=[CONFIG, [row]]),
            patch.object(arxiv_bot, "classifier_key_present", return_value=True),
            patch.object(arxiv_bot, "classifier_dead", return_value=False),
            patch.object(arxiv_bot, "classify_external_llm_batch",
                         return_value=[["qec", "qit"]]),
            patch.object(arxiv_bot, "atomic_write_json") as write,
            patch.object(arxiv_bot, "post_to_discord") as post,
            patch.object(arxiv_bot, "notify_run_report") as report,
            redirect_stdout(output),
        ):
            self.assertEqual(audit.main(), 0)
        self.assertIn("differences=0", output.getvalue())
        write.assert_not_called()
        post.assert_not_called()
        report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
