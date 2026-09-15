import unittest

from app.core.vote_runner import run_vote


class FakeStorage:
    def __init__(self, values=None, *, fail_load=False):
        self.values = dict(values or {})
        self.saves = []
        self.fail_load = fail_load

    def load(self, key, *, raise_on_error: bool = False):
        if self.fail_load and raise_on_error:
            raise RuntimeError("storage unavailable")
        return self.values.get(key)

    def save(self, key, value):
        self.saves.append((key, value))
        self.values[key] = value


class FakeClient:
    def __init__(self, pages, storage, vote_result="voted"):
        self.storage = storage
        self.pages = pages
        self.vote_result = vote_result
        self.voted_ids = []
        self.page_fetch_calls = []

    def check_session(self, board_id):
        return True

    def get_article_ids(self, board_id, limit_num=20, start_num=0):
        self.page_fetch_calls.append(start_num)
        return self.pages.get(start_num, [])

    def push_vote(self, article_id):
        if self.storage.load("last_article_id") == "newest":
            raise AssertionError("checkpoint must not be updated before votes finish")
        self.voted_ids.append(article_id)
        return self.vote_result


class FakeInterestDecider:
    def __init__(self, liked_ids, failing_ids=()):
        self.liked_ids = set(liked_ids)
        self.failing_ids = set(failing_ids)
        self.seen_ids = []
        self.marked_liked_ids = []

    def should_vote(self, article):
        self.seen_ids.append(article["id"])
        if article["id"] in self.failing_ids:
            return None  # 일시적 LLM 평가 실패 흉내
        return article["id"] in self.liked_ids

    def mark_liked(self, article_id):
        self.marked_liked_ids.append(article_id)


def article(article_id, posvote=0):
    return {
        "id": article_id,
        "title": f"title {article_id}",
        "created_at": "2026-06-21 12:00:00",
        "posvote": posvote,
    }


