# TaskMate AI

> A task manager with an AI assistant whose every task action is authorized by ArmorIQ, plus deadline reminders that check in like a mentor would.

## The problem

Students and small teams lose track of deadlines, and AI assistants that can change data raise an obvious question: what stops the assistant from doing something it shouldn't? TaskMate lets you manage tasks by chatting, while ArmorIQ policy decides, before anything reaches your data, which actions the AI may perform. It then reminds you about deadlines ("You have a task due tomorrow. Have you completed it?").

## Features

- 🤖 **AI chat** (Gemini function calling): list, create, and complete tasks, and *request* deletions, which policy blocks.
- 🔒 **ArmorIQ enforcement + observability**: each AI action gets an intent token, goes through the ArmorIQ proxy, and ships a trace with the policy decision.
- ✅ **Task board**: priority, deadline, planned period, document link, complete/reopen/delete, overdue and due-soon badges.
- ⏰ **Deadline reminders**: persistent, time-zone-aware, duplicate-safe, and actionable (Completed / Not yet / Remind me later / Change deadline).
- 💬 **Optional WhatsApp delivery** through the official WhatsApp Business Platform (Cloud API). It is off and clearly labeled "not configured" until you set it up.
- 📅 Calendar view, document links, and schedule-overlap detection.

## Architecture

```
React + Vite (frontend)
   │  REST: /tasks CRUD, /reminders*, /agent/chat
   ▼
FastAPI backend (backend/main.py)
   ├── /tasks*, /reminders* ───────────────────────────► Supabase (tasks, task_reminders, reminder_preferences)
   ├── reminder scheduler (in-process loop + /internal/reminders/run for an external cron)
   └── /agent/chat ─► Gemini picks at most one tool call
                       │  backend validates arguments, resolves task titles to IDs
                       ▼  one ArmorIQSession per action (armoriq_runtime.py)
                   ArmorIQ: token issuance (api.armoriq.ai) → proxy enforcement (proxy.armoriq.ai)
                       │  policy check + CSRG proof, bearer auth to MCP     └─► trace → /observability/spans
                       ▼
                   TaskMate MCP server (mcp-server/server.py, Render) ─► Supabase
```

| Component | Role |
|---|---|
| **Gemini** | Understands the chat message and proposes one tool call (`list_tasks`, `create_task`, `complete_task`, `delete_task`) or answers in text. It never touches data directly. |
| **ArmorIQ** | Issues an intent token for exactly that action, enforces policy at the proxy, and records the decision in an observability trace. |
| **MCP server** | The only component that changes data on the AI's behalf. Requires a bearer token on every request. |
| **Supabase** | PostgreSQL storage. The backend and MCP server use a server-side key that never reaches the browser. |

The manual task board and the reminder buttons are direct user actions, so they call the backend routes rather than going through ArmorIQ. ArmorIQ governs the **AI agent**.

## How ArmorIQ enforcement works

Policy (`armoriq.yaml`):

| Tool | Policy | Why |
|---|---|---|
| `list_tasks` | allow | read-only |
| `create_task` | allow | additive |
| `complete_task` | allow (after re-registration, see below) | reversible status change |
| `delete_task` | **deny** | permanent; the AI may never delete |

For every AI action (`backend/armoriq_runtime.py`):

1. `client.start_session(mode="proxy")` opens a session whose ID is the request's correlation ID.
2. `session.start_plan([ToolCall(action, params)])` captures the plan and requests an intent token. Policy can deny here (`PolicyBlockedException` at issuance).
3. `client.invoke(...)` sends the action with its CSRG proof to the ArmorIQ proxy, which evaluates policy **before** forwarding to the MCP server. A denial raises `PolicyBlockedException`, and the MCP server is never called.
4. The session is closed off the request path (`session.close()`), which ends the plan trace and flushes it to ArmorIQ.

