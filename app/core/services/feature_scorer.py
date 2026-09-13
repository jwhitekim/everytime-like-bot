"""게시글의 특성을 LLM 없이 규칙/통계로 계산한다.

의미 이해가 필요한 특성(재미, 독창성, 유용성 등)은 근거 있게 계산할 수 없어 제외했다.
욕설/광고/낚시 판단도 특정 단어 목록을 심어두는 대신, 문자 구성비·기호 반복 같은
순수 통계 신호만 사용한다 — 단어 목록은 그 자체로 저장소에 노출되는 문제도 있고,
목록에 없는 표현은 못 잡는다는 근본적인 한계도 있다.
GeminiFeatureEvaluator와 동일한 인터페이스(evaluate(article) -> dict | None)를 구현해
post_evaluator.PostEvaluator를 그대로 재사용한다.
"""

import re

_WORD_RE = re.compile(r"[가-힣]+|[A-Za-z]+|[0-9]+")
_URL_RE = re.compile(r"https?://|www\.")
_PRICE_RE = re.compile(r"\d[\d,]*\s*(원|만원)")
_PHONE_RE = re.compile(r"01[016789]-?\d{3,4}-?\d{4}")
# 자음/모음만 단독으로 입력된 문자 (예: "ㅅㅂ", "ㅗㅜㅑ", "ㅋㅋㅋ", "ㅠㅠ").
# 완성된 단어가 아니라 감정 표출·욕설 회피 표기에서 통계적으로 흔한 패턴이라,
# 특정 욕설 단어를 나열하지 않고도 거친 표현의 비율을 잡는 데 쓴다.
_ISOLATED_JAMO_RE = re.compile(r"[ㄱ-ㅎㅏ-ㅣ]")
_REPEATED_PUNCT_RE = re.compile(r"([!?])\1{1,}")

EFFORT_TARGET_LENGTH = 500
PROMOTION_HIT_WEIGHT = 0.35
JAMO_RATIO_SCALE = 6.0
CLICKBAIT_PUNCT_WEIGHT = 0.15
CLICKBAIT_REPEAT_WEIGHT = 0.25
RECENT_FINGERPRINT_LIMIT = 50


def _tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


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


def _promotion(text: str) -> float:
    """구조적 광고 패턴(전화번호/링크/가격 표기)만 본다 — "홍보", "할인" 같은 홍보성
    단어 목록은 쓰지 않는다."""
    hits = 0
    if _PHONE_RE.search(text):
        hits += 1
    if _URL_RE.search(text):
        hits += 1
    if _PRICE_RE.search(text):
        hits += 1
    return _clamp(hits * PROMOTION_HIT_WEIGHT)


def _toxicity(text: str) -> float:
    if not text:
        return 0.0
    isolated = len(_ISOLATED_JAMO_RE.findall(text))
    return _clamp((isolated / len(text)) * JAMO_RATIO_SCALE)


def _clickbait(title: str) -> float:
    punct_score = _clamp((title.count("?") + title.count("!")) * CLICKBAIT_PUNCT_WEIGHT)
    repeat_hits = len(_REPEATED_PUNCT_RE.findall(title))
    repeat_score = _clamp(repeat_hits * CLICKBAIT_REPEAT_WEIGHT)
    return _clamp(punct_score + repeat_score)


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
    """게시글 특성 7개를 규칙/통계 기반으로 계산한다 (LLM 호출 없음, 단어 목록 없음).

    repetitiveness 계산을 위해 최근 게시글 기록을 읽고(recent_fingerprints_provider),
    평가 후 이번 글을 기록(record_fingerprint)한다. 둘 다 주입하지 않으면 repetitiveness는
    항상 0.0으로 취급한다 (비교 대상이 없다는 뜻과 같다).
    """

    def __init__(
        self,
        *,
        topics: list[str] | None = None,
        recent_fingerprints_provider=None,
        record_fingerprint=None,
    ):
        self.topics = topics or []
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
            "promotion": _promotion(text),
            "toxicity": _toxicity(text),
            "clickbait": _clickbait(title),
            "repetitiveness": _repetitiveness(tokens, self._get_recent()),
        }

        self._record(tokens)
        return features
