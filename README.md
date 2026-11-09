<div align="center">

# 🤖 TaskMate AI

**An AI-powered task manager secured by ArmorIQ policy enforcement**

<p>
  <img src="https://img.shields.io/badge/Python-3.13-blue?style=for-the-badge&logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/React-18-61DAFB?style=for-the-badge&logo=react&logoColor=black"/>
  <img src="https://img.shields.io/badge/Gemini-AI-4285F4?style=for-the-badge&logo=google&logoColor=white"/>
  <img src="https://img.shields.io/badge/Supabase-3ECF8E?style=for-the-badge&logo=supabase&logoColor=white"/>
  <img src="https://img.shields.io/badge/ArmorIQ-Secured-FF6B35?style=for-the-badge"/>
  <img src="https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge"/>
</p>

| 🌐 Live App | 🎬 Demo Video | 📝 Medium Article | 💻 GitHub |
|:---:|:---:|:---:|:---:|
| [Open TaskMate](https://taskmate-frontend-5kp9.onrender.com) | [Watch Demo](https://youtu.be/YOUR_VIDEO_ID) | [Coming Soon](#) | [View Code](https://github.com/Harshitha-07-btech/TaskMate-AI) |

> Built for the **ArmorIQ Flow State Hackathon**

</div>

---

## 💡 What Is This?

TaskMate AI lets you manage your tasks by **chatting**. Tell the AI to add a task, list your tasks, or mark something complete — it understands and acts.

But here's the twist: **every action the AI wants to take is checked by ArmorIQ before anything happens.** If the policy says no, the AI is blocked — even if you ask it to delete a task.

You can see the security decision right in the chat:
- ✅ Green → *Authorized by ArmorIQ*
- 🔴 Red → *Blocked by ArmorIQ policy*

---

## ✨ Features

- 🤖 **Chat with AI** — powered by Google Gemini, understands natural language
- 🔒 **ArmorIQ Security** — every AI action is checked against a policy before it runs
- 🚫 **Allowed vs Blocked** — AI can create tasks, but cannot delete them (policy blocks it)
- 📡 **Real MCP Server** — a proper Model Context Protocol server connected to live data
- 🗄️ **Supabase Database** — real PostgreSQL storage, not mock data
- ⏰ **Smart Reminders** — deadline alerts with "Completed / Snooze / Change deadline" options
- 📊 **Task Board** — priority, due dates, overdue badges, calendar view

---

## 🛠️ Built With

| Technology | Purpose |
|---|---|
| **React + Vite** | Frontend UI |
| **Python FastAPI** | Backend API |
| **Google Gemini** | AI chat agent |
| **Supabase (PostgreSQL)** | Database |
| **ArmorIQ** | Security policy enforcement |
| **MCP Protocol** | Secure tool layer between AI and database |

---

## 🔒 How ArmorIQ Works Here

The AI decides what to do → ArmorIQ checks the policy → action is allowed or blocked.

```
User: "Delete my task"
  ↓
Gemini: wants to call delete_task
  ↓
ArmorIQ: checks policy → ❌ BLOCKED (delete_task is in the deny list)
  ↓
Chat shows: "Blocked by ArmorIQ policy"
Task: untouched in the database
```

**Policy file (`armoriq.yaml`):**

```yaml
policy:
  allow:
    - taskmate-mcp.list_tasks     # ✅ AI can read tasks
    - taskmate-mcp.create_task    # ✅ AI can add tasks
    - taskmate-mcp.complete_task  # ✅ AI can mark tasks done
  deny:
    - taskmate-mcp.delete_task    # 🚫 AI can NEVER delete
```

Every decision is recorded as a trace on the [ArmorIQ Dashboard](https://platform.armoriq.ai/dashboard/observability/traces).

---

## 🗄️ Database (Supabase)

Three tables in PostgreSQL:

| Table | What it stores |
|---|---|
| `tasks` | Your tasks — title, priority, status, due date |
| `task_reminders` | Reminder slots for each task deadline |
| `reminder_preferences` | Your timezone and notification settings |

The database key never reaches the browser. Only the backend can write to it.

---

## 🚀 Run It Locally

```bash
# 1. Backend
cd backend
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env    ← fill in your keys
uvicorn main:app --port 8000

# 2. MCP Server (new terminal)
cd mcp-server
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
uvicorn server:app --port 8001

# 3. Frontend (new terminal)
cd frontend
npm install
npm run dev    ← opens at http://localhost:5173
```

**You need:** Python 3.13, Node 18+, a [Supabase](https://supabase.com) project, a [Gemini API key](https://aistudio.google.com), and an [ArmorIQ account](https://armoriq.ai).

---

## 📂 Project Structure

```
taskmate-ai/
├── frontend/src/       React UI — chat, tasks, reminders
├── backend/
│   ├── main.py         API + Gemini agent
│   └── armoriq_runtime.py   ArmorIQ session per action
├── mcp-server/
│   └── server.py       MCP server (talks to Supabase)
├── armoriq.yaml        Security policy (allow/deny rules)
└── supabase/migrations/    Database setup SQL
```

---

## 📄 License

MIT — see [LICENSE](LICENSE) for details.

---

<div align="center">

**Built with ❤️ for the [ArmorIQ Flow State Hackathon](https://armoriq.ai)**

Made by [Harshitha](https://github.com/Harshitha-07-btech)

<br/>

[🌐 Live App](https://taskmate-frontend-5kp9.onrender.com) &nbsp;·&nbsp;
[💻 GitHub](https://github.com/Harshitha-07-btech/TaskMate-AI) &nbsp;·&nbsp;
[📝 Medium](https://medium.com) &nbsp;·&nbsp;
[🔒 ArmorIQ](https://armoriq.ai)

</div>
