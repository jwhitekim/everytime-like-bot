import unittest
from datetime import datetime, timedelta, timezone

from app.core.services.post_filter import hard_filter


class HardFilterTests(unittest.TestCase):
    def test_passes_normal_post(self):
        article = {"title": "오늘 학식 어때요", "content": "3층 식당 괜찮던데"}
        self.assertIsNone(hard_filter(article))

    # 빈 글 체크, 건너뛸 키워드, 광고 전화번호 패턴은 hard_filter에서 임시로 꺼둔 상태라
    # 아래 테스트들도 같이 꺼둔다 (post_filter.py 주석 해제 시 같이 복원할 것).
    #
    # def test_rejects_empty_post(self):
    #     article = {"title": "", "content": ""}
    #     self.assertEqual(hard_filter(article), "empty_content")
    #
    # def test_rejects_blacklist_keyword_in_title(self):
    #     article = {"title": "과외 구합니다", "content": "본문"}
    #     reason = hard_filter(article, skip_keywords=["과외"])
    #     self.assertEqual(reason, "blacklist_keyword:과외")
    #
    # def test_blacklist_match_is_case_insensitive(self):
    #     article = {"title": "PROMO event", "content": "본문"}
    #     reason = hard_filter(article, skip_keywords=["promo"])
    #     self.assertIsNotNone(reason)
    #
    # def test_rejects_blacklist_keyword_in_content_too(self):
    #     article = {"title": "제목", "content": "과외 구합니다 연락주세요"}
    #     reason = hard_filter(article, skip_keywords=["과외"])
    #     self.assertEqual(reason, "blacklist_keyword:과외")
    #
    # def test_rejects_phone_number_ad_pattern(self):
    #     article = {"title": "급구", "content": "010-1234-5678로 연락주세요"}
    #     reason = hard_filter(article)
    #     self.assertEqual(reason, "ad_pattern:phone_number")

    def test_no_keywords_configured_does_not_filter(self):
        article = {"title": "아무 제목", "content": "본문"}
        self.assertIsNone(hard_filter(article, skip_keywords=[]))

    def test_does_not_flag_normal_post_as_ad(self):
        article = {"title": "오늘 날씨 어때요", "content": "비 온다는데 우산 챙기세요"}
        self.assertIsNone(hard_filter(article))

    def test_rejects_post_older_than_max_age_days(self):
        old_time = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        article = {"title": "제목", "content": "본문", "created_at": old_time}
        reason = hard_filter(article, max_age_days=7)
        self.assertTrue(reason.startswith("too_old:"))

    def test_keeps_post_within_max_age_days(self):
        recent_time = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        article = {"title": "제목", "content": "본문", "created_at": recent_time}
        self.assertIsNone(hard_filter(article, max_age_days=7))

    def test_max_age_days_none_disables_age_check(self):
        old_time = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        article = {"title": "제목", "content": "본문", "created_at": old_time}
        self.assertIsNone(hard_filter(article, max_age_days=None))


if __name__ == "__main__":
    unittest.main()
