import logging
import random
import time

from .clients.everytime import EverytimeClient
from .services import score_calculator
from .services.feature_scorer import FeatureScorer
from .services.post_evaluator import PostEvaluator
from .database import db, get_recent_post_fingerprints, add_post_fingerprint
from ..config import (
    PAGE_NUM,
    BOARD_PAGE_SIZE,
    CHECKPOINT_SEARCH_MAX_PAGES,
    get_dry_run,
    load_taste_config,
)


def run_vote(
    *,
    client,
    storage,
    cfg,
    target_board: str,
    progress_callback=None,
    skip_keywords: list[str] | None = None,
    interest_decider=None,
    dry_run: bool = False,
) -> dict:
    processed = 0
    skipped = 0
    already = 0
    failed = 0
    final_page = 0
    scanned = 0
    scan_limit_reached = False
    checkpoint_found = False

    if not client.check_session(target_board):
        logging.error("세션이 유효하지 않아 종료합니다.")
        return {
            "processed": 0, "skipped": 0, "already": 0, "failed": 0,
            "candidates": 0, "scanned": 0, "final_page": 0,
            "checkpoint_found": False, "scan_limit_reached": False,
            "success": False,
        }

    try:
        checkpoint_id = storage.load("last_article_id", raise_on_error=True)
    except Exception as e:
        # 저장소 조회 실패를 "체크포인트 없음(최초 실행)"으로 오인하면 안 된다 - 그러면 이미 있는
        # 체크포인트를 무시하고 최신 N페이지만 다시 스캔하게 되어, 그 사이 밀려난 글들을 놓친다.
        logging.error(f"체크포인트 조회 실패로 이번 실행을 중단합니다: {e}")
        return {
            "processed": 0, "skipped": 0, "already": 0, "failed": 0,
            "candidates": 0, "scanned": 0, "final_page": 0,
            "checkpoint_found": False, "scan_limit_reached": False,
            "success": False,
        }
    is_initial = checkpoint_id is None
    articles_to_vote = []
    first_article_id = None
    max_pages = cfg["bot"]["max_pages"]

    if is_initial:
        for i in range(max_pages):
            final_page = i + 1
            logging.info(f"[초기] {final_page}페이지 스캔 중...")
            page_articles = client.get_article_ids(target_board, start_num=i * 20)
            if not page_articles:
                break
            scanned += len(page_articles)
            if first_article_id is None:
                first_article_id = page_articles[0]["id"]
            articles_to_vote.extend(page_articles)
            time.sleep(cfg["timing"]["page_delay"])
        scan_limit_reached = final_page >= max_pages
    else:
        # 탐색(체크포인트 찾기)과 수집(공감 후보 모으기)을 한 번의 페이지 조회로 같이
        # 한다 — 예전에는 먼저 find_article()로 최대 200페이지를 탐색만 하고, 못
        # 찾으면 처음부터 다시 같은 페이지들을 조회해 수집했다 (같은 페이지를 두 번
        # 요청하는 낭비). 체크포인트를 찾을지 여부는 끝까지 가봐야 알 수 있으므로,
        # 일단 전부 수집해두고 결과에 따라 사후에 자른다.
        offset = 0
        checkpoint_found = False
        for i in range(CHECKPOINT_SEARCH_MAX_PAGES):
            final_page = i + 1
            logging.info(f"[재개] {final_page}페이지 탐색 중 (체크포인트: {checkpoint_id})...")
            page_articles = client.get_article_ids(target_board, start_num=offset)
            if not page_articles:
                break
            scanned += len(page_articles)
            if first_article_id is None:
                first_article_id = page_articles[0]["id"]
            for article in page_articles:
                if article["id"] == checkpoint_id:
                    checkpoint_found = True
                    break
                articles_to_vote.append(article)
            if checkpoint_found:
                break
            offset += 20
            time.sleep(cfg["timing"]["page_delay"])

        if checkpoint_found:
            scan_limit_reached = False
        else:
            # 체크포인트를 못 찾으면(게시글 삭제 추정) 탐색은 200페이지까지 했더라도,
            # 실제 처리 대상은 초기 스캔과 같은 상한(max_pages)으로 좁힌다 — 그대로
            # 두면 체크포인트를 잃을 때마다 대량 처리가 발생해 max_pages를 둔 안전장치
            # 취지가 무색해진다.
            logging.warning("체크포인트 게시글을 찾지 못했습니다 (게시글 삭제 추정). 최근 %d페이지만 처리합니다.", max_pages)
            scan_limit_reached = final_page >= max_pages
            if final_page > max_pages:
                cutoff = max_pages * PAGE_NUM
                articles_to_vote = articles_to_vote[:cutoff]
                scanned = min(scanned, cutoff)
                final_page = max_pages

    for item in articles_to_vote:
        # 건너뛸 키워드(skip_keywords) 필터는 임시로 꺼둔 상태 — 필요해지면 주석만 풀면 됨.
        # title = (item.get("title") or "").lower()
        # if skip_keywords and any(kw.lower() in title for kw in skip_keywords):
        #     logging.info(f"[{item['id']}] 건너뜀 (키워드 일치): {item.get('title')}")
        #     skipped += 1
        #     continue

        if item.get("posvote", 0) >= 1:
            logging.info(f"[{item['id']}] 건너뜀 (공감 {item['posvote']}개): {item.get('title')}")
            skipped += 1
            continue

        if interest_decider is not None:
            interest_decision = interest_decider.should_vote(item)
            if interest_decision is None:
                # 일시적인 LLM 장애라면 다음 실행에서 다시 판단할 수 있도록
                # 실패로 집계하고 체크포인트를 갱신하지 않는다.
                failed += 1
                continue
            if not interest_decision:
                logging.info(f"[{item['id']}] 건너뜀 (취향 파라미터 판단): {item.get('title')}")
                skipped += 1
                continue

        if dry_run:
            logging.info(f"[{item['id']}] [DRY_RUN] 실제 공감 요청은 보내지 않습니다: {item.get('title')}")
            processed += 1
            if progress_callback:
                progress_callback(processed, final_page)
            continue

        vote_result = client.push_vote(article_id=item["id"])
        if vote_result == "voted":
            processed += 1
        elif vote_result == "already":
            already += 1
        else:
            failed += 1
        if vote_result in ("voted", "already") and interest_decider is not None:
            mark_liked = getattr(interest_decider, "mark_liked", None)
            if mark_liked:
                mark_liked(item["id"])
        if progress_callback:
            progress_callback(processed, final_page)
        time.sleep(random.uniform(
            cfg["timing"]["sleep_min"],
            cfg["timing"]["sleep_max"],
        ))

    if dry_run:
        logging.info("[DRY_RUN] 체크포인트를 갱신하지 않습니다 (실제 실행 시 이 글들을 다시 검토합니다).")
    elif not first_article_id:
        pass  # 스캔된 글이 없음 (게시판이 비었거나 세션 문제) - 체크포인트 변경 없음
    else:
        # 실패(판단/공감 실패)한 글은 이번 실행에서는 포기하고 다음 새 글로 체크포인트를 세운다.
        # resume 상태에서 실패를 이유로 체크포인트를 미루더라도, 실패가 계속되면 결국 max_pages를
        # 넘어 "체크포인트 못 찾음" 상태가 되고 그때는 실패 여부와 무관하게 저장하게 되므로,
        # 처음부터 이렇게 통일하는 편이 동작을 예측하기 쉽고 매번 배치를 통째로 재처리하지 않는다.
        storage.save("last_article_id", first_article_id)
        if failed:
            logging.warning(f"{failed}건 실패했지만, 해당 글은 포기하고 체크포인트는 갱신합니다.")

    return {
        "processed": processed,
        "skipped": skipped,
        "already": already,
        "failed": failed,
        "candidates": len(articles_to_vote),
        "scanned": scanned,
        "final_page": final_page,
        "checkpoint_found": checkpoint_found,
        "scan_limit_reached": scan_limit_reached,
        "is_initial": is_initial,
        "success": True,
    }


