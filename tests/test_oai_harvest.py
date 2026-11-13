from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import email.utils
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

import arxiv_bot


V_APR = "Sat, 25 Apr 2026 10:00:00 GMT"
V_SEP = "Tue, 08 Sep 2026 12:00:00 GMT"
V_NOV10 = "Tue, 10 Nov 2026 18:45:36 GMT"
V_NOV11 = "Wed, 11 Nov 2026 09:00:00 GMT"


def raw_record(pid: str, datestamp: str, versions: list[str], categories: str,
               *, title: str = "Quantum error correction with LDPC codes",
               abstract: str = "We construct quantum codes.",
               deleted: bool = False, comments: str = "") -> str:
    header = (f"<identifier>oai:arXiv.org:{pid}</identifier>"
              f"<datestamp>{datestamp}</datestamp>"
              "<setSpec>physics:quant-ph</setSpec>")
    if deleted:
        return f'<record><header status="deleted">{header}</header></record>'
    version_xml = "".join(
        f'<version version="v{i + 1}"><date>{date}</date><size>1kb</size>'
        "</version>"
        for i, date in enumerate(versions))
    return (
        f"<record><header>{header}</header><metadata>"
        '<arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/">'
        f"<id>{pid}</id><submitter>Example</submitter>{version_xml}"
        f"<title>{title}\n  continued</title>"
        "<authors>A. Author, B. Author</authors>"
        f"<categories>{categories}</categories>"
        + (f"<comments>{comments}</comments>" if comments else "")
        + f"<abstract>  {abstract}\n</abstract>"
        "</arXivRaw></metadata></record>")


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def oai_page(records: list[str], *, token: str | None = None,
             response_date: str | None = None,
             error: tuple[str, str] | None = None,
             verb: str = "ListRecords") -> bytes:
    response_date = response_date or now_stamp()
    if error:
        body = f'<error code="{error[0]}">{error[1]}</error>'
    elif verb == "GetRecord":
        body = f"<GetRecord>{''.join(records)}</GetRecord>"
    else:
        resumption = (
            f'<resumptionToken expirationDate="2026-11-13T00:00:00Z">{token}'
            "</resumptionToken>" if token else "<resumptionToken/>")
        body = f"<ListRecords>{''.join(records)}{resumption}</ListRecords>"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
        f"<responseDate>{response_date}</responseDate>"
        '<request verb="ListRecords">https://oaipmh.arxiv.org/oai</request>'
        f"{body}</OAI-PMH>").encode()


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in urllib.parse.parse_qs(
        urllib.parse.urlsplit(url).query).items()}


