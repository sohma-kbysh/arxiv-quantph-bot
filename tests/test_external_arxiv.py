from __future__ import annotations

from datetime import datetime, timezone
import os
import unittest
from unittest.mock import patch
import urllib.parse

import arxiv_bot


ATOM_FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2607.12345v2</id>
    <updated>2026-07-30T01:02:03Z</updated>
    <published>2026-07-29T01:02:03Z</published>
    <title>
      A Post-Quantum Construction
    </title>
    <summary>We construct an ML-KEM variant.</summary>
    <author><name>Alice Example</name></author>
    <author><name>Bob Example</name></author>
    <category term="cs.CR"/>
    <arxiv:primary_category term="cs.CR"/>
    <link rel="alternate" href="https://arxiv.org/abs/2607.12345"/>
  </entry>
</feed>
"""


def genre(gid: str) -> dict:
    return {
        "id": gid,
        "name": gid,
        "description": f"description for {gid}",
        "keywords": [],
    }


class ExternalArxivTests(unittest.TestCase):
    def test_build_query_combines_category_and_terms(self) -> None:
        url = arxiv_bot.build_external_arxiv_query(
            "cs.CR", ["post quantum", "ML-KEM"], 25)
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "export.arxiv.org")
        self.assertEqual(params["max_results"], ["25"])
        self.assertEqual(params["start"], ["0"])
        self.assertIn("cat:cs.CR", params["search_query"][0])
        self.assertIn('all:"post quantum"', params["search_query"][0])
        self.assertIn("all:ML-KEM", params["search_query"][0])

    def test_parse_external_atom_preserves_primary_and_strips_version(self) -> None:
        papers = arxiv_bot.parse_external_atom(ATOM_FIXTURE, "cs.CR")
        self.assertEqual(len(papers), 1)
        paper = papers[0]
        self.assertEqual(paper["id"], "2607.12345")
        self.assertEqual(paper["primary"], "cs.CR")
        self.assertEqual(paper["source_feed"], "cs.CR")
        self.assertEqual(paper["authors"], "Alice Example, Bob Example")
        self.assertEqual(paper["announce_type"], "external")

    def test_external_fetch_splits_long_term_lists_and_deduplicates(self) -> None:
        rule = {
            "terms": ["one", "two", "three"],
            "terms_per_query": 2,
            "max_results": 10,
            "query_min_interval_sec": 0,
        }
        with patch.object(
                arxiv_bot, "http_get", return_value=ATOM_FIXTURE) as get:
            papers = arxiv_bot.fetch_external_arxiv("cs.CR", rule)
        self.assertEqual(get.call_count, 2)
        self.assertEqual([paper["id"] for paper in papers], ["2607.12345"])

    def test_external_fetch_pages_until_lookback_boundary(self) -> None:
        old_page = (
            ATOM_FIXTURE
            .replace(b"2607.12345", b"2607.00001")
            .replace(
                b"2026-07-29T01:02:03Z",
                b"2020-01-01T01:02:03Z")
        )
        rule = {
            "terms": ["quantum"],
            "terms_per_query": 1,
            "max_results": 1,
            "lookback_days": 4,
            "query_min_interval_sec": 0,
        }
        with (
            patch.object(
                arxiv_bot, "http_get",
                side_effect=[ATOM_FIXTURE, old_page]) as get,
            patch.object(
                arxiv_bot, "external_paper_is_recent",
                side_effect=[True, False]),
        ):
            papers = arxiv_bot.fetch_external_arxiv("cs.CR", rule)
        self.assertEqual(get.call_count, 2)
        starts = [
            urllib.parse.parse_qs(
                urllib.parse.urlparse(call.args[0]).query)["start"][0]
            for call in get.call_args_list
        ]
        self.assertEqual(starts, ["0", "1"])
        self.assertEqual(
            [paper["id"] for paper in papers],
            ["2607.12345", "2607.00001"],
        )

    def test_local_term_match_uses_token_boundaries(self) -> None:
        paper = {
            "title": "Security analysis of a protocol",
            "abstract": "No post quantum claims are made.",
        }
        self.assertFalse(arxiv_bot.matches_external_terms(paper, ["SIS"]))
        self.assertTrue(
            arxiv_bot.matches_external_terms(paper, ["post-quantum"]))

    def test_lookback_is_bounded(self) -> None:
        now = datetime(2026, 7, 30, tzinfo=timezone.utc)
        recent = {"published": "2026-07-27T12:00:00Z"}
        old = {"published": "2026-07-20T12:00:00Z"}
        self.assertTrue(arxiv_bot.external_paper_is_recent(recent, 4, now))
        self.assertFalse(arxiv_bot.external_paper_is_recent(old, 4, now))

    def test_candidate_selection_requires_primary_and_no_quantph_cross(self) -> None:
        now = datetime(2026, 7, 30, tzinfo=timezone.utc)
        base = {
            "title": "Quantum channel coding",
            "abstract": "A quantum information result.",
            "published": "2026-07-29T12:00:00Z",
        }
        fetched = [
            {
                **base, "id": "accepted", "primary": "cs.IT",
                "categories": ["cs.IT"],
            },
            {
                **base, "id": "secondary-only", "primary": "cs.LG",
                "categories": ["cs.LG", "cs.IT"],
            },
            {
                **base, "id": "already-crossed", "primary": "cs.IT",
                "categories": ["cs.IT", "quant-ph"],
            },
            {
                **base, "id": "already-core", "primary": "cs.IT",
                "categories": ["cs.IT"],
            },
        ]
        selected = arxiv_bot.select_external_candidates(
            fetched,
            "cs.IT",
            {"terms": ["quantum"], "lookback_days": 4},
            {"already-core"},
            set(),
            now,
        )
        self.assertEqual([paper["id"] for paper in selected], ["accepted"])

    def test_external_review_accepts_allowed_genre_and_caches_skip(self) -> None:
        genres = [genre("pqc"), genre("crypto"), genre("other")]
        cfg = {
            "gemini_model_primary": "gemini-test",
            "gemini_model_secondary": "gemini-test-2",
            "translate_batch_size": 5,
            "max_translate_chars": 2000,
            "external_arxiv_queries": {
                "cs.CR": {
                    "candidate_genres": ["pqc"],
                    "review_instructions": "Only substantive PQC.",
                }
            },
        }
        papers = [
            {
                "id": "2607.00001",
                "title": "New ML-KEM proof",
                "abstract": "A post-quantum security proof.",
            },
            {
                "id": "2607.00002",
                "title": "Web security scanner",
                "abstract": "Generic cybersecurity.",
            },
        ]
        responses = [
            "<<<1>>> pqc\n<<<2>>> skip",
            "<<<1>>> skip",
        ]
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test"}, clear=False):
            with patch.object(
                    arxiv_bot, "_gemini_request", side_effect=responses):
                accepted, reviews, stats = (
                    arxiv_bot.review_external_candidates(
                        {"cs.CR": papers}, cfg, genres, {}))

        self.assertEqual(
            [paper["id"] for paper in accepted], ["2607.00001"])
        self.assertEqual(
            reviews["cs.CR:2607.00001"]["genre_ids"], ["pqc"])
        self.assertEqual(
            reviews["cs.CR:2607.00001"]["paper"]["id"], "2607.00001")
        self.assertEqual(reviews["cs.CR:2607.00002"]["genre_ids"], [])
        self.assertEqual(stats["reviewed"], 2)
        self.assertEqual(stats["accepted"], 1)
        self.assertEqual(stats["skipped"], 1)

    def test_configured_cs_cr_query_covers_pqc_and_quantum_crypto(self) -> None:
        cfg = arxiv_bot.load_json(arxiv_bot.CONFIG_PATH, {})
        rule = cfg["external_arxiv_queries"]["cs.CR"]
        self.assertEqual(rule["candidate_genres"], ["pqc", "crypto"])
        self.assertTrue(rule["allow_all_genres"])
        self.assertIn("quantum", rule["terms"])
        self.assertIn("PQC", rule["terms"])
        self.assertIn("quantum key distribution", rule["terms"])
        self.assertIn("QKD", rule["terms"])
        self.assertIn("QKD", rule["review_instructions"])
        self.assertIn("CLASSICAL cryptography", rule["review_instructions"])

    def test_external_review_can_route_qkd_to_crypto(self) -> None:
        genres = [genre("pqc"), genre("crypto")]
        cfg = {
            "gemini_model_primary": "gemini-test",
            "translate_batch_size": 5,
            "external_arxiv_queries": {
                "cs.CR": {
                    "candidate_genres": ["pqc", "crypto"],
                    "review_instructions": "QKD goes to crypto.",
                }
            },
        }
        paper = {
            "id": "2607.00004",
            "title": "Composable security for twin-field QKD",
            "abstract": "A quantum key distribution security proof.",
        }
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test"}, clear=False):
            with patch.object(
                    arxiv_bot, "_gemini_request",
                    return_value="<<<1>>> crypto"):
                accepted, reviews, _ = arxiv_bot.review_external_candidates(
                    {"cs.CR": [paper]}, cfg, genres, {})
        self.assertEqual(accepted[0]["external_genre_ids"], ["crypto"])
        self.assertEqual(
            reviews["cs.CR:2607.00004"]["genre_ids"], ["crypto"])

    def test_external_review_accepts_when_second_model_overrules_skip(self) -> None:
        genres = [genre("pqc"), genre("crypto"), genre("algo"), genre("other")]
        cfg = {
            "gemini_model_primary": "gemini-test",
            "gemini_model_secondary": "gemini-test-2",
            "external_skip_consensus": 2,
            "external_arxiv_queries": {
                "cs.CR": {
                    "candidate_genres": ["pqc", "crypto"],
                    "allow_all_genres": True,
                    "excluded_genres": ["other"],
                }
            },
        }
        paper = {
            "id": "2607.00005",
            "title": "Quantum cryptanalysis with Simon's algorithm",
            "abstract": "We attack a classical cipher on quantum hardware.",
        }
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test"}, clear=False):
            with patch.object(
                    arxiv_bot, "_gemini_request",
                    side_effect=["<<<1>>> skip", "<<<1>>> algo,crypto"]):
                accepted, reviews, stats = (
                    arxiv_bot.review_external_candidates(
                        {"cs.CR": [paper]}, cfg, genres, {}))
        self.assertEqual(
            accepted[0]["external_genre_ids"], ["algo", "crypto"])
        self.assertEqual(
            reviews["cs.CR:2607.00005"]["skip_votes"], ["gemini-test"])
        self.assertEqual(stats["skip_disagreements"], 1)
        self.assertEqual(stats["skipped"], 0)

    def test_single_skip_vote_remains_unreviewed(self) -> None:
        genres = [genre("complexity"), genre("algo")]
        cfg = {
            "gemini_model_primary": "gemini-only",
            "external_skip_consensus": 2,
            "external_arxiv_queries": {
                "cs.CC": {"candidate_genres": ["complexity", "algo"]}
            },
        }
        paper = {
            "id": "2607.00006",
            "title": "Classical SAT",
            "abstract": "Quantum computing is future work.",
        }
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test"}, clear=False):
            with patch.object(
                    arxiv_bot, "_gemini_request",
                    return_value="<<<1>>> skip"):
                accepted, reviews, stats = (
                    arxiv_bot.review_external_candidates(
                        {"cs.CC": [paper]}, cfg, genres, {}))
        self.assertEqual(accepted, [])
        self.assertEqual(reviews, {})
        self.assertEqual(stats["single_skip_pending"], 1)
        self.assertIn("cs.CC:2607.00006", stats["_pending_papers"])
        self.assertEqual(stats["unreviewed"], 1)

    def test_allow_all_genres_treats_source_mapping_as_soft_hint(self) -> None:
        genres = [
            genre("qit"), genre("qec"), genre("network"),
            genre("algo"), genre("crypto"), genre("other"),
        ]
        rule = {
            "candidate_genres": ["qit", "qec", "network"],
            "allow_all_genres": True,
            "excluded_genres": ["other"],
        }
        self.assertEqual(
            arxiv_bot.external_allowed_genre_ids(rule, genres),
            ["qit", "qec", "network", "algo", "crypto"],
        )

    def test_cached_external_skip_does_not_use_global_seen_semantics(self) -> None:
        genres = [genre("complexity"), genre("algo")]
        cfg = {
            "external_arxiv_queries": {
                "cs.CC": {"candidate_genres": ["complexity", "algo"]}
            }
        }
        paper = {
            "id": "2607.00003",
            "title": "Classical lower bound",
            "abstract": "No quantum result.",
        }
        cached = {
            "cs.CC:2607.00003": {
                "genre_ids": [],
                "classifier": "gemini-test",
            }
        }
        accepted, reviews, stats = arxiv_bot.review_external_candidates(
            {"cs.CC": [paper]}, cfg, genres, cached)
        self.assertEqual(accepted, [])
        self.assertEqual(reviews, {})
        self.assertEqual(stats["cached"], 1)
        self.assertEqual(stats["skipped"], 1)

    def test_translation_always_prioritizes_quantph_over_external(self) -> None:
        cfg = {"translation_priority_genres": ["pqc", "other"]}
        external_high_genre = {
            "paper": {"id": "external", "announce_type": "external"},
            "genres": [genre("pqc")],
        }
        quantph_low_genre = {
            "paper": {"id": "quantph", "announce_type": "new"},
            "genres": [genre("other")],
        }
        ordered = sorted(
            [external_high_genre, quantph_low_genre],
            key=lambda entry: arxiv_bot.translation_priority(entry, cfg),
        )
        self.assertEqual(
            [entry["paper"]["id"] for entry in ordered],
            ["quantph", "external"],
        )


if __name__ == "__main__":
    unittest.main()
