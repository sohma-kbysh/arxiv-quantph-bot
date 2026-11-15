from __future__ import annotations

from datetime import datetime, timezone
import unittest

import arxiv_bot


NOW = datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)
CFG = {"delivery_spread": {
    "enabled": True, "batches_per_day": 3, "max_hold_hours": 20}}


def rows(count: int, queued_at: str = "2026-11-16T07:30:00Z") -> list[dict]:
    return [{"id": f"2611.{i:05d}", "queued_at": queued_at,
             "partly_delivered": False} for i in range(count)]


class DeliverySpreadTests(unittest.TestCase):
    def test_day_is_split_into_three_batches_across_the_list(self) -> None:
        state: dict = {}
        ready = rows(10)
        delivered: list[set[str]] = []
        for _ in range(3):
            batch = arxiv_bot.select_delivery_batch(state, ready, CFG, NOW)
            delivered.append(batch)
            ready = [row for row in ready if row["id"] not in batch]
        self.assertEqual([len(batch) for batch in delivered], [4, 3, 3])
        # Every third paper of the priority-sorted list, not the first third.
        self.assertEqual(delivered[0], {
            "2611.00000", "2611.00003", "2611.00006", "2611.00009"})
        self.assertEqual(ready, [])
        self.assertEqual(state["delivery_spread"],
                         {"date": "2026-11-16", "runs": 3})

    def test_new_utc_day_restarts_the_count(self) -> None:
        state = {"delivery_spread": {"date": "2026-11-15", "runs": 3}}
        batch = arxiv_bot.select_delivery_batch(state, rows(9), CFG, NOW)
        self.assertEqual(len(batch), 3)
        self.assertEqual(state["delivery_spread"],
                         {"date": "2026-11-16", "runs": 1})

    def test_overdue_and_partly_delivered_papers_are_never_held(self) -> None:
        ready = rows(6)
        ready[1]["queued_at"] = "2026-11-15T07:30:00Z"  # waited 24.5 h
        ready[2]["partly_delivered"] = True
        ready[4]["queued_at"] = None
        batch = arxiv_bot.select_delivery_batch({}, ready, CFG, NOW)
        self.assertEqual(batch, {
            "2611.00000", "2611.00001", "2611.00002", "2611.00004"})

    def test_disabled_forced_or_empty_delivers_everything(self) -> None:
        everything = {row["id"] for row in rows(5)}
        state: dict = {}
        self.assertEqual(
            arxiv_bot.select_delivery_batch(state, rows(5), {}, NOW),
            everything)
        self.assertEqual(
            arxiv_bot.select_delivery_batch(
                state, rows(5), CFG, NOW, force_all=True), everything)
        self.assertEqual(
            arxiv_bot.select_delivery_batch(state, [], CFG, NOW), set())
        # None of these count as one of the day's batches.
        self.assertNotIn("delivery_spread", state)


if __name__ == "__main__":
    unittest.main()
