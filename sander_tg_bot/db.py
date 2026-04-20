"""
db.py — Chronicle Engine: PostgreSQL Persistence Layer
=======================================================
Replaces MongoDB. Uses the same Vercel Postgres DB as the website.
Collections → Tables:
  tasks         — task records
  user_settings — per-user timezone + briefing prefs (bot_user_settings)
  tg_connections — telegram chat_id ↔ user_id link
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List

import psycopg2
import psycopg2.extras
from psycopg2 import pool as pg_pool

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")

_pool: Optional[pg_pool.SimpleConnectionPool] = None


# ── Connection ────────────────────────────────────────────────────────────────

def _get_pool():
    global _pool
    if _pool is None:
        _pool = pg_pool.SimpleConnectionPool(
            1, 5,
            dsn=DATABASE_URL,
            sslmode="require",
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    return _pool


def _conn():
    return _get_pool().getconn()


def _release(conn):
    _get_pool().putconn(conn)


def _query(sql: str, params=()) -> List[dict]:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            try:
                return [dict(r) for r in cur.fetchall()]
            except Exception:
                return []
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        _release(conn)


def _exec(sql: str, params=()) -> Optional[dict]:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            try:
                row = cur.fetchone()
                return dict(row) if row else None
            except Exception:
                return None
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        _release(conn)


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def init_db():
    """Create bot-specific tables if they don't exist."""
    # Bot user settings (separate from web app users)
    _exec("""
        CREATE TABLE IF NOT EXISTS bot_user_settings (
            tg_user_id   TEXT PRIMARY KEY,
            timezone     TEXT DEFAULT 'Asia/Almaty',
            briefing_hour INT DEFAULT 9,
            briefing_enabled INT DEFAULT 1
        )
    """)
    # Add archived column to tasks if missing
    _exec("""
        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS archived BOOLEAN DEFAULT FALSE
    """)
    # Add status column to tasks if missing
    _exec("""
        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'todo'
    """)
    # Add priority column to tasks if missing (already exists but just in case)
    _exec("""
        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS priority TEXT DEFAULT 'medium'
    """)
    # Add reminder columns
    _exec("""
        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS reminder_24h BOOLEAN DEFAULT FALSE
    """)
    _exec("""
        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS reminder_1h BOOLEAN DEFAULT FALSE
    """)
    _exec("""
        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS reminder_15m BOOLEAN DEFAULT FALSE
    """)
    # tg_connections already exists from migrate.js
    logger.info("PostgreSQL initialized")


def migrate_from_json(json_path: str = "tasks.json"):
    pass  # No longer needed


# ── Telegram Link ─────────────────────────────────────────────────────────────

def link_telegram(tg_user_id: str, email: str) -> Optional[str]:
    """Link a Telegram user to a website account by email. Returns user_id or None."""
    rows = _query("SELECT id FROM users WHERE email = %s", (email,))
    if not rows:
        return None
    user_id = rows[0]["id"]
    _exec("""
        INSERT INTO tg_connections (user_id, chat_id, created_at, updated_at)
        VALUES (%s, %s, NOW(), NOW())
        ON CONFLICT (user_id) DO UPDATE SET chat_id = EXCLUDED.chat_id, updated_at = NOW()
    """, (user_id, tg_user_id))
    return user_id


def get_web_user_id(tg_user_id: str) -> Optional[str]:
    """Get website user_id linked to this Telegram chat."""
    rows = _query("SELECT user_id FROM tg_connections WHERE chat_id = %s", (tg_user_id,))
    return rows[0]["user_id"] if rows else None


# ── User Settings ─────────────────────────────────────────────────────────────

_DEFAULTS = {"timezone": "Asia/Almaty", "briefing_hour": 9, "briefing_enabled": 1}


def get_settings(tg_user_id: str) -> dict:
    rows = _query("SELECT * FROM bot_user_settings WHERE tg_user_id = %s", (tg_user_id,))
    if rows:
        return dict(rows[0])
    _exec("""
        INSERT INTO bot_user_settings (tg_user_id, timezone, briefing_hour, briefing_enabled)
        VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING
    """, (tg_user_id, _DEFAULTS["timezone"], _DEFAULTS["briefing_hour"], _DEFAULTS["briefing_enabled"]))
    return {"tg_user_id": tg_user_id, **_DEFAULTS}


