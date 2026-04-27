"""
nlp_parser.py — Chronicle Engine: Smart Task Parser
=====================================================
Attempts Groq-powered NLP extraction first; falls back to regex heuristics.

Output schema:
  {
    "title":    str,           # cleaned task name
    "deadline": str | None,    # UTC ISO "YYYY-MM-DDTHH:MM:SS" or date-only "YYYY-MM-DD"
    "priority": "high"|"medium"|"low"
  }
"""

import json
import re
import logging
from datetime import datetime, timedelta
from typing import Optional
import pytz

logger = logging.getLogger(__name__)

# ── Groq system prompt ────────────────────────────────────────────────────────

_SYSTEM = """You are a precise task parser for a personal productivity bot.
Extract task information from the user's message and return ONLY a JSON object.
No markdown, no backticks, no explanation — raw JSON only.

Required JSON fields:
{
  "title":    "<clean task title with no date/time words>",
  "deadline": "<YYYY-MM-DD HH:MM>" or null,
  "priority": "high" | "medium" | "low"
}

Priority inference rules:
  high   → words: urgent, asap, critical, срочно, важно, немедленно, важнейший
  low    → words: someday, later, when possible, когда-нибудь, потом, не срочно
  medium → everything else (default)

Deadline inference rules:
  "today" / "сегодня"        → today 23:59
  "tomorrow" / "завтра"      → tomorrow 09:00
  "in X hours" / "через X ч" → now + X hours (round to minute)
  "in X days" / "через X дн" → that day at 09:00
  Weekday name               → next occurrence at 09:00
  Explicit time only          → today at that time (if in past → tomorrow)
  No date mentioned           → null
"""

# ── Public API ────────────────────────────────────────────────────────────────

def parse_task_message(
    text: str,
    user_tz: str,
    groq_client,
) -> dict:
    """
    Parse a natural-language task description.
    Always returns a valid dict (never raises).
    """
    if groq_client:
        result = _groq_parse(text, user_tz, groq_client)
        if result:
            return result
    return _regex_parse(text, user_tz)


# ── Groq path ─────────────────────────────────────────────────────────────────

def _groq_parse(text: str, user_tz: str, groq_client) -> Optional[dict]:
    tz = pytz.timezone(user_tz)
    now_local = datetime.now(tz)

    user_prompt = (
        f"Local date/time: {now_local.strftime('%Y-%m-%d %H:%M')} ({user_tz})\n"
        f"Parse this task: {text}"
    )

    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=150,
            temperature=0.05,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"```json|```", "", raw).strip()
        parsed = json.loads(raw)

        title    = str(parsed.get("title") or text).strip() or text
        priority = parsed.get("priority", "medium")
        if priority not in ("high", "medium", "low"):
            priority = "medium"

        deadline_utc = None
        dl = parsed.get("deadline")
        if dl and isinstance(dl, str) and dl.lower() != "null":
            deadline_utc = _local_str_to_utc(dl, tz)

        logger.info("NLP parsed '%s' → title=%s dl=%s pri=%s", text[:40], title, deadline_utc, priority)
        return {"title": title, "deadline": deadline_utc, "priority": priority}

    except Exception as exc:
        logger.warning("Groq NLP failed (%s) — using regex fallback", exc)
        return None


def _local_str_to_utc(local_str: str, tz: pytz.BaseTzInfo) -> Optional[str]:
    """Convert 'YYYY-MM-DD HH:MM' in local tz to UTC ISO string."""
    try:
        dt = datetime.strptime(local_str.strip(), "%Y-%m-%d %H:%M")
        dt_local = tz.localize(dt)
        dt_utc   = dt_local.astimezone(pytz.utc)
        return dt_utc.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


# ── Regex / heuristic fallback ────────────────────────────────────────────────

_HIGH_WORDS = frozenset({"urgent","asap","critical","срочно","важно","немедленно"})
_LOW_WORDS  = frozenset({"someday","later","потом","когда-нибудь","не_срочно","необязательно"})

_WEEKDAYS = {
    "monday":    0, "понедельник": 0,
    "tuesday":   1, "вторник":     1,
    "wednesday": 2, "среда":       2, "среду": 2,
    "thursday":  3, "четверг":     3,
    "friday":    4, "пятница":     4, "пятницу": 4,
    "saturday":  5, "суббота":     5, "субботу": 5,
    "sunday":    6, "воскресенье": 6,
}