class OaiParsingTests(unittest.TestCase):
    def test_category_to_set_mapping(self) -> None:
        self.assertEqual(arxiv_bot.oai_set_for_category("quant-ph"),
                         "physics:quant-ph")
        self.assertEqual(arxiv_bot.oai_set_for_category("cs.CR"), "cs:cs:CR")
        self.assertEqual(arxiv_bot.oai_set_for_category("cond-mat.str-el"),
                         "physics:cond-mat:str-el")
        self.assertEqual(arxiv_bot.oai_set_for_category("math.CO"),
                         "math:math:CO")

    def test_parse_records_versions_deleted_and_malformed(self) -> None:
        page = oai_page([
            raw_record("2611.00001", "2026-11-11", [V_NOV10],
                       "quant-ph cs.ET"),
            raw_record("2605.00002", "2026-11-11", [V_NOV10, V_APR],
                       "cs.IT quant-ph"),
            raw_record("2611.00003", "2026-11-11", [], "quant-ph"),
            raw_record("2611.00004", "2026-11-11", [V_NOV10], "quant-ph",
                       deleted=True),
        ], token="next-page", response_date="2026-11-11T20:00:00Z")
        rows, token, served, skipped = arxiv_bot.parse_oai_list_records(page)
        self.assertEqual(token, "next-page")
        self.assertEqual(served, "2026-11-11T20:00:00Z")
        self.assertEqual(skipped, 1)
        self.assertEqual([r["id"] for r in rows], ["2611.00001", "2605.00002"])
        first, replaced = rows
        self.assertEqual(first["title"],
                         "Quantum error correction with LDPC codes continued")
        self.assertEqual(first["abstract"], "We construct quantum codes.")
        self.assertEqual(first["primary"], "quant-ph")
        self.assertEqual(first["categories"], ["quant-ph", "cs.ET"])
        self.assertEqual(first["link"], "https://arxiv.org/abs/2611.00001")
        self.assertEqual(first["published"], "2026-11-10T18:45:36Z")
        self.assertEqual(first["version_count"], 1)
        # Versions are ordered by number, not by document order.
        self.assertEqual(replaced["published"], "2026-11-10T18:45:36Z")
        self.assertEqual(replaced["updated"], "2026-04-25T10:00:00Z")
        self.assertEqual(replaced["primary"], "cs.IT")

    def test_no_records_is_success_but_other_errors_fail(self) -> None:
        rows, token, served, skipped = arxiv_bot.parse_oai_list_records(
            oai_page([], error=("noRecordsMatch", "none"),
                     response_date="2026-11-11T20:00:00Z"))
        self.assertEqual((rows, token, skipped), ([], None, 0))
        self.assertEqual(served, "2026-11-11T20:00:00Z")
        for raw in [
            oai_page([], error=("badArgument", "bad from")),
            b"<html><body>Service unavailable</body></html>",
            oai_page([], response_date="2026-11-11T20:00:00Z").replace(
                b"<responseDate>2026-11-11T20:00:00Z</responseDate>", b""),
        ]:
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(ValueError):
                    arxiv_bot.parse_oai_list_records(raw)

    def test_harvest_follows_tokens_and_uses_first_response_date(self) -> None:
        pages = [
            oai_page([raw_record("2611.00001", "2026-11-10", [V_NOV10],
                                 "quant-ph")],
                     token="verb%3DListRecords%26page%3D2",
                     response_date="2026-11-11T19:00:00Z"),
            oai_page([raw_record("2611.00002", "2026-11-11", [V_NOV11],
                                 "quant-ph")],
                     response_date="2026-11-11T19:00:09Z"),
        ]
        with (patch.object(arxiv_bot, "http_get_arxiv_api",
                           side_effect=pages) as get,
              patch.object(arxiv_bot.time, "sleep")):
            rows, cursor, skipped = arxiv_bot.fetch_oai_records(
                "physics:quant-ph", "2026-11-08", {},
                now=datetime(2026, 11, 11, 19, 5, tzinfo=timezone.utc))
        self.assertEqual(sorted(r["id"] for r in rows),
                         ["2611.00001", "2611.00002"])
        self.assertEqual(cursor, "2026-11-11T19:00:00Z")
        self.assertEqual(skipped, 0)
        first, second = [query(c.args[0]) for c in get.call_args_list]
        self.assertEqual(first, {"verb": "ListRecords",
                                 "metadataPrefix": "arXivRaw",
                                 "set": "physics:quant-ph",
                                 "from": "2026-11-08",
                                 "until": "2026-11-11"})
        self.assertEqual(second, {"verb": "ListRecords",
                                  "resumptionToken":
                                  "verb%3DListRecords%26page%3D2"})

    def test_repeated_token_or_page_limit_never_returns_partial_harvest(
            self) -> None:
        looping = oai_page([], token="same")
        for rule, pages in [({"max_pages": 5}, [looping, looping]),
                            ({"max_pages": 1}, [looping])]:
            with self.subTest(rule=rule):
                with (patch.object(arxiv_bot, "http_get_arxiv_api",
                                   side_effect=pages),
                      patch.object(arxiv_bot.time, "sleep")):
                    with self.assertRaises(ValueError):
                        arxiv_bot.fetch_oai_records(
                            "physics:quant-ph", "2026-11-08", rule)

    def test_future_response_date_cannot_move_cursor_past_local_clock(
            self) -> None:
        page = oai_page([], response_date="2099-01-01T00:00:00Z")
        with patch.object(arxiv_bot, "http_get_arxiv_api", return_value=page):
            _rows, cursor, _ = arxiv_bot.fetch_oai_records(
                "physics:quant-ph", "2026-11-08", {})
        self.assertLess(cursor, "2099")

    def test_cached_stale_response_is_retried_with_a_new_url(self) -> None:
        now = datetime(2026, 11, 12, 6, 30, tzinfo=timezone.utc)
        stale = oai_page([], error=("noRecordsMatch", "none"),
                         response_date="2026-11-12T01:17:18Z")
        fresh = oai_page([raw_record("2611.00001", "2026-11-12", [V_NOV11],
                                     "quant-ph")],
                         response_date="2026-11-12T06:30:05Z")
        with (patch.object(arxiv_bot, "http_get_arxiv_api",
                           side_effect=[stale, fresh]) as get,
              patch.object(arxiv_bot.time, "sleep"),
              patch("sys.stderr", new=io.StringIO())):
            rows, cursor, _ = arxiv_bot.fetch_oai_records(
                "physics:quant-ph", "2026-11-09", {}, now=now)
        self.assertEqual([r["id"] for r in rows], ["2611.00001"])
        self.assertEqual(cursor, "2026-11-12T06:30:00Z")
        urls = [query(c.args[0]) for c in get.call_args_list]
        self.assertEqual([(u["from"], u["until"]) for u in urls],
                         [("2026-11-09", "2026-11-12"),
                          ("2026-11-08", "2026-11-12")])

    def test_all_stale_attempts_return_freshest_with_conservative_cursor(
            self) -> None:
        now = datetime(2026, 11, 12, 6, 30, tzinfo=timezone.utc)
        pages = [oai_page([], error=("noRecordsMatch", "none"),
                          response_date=stamp)
                 for stamp in ("2026-11-12T01:00:00Z", "2026-11-12T03:00:00Z")]
        with (patch.object(arxiv_bot, "http_get_arxiv_api",
                           side_effect=pages),
              patch.object(arxiv_bot.time, "sleep"),
              patch("sys.stderr", new=io.StringIO())):
            _rows, cursor, _ = arxiv_bot.fetch_oai_records(
                "physics:quant-ph", "2026-11-09", {"cache_retry_attempts": 2},
                now=now)
        self.assertEqual(cursor, "2026-11-12T03:00:00Z")

    def test_from_date_uses_cursor_overlap_and_optional_backfill(self) -> None:
        now = datetime(2026, 11, 12, 8, tzinfo=timezone.utc)
        rule = {"overlap_days": 3}
        self.assertEqual(arxiv_bot.oai_harvest_from_date(
            "2026-11-11T20:00:00Z", rule, now), "2026-11-08")
        self.assertEqual(arxiv_bot.oai_harvest_from_date(
            None, rule, now), "2026-11-09")
        self.assertEqual(arxiv_bot.oai_harvest_from_date(
            "2026-10-01T00:00:00Z", rule, now), "2026-09-28")
        self.assertEqual(arxiv_bot.oai_harvest_from_date(
            "2026-11-11T20:00:00Z", rule, now, extra_days=30), "2026-10-13")


