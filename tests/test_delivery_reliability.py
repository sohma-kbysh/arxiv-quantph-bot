from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import arxiv_bot


def paper(pid: str = "2607.99999") -> dict:
    return {
        "id": pid,
        "title": "Quantum code delivery test",
        "link": f"https://arxiv.org/abs/{pid}",
        "authors": "Test Author",
        "announce_type": "cross",
        "categories": ["cs.IT", "quant-ph"],
        "primary": "cs.IT",
        "abstract": "We construct and decode quantum LDPC codes.",
        "source_feed": "quant-ph",
    }


def delivery_state(genre_ids: list[str], *, translated: str | None = "訳") -> dict:
    return {
        "schema_version": 2,
        "completed_ids": [],
        "seen": [],
        "deliveries": {
            "2607.99999": {
                "paper": paper(),
                "genre_ids": genre_ids,
                "classifier": "gemini-test",
                "abstract_translated": translated,
                "title_translated": "量子符号",
                "need_translation": True,
                "allow_untranslated": False,
                "channels": {
                    gid: {"status": "pending"} for gid in genre_ids
                },
                "queued_at": "2026-07-30T00:00:00Z",
            }
        },
        "external_reviews": {},
        "external_pending": {},
        "external_cursors": {},
    }


class DeliveryReliabilityTests(unittest.TestCase):
    def test_quantph_crosslist_keeps_ai_genres_and_gets_qec(self) -> None:
        genres = [
            {"id": gid, "name": gid, "keywords": []}
            for gid in ("qit", "qec", "other")
        ]
        cfg = {
            "cross_classify_primary_as_quantph": ["quant-ph", "cs.CR"],
            "category_other_overrides": [],
            "qec_adjacent_coding_terms": ["quantum code"],
        }
        result = arxiv_bot.postprocess_genres(
            paper(), [genres[0]], genres, cfg)
        self.assertEqual([g["id"] for g in result], ["qit", "qec"])

    def test_quantph_source_coding_is_also_qec(self) -> None:
        cfg = arxiv_bot.load_json(arxiv_bot.CONFIG_PATH, {})
        genre_map = {g["id"]: g for g in cfg["genres"]}
        row = {
            **paper(),
            "title": "Lossless Address Coding for Quantum Networks",
            "abstract": (
                "We introduce a lossless source coding scheme for coherent "
                "quantum network addresses."
            ),
        }
        result = arxiv_bot.postprocess_genres(
            row,
            [genre_map["network"], genre_map["qit"]],
            cfg["genres"],
            cfg,
        )
        self.assertEqual(
            [g["id"] for g in result], ["network", "qit", "qec"])

    def test_explicit_external_context_keeps_restrictive_primary_rule(self) -> None:
        genres = [
            {"id": gid, "name": gid, "keywords": []}
            for gid in ("qit", "other")
        ]
        row = {
            **paper(),
            "source_feed": "cs.IT",
            "announce_type": "external",
            "categories": ["cs.IT"],
        }
        cfg = {
            "cross_classify_primary_as_quantph": ["quant-ph", "cs.CR"],
            "category_other_overrides": [],
        }
        result = arxiv_bot.postprocess_genres(
            row, [genres[0]], genres, cfg)
        self.assertEqual([g["id"] for g in result], ["other"])

    def test_redact_url_removes_api_keys_and_discord_tokens(self) -> None:
        gemini = arxiv_bot.redact_url(
            "https://example.test/v1/model?key=super-secret")
        discord = arxiv_bot.redact_url(
            "https://discord.com/api/webhooks/123456/token-value")
        self.assertNotIn("super-secret", gemini)
        self.assertIn("<redacted>", gemini)
        self.assertNotIn("123456", discord)
        self.assertNotIn("token-value", discord)

    def test_http_error_log_never_contains_secret_url(self) -> None:
        stream = io.StringIO()
        url = "https://example.test/v1/model?key=super-secret"
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(
                    arxiv_bot.urllib.request, "urlopen",
                    side_effect=urllib.error.URLError("offline"),
                ),
                patch.object(
                    arxiv_bot, "DIAGNOSTICS_PATH",
                    Path(tmpdir) / "diagnostics.jsonl",
                ),
                patch.object(sys, "stderr", stream),
            ):
                status, _ = arxiv_bot.http_post_json(url, {})
        self.assertEqual(status, 0)
        self.assertNotIn("super-secret", stream.getvalue())
        self.assertIn("<redacted>", stream.getvalue())

    def test_http_error_diagnostic_keeps_safe_details_and_redacts_secrets(
            self) -> None:
        url = "https://example.test/api/query?key=super-secret&q=quantum"
        body = b'upstream policy: token "super-secret" is not acceptable'
        headers = {
            "Content-Type": "text/plain",
            "X-Request-Id": "request-123",
            "Set-Cookie": "session=must-not-be-saved",
        }
        error = urllib.error.HTTPError(
            url, 406, "Not Acceptable", headers, io.BytesIO(body))
        with tempfile.TemporaryDirectory() as tmpdir:
            diagnostic_path = Path(tmpdir) / "diagnostics.jsonl"
            with (
                patch.object(
                    arxiv_bot.urllib.request, "urlopen", side_effect=error),
                patch.object(
                    arxiv_bot, "DIAGNOSTICS_PATH", diagnostic_path),
                patch.dict(os.environ, {"GEMINI_API_KEY": "super-secret"}),
                self.assertRaises(urllib.error.HTTPError),
            ):
                arxiv_bot.http_get(url)

            record = json.loads(
                diagnostic_path.read_text(encoding="utf-8").splitlines()[0])

        self.assertEqual(record["kind"], "http_error")
        self.assertEqual(record["status"], 406)
        self.assertEqual(record["method"], "GET")
        self.assertIn("<redacted>", record["url"])
        self.assertNotIn("super-secret", json.dumps(record))
        self.assertIn("upstream policy", record["response_body"])
        self.assertEqual(
            record["response_headers"]["x-request-id"], "request-123")
        self.assertNotIn("set-cookie", record["response_headers"])

    def test_cursor_expands_lookback_after_outage(self) -> None:
        now = datetime(2026, 7, 30, tzinfo=timezone.utc)
        days = arxiv_bot.effective_external_lookback_days(
            {"lookback_days": 4, "cursor_overlap_days": 2},
            "2026-07-22T00:00:00Z",
            now,
        )
        self.assertEqual(days, 10)

    def test_completed_ids_are_not_truncated_during_migration(self) -> None:
        ids = [f"2501.{i:05d}" for i in range(3500)]
        state = arxiv_bot.normalize_bot_state({"seen": ids})
        self.assertEqual(len(state["completed_ids"]), 3500)
        self.assertEqual(state["seen"], state["completed_ids"])

    def test_external_retry_payload_is_removed_after_queueing(self) -> None:
        reviews = {
            "cs.IT:2607.99999": {
                "genre_ids": ["qec"],
                "paper": paper(),
            }
        }
        arxiv_bot.strip_completed_external_papers(
            reviews, set(), {"2607.99999"})
        self.assertNotIn("paper", reviews["cs.IT:2607.99999"])

    def _run_delivery(
            self, state_data: dict, post_results: list[bool],
            env: dict[str, str],
            log_data: list[dict] | None = None,
    ) -> tuple[dict, list[dict], object, dict]:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            log_path = Path(tmp) / "log.json"
            arxiv_bot.atomic_write_json(state_path, state_data)
            arxiv_bot.atomic_write_json(
                log_path, log_data or [], ensure_ascii=False)
            report: dict = {}

            def capture_report(value: dict, _cfg: dict) -> None:
                report.update(value)

            with (
                patch.object(arxiv_bot, "STATE_PATH", state_path),
                patch.object(arxiv_bot, "LOG_PATH", log_path),
                patch.object(
                    arxiv_bot, "DIAGNOSTICS_PATH",
                    Path(tmp) / "diagnostics.jsonl"),
                patch.object(
                    arxiv_bot, "post_to_discord",
                    side_effect=post_results,
                ) as post,
                patch.object(arxiv_bot, "notify_run_report",
                             side_effect=capture_report),
                patch.object(arxiv_bot.time, "sleep"),
                patch.object(sys, "argv", ["arxiv_bot.py", "--deliver-only"]),
                patch.dict(os.environ, env, clear=True),
            ):
                try:
                    arxiv_bot.main()
                except SystemExit as exc:
                    if exc.code != 3:
                        raise
            return (
                json.loads(state_path.read_text()),
                json.loads(log_path.read_text()),
                post,
                report,
            )

    def test_partial_delivery_retries_only_missing_channel(self) -> None:
        initial = delivery_state(["qec", "crypto"])
        env = {
            "DISCORD_WEBHOOK_QEC": "https://discord.test/qec",
            "DISCORD_WEBHOOK_CRYPTO": "https://discord.test/crypto",
        }
        state, log, post, report = self._run_delivery(
            initial, [True, False], env)
        self.assertEqual(post.call_count, 2)
        queued = state["deliveries"]["2607.99999"]
        self.assertEqual(queued["channels"]["qec"]["status"], "delivered")
        self.assertEqual(queued["channels"]["crypto"]["status"], "pending")
        self.assertNotIn("2607.99999", state["completed_ids"])
        self.assertEqual(log[0]["genre_ids"], ["qec"])
        self.assertEqual(
            report["failed"][0]["genre_names"], ["暗号・セキュリティ"])

        state2, log2, post2, _ = self._run_delivery(
            state, [True], env, log)
        self.assertEqual(post2.call_count, 1)
        self.assertEqual(
            post2.call_args.args[0], "https://discord.test/crypto")
        self.assertNotIn("2607.99999", state2["deliveries"])
        self.assertIn("2607.99999", state2["completed_ids"])
        self.assertEqual(log2[0]["genre_ids"], ["qec", "crypto"])

    def test_missing_genre_webhook_does_not_fall_back_to_general(self) -> None:
        state, _log, post, report = self._run_delivery(
            delivery_state(["qec"]),
            [],
            {"DISCORD_WEBHOOK_GENERAL": "https://discord.test/general"},
        )
        self.assertEqual(post.call_count, 0)
        self.assertIn("2607.99999", state["deliveries"])
        self.assertIn(
            "webhook未設定", report["failed"][0]["genre_names"][0])

    def test_translation_pending_survives_without_posting(self) -> None:
        state, _log, post, report = self._run_delivery(
            delivery_state(["qec"], translated=None),
            [],
            {"DISCORD_WEBHOOK_QEC": "https://discord.test/qec"},
        )
        self.assertEqual(post.call_count, 0)
        self.assertIn("2607.99999", state["deliveries"])
        self.assertEqual(report["deferred"][0]["id"], "2607.99999")

    def test_source_failure_is_reported_and_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            log_path = Path(tmp) / "log.json"
            arxiv_bot.atomic_write_json(state_path, {"seen": []})
            arxiv_bot.atomic_write_json(log_path, [], ensure_ascii=False)
            reports: list[dict] = []
            with (
                patch.object(arxiv_bot, "STATE_PATH", state_path),
                patch.object(arxiv_bot, "LOG_PATH", log_path),
                patch.object(
                    arxiv_bot, "DIAGNOSTICS_PATH",
                    Path(tmp) / "diagnostics.jsonl"),
                patch.object(
                    arxiv_bot, "fetch_feed",
                    side_effect=RuntimeError("RSS offline"),
                ),
                patch.object(
                    arxiv_bot, "notify_run_report",
                    side_effect=lambda report, _cfg: reports.append(report),
                ),
                patch.object(arxiv_bot, "post_to_discord") as post,
                patch.object(arxiv_bot.time, "sleep"),
                patch.object(
                    sys, "argv", ["arxiv_bot.py", "--prepare-only"]),
                patch.dict(
                    os.environ, {"ARXIV_TEST_FEED": "fixture"}, clear=True),
            ):
                with self.assertRaises(SystemExit) as raised:
                    arxiv_bot.main()
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(post.call_count, 0)
            self.assertEqual(
                reports[0]["source_failures"][0]["source"],
                "RSS:quant-ph",
            )

    def test_discovery_checkpoints_tfidf_crosslist_before_translation(self) -> None:
        cfg = arxiv_bot.load_json(arxiv_bot.CONFIG_PATH, {})
        qit = next(g for g in cfg["genres"] if g["id"] == "qit")
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            log_path = Path(tmp) / "log.json"
            arxiv_bot.atomic_write_json(state_path, {"seen": []})
            arxiv_bot.atomic_write_json(log_path, [], ensure_ascii=False)
            with (
                patch.object(arxiv_bot, "STATE_PATH", state_path),
                patch.object(arxiv_bot, "LOG_PATH", log_path),
                patch.object(arxiv_bot, "fetch_feed", return_value=[paper()]),
                patch.object(arxiv_bot, "classify_multi", return_value=[qit]),
                patch.object(arxiv_bot, "translate_batch") as translate,
                patch.object(arxiv_bot, "post_to_discord") as post,
                patch.object(arxiv_bot.time, "sleep"),
                patch.object(
                    sys, "argv", ["arxiv_bot.py", "--discover-only"]),
                patch.dict(
                    os.environ, {"ARXIV_TEST_FEED": "fixture"}, clear=True),
            ):
                arxiv_bot.main()
            saved = json.loads(state_path.read_text())
            queued = saved["deliveries"]["2607.99999"]
            self.assertEqual(queued["genre_ids"], ["qit", "qec"])
            self.assertIsNone(queued["abstract_translated"])
            self.assertEqual(translate.call_count, 0)
            self.assertEqual(post.call_count, 0)

    def test_discovery_never_uses_combined_gemini_translation(self) -> None:
        cfg = arxiv_bot.load_json(arxiv_bot.CONFIG_PATH, {})
        cfg["translators"] = ["gemini"]
        qit = next(g for g in cfg["genres"] if g["id"] == "qit")
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            log_path = Path(tmp) / "log.json"
            config_path = Path(tmp) / "config.json"
            arxiv_bot.atomic_write_json(state_path, {"seen": []})
            arxiv_bot.atomic_write_json(log_path, [], ensure_ascii=False)
            arxiv_bot.atomic_write_json(config_path, cfg, ensure_ascii=False)
            with (
                patch.object(arxiv_bot, "STATE_PATH", state_path),
                patch.object(arxiv_bot, "LOG_PATH", log_path),
                patch.object(arxiv_bot, "CONFIG_PATH", config_path),
                patch.object(arxiv_bot, "fetch_feed", return_value=[paper()]),
                patch.object(arxiv_bot, "classify_multi", return_value=[qit]),
                patch.object(
                    arxiv_bot, "classify_llm_batch",
                    return_value=[["qec"]],
                ) as classify,
                patch.object(
                    arxiv_bot, "translate_classify_gemini_batch",
                    side_effect=AssertionError(
                        "discovery must not combine translation"),
                ) as combined,
                patch.object(arxiv_bot, "translate_batch") as translate,
                patch.object(arxiv_bot, "post_to_discord") as post,
                patch.object(arxiv_bot.time, "sleep"),
                patch.object(
                    sys, "argv", ["arxiv_bot.py", "--discover-only"]),
                patch.dict(
                    os.environ,
                    {
                        "ARXIV_TEST_FEED": "fixture",
                        "GEMINI_API_KEY": "test",
                    },
                    clear=True,
                ),
            ):
                arxiv_bot.main()
            saved = json.loads(state_path.read_text())
            self.assertEqual(
                saved["deliveries"]["2607.99999"]["genre_ids"], ["qec"])
            self.assertGreater(classify.call_count, 0)
            self.assertEqual(combined.call_count, 0)
            self.assertEqual(translate.call_count, 0)
            self.assertEqual(post.call_count, 0)

    def test_duplicate_webhooks_are_a_visible_pending_error(self) -> None:
        state, _log, post, report = self._run_delivery(
            delivery_state(["qec", "crypto"]),
            [],
            {
                "DISCORD_WEBHOOK_QEC": "https://discord.test/shared",
                "DISCORD_WEBHOOK_CRYPTO": "https://discord.test/shared",
            },
        )
        self.assertEqual(post.call_count, 0)
        self.assertIn("2607.99999", state["deliveries"])
        failures = report["failed"][0]["genre_names"]
        self.assertEqual(len(failures), 2)
        self.assertTrue(all("webhook重複" in item for item in failures))


if __name__ == "__main__":
    unittest.main()