def get_tz(tg_user_id: str) -> str:
    return get_settings(tg_user_id).get("timezone", "Asia/Almaty")


def update_settings(tg_user_id: str, **kwargs):
    if not kwargs:
        return
    sets = ", ".join(f"{k} = %s" for k in kwargs)
    vals = list(kwargs.values()) + [tg_user_id]
    _exec(f"""
        INSERT INTO bot_user_settings (tg_user_id) VALUES (%s)
        ON CONFLICT (tg_user_id) DO UPDATE SET {sets}
    """.replace("INSERT INTO bot_user_settings (tg_user_id) VALUES (%s)", 
                f"INSERT INTO bot_user_settings (tg_user_id, {', '.join(kwargs.keys())}) VALUES (%s, {', '.join(['%s']*len(kwargs))})"),
        [tg_user_id] + list(kwargs.values()))
    # Simpler approach:
    _exec(f"UPDATE bot_user_settings SET {sets} WHERE tg_user_id = %s", vals)


# ── Task helpers ──────────────────────────────────────────────────────────────

_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _sort_tasks(tasks: List[dict]) -> List[dict]:
    return sorted(
        tasks,
        key=lambda t: (
            _PRIORITY_ORDER.get(t.get("priority", "medium"), 1),
            0 if t.get("deadline") or t.get("due_date") else 1,
            str(t.get("deadline") or t.get("due_date") or ""),
            -(t.get("id") or 0),
        ),
    )


def _get_user_id(tg_user_id: str) -> Optional[str]:
    """Get website user_id for task operations."""
    return get_web_user_id(tg_user_id)


# ── Task CRUD ─────────────────────────────────────────────────────────────────

def add_task(
    user_id: str,
    text: str,
    priority: str = "medium",
    deadline_utc: Optional[str] = None,
    status: str = "todo",
) -> int:
    # user_id here is tg_user_id — get web user_id
    web_uid = _get_user_id(user_id)
    if not web_uid:
        raise ValueError(f"Telegram user {user_id} is not linked to a website account. Use /link <email>")

    due_date = None
    if deadline_utc:
        try:
            due_date = datetime.fromisoformat(deadline_utc).date()
        except Exception:
            pass

    completed = status == "done"
    row = _exec("""
        INSERT INTO tasks (user_id, title, due_date, priority, completed, status, archived, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, FALSE, NOW()) RETURNING id
    """, (web_uid, text, due_date, priority, completed, status))
    return row["id"] if row else 0


def get_tasks(tg_user_id: str, archived: bool = False) -> List[dict]:
    web_uid = _get_user_id(tg_user_id)
    if not web_uid:
        return []
    rows = _query("""
        SELECT * FROM tasks WHERE user_id = %s AND archived = %s ORDER BY created_at DESC
    """, (web_uid, archived))
    return _sort_tasks([dict(r) for r in rows])


def get_task(task_id: int) -> Optional[dict]:
    rows = _query("SELECT * FROM tasks WHERE id = %s", (task_id,))
    return dict(rows[0]) if rows else None


def update_task(task_id: int, **kwargs):
    if not kwargs:
        return
    # Map old field names to new ones
    if "archived" in kwargs:
        kwargs["archived"] = bool(kwargs["archived"])
    if "deadline" in kwargs:
        kwargs["due_date"] = kwargs.pop("deadline")
    sets = ", ".join(f"{k} = %s" for k in kwargs)
    vals = list(kwargs.values()) + [task_id]
    _exec(f"UPDATE tasks SET {sets} WHERE id = %s", vals)


def set_status(task_id: int, status: str):
    completed = status == "done"
    _exec("UPDATE tasks SET status = %s, completed = %s WHERE id = %s", (status, completed, task_id))


