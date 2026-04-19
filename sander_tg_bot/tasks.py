"""
tasks.py — Chronicle Engine: Task Manager v3
=============================================
Features:
  · Smart NLP task creation via Groq
  · Statuses: todo → in_progress → done
  · Priorities: high / medium / low
  · Deadline system with timezone support
  · Interactive month calendar view
  · Productivity analytics dashboard
  · Archive view for completed tasks
  · Auto-archive of done tasks
  · Proactive deadline reminder notifications

FSM States (ConversationHandler):
  WAIT_TEXT      — waiting for user to type task name/description
  WAIT_PRIORITY  — waiting for user to pick priority via button
"""

import calendar as cal_mod
import logging
from datetime import datetime, timedelta
from typing import Optional

import pytz
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import db
from nlp_parser import (
    format_deadline_local,
    parse_task_message,
    time_until,
)

logger = logging.getLogger(__name__)

# ── FSM States ────────────────────────────────────────────────────────────────
WAIT_TEXT, WAIT_PRIORITY = range(2)

# ── Constants ─────────────────────────────────────────────────────────────────
PAGE_SIZE  = 5
DIVIDER    = "━━━━━━━━━━━━━━━━━━━━"

STATUS_ICON  = {"todo": "🔲", "in_progress": "🔄", "done": "✅"}
STATUS_LABEL = {"todo": "To Do", "in_progress": "In Progress", "done": "Done"}
PRIORITY_ICON  = {"high": "🔴", "medium": "🟡", "low": "🟢"}
PRIORITY_LABEL = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}


# ═══════════════════════════════════════════════════════════════════════════════
#  UI BUILDERS
# ═══════════════════════════════════════════════════════════════════════════════

def _task_line(t: dict, idx: int, user_tz: str) -> str:
    si   = STATUS_ICON.get(t["status"], "🔲")
    pi   = PRIORITY_ICON.get(t["priority"], "🟡")
    title = t["text"]
    if t["status"] == "done":
        title = f"<s>{title}</s>"
    else:
        title = f"<b>{title}</b>"

    line = f"  {pi} {si}  {idx}. {title}"

    if t.get("deadline"):
        dl_str = format_deadline_local(t["deadline"], user_tz)
        eta    = time_until(t["deadline"])
        line += f"\n        📅 <code>{dl_str}</code>  <i>({eta})</i>"

    return line


def _build_list_text(tasks: list, page: int, user_tz: str) -> str:
    total = len(tasks)
    start = page * PAGE_SIZE
    end   = min(start + PAGE_SIZE, total)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    counts = {s: sum(1 for t in tasks if t["status"] == s) for s in STATUS_ICON}

    header = (
        "⚡️ <b>PROJECT: CHRONICLE</b>\n"
        f"{DIVIDER}\n"
        f"  🔲 <code>{counts['todo']}</code>  "
        f"🔄 <code>{counts['in_progress']}</code>  "
        f"✅ <code>{counts['done']}</code>\n"
        f"{DIVIDER}\n"
    )

    if not tasks:
        body = "\n  <i>⌁  No tasks yet. Press ➕ to begin.</i>\n\n"
    else:
        lines = [_task_line(t, start + i + 1, user_tz) for i, t in enumerate(tasks[start:end])]
        body = "\n\n".join(lines) + "\n\n"

    footer = (
        f"{DIVIDER}\n"
        f"  📄 Page <code>{page + 1}/{total_pages}</code>"
    )
    return header + body + footer


