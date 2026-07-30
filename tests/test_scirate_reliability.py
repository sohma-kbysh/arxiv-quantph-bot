from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import arxiv_bot
import scirate_weekly


PAPER_ID = "2607.99998"


def scirate_paper() -> dict:
    return {
        "id": PAPER_ID,
        "title": "SciRate quantum coding test",
        "link": f"https://arxiv.org/abs/{PAPER_ID}",
        "authors": "Test Author",
        "announce_type": "scirate weekly · 42 Scites",
        "categories": ["cs.IT", "quant-ph"],
        "primary": "cs.IT",
        "abstract": "We construct a quantum error-correcting code.",
    }


def scirate_state(
        genre_ids: list[str], *, translated: str | None = "訳"
) -> dict:
    return {
        "posted": {"7": []},
        "deliveries": {
            "7": {
                PAPER_ID: {
                    "paper": scirate_paper(),
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
                    "scites": 42,
                }
            }
        },
    }


class SciRateReliabilityTests(unittest.TestCase):
    def _paths(self, tmp: str) -> tuple[Path, Path]:
        return Path(tmp) / "scirate-state.json", Path(tmp) / "log.json"

    def _run_delivery(
        self,
        state_data: dict,
        post_results: list[bool],
        env: dict[str, str],
        log_data: list[dict] | None = None,
    ) -> tuple[dict, list[dict], object, dict]:
        with tempfile.TemporaryDirectory() as tmp:
            state_path, log_path = self._paths(tmp)
            arxiv_bot.atomic_write_json(state_path, state_data)
            arxiv_bot.atomic_write_json(
                log_path, log_data or [], ensure_ascii=False)
            report: dict = {}

            with (
                patch.object(scirate_weekly, "STATE_PATH", state_path),
                patch.object(arxiv_bot, "LOG_PATH", log_path),
                patch.object(
                    scirate_weekly, "fetch_text",
                    side_effect=AssertionError("deliver must not fetch"),
                ),
                patch.object(
                    scirate_weekly, "fetch_arxiv_metadata",
                    side_effect=AssertionError("deliver must not fetch"),
                ),
                patch.object(
                    arxiv_bot, "post_to_discord",
                    side_effect=post_results,
                ) as post,
                patch.object(
                    arxiv_bot, "notify_run_report",
                    side_effect=lambda value, _cfg: report.update(value),
                ),
                patch.object(scirate_weekly.time, "sleep"),
                patch.object(
                    sys, "argv",
                    ["scirate_weekly.py", "--deliver-only"],
                ),
                patch.dict(os.environ, env, clear=True),
            ):
                try:
                    scirate_weekly.main()
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
        env = {
            "DISCORD_WEBHOOK_QEC": "https://discord.test/qec",
            "DISCORD_WEBHOOK_CRYPTO": "https://discord.test/crypto",
        }
        state, log, post, report = self._run_delivery(
            scirate_state(["qec", "crypto"]), [True, False], env)
        self.assertEqual(post.call_count, 2)
        queued = state["deliveries"]["7"][PAPER_ID]
        self.assertEqual(queued["channels"]["qec"]["status"], "delivered")
        self.assertEqual(queued["channels"]["crypto"]["status"], "pending")
        self.assertNotIn(PAPER_ID, state["posted"]["7"])
        self.assertEqual(log, [])
        self.assertEqual(
            report["failed"][0]["genre_names"], ["暗号・セキュリティ"])

        state2, log2, post2, _ = self._run_delivery(
            state, [True], env, log)
        self.assertEqual(post2.call_count, 1)
        self.assertEqual(
            post2.call_args.args[0], "https://discord.test/crypto")
        self.assertNotIn(PAPER_ID, state2["deliveries"]["7"])
        self.assertIn(PAPER_ID, state2["posted"]["7"])
        self.assertEqual(log2[0]["genre_ids"], ["qec", "crypto"])

    def test_translate_only_uses_queue_without_fetch_or_classification(
            self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path, log_path = self._paths(tmp)
            arxiv_bot.atomic_write_json(
                state_path, scirate_state(["qec"], translated=None))
            arxiv_bot.atomic_write_json(log_path, [], ensure_ascii=False)

            def translate(entries: list[dict], _cfg: dict) -> None:
                entries[0]["jp"] = "保存された翻訳"
                entries[0]["jp_title"] = "保存された題名"

            with (
                patch.object(scirate_weekly, "STATE_PATH", state_path),
                patch.object(arxiv_bot, "LOG_PATH", log_path),
                patch.object(
                    scirate_weekly, "fetch_text",
                    side_effect=AssertionError("translate must not fetch"),
                ) as fetch,
                patch.object(
                    scirate_weekly, "fetch_arxiv_metadata",
                    side_effect=AssertionError("translate must not fetch"),
                ) as metadata,
                patch.object(
                    scirate_weekly, "classify_entries",
                    side_effect=AssertionError(
                        "translate must not classify"),
                ) as classify,
                patch.object(
                    scirate_weekly, "translate_entries",
                    side_effect=translate,
                ),
                patch.object(arxiv_bot, "post_to_discord") as post,
                patch.object(
                    sys, "argv",
                    ["scirate_weekly.py", "--translate-only"],
                ),
                patch.dict(os.environ, {}, clear=True),
            ):
                scirate_weekly.main()

            queued = json.loads(
                state_path.read_text())["deliveries"]["7"][PAPER_ID]
            self.assertEqual(
                queued["abstract_translated"], "保存された翻訳")
            self.assertEqual(
                queued["title_translated"], "保存された題名")
            self.assertEqual(fetch.call_count, 0)
            self.assertEqual(metadata.call_count, 0)
            self.assertEqual(classify.call_count, 0)
            self.assertEqual(post.call_count, 0)

    def test_discover_only_checkpoints_before_translation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path, log_path = self._paths(tmp)
            arxiv_bot.atomic_write_json(state_path, {"posted": {}})
            arxiv_bot.atomic_write_json(log_path, [], ensure_ascii=False)
            cfg = arxiv_bot.load_json(arxiv_bot.CONFIG_PATH, {})
            qec = next(g for g in cfg["genres"] if g["id"] == "qec")

            def classify(
                entries: list[dict], _cfg: dict, _genres: list[dict],
                _dry_run: bool,
            ) -> tuple[int, int, int]:
                entries[0]["genres"] = [qec]
                entries[0]["classified_by"] = "gemini-test"
                return 1, 1, 0

            with (
                patch.object(scirate_weekly, "STATE_PATH", state_path),
                patch.object(arxiv_bot, "LOG_PATH", log_path),
                patch.object(scirate_weekly, "fetch_text",
                             return_value="fixture"),
                patch.object(
                    scirate_weekly, "parse_scirate_candidates",
                    return_value=[{"id": PAPER_ID, "scites": 42}],
                ),
                patch.object(
                    scirate_weekly, "fetch_arxiv_metadata",
                    return_value={PAPER_ID: scirate_paper()},
                ),
                patch.object(
                    scirate_weekly, "classify_entries",
                    side_effect=classify,
                ),
                patch.object(scirate_weekly, "translate_entries") as translate,
                patch.object(arxiv_bot, "post_to_discord") as post,
                patch.object(
                    sys, "argv",
                    ["scirate_weekly.py", "--discover-only"],
                ),
                patch.dict(os.environ, {}, clear=True),
            ):
                scirate_weekly.main()

            queued = json.loads(
                state_path.read_text())["deliveries"]["7"][PAPER_ID]
            self.assertEqual(queued["genre_ids"], ["qec"])
            self.assertIsNone(queued["abstract_translated"])
            self.assertEqual(translate.call_count, 0)
            self.assertEqual(post.call_count, 0)

    def test_source_failure_preserves_existing_queue_and_exits_nonzero(
            self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path, log_path = self._paths(tmp)
            initial = scirate_state(["qec"])
            arxiv_bot.atomic_write_json(state_path, initial)
            arxiv_bot.atomic_write_json(log_path, [], ensure_ascii=False)
            reports: list[dict] = []
            with (
                patch.object(scirate_weekly, "STATE_PATH", state_path),
                patch.object(arxiv_bot, "LOG_PATH", log_path),
                patch.object(
                    scirate_weekly, "fetch_text",
                    side_effect=RuntimeError("SciRate offline"),
                ),
                patch.object(
                    arxiv_bot, "notify_run_report",
                    side_effect=lambda value, _cfg: reports.append(value),
                ),
                patch.object(arxiv_bot, "post_to_discord") as post,
                patch.object(sys, "argv", ["scirate_weekly.py"]),
                patch.dict(os.environ, {}, clear=True),
            ):
                with self.assertRaises(SystemExit) as raised:
                    scirate_weekly.main()
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(
                json.loads(state_path.read_text()), initial)
            self.assertEqual(post.call_count, 0)
            self.assertEqual(
                reports[0]["source_failures"][0]["source"], "SciRate")


if __name__ == "__main__":
    unittest.main()
