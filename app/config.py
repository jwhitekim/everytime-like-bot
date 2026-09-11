import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv, set_key

KST = timezone(timedelta(hours=9))


class KSTFormatter(logging.Formatter):
    """모든 애플리케이션 로그 시각을 KST 오프셋과 함께 표시."""

    def formatTime(self, record, datefmt=None):
        value = datetime.fromtimestamp(record.created, KST)
        if datefmt:
            return value.strftime(datefmt)
        return value.isoformat(sep=" ", timespec="milliseconds")


DEFAULTS = {
    "bot": {
        "max_pages": 5,
    },
    "timing": {
        "sleep_min": 1.0,
        "sleep_max": 3.0,
        "page_delay": 0.5,
    },
    "logging": {
        "level": "INFO",
        "format": "%(asctime)s - %(levelname)s - %(message)s",
    },
}

_ROOT = Path(__file__).parent
# 실제 비밀값은 소스 코드와 분리된 프로젝트 루트의 .env에만 둔다.
# .env와 data/는 .gitignore 및 .dockerignore에서 제외된다.
ENV_FILE = _ROOT.parent / ".env"
TASTE_CONFIG_FILE = _ROOT / "config" / "taste.json"

# 규칙 기반으로 측정하는 게시글 특성 이름 (taste.json의 preferences/penalties/hard_reject 키와
# 일치해야 함). 의미 이해가 필요해 알고리즘으로 근거 있게 계산할 수 없는 특성(재미, 독창성,
# 유용성, 감성 자극, 기술적 깊이, 명확성, 새로움, 개인적 흥미)은 제외했다.
POSITIVE_FEATURES = ("topic_relevance", "effort", "information_density")
PENALTY_FEATURES = ("promotion", "toxicity", "clickbait", "controversy", "repetitiveness")
ALL_FEATURES = POSITIVE_FEATURES + PENALTY_FEATURES

# taste.json이 없거나 일부 키가 빠졌을 때 사용하는 기본값.
DEFAULT_TASTE_CONFIG: dict[str, Any] = {
    "preferences": {
        "topic_relevance": 0.9,
        "effort": 0.5,
        "information_density": 0.5,
    },
    "penalties": {
        "promotion": 1.0,
        "toxicity": 1.0,
        "clickbait": 0.7,
        "controversy": 0.5,
        "repetitiveness": 0.6,
    },
    "decision": {
        "threshold": 0.31,
        "strictness": 0.3,
        "exploration": 0.07,
        "penalty_strength": 1.0,
        # 설정하면 threshold를 고정값 대신 "최근 평가 기록 중 상위 N%" 기준으로 매번 다시
        # 계산한다 (게시판 콘텐츠 성향이 시간이 지나 바뀌어도 threshold를 수동으로 재조정할
        # 필요가 없어짐). null이면 기존처럼 threshold 고정값을 그대로 쓴다.
        "target_like_rate": None,
    },
    "hard_reject": {
        "promotion": 0.85,
        "toxicity": 0.7,
    },
    # topic_relevance를 측정할 기준 키워드. 비워두면 topic_relevance는 항상 중립값(0.5).
    "topics": [],
    # 규칙 기반 특성 계산에 쓰는 키워드 블랙리스트. 소규모 스타터 세트이므로 실제 게시판
    # 성향에 맞춰 직접 추가/편집하는 걸 전제로 한다 (skip_keywords와 같은 방식).
    "keywords": {
        "toxicity": ["씨발", "병신", "지랄", "미친놈", "미친년", "개새끼", "닥쳐", "꺼져"],
        "controversy": ["정치", "페미", "남혐", "여혐", "지역감정", "종교", "동성애"],
        "clickbait_phrases": ["충격", "실화냐", "레전드", "헐", "대박", "미쳤다", "지금 아니면", "곧 삭제"],
        "promotion_terms": ["문의", "카톡", "오픈채팅", "구매대행", "판매합니다", "가격문의", "할인", "이벤트", "홍보"],
    },
    "hard_filter": {
        "max_age_days": None,
    },
}

_KEYWORD_LIST_KEYS = ("toxicity", "controversy", "clickbait_phrases", "promotion_terms")