def _build_list_keyboard(tasks: list, page: int) -> InlineKeyboardMarkup:
    total = len(tasks)
    start = page * PAGE_SIZE
    end   = min(start + PAGE_SIZE, total)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    rows = []

    # Per-task "view" buttons
    task_row = []
    for i in range(start, end):
        t = tasks[i]
        icon = STATUS_ICON.get(t["status"], "🔲")
        task_row.append(
            InlineKeyboardButton(f"{icon} {i + 1}", callback_data=f"tm_vw_{t['id']}")
        )
        if len(task_row) == 3:
            rows.append(task_row)
            task_row = []
    if task_row:
        rows.append(task_row)

    # Nav row
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"tm_pg_{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"tm_pg_{page + 1}"))
    if nav:
        rows.append(nav)

    # Actions
    rows.append([
        InlineKeyboardButton("➕ Add Task", callback_data="task_add"),
        InlineKeyboardButton("📅 Calendar", callback_data="tm_cal_now"),
    ])
    rows.append([
        InlineKeyboardButton("📊 Analytics", callback_data="tm_ana"),
        InlineKeyboardButton("📦 Archive",   callback_data="tm_arc_0"),
    ])
    rows.append([
        InlineKeyboardButton("🔄 Refresh",   callback_data=f"tm_pg_{page}"),
        InlineKeyboardButton("🗂 Auto-Clean", callback_data="tm_autoarch"),
    ])
    return InlineKeyboardMarkup(rows)


def _build_detail_text(t: dict, user_tz: str) -> str:
    si = STATUS_ICON[t["status"]]
    pi = PRIORITY_ICON[t["priority"]]
    sl = STATUS_LABEL[t["status"]]
    pl = PRIORITY_LABEL[t["priority"]]

    lines = [
        f"⚡️ <b>TASK #{t['id']}</b>",
        DIVIDER,
        f"  📌 <b>{t['text']}</b>",
        "",
        f"  {pi}  Priority: <b>{pl}</b>",
        f"  {si}  Status:   <b>{sl}</b>",
    ]

    if t.get("deadline"):
        dl_str = format_deadline_local(t["deadline"], user_tz)
        eta    = time_until(t["deadline"])
        lines += [
            f"  📅  Deadline: <code>{dl_str}</code>",
            f"  ⏱  Remaining: <i>{eta}</i>",
        ]

    created = t.get("created_at", "")[:16].replace("T", " ")
    lines.append(f"\n  🕓  Created: <code>{created} UTC</code>")

    if t.get("completed_at"):
        done_at = t["completed_at"][:16].replace("T", " ")
        lines.append(f"  ✅  Completed: <code>{done_at} UTC</code>")

    lines.append(f"\n{DIVIDER}")
    return "\n".join(lines)


def _build_detail_keyboard(task_id: int, current_status: str, current_priority: str) -> InlineKeyboardMarkup:
    # Status row — highlight current
    status_row = []
    for s, icon in STATUS_ICON.items():
        label = f"·{icon}·" if s == current_status else icon
        status_row.append(InlineKeyboardButton(label, callback_data=f"tm_st_{task_id}_{s[0]}"))

    # Priority row
    priority_row = []
    for p, icon in PRIORITY_ICON.items():
        label = f"·{icon}·" if p == current_priority else icon
        priority_row.append(InlineKeyboardButton(label, callback_data=f"tm_pr_{task_id}_{p[0]}"))

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("— Status —", callback_data="tm_noop")],
        status_row,
        [InlineKeyboardButton("— Priority —", callback_data="tm_noop")],
        priority_row,
        [
            InlineKeyboardButton("🗑 Delete",  callback_data=f"tm_dl_{task_id}"),
            InlineKeyboardButton("📦 Archive", callback_data=f"tm_ar_{task_id}"),
        ],
        [InlineKeyboardButton("↩️ Back to List", callback_data="tm_pg_0")],
    ])


# ── Calendar ──────────────────────────────────────────────────────────────────

def _build_calendar_text(year: int, month: int) -> str:
    month_name = datetime(year, month, 1).strftime("%B %Y")
    return (
        f"📅 <b>{month_name.upper()}</b>\n"
        f"{DIVIDER}\n"
        "  🔴 high  🟡 medium  🟢 low  ✅ all done\n"
        f"{DIVIDER}"
    )