Defense in depth: the MCP server also refuses `delete_task` unless `TASKMATE_MCP_ALLOW_DELETE=true`. This guard only matters if the ArmorIQ policy were misconfigured, and such a refusal is reported as an MCP error, not as an ArmorIQ denial.

The backend reports each outcome with a distinct `error_type`, so a security denial is never confused with an outage:

| `error_type` | HTTP | Meaning |
|---|---|---|
| `policy_denied` / `policy_hold` | 403 | ArmorIQ evaluated the action and did not allow it |
| `token_issuance` | 502 | ArmorIQ could not issue an intent token |
| `auth_failed` | 502 | ArmorIQ's proxy or the MCP server rejected service credentials |
| `intent_mismatch` | 409 | the action did not match its captured plan |
| `timeout` | 504 | the MCP server did not answer in time (often a sleeping free instance) |
| `mcp_error` / `mcp_tool_error` | 502 | the MCP call or tool failed |
| `configuration` | 503 | ArmorIQ is not configured; protected actions fail closed |
| `unexpected` | 500 | anything else; nothing is confirmed |

### Observability

**Why the Traces page was empty before:** in `armoriq-sdk` 0.6.10, `ArmorIQClient.invoke()` emits no telemetry. Only `ArmorIQSession` creates traces. The old code called `capture_plan` / `get_intent_token` / `invoke` directly, so nothing was ever shipped. The proxy still enforced policy, which is consistent with the dashboard counting denied decisions while showing no traces. Which runs produced those nine denials has not been verified (likely earlier `security_demo.py` runs, but that is unconfirmed).

Each AI action now produces one `iap.plan` trace containing:

- `iap.plan.start` span (token issuance)
- `generation` span with Gemini token counts only (no prompt or completion text)
- `taskmate.mcp.invoke` span: MCP id, tool name, argument **names**, duration, status
- `policy_call` span: `allow` / `deny` / `hold`, source (`proxy` or `sdk`), and ArmorIQ's reason. It is recorded only from a real ArmorIQ outcome; timeouts and errors are never recorded as denials.
- Trace status `ok`, `denied`, or `error`.

The trace and session IDs are returned in the chat response (`trace_id`, `request_id`), shown under each AI reply, and written to the backend log. The SDK's own `session.check()` / `dispatch()` copy raw tool arguments into spans, so TaskMate uses its own redacted span instead.

Note that the plan sent to ArmorIQ for token issuance does contain the task parameters, because ArmorIQ binds the token to them.

**Finding traces:** open <https://platform.armoriq.ai/dashboard/observability/traces> in the organization that owns `ARMORIQ_API_KEY`. The Trace Explorer shows the last 7 days and the first 100 traces, and polls every 30 seconds. Filter Status = **Denied** to find blocked actions. `ARMORIQ_OBSERVABILITY=false` disables shipping.

## Deadline reminders

- **Slots** per open task with a deadline, in the user's time zone: a heads-up `before_deadline_hours` before it (default 24), a deadline-day reminder at `deadline_day_hour` (default 09:00), and overdue reminders every `overdue_interval_hours` (default 24), up to `max_overdue_reminders` (default 3).
- **Idempotent:** each reminder has a unique key `task:kind:sequence:deadline:channel`, so restarts, repeated runs, and several workers never duplicate. Only the most recent due slot is posted, so a service that slept for days shows one current reminder instead of a backlog.
- **Lifecycle:** completing or deleting a task cancels its reminders. Changing the deadline marks old reminders `superseded`, and new ones follow the new deadline. A heads-up whose time passed before the task was created is skipped.
- **Interaction:** Completed (marks the task done), Not yet, Remind me in 1h / tomorrow (snooze), Change deadline, Open task, Dismiss. Reminder responses use the same task-update code path as the task board.
- **Scheduling:** an in-process loop (`REMINDER_SCHEDULER_INTERVAL_SECONDS`, default 300) while the API is awake. `GET /reminders` catches up whenever the app is opened. `POST /internal/reminders/run` (header `X-Reminder-Cron-Secret`) is for an external scheduler; `.github/workflows/reminders.yml` calls it every 30 minutes once its secrets are set.
- **Storage:** `supabase/migrations/20261017000000_task_reminders.sql` (additive; RLS enabled with no browser policies). Until it is run, reminder endpoints return 503 with that instruction, and everything else keeps working.

