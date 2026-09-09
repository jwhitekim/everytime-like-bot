"""게시글 선별 파이프라인의 오케스트레이터.

crawl_post() -> hard_filter() -> evaluate_features() -> calculate_score()
    -> apply_hard_reject() -> make_decision() -> (dry_run OR like_post()) -> save_result()

vote_runner.run_vote()가 기대하는 interest_decider 인터페이스(should_vote(article) -> bool | None)
를 그대로 구현해서 기존 좋아요 로직 앞에 최소한의 변경으로 끼워 넣는다.
좋아요 실행 자체(dry_run 여부에 따른 push_vote 호출)는 run_vote가 맡는다 — 이 클래스는
"좋아요 후보인가"만 판단하고 기록한다.
"""

import logging
from datetime import datetime, timezone

from . import score_calculator as sc
from .post_filter import hard_filter
from ..database import get_evaluated_post, save_evaluated_post

TOP_N_LOG = 3


class PostEvaluator:
    def __init__(self, feature_client, taste_cfg: dict, *, skip_keywords: list[str] | None = None, dry_run: bool = False):
        self.feature_client = feature_client
        self.taste_cfg = taste_cfg
        self.skip_keywords = skip_keywords or []
        self.dry_run = dry_run

    def should_vote(self, article: dict) -> bool | None:
        post_id = article.get("id")

        cached = get_evaluated_post(post_id) if post_id else None
        if cached is not None:
            return self._decide_from_cache(post_id, cached)

        reason = hard_filter(
            article,
            skip_keywords=self.skip_keywords,
            max_age_days=self.taste_cfg.get("hard_filter", {}).get("max_age_days"),
        )
        if reason:
            logging.info("[%s] hard_filter 제외: %s", post_id, reason)
            self._save(post_id, {}, 0.0, "REJECT", liked=False)
            return False

        features = self.feature_client.evaluate(article)
        if features is None:
            # 특성 계산 실패(예상 밖 예외). 저장하지 않아야 다음 실행에서 다시 시도한다.
            return None

        decision, score_result = self._score_and_log(post_id, features)

        liked = False  # 실제 push_vote 성공 후 run_vote가 mark_liked()로 갱신한다.
        self._save(post_id, features, score_result["final_score"], decision, liked=liked)

        if self.dry_run and decision == "LIKE":
            logging.info("[%s] [DRY_RUN] 판단은 LIKE지만 실제 공감은 누르지 않습니다.", post_id)

        return decision == "LIKE"

    def _decide_from_cache(self, post_id, cached: dict) -> bool | None:
        """캐시된 feature_scores로 현재 taste_cfg를 재적용한다 (특성 재계산은 하지 않음).

        hard_filter로 걸러졌던 글(feature_scores가 비어 있음)은 재평가할 특성이 없으므로
        저장된 결정을 그대로 재사용한다.
        """
        features = cached.get("feature_scores") or {}
        if not features:
            logging.info("[%s] 이미 평가된 게시글(hard_filter), 저장된 결정 재사용: %s", post_id, cached.get("decision"))
            return cached.get("decision") == "LIKE"

        decision, score_result = self._score_and_log(post_id, features, note="cached features, 현재 설정으로 재계산")
        liked = cached.get("liked", False)
        self._save(post_id, features, score_result["final_score"], decision, liked=liked)
        return decision == "LIKE"

    def _score_and_log(self, post_id, features: dict, *, note: str | None = None) -> tuple[str, dict]:
        confidence = features.get("confidence", 0.0)
        score_result = sc.calculate_score(features, self.taste_cfg)
        hard_reject_reason = sc.apply_hard_reject(features, self.taste_cfg.get("hard_reject"))
        decision, reason = sc.make_decision(
            score_result, confidence, self.taste_cfg, hard_reject_reason=hard_reject_reason
        )
        self._log(post_id, features, score_result, confidence, decision, reason, note=note)
        return decision, score_result

    def mark_liked(self, post_id) -> None:
        """push_vote()가 실제로 성공(voted/already)한 뒤에만 호출해서 liked 플래그를 확정한다."""
        cached = get_evaluated_post(post_id)
        if cached is None:
            logging.warning("[%s] mark_liked 호출됐지만 저장된 평가 기록이 없습니다.", post_id)
            return
        save_evaluated_post(
            post_id,
            evaluated_at=cached.get("evaluated_at", datetime.now(timezone.utc).isoformat()),
            feature_scores=cached.get("feature_scores", {}),
            final_score=cached.get("final_score", 0.0),
            decision=cached.get("decision", "LIKE"),
            liked=True,
        )

    def _save(self, post_id, features, final_score, decision, *, liked) -> None:
        if not post_id:
            return
        save_evaluated_post(
            post_id,
            evaluated_at=datetime.now(timezone.utc).isoformat(),
            feature_scores=features,
            final_score=final_score,
            decision=decision,
            liked=liked,
        )

    def _log(self, post_id, features, score_result, confidence, decision, reason, *, note: str | None = None) -> None:
        preferences = self.taste_cfg.get("preferences", {})
        penalties = self.taste_cfg.get("penalties", {})
        positive_contrib = sc.compute_contributions(features, preferences)
        penalty_contrib = sc.compute_contributions(features, penalties)

        top_positive = sorted(positive_contrib.items(), key=lambda kv: kv[1], reverse=True)[:TOP_N_LOG]
        top_penalty = sorted(penalty_contrib.items(), key=lambda kv: kv[1], reverse=True)[:TOP_N_LOG]

        lines = [
            f"POST {post_id}" + (f" ({note})" if note else ""),
            f"positive: {score_result['positive_score']:.3f}",
            f"penalty: {score_result['penalty_score']:.3f}",
            f"final: {score_result['final_score']:.3f}",
            f"confidence: {confidence:.2f}",
            "TOP POSITIVE",
        ]
        lines += [f"{name} +{value:.3f}" for name, value in top_positive]
        lines.append("TOP PENALTY")
        lines += [f"{name} -{value:.3f}" for name, value in top_penalty]
        lines.append(f"DECISION: {decision} ({reason})")
        logging.info("\n".join(lines))
