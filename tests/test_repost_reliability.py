from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import arxiv_bot
from scripts import repost_missing_channels


class RepostReliabilityTests(unittest.TestCase):
    def _fixtures(self, tmp: str) -> tuple[Path, Path]:
        log_path = Path(tmp) / "log.json"
        plan_path = Path(tmp) / "plan.json"
        arxiv_bot.atomic_write_json(
            log_path,
            [{
                "id": "2607.12242",
                "title": "Quantum Codes",
                "title_translated": "量子符号",
                "authors": "Test Author",
                "link": "https://arxiv.org/abs/2607.12242",
                "primary": "cs.IT",
                "announce_type": "cross",
                "genre_ids": ["other"],
                "genre_names": ["その他・異分野"],
                "abstract_en": "We construct quantum stabilizer codes.",
                "abstract_translated": "量子安定化符号を構成する。",
            }],
            ensure_ascii=False,
        )
        arxiv_bot.atomic_write_json(
            plan_path,
            [{
                "id": "2607.12242",
                "channels": ["qec"],
                "genres_after": ["qec"],
            }],
            ensure_ascii=False,
        )
        return log_path, plan_path

    def test_success_is_recorded_and_rerun_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path, plan_path = self._fixtures(tmp)
            with (
                patch.object(arxiv_bot, "LOG_PATH", log_path),
                patch.object(
                    arxiv_bot, "post_to_discord", return_value=True) as post,
                patch.object(arxiv_bot, "notify_run_report"),
                patch.object(repost_missing_channels.time, "sleep"),
                patch.object(
                    sys, "argv",
                    ["repost_missing_channels.py", "--plan", str(plan_path)],
                ),
                patch.dict(
                    os.environ,
                    {"DISCORD_WEBHOOK_QEC": "https://discord.test/qec"},
                    clear=True,
                ),
            ):
                self.assertEqual(repost_missing_channels.main(), 0)
                self.assertEqual(repost_missing_channels.main(), 0)

            self.assertEqual(post.call_count, 1)
            row = json.loads(log_path.read_text())[0]
            self.assertEqual(row["genre_ids"], ["qec"])
            self.assertEqual(row["repost_genre_ids"], ["qec"])

    def test_missing_genre_webhook_never_falls_back_to_general(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path, plan_path = self._fixtures(tmp)
            reports: list[dict] = []
            with (
                patch.object(arxiv_bot, "LOG_PATH", log_path),
                patch.object(arxiv_bot, "post_to_discord") as post,
                patch.object(
                    arxiv_bot, "notify_run_report",
                    side_effect=lambda value, _cfg: reports.append(value),
                ),
                patch.object(
                    sys, "argv",
                    ["repost_missing_channels.py", "--plan", str(plan_path)],
                ),
                patch.dict(
                    os.environ,
                    {
                        "DISCORD_WEBHOOK_GENERAL":
                            "https://discord.test/general",
                    },
                    clear=True,
                ),
            ):
                self.assertEqual(repost_missing_channels.main(), 3)

            self.assertEqual(post.call_count, 0)
            self.assertIn(
                "webhook未設定",
                reports[0]["failed"][0]["genre_names"][0],
            )
            row = json.loads(log_path.read_text())[0]
            self.assertEqual(row["genre_ids"], ["other"])


if __name__ == "__main__":
    unittest.main()
