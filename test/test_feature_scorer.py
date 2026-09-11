import unittest

from app.core.services.feature_scorer import FeatureScorer


def article(title="", content="", **overrides):
    return {"id": "1", "title": title, "content": content, **overrides}


class FeatureScorerTests(unittest.TestCase):
    def test_topic_relevance_matches_configured_topics(self):
        scorer = FeatureScorer(topics=["백엔드", "헬스"])
        result = scorer.evaluate(article(title="백엔드 개발자 후기", content="이직 준비 중"))
        self.assertGreater(result["topic_relevance"], 0.0)

    def test_topic_relevance_neutral_when_no_topics_configured(self):
        scorer = FeatureScorer(topics=[])
        result = scorer.evaluate(article(title="아무 글", content="내용"))
        self.assertEqual(result["topic_relevance"], 0.5)

    def test_topic_relevance_zero_when_no_match(self):
        scorer = FeatureScorer(topics=["백엔드"])
        result = scorer.evaluate(article(title="오늘 점심 뭐 먹지", content="추천 좀"))
        self.assertEqual(result["topic_relevance"], 0.0)

    def test_effort_increases_with_content_length_and_structure(self):
        scorer = FeatureScorer()
        short = scorer.evaluate(article(title="글", content="짧음"))
        long_structured = scorer.evaluate(article(title="글", content="문단1\n\n문단2\n\n" + "내용 " * 200))
        self.assertGreater(long_structured["effort"], short["effort"])

    def test_information_density_high_for_varied_vocabulary(self):
        scorer = FeatureScorer()
        varied = scorer.evaluate(article(title="글", content="사과 바나나 딸기 포도 수박 참외 자두"))
        repetitive = scorer.evaluate(article(title="글", content="사과 사과 사과 사과 사과 사과 사과"))
        self.assertGreater(varied["information_density"], repetitive["information_density"])

    def test_promotion_detects_phone_number(self):
        scorer = FeatureScorer()
        result = scorer.evaluate(article(title="문의", content="010-1234-5678로 연락주세요"))
        self.assertGreater(result["promotion"], 0.0)

    def test_promotion_detects_keyword_terms(self):
        scorer = FeatureScorer(keywords={"promotion_terms": ["카톡", "할인"]})
        result = scorer.evaluate(article(title="할인 이벤트", content="카톡으로 문의주세요"))
        self.assertGreater(result["promotion"], 0.0)

    def test_promotion_zero_for_clean_post(self):
        scorer = FeatureScorer()
        result = scorer.evaluate(article(title="오늘 날씨 좋다", content="산책하기 좋은 날"))
        self.assertEqual(result["promotion"], 0.0)

    def test_toxicity_detects_blocklisted_word(self):
        scorer = FeatureScorer(keywords={"toxicity": ["병신"]})
        result = scorer.evaluate(article(title="화남", content="진짜 병신 같다"))
        self.assertGreater(result["toxicity"], 0.0)

    def test_clickbait_detects_phrase_and_punctuation(self):
        scorer = FeatureScorer(keywords={"clickbait_phrases": ["충격"]})
        result = scorer.evaluate(article(title="충격!! 실화???", content="내용"))
        self.assertGreater(result["clickbait"], 0.0)

    def test_controversy_detects_blocklisted_topic(self):
        scorer = FeatureScorer(keywords={"controversy": ["정치"]})
        result = scorer.evaluate(article(title="정치 얘기 좀", content="요즘 정치 실망스럽다"))
        self.assertGreater(result["controversy"], 0.0)

    def test_repetitiveness_zero_without_history(self):
        scorer = FeatureScorer()
        result = scorer.evaluate(article(title="새 글", content="새 내용"))
        self.assertEqual(result["repetitiveness"], 0.0)

    def test_repetitiveness_high_for_near_duplicate(self):
        recent = [["같이", "밥", "먹을", "사람", "구합니다"]]
        scorer = FeatureScorer(recent_fingerprints_provider=lambda: recent)
        result = scorer.evaluate(article(title="같이 밥 먹을 사람", content="구합니다"))
        self.assertGreater(result["repetitiveness"], 0.5)

    def test_records_fingerprint_after_evaluation(self):
        recorded = []
        scorer = FeatureScorer(record_fingerprint=recorded.append)
        scorer.evaluate(article(title="글", content="내용"))
        self.assertEqual(len(recorded), 1)

    def test_all_feature_keys_present(self):
        scorer = FeatureScorer()
        result = scorer.evaluate(article(title="글", content="내용"))
        expected_keys = {
            "topic_relevance", "effort", "information_density",
            "promotion", "toxicity", "clickbait", "controversy", "repetitiveness",
        }
        self.assertEqual(set(result.keys()), expected_keys)


if __name__ == "__main__":
    unittest.main()
