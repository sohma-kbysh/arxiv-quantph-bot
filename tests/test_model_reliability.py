"""No-network provider, classification queue and policy-cache regressions."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import arxiv_bot


def response(parts=None, *, finish="STOP") -> tuple[int, bytes]:
    return 200, json.dumps({"candidates": [{
        "content": {"parts": parts or [{"text": "<<<1>>> qit"}]},
        "finishReason": finish,
    }]}).encode()


def sample_paper() -> dict:
    return {"id": "2610.99999", "title": "Quantum entropy inequalities",
            "abstract": "We prove entropy inequalities for quantum channels.",
            "authors": "Example Author", "link": "https://arxiv.org/abs/2610.99999",
            "source_feed": "quant-ph", "announce_type": "new",
            "primary": "quant-ph", "categories": ["quant-ph"]}


def sample_genres() -> list[dict]:
    return [{"id": gid, "name": gid, "keywords": [], "description": gid,
             "webhook_env": f"DISCORD_WEBHOOK_{gid.upper()}"}
            for gid in ("qit", "network", "other")]


def queued_record() -> dict:
    return {"paper": sample_paper(), "genre_ids": ["qit"],
            "classifier": "tfidf", "abstract_translated": "訳文",
            "title_translated": "量子エントロピー", "need_translation": True,
            "channels": {"qit": {"status": "pending"}}}


class GeminiReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"gemini_model": "unit-model", "gemini_min_interval_sec": 0,
                    "gemini_max_retries": 2, "gemini_overload_giveup": 1,
                    "gemini_max_output_tokens": 4096,
                    "gemini_thinking_levels": {"unit-model": "minimal"}}
        self.patches = [
            patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-key"}, clear=True),
            patch.object(arxiv_bot, "_last_gemini_calls", {}),
            patch.object(arxiv_bot, "_gemini_dead_models", set()),
            patch.object(arxiv_bot, "_gemini_fail_streaks", {}),
            patch.object(arxiv_bot, "save_error_diagnostic"),
            patch.object(arxiv_bot.time, "sleep"),
            patch.object(arxiv_bot.time, "time", return_value=1000.0),
            patch.object(arxiv_bot.urllib.request, "urlopen",
                         side_effect=AssertionError("network is forbidden in tests")),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_generation_settings_for_each_configured_model(self):
        cfg = arxiv_bot.load_json(arxiv_bot.CONFIG_PATH, {})
        for spec in arxiv_bot.classifier_model_specs(cfg):
            with self.subTest(model=spec["model"]), patch.object(
                    arxiv_bot, "http_post_json", return_value=response()) as post:
                self.assertEqual(arxiv_bot._gemini_request("classify", cfg,
                                                          model=spec["model"]),
                                 "<<<1>>> qit")
                generation = post.call_args.args[1]["generationConfig"]
                self.assertEqual(generation["maxOutputTokens"], 4096)
                self.assertEqual(generation["thinkingConfig"]["thinkingLevel"],
                                 cfg["gemini_thinking_levels"][spec["model"]])
                self.assertNotIn("temperature", generation)
                self.assertNotIn("topP", generation)
                self.assertNotIn("topK", generation)

    def test_multipart_output_joins_final_text_and_ignores_thoughts(self):
        body = response([
            {"text": "internal reasoning", "thought": True},
            {"text": "<<<1>>> "}, {"text": "qit\n<<<2>>> network"},
            {"inlineData": {"mimeType": "image/png", "data": "ignored"}},
        ])
        with patch.object(arxiv_bot, "http_post_json", return_value=body):
            self.assertEqual(arxiv_bot._gemini_request("classify", self.cfg),
                             "<<<1>>> qit\n<<<2>>> network")

    def test_truncation_blocked_empty_and_malformed_responses_fall_back(self):
        bodies = [
            response(finish="MAX_TOKENS")[1],
            response(finish="SAFETY")[1],
            json.dumps({"candidates": [{"content": None}]}).encode(),
            json.dumps({"candidates": [None]}).encode(),
            json.dumps({"candidates": []}).encode(),
            response([{"text": "reasoning only", "thought": True}])[1],
            b"not-json",
        ]
        for body in bodies:
            with self.subTest(body=body), patch.object(
                    arxiv_bot, "http_post_json", return_value=(200, body)) as post:
                self.assertIsNone(arxiv_bot._gemini_request("classify", self.cfg))
                self.assertEqual(post.call_count, 1)
                self.assertNotIn("unit-model", arxiv_bot._gemini_dead_models)

    def test_daily_quota_skips_backoff_and_opens_only_that_model_circuit(self):
        quota = json.dumps({"error": {"details": [{"violations": [{
            "quotaId": "GenerateContentRequestsPerDayPerProjectPerModel-FreeTier",
        }]}]}}).encode()
        with patch.object(arxiv_bot, "http_post_json", return_value=(429, quota)) as post, \
                patch.object(arxiv_bot.time, "sleep") as sleep:
            self.assertIsNone(arxiv_bot._gemini_request("classify", self.cfg))
            self.assertIsNone(arxiv_bot._gemini_request("another batch", self.cfg))
            self.assertEqual(post.call_count, 1)
            sleep.assert_not_called()
        with patch.object(arxiv_bot, "http_post_json", return_value=response()):
            self.assertEqual(arxiv_bot._gemini_request("classify", self.cfg,
                                                      model="second-model"),
                             "<<<1>>> qit")

    def test_daily_quota_message_without_details_is_not_retried(self):
        body = json.dumps({"error": {"message": "Free requests per day exhausted"}}).encode()
        with patch.object(arxiv_bot, "http_post_json", return_value=(429, body)) as post, \
                patch.object(arxiv_bot.time, "sleep") as sleep:
            self.assertIsNone(arxiv_bot._gemini_request("classify", self.cfg))
            self.assertEqual(post.call_count, 1)
            sleep.assert_not_called()

    def test_minute_rate_limit_retries_and_recovers_using_server_delay(self):
        body = json.dumps({"error": {"details": [{"retryDelay": "12s"}]}}).encode()
        with patch.object(arxiv_bot, "http_post_json", side_effect=[
                (429, body), response()]) as post, \
                patch.object(arxiv_bot.time, "sleep") as sleep:
            self.assertEqual(arxiv_bot._gemini_request("classify", self.cfg),
                             "<<<1>>> qit")
            self.assertEqual(post.call_count, 2)
            sleep.assert_called_once_with(12)
            self.assertNotIn("unit-model", arxiv_bot._gemini_dead_models)

    def test_long_retry_delay_uses_next_model_without_waiting(self):
        body = json.dumps({"error": {"details": [{"retryDelay": "90s"}]}}).encode()
        with patch.object(arxiv_bot, "http_post_json", return_value=(429, body)) as post, \
                patch.object(arxiv_bot.time, "sleep") as sleep:
            self.assertIsNone(arxiv_bot._gemini_request("classify", self.cfg))
            self.assertEqual(post.call_count, 1)
            sleep.assert_not_called()
            self.assertIn("unit-model", arxiv_bot._gemini_dead_models)

    def test_permanent_http_errors_are_disabled_without_retries(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status), patch.object(
                    arxiv_bot, "_gemini_dead_models", set()), patch.object(
                    arxiv_bot, "http_post_json", return_value=(status, b"error")) as post, \
                    patch.object(arxiv_bot.time, "sleep") as sleep:
                self.assertIsNone(arxiv_bot._gemini_request("classify", self.cfg))
                self.assertIsNone(arxiv_bot._gemini_request("another", self.cfg))
                self.assertEqual(post.call_count, 1)
                sleep.assert_not_called()

    def test_server_overload_retries_then_breaks_circuit_after_budget(self):
        with patch.object(arxiv_bot, "http_post_json", return_value=(503, b"overload")) as post, \
                patch.object(arxiv_bot.time, "sleep") as sleep:
            self.assertIsNone(arxiv_bot._gemini_request("classify", self.cfg))
            self.assertEqual(post.call_count, 3)
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [10, 20])
            self.assertIn("unit-model", arxiv_bot._gemini_dead_models)

    def test_transient_server_failure_recovers(self):
        with patch.object(arxiv_bot, "http_post_json", side_effect=[
                (503, b"overload"), response()]) as post:
            self.assertEqual(arxiv_bot._gemini_request("classify", self.cfg),
                             "<<<1>>> qit")
            self.assertEqual(post.call_count, 2)
            self.assertNotIn("unit-model", arxiv_bot._gemini_dead_models)
            self.assertEqual(arxiv_bot._gemini_fail_streaks["unit-model"], 0)


class ClassificationStateReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.genres = sample_genres()
        self.genre_map = {g["id"]: g for g in self.genres}
        self.cfg = {"genres": self.genres, "gemini_model_primary": "unit-model",
                    "gemini_model_secondary": "unit-model", "revalidate_external_skips": True}

    def test_only_unposted_quantph_tfidf_is_reclassified(self):
        for classifier in ("tfidf", "unit-model"):
            for delivered in (False, True):
                for external in (False, True):
                    with self.subTest(classifier=classifier, delivered=delivered,
                                      external=external):
                        record = queued_record()
                        record["classifier"] = classifier
                        record["channels"]["qit"]["status"] = (
                            "delivered" if delivered else "pending")
                        if external:
                            record["paper"]["announce_type"] = "external"
                        entry = arxiv_bot.entry_from_delivery(
                            record, self.genre_map, reclassify_fallback=True)
                        self.assertEqual(entry["llm_done"],
                                         not (classifier == "tfidf" and not delivered
                                              and not external))
                        self.assertTrue(arxiv_bot.entry_from_delivery(
                            record, self.genre_map)["llm_done"])

    def test_classification_trace_survives_queue_roundtrip_and_delivery_log(self):
        trace = arxiv_bot.classification_record(
            sample_paper(), ["qit"], ["qit"], self.cfg)
        entry = arxiv_bot.entry_from_delivery(queued_record(), self.genre_map)
        entry["classification"] = trace
        delivery = arxiv_bot.merge_entry_into_delivery(entry)
        restored = arxiv_bot.entry_from_delivery(
            json.loads(json.dumps(delivery)), self.genre_map)
        self.assertEqual(restored["classification"], trace)
        delivery["channels"]["qit"]["status"] = "delivered"
        log = []
        arxiv_bot.record_delivery_success(
            log, restored, "qit", self.genre_map, delivery, self.cfg)
        self.assertEqual(log[0]["classification"], trace)
        self.assertEqual(log[0]["genre_ids"], ["qit"])

    def test_skip_cache_invalidates_on_policy_change_but_acceptance_is_retained(self):
        fingerprint = arxiv_bot.classification_policy_fingerprint(self.cfg)
        self.assertTrue(arxiv_bot.external_cache_is_current(
            {"genre_ids": [], "policy": fingerprint}, self.cfg))
        self.assertFalse(arxiv_bot.external_cache_is_current(
            {"genre_ids": [], "policy": "old"}, self.cfg))
        self.assertFalse(arxiv_bot.external_cache_is_current(
            {"genre_ids": []}, self.cfg))
        self.assertTrue(arxiv_bot.external_cache_is_current(
            {"genre_ids": ["qit"], "policy": "old"}, self.cfg))
        for key in ("gemini_model_primary", "force_genre_keywords"):
            cfg = copy.deepcopy(self.cfg)
            cfg[key] = "new-model" if key == "gemini_model_primary" else {"crypto": ["new phrase"]}
            self.assertNotEqual(arxiv_bot.classification_policy_fingerprint(cfg), fingerprint)

    def test_stale_external_skip_is_actually_sent_for_review(self):
        cfg = {**self.cfg, "external_arxiv_queries": {"cs.IT": {
            "candidate_genres": ["qit"], "excluded_genres": ["other"]}}}
        paper = {**sample_paper(), "announce_type": "external",
                 "source_feed": "cs.IT", "primary": "cs.IT", "categories": ["cs.IT"]}
        cached = {f"cs.IT:{paper['id']}": {"genre_ids": [], "policy": "old"}}
        with patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-key"}, clear=True), \
                patch.object(arxiv_bot, "_gemini_dead_models", set()), \
                patch.object(arxiv_bot, "classify_external_llm_batch", return_value=[["qit"]]) as review:
            accepted, decisions, stats = arxiv_bot.review_external_candidates(
                {"cs.IT": [paper]}, cfg, self.genres, cached)
        self.assertEqual(review.call_count, 1)
        self.assertEqual([p["id"] for p in accepted], [paper["id"]])
        self.assertEqual(stats["cached"], 0)
        self.assertEqual(decisions[f"cs.IT:{paper['id']}"]["policy"],
                         arxiv_bot.classification_policy_fingerprint(cfg))

    def test_resume_phases_do_not_reclassify_tfidf_queue(self):
        for phase in ("--translate-only", "--deliver-only"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temp:
                cfg = {**self.cfg, "feeds": [], "external_arxiv_queries": {},
                       "translators": ["deepl"], "classify_with_llm": True,
                       "reclassify_pending_fallback": True}
                config_path, state_path, log_path = [Path(temp) / name for name in
                                                     ("config.json", "state.json", "log.json")]
                config_path.write_text(json.dumps(cfg))
                state_path.write_text(json.dumps({"deliveries": {
                    "2610.99999": queued_record()}}))
                log_path.write_text("[]")
                with patch.object(arxiv_bot, "CONFIG_PATH", config_path), \
                        patch.object(arxiv_bot, "STATE_PATH", state_path), \
                        patch.object(arxiv_bot, "LOG_PATH", log_path), \
                        patch.object(arxiv_bot, "fetch_feed") as fetch, \
                        patch.object(arxiv_bot, "classify_llm_batch") as classify, \
                        patch.object(arxiv_bot, "post_to_discord", return_value=True), \
                        patch.object(arxiv_bot, "notify_run_report"), \
                        patch.object(arxiv_bot.time, "sleep"), \
                        patch.object(sys, "argv", ["arxiv_bot.py", phase]), \
                        patch.dict(os.environ, {"GEMINI_API_KEY": "unit-test-key",
                                               "DISCORD_WEBHOOK_QIT": "https://discord.test/unit"},
                                   clear=True):
                    arxiv_bot.main()
                fetch.assert_not_called()
                classify.assert_not_called()


class OpenAICompatibleResponseTests(unittest.TestCase):
    def test_null_empty_and_truncated_outputs_are_rejected(self):
        spec = {"name": "test-adapter", "model": "unit-model",
                "api_key_env": "UNIT_ADAPTER_KEY", "base_url": "https://unit.test/v1",
                "min_interval_sec": 0}
        for content, finish in ((None, "stop"), ("", "stop"),
                                ("<<<1>>> qit", "length"), ("<<<1>>> qit", "stop")):
            with self.subTest(content=content, finish=finish):
                fake = MagicMock()
                fake.__enter__.return_value.read.return_value = json.dumps({"choices": [{
                    "message": {"content": content}, "finish_reason": finish}]}).encode()
                with patch.dict(os.environ, {"UNIT_ADAPTER_KEY": "unit-key"}, clear=True), \
                        patch.object(arxiv_bot, "_last_openai_compat_calls", {}), \
                        patch.object(arxiv_bot, "_openai_compat_dead_models", set()), \
                        patch.object(arxiv_bot, "save_error_diagnostic"), \
                        patch.object(arxiv_bot.urllib.request, "urlopen", return_value=fake):
                    actual = arxiv_bot._openai_compat_request("classify", {}, spec)
                self.assertEqual(actual, "<<<1>>> qit" if content and finish == "stop" else None)


if __name__ == "__main__":
    unittest.main()
