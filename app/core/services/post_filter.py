"""LLM 호출 전에 명확히 제외할 수 있는 게시글을 걸러낸다 (API 비용 절감용)."""

import re
from datetime import datetime

from ..article_trends import parse_article_datetime
from ...config import KST

MIN_CONTENT_LENGTH = 2

# 명백한 광고 패턴만 좁게 잡는다 (오탐 위험이 낮은 것만) — 애매한 홍보성 판단은
# LLM의 promotion 특성 + hard_reject에 맡긴다.
_PHONE_NUMBER_RE = re.compile(r"01[016789]-?\d{3,4}-?\d{4}")


def hard_filter(
    article: dict,
    *,
    skip_keywords: list[str] | None = None,
    max_age_days: float | None = None,
) -> str | None:
    """통과하면 None, 제외해야 하면 사유 문자열을 반환한다.

    아래 세 가지는 임시로 꺼둔 상태다 (필요해지면 주석만 풀면 됨):
    - 빈 글 체크: 실제로 극히 드물게 발생해 별도 처리 불필요 판단
    - 건너뛸 키워드(skip_keywords): 하드 필터 대신 다시 켤 경우를 대비해 파라미터/명령어는 유지
    - 광고 전화번호 패턴: 하드 컷 없이 score_calculator의 promotion 점수/hard_reject에 맡기기로 함
    """
    # title = (article.get("title") or "").strip()
    # content = (article.get("content") or "").strip()
    #
    # if not title and not content:
    #     return "empty_content"
    #
    # if len(title) + len(content) < MIN_CONTENT_LENGTH:
    #     return "empty_content"

    # if skip_keywords:
    #     haystack = f"{title}\n{content}".lower()
    #     for kw in skip_keywords:
    #         if kw.lower() in haystack:
    #             return f"blacklist_keyword:{kw}"

    # if _PHONE_NUMBER_RE.search(content) or _PHONE_NUMBER_RE.search(title):
    #     return "ad_pattern:phone_number"

    if max_age_days is not None:
        created_at = parse_article_datetime(article.get("created_at", ""))
        if created_at is not None:
            age_days = (datetime.now(KST) - created_at).total_seconds() / 86400
            if age_days > max_age_days:
                return f"too_old:{age_days:.1f}d"

    return None