class OaiSelectionTests(unittest.TestCase):
    def rows(self) -> list[dict]:
        page = oai_page([
            raw_record("2611.00001", "2026-11-11", [V_NOV10], "quant-ph"),
            raw_record("2611.00002", "2026-11-11", [V_NOV10],
                       "cs.IT quant-ph"),
            raw_record("2611.00003", "2026-11-11", [V_SEP], "quant-ph"),
            raw_record("2604.00004", "2026-11-11", [V_APR], "quant-ph"),
            raw_record("2605.00005", "2026-11-11", [V_APR, V_NOV10],
                       "quant-ph"),
        ])
        return arxiv_bot.parse_oai_list_records(page)[0]

    def test_feed_selection_keeps_first_versions_within_delay_bound(
            self) -> None:
        papers = arxiv_bot.select_oai_feed_papers(
            self.rows(), "quant-ph", {"max_announce_delay_days": 90})
        by_id = {p["id"]: p for p in papers}
        # A paper held 64 days in moderation is still a new announcement.
        self.assertEqual(sorted(by_id),
                         ["2611.00001", "2611.00002", "2611.00003"])
        self.assertEqual(by_id["2611.00001"]["announce_type"], "new")
        self.assertEqual(by_id["2611.00002"]["announce_type"], "cross")
        self.assertEqual(by_id["2611.00002"]["source_feed"], "quant-ph")
        self.assertNotIn("version_count", by_id["2611.00001"])
        self.assertNotIn("oai_datestamp", by_id["2611.00001"])
        self.assertTrue(all(arxiv_bot.should_post(p, {}) for p in papers))

    def test_external_selection_applies_mechanical_filters(self) -> None:
        page = oai_page([
            raw_record("2611.10001", "2026-11-11", [V_SEP], "cs.CR",
                       title="Post-quantum signatures"),
            raw_record("2611.10002", "2026-11-11", [V_NOV10], "cs.CR quant-ph",
                       title="Quantum key distribution"),
            raw_record("2611.10003", "2026-11-11", [V_NOV10], "cs.LG cs.CR",
                       title="Quantum learning"),
            raw_record("2611.10004", "2026-11-11", [V_NOV10], "cs.CR",
                       title="Web security", abstract="No such terms."),
            raw_record("2611.10005", "2026-11-11", [V_NOV10], "cs.CR",
                       title="Quantum attack"),
            raw_record("2611.10006", "2026-11-11", [V_APR, V_NOV10], "cs.CR",
                       title="Quantum revision"),
        ])
        records = arxiv_bot.parse_oai_list_records(page)[0]
        rows = arxiv_bot.select_oai_external_candidates(
            records, "cs.CR", {"terms": ["post-quantum", "quantum"]}, 90,
            set(), {"2611.10005"})
        self.assertEqual([p["id"] for p in rows], ["2611.10001"])
        self.assertEqual(rows[0]["announce_type"], "external")
        self.assertEqual(rows[0]["source_feed"], "cs.CR")