class VoterTests(unittest.TestCase):
    def _run(self, client, storage):
        cfg = {
            "bot": {"board_id": "board", "max_pages": 5},
            "timing": {"sleep_min": 0, "sleep_max": 0, "page_delay": 0},
        }
        return run_vote(
            client=client,
            storage=storage,
            cfg=cfg,
            target_board="board",
        )

    def test_votes_only_articles_before_found_checkpoint_then_updates_checkpoint(self):
        storage = FakeStorage({"last_article_id": "old"})
        client = FakeClient({
            0: [article("newest"), article("newer"), article("old"), article("older")]
        }, storage)

        result = self._run(client, storage)

        self.assertEqual(client.voted_ids, ["newest", "newer"])
        self.assertTrue(result["checkpoint_found"])
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["scanned"], 4)
        self.assertEqual(storage.load("last_article_id"), "newest")

    def test_reports_scan_limit_when_checkpoint_is_not_found(self):
        """체크포인트를 못 찾으면(글 삭제 추정), 실제 처리 대상은 초기 스캔과 같은
        max_pages(5)로 제한된다 — 탐색 자체는 200페이지까지 넓게 봐도, 대량 처리로
        이어지지는 않아야 한다."""
        storage = FakeStorage({"last_article_id": "missing"})
        pages = {
            i * 20: [article(f"post-{i}-{j}") for j in range(20)]
            for i in range(10)  # 체크포인트 탐색 상한(200페이지)보다는 한참 적지만
        }                       # 초기 스캔 상한(max_pages=5)보다는 많은 페이지
        client = FakeClient(pages, storage)

        result = self._run(client, storage)

        self.assertFalse(result["checkpoint_found"])
        self.assertTrue(result["scan_limit_reached"])
        self.assertEqual(result["scanned"], 100)  # 5페이지 x 20개 = 100개 (200개가 아님)
        self.assertEqual(result["candidates"], 100)
        self.assertEqual(result["processed"], 100)
        # 탐색과 수집을 한 번에 하므로, 같은 페이지를 두 번 요청하지 않는다
        # (10페이지 데이터 전부 존재하지만 빈 페이지를 만나 11번째 시도에서 멈춤).
        self.assertEqual(client.page_fetch_calls, [i * 20 for i in range(11)])

    def test_advances_checkpoint_to_newest_even_when_a_vote_fails(self):
        """실패한 개별 글은 이번 실행에서 포기하고, 체크포인트는 항상 이번에 확인한 최신 글로 세운다
        (resume 상태에서 실패 때문에 계속 미루면 결국 max_pages를 넘어 '못 찾음' 상태가 되어 버려서,
        어차피 그때는 실패 여부와 무관하게 저장하게 된다 - 처음부터 이렇게 통일한다)."""
        storage = FakeStorage({"last_article_id": "old"})
        client = FakeClient({
            0: [article("newest"), article("old")]
        }, storage, vote_result="failed")

        result = self._run(client, storage)

        self.assertEqual(result["failed"], 1)
        self.assertEqual(storage.load("last_article_id"), "newest")

    def test_dry_run_does_not_call_push_vote(self):
        storage = FakeStorage({"last_article_id": "old"})
        client = FakeClient({
            0: [article("newest"), article("old")]
        }, storage)
        cfg = {
            "bot": {"board_id": "board", "max_pages": 5},
            "timing": {"sleep_min": 0, "sleep_max": 0, "page_delay": 0},
        }

        result = run_vote(
            client=client,
            storage=storage,
            cfg=cfg,
            target_board="board",
            dry_run=True,
        )

        self.assertEqual(client.voted_ids, [])
        self.assertEqual(result["processed"], 1)
        # dry-run은 실제로 아무것도 처리하지 않았으므로 체크포인트를 전진시키면 안 된다 —
        # 그래야 실제 모드로 전환했을 때 이 글들을 다시 검토한다.
        self.assertEqual(storage.load("last_article_id"), "old")

    def test_votes_only_articles_selected_by_interest_decider(self):
        storage = FakeStorage({"last_article_id": "old"})
        client = FakeClient({
            0: [article("liked"), article("not-liked"), article("old")]
        }, storage)
        decider = FakeInterestDecider({"liked"})
        cfg = {
            "bot": {"board_id": "board", "max_pages": 5},
            "timing": {"sleep_min": 0, "sleep_max": 0, "page_delay": 0},
        }

        result = run_vote(
            client=client,
            storage=storage,
            cfg=cfg,
            target_board="board",
            interest_decider=decider,
        )

        self.assertEqual(decider.seen_ids, ["liked", "not-liked"])
        self.assertEqual(client.voted_ids, ["liked"])
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["skipped"], 1)

    def test_marks_liked_only_after_push_vote_succeeds(self):
        storage = FakeStorage({"last_article_id": "old"})
        client = FakeClient({
            0: [article("liked"), article("old")]
        }, storage)
        decider = FakeInterestDecider({"liked"})
        cfg = {
            "bot": {"board_id": "board", "max_pages": 5},
            "timing": {"sleep_min": 0, "sleep_max": 0, "page_delay": 0},
        }

        run_vote(
            client=client, storage=storage, cfg=cfg, target_board="board", interest_decider=decider,
        )

        self.assertEqual(decider.marked_liked_ids, ["liked"])

    def test_does_not_mark_liked_when_push_vote_fails(self):
        storage = FakeStorage({"last_article_id": "old"})
        client = FakeClient({
            0: [article("liked"), article("old")]
        }, storage, vote_result="failed")
        decider = FakeInterestDecider({"liked"})
        cfg = {
            "bot": {"board_id": "board", "max_pages": 5},
            "timing": {"sleep_min": 0, "sleep_max": 0, "page_delay": 0},
        }

        run_vote(
            client=client, storage=storage, cfg=cfg, target_board="board", interest_decider=decider,
        )

        self.assertEqual(decider.marked_liked_ids, [])

    def test_aborts_without_scanning_when_checkpoint_lookup_fails(self):
        """저장소 조회 자체가 실패하면 '체크포인트 없음(최초 실행)'으로 오인해서 최신 N페이지를
        다시 스캔하면 안 된다 - 멀쩡한 체크포인트를 무시하고 그 사이 밀려난 글을 놓치게 된다."""
        storage = FakeStorage({"last_article_id": "old"}, fail_load=True)
        client = FakeClient({0: [article("newest"), article("old")]}, storage)

        result = self._run(client, storage)

        self.assertFalse(result["success"])
        self.assertEqual(client.voted_ids, [])
        self.assertEqual(storage.load("last_article_id"), "old")  # 값 자체는 안 건드려짐

    def test_advances_checkpoint_despite_failure_during_initial_scan(self):
        """체크포인트가 아예 없는 최초 실행은 매번 '현재 시점 최신 N페이지'를 다시 스캔하므로,
        개별 실패 때문에 체크포인트를 안 세우면 다음 실행 사이 쌓인 새 글에 밀려 스캔 범위 밖으로
        벗어난 글이 영구히 누락될 수 있다. 그래서 실패가 있어도 체크포인트는 세워야 한다."""
        storage = FakeStorage({})  # last_article_id 없음 -> is_initial
        client = FakeClient({
            0: [article("newest"), article("bad"), article("old")]
        }, storage)
        decider = FakeInterestDecider(liked_ids=set(), failing_ids={"bad"})
        cfg = {
            "bot": {"board_id": "board", "max_pages": 5},
            "timing": {"sleep_min": 0, "sleep_max": 0, "page_delay": 0},
        }

        result = run_vote(
            client=client, storage=storage, cfg=cfg, target_board="board", interest_decider=decider,
        )

        self.assertEqual(result["failed"], 1)
        self.assertEqual(storage.load("last_article_id"), "newest")

    def test_advances_checkpoint_despite_failure_when_checkpoint_not_found(self):
        storage = FakeStorage({"last_article_id": "long-gone"})
        client = FakeClient({
            0: [article("newest"), article("bad"), article("old")]
        }, storage)
        decider = FakeInterestDecider(liked_ids=set(), failing_ids={"bad"})
        cfg = {
            "bot": {"board_id": "board", "max_pages": 5},
            "timing": {"sleep_min": 0, "sleep_max": 0, "page_delay": 0},
        }

        result = run_vote(
            client=client, storage=storage, cfg=cfg, target_board="board", interest_decider=decider,
        )

        self.assertFalse(result["checkpoint_found"])
        self.assertEqual(result["failed"], 1)
        self.assertEqual(storage.load("last_article_id"), "newest")

    def test_dry_run_does_not_mark_liked(self):
        storage = FakeStorage({"last_article_id": "old"})
        client = FakeClient({
            0: [article("liked"), article("old")]
        }, storage)
        decider = FakeInterestDecider({"liked"})
        cfg = {
            "bot": {"board_id": "board", "max_pages": 5},
            "timing": {"sleep_min": 0, "sleep_max": 0, "page_delay": 0},
        }

        run_vote(
            client=client, storage=storage, cfg=cfg, target_board="board",
            interest_decider=decider, dry_run=True,
        )

        self.assertEqual(decider.marked_liked_ids, [])


if __name__ == "__main__":
    unittest.main()
