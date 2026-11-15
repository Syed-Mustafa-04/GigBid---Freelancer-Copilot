"""Metrics are tested against hand-computed values.

A quietly wrong metric is worse than no metric: it still produces a table, and
the table still gets believed. Each case below has its expected value derived
by hand in the comment so the test is checkable without running it.
"""

from __future__ import annotations

import pytest

from evals.metrics import (
    accuracy,
    cohens_kappa,
    dcg,
    macro_f1,
    ndcg_at_k,
    precision_at_k,
    precision_recall_f1,
    quadratic_weighted_kappa,
    recall_at_k,
    reciprocal_rank,
    two_proportion_z_test,
    wilson_interval,
)


class TestRecall:
    def test_all_gold_in_top_k(self):
        # gold {a,b}, top-3 = a,b,c -> 2 hits / min(2,3)=2 -> 1.0
        assert recall_at_k(["a", "b", "c"], {"a", "b"}, 3) == 1.0

    def test_gold_larger_than_k_is_not_capped_at_an_unreachable_value(self):
        # gold has 5 items, k=2, both retrieved are gold.
        # Naive denominator (5) would give 0.4 and no system could ever score 1.
        # min(5,2)=2 -> 2/2 = 1.0
        assert recall_at_k(["a", "b"], {"a", "b", "c", "d", "e"}, 2) == 1.0

    def test_partial(self):
        # gold {a,b,c}, top-3 = a,x,y -> 1 hit / min(3,3)=3 -> 0.333…
        assert recall_at_k(["a", "x", "y"], {"a", "b", "c"}, 3) == pytest.approx(1 / 3)

    def test_empty_gold_is_zero_not_an_error(self):
        assert recall_at_k(["a"], set(), 3) == 0.0

    def test_items_past_k_are_ignored(self):
        assert recall_at_k(["x", "y", "a"], {"a"}, 2) == 0.0


class TestPrecision:
    def test_half(self):
        assert precision_at_k(["a", "x"], {"a"}, 2) == 0.5

    def test_short_result_list_uses_actual_length(self):
        # Only one result returned, and it is correct -> 1.0, not 1/5.
        assert precision_at_k(["a"], {"a"}, 5) == 1.0


class TestReciprocalRank:
    def test_first_position_is_one(self):
        assert reciprocal_rank(["a", "b"], {"a"}) == 1.0

    def test_third_position(self):
        assert reciprocal_rank(["x", "y", "a"], {"a"}) == pytest.approx(1 / 3)

    def test_ranks_count_from_one_not_zero(self):
        # A zero-based implementation would divide by zero here.
        assert reciprocal_rank(["a"], {"a"}) == 1.0

    def test_no_hit(self):
        assert reciprocal_rank(["x", "y"], {"a"}) == 0.0


class TestNDCG:
    def test_dcg_matches_hand_calculation(self):
        # rel = [1,0,1] -> 1/log2(2) + 0 + 1/log2(4) = 1 + 0.5 = 1.5
        assert dcg([1.0, 0.0, 1.0]) == pytest.approx(1.5)

    def test_perfect_ranking_is_one(self):
        assert ndcg_at_k(["a", "b"], {"a", "b"}, 2) == pytest.approx(1.0)

    def test_order_matters(self):
        good = ndcg_at_k(["a", "x", "y"], {"a"}, 3)
        bad = ndcg_at_k(["x", "y", "a"], {"a"}, 3)
        assert good > bad

    def test_ideal_is_capped_at_k(self):
        # 4 gold items but k=2: ideal is [1,1], so retrieving 2 gold -> 1.0
        assert ndcg_at_k(["a", "b"], {"a", "b", "c", "d"}, 2) == pytest.approx(1.0)


class TestClassification:
    def test_precision_recall_f1_hand_computed(self):
        pred = ["bid", "bid", "skip", "skip"]
        gold = ["bid", "skip", "skip", "bid"]
        # tp=1 (i0), fp=1 (i1), fn=1 (i3), tn=1 (i2)
        result = precision_recall_f1(pred, gold, "bid")
        assert result["tp"] == 1 and result["fp"] == 1 and result["fn"] == 1
        assert result["precision"] == 0.5
        assert result["recall"] == 0.5
        assert result["f1"] == 0.5

    def test_no_predictions_does_not_divide_by_zero(self):
        result = precision_recall_f1(["skip", "skip"], ["bid", "bid"], "bid")
        assert result["precision"] == 0.0 and result["f1"] == 0.0

    def test_accuracy(self):
        assert accuracy(["a", "b", "c"], ["a", "b", "x"]) == pytest.approx(2 / 3, abs=1e-4)

    def test_macro_f1_averages_classes_equally(self):
        pred = ["a", "a", "b", "b"]
        gold = ["a", "a", "b", "b"]
        assert macro_f1(pred, gold, ["a", "b"]) == 1.0


