"""
bot.py — Chronicle Engine v3.0
================================
Telegram bot with:
  · Advanced Task Manager (NLP, calendar, analytics, reminders)
  · AITU LMS Deadline fetcher
  · Groq AI chat + business auto-reply

Run:
  python bot.py
"""

import os
import re
import sys
import logging
import pytz
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError

from groq import Groq
from dotenv import load_dotenv
from icalendar import Calendar
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
    TypeHandler,
)

import db
from tasks import (
    build_task_conversation,
    build_task_callbacks,
    daily_briefing_job,
    reminder_check_job,
)

# ─── Conversation states ───────────────────────────────────────────────────────
WAITING_ICAL_URL = 1

# ─── Bootstrap ────────────────────────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    format="%(asctime)s  %(name)-20s  %(levelname)s  %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Groq ──────────────────────────────────────────────────────────────────────
GROQ_KEY = os.getenv("GROQ_API_KEY")
groq_client = Groq(api_key=GROQ_KEY) if GROQ_KEY else None

# Use a mutable container so cmd_persona can mutate without `global`
_persona = {
    "prompt": os.getenv(
        "BOT_PERSONA",
        "Ты отвечаешь вместо владельца этого Telegram аккаунта. "
        "Отвечай вежливо, коротко и по делу. "
        "Если не знаешь ответа — скажи что владелец скоро ответит лично. "
        "Отвечай на языке собеседника.",
    )
}

# ─── AITU Deadline config ──────────────────────────────────────────────────────
ICAL_URL = os.getenv(
    "ICAL_URL",
    "https://lms.astanait.edu.kz/calendar/export_execute.php"
    "?userid=17634&authtoken=3f6f62339ece52c531c9dbffe568d0eacd33444f"
    "&preset_what=courses&preset_time=recentupcoming",
)
DEADLINE_CHAT_ID = os.getenv("DEADLINE_CHAT_ID", "")
DEADLINE_HOUR    = int(os.getenv("DEADLINE_HOUR",   "8"))
DEADLINE_MINUTE  = int(os.getenv("DEADLINE_MINUTE", "0"))
DEADLINE_TZ      = os.getenv("DEADLINE_TZ", "Asia/Almaty")
DAYS_AHEAD       = int(os.getenv("DAYS_AHEAD", "7"))

# ─── In-memory bot-level state ────────────────────────────────────────────────
_ai_history:  dict[str, list] = {}
_biz_history: dict[str, list] = {}
_ai_enabled:  dict[str, bool] = {}
_ical_urls:   dict[str, str]  = {}


def _get_ai_history(key: str) -> list:
    return _ai_history.get(key, [])

def _set_ai_history(key: str, msgs: list) -> None:
    _ai_history[key] = msgs

def _get_biz_history(chat_id: str) -> list:
    return _biz_history.get(chat_id, [])

def _set_biz_history(chat_id: str, msgs: list) -> None:
    _biz_history[chat_id] = msgs

def _get_ai_enabled(chat_id: str) -> bool:
    return _ai_enabled.get(chat_id, False)

def _set_ai_enabled(chat_id: str, value: bool) -> None:
    _ai_enabled[chat_id] = value

def _get_ical_url(user_id: str) -> str:
    return _ical_urls.get(user_id, "")

def _set_ical_url(user_id: str, url: str) -> None:
    _ical_urls[user_id] = url


# ═══════════════════════════════════════════════════════════════════════════════
#  AITU LMS DEADLINES
# ═══════════════════════════════════════════════════════════════════════════════

def get_chat_key(update: Update) -> str:
    if update.effective_chat:
        return f"chat_{update.effective_chat.id}"
    return f"user_{update.effective_user.id}"


def _escape_md(text: str) -> str:
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, "\\" + ch)
    return text


