"""
db.py — Chronicle Engine: SQLite Persistence Layer
====================================================
Schema
  tasks        — full task records with lifecycle fields
  user_settings — per-user timezone + briefing prefs
"""

import sqlite3
import os
import json
import logging
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from typing import Optional, List

logger = logging.getLogger(__name__)
DB_PATH = os.getenv("TASKS_DB", "tasks.db")


# ── Connection ────────────────────────────────────────────────────────────────

@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def init_db():
    """Create tables and indexes if they don't exist."""
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id          TEXT    NOT NULL,
                text             TEXT    NOT NULL,
                status           TEXT    NOT NULL DEFAULT 'todo'
                                         CHECK(status IN ('todo','in_progress','done')),
                priority         TEXT    NOT NULL DEFAULT 'medium'
                                         CHECK(priority IN ('high','medium','low')),
                deadline         TEXT,                     -- UTC ISO "YYYY-MM-DDTHH:MM:SS"
                created_at       TEXT    NOT NULL,          -- UTC ISO
                completed_at     TEXT,                     -- UTC ISO, set when done
                archived         INTEGER NOT NULL DEFAULT 0,
                reminder_24h     INTEGER NOT NULL DEFAULT 0,
                reminder_1h      INTEGER NOT NULL DEFAULT 0,
                reminder_15m     INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id          TEXT PRIMARY KEY,
                timezone         TEXT    NOT NULL DEFAULT 'Asia/Almaty',
                briefing_hour    INTEGER NOT NULL DEFAULT 9,
                briefing_enabled INTEGER NOT NULL DEFAULT 1
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_user     ON tasks(user_id, archived);
            CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline)
                WHERE deadline IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_tasks_remind   ON tasks(reminder_24h, reminder_1h, reminder_15m)
                WHERE archived=0 AND status != 'done';
        """)
    logger.info("DB initialized at %s", DB_PATH)


# ── Migration: tasks.json → SQLite ───────────────────────────────────────────

def migrate_from_json(json_path: str = "tasks.json"):
    """One-time import from the old flat-file format."""
    if not os.path.exists(json_path):
        return
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            old = json.load(f)
        count = 0
        for user_id, task_list in old.items():
            for t in task_list:
                if not t.get("text"):
                    continue
                add_task(
                    user_id=user_id,
                    text=t["text"],
                    priority="medium",
                    deadline_utc=None,
                    status="done" if t.get("done") else "todo",
                )
                count += 1
        os.rename(json_path, json_path + ".migrated")
        logger.info("Migrated %d tasks from %s", count, json_path)
    except Exception as e:
        logger.warning("Migration skipped: %s", e)


# ── User Settings ─────────────────────────────────────────────────────────────

def get_settings(user_id: str) -> dict:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM user_settings WHERE user_id=?", (user_id,)
        ).fetchone()
        if row:
            return dict(row)
        con.execute(
            "INSERT OR IGNORE INTO user_settings(user_id) VALUES(?)", (user_id,)
        )
        return {
            "user_id": user_id,
            "timezone": "Asia/Almaty",
            "briefing_hour": 9,
            "briefing_enabled": 1,
        }


def get_tz(user_id: str) -> str:
    return get_settings(user_id).get("timezone", "Asia/Almaty")


def update_settings(user_id: str, **kwargs):
    if not kwargs:
        return
    with _conn() as con:
        # Ensure row exists
        con.execute("INSERT OR IGNORE INTO user_settings(user_id) VALUES(?)", (user_id,))
        set_clause = ", ".join(f"{k}=?" for k in kwargs)
        values = list(kwargs.values()) + [user_id]
        con.execute(f"UPDATE user_settings SET {set_clause} WHERE user_id=?", values)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ── Task CRUD ─────────────────────────────────────────────────────────────────

def add_task(
    user_id: str,
    text: str,
    priority: str = "medium",
    deadline_utc: Optional[str] = None,
    status: str = "todo",
) -> int:
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO tasks(user_id, text, priority, deadline, created_at, status)
               VALUES(?,?,?,?,?,?)""",
            (user_id, text, priority, deadline_utc, _now_utc(), status),
        )
        return cur.lastrowid