def _build_calendar_keyboard(user_id: str, year: int, month: int) -> InlineKeyboardMarkup:
    day_tasks = db.get_tasks_for_month(user_id, year, month)
    today = datetime.utcnow()

    rows = []
    # Weekday header
    rows.append([
        InlineKeyboardButton(d, callback_data="tm_noop")
        for d in ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
    ])

    _, num_days = cal_mod.monthrange(year, month)
    first_wd   = cal_mod.monthrange(year, month)[0]  # 0=Mon
    week_row   = [InlineKeyboardButton(" ", callback_data="tm_noop")] * first_wd

    for day in range(1, num_days + 1):
        tasks_today = day_tasks.get(day, [])
        if tasks_today:
            statuses   = {t["status"] for t in tasks_today}
            priorities = {t["priority"] for t in tasks_today}
            if statuses == {"done"}:
                emoji = "✅"
            elif "high" in priorities:
                emoji = "🔴"
            elif "medium" in priorities:
                emoji = "🟡"
            else:
                emoji = "🟢"
            label = f"{emoji}{day}"
        else:
            is_today = (day == today.day and month == today.month and year == today.year)
            label = f"[{day}]" if is_today else str(day)

        date_str = f"{year}{month:02d}{day:02d}"
        week_row.append(InlineKeyboardButton(label, callback_data=f"tm_day_{date_str}"))

        if len(week_row) == 7:
            rows.append(week_row)
            week_row = []

    if week_row:
        pad = 7 - len(week_row)
        week_row += [InlineKeyboardButton(" ", callback_data="tm_noop")] * pad
        rows.append(week_row)

    # Navigation
    prev_year, prev_month = (year, month - 1) if month > 1 else (year - 1, 12)
    next_year, next_month = (year, month + 1) if month < 12 else (year + 1, 1)
    rows.append([
        InlineKeyboardButton(f"◀️", callback_data=f"tm_cal_{prev_year}_{prev_month}"),
        InlineKeyboardButton(f"🗓 {year}-{month:02d}", callback_data="tm_noop"),
        InlineKeyboardButton(f"▶️", callback_data=f"tm_cal_{next_year}_{next_month}"),
    ])
    rows.append([InlineKeyboardButton("↩️ Back to List", callback_data="tm_pg_0")])
    return InlineKeyboardMarkup(rows)


def _build_day_text(date_str: str, tasks: list, user_tz: str) -> str:
    year  = int(date_str[:4])
    month = int(date_str[4:6])
    day   = int(date_str[6:8])
    date_label = datetime(year, month, day).strftime("%d %B %Y")

    header = (
        f"📅 <b>{date_label}</b>\n"
        f"{DIVIDER}\n"
    )
    if not tasks:
        return header + "  <i>No tasks with deadlines on this day.</i>\n" + DIVIDER

    lines = [header]
    for t in tasks:
        si   = STATUS_ICON.get(t["status"], "🔲")
        pi   = PRIORITY_ICON.get(t["priority"], "🟡")
        dl   = format_deadline_local(t["deadline"], user_tz) if t.get("deadline") else ""
        lines.append(f"  {pi} {si} <b>{t['text']}</b>\n      <code>{dl}</code>")

    lines.append(DIVIDER)
    return "\n".join(lines)


def _build_day_keyboard(date_str: str, tasks: list) -> InlineKeyboardMarkup:
    rows = []
    for t in tasks:
        rows.append([InlineKeyboardButton(
            f"{STATUS_ICON.get(t['status'],'🔲')} {t['text'][:28]}",
            callback_data=f"tm_vw_{t['id']}"
        )])
    year  = int(date_str[:4])
    month = int(date_str[4:6])
    rows.append([InlineKeyboardButton("↩️ Back to Calendar", callback_data=f"tm_cal_{year}_{month}")])
    return InlineKeyboardMarkup(rows)


# ── Analytics ─────────────────────────────────────────────────────────────────

