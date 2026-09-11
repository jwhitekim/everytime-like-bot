"""게시글의 특성을 LLM 없이 규칙/통계로 계산한다.

의미 이해가 필요한 특성(재미, 독창성, 유용성 등)은 근거 있게 계산할 수 없어 제외했다.
남긴 7개는 전부 정규식/키워드 매칭/집합 연산만으로 구할 수 있는 값이다.
GeminiFeatureEvaluator와 동일한 인터페이스(evaluate(article) -> dict | None)를 구현해
post_evaluator.PostEvaluator를 그대로 재사용한다.
"""

import re

_WORD_RE = re.compile(r"[가-힣]+|[A-Za-z]+|[0-9]+")
_URL_RE = re.compile(r"https?://|www\.")
_PRICE_RE = re.compile(r"\d[\d,]*\s*(원|만원)")
_PHONE_RE = re.compile(r"01[016789]-?\d{3,4}-?\d{4}")

EFFORT_TARGET_LENGTH = 500
PROMOTION_HIT_WEIGHT = 0.35
TOXICITY_HIT_WEIGHT = 0.5
CONTROVERSY_HIT_WEIGHT = 0.4
CLICKBAIT_PUNCT_WEIGHT = 0.15
RECENT_FINGERPRINT_LIMIT = 50


def _tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _keyword_hit_count(text: str, keywords: list[str]) -> int:
    lowered = text.lower()
    return sum(1 for kw in keywords if kw and kw.lower() in lowered)


def _topic_relevance(text: str, topics: list[str]) -> float:
    if not topics:
        return 0.5
    lowered = text.lower()
    matched = sum(1 for topic in topics if topic and topic.lower() in lowered)
    return _clamp(matched / len(topics))


def _effort(content: str) -> float:
    length_score = _clamp(len(content) / EFFORT_TARGET_LENGTH)
    structure_score = 1.0 if content.count("\n") >= 2 else 0.0
    return _clamp(length_score * 0.8 + structure_score * 0.2)


def _information_density(tokens: list[str]) -> float:
    if not tokens:
        return 0.0
    unique_ratio = len(set(tokens)) / len(tokens)
    digit_bonus = 0.1 if any(tok.isdigit() for tok in tokens) else 0.0
    return _clamp(unique_ratio + digit_bonus)


def _promotion(text: str, promotion_terms: list[str]) -> float:
    hits = _keyword_hit_count(text, promotion_terms)
    if _PHONE_RE.search(text):
        hits += 1
    if _URL_RE.search(text):
        hits += 1
    if _PRICE_RE.search(text):
        hits += 1
    return _clamp(hits * PROMOTION_HIT_WEIGHT)


def _toxicity(text: str, toxicity_words: list[str]) -> float:
    hits = _keyword_hit_count(text, toxicity_words)
    return _clamp(hits * TOXICITY_HIT_WEIGHT)


def _clickbait(title: str, clickbait_phrases: list[str]) -> float:
    hits = _keyword_hit_count(title, clickbait_phrases)
    punct_score = _clamp((title.count("?") + title.count("!")) * CLICKBAIT_PUNCT_WEIGHT)
    return _clamp(hits * 0.4 + punct_score)


def _controversy(text: str, controversy_words: list[str]) -> float:
    hits = _keyword_hit_count(text, controversy_words)
    return _clamp(hits * CONTROVERSY_HIT_WEIGHT)


def _repetitiveness(tokens: list[str], recent_token_lists: list[list[str]]) -> float:
    if not tokens or not recent_token_lists:
        return 0.0
    current = set(tokens)
    best = 0.0
    for other_tokens in recent_token_lists:
        other = set(other_tokens)
        if not other:
            continue
        union = current | other
        if not union:
            continue
        similarity = len(current & other) / len(union)
        best = max(best, similarity)
    return _clamp(best)


class FeatureScorer:
    """게시글 특성 7개를 규칙 기반으로 계산한다 (LLM 호출 없음).

    repetitiveness 계산을 위해 최근 게시글 기록을 읽고(recent_fingerprints_provider),
    평가 후 이번 글을 기록(record_fingerprint)한다. 둘 다 주입하지 않으면 repetitiveness는
    항상 0.0으로 취급한다 (비교 대상이 없다는 뜻과 같다).
    """

    def __init__(
        self,
        *,
        topics: list[str] | None = None,
        keywords: dict | None = None,
        recent_fingerprints_provider=None,
        record_fingerprint=None,
    ):
        self.topics = topics or []
        keywords = keywords or {}
        self.toxicity_words = keywords.get("toxicity", [])
        self.controversy_words = keywords.get("controversy", [])
        self.clickbait_phrases = keywords.get("clickbait_phrases", [])
        self.promotion_terms = keywords.get("promotion_terms", [])
        self._get_recent = recent_fingerprints_provider or (lambda: [])
        self._record = record_fingerprint or (lambda tokens: None)

    def evaluate(self, article: dict) -> dict | None:
        title = str(article.get("title") or "")
        content = str(article.get("content") or "")
        text = f"{title}\n{content}"
        tokens = _tokenize(text)

        features = {
            "topic_relevance": _topic_relevance(text, self.topics),
            "effort": _effort(content),
            "information_density": _information_density(tokens),
            "promotion": _promotion(text, self.promotion_terms),
            "toxicity": _toxicity(text, self.toxicity_words),
            "clickbait": _clickbait(title, self.clickbait_phrases),
            "controversy": _controversy(text, self.controversy_words),
            "repetitiveness": _repetitiveness(tokens, self._get_recent()),
        }

        self._record(tokens)
        return features
