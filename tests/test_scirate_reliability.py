from __future__ import annotations

from contextlib import ExitStack
from datetime import date
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import arxiv_bot
import scirate_weekly


PAPER_ID = "2607.99998"


def scirate_paper(pid: str = PAPER_ID) -> dict:
    return {
        "id": pid,
        "title": f"SciRate quantum coding test {pid}",
        "link": f"https://arxiv.org/abs/{pid}",
        "authors": "Test Author",
        "announce_type": "scirate",
        "categories": ["cs.IT", "quant-ph"],
        "primary": "cs.IT",
        "abstract": "We construct a quantum error-correcting code.",
    }


def delivery(
    pid: str = PAPER_ID,
    *,
    scites: int = 42,
    rank: int = 1,
    abstract: str | None = "要旨訳",
    title: str | None = "量子符号",
) -> dict:
    return {
        "paper": scirate_paper(pid),
        "scites": scites,
        "rank": rank,
        "abstract_translated": abstract,
        "title_translated": title,
        "genre_names": ["量子誤り訂正・量子符号"],
        "status": "pending",
        "queued_at": "2026-08-03T00:00:00Z",
    }


def digest_state(mode: str = "weekly") -> dict:
    state = {
        "schema_version": 2,
        "source_status": {"status": "available", "api_ever_available": True},
        "daily_posted": {},
        "weekly_posted": {},
        "daily_deliveries": {},
        "weekly_deliveries": {},
    }
    if mode == "daily":
        state["daily_deliveries"]["2026-08-03"] = {PAPER_ID: delivery()}
    else:
        state["weekly_deliveries"]["2026-07-27_2026-08-02"] = {
            PAPER_ID: delivery()
        }
    return state


