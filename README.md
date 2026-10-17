# TaskMate AI

> A secure, AI-powered task manager built for the hackathon.

## Overview

TaskMate AI lets users manage tasks through a regular task board and a natural-language chat. Every task action the AI takes goes through **ArmorIQ**: the backend captures a plan, gets a signed intent token, and invokes the MCP tool through the ArmorIQ proxy. If the token, the policy, or the proxy call doesn't confirm the action, the user is told it did not happen. Nothing is faked as success.

## Features

- 🤖 **AI chat** (Gemini function calling) that can create and list tasks
- ✅ **Task board** with priority, due dates, planned start/end, document links, and complete/reopen/delete
- 📅 **Calendar view and overlap detection** for scheduled tasks (computed in the frontend)
- 🔒 **ArmorIQ policy enforcement** on AI task actions: `list_tasks` and `create_task` are allowed, `delete_task` is denied
- 🗄️ **Supabase (PostgreSQL)** storage
- 🔌 **MCP server** exposing `list_tasks`, `create_task`, and `delete_task`, protected by a bearer token

Not implemented yet: push/email reminders. Deadlines appear as "upcoming" in the UI only.

## Architecture

```
React + Vite (frontend, :5173)
   │  REST: /tasks CRUD, /agent/chat
   ▼
FastAPI backend (backend/main.py, :8000)
   ├── /tasks*        ──────────────────────────────► Supabase `tasks` table
   └── /agent/chat ─► Gemini (picks one tool call)
                       │
                       ▼  capture_plan → get_intent_token → invoke
                   ArmorIQ (api.armoriq.ai token issuance, proxy.armoriq.ai)
                       │  policy check + CSRG proof, bearer auth to MCP
                       ▼
                   TaskMate MCP server (mcp-server/server.py, Render) ─► Supabase
```

The manual task board talks to Supabase directly through the backend. Only AI-initiated actions go through ArmorIQ and the MCP server.

| Layer      | Technology                         |
|------------|------------------------------------|
| Frontend   | React 18 + Vite 6                  |
| Backend    | Python, FastAPI, google-genai      |
| Database   | Supabase (PostgreSQL)              |
| AI tools   | MCP server (FastAPI app, `/mcp`)   |
| Security   | ArmorIQ SDK 0.6.10 (`armoriq.yaml`)|

## Project structure

```
taskmate-ai/
├── frontend/            React + Vite app (src/App.jsx)
├── backend/             FastAPI API + Gemini chat + ArmorIQ client
│   └── test_agent_security.py
├── mcp-server/          MCP server with the task tools
│   └── test_server.py
├── scripts/
│   └── security_demo.py Live ArmorIQ allow/deny check
├── armoriq.yaml         ArmorIQ identity, MCP registration, and policy
└── render.yaml          Render blueprint for the MCP server
```

## Environment variables (names only; never commit values)

**`backend/.env`**

| Name | Purpose |
|------|---------|
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_KEY` | Supabase key used by the backend |
| `GEMINI_API_KEY` | Google Gemini API key |
| `GEMINI_MODEL` | Optional; defaults to `gemini-3.8-flash` |
| `ARMORIQ_API_KEY` | ArmorIQ API key (`ak_live_…`), referenced by `armoriq.yaml` |
| `TASKMATE_MCP_BEARER_TOKEN` | Shared bearer token for the MCP server, referenced by `armoriq.yaml` |
| `TASKMATE_MCP_URL` | Deployed MCP URL (reference only; `armoriq.yaml` holds the literal URL) |

**MCP server (Render dashboard or a local shell):** `SUPABASE_URL`, `SUPABASE_KEY`, `TASKMATE_MCP_BEARER_TOKEN` (must be the same value as the backend's).

**Frontend (optional):** `VITE_API_BASE_URL`. Defaults to `http://127.0.0.1:8000`.

## Supabase table

The code expects a `tasks` table with these columns: `id` (uuid, default generated), `title` (text), `description` (text), `status` (`pending`/`done`, default `pending`), `priority` (`low`/`normal`/`high`), `due_at`, `scheduled_start`, `scheduled_end` (timestamptz, nullable), `document_url` (text, nullable), `created_at` (timestamptz, default `now()`), `updated_at` (timestamptz).

## Getting started

Prerequisites: Python 3.13, Node 18+, a Supabase project, a Gemini API key, and an ArmorIQ account.

```bash
# Backend
cd backend
python -m venv .venv
.venv\Scripts\activate           # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
# create backend/.env with the variables above
uvicorn main:app --reload --port 8000

# MCP server (local run; production runs on Render via render.yaml)
cd mcp-server
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn server:app --port 8001

# Frontend
cd frontend
npm install
npm run dev                      # http://localhost:5173
```

### ArmorIQ setup

1. `armoriq.yaml` must use `environment: production` for the hosted ArmorIQ service. In SDK 0.6.10, `sandbox` makes `ArmorIQClient.from_config` use a *local* ArmorIQ dev stack (`http://127.0.0.1:3000` / `:8080`). Token issuance then fails with `InvalidTokenException`.
2. Deploy the MCP server (Render, `render.yaml`) and set its environment variables.
3. From the repo root, with `ARMORIQ_API_KEY` and `TASKMATE_MCP_BEARER_TOKEN` exported: `armoriq validate`, then `armoriq register`.

## Testing

```bash
cd backend && .venv\Scripts\python -m unittest -v test_agent_security
cd mcp-server && .venv\Scripts\python -m unittest -v test_server
cd frontend && npm run build     # no frontend unit tests; the build is the check
```

The backend tests mock Gemini and ArmorIQ. They check that task actions fail closed: a blocked or held action, a missing config, an invalid token, an intent mismatch, or an MCP failure never produces a success reply.

## Hackathon demo

1. Start the backend and frontend. The MCP server must be live on Render.
2. **Allowed action:** in chat, type *"Show my tasks"* (`list_tasks`) or *"Add a task to submit the hackathon demo tomorrow"* (`create_task`). The reply lists or confirms the task, and a created task appears on the board.
3. **Blocked action:** run `python scripts/security_demo.py` from the repo root with the backend venv. `delete_task` (on an all-zero test UUID) is rejected by ArmorIQ policy with `PolicyBlockedException`. The Gemini chat never offers `delete_task` at all.
4. Show `armoriq.yaml`'s `policy` block and the ArmorIQ dashboard logs as evidence.

## License

MIT