# decision 항목 중 0.0~1.0로 clamp할 필드 (penalty_strength는 배율이라 제외).
_DECISION_UNIT_RANGE_FIELDS = ("threshold", "strictness", "exploration")
_PENALTY_STRENGTH_MAX = 2.0


def _clamp01(value) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    return max(0.0, min(1.0, value))


def _validate_taste_config(cfg: dict) -> dict:
    """알 수 없는 키/타입이 아닌 값/범위를 벗어난 값을 걸러내 안전한 설정으로 만든다."""
    cfg = {**cfg}

    for section, known_keys in (("preferences", POSITIVE_FEATURES), ("penalties", PENALTY_FEATURES)):
        raw = cfg.get(section, {})
        cleaned = {}
        for key in known_keys:
            if key not in raw:
                continue
            clamped = _clamp01(raw[key])
            if clamped is None:
                logging.warning(f"taste.json {section}.{key} 값이 올바르지 않아 무시합니다: {raw[key]!r}")
                continue
            cleaned[key] = clamped
        for key in raw:
            if key not in known_keys:
                logging.warning(f"taste.json {section}에 알 수 없는 키를 무시합니다: {key}")
        cfg[section] = {**DEFAULT_TASTE_CONFIG[section], **cleaned}

    decision = {**DEFAULT_TASTE_CONFIG["decision"], **cfg.get("decision", {})}
    for key in _DECISION_UNIT_RANGE_FIELDS:
        clamped = _clamp01(decision.get(key))
        decision[key] = clamped if clamped is not None else DEFAULT_TASTE_CONFIG["decision"][key]
    try:
        penalty_strength = float(decision.get("penalty_strength", 1.0))
        decision["penalty_strength"] = max(0.0, min(_PENALTY_STRENGTH_MAX, penalty_strength))
    except (TypeError, ValueError):
        decision["penalty_strength"] = DEFAULT_TASTE_CONFIG["decision"]["penalty_strength"]

    target_rate = decision.get("target_like_rate")
    if target_rate is None:
        decision["target_like_rate"] = None
    else:
        clamped = _clamp01(target_rate)
        if clamped is None:
            logging.warning(f"taste.json decision.target_like_rate 값이 올바르지 않아 무시합니다: {target_rate!r}")
        decision["target_like_rate"] = clamped

    cfg["decision"] = decision

    hard_reject = {}
    for key, value in cfg.get("hard_reject", {}).items():
        if key not in ALL_FEATURES:
            logging.warning(f"taste.json hard_reject에 알 수 없는 키를 무시합니다: {key}")
            continue
        clamped = _clamp01(value)
        if clamped is None:
            logging.warning(f"taste.json hard_reject.{key} 값이 올바르지 않아 무시합니다: {value!r}")
            continue
        hard_reject[key] = clamped
    cfg["hard_reject"] = hard_reject

    topics = cfg.get("topics", [])
    cfg["topics"] = [str(t) for t in topics] if isinstance(topics, list) else []

    raw_keywords = cfg.get("keywords", {})
    keywords = {}
    for key in _KEYWORD_LIST_KEYS:
        value = raw_keywords.get(key, DEFAULT_TASTE_CONFIG["keywords"][key])
        keywords[key] = [str(w) for w in value] if isinstance(value, list) else DEFAULT_TASTE_CONFIG["keywords"][key]
    for key in raw_keywords:
        if key not in _KEYWORD_LIST_KEYS:
            logging.warning(f"taste.json keywords에 알 수 없는 키를 무시합니다: {key}")
    cfg["keywords"] = keywords

    max_age_days = cfg.get("hard_filter", {}).get("max_age_days")
    if max_age_days is not None:
        try:
            max_age_days = float(max_age_days)
        except (TypeError, ValueError):
            logging.warning(f"taste.json hard_filter.max_age_days 값이 올바르지 않아 무시합니다: {max_age_days!r}")
            max_age_days = None
    cfg["hard_filter"] = {"max_age_days": max_age_days}

    return cfg

# Everytime API client
EVERYTIME_BASE_URL = "https://api.everytime.kr"
EVERYTIME_REQUEST_TIMEOUT = 10
EVERYTIME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

