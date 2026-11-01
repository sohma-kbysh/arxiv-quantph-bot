"""Offline editorial regression examples, not an estimate of live accuracy.

The first five examples reproduce phrases from actual misrouted papers in
posted_log.json. Remaining cases are small, manually specified domain cases.
Labels are reviewable expectations; previous bot/model labels are not truth.
"""
from __future__ import annotations

import copy
import unittest

import arxiv_bot


# name, title, abstract, required genres, allowed genres
CASES = [
    ("2610.28031", "Microscopically informed self-consistent multi-resolvent closure for dense many-body spectra",
     "In a blind test on the interacting Ising chain, the Hamiltonian-derived closure predicts the tested probes.",
     {"sim"}, {"sim", "other"}),
    ("2610.29316", "Boundary exceptional point driven chiral switch of topological edge states",
     "A minimal framework for a large-scale topological chiral switch that is experimentally verifiable.",
     {"other"}, {"other"}),
    ("2610.29865", "From Traveling Waves to Synthetic Gauge Fields: A Field-Free Route to Superconducting Diode Effect",
     "The protocol is spin-blind; time-reversal breaking arises via a synthetic gauge flux rather than an effective Zeeman field.",
     {"other"}, {"other"}),
    ("2610.25233", "Noise-Awareness in Quantum Software Testing: An Empirical Landscape Analysis",
     "Noise awareness and error mitigation in quantum software are classified against a blinded, human-coded reference sample.",
     {"nisq"}, {"nisq"}),
    ("2610.06076", "Quantum data loading from the learned shared structure of real signals",
     "Quantum machine learning loads quantum data using a learned shared structure. In a preregistered blind replication the required subset size stayed constant.",
     {"qml"}, {"qml"}),
    ("qec-decoder", "Decoders for quantum LDPC codes",
     "We construct error-correcting codes and improve syndrome decoding thresholds.", {"qec"}, {"qec"}),
    ("qec-ft", "Transversal logical gates for quantum LDPC codes",
     "We construct quantum codes and fault-tolerant logical operations using transversal gates.",
     {"qec", "ft"}, {"qec", "ft"}),
    ("ft-magic", "Magic-state distillation with logical resource estimation",
     "We improve the cost of magic state injection for fault tolerant computation.", {"ft"}, {"ft"}),
    ("ft-dash", "Fault–tolerance with lattice surgery",
     "We implement logical operations with reduced overhead.", {"ft"}, {"ft"}),
    ("qit-capacity", "Entropies and quantum channel capacities",
     "We prove entropy inequalities for one-shot information and entanglement-assisted channel capacity.",
     {"qit"}, {"qit", "network"}),
    ("qit-resource", "Resource theories of coherence and entanglement",
     "We derive new entanglement measures and distillation bounds.", {"qit"}, {"qit"}),
    ("network-repeaters", "Quantum repeater networks with networked memories",
     "Entanglement distribution and entanglement swapping enable quantum internet routing.",
     {"network"}, {"network"}),
    ("crypto-network", "Quantum key distribution in quantum repeater networks",
     "We provide a security proof for a QKD routing protocol with entanglement distribution.",
     {"crypto", "network"}, {"crypto", "network"}),
    ("algo-grover", "Quantum search with amplitude amplification",
     "We improve Grover search and the quantum algorithm's query cost.", {"algo"}, {"algo"}),
    ("sim-trotter", "Hamiltonian simulation with Trotter product formulas",
     "Digital quantum simulation approximates many-body time evolution.", {"sim"}, {"sim", "algo"}),
    ("hardware-transmon", "High-fidelity two-qubit gates in superconducting transmons",
     "We calibrate readout and reduce crosstalk in a superconducting qubit processor.",
     {"hardware"}, {"hardware"}),
    ("hardware-atoms", "Neutral–atom quantum processors with Rydberg gates",
     "We experimentally improve two-qubit gate fidelity in neutral atoms.", {"hardware"}, {"hardware"}),
    ("sensing", "Ramsey spectroscopy for quantum metrology",
     "Spin squeezing improves quantum Fisher information in atomic clocks and magnetometry.",
     {"sensing"}, {"sensing"}),
    ("foundations", "Bell inequalities and quantum contextuality",
     "We study nonlocality and the foundations of quantum measurement theory.",
     {"foundations"}, {"foundations"}),
    ("nisq", "Variational quantum eigensolvers with error mitigation",
     "VQE and QAOA optimization avoid barren plateaus with zero-noise extrapolation.",
     {"nisq"}, {"nisq"}),
    ("qml", "Quantum kernels for quantum machine learning",
     "Quantum neural networks learn quantum data for supervised classification.", {"qml"}, {"qml"}),
    ("complexity", "Oracle separations between BQP and QMA",
     "We prove query complexity lower bounds and quantum complexity class separations.",
     {"complexity"}, {"complexity"}),
    ("pqc", "Lattice-based post-quantum signatures from learning with errors",
     "We analyze Dilithium and Kyber under quantum attacks on classical cryptography.", {"pqc"}, {"pqc"}),
    ("crypto-blind", "Blind quantum computation with verifiable delegation",
     "A client delegates quantum computation to an untrusted server and verifies the result.",
     {"crypto"}, {"crypto"}),
    ("qec-broad", "Classical source coding for quantum communication",
     "We construct lossless coding schemes for coherent network addresses.", {"qec"}, {"qec", "network", "qit"}),
]


def case_paper(case: tuple) -> dict:
    name, title, abstract, _required, _allowed = case
    return {"id": name, "title": title, "abstract": abstract,
            "source_feed": "quant-ph", "primary": "quant-ph",
            "announce_type": "new", "categories": ["quant-ph"]}


