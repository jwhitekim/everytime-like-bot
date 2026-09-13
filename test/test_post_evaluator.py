import unittest

from app.core.services import post_evaluator as pe


def make_taste(**decision_overrides):
    decision = {
        "threshold": 0.6, "strictness": 0.0, "exploration": 0.0,
        "penalty_strength": 1.0,
    }
    decision.update(decision_overrides)
    return {
        "preferences": {"usefulness": 1.0},
        "penalties": {"promotion": 1.0},
        "decision": decision,
        "hard_reject": {},
        "topics": [],
        "hard_filter": {"max_age_days": None},
    }


class FakeFeatureClient:
    def __init__(self, features):
        self.features = features
        self.calls = 0

    def evaluate(self, article):
        self.calls += 1
        return self.features


class InMemoryEvaluatedPosts(unittest.TestCase):
    """PostEvaluator는 app.core.database의 get/save_evaluated_post를 직접 참조하므로
    모듈 함수를 in-memory 구현으로 몽키패치해서 Supabase 없이 검증한다."""

    def setUp(self):
        self.store = {}

        def fake_get(post_id):
            return self.store.get(post_id)

        def fake_save(post_id, **kwargs):
            self.store[post_id] = {"post_id": post_id, **kwargs}

        self._orig_get = pe.get_evaluated_post
        self._orig_save = pe.save_evaluated_post
        pe.get_evaluated_post = fake_get
        pe.save_evaluated_post = fake_save

    def tearDown(self):
        pe.get_evaluated_post = self._orig_get
        pe.save_evaluated_post = self._orig_save


class PostEvaluatorTests(InMemoryEvaluatedPosts):
    def test_like_decision_is_not_marked_liked_until_confirmed(self):
        features = {"usefulness": 0.9, "promotion": 0.0, "confidence": 0.9}
        fc = FakeFeatureClient(features)
        ev = pe.PostEvaluator(fc, make_taste())

        result = ev.should_vote({"id": "p1", "title": "유용한 글", "content": "실제로 도움됨"})

        self.assertTrue(result)
        self.assertFalse(self.store["p1"]["liked"])

    def test_mark_liked_flips_stored_flag_without_recalling_llm(self):
        features = {"usefulness": 0.9, "promotion": 0.0, "confidence": 0.9}
        fc = FakeFeatureClient(features)
        ev = pe.PostEvaluator(fc, make_taste())
        ev.should_vote({"id": "p1", "title": "유용한 글", "content": "실제로 도움됨"})

        ev.mark_liked("p1")

        self.assertTrue(self.store["p1"]["liked"])
        self.assertEqual(fc.calls, 1)

    def test_cached_evaluation_does_not_call_llm_again(self):
        features = {"usefulness": 0.9, "promotion": 0.0, "confidence": 0.9}
        fc = FakeFeatureClient(features)
        taste = make_taste()
        ev = pe.PostEvaluator(fc, taste)
        article = {"id": "p1", "title": "유용한 글", "content": "실제로 도움됨"}

        ev.should_vote(article)
        ev.should_vote(article)

        self.assertEqual(fc.calls, 1)

    def test_config_change_is_reflected_on_cached_post_without_new_llm_call(self):
        """threshold를 낮추면, 이미 SKIP으로 저장된 글도 새 설정으로 재계산되어 LIKE가 될 수 있어야 한다."""
        features = {"usefulness": 0.5, "promotion": 0.0, "confidence": 0.9}
        fc = FakeFeatureClient(features)
        strict_taste = make_taste(threshold=0.9)
        ev = pe.PostEvaluator(fc, strict_taste)
        article = {"id": "p1", "title": "글", "content": "내용"}

        first = ev.should_vote(article)
        self.assertFalse(first)
        self.assertEqual(fc.calls, 1)

        lenient_taste = make_taste(threshold=0.3)
        ev2 = pe.PostEvaluator(fc, lenient_taste)
        second = ev2.should_vote(article)

        self.assertTrue(second)
        self.assertEqual(fc.calls, 1)  # LLM은 다시 호출되지 않았다

    def test_cache_recompute_preserves_previously_confirmed_liked_flag(self):
        features = {"usefulness": 0.9, "promotion": 0.0, "confidence": 0.9}
        fc = FakeFeatureClient(features)
        taste = make_taste()
        ev = pe.PostEvaluator(fc, taste)
        article = {"id": "p1", "title": "글", "content": "내용"}
        ev.should_vote(article)
        ev.mark_liked("p1")

        stricter = make_taste(threshold=0.99)
        ev2 = pe.PostEvaluator(fc, stricter)
        ev2.should_vote(article)  # 이제는 SKIP으로 재계산되지만

        self.assertTrue(self.store["p1"]["liked"])  # 과거에 실제로 눌렀던 기록은 유지된다

    # 건너뛸 키워드 하드 필터는 임시로 꺼둔 상태라 이 테스트도 같이 꺼둔다
    # (post_filter.py 주석 해제 시 같이 복원할 것).
    # def test_hard_filter_rejects_without_calling_llm(self):
    #     fc = FakeFeatureClient({"usefulness": 1.0, "promotion": 0.0, "confidence": 0.9})
    #     ev = pe.PostEvaluator(fc, make_taste(), skip_keywords=["광고"])
    #
    #     result = ev.should_vote({"id": "p2", "title": "광고 홍보", "content": "본문"})
    #
    #     self.assertFalse(result)
    #     self.assertEqual(fc.calls, 0)

    def test_llm_failure_returns_none_and_does_not_cache(self):
        class FailingClient:
            def evaluate(self, article):
                return None

        ev = pe.PostEvaluator(FailingClient(), make_taste())
        result = ev.should_vote({"id": "p3", "title": "글", "content": "내용"})

        self.assertIsNone(result)
        self.assertNotIn("p3", self.store)


if __name__ == "__main__":
    unittest.main()
