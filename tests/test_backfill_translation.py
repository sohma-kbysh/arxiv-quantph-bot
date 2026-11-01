from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse

import arxiv_bot


def atom(rows, *, total=3, start=0):
    entries = []
    for pid, published, primary in rows:
        entries.append(f"""<entry>
          <id>https://arxiv.org/abs/{pid}v1</id>
          <title>Quantum information test</title><summary>A quantum code.</summary>
          <published>{published}</published><updated>{published}</updated>
          <author><name>Example</name></author>
          <category term="{primary}"/><category term="quant-ph"/>
          <arxiv:primary_category term="{primary}"/>
        </entry>""")
    return ('<feed xmlns="http://www.w3.org/2005/Atom" '
            'xmlns:arxiv="http://arxiv.org/schemas/atom" '
            'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
            + f'<opensearch:totalResults>{total}</opensearch:totalResults>'
            + f'<opensearch:startIndex>{start}</opensearch:startIndex>'
            + ''.join(entries) + '</feed>').encode()


class BackfillTranslationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)

    def test_backfill_pages_full_window_and_preserves_crosslist_context(self):
        pages = [
            atom([("2610.1", "2026-10-31T00:00:00Z", "quant-ph"),
                  ("2610.2", "2026-10-30T00:00:00Z", "cs.IT")]),
            atom([("2610.0", "2026-10-01T00:00:00Z", "quant-ph")], start=2),
        ]
        with (patch.object(arxiv_bot, "http_get_arxiv_api", side_effect=pages) as get,
              patch.object(arxiv_bot.time, "sleep")):
            rows = arxiv_bot.fetch_feed_backfill(
                "quant-ph", {"max_results": 2, "lookback_days": 7}, self.now)
        self.assertEqual([p["id"] for p in rows], ["2610.1", "2610.2"])
        self.assertEqual(rows[0]["announce_type"], "new")
        self.assertEqual(rows[1]["announce_type"], "cross")
        self.assertEqual(rows[1]["source_feed"], "quant-ph")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(get.call_args_list[1].args[0]).query)
        self.assertEqual(query["start"], ["2"])
        self.assertEqual(query["sortBy"], ["submittedDate"])

    def test_truncated_or_repeated_pages_fail_instead_of_advancing(self):
        page = atom([("2610.1", "2026-10-31T00:00:00Z", "quant-ph")])
        for rule, pages in [
            ({"max_results": 1, "max_pages": 1}, [page]),
            ({"max_results": 1, "max_pages": 3}, [page, page]),
        ]:
            with self.subTest(rule=rule):
                with (patch.object(arxiv_bot, "http_get_arxiv_api", side_effect=pages),
                      patch.object(arxiv_bot.time, "sleep")):
                    with self.assertRaises(ValueError):
                        arxiv_bot.fetch_feed_backfill("quant-ph", rule, self.now)

    def test_api_error_and_html_are_not_successful_empty_feeds(self):
        for raw in [
            b"<html><body>Temporarily unavailable</body></html>",
            atom([("api/errors", "2026-10-31T00:00:00Z", "quant-ph")]).replace(
                b"https://arxiv.org/abs/api/errorsv1", b"http://arxiv.org/api/errors#bad"),
        ]:
            with self.subTest(raw=raw[:30]):
                with self.assertRaises(ValueError):
                    arxiv_bot.parse_external_atom(raw, "quant-ph")

    def test_backfill_cadence_and_outage_overlap(self):
        rule = {"enabled": True, "lookback_days": 7, "min_interval_hours": 20}
        recent = (self.now - timedelta(hours=3)).isoformat()
        old = (self.now - timedelta(days=21)).isoformat()
        self.assertFalse(arxiv_bot.feed_backfill_due(rule, recent, self.now))
        self.assertTrue(arxiv_bot.feed_backfill_due(rule, old, self.now))
        self.assertTrue(arxiv_bot.feed_backfill_due(rule, None, self.now))
        self.assertEqual(arxiv_bot.effective_external_lookback_days(rule, old, self.now), 23)

    def test_short_page_advertising_more_results_cannot_advance(self):
        raw = atom([("2610.1", "2026-10-31T00:00:00Z", "quant-ph")], total=2)
        with patch.object(arxiv_bot, "http_get_arxiv_api", return_value=raw):
            with self.assertRaisesRegex(ValueError, "incomplete page"):
                arxiv_bot.fetch_feed_backfill("quant-ph", {"max_results": 2}, self.now)

    def test_complete_empty_feed_is_successful(self):
        with patch.object(arxiv_bot, "http_get_arxiv_api", return_value=atom([], total=0)):
            self.assertEqual(arxiv_bot.fetch_feed_backfill("quant-ph", {}, self.now), [])

    def test_blank_translation_tries_next_backend_and_old_blank_queue_retries(self):
        with (patch.object(arxiv_bot, "translate_deepl", return_value="  "),
              patch.object(arxiv_bot, "translate_apps_script_batch", return_value=["訳"]) as bridge):
            self.assertEqual(arxiv_bot.translate_batch(
                ["An abstract"], {"translators": ["deepl", "apps_script"]}), ["訳"])
            bridge.assert_called_once()
        entry = arxiv_bot.entry_from_delivery({
            "paper": {"abstract": "text"}, "abstract_translated": " ",
            "title_translated": "", "genre_ids": [],
        }, {})
        self.assertIsNone(entry["jp"])
        self.assertIsNone(entry["jp_title"])

    def test_failed_backfill_keeps_previous_cursor_and_durable_rss_paper(self):
        cfg = arxiv_bot.load_json(arxiv_bot.CONFIG_PATH, {})
        cfg["external_arxiv_queries"] = {}
        cfg["classify_with_llm"] = False
        row = {"id": "2610.1", "title": "Quantum code", "abstract": "Quantum error correction.",
               "authors": "", "link": "https://arxiv.org/abs/2610.1",
               "primary": "quant-ph", "categories": ["quant-ph"], "announce_type": "new"}
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({"seen": [], "feed_cursors": {
                "quant-ph": "2026-01-01T00:00:00Z"}}))
            with (patch.object(arxiv_bot, "CONFIG_PATH", Path(tmp) / "cfg.json"),
                  patch.object(arxiv_bot, "STATE_PATH", state_path),
                  patch.object(arxiv_bot, "LOG_PATH", Path(tmp) / "log.json"),
                  patch.object(arxiv_bot, "DIAGNOSTICS_PATH", Path(tmp) / "errors.jsonl"),
                  patch.object(arxiv_bot, "fetch_feed", return_value=[row]),
                  patch.object(arxiv_bot, "fetch_feed_backfill", side_effect=TimeoutError("offline")),
                  patch.object(arxiv_bot, "notify_run_report"),
                  patch.object(arxiv_bot, "post_to_discord") as post,
                  patch.object(arxiv_bot.time, "sleep"),
                  patch.dict(os.environ, {}, clear=True),
                  patch.object(sys, "argv", ["arxiv_bot.py", "--discover-only"]),
                  patch("sys.stdout", new=io.StringIO())):
                (Path(tmp) / "cfg.json").write_text(json.dumps(cfg))
                with self.assertRaises(SystemExit) as stopped:
                    arxiv_bot.main()
            saved = json.loads(state_path.read_text())
            self.assertEqual(stopped.exception.code, 2)
            self.assertEqual(saved["feed_cursors"]["quant-ph"], "2026-01-01T00:00:00Z")
            self.assertIn("2610.1", saved["deliveries"])
            post.assert_not_called()

    def test_apps_script_is_optional_and_validates_all_batch_results(self):
        url = "https://script.google.com/macros/s/deployment-test/exec"
        token = "t" * 32
        with (patch.dict(os.environ, {}, clear=True),
              patch.object(arxiv_bot, "http_post_json") as post):
            self.assertEqual(arxiv_bot.translate_apps_script_batch(["hello"], {}), [None])
            post.assert_not_called()
        with (patch.dict(os.environ, {"GOOGLE_APPS_SCRIPT_URL": url,
                                    "GOOGLE_APPS_SCRIPT_TOKEN": token}, clear=True),
              patch.object(arxiv_bot, "_apps_script_dead", False),
              patch.object(arxiv_bot, "_last_apps_script_call", 0),
              patch.object(arxiv_bot.time, "sleep"),
              patch.object(arxiv_bot, "http_post_json", side_effect=[
                  (200, json.dumps({"translations": ["訳"] * 5}).encode()),
                  (200, json.dumps({"translations": ["六"]}).encode()),
              ]) as post):
            self.assertEqual(arxiv_bot.translate_apps_script_batch(["text"] * 6, {}),
                             ["訳"] * 5 + ["六"])
            self.assertEqual(post.call_count, 2)
            self.assertEqual(post.call_args_list[0].args[1]["token"], token)
            self.assertNotIn(token, post.call_args_list[0].args[0])

    def test_apps_script_200_error_breaks_circuit_and_unsafe_url_is_not_called(self):
        for url, body in [
            ("https://script.google.com/macros/s/test/exec",
             {"error": "translation_failed", "translations": []}),
            ("https://attacker.test/exec", {"translations": ["訳"]}),
        ]:
            with self.subTest(url=url):
                with (patch.dict(os.environ, {"GOOGLE_APPS_SCRIPT_URL": url,
                                             "GOOGLE_APPS_SCRIPT_TOKEN": "t" * 32}, clear=True),
                      patch.object(arxiv_bot, "_apps_script_dead", False),
                      patch.object(arxiv_bot, "_last_apps_script_call", 0),
                      patch.object(arxiv_bot.time, "sleep"),
                      patch.object(arxiv_bot, "http_post_json",
                                   return_value=(200, json.dumps(body).encode())) as post):
                    self.assertEqual(arxiv_bot.translate_apps_script_batch(["hello"], {}), [None])
                    self.assertTrue(arxiv_bot._apps_script_dead)
                    arxiv_bot.translate_apps_script_batch(["again"], {})
                    self.assertEqual(post.call_count, 1 if "script.google.com" in url else 0)

    def test_required_title_and_untranslated_paper_remain_pending(self):
        cfg = arxiv_bot.load_json(arxiv_bot.CONFIG_PATH, {})
        for abstract, title in [("訳", None), (None, None)]:
            with self.subTest(abstract=abstract):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    row = {"id": "2610.1", "title": "A title", "abstract": "An abstract",
                           "authors": "", "link": "https://arxiv.org/abs/2610.1",
                           "primary": "quant-ph", "categories": ["quant-ph"], "announce_type": "new"}
                    record = {"paper": row, "genre_ids": ["other"], "classifier": "test",
                              "abstract_translated": abstract, "title_translated": title,
                              "need_translation": True, "allow_untranslated": True,
                              "channels": {"other": {"status": "pending"}}}
                    (root / "cfg.json").write_text(json.dumps(cfg))
                    (root / "state.json").write_text(json.dumps({
                        "completed_ids": [], "deliveries": {"2610.1": record}}))
                    with (patch.object(arxiv_bot, "CONFIG_PATH", root / "cfg.json"),
                          patch.object(arxiv_bot, "STATE_PATH", root / "state.json"),
                          patch.object(arxiv_bot, "LOG_PATH", root / "log.json"),
                          patch.object(arxiv_bot, "notify_run_report"),
                          patch.object(arxiv_bot, "post_to_discord") as post,
                          patch.dict(os.environ, {}, clear=True),
                          patch.object(sys, "argv", ["arxiv_bot.py", "--deliver-only"]),
                          patch("sys.stdout", new=io.StringIO())):
                        arxiv_bot.main()
                    saved = json.loads((root / "state.json").read_text())
                    self.assertIn("2610.1", saved["deliveries"])
                    self.assertFalse(saved["deliveries"]["2610.1"]["allow_untranslated"])
                    post.assert_not_called()

    def test_default_chain_excludes_automatic_paid_translation(self):
        cfg = arxiv_bot.load_json(arxiv_bot.CONFIG_PATH, {})
        self.assertEqual(cfg["main_translators"], ["deepl", "azure", "apps_script"])
        self.assertEqual(cfg["translators"], ["deepl", "azure", "google"])
        self.assertTrue(cfg["require_translation"])
        self.assertTrue(cfg["require_translated_title"])
        self.assertFalse(cfg["allow_untranslated_low_priority"])


if __name__ == "__main__":
    unittest.main()