# Board selection UI
BOARD_PAGE_SIZE = 10

# Page size for article listing
PAGE_NUM = 20

# 체크포인트 게시글을 찾을 때는 bot.max_pages(초기 스캔 한도)와 무관하게 더 넓게 탐색한다.
# 봇이 오래 쉬었다 재개하면 체크포인트 글이 초기 스캔 한도보다 뒤 페이지에 있을 수 있는데,
# 여기서도 같은 한도를 쓰면 아직 살아있는 글을 삭제된 것으로 오인해 그 사이 글들을 놓친다.
CHECKPOINT_SEARCH_MAX_PAGES = 200

# Autonomy scheduling
AUTONOMY_WINDOW_A = ((6, 0), (9, 0))
AUTONOMY_WINDOW_B = ((17, 0), (20, 0))
AUTONOMY_TREND_SEARCH_RANGES = (((5, 0), (12, 0)), ((15, 0), (23, 0)))
AUTONOMY_TREND_LOOKBACK_DAYS = 7
AUTONOMY_TREND_MAX_PAGES = 5
AUTONOMY_TREND_MIN_SAMPLES = 20
AUTONOMY_TREND_MIN_SLOT_SAMPLES = 3
AUTONOMY_UNREAD_CHECK_WAIT_SECONDS = 2 * 60
AUTONOMY_ACTIVITY_RETRY_WAIT_RANGE = (5 * 60, 15 * 60)
AUTONOMY_MAX_ACTIVITY_ATTEMPTS = 3
AUTONOMY_DAILY_RESCHEDULE_JOB_NAME = "autonomy_daily_reschedule"
AUTONOMY_BOOTSTRAP_JOB_NAME = "autonomy_bootstrap_reschedule"
AUTONOMY_TRIGGER_JOB_NAME_PREFIX = "autonomy_trigger_"
AUTONOMY_SLOT_LABELS = {"A": "오전 슬롯", "B": "오후 슬롯"}
AUTONOMY_SLOT_EMOJIS = {"A": "☀️", "B": "🌙"}


def load_env() -> None:
    load_dotenv(ENV_FILE)


def _deep_merge(base, override):
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(path=None):
    if path is None:
        path = _ROOT / "config" / "config.yaml"
    try:
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        user_cfg = {}
    return _deep_merge(DEFAULTS, user_cfg)


def load_taste_config(path=None) -> dict:
    """취향 파라미터(config/taste.json)를 읽는다. 없거나 일부 키가 빠지면 기본값으로 채운다."""
    if path is None:
        path = TASTE_CONFIG_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f) or {}
    except FileNotFoundError:
        user_cfg = {}
    except (json.JSONDecodeError, OSError) as e:
        logging.error(f"taste.json 파싱 실패, 기본값을 사용합니다: {e}")
        user_cfg = {}
    merged = _deep_merge(DEFAULT_TASTE_CONFIG, user_cfg)
    return _validate_taste_config(merged)


def get_telegram_token() -> str:
    return os.environ.get("TELEGRAM_TOKEN", "").strip()


def get_telegram_chat_id() -> int | None:
    raw = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not raw:
        logging.error("TELEGRAM_CHAT_ID 환경변수가 설정되지 않았습니다.")
        return None
    try:
        return int(raw)
    except ValueError:
        logging.error("TELEGRAM_CHAT_ID는 숫자여야 합니다.")
        return None


def get_supabase_credentials() -> tuple[str, str]:
    return (
        os.environ.get("SUPABASE_URL", "").strip(),
        os.environ.get("SUPABASE_KEY", "").strip(),
    )


def get_encryption_key() -> str:
    return os.environ.get("ENCRYPTION_KEY", "").strip()


def get_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def set_encryption_key(key: str) -> None:
    set_key(str(ENV_FILE), "ENCRYPTION_KEY", key)
    os.environ["ENCRYPTION_KEY"] = key


def build_everytime_headers(etsid: str) -> dict:
    return {
        "Host": "api.everytime.kr",
        "Connection": "keep-alive",
        "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": EVERYTIME_USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://everytime.kr",
        "Referer": "https://everytime.kr/",
        "Cookie": f"etsid={etsid};",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }
