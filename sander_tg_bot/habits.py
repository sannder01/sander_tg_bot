"""
habits.py — Chronicle Engine: Habit Tracker Module
===================================================
Commands:
  /habits    — список привычек с отметкой за сегодня
  + FSM через inline-кнопку ➕

Callback prefixes:
  hb_tog_{id}        — отметить/снять сегодня
  hb_del_{id}        — запрос подтверждения удаления
  hb_deld_{id}       — подтверждённое удаление
  hb_refresh         — обновить список
  hb_new             — начать FSM создания привычки
  hb_cnl             — отмена FSM
  hb_freq_daily      — выбрать «каждый день»
  hb_freq_custom     — выбрать «по дням»
  hb_day_{d}         — переключить день d (0=Пн…6=Вс) в FSM
  hb_days_done       — подтвердить выбор дней и создать привычку
"""

import json
import logging
from datetime import date, datetime
from typing import Optional, List

import db
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler, CommandHandler, ConversationHandler,
    ContextTypes, MessageHandler, filters,
)

logger = logging.getLogger(__name__)

# ── FSM States ────────────────────────────────────────────────────────────────
HB_WAIT_NAME, HB_WAIT_FREQ, HB_WAIT_DAYS = range(10, 13)

# ── Constants ─────────────────────────────────────────────────────────────────
DAY_LABELS      = ['Пн','Вт','Ср','Чт','Пт','Сб','Вс']
DAY_LABELS_FULL = ['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье']
ALL_DAYS        = list(range(7))
WORKDAYS        = list(range(5))
WEEKEND         = [5, 6]
DIVIDER         = "━━━━━━━━━━━━━━━━━━━━"


def _today_weekday() -> int:
    """Return 0-based weekday index (0=Пн, 6=Вс)."""
    return (date.today().weekday())  # Python weekday(): 0=Mon already


def _parse_days(habit: dict) -> List[int]:
    if habit.get('frequency') == 'daily':
        return ALL_DAYS
    raw = habit.get('days')
    if raw is None:
        return ALL_DAYS
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return ALL_DAYS


