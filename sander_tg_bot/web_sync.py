"""
web_sync.py — Chronicle Engine: Web ↔ Bot двусторонняя синхронизация
=====================================================================
Подключи этот модуль в tasks.py:
  from web_sync import sync_task_created, sync_task_updated, sync_task_deleted

Переменные окружения (.env):
  WEB_API_URL      = https://your-site.railway.app
  TG_WEBHOOK_SECRET = <сгенерируй: python -c "import secrets; print(secrets.token_hex(32))">
"""

import os
import json
import logging
from urllib.request import urlopen, Request
from urllib.error import URLError

logger = logging.getLogger(__name__)

WEB_API_URL       = os.getenv("WEB_API_URL", "").rstrip("/")
WEBHOOK_SECRET    = os.getenv("TG_WEBHOOK_SECRET", "")


def _post(payload: dict) -> bool:
    """Send a JSON POST to /api/tg-webhook on the web app."""
    if not WEB_API_URL or not WEBHOOK_SECRET:
        logger.debug("web_sync: WEB_API_URL or TG_WEBHOOK_SECRET not set — skipping")
        return False

    url = f"{WEB_API_URL}/api/tg-webhook"
    data = json.dumps(payload).encode()
    req = Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-webhook-secret": WEBHOOK_SECRET,
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=5) as resp:
            status = resp.status
            if status not in (200, 201):
                logger.warning("web_sync: unexpected status %s from %s", status, url)
                return False
        return True
    except URLError as e:
        logger.warning("web_sync: network error — %s", e.reason)
        return False
    except Exception as e:
        logger.warning("web_sync: error — %s", e)
        return False


def sync_task_created(chat_id: str | int, task: dict) -> bool:
    """
    Notify web app that a task was created in Telegram.

    task dict keys:
      title    (str, required)
      due_date (str ISO 8601 or None)
      priority (str: "high" | "medium" | "low")
    """
    return _post({
        "action":  "task_created",
        "chat_id": str(chat_id),
        "task": {
            "title":    task.get("title", ""),
            "due_date": task.get("deadline_utc"),   # ISO string or None
            "priority": task.get("priority", "medium"),
        },
    })


def sync_task_updated(chat_id: str | int, task: dict) -> bool:
    """
    Notify web app that a task was updated in Telegram.

    task dict keys (all optional except id):
      id        (int or str, required)
      title, due_date, priority, completed
    """
    payload_task = {"id": str(task["id"])}

    for key in ("title", "due_date", "priority", "completed"):
        if key in task:
            val = task[key]
            if key == "due_date":
                val = task.get("deadline_utc")
            payload_task[key] = val

    return _post({
        "action":  "task_updated",
        "chat_id": str(chat_id),
        "task":    payload_task,
    })


def sync_task_deleted(chat_id: str | int, task_id) -> bool:
    """Notify web app that a task was deleted in Telegram."""
    return _post({
        "action":  "task_deleted",
        "chat_id": str(chat_id),
        "task":    {"id": str(task_id)},
    })


# ──────────────────────────────────────────────────────────────────────
# Инструкция по интеграции в tasks.py
# ──────────────────────────────────────────────────────────────────────
#
# В конец функции add_task() добавь:
#
#   from web_sync import sync_task_created
#   sync_task_created(chat_id, {
#       "title":        task_text,
#       "deadline_utc": deadline_utc,   # datetime.isoformat() или None
#       "priority":     priority,
#   })
#
# В конец функции complete_task() / toggle_task():
#
#   from web_sync import sync_task_updated
#   sync_task_updated(chat_id, {"id": task_id, "completed": True})
#
# В конец функции delete_task():
#
#   from web_sync import sync_task_deleted
#   sync_task_deleted(chat_id, task_id)
#
# ──────────────────────────────────────────────────────────────────────
