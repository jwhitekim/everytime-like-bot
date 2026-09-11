import unittest

from app.core.services import score_calculator as sc


class FakeRng:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


def make_taste(**overrides):
    taste = {
        "preferences": {"usefulness": 1.0, "originality": 1.0},
        "penalties": {"promotion": 1.0, "toxicity": 1.0},
        "decision": {
            "threshold": 0.68,
            "strictness": 0.5,
            "exploration": 0.1,
            "penalty_strength": 1.0,
        },
        "hard_reject": {},
    }
    taste.update(overrides)
    return taste


class ScoreCalculatorTests(unittest.TestCase):
    def test_positive_score_is_weighted_average(self):
        features = {"usefulness": 0.8, "originality": 0.4}
        preferences = {"usefulness": 0.9, "originality": 0.1}
        score = sc.calculate_positive_score(features, preferences)
        expected = (0.8 * 0.9 + 0.4 * 0.1) / (0.9 + 0.1)
        self.assertAlmostEqual(score, expected)

    def test_penalty_score_ignores_missing_features_as_zero(self):
        score = sc.calculate_penalty_score({}, {"promotion": 1.0})
        self.assertEqual(score, 0.0)

    def test_final_score_clamped_to_zero_when_penalty_dominates(self):
        final = sc.calculate_final_score(positive_score=0.2, penalty_score=0.9, penalty_strength=1.0)
        self.assertEqual(final, 0.0)

    def test_final_score_clamped_to_one(self):
        final = sc.calculate_final_score(positive_score=1.0, penalty_score=0.0, penalty_strength=1.0)
        self.assertEqual(final, 1.0)

    def test_effective_threshold_zero_strictness_is_identity(self):
        self.assertAlmostEqual(sc.effective_threshold(0.68, strictness=0.0), 0.68)

    def test_effective_threshold_raises_bar_with_max_strictness(self):
        self.assertAlmostEqual(
            sc.effective_threshold(0.68, strictness=1.0),
            0.68 + sc.MAX_STRICTNESS_THRESHOLD_BONUS,
        )

    def test_effective_threshold_never_exceeds_one(self):
        self.assertEqual(sc.effective_threshold(0.95, strictness=1.0), 1.0)

    def test_adaptive_threshold_uses_fallback_when_samples_too_few(self):
        scores = [0.5, 0.6, 0.7]  # min_samples(기본 30) 미달
        result = sc.compute_adaptive_threshold(scores, target_like_rate=0.15, strictness=0.0, fallback=0.31)
        self.assertEqual(result, 0.31)

    def test_adaptive_threshold_picks_top_percentile(self):
        scores = [i / 100 for i in range(100)]  # 0.00, 0.01, ..., 0.99 (100개)
        # 상위 10% -> 0.90 이상이 되어야 함 (인덱스 90 = 0.90)
        result = sc.compute_adaptive_threshold(scores, target_like_rate=0.10, strictness=0.0, fallback=0.0, min_samples=1)
        self.assertAlmostEqual(result, 0.90)

    def test_adaptive_threshold_strictness_shrinks_target_rate(self):
        scores = [i / 100 for i in range(100)]
        lenient = sc.compute_adaptive_threshold(scores, target_like_rate=0.20, strictness=0.0, fallback=0.0, min_samples=1)
        strict = sc.compute_adaptive_threshold(scores, target_like_rate=0.20, strictness=0.5, fallback=0.0, min_samples=1)
        self.assertGreater(strict, lenient)  # strictness가 높을수록 더 적게(더 높은 점수만) 통과

    def test_adaptive_threshold_zero_target_rate_is_max_score(self):
        scores = [0.1, 0.2, 0.3, 0.4] * 10
        result = sc.compute_adaptive_threshold(scores, target_like_rate=0.0, strictness=0.0, fallback=0.0, min_samples=1)
        self.assertEqual(result, max(scores))

    def test_apply_hard_reject_returns_none_when_not_configured(self):
        self.assertIsNone(sc.apply_hard_reject({"promotion": 0.99}, {}))
        self.assertIsNone(sc.apply_hard_reject({"promotion": 0.99}, None))

    def test_apply_hard_reject_triggers_on_threshold(self):
        reason = sc.apply_hard_reject({"promotion": 0.95}, {"promotion": 0.92})
        self.assertEqual(reason, "promotion")

    def test_apply_hard_reject_passes_below_threshold(self):
        reason = sc.apply_hard_reject({"promotion": 0.5}, {"promotion": 0.92})
        self.assertIsNone(reason)

    def test_make_decision_hard_reject_wins_regardless_of_score(self):
        taste = make_taste()
        score_result = sc.calculate_score({"usefulness": 1.0, "originality": 1.0}, taste)
        decision, reason = sc.make_decision(
            score_result, taste_cfg=taste, hard_reject_reason="toxicity"
        )
        self.assertEqual(decision, "REJECT")
        self.assertIn("toxicity", reason)

    def test_make_decision_likes_above_threshold(self):
        taste = make_taste()
        score_result = sc.calculate_score({"usefulness": 1.0, "originality": 1.0}, taste)
        decision, _ = sc.make_decision(score_result, taste_cfg=taste)
        self.assertEqual(decision, "LIKE")

    def test_make_decision_skips_clearly_below_threshold(self):
        taste = make_taste()
        score_result = sc.calculate_score({"usefulness": 0.1, "originality": 0.1}, taste)
        decision, _ = sc.make_decision(score_result, taste_cfg=taste)
        self.assertEqual(decision, "SKIP")

    def test_exploration_band_can_like_with_low_penalty_and_lucky_roll(self):
        taste = make_taste(decision={
            "threshold": 0.6, "strictness": 0.0, "exploration": 0.2,
            "penalty_strength": 1.0,
        })
        # final_score(=positive_score, penalty=0) lands inside [threshold-exploration, threshold)
        score_result = sc.calculate_score({"usefulness": 0.5, "originality": 0.5}, taste)
        self.assertTrue(0.4 <= score_result["final_score"] < 0.6)

        liked, _ = sc.make_decision(score_result, taste_cfg=taste, rng=FakeRng(0.0))
        skipped, _ = sc.make_decision(score_result, taste_cfg=taste, rng=FakeRng(0.99))
        self.assertEqual(liked, "LIKE")
        self.assertEqual(skipped, "SKIP")

    def test_exploration_never_picks_high_penalty_posts(self):
        taste = make_taste(decision={
            "threshold": 0.6, "strictness": 0.0, "exploration": 0.2,
            "penalty_strength": 0.0,
        })
        features = {"usefulness": 0.5, "originality": 0.5, "promotion": 0.9, "toxicity": 0.9}
        score_result = sc.calculate_score(features, taste)
        # penalty_strength=0 keeps final_score in-band even though penalty_score itself is high
        self.assertTrue(0.4 <= score_result["final_score"] < 0.6)
        self.assertGreaterEqual(score_result["penalty_score"], sc.EXPLORATION_MAX_PENALTY)

        decision, _ = sc.make_decision(score_result, taste_cfg=taste, rng=FakeRng(0.0))
        self.assertEqual(decision, "SKIP")


if __name__ == "__main__":
    unittest.main()