def _schedule_label(habit: dict) -> str:
    if habit.get('frequency') == 'daily':
        return '📅 Каждый день'
    days = _parse_days(habit)
    if not days or len(days) == 7:
        return '📅 Каждый день'
    if sorted(days) == WORKDAYS:
        return '💼 Будни (Пн–Пт)'
    if sorted(days) == WEEKEND:
        return '🛋 Выходные (Сб–Вс)'
    return '📆 ' + ', '.join(DAY_LABELS[d] for d in days)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _ensure_tables():
    db._exec("""
        CREATE TABLE IF NOT EXISTS habits (
            id          SERIAL PRIMARY KEY,
            user_id     TEXT NOT NULL,
            name        TEXT NOT NULL,
            description TEXT,
            frequency   TEXT DEFAULT 'daily',
            days        JSONB,
            color       TEXT DEFAULT '#8B5CF6',
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    db._exec("ALTER TABLE habits ADD COLUMN IF NOT EXISTS days JSONB")
    db._exec("""
        CREATE TABLE IF NOT EXISTS habit_logs (
            id         SERIAL PRIMARY KEY,
            habit_id   INTEGER NOT NULL,
            user_id    TEXT NOT NULL,
            logged_at  DATE NOT NULL DEFAULT CURRENT_DATE,
            UNIQUE(habit_id, logged_at)
        )
    """)


def _get_habits(tg_user_id: str) -> List[dict]:
    web_uid = db.get_web_user_id(tg_user_id)
    if not web_uid:
        return []
    rows = db._query("""
        SELECT h.*,
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
        h = dict(row)
        h['streak'] = _calc_streak(h['id'])
        result.append(h)
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
        d = r['logged_at']
        logs.add(d if isinstance(d, date) else date.fromisoformat(str(d)[:10]))
    today     = date.today()
    yesterday = date.fromordinal(today.toordinal() - 1)
    if today not in logs and yesterday not in logs:
        return 0
    check = today if today in logs else yesterday
    streak = 0
    while check in logs:
        streak += 1
        check = date.fromordinal(check.toordinal() - 1)
    return streak


def _toggle_log(tg_user_id: str, habit_id: int) -> bool:
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
    db._exec(
        "INSERT INTO habit_logs (habit_id, user_id, logged_at) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        (habit_id, web_uid, today)
    )
    return True


def _create_habit(tg_user_id: str, name: str, frequency: str, days: Optional[List[int]]) -> Optional[dict]:
    web_uid = db.get_web_user_id(tg_user_id)
    if not web_uid:
        return None
    days_json = json.dumps(days) if frequency == 'custom' and days else None
    row = db._exec("""
        INSERT INTO habits (user_id, name, frequency, days)
        VALUES (%s, %s, %s, %s) RETURNING *
    """, (web_uid, name, frequency, days_json))
    return dict(row) if row else None


def _delete_habit(tg_user_id: str, habit_id: int):
    web_uid = db.get_web_user_id(tg_user_id)
    if not web_uid:
        return
    db._exec("DELETE FROM habits WHERE id = %s AND user_id = %s", (habit_id, web_uid))


# ── UI builders ───────────────────────────────────────────────────────────────

def _build_text(habits: List[dict]) -> str:
    today_str = date.today().strftime('%d.%m.%Y')
    wd = _today_weekday()
    header = (
        f"🔥 <b>HABIT TRACKER</b>\n"
        f"{DIVIDER}\n"
        f"  📅 {today_str} ({DAY_LABELS_FULL[wd]})\n"
        f"{DIVIDER}\n"
    )
    if not habits:
        return header + "\n  <i>Нет привычек. Нажмите ➕</i>\n\n" + DIVIDER

    lines = []
    for h in habits:
        days = _parse_days(h)
        scheduled = wd in days
        done   = h.get('done_today', False)
        streak = h.get('streak', 0)
        check  = '✅' if done else ('⬜' if scheduled else '➖')
        streak_txt = f"  🔥<code>{streak}</code>" if streak > 0 else ''
        sub = _schedule_label(h)
        dim = '' if scheduled else ' <i>(не сегодня)</i>'
        lines.append(f"  {check} <b>{h['name']}</b>{streak_txt}{dim}\n      {sub}")

    done_count = sum(1 for h in habits if h.get('done_today'))
    today_count = sum(1 for h in habits if _today_weekday() in _parse_days(h))
    footer = (
        f"\n{DIVIDER}\n"
        f"  ✅ Сегодня: <code>{done_count}/{today_count}</code>"
    )
    return header + "\n\n".join(lines) + footer


def _build_keyboard(habits: List[dict]) -> InlineKeyboardMarkup:
    wd = _today_weekday()
    rows = []
    for h in habits:
        days = _parse_days(h)
        scheduled = wd in days
        done   = h.get('done_today', False)
        streak = h.get('streak', 0)
        streak_txt = f" 🔥{streak}" if streak > 0 else ''
        icon = '✅' if done else ('⬜' if scheduled else '➖')
        rows.append([
            InlineKeyboardButton(
                f"{icon} {h['name'][:22]}{streak_txt}",
                callback_data=f"hb_tog_{h['id']}"
            ),
            InlineKeyboardButton("🗑", callback_data=f"hb_del_{h['id']}"),
        ])
    rows.append([
        InlineKeyboardButton("➕ Новая привычка", callback_data="hb_new"),
        InlineKeyboardButton("🔄 Обновить",       callback_data="hb_refresh"),
    ])
    return InlineKeyboardMarkup(rows)


def _build_days_keyboard(selected: List[int]) -> InlineKeyboardMarkup:
    """Inline keyboard for day selection in FSM."""
    # Row 1: day toggles
    row1 = []
    for i, label in enumerate(DAY_LABELS):
        check = '✓' if i in selected else ' '
        row1.append(InlineKeyboardButton(f"{check}{label}", callback_data=f"hb_day_{i}"))

    # Row 2: presets
    row2 = [
        InlineKeyboardButton("Будни",    callback_data="hb_day_preset_work"),
        InlineKeyboardButton("Выходные", callback_data="hb_day_preset_wknd"),
        InlineKeyboardButton("Все дни",  callback_data="hb_day_preset_all"),
    ]

    # Row 3: confirm / cancel
    row3 = [
        InlineKeyboardButton("❌ Отмена",     callback_data="hb_cnl"),
        InlineKeyboardButton("✅ Создать",    callback_data="hb_days_done"),
    ]

    return InlineKeyboardMarkup([row1, row2, row3])


def _days_summary(days: List[int]) -> str:
    if not days:
        return "(нет дней)"
    if sorted(days) == ALL_DAYS:
        return "каждый день"
    if sorted(days) == WORKDAYS:
        return "будни (Пн–Пт)"
    if sorted(days) == WEEKEND:
        return "выходные (Сб–Вс)"
    return ', '.join(DAY_LABELS[d] for d in sorted(days))


# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_habits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    _ensure_tables()
    habits = _get_habits(uid)
    await update.message.reply_text(
        _build_text(habits), parse_mode="HTML",
        reply_markup=_build_keyboard(habits),
    )
    return ConversationHandler.END


async def cb_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    uid = str(query.from_user.id)
    _ensure_tables()
    habits = _get_habits(uid)
    await query.edit_message_text(
        _build_text(habits), parse_mode="HTML",
        reply_markup=_build_keyboard(habits),
    )


async def cb_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = str(q.from_user.id)
    habit_id = int(q.data.split('_')[-1])
    _ensure_tables()
    done = _toggle_log(uid, habit_id)
    await q.answer("✅ Отмечено!" if done else "↩️ Отменено")
    habits = _get_habits(uid)
    await q.edit_message_text(
        _build_text(habits), parse_mode="HTML",
        reply_markup=_build_keyboard(habits),
    )


async def cb_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    habit_id = int(q.data.split('_')[-1])
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Да, удалить", callback_data=f"hb_deld_{habit_id}"),
        InlineKeyboardButton("↩️ Отмена",       callback_data="hb_refresh"),
    ]])
    await q.edit_message_text(
        "🗑 <b>Удалить привычку?</b>\n\nЭто действие нельзя отменить.",
        parse_mode="HTML", reply_markup=kb,
    )


async def cb_delete_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = str(q.from_user.id)
    habit_id = int(q.data.split('_')[-1])
    _ensure_tables()
    _delete_habit(uid, habit_id)
    await q.answer("🗑 Удалено.")
    habits = _get_habits(uid)
    await q.edit_message_text(
        _build_text(habits), parse_mode="HTML",
        reply_markup=_build_keyboard(habits),
    )


# ── FSM: Create habit ─────────────────────────────────────────────────────────

async def cb_new_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="hb_cnl")]])
    await q.edit_message_text(
        f"🔥 <b>НОВАЯ ПРИВЫЧКА</b>\n{DIVIDER}\n\n"
        "  📝 Введите название:\n\n"
        "  <i>Например: Медитация, Зарядка, Чтение</i>\n\n"
        f"{DIVIDER}",
        parse_mode="HTML", reply_markup=kb,
    )
    return HB_WAIT_NAME


async def fsm_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or '').strip()
    if not name:
        await update.message.reply_text("⚠️ Введите название:"); return HB_WAIT_NAME
    if len(name) > 60:
        await update.message.reply_text("⚠️ Слишком длинное (макс. 60 символов):"); return HB_WAIT_NAME

    context.user_data['hb_name'] = name
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Каждый день",   callback_data="hb_freq_daily"),
            InlineKeyboardButton("📆 По дням",        callback_data="hb_freq_custom"),
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data="hb_cnl")],
    ])
    await update.message.reply_text(
        f"✅ Название: <b>{name}</b>\n\n  Выберите расписание:",
        parse_mode="HTML", reply_markup=kb,
    )
    return HB_WAIT_FREQ


async def fsm_freq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    freq = q.data.split('_')[-1]  # 'daily' or 'custom'

    if freq == 'daily':
        # Create immediately
        uid = str(q.from_user.id)
        name = context.user_data.pop('hb_name', '')
        if not name:
            await q.answer("⚠️ Сессия истекла.", show_alert=True)
            return ConversationHandler.END
        _ensure_tables()
        _create_habit(uid, name, 'daily', None)
        await q.answer(f"✅ Привычка «{name}» создана!")
        habits = _get_habits(uid)
        await q.edit_message_text(
            _build_text(habits), parse_mode="HTML",
            reply_markup=_build_keyboard(habits),
        )
        return ConversationHandler.END

    # custom → show day picker
    context.user_data['hb_days'] = list(range(5))  # default: weekdays
    name = context.user_data.get('hb_name', '?')
    await q.edit_message_text(
        f"✅ Название: <b>{name}</b>\n\n"
        f"  Выберите дни:\n"
        f"  <i>Выбрано: {_days_summary(context.user_data['hb_days'])}</i>",
        parse_mode="HTML",
        reply_markup=_build_days_keyboard(context.user_data['hb_days']),
    )
    return HB_WAIT_DAYS