def get_tasks(user_id: str, archived: bool = False) -> List[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM tasks WHERE user_id=? AND archived=?
               ORDER BY
                 CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                 CASE WHEN deadline IS NULL THEN 1 ELSE 0 END,
                 deadline ASC,
                 created_at DESC""",
            (user_id, int(archived)),
        ).fetchall()
        return [dict(r) for r in rows]


def get_task(task_id: int) -> Optional[dict]:
    with _conn() as con:
        row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None


def update_task(task_id: int, **kwargs):
    if not kwargs:
        return
    set_clause = ", ".join(f"{k}=?" for k in kwargs)
    with _conn() as con:
        con.execute(
            f"UPDATE tasks SET {set_clause} WHERE id=?",
            list(kwargs.values()) + [task_id],
        )


def set_status(task_id: int, status: str):
    kwargs: dict = {"status": status}
    if status == "done":
        kwargs["completed_at"] = _now_utc()
    else:
        kwargs["completed_at"] = None
    update_task(task_id, **kwargs)


def delete_task(task_id: int):
    with _conn() as con:
        con.execute("DELETE FROM tasks WHERE id=?", (task_id,))


def archive_done_tasks(user_id: str) -> int:
    """Move all 'done' tasks to archive. Returns count archived."""
    with _conn() as con:
        cur = con.execute(
            """UPDATE tasks SET archived=1
               WHERE user_id=? AND status='done' AND archived=0""",
            (user_id,),
        )
        return cur.rowcount


# ── Calendar Query ────────────────────────────────────────────────────────────

def get_tasks_for_month(user_id: str, year: int, month: int) -> dict:
    """
    Returns {day_int: [task_dicts]} for tasks with deadlines in this month.
    Deadlines stored in UTC; we group by calendar day in UTC for simplicity.
    """
    import calendar as cal_mod

    last_day = cal_mod.monthrange(year, month)[1]
    month_start = f"{year:04d}-{month:02d}-01T00:00:00"
    month_end   = f"{year:04d}-{month:02d}-{last_day:02d}T23:59:59"

    with _conn() as con:
        rows = con.execute(
            """SELECT id, text, status, priority, deadline
               FROM tasks
               WHERE user_id=? AND archived=0 AND deadline IS NOT NULL
               AND deadline BETWEEN ? AND ?
               ORDER BY deadline ASC""",
            (user_id, month_start, month_end),
        ).fetchall()

    result: dict = {}
    for row in rows:
        d = dict(row)
        try:
            dt = datetime.fromisoformat(d["deadline"])
            result.setdefault(dt.day, []).append(d)
        except Exception:
            pass
    return result


# ── Reminder Queries ──────────────────────────────────────────────────────────

def get_tasks_needing_reminders() -> List[tuple]:
    """
    Returns list of (flag_col, task_dict) for tasks whose deadline falls
    inside a reminder window and whose flag hasn't been sent yet.
    """
    now = datetime.now(timezone.utc)
    windows = [
        ("reminder_15m", now + timedelta(minutes=10), now + timedelta(minutes=20)),
        ("reminder_1h",  now + timedelta(minutes=50), now + timedelta(minutes=70)),
        ("reminder_24h", now + timedelta(hours=23),   now + timedelta(hours=25)),
    ]
    results = []
    with _conn() as con:
        for flag, t_min, t_max in windows:
            rows = con.execute(
                f"""SELECT * FROM tasks
                    WHERE {flag}=0 AND status!='done' AND archived=0
                    AND deadline IS NOT NULL
                    AND deadline BETWEEN ? AND ?""",
                (t_min.strftime("%Y-%m-%dT%H:%M:%S"),
                 t_max.strftime("%Y-%m-%dT%H:%M:%S")),
            ).fetchall()
            for row in rows:
                results.append((flag, dict(row)))
    return results


def mark_reminder_sent(task_id: int, flag: str):
    with _conn() as con:
        con.execute(f"UPDATE tasks SET {flag}=1 WHERE id=?", (task_id,))


# ── Analytics ─────────────────────────────────────────────────────────────────

def get_analytics(user_id: str) -> dict:
    now = datetime.now(timezone.utc)
    week_start      = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    last_week_start = week_start - timedelta(weeks=1)

    with _conn() as con:
        total_active = con.execute(
            "SELECT COUNT(*) FROM tasks WHERE user_id=? AND archived=0", (user_id,)
        ).fetchone()[0]

        done_this_week = con.execute(
            """SELECT COUNT(*) FROM tasks WHERE user_id=? AND status='done'
               AND completed_at >= ?""",
            (user_id, week_start.strftime("%Y-%m-%dT%H:%M:%S")),
        ).fetchone()[0]

        done_last_week = con.execute(
            """SELECT COUNT(*) FROM tasks WHERE user_id=?
               AND status='done' AND completed_at >= ? AND completed_at < ?""",
            (user_id,
             last_week_start.strftime("%Y-%m-%dT%H:%M:%S"),
             week_start.strftime("%Y-%m-%dT%H:%M:%S")),
        ).fetchone()[0]

        by_status = {
            r[0]: r[1]
            for r in con.execute(
                "SELECT status, COUNT(*) FROM tasks WHERE user_id=? AND archived=0 GROUP BY status",
                (user_id,),
            ).fetchall()
        }

        by_priority = {
            r[0]: r[1]
            for r in con.execute(
                "SELECT priority, COUNT(*) FROM tasks WHERE user_id=? AND archived=0 GROUP BY priority",
                (user_id,),
            ).fetchall()
        }

        total_archived = con.execute(
            "SELECT COUNT(*) FROM tasks WHERE user_id=? AND archived=1", (user_id,)
        ).fetchone()[0]

        total_ever = con.execute(
            "SELECT COUNT(*) FROM tasks WHERE user_id=?", (user_id,)
        ).fetchone()[0]

    return {
        "total_active":  total_active,
        "done_this_week": done_this_week,
        "done_last_week": done_last_week,
        "by_status":      by_status,
        "by_priority":    by_priority,
        "total_archived": total_archived,
        "total_ever":     total_ever,
    }
