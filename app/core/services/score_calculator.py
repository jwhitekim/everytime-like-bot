"""취향 파라미터와 LLM이 측정한 게시글 특성을 조합해 최종 점수/결정을 계산한다.

LLM은 게시글의 특성만 측정하고, "좋아요를 누를지"는 이 모듈이 코드로 계산한다.
과도하게 복잡한 수학 모델은 쓰지 않는다 (가중 평균 + 간단한 strictness 커브 + 좁은 exploration 구간).
"""

import random

# threshold 바로 아래 exploration 구간에 들어온 글 중 실제로 뽑힐 확률.
# exploration(설정값)은 구간의 폭만 정하고, 뽑힐 확률은 아래 상수로 고정한다
# (하나의 값에 두 가지 의미를 섞지 않기 위함).
EXPLORATION_PICK_PROBABILITY = 0.3
# penalty_score가 이 값 이상이면 exploration 대상에서 제외한다.
EXPLORATION_MAX_PENALTY = 0.4
# strictness=1.0일 때 threshold를 최대 이만큼 끌어올린다 (덧셈 방식이라 설정값 threshold가
# 실제 커트라인과 크게 동떨어지지 않는다. strictness=0이면 threshold를 그대로 쓴다).
MAX_STRICTNESS_THRESHOLD_BONUS = 0.15
# 적응형 threshold(target_like_rate)를 계산하기에 표본이 너무 적으면 신뢰할 수 없으므로,
# 이만큼 쌓이기 전에는 설정 파일의 고정 threshold를 그대로 쓴다.
MIN_ADAPTIVE_SAMPLES = 30


def _weighted_average(features: dict, weights: dict) -> float:
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return 0.0
    total = sum(features.get(name, 0.0) * weight for name, weight in weights.items())
    return total / total_weight


def calculate_positive_score(features: dict, preferences: dict) -> float:
    return _weighted_average(features, preferences)


def calculate_penalty_score(features: dict, penalties: dict) -> float:
    return _weighted_average(features, penalties)


def calculate_final_score(positive_score: float, penalty_score: float, penalty_strength: float) -> float:
    base_score = positive_score - penalty_score * penalty_strength
    return max(0.0, min(1.0, base_score))


def effective_threshold(threshold: float, strictness: float) -> float:
    """strictness가 높을수록 threshold를 살짝 끌어올려 중간 수준의 글이 넘기 어렵게 한다.

    최상위권 글(예: final_score 0.95)은 어차피 threshold를 넉넉히 넘기므로 거의 영향이 없고,
    threshold 근처의 애매한 글만 strictness에 비례해 더 걸러진다.
    """
    strictness = max(0.0, min(1.0, strictness))
    return min(1.0, threshold + strictness * MAX_STRICTNESS_THRESHOLD_BONUS)


def compute_adaptive_threshold(
    recent_scores: list[float],
    target_like_rate: float,
    strictness: float,
    *,
    fallback: float,
    min_samples: int = MIN_ADAPTIVE_SAMPLES,
) -> float:
    """고정된 점수 대신 "최근 평가한 글 중 상위 N%"를 threshold로 매번 다시 계산한다.

    게시판 콘텐츠의 점수 분포가 시간이 지나며 통째로 오르내려도 목표 비율(target_like_rate)이
    유지되므로, threshold를 사람이 수동으로 재조정할 필요가 줄어든다. strictness는 여기서
    목표 비율 자체를 줄이는 데 쓴다 (effective_threshold처럼 절대 점수를 더하면, 점수 분포의
    스케일이 달라질 때 그 절대값의 의미도 같이 달라져서 여기서는 맞지 않는다).

    표본이 충분하지 않으면(min_samples 미만) 아직 통계적으로 못 미더우므로 fallback을 쓴다.
    """
    if len(recent_scores) < min_samples:
        return fallback

    strictness = max(0.0, min(1.0, strictness))
    effective_rate = max(0.0, min(1.0, target_like_rate * (1 - strictness)))

    sorted_scores = sorted(recent_scores)
    n = len(sorted_scores)
    idx = max(0, min(n - 1, int(n * (1 - effective_rate))))
    return sorted_scores[idx]


def calculate_score(features: dict, taste_cfg: dict) -> dict:
    """positive/penalty/final 점수를 한 번에 계산한다."""
    preferences = taste_cfg.get("preferences", {})
    penalties = taste_cfg.get("penalties", {})
    decision_cfg = taste_cfg.get("decision", {})

    positive_score = calculate_positive_score(features, preferences)
    penalty_score = calculate_penalty_score(features, penalties)
    final_score = calculate_final_score(
        positive_score, penalty_score, decision_cfg.get("penalty_strength", 1.0)
    )

    return {
        "positive_score": positive_score,
        "penalty_score": penalty_score,
        "final_score": final_score,
    }


def apply_hard_reject(features: dict, hard_reject_cfg: dict | None) -> str | None:
    """features 중 하나라도 hard_reject 임계값 이상이면 해당 항목명을 반환한다 (optional 기능)."""
    if not hard_reject_cfg:
        return None
    for name, cutoff in hard_reject_cfg.items():
        if features.get(name, 0.0) >= cutoff:
            return name
    return None


def make_decision(
    score_result: dict,
    taste_cfg: dict,
    *,
    hard_reject_reason: str | None = None,
    rng=random,
) -> tuple[str, str]:
    """("LIKE" | "SKIP" | "REJECT", 이유 문자열)을 반환한다.

    confidence(신뢰도) 게이트는 두지 않는다 — 규칙 기반 특성 계산에는 LLM 같은 "판단
    가능 여부 자기 보고"가 없고, 글자수로 대신하면 캐주얼한 짧은 글(에브리타임 자유게시판의
    대다수)을 근거 없이 걸러내게 된다. 진짜 판단 불가능한 글(빈 글, 2자 미만)은 이미
    hard_filter가 앞단에서 제외한다.
    """
    decision_cfg = taste_cfg.get("decision", {})
    raw_threshold = decision_cfg.get("threshold", 0.68)
    exploration = decision_cfg.get("exploration", 0.0)
    threshold = effective_threshold(raw_threshold, decision_cfg.get("strictness", 0.0))
    final_score = score_result["final_score"]
    penalty_score = score_result["penalty_score"]

    if hard_reject_reason:
        return "REJECT", f"hard_reject:{hard_reject_reason}"

    if final_score >= threshold:
        return "LIKE", f"score {final_score:.3f} >= threshold {threshold:.3f}"

    band_low = threshold - exploration
    if band_low <= final_score < threshold and penalty_score < EXPLORATION_MAX_PENALTY:
        if rng.random() < EXPLORATION_PICK_PROBABILITY:
            return "LIKE", f"exploration pick (score {final_score:.3f} in [{band_low:.3f}, {threshold:.3f}))"

    return "SKIP", f"score {final_score:.3f} < threshold {threshold:.3f}"


def compute_contributions(features: dict, weights: dict) -> dict[str, float]:
    """로그용: 각 항목이 가중 평균에 실제로 기여한 양(feature * weight / sum_weight)."""
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return {}
    return {
        name: features.get(name, 0.0) * weight / total_weight
        for name, weight in weights.items()
    }