def _build_analytics_text(stats: dict) -> str:
    tw = stats["done_this_week"]
    lw = stats["done_last_week"]
    delta = tw - lw
    trend = f"🔼 +{delta}" if delta > 0 else (f"🔽 {delta}" if delta < 0 else "➡️ same")

    bs = stats["by_status"]
    bp = stats["by_priority"]

    total_active = stats["total_active"]
    rate = 0
    if total_active:
        done_active = bs.get("done", 0)
        rate = round(done_active / total_active * 100)

    return (
        f"📊 <b>PRODUCTIVITY REPORT</b>\n"
        f"{DIVIDER}\n\n"
        f"  <b>This week:</b>   <code>{tw}</code> tasks done\n"
        f"  <b>Last week:</b>   <code>{lw}</code> tasks done\n"
        f"  <b>Trend:</b>       {trend}\n\n"
        f"{DIVIDER}\n"
        f"  <b>Active Tasks ({total_active}):</b>\n"
        f"  🔲 Todo:        <code>{bs.get('todo', 0)}</code>\n"
        f"  🔄 In Progress: <code>{bs.get('in_progress', 0)}</code>\n"
        f"  ✅ Done:        <code>{bs.get('done', 0)}</code>\n"
        f"  Completion rate: <b>{rate}%</b>\n\n"
        f"{DIVIDER}\n"
        f"  <b>By Priority:</b>\n"
        f"  🔴 High:   <code>{bp.get('high', 0)}</code>\n"
        f"  🟡 Medium: <code>{bp.get('medium', 0)}</code>\n"
        f"  🟢 Low:    <code>{bp.get('low', 0)}</code>\n\n"
        f"{DIVIDER}\n"
        f"  📦 Archived: <code>{stats['total_archived']}</code>  "
        f"  🗂 All-time:  <code>{stats['total_ever']}</code>\n"
        f"{DIVIDER}"
    )


# ── Archive ───────────────────────────────────────────────────────────────────

def _build_archive_text(tasks: list, page: int) -> str:
    total = len(tasks)
    start = page * PAGE_SIZE
    end   = min(start + PAGE_SIZE, total)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    header = (
        f"📦 <b>ARCHIVE</b>  ({total} tasks)\n"
        f"{DIVIDER}\n"
    )
    if not tasks:
        return header + "  <i>Archive is empty.</i>\n" + DIVIDER

    lines = []
    for t in tasks[start:end]:
        pi = PRIORITY_ICON.get(t["priority"], "🟡")
        completed = (t.get("completed_at") or "")[:10]
        lines.append(f"  {pi} ✅ <s>{t['text']}</s>  <code>{completed}</code>")

    footer = f"\n{DIVIDER}\n📄 Page <code>{page + 1}/{total_pages}</code>"
    return header + "\n".join(lines) + footer


