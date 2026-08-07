"""The comparator is the definition of 'correct' for tiers 1-3, so its edge cases
are enumerated here rather than discovered during a paid model sweep."""
from __future__ import annotations

from decimal import Decimal

import pytest

from evals.harness.comparator import (
    FLOAT_TOLERANCE,
    compare_results,
    rows_equal,
    values_equal,
)


class TestValuesEqual:
    def test_identical_ints(self):
        assert values_equal(5, 5)

    def test_numeric_tower_is_coerced(self):
        # The same correct answer arrives as int, float, or Decimal depending on how
        # the query was written. All three must compare equal.
        assert values_equal(5, 5.0)
        assert values_equal(Decimal("5"), 5)
        assert values_equal(Decimal("5.0"), 5.0)

    def test_float_tolerance(self):
        assert values_equal(1.0, 1.0 + FLOAT_TOLERANCE / 2)
        assert not values_equal(1.0, 1.0 + FLOAT_TOLERANCE * 10)

    def test_rounded_percentages_differ(self):
        # ROUND(AVG(...) * 100, 1) is all over the gold set; 45.2 vs 45.3 is a real
        # difference, not a tolerance artifact.
        assert not values_equal(Decimal("45.2"), Decimal("45.3"))

    def test_bool_is_not_a_number(self):
        # bool subclasses int in Python. A shot_made_flag of True must never compare
        # equal to a count of 1.
        assert not values_equal(True, 1)
        assert not values_equal(False, 0)
        assert values_equal(True, True)
        assert not values_equal(True, False)

    def test_nulls(self):
        assert values_equal(None, None)
        assert not values_equal(None, 0)
        assert not values_equal(None, "")
        assert not values_equal(0, None)

    def test_nan_never_equal(self):
        nan = float("nan")
        assert not values_equal(nan, nan)

    def test_strings_exact(self):
        assert values_equal("Stephen Curry", "Stephen Curry")
        assert not values_equal("Stephen Curry", "stephen curry")

    def test_same_type_non_numeric_compares_directly(self):
        assert values_equal("2024-01-15", "2024-01-15")
        assert not values_equal("2024-01-15", "2024-01-16")

    def test_cross_type_falls_back_to_string_form(self):
        # Deliberately permissive: `game_date` and `game_date::text` are the same
        # answer to a user, and a spurious failure there would be noise. The known
        # limitation is that the fallback is textual, so 5 matches "5" but 5.0 does
        # not (str(5.0) == "5.0") -- acceptable, because a gold/agent pair that
        # disagrees on int-vs-float typing is already caught by the numeric tower.
        assert values_equal("5", 5)
        assert not values_equal("5", 6)


class TestRowsEqual:
    def test_matching_rows(self):
        assert rows_equal(("Curry", 100), ("Curry", 100.0))

    def test_different_width(self):
        assert not rows_equal(("Curry", 100), ("Curry", 100, 1))


class TestCompareResults:
    def test_empty_results_match(self):
        assert compare_results([], []).matches

    def test_row_count_mismatch(self):
        result = compare_results([(1,), (2,)], [(1,)])
        assert not result.matches
        assert result.mismatch.kind == "row_count"

    def test_column_count_mismatch(self):
        # An extra column is a different answer shape, not a superset.
        result = compare_results([("Curry", 100)], [("Curry", 100, 1)])
        assert not result.matches
        assert result.mismatch.kind == "column_count"

    def test_column_names_are_irrelevant(self):
        # The comparator never sees column names at all -- this test exists to pin
        # that fact in place should the signature ever grow them.
        assert compare_results([(42,)], [(42,)]).matches

    def test_row_order_ignored_by_default(self):
        gold = [("Curry", 3), ("Jokic", 1), ("Doncic", 2)]
        agent = [("Doncic", 2), ("Curry", 3), ("Jokic", 1)]
        assert compare_results(gold, agent).matches

    def test_row_order_enforced_when_requested(self):
        gold = [("Curry", 3), ("Jokic", 1)]
        agent = [("Jokic", 1), ("Curry", 3)]
        assert compare_results(gold, agent, order_matters=True).matches is False
        assert compare_results(gold, agent, order_matters=False).matches is True

    def test_ordered_comparison_reports_row_index(self):
        result = compare_results([(1,), (2,)], [(2,), (1,)], order_matters=True)
        assert result.mismatch.kind == "row_values"
        assert "at row 0" in result.mismatch.detail

    def test_multiset_semantics_duplicates_matter(self):
        # Set semantics would call these equal. Bag semantics must not: an agent that
        # emitted a spurious DISTINCT collapsed a real duplicate.
        gold = [("Curry",), ("Curry",), ("Jokic",)]
        agent = [("Curry",), ("Jokic",), ("Jokic",)]
        assert not compare_results(gold, agent).matches

    def test_multiset_identical_duplicates_match(self):
        gold = [("Curry",), ("Curry",)]
        agent = [("Curry",), ("Curry",)]
        assert compare_results(gold, agent).matches

    def test_unordered_comparison_tolerates_float_noise(self):
        gold = [(1.0000000, "a"), (2.0000000, "b")]
        agent = [(2.0000001, "b"), (1.0000001, "a")]
        assert compare_results(gold, agent).matches

    def test_nulls_sort_and_compare(self):
        gold = [(None, 1), ("Curry", 2)]
        agent = [("Curry", 2), (None, 1)]
        assert compare_results(gold, agent).matches

    def test_mixed_null_and_value_mismatch(self):
        assert not compare_results([(None,)], [("",)]).matches

    def test_trap_case_shape(self):
        # The flagship case: shot_detail-only scoring drops free throws, so the
        # totals differ by hundreds. Execution accuracy catches it with no
        # special-casing -- this is that claim, asserted.
        gold = [("Luka Doncic", 2370), ("Shai Gilgeous-Alexander", 2254)]
        fg_only = [("Luka Doncic", 1892), ("Shai Gilgeous-Alexander", 1810)]
        result = compare_results(gold, fg_only, order_matters=True)
        assert not result.matches
        assert result.mismatch.kind == "row_values"

    def test_single_aggregate_value(self):
        assert compare_results([(567662,)], [(567662,)]).matches
        assert not compare_results([(567662,)], [(567661,)]).matches
