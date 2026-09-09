import json
import logging
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from supabase import create_client

from ..config import get_encryption_key, get_supabase_credentials, load_env, set_encryption_key


class SecureDatabase:
    def __init__(self):
        load_env()

        url, key_sb = get_supabase_credentials()
        if not url or not key_sb:
            raise RuntimeError("SUPABASE_URL 또는 SUPABASE_KEY 환경변수가 설정되지 않았습니다.")

        self.supabase = create_client(url, key_sb)

        enc_key = get_encryption_key()
        if not enc_key:
            enc_key = Fernet.generate_key().decode()
            set_encryption_key(enc_key)
            logging.warning(
                "새 암호화 키를 생성했습니다. "
                "Railway 배포 시 ENCRYPTION_KEY를 영구 환경변수로 설정하세요."
            )
        self._fernet = Fernet(enc_key.encode())

    def _encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def _decrypt(self, encrypted: str) -> str | None:
        try:
            return self._fernet.decrypt(encrypted.encode()).decode()
        except Exception:
            logging.error("복호화 실패")
            return None

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def save(self, key: str, value: str) -> None:
        try:
            self.supabase.table("bot_storage").upsert({
                "key": key,
                "value": self._encrypt(value),
                "updated_at": self._now(),
            }).execute()
        except Exception as e:
            logging.error(f"db.save 실패 ({key}): {e}")

    def claim_once(self, key: str, value: str) -> bool | None:
        """고유 키를 원자적으로 선점.

        True는 이번 호출이 선점했음을, False는 이미 선점된 키임을 뜻한다.
        저장소 오류는 중복 실행을 막기 위해 None으로 구분해 호출자가 실행을
        중단할 수 있게 한다.
        """
        try:
            self.supabase.table("bot_storage").insert({
                "key": key,
                "value": self._encrypt(value),
                "updated_at": self._now(),
            }).execute()
            return True
        except Exception as e:
            if getattr(e, "code", None) == "23505" or "duplicate key" in str(e).lower():
                return False
            logging.error(f"db.claim_once 실패 ({key}): {e}")
            return None

    def load(self, key: str, *, raise_on_error: bool = False) -> str | None:
        try:
            res = (
                self.supabase.table("bot_storage")
                .select("value")
                .eq("key", key)
                .execute()
            )
            if res.data:
                return self._decrypt(res.data[0]["value"])
        except Exception as e:
            logging.error(f"db.load 실패 ({key}): {e}")
            if raise_on_error:
                raise
        return None

    def delete(self, key: str) -> None:
        try:
            self.supabase.table("bot_storage").delete().eq("key", key).execute()
        except Exception as e:
            logging.error(f"db.delete 실패 ({key}): {e}")

    def exists(self, key: str) -> bool:
        return self.load(key) is not None

    def get_recent_final_scores(self, limit: int = 300) -> list[float]:
        """최근 LLM으로 평가된(hard_filter로 걸러지지 않은) 글들의 final_score 목록.

        적응형 threshold(decision.target_like_rate) 계산용 — 실제로 특성을 측정한 글만
        포함한다 (hard_filter로 걸러진 글은 final_score=0.0/feature_scores={}로 저장되는데,
        이걸 섞으면 분포가 실제보다 낮게 왜곡된다).
        """
        try:
            res = (
                self.supabase.table("bot_storage")
                .select("value")
                .like("key", "evaluated_post:%")
                .order("updated_at", desc=True)
                .limit(limit)
                .execute()
            )
        except Exception as e:
            logging.error(f"get_recent_final_scores 실패: {e}")
            return []

        scores = []
        for row in res.data:
            raw = self._decrypt(row["value"])
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if rec.get("feature_scores"):
                scores.append(rec.get("final_score", 0.0))
        return scores


class LazyDatabase:
    def __init__(self):
        self._db = None

    def _get(self) -> SecureDatabase:
        if self._db is None:
            self._db = SecureDatabase()
        return self._db

    @property
    def supabase(self):
        return self._get().supabase

    def save(self, key: str, value: str) -> None:
        self._get().save(key, value)

    def claim_once(self, key: str, value: str) -> bool | None:
        return self._get().claim_once(key, value)

    def load(self, key: str, *, raise_on_error: bool = False) -> str | None:
        return self._get().load(key, raise_on_error=raise_on_error)

    def get_recent_final_scores(self, limit: int = 300) -> list[float]:
        return self._get().get_recent_final_scores(limit)

    def delete(self, key: str) -> None:
        self._get().delete(key)

    def exists(self, key: str) -> bool:
        return self._get().exists(key)


db = LazyDatabase()


def get_skip_keywords() -> list[str]:
    raw = db.load("skip_keywords")
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


def save_skip_keywords(keywords: list[str]) -> None:
    db.save("skip_keywords", json.dumps(keywords))


def load_run_history() -> list[dict]:
    raw = db.load("run_history")
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


def save_run_history(history: list[dict]) -> None:
    db.save("run_history", json.dumps(history))


def get_evaluated_post(post_id: str) -> dict | None:
    """이미 LLM으로 평가한 게시글이면 저장된 평가 결과를 반환 (재평가 방지용)."""
    raw = db.load(f"evaluated_post:{post_id}")
    try:
        return json.loads(raw) if raw else None
    except Exception:
        return None


def save_evaluated_post(
    post_id: str,
    *,
    evaluated_at: str,
    feature_scores: dict,
    final_score: float,
    decision: str,
    liked: bool,
) -> None:
    db.save(f"evaluated_post:{post_id}", json.dumps({
        "post_id": post_id,
        "evaluated_at": evaluated_at,
        "feature_scores": feature_scores,
        "final_score": final_score,
        "decision": decision,
        "liked": liked,
    }))


_RECENT_FINGERPRINTS_LIMIT = 50


def get_recent_post_fingerprints(board_id: str) -> list[list[str]]:
    """repetitiveness 계산용 — 게시판별 최근 게시글의 토큰 목록."""
    raw = db.load(f"recent_fingerprints:{board_id}")
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


def add_post_fingerprint(board_id: str, tokens: list[str]) -> None:
    fingerprints = get_recent_post_fingerprints(board_id)
    fingerprints.append(tokens)
    db.save(f"recent_fingerprints:{board_id}", json.dumps(fingerprints[-_RECENT_FINGERPRINTS_LIMIT:]))


def delete_run_stat(idx: int) -> bool:
    history = load_run_history()
    if not (0 <= idx < len(history)):
        return False
    history.pop(idx)
    save_run_history(history)
    return True


def append_run_stat(board_id: str, voted: int, skipped: int, ran_at: str, is_full_scan: bool | None = None) -> None:
    history = load_run_history()
    board_name = db.load("board_name") or board_id
    history.append({
        "board_id": board_id,
        "board_name": board_name,
        "voted": voted,
        "skipped": skipped,
        "ran_at": ran_at,
        "is_valid": True,
        "is_full_scan": is_full_scan,
    })
    db.save("run_history", json.dumps(history[-30:]))