def _regex_parse(text: str, user_tz: str) -> dict:
    tz   = pytz.timezone(user_tz)
    now  = datetime.now(tz)
    ltext = text.lower()
    deadline: Optional[datetime] = None

    # ── Extract time ──
    time_m = re.search(r"\b(\d{1,2})[:\.](\d{2})\b", text)
    hour   = int(time_m.group(1)) if time_m else None
    minute = int(time_m.group(2)) if time_m else 0

    # ── Date keywords ──
    if re.search(r"\btoday\b|\bсегодня\b", ltext):
        deadline = now.replace(hour=hour or 23, minute=minute or 59, second=0, microsecond=0)

    elif re.search(r"\btomorrow\b|\bзавтра\b", ltext):
        deadline = (now + timedelta(days=1)).replace(
            hour=hour or 9, minute=minute, second=0, microsecond=0
        )

    elif m := re.search(r"(?:in|через)\s+(\d+)\s*(?:hours?|h|час(?:а|ов)?)", ltext):
        deadline = now + timedelta(hours=int(m.group(1)))

    elif m := re.search(r"(?:in|через)\s+(\d+)\s*(?:days?|d|дн(?:ей|я)?)", ltext):
        deadline = (now + timedelta(days=int(m.group(1)))).replace(
            hour=9, minute=0, second=0, microsecond=0
        )

    elif m := re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", text):
        try:
            day, mon = int(m.group(1)), int(m.group(2))
            yr = int(m.group(3) or now.year)
            if yr < 100:
                yr += 2000
            deadline = tz.localize(datetime(yr, mon, day, hour or 9, minute, 0))
        except Exception:
            pass

    else:
        # Weekday name
        for name, wd in _WEEKDAYS.items():
            if name in ltext:
                days_ahead = (wd - now.weekday()) % 7 or 7
                deadline = (now + timedelta(days=days_ahead)).replace(
                    hour=hour or 9, minute=minute, second=0, microsecond=0
                )
                break

        # Time only → today or tomorrow
        if deadline is None and time_m:
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            deadline = candidate

    # ── Ensure not in the past ──
    if deadline and deadline < now:
        deadline = None

    # ── Convert to UTC ISO ──
    deadline_utc: Optional[str] = None
    if deadline is not None:
        if deadline.tzinfo is None:
            deadline = tz.localize(deadline)
        deadline_utc = deadline.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%S")

    # ── Priority ──
    words = set(re.findall(r"\w+", ltext))
    if words & _HIGH_WORDS:
        priority = "high"
    elif words & _LOW_WORDS:
        priority = "low"
    else:
        priority = "medium"

    return {"title": text, "deadline": deadline_utc, "priority": priority}


# ── Display helpers (used by tasks.py) ────────────────────────────────────────

def format_deadline_local(deadline_str: str, user_tz: str) -> str:
    """
    Return human-readable deadline in user's timezone.

    FIX: When deadline_str has no 'T' (date-only, e.g. '2026-04-23'), display
    just the date without any timezone conversion. Previously, a date-only string
    was treated as midnight UTC and converted to local time — showing wrong hours
    (e.g. 05:00 for UTC+5 users when no specific time was set).
    """
    try:
        if "T" not in deadline_str:
            # Date-only: parse and format without timezone conversion
            dt = datetime.strptime(deadline_str[:10], "%Y-%m-%d")
            return dt.strftime("%d.%m.%Y")
        # Has explicit time: convert from UTC to user's timezone
        tz   = pytz.timezone(user_tz)
        dt   = datetime.fromisoformat(deadline_str).replace(tzinfo=pytz.utc)
        dt_l = dt.astimezone(tz)
        return dt_l.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return deadline_str


def time_until(deadline_str: str) -> str:
    """
    Return human-readable time remaining.

    FIX: Date-only strings (no 'T') are treated as end-of-day (23:59:59 UTC)
    to avoid false "overdue" for tasks due today when no time was specified.
    """
    try:
        if "T" not in deadline_str:
            # Date-only: treat as end of that calendar day in UTC
            dt = datetime.strptime(deadline_str[:10], "%Y-%m-%d")
            dt = dt.replace(hour=23, minute=59, second=59, tzinfo=pytz.utc)
        else:
            dt = datetime.fromisoformat(deadline_str).replace(tzinfo=pytz.utc)

        now = datetime.now(pytz.utc)
        if dt <= now:
            return "⚠️ overdue"
        delta = dt - now
        days  = delta.days
        hrs   = delta.seconds // 3600
        mins  = (delta.seconds % 3600) // 60
        if days > 1:
            return f"in {days}d"
        if days == 1:
            return f"in {days}d {hrs}h"
        if hrs > 0:
            return f"in {hrs}h {mins}m"
        return f"in {mins}m"
    except Exception:
        return ""