class SciRateReliabilityTests(unittest.TestCase):
    def _run_main(
        self,
        state_data: dict,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        patches: tuple = (),
    ) -> tuple[dict, dict]:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            log_path = Path(tmp) / "log.json"
            arxiv_bot.atomic_write_json(state_path, state_data)
            arxiv_bot.atomic_write_json(log_path, [], ensure_ascii=False)
            report: dict = {}
            with ExitStack() as stack:
                stack.enter_context(patch.object(
                    scirate_weekly, "STATE_PATH", state_path))
                stack.enter_context(patch.object(
                    arxiv_bot, "LOG_PATH", log_path))
                stack.enter_context(patch.object(
                    arxiv_bot, "DIAGNOSTICS_PATH",
                    Path(tmp) / "diagnostics.jsonl"))
                stack.enter_context(patch.object(
                    scirate_weekly, "fetch_scirate_candidates_api",
                    side_effect=AssertionError("resume phase must not fetch")))
                stack.enter_context(patch.object(
                    scirate_weekly, "fetch_arxiv_metadata",
                    side_effect=AssertionError("resume phase must not fetch")))
                stack.enter_context(patch.object(
                    arxiv_bot, "notify_run_report",
                    side_effect=lambda value, _cfg: report.update(value)))
                stack.enter_context(patch.object(scirate_weekly.time, "sleep"))
                stack.enter_context(patch.object(sys, "argv", argv))
                stack.enter_context(patch.dict(
                    os.environ, env or {}, clear=True))
                for extra_patch in patches:
                    stack.enter_context(extra_patch)
                try:
                    scirate_weekly.main()
                except SystemExit as exc:
                    if exc.code != 3:
                        raise
            return json.loads(state_path.read_text()), report

    def test_json_api_fetches_weekly_pages_until_threshold(self) -> None:
        first_page = {
            "date": "2026-08-02",
            "papers": [
                {
                    "uid": f"2607.{index:05d}",
                    "scites_count": score,
                    "pubdate": "2026-07-30",
                }
                for index, score in enumerate(range(80, 30, -1), start=1)
            ],
        }
        second_page = {
            "date": "2026-08-02",
            "papers": [
                {"uid": "2607.99991v2", "scites_count": 30,
                 "pubdate": "2026-07-29"},
                {"uid": "2607.99992", "scites_count": 29,
                 "pubdate": "2026-07-29"},
            ],
        }
        responses = [json.dumps(first_page).encode(),
                     json.dumps(second_page).encode()]
        with patch.object(arxiv_bot, "http_get", side_effect=responses) as fetch:
            candidates, pages = scirate_weekly.fetch_scirate_candidates_api(
                "https://api.scirate.test/arxiv/quant-ph.json"
                "?date={date}&range={days}&page={page}",
                7, 30, target_date="2026-08-02")

        self.assertEqual(pages, 2)
        self.assertEqual(len(candidates), 51)
        self.assertEqual(candidates[-1]["id"], "2607.99991")
        self.assertEqual(candidates[-1]["scites"], 30)
        self.assertEqual(candidates[-1]["rank"], 51)
        self.assertIn("date=2026-08-02", fetch.call_args_list[0].args[0])

    def test_daily_top_three_preserves_scirate_tie_order(self) -> None:
        payload = {
            "date": "2026-08-03T00:00:00Z",
            "papers": [
                {"uid": "2608.00003", "scites_count": 5,
                 "pubdate": "2026-08-03"},
                {"uid": "2608.00001", "scites_count": 5,
                 "pubdate": "2026-08-03"},
                {"uid": "2608.00002", "scites_count": 4,
                 "pubdate": "2026-08-03"},
                {"uid": "2608.00004", "scites_count": 3,
                 "pubdate": "2026-08-03"},
            ],
        }
        template = "https://api.scirate.test/feed?range={days}&page={page}"
        with patch.object(
                arxiv_bot, "http_get",
                return_value=json.dumps(payload).encode()) as fetch:
            candidates, pages = scirate_weekly.fetch_scirate_candidates_api(
                template, 1, 1, target_date="2026-08-03", limit=3,
                require_pubdate=True)

        self.assertEqual(pages, 1)
        self.assertEqual(
            [row["id"] for row in candidates],
            ["2608.00003", "2608.00001", "2608.00002"])
        self.assertEqual([row["rank"] for row in candidates], [1, 2, 3])
        self.assertIn("date=2026-08-03", fetch.call_args.args[0])

    def test_daily_rejects_missing_or_wrong_pubdate(self) -> None:
        missing = {"date": "2026-08-03", "papers": [
            {"uid": PAPER_ID, "scites_count": 10}]}
        with patch.object(
                arxiv_bot, "http_get",
                return_value=json.dumps(missing).encode()):
            with self.assertRaisesRegex(
                    scirate_weekly.SciRateAPIError, "no pubdate"):
                scirate_weekly.fetch_scirate_candidates_api(
                    "https://api.test/feed?page={page}", 1, 1,
                    target_date="2026-08-03", limit=3,
                    require_pubdate=True)

        wrong = {"date": "2026-08-03", "papers": [
            {"uid": PAPER_ID, "scites_count": 10,
             "pubdate": "2026-08-02"}]}
        with patch.object(
                arxiv_bot, "http_get",
                return_value=json.dumps(wrong).encode()):
            with self.assertRaisesRegex(
                    scirate_weekly.SciRateAPIError, "outside"):
                scirate_weekly.fetch_scirate_candidates_api(
                    "https://api.test/feed?page={page}", 1, 1,
                    target_date="2026-08-03", limit=3,
                    require_pubdate=True)

    def test_json_api_rejects_unrelated_json_and_partial_pages(self) -> None:
        with patch.object(
                arxiv_bot, "http_get", return_value=b'{"status":"ok"}'):
            with self.assertRaises(scirate_weekly.SciRateAPIError):
                scirate_weekly.fetch_scirate_candidates_api(
                    "https://api.test/feed?page={page}", 7, 30)

        full_page = {"papers": [
            {"uid": f"2607.{index:05d}", "scites_count": 100}
            for index in range(50)]}
        with patch.object(
                arxiv_bot, "http_get",
                return_value=json.dumps(full_page).encode()):
            with self.assertRaisesRegex(
                    scirate_weekly.SciRateAPIError, "partial digest"):
                scirate_weekly.fetch_scirate_candidates_api(
                    "https://api.test/feed?page={page}", 7, 30,
                    max_pages=1)

    def test_auto_schedule_uses_jst_weekdays_and_sunday(self) -> None:
        self.assertEqual(
            scirate_weekly.resolve_mode("auto", date(2026, 8, 3)), "daily")
        self.assertEqual(
            scirate_weekly.resolve_mode("auto", date(2026, 8, 8)), "skip")
        self.assertEqual(
            scirate_weekly.resolve_mode("auto", date(2026, 8, 9)), "weekly")

    def test_legacy_weekly_queue_is_migrated_to_dedicated_delivery(self) -> None:
        legacy = {
            "deliveries": {"7": {PAPER_ID: {
                "paper": scirate_paper(),
                "scites": 42,
                "abstract_translated": "訳",
                "title_translated": "題",
                "queued_at": "2026-08-01T00:00:00Z",
            }}},
            "posted": {"7": []},
        }
        state = scirate_weekly.normalize_state(legacy)
        self.assertIn(PAPER_ID, state["weekly_deliveries"]["legacy"])
        self.assertEqual(state["deliveries"]["7"], {})

    def test_discovery_checkpoints_daily_top_three_without_classifying(self) -> None:
        api = Mock(return_value=([{
            "id": PAPER_ID, "scites": 7, "rank": 1,
            "pubdate": "2026-08-03", "submit_date": "2026-08-02",
        }], 1))
        metadata = Mock(return_value={PAPER_ID: scirate_paper()})
        state_after, _report = self._run_main(
            scirate_weekly.normalize_state({}),
            ["scirate_weekly.py", "--mode", "daily", "--date",
             "2026-08-03", "--discover-only"],
            patches=(
                patch.object(
                    scirate_weekly, "fetch_scirate_candidates_api", new=api),
                patch.object(
                    scirate_weekly, "fetch_arxiv_metadata", new=metadata),
            ))

        queued = state_after["daily_deliveries"]["2026-08-03"][PAPER_ID]
        self.assertEqual(queued["scites"], 7)
        self.assertEqual(queued["rank"], 1)
        self.assertNotIn(
            "2026-08-03", state_after["pending_discovery"]["daily"])
        self.assertEqual(api.call_args.kwargs["target_date"], "2026-08-03")
        self.assertEqual(api.call_args.kwargs["limit"], 3)

    def test_degraded_daily_period_is_caught_up_on_next_daily_run(self) -> None:
        state = scirate_weekly.normalize_state({})
        state["source_status"] = {
            "status": "degraded", "api_ever_available": True}
        state["pending_discovery"]["daily"]["2026-08-03"] = {
            "target_date": "2026-08-03", "queued_at": "2026-08-03T14:30:00Z"}
        api = Mock(side_effect=[([], 1), ([], 1)])
        metadata = Mock(return_value={})
        state_after, _report = self._run_main(
            state,
            ["scirate_weekly.py", "--mode", "daily", "--date",
             "2026-08-04", "--discover-only"],
            patches=(
                patch.object(
                    scirate_weekly, "fetch_scirate_candidates_api", new=api),
                patch.object(
                    scirate_weekly, "fetch_arxiv_metadata", new=metadata),
            ))

        self.assertEqual(
            [call.kwargs["target_date"] for call in api.call_args_list],
            ["2026-08-03", "2026-08-04"])
        self.assertEqual(state_after["pending_discovery"]["daily"], {})
        self.assertTrue(state_after["daily_posted"]["2026-08-03"]["empty"])
        self.assertTrue(state_after["daily_posted"]["2026-08-04"]["empty"])

    def test_daily_delivery_is_one_message_to_dedicated_webhook(self) -> None:
        state = digest_state("daily")
        state["daily_deliveries"]["2026-08-03"] = {
            "2608.00003": delivery("2608.00003", scites=5, rank=1),
            "2608.00001": delivery("2608.00001", scites=5, rank=2),
            "2608.00002": delivery("2608.00002", scites=4, rank=3),
        }
        post = Mock(return_value=True)
        state_after, report = self._run_main(
            state,
            ["scirate_weekly.py", "--mode", "daily", "--date",
             "2026-08-03", "--deliver-only"],
            env={"DISCORD_WEBHOOK_SCIRATE": "https://discord.test/scirate"},
            patches=(patch.object(
                scirate_weekly, "post_daily_digest", new=post),))

        self.assertNotIn("2026-08-03", state_after["daily_deliveries"])
        self.assertEqual(
            state_after["daily_posted"]["2026-08-03"]["ids"],
            ["2608.00003", "2608.00001", "2608.00002"])
        self.assertEqual(report["messages"], 1)
        self.assertEqual(len(report["posted"]), 3)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0],
                         "https://discord.test/scirate")

    def test_weekly_delivery_uses_only_scirate_webhook(self) -> None:
        post = Mock(return_value=True)
        state_after, report = self._run_main(
            digest_state("weekly"),
            ["scirate_weekly.py", "--mode", "weekly", "--date",
             "2026-08-02", "--deliver-only"],
            env={
                "DISCORD_WEBHOOK_SCIRATE": "https://discord.test/scirate",
                "DISCORD_WEBHOOK_QEC": "https://discord.test/qec",
            },
        patches=(patch.object(arxiv_bot, "post_to_discord", new=post),))

        self.assertEqual(report["messages"], 1)
        self.assertEqual(report["posted"][0]["genre_names"],
                         ["SciRate週間30+"])
        self.assertNotIn(
            "2026-07-27_2026-08-02", state_after["weekly_deliveries"])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0],
                         "https://discord.test/scirate")

    def test_missing_scirate_webhook_keeps_weekly_queue(self) -> None:
        state_after, report = self._run_main(
            digest_state("weekly"),
            ["scirate_weekly.py", "--mode", "weekly", "--date",
             "2026-08-02", "--deliver-only"])
        self.assertIn(
            PAPER_ID,
            state_after["weekly_deliveries"]["2026-07-27_2026-08-02"])
        self.assertEqual(
            report["failed"][0]["genre_names"],
            ["SciRate(webhook未設定)"])

    def test_translate_only_uses_queue_without_fetch(self) -> None:
        state = digest_state("daily")
        state["daily_deliveries"]["2026-08-03"][PAPER_ID][
            "title_translated"] = None

        def translate(entries: list[dict], _cfg: dict, *,
                      include_abstract: bool) -> None:
            self.assertFalse(include_abstract)
            entries[0]["jp_title"] = "保存された題名"

        with patch.object(
                scirate_weekly, "translate_entries", side_effect=translate):
            state_after, _report = self._run_main(
                state,
                ["scirate_weekly.py", "--mode", "daily", "--date",
                 "2026-08-03", "--translate-only"])
        queued = state_after["daily_deliveries"]["2026-08-03"][PAPER_ID]
        self.assertEqual(queued["title_translated"], "保存された題名")

    def test_api_wait_is_optional_but_regression_is_reported(self) -> None:
        waiting = scirate_weekly.normalize_state({})
        waiting["source_status"] = {
            "status": "waiting_for_api", "api_ever_available": False}
        with patch.object(
                arxiv_bot, "post_to_discord", return_value=True):
            _state, report = self._run_main(
                waiting,
                ["scirate_weekly.py", "--mode", "weekly", "--date",
                 "2026-08-02", "--deliver-only"],
                env={"DISCORD_WEBHOOK_SCIRATE": "https://discord.test/scirate"})
        self.assertEqual(report["source_notices"][0]["source"],
                         "SciRate JSON API")
        self.assertEqual(report["source_failures"], [])

        degraded = scirate_weekly.normalize_state({})
        degraded["source_status"] = {
            "status": "degraded", "api_ever_available": True,
            "last_error": "SciRate JSON API HTTP 503"}
        _state, report = self._run_main(
            degraded,
            ["scirate_weekly.py", "--mode", "weekly", "--date",
             "2026-08-02", "--deliver-only"])
        self.assertEqual(report["source_failures"][0]["source"],
                         "SciRate JSON API")


if __name__ == "__main__":
    unittest.main()