def _parse_course(component) -> str:
    cats = str(component.get("CATEGORIES", ""))
    if cats and cats not in ("None", ""):
        return cats.strip()
    desc  = str(component.get("DESCRIPTION", ""))
    match = re.search(r"Course[:\s]+(.+)", desc, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    summ  = str(component.get("SUMMARY", ""))
    match = re.search(r"\((.+?)\)\s*$", summ)
    if match:
        return match.group(1).strip()
    return "Unknown"


def fetch_deadlines(ical_url: str | None = None) -> list:
    url = ical_url or ICAL_URL
    req = Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    })
    # BUG FIX: wrapped in try/except with proper URLError handling
    try:
        with urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except URLError as e:
        raise RuntimeError(f"Network error fetching calendar: {e.reason}") from e

    tz_obj = pytz.timezone(DEADLINE_TZ)
    cal    = Calendar.from_ical(raw)
    now    = datetime.now(tz_obj)
    limit  = now + timedelta(days=DAYS_AHEAD)
    events = []

    for comp in cal.walk():
        if comp.name != "VEVENT":
            continue
        dtstart = comp.get("DTSTART")
        if dtstart is None:
            continue
        dt = dtstart.dt
        if not isinstance(dt, datetime):
            dt = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
        if dt.tzinfo is None:
            dt = tz_obj.localize(dt)
        else:
            dt = dt.astimezone(tz_obj)
        if now <= dt <= limit:
            events.append({
                "title":  str(comp.get("SUMMARY", "(no title)")),
                "course": _parse_course(comp),
                "dt":     dt,
                "url":    str(comp.get("URL", "")),
            })

    events.sort(key=lambda e: e["dt"])
    return events


def _build_deadline_msg(events: list) -> str:
    tz_obj  = pytz.timezone(DEADLINE_TZ)
    now     = datetime.now(tz_obj)
    now_str = _escape_md(now.strftime("%d.%m.%Y %H:%M"))

    if not events:
        return (
            "✅ *No deadlines\\!*\n"
            f"Nothing in the next {DAYS_AHEAD} days\\.\n"
            f"_Updated: {now_str}_"
        )

    lines = [
        f"📚 *AITU Deadlines — next {DAYS_AHEAD} days*",
        f"_Updated: {now_str}_\n",
    ]
    for e in events:
        delta = e["dt"] - now
        days  = delta.days
        hours = delta.seconds // 3600
        if days == 0:
            left = f"⚠️ today, in {hours}h"
        elif days == 1:
            left = "🔶 tomorrow"
        elif days <= 3:
            left = f"🟡 in {days} d\\."
        else:
            left = f"🟢 in {days} d\\."

        date_str = _escape_md(e["dt"].strftime("%d.%m %H:%M"))
        title    = _escape_md(e["title"])
        course   = _escape_md(e["course"])
        line     = f"{left} — *{title}*\n    📖 {course}\n    📅 {date_str}"
        if e["url"] and e["url"] != "None":
            line += f"\n    🔗 [Open]({e['url']})"
        lines.append(line)

    return "\n\n".join(lines)