class OaiMainFlowTests(unittest.TestCase):
    def run_discover(self, cfg: dict, state: dict, rss: list[dict],
                     oai, extra_env: dict | None = None):
        reports: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cfg.json").write_text(json.dumps(cfg))
            (root / "state.json").write_text(json.dumps(state))
            with (patch.object(arxiv_bot, "CONFIG_PATH", root / "cfg.json"),
                  patch.object(arxiv_bot, "STATE_PATH", root / "state.json"),
                  patch.object(arxiv_bot, "LOG_PATH", root / "log.json"),
                  patch.object(arxiv_bot, "DIAGNOSTICS_PATH",
                               root / "errors.jsonl"),
                  patch.object(arxiv_bot, "fetch_feed", return_value=rss),
                  patch.object(arxiv_bot, "http_get_arxiv_api",
                               side_effect=oai) as get,
                  patch.object(arxiv_bot, "notify_run_report",
                               side_effect=lambda r, _c: reports.append(r)),
                  patch.object(arxiv_bot, "post_to_discord") as post,
                  patch.object(arxiv_bot.time, "sleep"),
                  patch.dict(os.environ, extra_env or {}, clear=True),
                  patch.object(sys, "argv",
                               ["arxiv_bot.py", "--discover-only"]),
                  patch("sys.stdout", new=io.StringIO())):
                code = None
                try:
                    arxiv_bot.main()
                except SystemExit as exc:
                    code = exc.code
            post.assert_not_called()
            saved = json.loads((root / "state.json").read_text())
        return code, saved, reports, get

    def base_cfg(self) -> dict:
        cfg = arxiv_bot.load_json(arxiv_bot.CONFIG_PATH, {})
        cfg["external_arxiv_queries"] = {}
        cfg["classify_with_llm"] = False
        return cfg

    def test_empty_rss_day_is_recovered_and_reported(self) -> None:
        page = oai_page([
            raw_record("2611.20001", "2026-11-11", [V_NOV10], "quant-ph"),
            raw_record("2611.20002", "2026-11-11", [V_NOV10],
                       "cs.IT quant-ph"),
            raw_record("2605.20003", "2026-11-11", [V_APR, V_NOV10],
                       "quant-ph"),
            raw_record("2604.20004", "2026-11-11", [V_APR], "quant-ph"),
            raw_record("2611.10000", "2026-11-11", [V_NOV10], "quant-ph"),
        ], response_date=(served := now_stamp()))
        state = {"seen": ["2611.10000"],
                 "feed_cursors": {"quant-ph": "2026-11-11T07:00:00Z"}}
        code, saved, reports, get = self.run_discover(
            self.base_cfg(), state, [], [page])
        self.assertIsNone(code)
        self.assertEqual(sorted(saved["deliveries"]),
                         ["2611.20001", "2611.20002"])
        self.assertEqual(
            saved["deliveries"]["2611.20002"]["paper"]["announce_type"],
            "cross")
        self.assertEqual(saved["oai_cursors"], {"physics:quant-ph": served})
        self.assertEqual(query(get.call_args.args[0])["from"], "2026-11-08")
        notice = reports[0]["source_notices"][0]
        self.assertEqual(notice["source"], "OAI-PMH:quant-ph")
        self.assertIn("2件", notice["message"])
        self.assertIn("2611.20001", notice["message"])
        self.assertEqual(reports[0]["source_failures"], [])

    def test_todays_listing_before_rss_rebuild_is_queued_quietly(
            self) -> None:
        now = datetime.now(timezone.utc)
        submitted = email.utils.format_datetime(now - timedelta(days=1),
                                                usegmt=True)
        yesterday_rss = {
            "id": "2611.10000", "title": "Quantum codes", "link": "",
            "authors": "", "announce_type": "new", "categories": ["quant-ph"],
            "primary": "quant-ph", "abstract": "Quantum error correction.",
        }
        for datestamp, expect_notice in [
            (now.date().isoformat(), False),
            ((now - timedelta(days=1)).date().isoformat(), True),
        ]:
            with self.subTest(datestamp=datestamp):
                page = oai_page([raw_record(
                    "2611.20001", datestamp, [submitted], "quant-ph")])
                code, saved, reports, _ = self.run_discover(
                    self.base_cfg(), {"seen": ["2611.10000"]},
                    [dict(yesterday_rss)], [page])
                self.assertIsNone(code)
                self.assertIn("2611.20001", saved["deliveries"])
                self.assertEqual(bool(reports), expect_notice)
                if expect_notice:
                    self.assertIn("RSS取りこぼし",
                                  reports[0]["source_notices"][0]["message"])

    def test_papers_already_in_rss_produce_no_recovery_notice(self) -> None:
        rss_row = {
            "id": "2611.20001", "title": "Quantum codes", "link": "",
            "authors": "", "announce_type": "new", "categories": ["quant-ph"],
            "primary": "quant-ph", "abstract": "Quantum error correction.",
        }
        page = oai_page([raw_record("2611.20001", "2026-11-11", [V_NOV10],
                                    "quant-ph")])
        code, saved, reports, _ = self.run_discover(
            self.base_cfg(), {"seen": []}, [rss_row], [page])
        self.assertIsNone(code)
        self.assertEqual(list(saved["deliveries"]), ["2611.20001"])
        self.assertEqual(reports, [])

    def test_oai_failure_keeps_cursor_and_queues_rss_rows(self) -> None:
        rss_row = {
            "id": "2611.20001", "title": "Quantum codes", "link": "",
            "authors": "", "announce_type": "new", "categories": ["quant-ph"],
            "primary": "quant-ph", "abstract": "Quantum error correction.",
        }
        state = {"seen": [], "oai_cursors": {
            "physics:quant-ph": "2026-11-01T00:00:00Z"}}
        code, saved, reports, _ = self.run_discover(
            self.base_cfg(), state, [rss_row], TimeoutError("offline"))
        self.assertEqual(code, 2)
        self.assertEqual(saved["oai_cursors"]["physics:quant-ph"],
                         "2026-11-01T00:00:00Z")
        self.assertIn("2611.20001", saved["deliveries"])
        self.assertEqual(reports[0]["source_failures"][0]["source"],
                         "arXiv OAI-PMH:physics:quant-ph")

    def test_external_categories_use_announcement_harvest(self) -> None:
        cfg = self.base_cfg()
        cfg["external_arxiv_queries"] = {"cs.IT": {
            "enabled": True, "terms": ["quantum"], "lookback_days": 4,
            "candidate_genres": ["qit"]}}

        def oai(url: str, _rule: dict) -> bytes:
            params = query(url)
            if params["set"] == "physics:quant-ph":
                return oai_page([], error=("noRecordsMatch", "none"))
            self.assertEqual(params["set"], "cs:cs:IT")
            # A moderation hold made this paper invisible to the old 4-day
            # submission-date window.
            return oai_page([raw_record(
                "2611.30001", "2026-11-11", [V_SEP], "cs.IT",
                title="Quantum channel capacity")])

        captured: dict = {}

        def review(candidates, *_args, **_kwargs):
            captured.update(candidates)
            return [], {}, {
                "mechanical_candidates": 1, "cached": 0, "reviewed": 0,
                "accepted": 0, "skipped": 0, "unreviewed": 0,
                "skip_disagreements": 0, "single_skip_pending": 0,
                "requests": 0, "classifier_counts": {}}

        with patch.object(arxiv_bot, "review_external_candidates",
                          side_effect=review):
            code, saved, _reports, _get = self.run_discover(
                cfg, {"seen": []}, [], oai)
        self.assertIsNone(code)
        self.assertEqual([p["id"] for p in captured["cs.IT"]], ["2611.30001"])
        self.assertIn("cs:cs:IT", saved["oai_cursors"])
        self.assertIn("cs.IT", saved["external_cursors"])

    def test_disabled_oai_falls_back_to_legacy_api_backfill(self) -> None:
        cfg = self.base_cfg()
        cfg["oai_harvest"] = {"enabled": False}
        with patch.object(arxiv_bot, "fetch_feed_backfill",
                          return_value=[]) as legacy:
            code, saved, _reports, get = self.run_discover(
                cfg, {"seen": []}, [], AssertionError("OAI must not run"))
        self.assertIsNone(code)
        legacy.assert_called_once()
        get.assert_not_called()
        self.assertEqual(saved["oai_cursors"], {})