class VoteRunner:
    def __init__(self, cfg, *, require_board: bool = True):
        self.cfg = cfg
        self.storage = db
        self.supabase = self.storage.supabase

        session_value = self.storage.load("etsid")
        if not session_value:
            raise ValueError("etsid가 저장되어 있지 않습니다. /setsession 을 먼저 실행하세요.")

        self.client = EverytimeClient(session_value)
        self.target_board = self.storage.load("board_id")
        if require_board and not self.target_board:
            raise ValueError("게시판이 선택되지 않았습니다. 텔레그램에서 /setboard를 먼저 실행하세요.")

    def check_session(self, board_id):
        return self.client.check_session(board_id)

    def get_board_list(self) -> list[dict]:
        return self.client.get_board_list()

    def delete_my_articles(self) -> int:
        """최신 페이지에서 본인(is_mine=True) 게시글을 찾아 삭제. 삭제된 개수 반환."""
        deleted = 0
        for page_idx in range(BOARD_PAGE_SIZE):
            articles = self.client.get_article_ids(self.target_board, start_num=page_idx*PAGE_NUM)
            for a in articles:
                if a.get("is_mine"):
                    if not a.get("posvote"):
                        if self.client.delete_article(a["id"]):
                            logging.info(
                                "[VoteRunner] 본인 게시글 삭제 완료: ",
                                extra={
                                    "article_id": a["id"], 
                                    "title": a.get("title"), 
                                    "created_at": a.get("created_at")
                                    }
                                )
                            deleted += 1
        return deleted

    def start(self, progress_callback=None, skip_keywords: list[str] | None = None) -> dict:
        if not self.target_board:
            raise ValueError("게시판이 선택되지 않았습니다. 텔레그램에서 /setboard를 먼저 실행하세요.")
        dry_run = get_dry_run()
        taste_cfg = load_taste_config()

        target_like_rate = taste_cfg["decision"].get("target_like_rate")
        if target_like_rate is not None:
            recent_scores = self.storage.get_recent_final_scores()
            adaptive_threshold = score_calculator.compute_adaptive_threshold(
                recent_scores,
                target_like_rate,
                taste_cfg["decision"]["strictness"],
                fallback=taste_cfg["decision"]["threshold"],
            )
            taste_cfg = {**taste_cfg, "decision": {**taste_cfg["decision"], "threshold": adaptive_threshold, "strictness": 0.0}}
            logging.info(
                f"[VoteRunner] 적응형 threshold={adaptive_threshold:.3f} "
                f"(목표 비율 {target_like_rate:.0%}, 최근 표본 {len(recent_scores)}개)"
            )

        feature_client = FeatureScorer(
            topics=taste_cfg.get("topics"),
            recent_fingerprints_provider=lambda: get_recent_post_fingerprints(self.target_board),
            record_fingerprint=lambda tokens: add_post_fingerprint(self.target_board, tokens),
        )
        interest_decider = PostEvaluator(
            feature_client,
            taste_cfg,
            skip_keywords=skip_keywords,
            dry_run=dry_run,
        )
        if dry_run:
            logging.warning("[VoteRunner] DRY_RUN 모드로 실행합니다 — 실제 공감은 누르지 않습니다.")
        return run_vote(
            client=self.client,
            storage=self.storage,
            cfg=self.cfg,
            target_board=self.target_board,
            progress_callback=progress_callback,
            skip_keywords=skip_keywords,
            interest_decider=interest_decider,
            dry_run=dry_run,
        )
