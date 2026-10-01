# ✨ Chronicle Telegram Bot
Task manager, habit tracker, calendar, reminders, and optional AI helpers.

---

## 🗂 File Structure

```
chronicle/
├── bot.py           — Main entry point, command handlers, scheduler setup
├── tasks.py         — Complete Task Manager (FSM, UI, callbacks)
├── db.py            — PostgreSQL persistence layer shared with the website
├── nlp_parser.py    — Smart NLP task parsing via Groq + regex fallback
├── requirements.txt — Dependencies
└── README.md        — This file
```

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

Copy `.env.example` to `.env` and fill in the values. Keep `.env` private; never commit tokens, calendar links, or database credentials.

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

# Shared PostgreSQL database used by the website
DATABASE_URL=postgresql://user:password@host:5432/database
```

`ICAL_URL` is optional. Set it only to a calendar URL you control; users can also add a personal calendar with `/add_deadline`.

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

## 🚀 Running

```bash
pip install -r requirements.txt
python bot.py
```