async def cmd_deadlines(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    user_ical_url = _get_ical_url(uid)

    # BUG FIX: _get_ical_url now returns "" instead of ICAL_URL when not set,
    # so this check correctly falls through to the default ICAL_URL in fetch_deadlines
    if not user_ical_url and not ICAL_URL:
        return await update.message.reply_text(
            "❌ No calendar linked\\.\n\n"
            "Use /add\\_deadline to add your iCal URL\\.",
            parse_mode="MarkdownV2",
        )

    msg = await update.message.reply_text("⏳ Loading deadlines…")
    try:
        events = fetch_deadlines(user_ical_url or None)
        text = _build_deadline_msg(events)
        if not user_ical_url:
            text += "\n_\\(using default AITU calendar\\)_"
        await msg.edit_text(
            text,
            parse_mode="MarkdownV2",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error("Deadline fetch error: %s", e)
        await msg.edit_text(
            f"❌ Failed to load deadlines:\n<code>{e}</code>",
            parse_mode="HTML",
        )


async def cmd_add_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start conversation: ask user to send their iCal URL."""
    uid = str(update.effective_user.id)
    existing = _get_ical_url(uid)
    hint = ""
    if existing:
        short = existing[:60] + "…" if len(existing) > 60 else existing
        hint = f"\n\n_Current URL:_ `{_escape_md(short)}`"

    await update.message.reply_text(
        "📅 *Add your calendar*\n\n"
        "Send your iCal \\(`.ics`\\) URL\\.\n\n"
        "In Moodle/LMS: *Calendar → Export → copy the link*\\."
        + hint
        + "\n\nSend /cancel to abort\\.",
        parse_mode="MarkdownV2",
    )
    return WAITING_ICAL_URL


async def receive_ical_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save the iCal URL sent by the user and validate it."""
    uid = str(update.effective_user.id)
    url = update.message.text.strip()

    if not (url.startswith("http://") or url.startswith("https://")):
        await update.message.reply_text(
            "❌ That doesn't look like a valid URL\\.\n"
            "Please send a link starting with `https://`\\.\n\n"
            "Try again or /cancel\\.",
            parse_mode="MarkdownV2",
        )
        return WAITING_ICAL_URL

    checking = await update.message.reply_text("⏳ Checking calendar…")
    try:
        events = fetch_deadlines(url)
        _set_ical_url(uid, url)
        count = len(events)
        await checking.edit_text(
            f"✅ *Calendar saved\\!*\n\n"
            f"Found *{count}* upcoming deadline{'s' if count != 1 else ''}\\.\n"
            f"Use /deadlines to view them\\.",
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        logger.error("iCal validation error: %s", e)
        await checking.edit_text(
            f"❌ Could not load that calendar:\n<code>{e}</code>\n\n"
            "Double-check the URL and try again, or /cancel\\.",
            parse_mode="HTML",
        )
        return WAITING_ICAL_URL

    return ConversationHandler.END


async def cmd_cancel_ical(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled\\.", parse_mode="MarkdownV2")
    return ConversationHandler.END


async def daily_deadlines_job(context: ContextTypes.DEFAULT_TYPE):
    if not DEADLINE_CHAT_ID:
        return
    try:
        events = fetch_deadlines()
        await context.bot.send_message(
            chat_id    = DEADLINE_CHAT_ID,
            text       = _build_deadline_msg(events),
            parse_mode = "MarkdownV2",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error("daily_deadlines_job error: %s", e)


# ═══════════════════════════════════════════════════════════════════════════════
#  BUSINESS AUTO-REPLY
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.business_message
    if not message or not message.text:
        return
    if message.from_user and message.from_user.is_bot:
        return

    chat_id     = str(message.chat.id)
    user_text   = message.text.strip()
    sender_name = (message.from_user.first_name or "User") if message.from_user else "User"

    # ── Command detection ──
    if user_text.startswith("/"):
        cmd  = user_text.split()[0].lstrip("/").split("@")[0].lower()
        args = user_text.split()[1:]
        user_msg = " ".join(args)
        reply = ""

        if cmd == "ai":
            if not groq_client:
                reply = "⚠️ No GROQ_API_KEY configured."
            elif not user_msg:
                reply = "✏️ /ai <your question>"
            else:
                try:
                    resp  = groq_client.chat.completions.create(
                        model="llama-3.3-70b-versatile",
                        messages=[
                            {"role": "system", "content": "Ты дружелюбный ассистент. Отвечай коротко с юмором. Отвечай на языке пользователя."},
                            {"role": "user",   "content": user_msg},
                        ],
                        max_tokens=500,
                    )
                    reply = "🤖 " + resp.choices[0].message.content
                except Exception:
                    reply = "❌ AI unavailable. Try later."

        elif cmd == "stop":
            _set_ai_enabled(chat_id, False)
            reply = "🔇 AI auto-reply disabled.\nSend /resume to re-enable."

        elif cmd == "resume":
            _set_ai_enabled(chat_id, True)
            reply = "🔊 AI auto-reply enabled!"

        elif cmd == "help":
            reply = (
                "📋 <b>Commands:</b>\n\n"
                "/deadlines — AITU LMS deadlines\n"
                "/ai &lt;question&gt; — ask AI\n"
                "/stop — disable AI auto-reply\n"
                "/resume — enable AI auto-reply"
            )
        else:
            reply = "❓ Unknown command. Try /help"

        try:
            await context.bot.send_message(
                chat_id=message.chat.id,
                text=reply,
                parse_mode="HTML",
                business_connection_id=message.business_connection_id,
            )
        except Exception as e:
            logger.error("Business command reply error: %s", e)
        return

    # ── Auto-reply ──
    if not _get_ai_enabled(chat_id):
        return

    # BUG FIX: guard against groq_client being None before calling it
    if not groq_client:
        return

    hist = _get_biz_history(chat_id)
    hist.append({"role": "user", "content": f"{sender_name}: {user_text}"})
    if len(hist) > 10:
        hist = hist[-10:]

    try:
        resp  = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": _persona["prompt"]}, *hist],
            max_tokens=300,
        )
        reply = resp.choices[0].message.content
        hist.append({"role": "assistant", "content": reply})
        _set_biz_history(chat_id, hist)
        await context.bot.send_message(
            chat_id=message.chat.id,
            text=reply,
            business_connection_id=message.business_connection_id,
        )
    except Exception as e:
        logger.error("Business auto-reply error: %s", e)


# ═══════════════════════════════════════════════════════════════════════════════
#  CORE COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ai_status = "✅ Groq (Llama 3)" if groq_client else "❌ No GROQ_API_KEY"
    await update.message.reply_text(
        "⚡️ <b>CHRONICLE ENGINE</b>  <code>v3.0</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  🤖  AI Engine:  {ai_status}\n\n"
        "  <b>Modules:</b>\n"
        "  🔹  /tasks      — <i>Task Manager</i>\n"
        "         ↳ NLP add · Calendar · Analytics\n"
        "         ↳ Reminders · Archive\n"
        "  🔹  /deadlines    — <i>Your calendar deadlines</i>\n"
        "  🔹  /add_deadline — <i>Link your iCal calendar</i>\n"
        "  🔹  /ai         — <i>AI Assistant</i>\n"
        "  🔹  /tz         — <i>Set your timezone</i>\n"
        "  🔹  /briefing   — <i>Toggle morning briefing</i>\n"
        "  🔹  /persona    — <i>Set AI auto-reply style</i>\n"
        "  🔹  /status     — <i>System status</i>\n"
        "  🔹  /reset      — <i>Reset AI history</i>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  Start with /tasks to manage your day.",
        parse_mode="HTML",
    )


async def cmd_set_tz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set user timezone: /tz Asia/Almaty"""
    if not context.args:
        uid = str(update.effective_user.id)
        current = db.get_tz(uid)
        return await update.message.reply_text(
            f"🕐 Your timezone: <code>{current}</code>\n\n"
            "To change:\n<code>/tz Asia/Almaty</code>\n<code>/tz Europe/Moscow</code>\n<code>/tz UTC</code>",
            parse_mode="HTML",
        )
    tz_name = context.args[0]
    try:
        pytz.timezone(tz_name)  # validate
    except pytz.exceptions.UnknownTimeZoneError:
        return await update.message.reply_text(
            f"❌ Unknown timezone: <code>{tz_name}</code>\n"
            "See: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones",
            parse_mode="HTML",
        )
    uid = str(update.effective_user.id)
    db.update_settings(uid, timezone=tz_name)
    now_local = datetime.now(pytz.timezone(tz_name)).strftime("%H:%M %Z")
    await update.message.reply_text(
        f"✅ Timezone set to <code>{tz_name}</code>\n"
        f"Current time: <code>{now_local}</code>",
        parse_mode="HTML",
    )


async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle or configure morning briefing: /briefing on|off [hour]"""
    uid  = str(update.effective_user.id)
    args = context.args

    if not args:
        s = db.get_settings(uid)
        status = "✅ ON" if s["briefing_enabled"] else "❌ OFF"
        return await update.message.reply_text(
            f"☀️ <b>Morning Briefing</b>: {status}\n"
            f"Time: <code>{s['briefing_hour']:02d}:00 {s['timezone']}</code>\n\n"
            "Toggle: <code>/briefing on</code> | <code>/briefing off</code>\n"
            "Set hour: <code>/briefing on 8</code>",
            parse_mode="HTML",
        )

    cmd  = args[0].lower()
    hour = int(args[1]) if len(args) > 1 and args[1].isdigit() else None

    if cmd == "on":
        kwargs: dict = {"briefing_enabled": 1}
        if hour is not None:
            # BUG FIX: validate hour range
            if not (0 <= hour <= 23):
                return await update.message.reply_text("❌ Hour must be 0–23.")
            kwargs["briefing_hour"] = hour
        db.update_settings(uid, **kwargs)
        s = db.get_settings(uid)
        await update.message.reply_text(
            f"✅ Morning briefing <b>enabled</b> at <code>{s['briefing_hour']:02d}:00 {s['timezone']}</code>",
            parse_mode="HTML",
        )
    elif cmd == "off":
        db.update_settings(uid, briefing_enabled=0)
        await update.message.reply_text("❌ Morning briefing <b>disabled</b>.", parse_mode="HTML")
    else:
        await update.message.reply_text("Usage: /briefing on|off [hour]")


async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not groq_client:
        return await update.message.reply_text("⚠️ No GROQ_API_KEY in .env")
    if not context.args:
        return await update.message.reply_text("✏️ /ai how do I bake bread?")

    key  = get_chat_key(update)
    msg  = " ".join(context.args)
    hist = _get_ai_history(key)
    hist.append({"role": "user", "content": msg})
    if len(hist) > 20:
        hist = hist[-20:]

    thinking = await update.message.reply_text("🤖 Thinking…")
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "Ты дружелюбный ассистент. Отвечай коротко с юмором. Отвечай на языке пользователя."},
                *hist,
            ],
            max_tokens=800,
        )
        reply = resp.choices[0].message.content
        hist.append({"role": "assistant", "content": reply})
        _set_ai_history(key, hist)
        await thinking.delete()
        await update.message.reply_text("🤖 " + reply)
    except Exception as e:
        logger.error("Groq error: %s", e)
        await thinking.edit_text("❌ AI unavailable. Try later.")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = get_chat_key(update)
    _set_ai_history(key, [])
    await update.message.reply_text("🔄 AI history cleared.")


async def cmd_persona(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # BUG FIX: removed `global AUTO_REPLY_SYSTEM_PROMPT` — now uses _persona dict
    if not context.args:
        return await update.message.reply_text(
            "✏️ Usage:\n/persona Reply briefly, I'm busy\n/persona Say I'll reply soon"
        )
    _persona["prompt"] = " ".join(context.args)
    await update.message.reply_text(
        f"✅ Persona updated:\n\n<i>{_persona['prompt']}</i>", parse_mode="HTML"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ai = "✅ Connected" if groq_client else "❌ Not configured"
    uid = str(update.effective_user.id)
    s   = db.get_settings(uid)
    task_count = len(db.get_tasks(uid))
    await update.message.reply_text(
        "⚡️ <b>SYSTEM STATUS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"  🤖  AI:       {ai}\n"
        f"  🕐  Timezone: <code>{s['timezone']}</code>\n"
        f"  ☀️  Briefing: {'✅' if s['briefing_enabled'] else '❌'}  "
        f"<code>{s['briefing_hour']:02d}:00</code>\n"
        f"  📝  Tasks:    <code>{task_count} active</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━",
        parse_mode="HTML",
    )


async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Link Telegram account to Chronicle website account."""
    uid  = str(update.effective_user.id)
    args = context.args

    if not args:
        await update.message.reply_text(
            "📧 *Привяжи аккаунт сайта к боту*\n\n"
            "Используй команду:\n`/link твой@email.com`\n\n"
            "Email должен совпадать с тем, через который ты входишь на сайт через Google.",
            parse_mode="Markdown",
        )
        return

    email = args[0].strip().lower()

    # BUG FIX: was returning early after "already linked" without checking new email.
    # Now always attempts to link (upsert), and shows the result.
    existing = db.get_web_user_id(uid)
    if existing:
        await update.message.reply_text(
            "ℹ️ Аккаунт уже привязан. Попытка обновить привязку…",
            parse_mode="Markdown",
        )

    user_id = db.link_telegram(uid, email)
    if user_id:
        await update.message.reply_text(
            f"✅ *Аккаунт привязан!*\n\n"
            f"Теперь все задачи из бота появятся на сайте [schronicle.vercel.app](https://schronicle.vercel.app) и наоборот.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"❌ Email `{email}` не найден на сайте.\n\n"
            f"Убедись что ты зарегистрирован на [schronicle.vercel.app](https://schronicle.vercel.app) через Google с этим email.",
            parse_mode="Markdown",
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  ERROR HANDLER
# ═══════════════════════════════════════════════════════════════════════════════

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors gracefully — suppress Conflict spam, log the rest."""
    if isinstance(context.error, Conflict):
        logger.warning(
            "Conflict error: another bot instance is still running. "
            "Will retry automatically…"
        )
        return
    logger.error("Unhandled exception:", exc_info=context.error)


# ═══════════════════════════════════════════════════════════════════════════════
#  LOCK FILE (prevent duplicate instances)
# ═══════════════════════════════════════════════════════════════════════════════

LOCK_FILE     = "bot.lock"
# Set DISABLE_LOCK_FILE=1 in Railway/Docker — the platform already ensures a single instance.
_LOCK_ENABLED = os.getenv("DISABLE_LOCK_FILE", "0") not in ("1", "true", "yes")


def _acquire_lock():
    """Write PID to lock file. Exit if another instance is already running.

    Container-safe: PID 1 is always the init process (tini/sh), never our bot.
    A lock file referencing PID 1 is therefore always stale and is overwritten.
    """
    if os.path.exists(LOCK_FILE):
        with open(LOCK_FILE) as f:
            raw = f.read().strip()
        try:
            old_pid = int(raw)
        except ValueError:
            logger.warning("Corrupt lock file (content %r). Overwriting.", raw)
            old_pid = None

        if old_pid is not None:
            # PID 1 is init — never our bot process, always stale.
            if old_pid == 1:
                logger.warning("Lock file references PID 1 (init/container). Overwriting.")
            else:
                try:
                    os.kill(old_pid, 0)
                    # Process exists and we can signal it → it really is alive.
                    logger.error(
                        "Another bot instance is running (PID %s). "
                        "Stop it first or delete '%s'.",
                        old_pid, LOCK_FILE,
                    )
                    sys.exit(1)
                except PermissionError:
                    # os.kill succeeded (process exists) but we lack permission — treat as stale.
                    logger.warning(
                        "Lock file PID %s exists but is not our process. Overwriting.", old_pid
                    )
                except ProcessLookupError:
                    logger.warning("Stale lock file (PID %s gone). Overwriting.", old_pid)

    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))


def _release_lock():
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set in .env!")

    if _LOCK_ENABLED:
        _acquire_lock()

    db.init_db()
    db.migrate_from_json()

    app = Application.builder().token(token).build()
    app.bot_data["groq_client"] = groq_client

    # ── Error handler ──────────────────────────────────────────────────────────
    app.add_error_handler(error_handler)

    # ── Business auto-reply (highest priority, group=-1) ──────────────────────
    app.add_handler(TypeHandler(Update, handle_business_message), group=-1)

    # ── Task Manager ──────────────────────────────────────────────────────────
    app.add_handler(build_task_conversation())
    for cb in build_task_callbacks():
        app.add_handler(cb)

    # ── Bot commands ──────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("help",         cmd_start))
    app.add_handler(CommandHandler("link",         cmd_link))
    app.add_handler(CommandHandler("deadlines",    cmd_deadlines))
    app.add_handler(CommandHandler("ai",           cmd_ai))
    app.add_handler(CommandHandler("reset",        cmd_reset))
    app.add_handler(CommandHandler("persona",      cmd_persona))
    app.add_handler(CommandHandler("status",       cmd_status))
    app.add_handler(CommandHandler("tz",           cmd_set_tz))
    app.add_handler(CommandHandler("briefing",     cmd_briefing))

    # ── /add_deadline conversation ────────────────────────────────────────────
    add_deadline_conv = ConversationHandler(
        entry_points=[CommandHandler("add_deadline", cmd_add_deadline)],
        states={
            WAITING_ICAL_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ical_url),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel_ical)],
        allow_reentry=True,
    )
    app.add_handler(add_deadline_conv)

    # ── Scheduled jobs ────────────────────────────────────────────────────────
    jq = app.job_queue

    jq.run_repeating(reminder_check_job, interval=60, first=10)
    logger.info("Reminder check job: every 60s")

    if DEADLINE_CHAT_ID:
        tz_obj    = pytz.timezone(DEADLINE_TZ)
        send_time = datetime.now(tz_obj).replace(
            hour=DEADLINE_HOUR, minute=DEADLINE_MINUTE, second=0, microsecond=0
        ).timetz()
        jq.run_daily(daily_deadlines_job, time=send_time)
        logger.info(
            "AITU deadline broadcast: %02d:%02d %s → %s",
            DEADLINE_HOUR, DEADLINE_MINUTE, DEADLINE_TZ, DEADLINE_CHAT_ID,
        )

    briefing_time = datetime.now(pytz.utc).replace(
        hour=1, minute=0, second=0, microsecond=0  # 01:00 UTC ≈ 06:00 Almaty
    ).timetz()
    jq.run_daily(daily_briefing_job, time=briefing_time)
    logger.info("Morning briefing job registered")

    logger.info("Chronicle Engine v3.0 started 🚀")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        if _LOCK_ENABLED:
            _release_lock()


if __name__ == "__main__":
    main()
