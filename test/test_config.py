import json
import tempfile
import unittest
from pathlib import Path

from app.config import load_taste_config


class TasteConfigValidationTests(unittest.TestCase):
    def _load(self, content: dict):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(content, f)
            path = Path(f.name)
        try:
            return load_taste_config(path)
        finally:
            path.unlink()

    def test_missing_file_falls_back_to_defaults(self):
        cfg = load_taste_config(Path("/nonexistent/taste.json"))
        self.assertIn("topic_relevance", cfg["preferences"])
        self.assertEqual(cfg["decision"]["threshold"], 0.31)

    def test_out_of_range_preference_is_clamped(self):
        cfg = self._load({"preferences": {"topic_relevance": 2.0}})
        self.assertEqual(cfg["preferences"]["topic_relevance"], 1.0)

    def test_negative_penalty_is_clamped_to_zero(self):
        cfg = self._load({"penalties": {"promotion": -0.5}})
        self.assertEqual(cfg["penalties"]["promotion"], 0.0)

    def test_non_numeric_value_falls_back_to_default(self):
        cfg = self._load({"preferences": {"topic_relevance": "very high"}})
        self.assertEqual(cfg["preferences"]["topic_relevance"], 0.9)  # DEFAULT_TASTE_CONFIG 값

    def test_unknown_preference_key_is_dropped(self):
        cfg = self._load({"preferences": {"made_up_feature": 0.9}})
        self.assertNotIn("made_up_feature", cfg["preferences"])

    def test_decision_fields_clamped_to_unit_range(self):
        cfg = self._load({"decision": {"threshold": 1.5, "strictness": -1}})
        self.assertEqual(cfg["decision"]["threshold"], 1.0)
        self.assertEqual(cfg["decision"]["strictness"], 0.0)

    def test_penalty_strength_allows_above_one_but_is_capped(self):
        cfg = self._load({"decision": {"penalty_strength": 99}})
        self.assertLessEqual(cfg["decision"]["penalty_strength"], 2.0)

    def test_hard_reject_unknown_key_is_dropped(self):
        cfg = self._load({"hard_reject": {"not_a_feature": 0.9, "toxicity": 0.9}})
        self.assertNotIn("not_a_feature", cfg["hard_reject"])
        self.assertEqual(cfg["hard_reject"]["toxicity"], 0.9)

    def test_topics_list_is_preserved(self):
        cfg = self._load({"topics": ["백엔드 개발", "자취"]})
        self.assertEqual(cfg["topics"], ["백엔드 개발", "자취"])

    def test_topics_non_list_falls_back_to_empty(self):
        cfg = self._load({"topics": "백엔드 개발"})
        self.assertEqual(cfg["topics"], [])

    def test_max_age_days_parsed_as_float(self):
        cfg = self._load({"hard_filter": {"max_age_days": "30"}})
        self.assertEqual(cfg["hard_filter"]["max_age_days"], 30.0)

    def test_max_age_days_defaults_to_none(self):
        cfg = self._load({})
        self.assertIsNone(cfg["hard_filter"]["max_age_days"])

    def test_target_like_rate_defaults_to_none(self):
        cfg = load_taste_config(Path("/nonexistent/taste.json"))
        self.assertIsNone(cfg["decision"]["target_like_rate"])

    def test_target_like_rate_is_preserved_when_valid(self):
        cfg = self._load({"decision": {"target_like_rate": 0.15}})
        self.assertEqual(cfg["decision"]["target_like_rate"], 0.15)

    def test_target_like_rate_clamped_to_unit_range(self):
        cfg = self._load({"decision": {"target_like_rate": 1.5}})
        self.assertEqual(cfg["decision"]["target_like_rate"], 1.0)

    def test_target_like_rate_invalid_falls_back_to_none(self):
        cfg = self._load({"decision": {"target_like_rate": "high"}})
        self.assertIsNone(cfg["decision"]["target_like_rate"])


if __name__ == "__main__":
    unittest.main()