class TestAgreement:
    def test_perfect_agreement_is_one(self):
        assert cohens_kappa(["a", "b", "a"], ["a", "b", "a"]) == 1.0

    def test_chance_agreement_is_near_zero(self):
        # Both raters say "a" 50% of the time at random; agreement is chance.
        a = ["a", "b", "a", "b"]
        b = ["a", "a", "b", "b"]
        assert cohens_kappa(a, b) == pytest.approx(0.0, abs=0.01)

    def test_kappa_punishes_skewed_agreement(self):
        # 9/10 agreement, but both almost always say "ok" -> kappa well below 0.9
        a = ["ok"] * 9 + ["bad"]
        b = ["ok"] * 10
        assert cohens_kappa(a, b) < 0.5

    def test_quadratic_weighted_kappa_tolerates_near_misses(self):
        exact = quadratic_weighted_kappa([1, 2, 3, 4, 5], [1, 2, 3, 4, 5])
        near = quadratic_weighted_kappa([1, 2, 3, 4, 5], [1, 2, 3, 4, 4])
        far = quadratic_weighted_kappa([1, 2, 3, 4, 5], [5, 4, 3, 2, 1])
        assert exact == 1.0
        assert near > far


class TestIntervals:
    def test_wilson_does_not_report_certainty_from_three_samples(self):
        low, high = wilson_interval(3, 3)
        assert low < 1.0, "3/3 must not read as a certain 100%"
        assert high <= 1.0

    def test_wilson_narrows_with_more_data(self):
        narrow = wilson_interval(500, 1000)
        wide = wilson_interval(5, 10)
        assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])

    def test_zero_trials_is_safe(self):
        assert wilson_interval(0, 0) == (0.0, 0.0)

    def test_two_proportion_detects_a_real_difference(self):
        result = two_proportion_z_test(80, 100, 20, 100)
        assert result["significant"] == 1.0
        assert result["p_value"] < 0.001

    def test_two_proportion_does_not_cry_wolf_on_noise(self):
        result = two_proportion_z_test(51, 100, 49, 100)
        assert result["significant"] == 0.0

    # --- exact values -----------------------------------------------------
    #
    # The tests above are property tests: they check the interval narrows, and
    # that the z-test fires on a real difference and not on noise. Those would
    # all still pass if the formula were subtly wrong -- a dropped continuity
    # term, z confused with z/2 -- so they do not support the claim that the
    # statistics are checked against hand-computed values.
    #
    # These do. Each expected value was derived independently from the closed
    # form rather than captured from this implementation's output, so a
    # transcription error in evals/metrics.py fails here instead of shipping a
    # confidence interval that is quietly the wrong width.

    def test_wilson_matches_an_independent_derivation(self):
        """12/40 at 95%: centre 0.34802/1.09604, half-width 0.14991/1.09604."""
        low, high = wilson_interval(12, 40)
        assert round(low, 4) == 0.1807
        assert round(high, 4) == 0.4543

    def test_wilson_matches_the_published_reference_for_one_in_ten(self):
        """(0.0179, 0.4042) is the standard worked example for 1/10 at 95%."""
        low, high = wilson_interval(1, 10)
        assert round(low, 4) == 0.0179
        assert round(high, 4) == 0.4042

    def test_wilson_lower_bound_is_clamped_not_negative(self):
        """0/10 derives to -0.0; a negative reply rate would be nonsense."""
        low, high = wilson_interval(0, 10)
        assert low == 0.0
        assert round(high, 4) == 0.2775

    def test_wilson_is_asymmetric_about_the_point_estimate(self):
        """The asymmetry is the whole reason for preferring Wilson to normal."""
        low, high = wilson_interval(1, 10)
        assert (0.1 - low) < (high - 0.1)

    def test_two_proportion_matches_an_independent_derivation(self):
        """60/100 vs 45/100: pooled 0.525, SE 0.0706223, z 2.123977."""
        result = two_proportion_z_test(60, 100, 45, 100)
        assert round(result["z"], 3) == 2.124
        assert round(result["p_value"], 5) == 0.03367
        assert result["significant"] == 1.0


class TestRateLimitRetry:
    """A 429 must not consume the retry budget.

    The first version of the rate-limit handler called `continue`, but the
    retry loop increments its counter at the top, so three throttled calls
    exhausted `max_attempts` and the node failed anyway - the exact failure
    the handler was written to prevent. Caught by watching a live run.
    """

    async def test_throttling_is_retried_beyond_max_attempts(self):
        import agent.llm as llm_module
        from agent.llm import LLMClient, ProviderRateLimited

        calls = {"n": 0}

        class Throttling:
            name = "openai_compatible"

            async def complete(self, **kwargs):
                calls["n"] += 1
                if calls["n"] <= 5:  # more 429s than max_attempts
                    raise ProviderRateLimited(0.01, "simulated")
                return '{"ok": true}', llm_module.Usage(10, 10)

        client = LLMClient()
        client._primary = lambda: (Throttling(), "openai_compatible")  # type: ignore[method-assign]
        result = await client.complete("scout", [{"role": "user", "content": "hi"}], max_attempts=3)
        assert calls["n"] == 6, f"gave up after {calls['n']} calls; throttling ate the retries"
        assert result.text == '{"ok": true}'
