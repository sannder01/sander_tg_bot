"""
habits.py — Chronicle Engine: Habit Tracker Module
===================================================
Telegram-side commands and callbacks for the habit tracker.

Commands:
  /habits       — show habit list with today's check-off status
  /newhabit     — create a new habit (FSM flow)

Callbacks (inline keyboard):
  hb_tog_{id}   — toggle today's completion for habit {id}
  hb_del_{id}   — delete habit {id} (with confirmation)
  hb_deld_{id}  — confirmed delete
  hb_back       — back to habits list
  hb_new        — start create habit FSM
  hb_noop       — no-op (header buttons)
"""

import logging
from datetime import date, datetime
from typing import Optional, List

import db
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

# ── FSM States ────────────────────────────────────────────────────────────────
HB_WAIT_NAME, HB_WAIT_FREQ = range(10, 12)

# ── Constants ─────────────────────────────────────────────────────────────────
DIVIDER = "━━━━━━━━━━━━━━━━━━━━"
FREQ_ICONS = {"daily": "📅", "weekly": "📆"}
FREQ_LABELS = {"daily": "Ежедневно", "weekly": "Еженедельно"}


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_habits(tg_user_id: str) -> List[dict]:
    web_uid = db.get_web_user_id(tg_user_id)
    if not web_uid:
        return []
    rows = db._query("""
        SELECT
            h.*,
            EXISTS(
                SELECT 1 FROM habit_logs hl
                WHERE hl.habit_id = h.id AND hl.logged_at = CURRENT_DATE
            ) AS done_today
        FROM habits h
        WHERE h.user_id = %s
        ORDER BY h.created_at ASC
    """, (web_uid,))
    result = []
    for row in rows:
        habit = dict(row)
        habit["streak"] = _calc_streak(habit["id"])
        result.append(habit)
    return result


def _calc_streak(habit_id: int) -> int:
    rows = db._query(
        "SELECT logged_at FROM habit_logs WHERE habit_id = %s ORDER BY logged_at DESC",
        (habit_id,)
    )
    if not rows:
        return 0

    logs = set()
    for r in rows:
        d = r["logged_at"]
        logs.add(d if isinstance(d, date) else date.fromisoformat(str(d)[:10]))

    today = date.today()
    yesterday = date.fromordinal(today.toordinal() - 1)

    if today not in logs and yesterday not in logs:
        return 0

    check = today if today in logs else yesterday
    streak = 0
    while check in logs:
        streak += 1
        check = date.fromordinal(check.toordinal() - 1)
    return streak


def _toggle_habit_log(tg_user_id: str, habit_id: int) -> bool:
    """Toggle today's log. Returns True if now done, False if un-done."""
    web_uid = db.get_web_user_id(tg_user_id)
    if not web_uid:
        return False
    today = date.today()
    existing = db._query(
        "SELECT id FROM habit_logs WHERE habit_id = %s AND logged_at = %s",
        (habit_id, today)
    )
    if existing:
        db._exec("DELETE FROM habit_logs WHERE habit_id = %s AND logged_at = %s", (habit_id, today))
        return False
    else:
        db._exec(
            "INSERT INTO habit_logs (habit_id, user_id, logged_at) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (habit_id, web_uid, today)
        )
        return True


def _create_habit(tg_user_id: str, name: str, frequency: str) -> Optional[dict]:
    web_uid = db.get_web_user_id(tg_user_id)
    if not web_uid:
        return None
    row = db._exec("""
        INSERT INTO habits (user_id, name, frequency)
        VALUES (%s, %s, %s) RETURNING *
    """, (web_uid, name, frequency))
    return dict(row) if row else None


def _delete_habit(tg_user_id: str, habit_id: int) -> bool:
    web_uid = db.get_web_user_id(tg_user_id)
    if not web_uid:
        return False
    db._exec("DELETE FROM habits WHERE id = %s AND user_id = %s", (habit_id, web_uid))
    return True