async def fsm_day_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = q.data  # hb_day_0 … hb_day_6  or  hb_day_preset_*

    days: List[int] = context.user_data.get('hb_days', list(range(5)))

    if data.startswith('hb_day_preset_'):
        preset = data.split('_')[-1]
        if preset == 'work': days = WORKDAYS[:]
        elif preset == 'wknd': days = WEEKEND[:]
        else: days = ALL_DAYS[:]
    else:
        d = int(data.split('_')[-1])
        if d in days:
            if len(days) == 1: await q.answer("⚠️ Нужен хотя бы один день"); return HB_WAIT_DAYS
            days = [x for x in days if x != d]
        else:
            days = sorted(days + [d])

    context.user_data['hb_days'] = days
    name = context.user_data.get('hb_name', '?')
    await q.edit_message_text(
        f"✅ Название: <b>{name}</b>\n\n"
        f"  Выберите дни:\n"
        f"  <i>Выбрано: {_days_summary(days)}</i>",
        parse_mode="HTML",
        reply_markup=_build_days_keyboard(days),
    )
    return HB_WAIT_DAYS


async def fsm_days_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = str(q.from_user.id)
    name = context.user_data.pop('hb_name', '')
    days = context.user_data.pop('hb_days', list(range(5)))

    if not name:
        await q.answer("⚠️ Сессия истекла.", show_alert=True)
        return ConversationHandler.END

    _ensure_tables()
    _create_habit(uid, name, 'custom', days)
    await q.answer(f"✅ Привычка «{name}» создана ({_days_summary(days)})")
    habits = _get_habits(uid)
    await q.edit_message_text(
        _build_text(habits), parse_mode="HTML",
        reply_markup=_build_keyboard(habits),
    )
    return ConversationHandler.END


async def fsm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("❌ Отменено.")
    context.user_data.pop('hb_name', None)
    context.user_data.pop('hb_days', None)
    uid = str(q.from_user.id)
    habits = _get_habits(uid)
    await q.edit_message_text(
        _build_text(habits), parse_mode="HTML",
        reply_markup=_build_keyboard(habits),
    )
    return ConversationHandler.END


# ── Registration ──────────────────────────────────────────────────────────────

def build_habit_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("habits", cmd_habits),
            CallbackQueryHandler(cb_new_start, pattern=r'^hb_new$'),
        ],
        states={
            HB_WAIT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, fsm_name),
                CallbackQueryHandler(fsm_cancel, pattern=r'^hb_cnl$'),
            ],
            HB_WAIT_FREQ: [
                CallbackQueryHandler(fsm_freq,   pattern=r'^hb_freq_(daily|custom)$'),
                CallbackQueryHandler(fsm_cancel, pattern=r'^hb_cnl$'),
            ],
            HB_WAIT_DAYS: [
                CallbackQueryHandler(fsm_day_toggle, pattern=r'^hb_day_(\d|preset_\w+)$'),
                CallbackQueryHandler(fsm_days_done,  pattern=r'^hb_days_done$'),
                CallbackQueryHandler(fsm_cancel,     pattern=r'^hb_cnl$'),
            ],
        },
        fallbacks=[
            CommandHandler("habits", cmd_habits),
            CallbackQueryHandler(fsm_cancel, pattern=r'^hb_cnl$'),
        ],
        per_message=False,
        allow_reentry=True,
    )


def build_habit_callbacks() -> list:
    return [
        CallbackQueryHandler(cb_refresh,          pattern=r'^hb_refresh$'),
        CallbackQueryHandler(cb_toggle,            pattern=r'^hb_tog_\d+$'),
        CallbackQueryHandler(cb_delete_confirm,    pattern=r'^hb_del_\d+$'),
        CallbackQueryHandler(cb_delete_confirmed,  pattern=r'^hb_deld_\d+$'),
    ]