def _build_archive_keyboard(tasks: list, page: int) -> InlineKeyboardMarkup:
    total = len(tasks)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    rows = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"tm_arc_{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"tm_arc_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("↩️ Back to List", callback_data="tm_pg_0")])
    return InlineKeyboardMarkup(rows)


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point /tasks — show task list."""
    uid   = str(update.effective_user.id)
    tz    = db.get_tz(uid)
    tasks = db.get_tasks(uid)
    text  = _build_list_text(tasks, 0, tz)
    kb    = _build_list_keyboard(tasks, 0)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
#  NAVIGATION CALLBACKS
# ═══════════════════════════════════════════════════════════════════════════════

async def cb_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid   = str(query.from_user.id)
    page  = int(query.data.split("_")[-1])
    tz    = db.get_tz(uid)
    tasks = db.get_tasks(uid)
    await query.edit_message_text(
        _build_list_text(tasks, page, tz),
        parse_mode="HTML",
        reply_markup=_build_list_keyboard(tasks, page),
    )


async def cb_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


# ═══════════════════════════════════════════════════════════════════════════════
#  TASK DETAIL CALLBACKS
# ═══════════════════════════════════════════════════════════════════════════════

async def cb_view_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid     = str(query.from_user.id)
    task_id = int(query.data.split("_")[-1])
    task    = db.get_task(task_id)

    if not task or task["user_id"] != uid:
        await query.answer("⚠️ Task not found.", show_alert=True)
        return

    tz = db.get_tz(uid)
    await query.edit_message_text(
        _build_detail_text(task, tz),
        parse_mode="HTML",
        reply_markup=_build_detail_keyboard(task_id, task["status"], task["priority"]),
    )


async def cb_set_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    uid     = str(query.from_user.id)
    parts   = query.data.split("_")   # tm_st_{id}_{s}
    task_id = int(parts[2])
    s_key   = parts[3]
    status_map = {"t": "todo", "i": "in_progress", "d": "done"}
    status  = status_map.get(s_key, "todo")

    task = db.get_task(task_id)
    if not task or task["user_id"] != uid:
        await query.answer("⚠️ Task not found.", show_alert=True)
        return

    db.set_status(task_id, status)
    task = db.get_task(task_id)  # reload

    label = STATUS_LABEL.get(status, status)
    await query.answer(f"{STATUS_ICON.get(status, '')} → {label}")

    tz = db.get_tz(uid)
    await query.edit_message_text(
        _build_detail_text(task, tz),
        parse_mode="HTML",
        reply_markup=_build_detail_keyboard(task_id, task["status"], task["priority"]),
    )


async def cb_set_priority(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    uid     = str(query.from_user.id)
    parts   = query.data.split("_")   # tm_pr_{id}_{p}
    task_id = int(parts[2])
    p_key   = parts[3]
    priority_map = {"h": "high", "m": "medium", "l": "low"}
    priority = priority_map.get(p_key, "medium")

    task = db.get_task(task_id)
    if not task or task["user_id"] != uid:
        await query.answer("⚠️ Task not found.", show_alert=True)
        return

    db.update_task(task_id, priority=priority)
    task = db.get_task(task_id)

    await query.answer(f"{PRIORITY_ICON.get(priority, '')} → {PRIORITY_LABEL.get(priority, '')}")
    tz = db.get_tz(uid)
    await query.edit_message_text(
        _build_detail_text(task, tz),
        parse_mode="HTML",
        reply_markup=_build_detail_keyboard(task_id, task["status"], task["priority"]),
    )


async def cb_delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    uid     = str(query.from_user.id)
    task_id = int(query.data.split("_")[-1])
    task    = db.get_task(task_id)

    if not task or task["user_id"] != uid:
        await query.answer("⚠️ Task not found.", show_alert=True)
        return

    title = task["text"][:25]
    db.delete_task(task_id)
    await query.answer(f"🗑 «{title}» deleted.")

    tz    = db.get_tz(uid)
    tasks = db.get_tasks(uid)
    await query.edit_message_text(
        _build_list_text(tasks, 0, tz),
        parse_mode="HTML",
        reply_markup=_build_list_keyboard(tasks, 0),
    )


async def cb_archive_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    uid     = str(query.from_user.id)
    task_id = int(query.data.split("_")[-1])
    task    = db.get_task(task_id)

    if not task or task["user_id"] != uid:
        await query.answer("⚠️ Task not found.", show_alert=True)
        return

    db.update_task(task_id, archived=1)
    await query.answer("📦 Archived.")

    tz    = db.get_tz(uid)
    tasks = db.get_tasks(uid)
    await query.edit_message_text(
        _build_list_text(tasks, 0, tz),
        parse_mode="HTML",
        reply_markup=_build_list_keyboard(tasks, 0),
    )


async def cb_auto_archive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid   = str(query.from_user.id)
    count = db.archive_done_tasks(uid)
    await query.answer(f"📦 {count} completed task(s) archived.", show_alert=True)

    tz    = db.get_tz(uid)
    tasks = db.get_tasks(uid)
    await query.edit_message_text(
        _build_list_text(tasks, 0, tz),
        parse_mode="HTML",
        reply_markup=_build_list_keyboard(tasks, 0),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  CALENDAR CALLBACKS
# ═══════════════════════════════════════════════════════════════════════════════

async def cb_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = str(query.from_user.id)
    data = query.data  # tm_cal_now  OR  tm_cal_{year}_{month}

    if data == "tm_cal_now":
        now = datetime.utcnow()
        year, month = now.year, now.month
    else:
        _, _, y, m = data.split("_")
        year, month = int(y), int(m)

    await query.edit_message_text(
        _build_calendar_text(year, month),
        parse_mode="HTML",
        reply_markup=_build_calendar_keyboard(uid, year, month),
    )


async def cb_day_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    uid      = str(query.from_user.id)
    tz_str   = db.get_tz(uid)
    date_str = query.data.split("_")[-1]   # YYYYMMDD

    year  = int(date_str[:4])
    month = int(date_str[4:6])
    day   = int(date_str[6:8])

    day_tasks_map = db.get_tasks_for_month(uid, year, month)
    tasks_today   = day_tasks_map.get(day, [])

    await query.edit_message_text(
        _build_day_text(date_str, tasks_today, tz_str),
        parse_mode="HTML",
        reply_markup=_build_day_keyboard(date_str, tasks_today),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  ANALYTICS CALLBACK
# ═══════════════════════════════════════════════════════════════════════════════

async def cb_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid   = str(query.from_user.id)
    stats = db.get_analytics(uid)
    kb    = InlineKeyboardMarkup([[
        InlineKeyboardButton("↩️ Back to List", callback_data="tm_pg_0")
    ]])
    await query.edit_message_text(
        _build_analytics_text(stats),
        parse_mode="HTML",
        reply_markup=kb,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  ARCHIVE CALLBACK
# ═══════════════════════════════════════════════════════════════════════════════

async def cb_archive_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = str(query.from_user.id)
    page = int(query.data.split("_")[-1])

    tasks = db.get_tasks(uid, archived=True)
    await query.edit_message_text(
        _build_archive_text(tasks, page),
        parse_mode="HTML",
        reply_markup=_build_archive_keyboard(tasks, page),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  FSM: ADD TASK FLOW
# ═══════════════════════════════════════════════════════════════════════════════

async def cb_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """FSM entry: user pressed ➕ Add Task."""
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data="tm_cnl")
    ]])
    await query.edit_message_text(
        f"⚡️ <b>NEW TASK</b>\n{DIVIDER}\n\n"
        "  📝 <b>Describe your task naturally:</b>\n\n"
        '  <i>e.g. "Submit report tomorrow at 3pm"</i>\n'
        '  <i>e.g. "URGENT: fix login bug asap"</i>\n'
        '  <i>e.g. "Buy groceries"</i>\n\n'
        "  ✨ <i>AI will extract deadline & priority automatically</i>\n\n"
        f"{DIVIDER}",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return WAIT_TEXT


async def fsm_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """FSM: parse user text with NLP, then ask for priority confirmation."""
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("⚠️ Please type something. Try again:")
        return WAIT_TEXT

    uid       = str(update.effective_user.id)
    tz_str    = db.get_tz(uid)
    groq_cli  = context.bot_data.get("groq_client")

    parsed = parse_task_message(text, tz_str, groq_cli)
    context.user_data["parsed_task"] = parsed

    # Build preview
    dl_display = (
        f"\n  📅  Deadline: <code>{format_deadline_local(parsed['deadline'], tz_str)}</code>"
        f"  <i>({time_until(parsed['deadline'])})</i>"
        if parsed.get("deadline") else "\n  📅  Deadline: <i>none</i>"
    )
    pi = PRIORITY_ICON.get(parsed["priority"], "🟡")
    pl = PRIORITY_LABEL.get(parsed["priority"], "MEDIUM")

    preview = (
        f"⚡️ <b>TASK PREVIEW</b>\n{DIVIDER}\n\n"
        f"  📌 <b>{parsed['title']}</b>\n"
        f"{dl_display}\n"
        f"  {pi}  Priority: <b>{pl}</b>\n\n"
        f"{DIVIDER}\n"
        "  Adjust priority if needed, then confirm:"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 HIGH",   callback_data="tm_spr_h"),
            InlineKeyboardButton("🟡 MEDIUM", callback_data="tm_spr_m"),
            InlineKeyboardButton("🟢 LOW",    callback_data="tm_spr_l"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="tm_cnl")],
    ])
    await update.message.reply_text(preview, parse_mode="HTML", reply_markup=kb)
    return WAIT_PRIORITY


async def fsm_select_priority(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """FSM: user picked a priority → save task."""
    query    = update.callback_query
    uid      = str(query.from_user.id)
    p_key    = query.data.split("_")[-1]   # h / m / l
    pmap     = {"h": "high", "m": "medium", "l": "low"}
    priority = pmap.get(p_key, "medium")

    parsed = context.user_data.pop("parsed_task", {})
    if not parsed:
        await query.answer("⚠️ Session expired. Please start again.", show_alert=True)
        return ConversationHandler.END

    parsed["priority"] = priority
    task_id = db.add_task(
        user_id      = uid,
        text         = parsed["title"],
        priority     = priority,
        deadline_utc = parsed.get("deadline"),
    )
    await query.answer(f"✅ Task #{task_id} created!", show_alert=False)

    tz    = db.get_tz(uid)
    tasks = db.get_tasks(uid)
    page  = max(0, (len(tasks) - 1) // PAGE_SIZE)
    await query.edit_message_text(
        _build_list_text(tasks, page, tz),
        parse_mode="HTML",
        reply_markup=_build_list_keyboard(tasks, page),
    )
    return ConversationHandler.END


async def fsm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """FSM: cancel add task flow → back to list."""
    query = update.callback_query
    await query.answer("❌ Cancelled.")
    context.user_data.pop("parsed_task", None)
    uid   = str(query.from_user.id)
    tz    = db.get_tz(uid)
    tasks = db.get_tasks(uid)
    await query.edit_message_text(
        _build_list_text(tasks, 0, tz),
        parse_mode="HTML",
        reply_markup=_build_list_keyboard(tasks, 0),
    )
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
#  REMINDER JOB (runs every 60 s via bot job_queue)
# ═══════════════════════════════════════════════════════════════════════════════

_REMIND_LABELS = {
    "reminder_15m": ("⏰", "15 minutes"),
    "reminder_1h":  ("🔔", "1 hour"),
    "reminder_24h": ("📅", "24 hours"),
}


async def reminder_check_job(context: ContextTypes.DEFAULT_TYPE):
    """Periodically check for approaching deadlines and send push notifications."""
    pending = db.get_tasks_needing_reminders()
    for flag, task in pending:
        try:
            emoji, label = _REMIND_LABELS.get(flag, ("⏰", "soon"))
            pi    = PRIORITY_ICON.get(task["priority"], "🟡")
            title = task["text"][:50]
            msg   = (
                f"{emoji} <b>DEADLINE REMINDER</b>\n"
                f"{DIVIDER}\n\n"
                f"  {pi} <b>{title}</b>\n\n"
                f"  ⏱ Due in <b>{label}</b>!\n"
                f"  📅 <code>{task.get('deadline','')[:16].replace('T',' ')} UTC</code>\n\n"
                f"{DIVIDER}\n"
                f"  Tap /tasks to manage it."
            )
            await context.bot.send_message(
                chat_id   = task["user_id"],
                text      = msg,
                parse_mode="HTML",
            )
            db.mark_reminder_sent(task["id"], flag)
            logger.info("Reminder [%s] sent for task %d to user %s", flag, task["id"], task["user_id"])
        except Exception as e:
            logger.error("Failed to send reminder for task %d: %s", task["id"], e)


# ═══════════════════════════════════════════════════════════════════════════════
#  DAILY BRIEFING JOB
# ═══════════════════════════════════════════════════════════════════════════════

async def daily_briefing_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Send each user their morning task briefing.
    Iterates over all users who have briefing enabled.
    Called from bot.py via job_queue.run_daily().
    """
    import sqlite3
    with db._conn() as con:
        users = con.execute(
            "SELECT user_id, timezone FROM user_settings WHERE briefing_enabled=1"
        ).fetchall()

    for row in users:
        uid, tz_str = row["user_id"], row["timezone"]
        try:
            tz  = pytz.timezone(tz_str)
            now = datetime.now(tz)

            tasks      = db.get_tasks(uid)
            active     = [t for t in tasks if t["status"] != "done"]
            today_tasks = []
            overdue     = []

            for t in active:
                if not t.get("deadline"):
                    continue
                try:
                    dt_utc  = datetime.fromisoformat(t["deadline"]).replace(tzinfo=pytz.utc)
                    dt_local = dt_utc.astimezone(tz)
                    if dt_local.date() == now.date():
                        today_tasks.append(t)
                    elif dt_local < now:
                        overdue.append(t)
                except Exception:
                    pass

            if not active and not today_tasks and not overdue:
                continue  # Nothing to report

            lines = [
                f"☀️ <b>MORNING BRIEFING — {now.strftime('%d %B')}</b>",
                DIVIDER,
            ]

            if overdue:
                lines.append(f"\n⚠️ <b>OVERDUE ({len(overdue)}):</b>")
                for t in overdue[:5]:
                    pi = PRIORITY_ICON.get(t["priority"], "🟡")
                    lines.append(f"  {pi} <s>{t['text'][:40]}</s>")

            if today_tasks:
                lines.append(f"\n📅 <b>DUE TODAY ({len(today_tasks)}):</b>")
                for t in today_tasks:
                    pi  = PRIORITY_ICON.get(t["priority"], "🟡")
                    si  = STATUS_ICON.get(t["status"], "🔲")
                    dl  = format_deadline_local(t["deadline"], tz_str)
                    lines.append(f"  {pi} {si} <b>{t['text'][:35]}</b>  <code>{dl[-5:]}</code>")

            lines.append(f"\n🔲 <b>Active tasks:</b> <code>{len(active)}</code>")
            lines.append(DIVIDER)
            lines.append("  Tap /tasks to manage your day.")

            await context.bot.send_message(
                chat_id   = uid,
                text      = "\n".join(lines),
                parse_mode="HTML",
            )
            logger.info("Daily briefing sent to %s (%d today, %d overdue)", uid, len(today_tasks), len(overdue))

        except Exception as e:
            logger.error("Briefing failed for user %s: %s", uid, e)