def evaluate_cases(module=arxiv_bot, cfg: dict | None = None) -> dict:
    """Expose a reproducible comparison usable with an earlier module/config."""
    cfg = cfg or module.load_json(module.CONFIG_PATH, {})
    module._classifier_cache = None
    correct = expected = unexpected = passed = 0
    failures = []
    for case in CASES:
        predicted = {g["id"] for g in module.classify_multi(
            case_paper(case), cfg["genres"], cfg)}
        required, allowed = case[3:]
        correct += len(predicted & required)
        expected += len(required)
        unexpected += len(predicted - allowed)
        if required <= predicted <= allowed:
            passed += 1
        else:
            failures.append({"case": case[0], "predicted": sorted(predicted),
                             "required": sorted(required), "allowed": sorted(allowed)})
    return {"cases_passed": passed, "cases": len(CASES),
            "required_labels_found": correct, "required_labels": expected,
            "unexpected_labels": unexpected, "failures": failures}


class ClassificationQualityTests(unittest.TestCase):
    def setUp(self):
        self.cfg = arxiv_bot.load_json(arxiv_bot.CONFIG_PATH, {})
        self.genres = self.cfg["genres"]

    def test_curated_domain_and_real_misrouting_cases(self):
        for case in CASES:
            with self.subTest(case=case[0]):
                predicted = {g["id"] for g in arxiv_bot.classify_multi(
                    case_paper(case), self.genres, self.cfg)}
                self.assertLessEqual(case[3], predicted)
                self.assertLessEqual(predicted, case[4])

    def test_keyword_variants_and_contained_phrases_do_not_multiply_evidence(self):
        paper = {"title": "Quantum error–correcting codes", "abstract": ""}
        genre = {"id": "qec", "keywords": ["quantum error correcting code"]}
        base = arxiv_bot._keyword_evidence_scores(paper, [genre], {})
        expanded = {"id": "qec", "keywords": [
            "quantum error correcting code", "quantum error-correcting codes",
            "error correction", "code", "codes"]}
        cfg = {"fallback_keyword_boosts": {"qec": [
            "quantum error correcting code", "error-correcting codes"]}}
        self.assertEqual(base, arxiv_bot._keyword_evidence_scores(paper, [expanded], cfg))

    def test_exact_tokens_avoid_acronym_and_prefix_collisions(self):
        genre = {"id": "crypto", "keywords": ["qkd", "blind"]}
        scores = arxiv_bot._keyword_evidence_scores(
            {"title": "AQKD and blinded analysis", "abstract": ""}, [genre], {})
        self.assertEqual(scores["crypto"], 0)

    def test_specific_crypto_forcing_preserves_ai_labels_and_rejects_generic_words(self):
        genre_map = {g["id"]: g for g in self.genres}
        for phrase in ["blind test", "spin-blind", "experimentally verifiable",
                       "secure memory allocation", "security of software packages"]:
            with self.subTest(phrase=phrase):
                result = arxiv_bot.apply_forced_genres(
                    {"title": phrase, "abstract": ""}, [genre_map["qit"]],
                    self.genres, self.cfg)
                self.assertEqual([g["id"] for g in result], ["qit"])
        result = arxiv_bot.apply_forced_genres(
            {"title": "Blind quantum computation", "abstract": ""},
            [genre_map["other"]], self.genres, self.cfg)
        self.assertEqual([g["id"] for g in result], ["crypto"])

    def test_tfidf_cache_updates_when_genre_definitions_change(self):
        paper = {"title": "syndrome", "abstract": "", "categories": []}
        genres = [{"id": "a", "classifier_description": "syndrome", "keywords": []},
                  {"id": "b", "classifier_description": "metrology", "keywords": []}]
        self.assertEqual(arxiv_bot.classify(paper, genres)["id"], "a")
        changed = copy.deepcopy(genres)
        changed[0]["classifier_description"] = "metrology"
        changed[1]["classifier_description"] = "syndrome"
        self.assertEqual(arxiv_bot.classify(paper, changed)["id"], "b")

    def test_repeated_category_hints_do_not_accumulate(self):
        paper = {"title": "entropy", "abstract": "", "categories": ["cs.IT"]}
        cfg = {"category_genre_hints": {"cs.IT": "qit", "math.IT": "qit"}}
        once = arxiv_bot._score_genres(paper, self.genres, cfg)
        paper["categories"].append("math.IT")
        self.assertEqual(once, arxiv_bot._score_genres(paper, self.genres, cfg))

    def test_trace_names_actual_crypto_and_qec_rules(self):
        paper = {**case_paper(CASES[-1]),
                 "title": "Blind quantum computation using quantum codes"}
        genre_map = {g["id"]: g for g in self.genres}
        final = arxiv_bot.postprocess_genres(
            paper, [genre_map["other"]], self.genres, self.cfg)
        trace = arxiv_bot.deterministic_genre_trace(
            paper, ["other"], final, self.cfg)
        self.assertEqual([event["rule"] for event in trace],
                         ["force_genre_keywords.crypto", "qec_adjacent_coding"])
        self.assertEqual(trace[0]["added_genre_ids"], ["crypto"])
        self.assertEqual(trace[0]["removed_genre_ids"], ["other"])
        self.assertIn("blind quantum computation", trace[0]["matched_terms"])
        self.assertEqual(trace[1]["added_genre_ids"], ["qec"])

    def test_trace_does_not_invent_a_primary_override_for_external_route(self):
        paper = {**case_paper(CASES[9]), "primary": "cs.IT",
                 "source_feed": "cs.IT", "announce_type": "external"}
        self.assertEqual(arxiv_bot.deterministic_genre_trace(
            paper, ["qit"], ["qit"], self.cfg), [])


if __name__ == "__main__":
    unittest.main()