class RecoveryTests(unittest.TestCase):
    def test_parse_recover_ids_accepts_free_form_operator_text(self) -> None:
        self.assertEqual(arxiv_bot.parse_recover_ids(
            "2609.35837v2, https://arxiv.org/abs/2609.35962 2609.35837\n"
            "2610.35931 12609.35837 2609.358371"),
            ["2609.35837", "2609.35962", "2610.35931"])
        self.assertEqual(arxiv_bot.parse_recover_ids(""), [])

    @staticmethod
    def get_record(pid: str, categories: str, *, versions: int = 1,
                   comments: str = "") -> bytes:
        dates = [V_SEP, V_NOV10][:versions]
        return oai_page([raw_record(pid, "2026-09-30", dates, categories,
                                    comments=comments)], verb="GetRecord")

    def test_fetch_maps_new_and_cross_and_rejects_unusable_ids(self) -> None:
        responses = {
            "2609.35837": self.get_record("2609.35837", "quant-ph",
                                          versions=2),
            "2609.35962": self.get_record("2609.35962",
                                          "cond-mat.str-el quant-ph"),
            "2609.35973": self.get_record("2609.35973", "cs.LG"),
            "2609.36000": self.get_record(
                "2609.36000", "quant-ph",
                comments="This paper has been withdrawn by the authors"),
            "2609.36001": oai_page([], error=("idDoesNotExist", "unknown"),
                                   verb="GetRecord"),
        }
        requested: list[dict[str, str]] = []

        def source(url: str, _rule: dict) -> bytes:
            params = query(url)
            requested.append(params)
            return responses[params["identifier"].rsplit(":", 1)[-1]]

        with (patch.object(arxiv_bot, "http_get_arxiv_api",
                           side_effect=source),
              patch.object(arxiv_bot.time, "sleep")):
            papers, rejected = arxiv_bot.fetch_recovery_papers(
                list(responses), "quant-ph", {})
        self.assertEqual([(p["id"], p["announce_type"], p["source_feed"])
                          for p in papers],
                         [("2609.35837", "new", "quant-ph"),
                          ("2609.35962", "cross", "quant-ph")])
        # A replaced paper is still recovered; only metadata keys of the
        # normal paper shape are kept.
        self.assertNotIn("withdrawn", papers[0])
        self.assertNotIn("version_count", papers[0])
        self.assertEqual(rejected, ["2609.35973", "2609.36000", "2609.36001"])
        self.assertEqual(requested[0], {
            "verb": "GetRecord", "metadataPrefix": "arXivRaw",
            "identifier": "oai:arXiv.org:2609.35837"})

    def test_discover_queues_requested_ids_once(self) -> None:
        cfg = arxiv_bot.load_json(arxiv_bot.CONFIG_PATH, {})
        cfg["external_arxiv_queries"] = {}
        cfg["classify_with_llm"] = False
        fetched: list[str] = []

        def source(url: str, _rule: dict) -> bytes:
            params = query(url)
            if params["verb"] == "ListRecords":
                return oai_page([], error=("noRecordsMatch", "none"))
            pid = params["identifier"].rsplit(":", 1)[-1]
            fetched.append(pid)
            categories = {"2609.35837": "quant-ph",
                          "2609.35962": "cond-mat.str-el quant-ph"}[pid]
            return self.get_record(pid, categories)

        code, saved, reports, _ = OaiMainFlowTests.run_discover(
            self, cfg, {"seen": ["2611.10000"]}, [], source,
            {"ARXIV_RECOVER_IDS": "2609.35837 2609.35962 2611.10000"})
        self.assertIsNone(code)
        self.assertEqual(fetched, ["2609.35837", "2609.35962"])
        self.assertEqual(sorted(saved["deliveries"]),
                         ["2609.35837", "2609.35962"])
        notice = reports[0]["source_notices"][0]
        self.assertEqual(notice["source"], "指定ID回収")
        self.assertIn("指定3件のうち2件", notice["message"])
        self.assertIn("処理済み・取得済み1件", notice["message"])


