import asyncio

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from ...core.vote_runner import VoteRunner
from ...core.database import db
from ...config import load_config, load_taste_config
from .shared import _authorized
from .vote_command import cmd_vote

ASK_EMPATHY = 0

VOTE_BUTTON_TEXT = "🗳 투표하기"

_VOTE_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton(VOTE_BUTTON_TEXT)]], resize_keyboard=True
)

_COMMAND_LIST = (
    "📋 사용 가능한 명령어:\n"
    "/setsession — etsid 수동 입력\n"
    "/setboard — 공감할 게시판 선택\n"
    "/vote — 공감 봇 실행\n"
    "/addskip 키워드 — 건너뛸 키워드 추가\n"
    "/removeskip 키워드 — 키워드 삭제\n"
    "/listskip — 등록된 키워드 목록\n"
    "/stats — 공감 통계\n"
    "/togglestat <번호> — 레코드 유효/제외 토글\n"
    "/deletestat <번호> — 레코드 영구 삭제\n"
    "/status — 현재 상태 확인\n"
    "/profile — 현재 취향 파라미터 확인"
)

_PROFILE_LABELS = {
    "topic_relevance": "관심주제", "effort": "노력", "information_density": "정보 밀도",
    "promotion": "광고 회피", "toxicity": "혐오 회피", "clickbait": "낚시 회피",
    "controversy": "논쟁 회피", "repetitiveness": "재탕 회피",
}


def _bar(value: float, width: int = 10) -> str:
    filled = round(max(0.0, min(1.0, value)) * width)
    return "█" * filled


def _profile_line(key: str, value: float) -> str:
    label = _PROFILE_LABELS.get(key, key)
    return f"{label:<8} {_bar(value)} {round(value * 100)}%"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "공감하시겠습니까? (1: 예 / 0: 아니오)", reply_markup=_VOTE_KEYBOARD
    )
    return ASK_EMPATHY


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    await update.message.reply_text(
        "아래 버튼을 눌러 공감을 실행하세요.", reply_markup=_VOTE_KEYBOARD
    )


async def vote_button_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    await cmd_vote(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    await update.message.reply_text(_COMMAND_LIST)


async def start_empathy_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.message.text.strip()
    if answer == "1":
        await cmd_vote(update, context)
    elif answer == "0":
        await update.message.reply_text("공감을 실행하지 않습니다.")
    else:
        await update.message.reply_text("1 또는 0으로 답해주세요. (1: 예 / 0: 아니오)")
        return ASK_EMPATHY

    await update.message.reply_text(_COMMAND_LIST)
    return ConversationHandler.END


async def start_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("취소되었습니다.")
    return ConversationHandler.END


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return

    taste = load_taste_config()
    preferences = taste.get("preferences", {})
    penalties = taste.get("penalties", {})
    decision = taste.get("decision", {})

    lines = ["현재 취향 프로필", ""]
    lines += [_profile_line(k, v) for k, v in preferences.items()]
    lines.append("")
    lines += [_profile_line(k, v) for k, v in penalties.items()]
    lines.append("")
    target_like_rate = decision.get("target_like_rate")
    if target_like_rate is not None:
        lines.append(f"Threshold: 적응형 (최근 평가 중 상위 {round(target_like_rate * 100)}% 목표)")
    else:
        lines.append(f"Threshold: {round(decision.get('threshold', 0) * 100)}%")
    lines.append(f"Strictness: {round(decision.get('strictness', 0) * 100)}%")
    lines.append(f"Exploration: {round(decision.get('exploration', 0) * 100)}%")

    await update.message.reply_text("\n".join(lines))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return

    etsid_saved = db.exists("etsid")
    session_valid = False
    board_name_cached = db.load("board_name")

    selected_board_id = db.load("board_id")

    if etsid_saved and selected_board_id:
        try:
            cfg = load_config()
            loop = asyncio.get_running_loop()
            bot = VoteRunner(cfg)
            session_valid = await loop.run_in_executor(
                None, bot.check_session, bot.target_board
            )
            if not board_name_cached and db.load("board_id"):
                boards = await loop.run_in_executor(None, bot.get_board_list)
                found = next(
                    (b["name"] for b in boards if b["id"] == db.load("board_id")),
                    None,
                )
                if found:
                    db.save("board_name", found)
                    board_name_cached = found
        except Exception:
            pass

    cfg = load_config()
    current_board = (
        board_name_cached
        or selected_board_id
        or "미선택 (/setboard 필요)"
    )

    await update.message.reply_text(
        f"🔐 세션: {'✅ 저장됨' if etsid_saved else '❌ 없음'}\n"
        f"📡 세션: {'✅ 유효' if session_valid else '❌ 만료/없음'}\n"
        f"📋 게시판: {current_board}\n"
        f"🕐 마지막 실행: {db.load('last_run_time') or '없음'}"
    )
