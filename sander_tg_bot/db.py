"""
db.py — Chronicle Engine: MongoDB Persistence Layer
====================================================
Collections:
  tasks         — full task records with lifecycle fields
  user_settings — per-user timezone + briefing prefs
  counters      — auto-increment for task IDs
"""

import os
import certifi
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from pymongo import MongoClient, ASCENDING
from pymongo.collection import Collection

logger = logging.getLogger(__name__)

MONGO_URI = os.getenv("MONGODB_URI", "")
_client: Optional[MongoClient] = None


# ── Connection ────────────────────────────────────────────────────────────────

def _db():
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, tlsCAFile=certifi.where())
    return _client["chronicle"]


def _col(name: str) -> Collection:
    return _db()[name]


def _next_id() -> int:
    """Auto-increment counter for task IDs."""
    result = _col("counters").find_one_and_update(
        {"_id": "task_id"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True,
    )
    return result["seq"]


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def init_db():
    """Create indexes."""
    tasks = _col("tasks")
    tasks.create_index([("user_id", ASCENDING), ("archived", ASCENDING)])
    tasks.create_index([("deadline", ASCENDING)], sparse=True)
    logger.info("MongoDB initialized — db: chronicle")


# ── Migration: tasks.json → MongoDB ──────────────────────────────────────────

def migrate_from_json(json_path: str = "tasks.json"):
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _sort_tasks(tasks: List[dict]) -> List[dict]:
    return sorted(
        tasks,
        key=lambda t: (
            _PRIORITY_ORDER.get(t.get("priority", "medium"), 1),
            0 if t.get("deadline") else 1,
            t.get("deadline") or "",
            -(t.get("id") or 0),
        ),
    )


# ── User Settings ─────────────────────────────────────────────────────────────

_DEFAULTS = {"timezone": "Asia/Almaty", "briefing_hour": 9, "briefing_enabled": 1}


def get_settings(user_id: str) -> dict:
    doc = _col("user_settings").find_one({"user_id": user_id})
    if doc:
        doc.pop("_id", None)
        return doc
    new = {"user_id": user_id, **_DEFAULTS}
    _col("user_settings").insert_one({**new})
    return new


def get_tz(user_id: str) -> str:
    return get_settings(user_id).get("timezone", "Asia/Almaty")


def update_settings(user_id: str, **kwargs):
    if not kwargs:
        return
    _col("user_settings").update_one({"user_id": user_id}, {"$set": kwargs}, upsert=True)


# ── Task CRUD ─────────────────────────────────────────────────────────────────

def add_task(
    user_id: str,
    text: str,
    priority: str = "medium",
    deadline_utc: Optional[str] = None,
    status: str = "todo",
) -> int:
    task_id = _next_id()
    _col("tasks").insert_one({
        "id":           task_id,
        "user_id":      user_id,
        "text":         text,
        "status":       status,
        "priority":     priority,
        "deadline":     deadline_utc,
        "created_at":   _now_utc(),
        "completed_at": None,
        "archived":     0,
        "reminder_24h": 0,
        "reminder_1h":  0,
        "reminder_15m": 0,
    })
    return task_id


def get_tasks(user_id: str, archived: bool = False) -> List[dict]:
    docs = list(_col("tasks").find({"user_id": user_id, "archived": int(archived)}, {"_id": 0}))
    return _sort_tasks(docs)


def get_task(task_id: int) -> Optional[dict]:
    return _col("tasks").find_one({"id": task_id}, {"_id": 0})


def update_task(task_id: int, **kwargs):
    if not kwargs:
        return
    _col("tasks").update_one({"id": task_id}, {"$set": kwargs})


def set_status(task_id: int, status: str):
    kwargs: dict = {"status": status}
    kwargs["completed_at"] = _now_utc() if status == "done" else None
    update_task(task_id, **kwargs)


def delete_task(task_id: int):
    _col("tasks").delete_one({"id": task_id})


def archive_done_tasks(user_id: str) -> int:
    result = _col("tasks").update_many(
        {"user_id": user_id, "status": "done", "archived": 0},
        {"$set": {"archived": 1}},
    )
    return result.modified_count


# ── Calendar Query ────────────────────────────────────────────────────────────

def get_tasks_for_month(user_id: str, year: int, month: int) -> dict:
    import calendar as cal_mod
    last_day    = cal_mod.monthrange(year, month)[1]
    month_start = f"{year:04d}-{month:02d}-01T00:00:00"
    month_end   = f"{year:04d}-{month:02d}-{last_day:02d}T23:59:59"

    docs = list(_col("tasks").find(
        {"user_id": user_id, "archived": 0,
         "deadline": {"$ne": None, "$gte": month_start, "$lte": month_end}},
        {"_id": 0, "id": 1, "text": 1, "status": 1, "priority": 1, "deadline": 1},
    ).sort("deadline", ASCENDING))

    result: dict = {}
    for doc in docs:
        try:
            dt = datetime.fromisoformat(doc["deadline"])
            result.setdefault(dt.day, []).append(doc)
        except Exception:
            pass
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
        docs = list(_col("tasks").find(
            {flag: 0, "status": {"$ne": "done"}, "archived": 0,
             "deadline": {
                 "$gte": t_min.strftime("%Y-%m-%dT%H:%M:%S"),
                 "$lte": t_max.strftime("%Y-%m-%dT%H:%M:%S"),
             }},
            {"_id": 0},
        ))
        for doc in docs:
            results.append((flag, doc))
    return results


def mark_reminder_sent(task_id: int, flag: str):
    _col("tasks").update_one({"id": task_id}, {"$set": {flag: 1}})


# ── Analytics ─────────────────────────────────────────────────────────────────

def get_analytics(user_id: str) -> dict:
    now             = datetime.now(timezone.utc)
    week_start      = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    last_week_start = week_start - timedelta(weeks=1)
    ws  = week_start.strftime("%Y-%m-%dT%H:%M:%S")
    lws = last_week_start.strftime("%Y-%m-%dT%H:%M:%S")

    col = _col("tasks")
    total_active   = col.count_documents({"user_id": user_id, "archived": 0})
    done_this_week = col.count_documents({"user_id": user_id, "status": "done", "completed_at": {"$gte": ws}})
    done_last_week = col.count_documents({"user_id": user_id, "status": "done", "completed_at": {"$gte": lws, "$lt": ws}})
    total_archived = col.count_documents({"user_id": user_id, "archived": 1})
    total_ever     = col.count_documents({"user_id": user_id})

    by_status: dict = {}
    for doc in col.aggregate([
        {"$match": {"user_id": user_id, "archived": 0}},
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]):
        by_status[doc["_id"]] = doc["count"]

    by_priority: dict = {}
    for doc in col.aggregate([
        {"$match": {"user_id": user_id, "archived": 0}},
        {"$group": {"_id": "$priority", "count": {"$sum": 1}}},
    ]):
        by_priority[doc["_id"]] = doc["count"]

    return {
        "total_active":   total_active,
        "done_this_week": done_this_week,
        "done_last_week": done_last_week,
        "by_status":      by_status,
        "by_priority":    by_priority,
        "total_archived": total_archived,
        "total_ever":     total_ever,
    }