### Optional WhatsApp delivery

Implemented with the official **WhatsApp Business Platform Cloud API** (`backend/notifications.py`). Nothing is sent unless all of these are true:

1. The server has `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, and `WHATSAPP_TEMPLATE_NAME`.
2. The user entered a number in `+country` format, ticked the consent box, and enabled WhatsApp (consent time is stored, and turning it off clears it).
3. The reminder is less than 24 hours old.

Messages use an **approved template** with two body variables, `{{1}}` = task title and `{{2}}` = due time. Business-initiated messages outside a 24-hour customer window require approved templates. A reminder is marked `sent` only when the Cloud API returns a message ID; that means accepted by Meta, not a delivery receipt. Retryable errors (429/5xx) back off up to 3 attempts. Without configuration, rows are recorded as `skipped` and the UI says "Not configured". Replying to reminders over WhatsApp (webhooks) is not implemented.

Setup (your Meta account, no code changes):

1. Create a Meta developer app with the WhatsApp product and a WhatsApp Business Account. Note the **Phone number ID**.
2. Create a permanent access token (System User with `whatsapp_business_messaging`).
3. Create a **Utility** message template, for example: `Reminder: "{{1}}" is due {{2}}. Have you completed it? Open TaskMate to update it.` Wait for approval.
4. Set `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_TEMPLATE_NAME`, and optionally `WHATSAPP_TEMPLATE_LANGUAGE` (default `en`) and `WHATSAPP_GRAPH_API_VERSION` (default `v21.0`; use the current version Meta lists) on the API service.
5. In test mode, Meta only delivers to numbers you have added as test recipients.

## Project structure

```
taskmate-ai/
├── frontend/src/        App.jsx, Reminders.jsx (reminder center + settings), api.js
├── backend/
│   ├── main.py              FastAPI routes, Gemini chat, error mapping, scheduler
│   ├── armoriq_runtime.py   per-action ArmorIQ session, redacted spans, error taxonomy
│   ├── reminders.py         reminder scheduling, persistence, delivery
│   ├── notifications.py     WhatsApp Cloud API provider
│   └── test_*.py
├── mcp-server/          MCP server (list/create/complete/delete tools) + tests
├── supabase/migrations/ reminder schema
├── scripts/security_demo.py  live allow/deny check with trace IDs
├── armoriq.yaml         ArmorIQ identity, MCP registration, policy
├── render.yaml          Render Blueprint: web (static), API, MCP
└── HACKATHON_EVIDENCE.md
```

## Local setup

Prerequisites: Python 3.13, Node 18+, a Supabase project, a Gemini API key, and an ArmorIQ account.

```bash
# Backend
cd backend
python -m venv .venv
.venv\Scripts\activate           # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env           # then fill in values
uvicorn main:app --reload --port 8000

# MCP server (local run; production runs on Render)
cd mcp-server
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
uvicorn server:app --port 8001