def _ensure_habits_table():
    """Create habits tables if they don't exist (safe to call multiple times)."""
    db._exec("""
        CREATE TABLE IF NOT EXISTS habits (
            id          SERIAL PRIMARY KEY,
            user_id     TEXT NOT NULL,
            name        TEXT NOT NULL,
            description TEXT,
            frequency   TEXT DEFAULT 'daily',
            color       TEXT DEFAULT '#8B5CF6',
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    db._exec("""
        CREATE TABLE IF NOT EXISTS habit_logs (
            id         SERIAL PRIMARY KEY,
            habit_id   INTEGER NOT NULL,
            user_id    TEXT NOT NULL,
            logged_at  DATE NOT NULL DEFAULT CURRENT_DATE,
            UNIQUE(habit_id, logged_at)
        )
    """)


# ── UI builders ───────────────────────────────────────────────────────────────

def _build_habits_text(habits: List[dict]) -> str:
    today_str = date.today().strftime("%d.%m.%Y")
    header = (
        f"🔥 <b>HABIT TRACKER</b>\n"
        f"{DIVIDER}\n"
        f"  📅 {today_str}\n"
        f"{DIVIDER}\n"
    )
    if not habits:
        return header + "\n  <i>У вас пока нет привычек.\n  Нажмите ➕ чтобы добавить первую.</i>\n\n" + DIVIDER

    lines = []
    for h in habits:
        done = h.get("done_today", False)
        streak = h.get("streak", 0)
        freq_icon = FREQ_ICONS.get(h.get("frequency", "daily"), "📅")
        name = h["name"]
        check = "✅" if done else "⬜"
        streak_txt = f"  🔥 <code>{streak}</code>" if streak > 0 else ""
        lines.append(f"  {check} <b>{name}</b>{streak_txt}\n      {freq_icon} {FREQ_LABELS.get(h.get('frequency','daily'), '')}")

    done_count = sum(1 for h in habits if h.get("done_today"))
    footer = (
        f"\n{DIVIDER}\n"
        f"  ✅ Выполнено сегодня: <code>{done_count}/{len(habits)}</code>"
    )
    return header + "\n\n".join(lines) + footer


def _build_habits_keyboard(habits: List[dict]) -> InlineKeyboardMarkup:
    rows = []
    for h in habits:
        done = h.get("done_today", False)
        streak = h.get("streak", 0)
        streak_txt = f" 🔥{streak}" if streak > 0 else ""
        toggle_icon = "✅" if done else "⬜"
        rows.append([
            InlineKeyboardButton(
                f"{toggle_icon} {h['name'][:22]}{streak_txt}",
                callback_data=f"hb_tog_{h['id']}"
            ),
            InlineKeyboardButton("🗑", callback_data=f"hb_del_{h['id']}"),
        ])
    rows.append([
        InlineKeyboardButton("➕ Новая привычка", callback_data="hb_new"),
        InlineKeyboardButton("🔄 Обновить",       callback_data="hb_refresh"),
    ])
    return InlineKeyboardMarkup(rows)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_habits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    _ensure_habits_table()
    habits = _get_habits(uid)
    await update.message.reply_text(
        _build_habits_text(habits),
        parse_mode="HTML",
        reply_markup=_build_habits_keyboard(habits),
    )
    return ConversationHandler.END


async def cb_habits_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(query.from_user.id)
    _ensure_habits_table()
    habits = _get_habits(uid)
    await query.edit_message_text(
        _build_habits_text(habits),
        parse_mode="HTML",
        reply_markup=_build_habits_keyboard(habits),
    )


async def cb_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = str(query.from_user.id)
    habit_id = int(query.data.split("_")[-1])
    _ensure_habits_table()

    done = _toggle_habit_log(uid, habit_id)
    await query.answer("✅ Отмечено!" if done else "↩️ Отменено")

    habits = _get_habits(uid)
    await query.edit_message_text(
        _build_habits_text(habits),
        parse_mode="HTML",
        reply_markup=_build_habits_keyboard(habits),
    )


async def cb_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    habit_id = int(query.data.split("_")[-1])
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Да, удалить", callback_data=f"hb_deld_{habit_id}"),
        InlineKeyboardButton("↩️ Отмена",       callback_data="hb_refresh"),
    ]])
    await query.edit_message_text(
        "🗑 <b>Удалить привычку?</b>\n\nЭто действие нельзя отменить.",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def cb_delete_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = str(query.from_user.id)
    habit_id = int(query.data.split("_")[-1])
    _ensure_habits_table()
    _delete_habit(uid, habit_id)
    await query.answer("🗑 Привычка удалена.")
    habits = _get_habits(uid)
    await query.edit_message_text(
        _build_habits_text(habits),
        parse_mode="HTML",
        reply_markup=_build_habits_keyboard(habits),
    )


# ── FSM: Create habit ─────────────────────────────────────────────────────────

async def cb_new_habit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Отмена", callback_data="hb_cnl")
    ]])
    await query.edit_message_text(
        f"🔥 <b>НОВАЯ ПРИВЫЧКА</b>\n{DIVIDER}\n\n"
        "  📝 Введите название привычки:\n\n"
        '  <i>Например: "Медитация", "Зарядка", "Чтение книги"</i>\n\n'
        f"{DIVIDER}",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return HB_WAIT_NAME


async def fsm_habit_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("⚠️ Введите название привычки:")
        return HB_WAIT_NAME
    if len(name) > 60:
        await update.message.reply_text("⚠️ Слишком длинное название (макс. 60 символов). Попробуйте снова:")
        return HB_WAIT_NAME

    context.user_data["new_habit_name"] = name
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Ежедневно",    callback_data="hb_freq_daily"),
            InlineKeyboardButton("📆 Еженедельно",  callback_data="hb_freq_weekly"),
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data="hb_cnl")],
    ])
    await update.message.reply_text(
        f"✅ Название: <b>{name}</b>\n\n"
        "  Выберите частоту выполнения:",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return HB_WAIT_FREQ


async def fsm_habit_freq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = str(query.from_user.id)
    freq = query.data.split("_")[-1]  # "daily" or "weekly"
    name = context.user_data.pop("new_habit_name", "")
    if not name:
        await query.answer("⚠️ Сессия истекла. Начните заново.", show_alert=True)
        return ConversationHandler.END

    _ensure_habits_table()
    habit = _create_habit(uid, name, freq)
    if not habit:
        await query.answer("⚠️ Аккаунт не привязан. Используйте /link <email>", show_alert=True)
        return ConversationHandler.END

    await query.answer(f"✅ Привычка «{name}» создана!")
    habits = _get_habits(uid)
    await query.edit_message_text(
        _build_habits_text(habits),
        parse_mode="HTML",
        reply_markup=_build_habits_keyboard(habits),
    )
    return ConversationHandler.END


async def fsm_habit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("❌ Отменено.")
    context.user_data.pop("new_habit_name", None)
    uid = str(query.from_user.id)
    habits = _get_habits(uid)
    await query.edit_message_text(
        _build_habits_text(habits),
        parse_mode="HTML",
        reply_markup=_build_habits_keyboard(habits),
    )
    return ConversationHandler.END


# ── Registration ──────────────────────────────────────────────────────────────

def build_habit_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("habits", cmd_habits),
            CallbackQueryHandler(cb_new_habit_start, pattern=r"^hb_new$"),
        ],
        states={
            HB_WAIT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, fsm_habit_name),
                CallbackQueryHandler(fsm_habit_cancel, pattern=r"^hb_cnl$"),
            ],
            HB_WAIT_FREQ: [
                CallbackQueryHandler(fsm_habit_freq, pattern=r"^hb_freq_(daily|weekly)$"),
                CallbackQueryHandler(fsm_habit_cancel, pattern=r"^hb_cnl$"),
            ],
        },
        fallbacks=[
            CommandHandler("habits", cmd_habits),
            CallbackQueryHandler(fsm_habit_cancel, pattern=r"^hb_cnl$"),
        ],
        per_message=False,
        allow_reentry=True,
    )


def build_habit_callbacks() -> list:
    return [
        CallbackQueryHandler(cb_habits_refresh,   pattern=r"^hb_refresh$"),
        CallbackQueryHandler(cb_toggle,            pattern=r"^hb_tog_\d+$"),
        CallbackQueryHandler(cb_delete_confirm,    pattern=r"^hb_del_\d+$"),
        CallbackQueryHandler(cb_delete_confirmed,  pattern=r"^hb_deld_\d+$"),
    ]