# ═══════════════════════════════════════════════════════════════════════════════
#  REGISTRATION
# ═══════════════════════════════════════════════════════════════════════════════

def build_task_conversation() -> ConversationHandler:
    """
    State machine for adding a new task.

    /tasks or ➕ button
        │
        ▼
     WAIT_TEXT ──── user types description ──►  WAIT_PRIORITY
        │                                             │
     ❌ Cancel                            priority btn / ❌ Cancel
        │                                             │
        ▼                                             ▼
      END (list)                               task saved → END (list)
    """
    return ConversationHandler(
        entry_points=[
            CommandHandler("tasks", cmd_tasks),
            CallbackQueryHandler(cb_add_start, pattern=r"^task_add$"),
        ],
        states={
            WAIT_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, fsm_receive_text),
                CallbackQueryHandler(fsm_cancel, pattern=r"^tm_cnl$"),
            ],
            WAIT_PRIORITY: [
                CallbackQueryHandler(fsm_select_priority, pattern=r"^tm_spr_[hml]$"),
                CallbackQueryHandler(fsm_cancel,          pattern=r"^tm_cnl$"),
            ],
        },
        fallbacks=[
            CommandHandler("tasks", cmd_tasks),
            CallbackQueryHandler(fsm_cancel, pattern=r"^tm_cnl$"),
        ],
        per_message=False,
        allow_reentry=True,
    )


