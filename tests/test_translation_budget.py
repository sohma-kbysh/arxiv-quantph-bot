from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import arxiv_bot


def isolated_translators():
    """Fresh module-level translator state for one test."""
    return (
        patch.object(arxiv_bot, "_google_dead", False),
        patch.object(arxiv_bot, "_last_google_call", 0.0),
        patch.object(arxiv_bot, "_translation_budget", None),
        patch.object(arxiv_bot, "_budget_exhausted", set()),
        patch.object(arxiv_bot, "_translation_success", Counter()),
        patch.object(arxiv_bot.time, "sleep"),
    )


def google_ok(url, payload, headers=None, timeout=120):
    assert "translation.googleapis.com" in url
    return 200, json.dumps({"data": {"translations": [
        {"translatedText": "訳:" + payload["q"][:5]}]}}).encode()


class TranslationBudgetTests(unittest.TestCase):
    def test_google_is_never_called_without_a_loaded_budget(self) -> None:
        a, b, c, d, e, f = isolated_translators()
        with (a, b, c, d, e, f,
              patch.dict(os.environ, {"GOOGLE_TRANSLATE_API_KEY": "k"},
                         clear=True),
              patch.object(arxiv_bot, "http_post_json") as post,
              patch("sys.stdout", new=io.StringIO())):
            self.assertIsNone(arxiv_bot.translate_google("text", {}))
            post.assert_not_called()
            self.assertIn("google", arxiv_bot._budget_exhausted)
            self.assertEqual(arxiv_bot.dead_translators(
                {"translators": ["google"]}), ["google"])

    def test_google_reserves_characters_and_stops_before_the_cap(
            self) -> None:
        a, b, c, d, e, f = isolated_translators()
        state: dict = {}
        with (a, b, c, d, e, f,
              patch.dict(os.environ, {"GOOGLE_TRANSLATE_API_KEY": "k"},
                         clear=True),
              patch.object(arxiv_bot, "http_post_json",
                           side_effect=google_ok) as post,
              patch("sys.stdout", new=io.StringIO())):
            arxiv_bot.load_translation_budget(
                state, {"google_monthly_char_limit": 100})
            self.assertEqual(arxiv_bot.translate_google("x" * 60, {}),
                             "訳:xxxxx")
            # 60 + 50 would exceed 100: refused without any request.
            self.assertIsNone(arxiv_bot.translate_google("y" * 50, {}))
            self.assertIsNone(arxiv_bot.translate_google("z", {}))
            self.assertEqual(post.call_count, 1)
            arxiv_bot.save_translation_budget(state)
        self.assertEqual(state["translation_usage"]["google"]["used"], 60)
        self.assertEqual(state["translation_usage"]["google"]["limit"], 100)

    def test_missing_cap_disables_google(self) -> None:
        a, b, c, d, e, f = isolated_translators()
        with (a, b, c, d, e, f,
              patch.dict(os.environ, {"GOOGLE_TRANSLATE_API_KEY": "k"},
                         clear=True),
              patch.object(arxiv_bot, "http_post_json") as post,
              patch("sys.stdout", new=io.StringIO())):
            arxiv_bot.load_translation_budget({}, {})
            self.assertIsNone(arxiv_bot.translate_google("text", {}))
            post.assert_not_called()

    def test_periods_follow_pacific_time(self) -> None:
        state = {"translation_usage": {
            "google": {"period": "2026-10", "used": 449_000},
            "gemini": {"period": "2026-10-31", "used": 99},
        }}
        cfg = {"google_monthly_char_limit": 450_000,
               "gemini_translation_daily_request_limit": 100}
        with patch.object(arxiv_bot, "_translation_budget", None), \
                patch.object(arxiv_bot, "_budget_exhausted", set()):
            # 06:00 UTC on Nov 1 is still Oct 31 in California.
            before = arxiv_bot.load_translation_budget(
                state, cfg, datetime(2026, 11, 1, 6, tzinfo=timezone.utc))
            self.assertEqual(before["google"]["used"], 449_000)
            self.assertEqual(before["gemini"]["used"], 99)
            after = arxiv_bot.load_translation_budget(
                state, cfg, datetime(2026, 11, 1, 9, tzinfo=timezone.utc))
            self.assertEqual(after["google"],
                             {"period": "2026-11", "used": 0,
                              "limit": 450_000})
            self.assertEqual(after["gemini"]["period"], "2026-11-01")
            self.assertEqual(after["gemini"]["used"], 0)

    def test_gemini_translation_draws_one_request_per_chunk(self) -> None:
        cfg = {"translators": ["gemini"],
               "gemini_translation_daily_request_limit": 1}
        with (patch.object(arxiv_bot, "_translation_budget", None),
              patch.object(arxiv_bot, "_budget_exhausted", set()),
              patch.object(arxiv_bot, "_translation_success", Counter()),
              patch.object(arxiv_bot, "translate_gemini_batch",
                           return_value=["訳1", "訳2"]) as gemini,
              patch("sys.stdout", new=io.StringIO())):
            arxiv_bot.load_translation_budget({}, cfg)
            self.assertEqual(arxiv_bot.translate_batch(["a", "b"], cfg),
                             ["訳1", "訳2"])
            self.assertEqual(arxiv_bot.translate_batch(["c", "d"], cfg),
                             [None, None])
            self.assertEqual(gemini.call_count, 1)
            self.assertEqual(arxiv_bot.dead_translators(cfg), ["gemini"])

    def test_translate_phase_spends_and_persists_google_budget(self) -> None:
        cfg = arxiv_bot.load_json(arxiv_bot.CONFIG_PATH, {})
        record = {
            "paper": {
                "id": "2611.50001", "title": "A quantum title", "link": "",
                "authors": "", "announce_type": "new",
                "categories": ["quant-ph"], "primary": "quant-ph",
                "abstract": "An abstract that needs translation.",
                "source_feed": "quant-ph",
            },
            "genre_ids": ["other"], "classifier": "test",
            "abstract_translated": None, "title_translated": None,
            "need_translation": True, "allow_untranslated": False,
            "channels": {"other": {"status": "pending"}},
        }
        a, b, c, d, e, f = isolated_translators()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cfg.json").write_text(json.dumps(cfg))
            (root / "state.json").write_text(json.dumps(
                {"deliveries": {"2611.50001": record}}))
            (root / "log.json").write_text("[]")
            with (a, b, c, d, e, f,
                  patch.object(arxiv_bot, "CONFIG_PATH", root / "cfg.json"),
                  patch.object(arxiv_bot, "STATE_PATH", root / "state.json"),
                  patch.object(arxiv_bot, "LOG_PATH", root / "log.json"),
                  patch.object(arxiv_bot, "DIAGNOSTICS_PATH",
                               root / "e.jsonl"),
                  patch.object(arxiv_bot, "_deepl_dead", False),
                  patch.object(arxiv_bot, "_azure_dead", False),
                  patch.object(arxiv_bot, "_apps_script_dead", False),
                  patch.object(arxiv_bot, "http_post_json",
                               side_effect=google_ok),
                  patch.dict(os.environ, {"GOOGLE_TRANSLATE_API_KEY": "k"},
                             clear=True),
                  patch.object(sys, "argv",
                               ["arxiv_bot.py", "--translate-only"]),
                  patch("sys.stdout", new=io.StringIO())):
                arxiv_bot.main()
            saved = json.loads((root / "state.json").read_text())
        spent = len(record["paper"]["abstract"]) + len(record["paper"]["title"])
        self.assertEqual(saved["translation_usage"]["google"]["used"], spent)
        queued = saved["deliveries"]["2611.50001"]
        self.assertTrue(queued["abstract_translated"].startswith("訳:"))
        self.assertTrue(queued["title_translated"].startswith("訳:"))
        status = saved["translation_status"]
        self.assertEqual(status["translated"], {"google": 2})
        self.assertEqual(status["free_tier"]["google"]["used"], spent)


if __name__ == "__main__":
    unittest.main()