def queued(pid: str) -> dict:
    return {
        "paper": {
            "id": pid, "title": "A quantum title", "link": "",
            "authors": "", "announce_type": "new",
            "categories": ["quant-ph"], "primary": "quant-ph",
            "abstract": "An abstract that needs translation.",
            "source_feed": "quant-ph",
        },
        "genre_ids": ["qec"], "classifier": "test",
        "abstract_translated": None, "title_translated": None,
        "need_translation": True, "allow_untranslated": False,
        "channels": {"qec": {"status": "pending"}},
    }


class TranslationOutageTests(unittest.TestCase):
    def run_phase(self, root: Path, phase: str, env: dict, post_json):
        with (patch.object(arxiv_bot, "CONFIG_PATH", root / "cfg.json"),
              patch.object(arxiv_bot, "STATE_PATH", root / "state.json"),
              patch.object(arxiv_bot, "LOG_PATH", root / "log.json"),
              patch.object(arxiv_bot, "DIAGNOSTICS_PATH", root / "e.jsonl"),
              patch.object(arxiv_bot, "http_post_json", side_effect=post_json),
              patch.object(arxiv_bot.urllib.request, "urlopen",
                           side_effect=urllib.error.HTTPError(
                               "https://api-free.deepl.com/v2/translate", 456,
                               "Quota Exceeded", {}, io.BytesIO(b"{}"))),
              patch.object(arxiv_bot.time, "sleep"),
              patch.dict(os.environ, env, clear=True),
              patch.object(sys, "argv", ["arxiv_bot.py", phase]),
              patch("sys.stdout", new=io.StringIO()),
              patch("sys.stderr", new=io.StringIO())):
            arxiv_bot.main()

    def test_translate_phase_alerts_once_per_day_on_full_outage(self) -> None:
        cfg = arxiv_bot.load_json(arxiv_bot.CONFIG_PATH, {})
        self.assertEqual(cfg["main_translators"],
                         ["deepl", "azure", "google", "apps_script", "gemini"])
        env = {
            "DEEPL_API_KEY": "deepl-test",
            "AZURE_TRANSLATOR_KEY": "azure-test",
            "GOOGLE_TRANSLATE_API_KEY": "google-test",
            "GEMINI_API_KEY": "gemini-test",
            "DISCORD_WEBHOOK_BOT_EMERGENCY": "https://discord.test/emergency",
            "DISCORD_WEBHOOK_QEC": "https://discord.test/qec",
        }
        # Google's monthly and Gemini's daily free-tier budgets are used up.
        local = arxiv_bot._pacific_time()
        usage = {
            "google": {"period": local.strftime("%Y-%m"),
                       "used": cfg["google_monthly_char_limit"]},
            "gemini": {"period": local.strftime("%Y-%m-%d"),
                       "used": cfg["gemini_translation_daily_request_limit"]},
        }
        posts: list[tuple[str, dict]] = []

        def post_json(url, payload, headers=None, timeout=120):
            posts.append((url, payload))
            if "microsofttranslator" in url:
                return 401, b'{"error":{"code":401000}}'
            return 204, b""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cfg.json").write_text(json.dumps(cfg))
            (root / "state.json").write_text(json.dumps(
                {"deliveries": {"2611.40001": queued("2611.40001")},
                 "translation_usage": usage}))
            (root / "log.json").write_text("[]")
            with (patch.object(arxiv_bot, "_deepl_dead", False),
                  patch.object(arxiv_bot, "_azure_dead", False),
                  patch.object(arxiv_bot, "_google_dead", False),
                  patch.object(arxiv_bot, "_apps_script_dead", False),
                  patch.object(arxiv_bot, "_gemini_dead_models", set()),
                  patch.object(arxiv_bot, "_translation_budget", None),
                  patch.object(arxiv_bot, "_budget_exhausted", set()),
                  patch.object(arxiv_bot, "_translation_success", Counter())):
                self.run_phase(root, "--translate-only", env, post_json)
                self.run_phase(root, "--translate-only", env, post_json)
                alerts = [p for u, p in posts if u.endswith("/emergency")]
                self.assertEqual(len(alerts), 1)
                for state_text in ("deepl: 停止", "azure: 停止", "google: 停止",
                                   "gemini: 停止", "apps_script: 未設定"):
                    self.assertIn(state_text, alerts[0]["content"])
                # Exhausted budgets never reach Google or Gemini.
                self.assertFalse(any("googleapis" in u for u, _ in posts))
                saved = json.loads((root / "state.json").read_text())
                status = saved["translation_status"]
                self.assertEqual(status["dead"],
                                 ["deepl", "azure", "google", "gemini"])
                self.assertEqual(status["budget_exhausted"],
                                 ["gemini", "google"])
                self.assertEqual(status["unconfigured"], ["apps_script"])
                self.assertEqual(saved["translation_usage"], usage | {
                    name: usage[name] | {"limit": usage[name]["used"]}
                    for name in usage})
                self.assertEqual(status["waiting"], 1)
                self.assertEqual(
                    saved["translation_alerts"]["outage_notified_on"],
                    status["at"][:10])

                reports: list[dict] = []
                with patch.object(arxiv_bot, "notify_run_report",
                                  side_effect=lambda r, _c: reports.append(r)):
                    self.run_phase(root, "--deliver-only", env, post_json)
                self.assertEqual(reports[0]["dead_translators"],
                                 ["deepl", "azure", "google", "gemini"])
                self.assertEqual(reports[0]["unconfigured_translators"],
                                 ["apps_script"])
                self.assertEqual(
                    reports[0]["free_tier"]["google"]["used"],
                    cfg["google_monthly_char_limit"])
                self.assertEqual(len(reports[0]["deferred"]), 1)

    def test_no_alert_while_a_configured_backend_still_works(self) -> None:
        cfg = {"translators": ["deepl", "apps_script"]}
        state: dict = {}
        entry = arxiv_bot.entry_from_delivery(queued("2611.40002"), {})
        env = {"DEEPL_API_KEY": "k",
               "GOOGLE_APPS_SCRIPT_URL": "https://script.google.com/x",
               "GOOGLE_APPS_SCRIPT_TOKEN": "t" * 32}
        with (patch.dict(os.environ, env, clear=True),
              patch.object(arxiv_bot, "_deepl_dead", True),
              patch.object(arxiv_bot, "_apps_script_dead", False),
              patch.object(arxiv_bot, "notify_translation_outage") as alert):
            status = arxiv_bot.record_translation_status(state, [entry], cfg)
        alert.assert_not_called()
        self.assertEqual(status["dead"], ["deepl"])
        self.assertEqual(status["unconfigured"], [])
        self.assertEqual(state["translation_status"]["waiting"], 1)

    def test_unconfigured_apps_script_counts_as_unavailable(self) -> None:
        cfg = {"translators": ["deepl", "apps_script", "mystery"]}
        with patch.dict(os.environ, {"DEEPL_API_KEY": "k"}, clear=True):
            self.assertEqual(arxiv_bot.unconfigured_translators(cfg),
                             ["apps_script", "mystery"])


if __name__ == "__main__":
    unittest.main()