# Frontend
cd frontend
npm install
npm run dev                      # http://localhost:5173 (dev falls back to http://127.0.0.1:8000)
```

Run the reminder migration once in the Supabase SQL editor: `supabase/migrations/20261017000000_task_reminders.sql`.

## Environment variables (names only)

**API service (`backend/.env` locally):** `SUPABASE_URL`, `SUPABASE_KEY`, `GEMINI_API_KEY`, `GEMINI_MODEL` (optional), `ARMORIQ_API_KEY`, `TASKMATE_MCP_BEARER_TOKEN`, `CORS_ALLOWED_ORIGINS`, `TASKMATE_DEFAULT_TIMEZONE` (optional), `REMINDER_SCHEDULER_ENABLED`, `REMINDER_SCHEDULER_INTERVAL_SECONDS`, `REMINDER_CRON_SECRET`, `LOG_LEVEL`, `ARMORIQ_OBSERVABILITY` (optional), and optionally `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_TEMPLATE_NAME`, `WHATSAPP_TEMPLATE_LANGUAGE`, `WHATSAPP_GRAPH_API_VERSION`.

**MCP service:** `SUPABASE_URL`, `SUPABASE_KEY`, `TASKMATE_MCP_BEARER_TOKEN` (same value as the API's), and optionally `TASKMATE_MCP_ALLOW_DELETE` (leave unset).

**Frontend (build time):** `VITE_API_BASE_URL` only. Never put server credentials in `VITE_` variables, because they are embedded in the public bundle. Production builds have no localhost fallback.

## Testing

```bash
cd backend && .venv\Scripts\python -m unittest -v test_agent_security test_reminders
.\backend\.venv\Scripts\python.exe -m unittest discover -s mcp-server -p "test_*.py"
cd frontend && npm run build
backend\.venv\Scripts\python.exe scripts\security_demo.py   # live: needs ArmorIQ + deployed MCP
```

The agent tests drive real `ArmorIQSession` objects against a fake ArmorIQ service and inspect the exact telemetry payload. They check allow/deny/hold paths, that denied actions never reach the tool or change data, distinct error types, redaction, and that every session is closed. The reminder tests cover scheduling, idempotency, cancellation, deadline changes, time zones (including DST), opt-in, delivery retries, and the API.

## Deployment (Render, single provider)

Render hosts all three pieces from `render.yaml`: `taskmate-web` (static site), `taskmate-api` (FastAPI), and `taskmate-mcp`. Supabase, Gemini, ArmorIQ, and (optionally) Meta remain external SaaS by nature.

1. Run the reminder migration in Supabase.
2. Render → **New → Blueprint** → select this repository. Render shows the existing `taskmate-mcp` plus the new `taskmate-api` and `taskmate-web`.
3. Enter the secret values when prompted. `CORS_ALLOWED_ORIGINS` = the web URL, and `VITE_API_BASE_URL` = the API URL. If URLs aren't known yet, deploy once, then set both and redeploy (the frontend must be rebuilt after changing `VITE_API_BASE_URL`).
4. Auto-deploy is off: trigger **Manual Deploy** for each service. Check `GET /health` on the API and MCP.
5. After the MCP redeploy (which adds `complete_task`), from the repo root with `ARMORIQ_API_KEY` and `TASKMATE_MCP_BEARER_TOKEN` exported, run `armoriq validate` then `armoriq register`. `armoriq status` currently reports the old registration as `environment: sandbox`; re-registering also records `production`.
6. Optional: add repository secrets `TASKMATE_API_URL` and `REMINDER_CRON_SECRET` (copy it from the API service) to enable the GitHub Actions reminder trigger.

**Free-tier limits:** free web services sleep after about 15 minutes idle and take up to about a minute to wake. The first AI action after a sleep may return `timeout` (MCP) or be slow, so retry. The in-process reminder loop does not run while asleep; the app catches up when opened, and the GitHub Actions trigger covers the gap. Render Cron Jobs are a paid feature and are not used. Free instances also have monthly usage limits; check Render's current pricing.

## Current limitations

- **No user accounts.** TaskMate is a single shared workspace, and the API is unauthenticated: anyone who knows the API URL can read and change tasks. Real per-user isolation needs Supabase Auth (or similar), a `user_id` column with RLS, and the API verifying user tokens. Do this before sharing a public deployment with real data.
- `complete_task` via chat is denied until the new MCP is deployed and `armoriq register` is re-run (verified: the current registration returns `tool_not_in_allowlist`).
- WhatsApp needs your Meta setup; delivery receipts and replies over WhatsApp are not implemented.
- The reminder tables are not created until you run the migration.

## License

MIT