def delete_task(task_id: int):
    _exec("DELETE FROM tasks WHERE id = %s", (task_id,))


def archive_done_tasks(tg_user_id: str) -> int:
    web_uid = _get_user_id(tg_user_id)
    if not web_uid:
        return 0
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tasks SET archived = TRUE
                WHERE user_id = %s AND completed = TRUE AND archived = FALSE
            """, (web_uid,))
            count = cur.rowcount
            conn.commit()
            return count
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        _release(conn)


# ── Calendar Query ────────────────────────────────────────────────────────────

def get_tasks_for_month(tg_user_id: str, year: int, month: int) -> dict:
    import calendar as cal_mod
    web_uid = _get_user_id(tg_user_id)
    if not web_uid:
        return {}
    last_day = cal_mod.monthrange(year, month)[1]
    rows = _query("""
        SELECT * FROM tasks
        WHERE user_id = %s AND archived = FALSE
          AND due_date >= %s AND due_date <= %s
        ORDER BY due_date ASC
    """, (web_uid, f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last_day:02d}"))

    result: dict = {}
    for row in rows:
        d = row.get("due_date")
        if d:
            day = d.day if hasattr(d, "day") else int(str(d).split("-")[2])
            result.setdefault(day, []).append(dict(row))
    return result


# ── Reminder Queries ──────────────────────────────────────────────────────────

def get_tasks_needing_reminders() -> List[tuple]:
    now = datetime.now(timezone.utc)
    windows = [
        ("reminder_15m", now + timedelta(minutes=10), now + timedelta(minutes=20)),
        ("reminder_1h",  now + timedelta(minutes=50), now + timedelta(minutes=70)),
        ("reminder_24h", now + timedelta(hours=23),   now + timedelta(hours=25)),
    ]
    results = []
    for flag, t_min, t_max in windows:
        rows = _query(f"""
            SELECT t.*, tc.chat_id as tg_chat_id FROM tasks t
            JOIN tg_connections tc ON tc.user_id = t.user_id
            WHERE t.{flag} = FALSE AND t.completed = FALSE AND t.archived = FALSE
              AND t.due_date >= %s AND t.due_date <= %s
        """, (t_min.date(), t_max.date()))
        for row in rows:
            results.append((flag, dict(row)))
    return results


def mark_reminder_sent(task_id: int, flag: str):
    _exec(f"UPDATE tasks SET {flag} = TRUE WHERE id = %s", (task_id,))


# ── Analytics ─────────────────────────────────────────────────────────────────

def get_analytics(tg_user_id: str) -> dict:
    web_uid = _get_user_id(tg_user_id)
    if not web_uid:
        return {}
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    last_week_start = week_start - timedelta(weeks=1)

    total_active   = len(_query("SELECT id FROM tasks WHERE user_id = %s AND archived = FALSE", (web_uid,)))
    done_this_week = len(_query("SELECT id FROM tasks WHERE user_id = %s AND completed = TRUE AND created_at >= %s", (web_uid, week_start)))
    done_last_week = len(_query("SELECT id FROM tasks WHERE user_id = %s AND completed = TRUE AND created_at >= %s AND created_at < %s", (web_uid, last_week_start, week_start)))
    total_archived = len(_query("SELECT id FROM tasks WHERE user_id = %s AND archived = TRUE", (web_uid,)))

    by_status: dict = {}
    for row in _query("SELECT status, COUNT(*) as cnt FROM tasks WHERE user_id = %s AND archived = FALSE GROUP BY status", (web_uid,)):
        by_status[row["status"]] = row["cnt"]

    by_priority: dict = {}
    for row in _query("SELECT priority, COUNT(*) as cnt FROM tasks WHERE user_id = %s AND archived = FALSE GROUP BY priority", (web_uid,)):
        by_priority[row["priority"]] = row["cnt"]

    return {
        "total_active":   total_active,
        "done_this_week": done_this_week,
        "done_last_week": done_last_week,
        "by_status":      by_status,
        "by_priority":    by_priority,
        "total_archived": total_archived,
        "total_ever":     total_active + total_archived,
    }