def build_task_callbacks() -> list:
    """All standalone CallbackQueryHandlers (registered outside ConversationHandler)."""
    return [
        # Navigation
        CallbackQueryHandler(cb_page,         pattern=r"^tm_pg_\d+$"),
        CallbackQueryHandler(cb_noop,         pattern=r"^tm_noop$"),

        # Task detail
        CallbackQueryHandler(cb_view_task,    pattern=r"^tm_vw_\d+$"),
        CallbackQueryHandler(cb_set_status,   pattern=r"^tm_st_\d+_[tid]$"),
        CallbackQueryHandler(cb_set_priority, pattern=r"^tm_pr_\d+_[hml]$"),
        CallbackQueryHandler(cb_delete_task,  pattern=r"^tm_dl_\d+$"),
        CallbackQueryHandler(cb_archive_task, pattern=r"^tm_ar_\d+$"),
        CallbackQueryHandler(cb_auto_archive, pattern=r"^tm_autoarch$"),

        # Calendar
        CallbackQueryHandler(cb_calendar,     pattern=r"^tm_cal_"),
        CallbackQueryHandler(cb_day_view,     pattern=r"^tm_day_\d{8}$"),

        # Views
        CallbackQueryHandler(cb_analytics,    pattern=r"^tm_ana$"),
        CallbackQueryHandler(cb_archive_view, pattern=r"^tm_arc_\d+$"),
    ]
