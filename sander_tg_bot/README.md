# ⚡️ Chronicle Engine v3.0
> A high-end Telegram Task Management System with Flow-state UX

---

## 🗂 File Structure

```
chronicle/
├── bot.py           — Main entry point, command handlers, scheduler setup
├── tasks.py         — Complete Task Manager (FSM, UI, callbacks)
├── db.py            — SQLite persistence layer (tasks + user settings)
├── nlp_parser.py    — Smart NLP task parsing via Groq + regex fallback
├── requirements.txt — Dependencies
└── README.md        — This file
```

---

## 🗃 Database Schema

### `tasks`
| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `user_id` | TEXT | Telegram user ID |
| `text` | TEXT | Task description |
| `status` | TEXT | `todo` / `in_progress` / `done` |
| `priority` | TEXT | `high` / `medium` / `low` |
| `deadline` | TEXT | UTC ISO `YYYY-MM-DDTHH:MM:SS` or NULL |
| `created_at` | TEXT | UTC ISO creation time |
| `completed_at` | TEXT | UTC ISO completion time (or NULL) |
| `archived` | INTEGER | `0` = active, `1` = archived |
| `reminder_24h` | INTEGER | `1` = 24h reminder already sent |
| `reminder_1h` | INTEGER | `1` = 1h reminder already sent |
| `reminder_15m` | INTEGER | `1` = 15m reminder already sent |

### `user_settings`
| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `user_id` | TEXT PK | — | Telegram user ID |
| `timezone` | TEXT | `Asia/Almaty` | pytz timezone string |
| `briefing_hour` | INTEGER | `9` | Local hour for morning briefing |
| `briefing_enabled` | INTEGER | `1` | `1` = enabled |

---

## 🧭 User Flow

```
/tasks
  │
  ├─ Task list (paginated)
  │    │
  │    ├─ Tap [👁 N] ──► Task Detail View
  │    │                    ├─ Toggle status  [🔲 Todo] [🔄 In Progress] [✅ Done]
  │    │                    ├─ Change priority [🔴] [🟡] [🟢]
  │    │                    ├─ [🗑 Delete]
  │    │                    ├─ [📦 Archive]
  │    │                    └─ [↩️ Back]
  │    │
  │    ├─ [➕ Add Task] ──► Type task description (NLP mode)
  │    │                    │
  │    │                    ▼ (AI parses title + deadline + priority)
  │    │                    Task Preview → [🔴🟡🟢 Select Priority] → ✅ Saved
  │    │
  │    ├─ [📅 Calendar] ──► Month Grid
  │    │                    ├─ 🔴🟡🟢✅ per-day emoji indicators
  │    │                    ├─ Tap day ──► Day Tasks View ──► Tap task ──► Detail
  │    │                    └─ ◀️ / ▶️  Navigate months
  │    │
  │    ├─ [📊 Analytics] ──► Stats Dashboard
  │    │                      ├─ This week vs last week completions
  │    │                      ├─ By status / priority breakdown
  │    │                      └─ Archive + all-time totals
  │    │
  │    ├─ [📦 Archive] ──► Archived tasks (read-only, paginated)
  │    │
  │    └─ [🗂 Auto-Clean] ──► Archives all "done" tasks instantly
```

---

## ⚙️ Environment Variables

Create a `.env` file:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
GROQ_API_KEY=your_groq_key_here

# AITU LMS Deadlines
ICAL_URL=https://lms.astanait.edu.kz/calendar/...
DEADLINE_CHAT_ID=your_chat_id
DEADLINE_TZ=Asia/Almaty
DEADLINE_HOUR=8
DEADLINE_MINUTE=0
DAYS_AHEAD=7

# AI auto-reply persona
BOT_PERSONA=Ты отвечаешь вместо владельца. Отвечай кратко.

# Database path (default: tasks.db in project root)
TASKS_DB=tasks.db
```

---

## 📱 Commands Reference

| Command | Description |
|---------|-------------|
| `/tasks` | Open Task Manager |
| `/deadlines` | AITU LMS deadlines |
| `/ai <question>` | Chat with AI |
| `/tz <timezone>` | Set your timezone (e.g. `Asia/Almaty`) |
| `/briefing on\|off [hour]` | Configure morning briefing |
| `/persona <text>` | Set AI auto-reply persona |
| `/status` | System status |
| `/reset` | Clear AI chat history |

---

## ⏰ Reminder System

The bot sends **proactive push notifications** when task deadlines approach:

| Window | Notification |
|--------|-------------|
| ~24h before | 📅 "24 hours until deadline!" |
| ~1h before  | 🔔 "1 hour until deadline!" |
| ~15m before | ⏰ "15 minutes until deadline!" |

- Reminders are **deduplicated** (each fires only once per task per window)
- The check job runs **every 60 seconds** via `job_queue.run_repeating`
- Deadlines stored in UTC; displayed in user's local timezone

---

## 🤖 NLP Smart Parsing

When you type a task like:

```
"Submit the report tomorrow at 3pm"
"URGENT: fix login bug asap"
"Call dentist next Friday"
"Buy groceries in 2 days"
```

The bot uses **Groq (Llama 3)** to extract:
- ✅ Clean task title
- 📅 Parsed deadline (in your local timezone → stored as UTC)
- 🔴 Priority level (inferred from urgency words)

Falls back to **regex heuristics** if Groq is unavailable.

---

## 📦 Migration

On first run, the bot automatically migrates tasks from the old `tasks.json`
format into the new SQLite database, then renames the JSON file to `tasks.json.migrated`.

---

## 🚀 Running

```bash
pip install -r requirements.txt
python bot.py
```
