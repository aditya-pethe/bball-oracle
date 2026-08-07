"""Loader validation, plus a structural check on the real seed set.

The seed-set test is deliberately here rather than in a live suite: it needs no
database and no API key, so a malformed case file fails in CI instead of forty
dollars into a model sweep.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from evals.harness.cases import CaseError, load_cases, parse_case

SEED_SET = Path(__file__).resolve().parents[1] / "text2sql-v0.yaml"


def minimal(**overrides):
    base = {
        "id": "t1-example",
        "tier": 1,
        "question": "How many shots were attempted?",
        "expects": "answer",
        "gold_sql": "SELECT COUNT(*) FROM nba.shot_detail",
    }
    base.update(overrides)
    return base


class TestParseCase:
    def test_minimal_answer_case(self):
        case = parse_case(minimal())
        assert case.id == "t1-example"
        assert case.is_execution_scored
        assert not case.order_matters

    def test_folded_question_whitespace_is_collapsed(self):
        case = parse_case(minimal(question="Who had the best\n  three-point percentage?\n"))
        assert case.question == "Who had the best three-point percentage?"

    def test_answer_case_requires_gold_sql(self):
        with pytest.raises(CaseError, match="requires gold_sql"):
            parse_case(minimal(gold_sql=None))

    def test_blank_gold_sql_is_treated_as_missing(self):
        with pytest.raises(CaseError, match="requires gold_sql"):
            parse_case(minimal(gold_sql="   \n  "))

    def test_clarify_case_requires_rubric(self):
        with pytest.raises(CaseError, match="requires a rubric"):
            parse_case(minimal(id="t4-x", tier=4, expects="clarify", gold_sql=None))

    def test_clarify_case_rejects_gold_sql(self):
        # Nothing would ever execute it, so its presence means the case was authored
        # under a misunderstanding of how it scores.
        with pytest.raises(CaseError, match="must not carry gold_sql"):
            parse_case(minimal(id="t4-x", tier=4, expects="clarify", rubric="..."))

    def test_decline_case_valid(self):
        case = parse_case(
            minimal(id="t5-x", tier=5, expects="decline", gold_sql=None, rubric="No salary data.")
        )
        assert not case.is_execution_scored
        assert case.rubric == "No salary data."

    def test_invalid_tier(self):
        with pytest.raises(CaseError, match="tier must be one of"):
            parse_case(minimal(tier=9))

    def test_invalid_expects(self):
        with pytest.raises(CaseError, match="expects must be one of"):
            parse_case(minimal(expects="maybe"))

    def test_unknown_key_rejected(self):
        # Typos in a case file are silent scoring bugs otherwise.
        with pytest.raises(CaseError, match="unknown keys"):
            parse_case(minimal(gold_sq1="SELECT 1"))

    def test_missing_id(self):
        raw = minimal()
        del raw["id"]
        with pytest.raises(CaseError, match="missing a string `id`"):
            parse_case(raw)

    def test_order_matters_requires_answer(self):
        with pytest.raises(CaseError, match="only meaningful with expects: answer"):
            parse_case(
                minimal(id="t4-x", tier=4, expects="clarify", gold_sql=None,
                        rubric="...", order_matters=True)
            )

    def test_trap_flag(self):
        assert parse_case(minimal(trap=True)).trap


class TestLoadCases:
    def test_duplicate_ids_rejected(self, tmp_path):
        path = tmp_path / "dupes.yaml"
        path.write_text(
            "cases:\n"
            "  - id: a\n    tier: 1\n    question: q\n    expects: answer\n    gold_sql: SELECT 1\n"
            "  - id: a\n    tier: 1\n    question: q\n    expects: answer\n    gold_sql: SELECT 2\n"
        )
        with pytest.raises(CaseError, match="duplicate case id"):
            load_cases(path)

    def test_missing_cases_key(self, tmp_path):
        path = tmp_path / "empty.yaml"
        path.write_text("something_else: []\n")
        with pytest.raises(CaseError, match="`cases` key"):
            load_cases(path)

    def test_empty_case_list(self, tmp_path):
        path = tmp_path / "empty.yaml"
        path.write_text("cases: []\n")
        with pytest.raises(CaseError, match="non-empty list"):
            load_cases(path)


class TestSeedSet:
    """Structural assertions on evals/text2sql-v0.yaml itself."""

    def test_loads(self):
        assert len(load_cases(SEED_SET)) == 25

    def test_tier_distribution(self):
        cases = load_cases(SEED_SET)
        by_tier: dict[int, int] = {}
        for case in cases:
            by_tier[case.tier] = by_tier.get(case.tier, 0) + 1
        assert set(by_tier) == {1, 2, 3, 4, 5}

    def test_seventeen_execution_scored_cases(self):
        # The plan's stated split: 17 with verified gold SQL, 8 judgment-scored.
        cases = load_cases(SEED_SET)
        assert sum(1 for c in cases if c.is_execution_scored) == 17
        assert sum(1 for c in cases if not c.is_execution_scored) == 8

    def test_every_judgment_case_has_a_rubric(self):
        for case in load_cases(SEED_SET):
            if not case.is_execution_scored:
                assert case.rubric, f"{case.id} is judgment-scored with no rubric"

    def test_trap_case_present_and_execution_scored(self):
        # Resolved 2026-08-06: the TRAP case scores strictly as `answer`. If someone
        # later relaxes it to accept `clarify`, this test is the tripwire.
        cases = {c.id: c for c in load_cases(SEED_SET)}
        trap = cases["t3-scoring-leaders-TRAP"]
        assert trap.trap
        assert trap.expects == "answer"
        assert trap.order_matters

    def test_ranking_cases_declare_order_matters(self):
        # A gold query with ORDER BY that forgets order_matters silently scores a
        # wrong ranking as correct.
        for case in load_cases(SEED_SET):
            if case.gold_sql and "ORDER BY" in case.gold_sql.upper():
                assert case.order_matters, (
                    f"{case.id} has ORDER BY in gold_sql but order_matters is false"
                )


class TestAlsoAccepts:
    """Cases where more than one response is genuinely defensible."""

    def test_widens_accepted_outcomes(self):
        case = parse_case(
            minimal(id="t4-x", tier=4, expects="clarify", gold_sql=None,
                    rubric="...", also_accepts=["decline"])
        )
        assert case.accepted_outcomes == {"clarify", "decline"}

    def test_absent_by_default(self):
        assert parse_case(minimal()).accepted_outcomes == {"answer"}

    def test_rejected_on_answer_cases(self):
        # An answerable question that accepts an abstention would make the
        # false-abstention metric unmeasurable.
        with pytest.raises(CaseError, match="not allowed on expects: answer"):
            parse_case(minimal(also_accepts=["clarify"]))

    def test_rejects_unknown_outcome(self):
        with pytest.raises(CaseError, match="also_accepts entries must be"):
            parse_case(minimal(id="t4-x", tier=4, expects="clarify", gold_sql=None,
                               rubric="...", also_accepts=["maybe"]))

    def test_rejects_repeating_expects(self):
        with pytest.raises(CaseError, match="repeats expects"):
            parse_case(minimal(id="t4-x", tier=4, expects="clarify", gold_sql=None,
                               rubric="...", also_accepts=["clarify"]))

    def test_seed_set_t4_improved_most_accepts_both(self):
        case = {c.id: c for c in load_cases(SEED_SET)}["t4-improved-most"]
        assert case.accepted_outcomes == {"clarify", "decline"}
