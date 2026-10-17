# TaskMate AI: ArmorIQ evidence guide

Everything below is either **verified** (observed during development, on 2026-10-17) or a **step you perform** to capture evidence. No screenshots or trace contents are invented here.

## What was verified on 2026-10-17 (live services, local backend)

| Check | Result | IDs (real, returned by the services) |
|---|---|---|
| Unauthenticated `POST /mcp` on Render | HTTP 401 | n/a |
| Chat "Show my open tasks" → `list_tasks` | 200, allowed, real task list | trace `5926ff3b-bb56-4317-bb93-f15d92df3a04` |
| Chat "Add a task called ArmorIQ denial demo" → `create_task` | 200, allowed, row persisted in Supabase | trace `f4af70d0-4fdb-40fa-8d51-da15064e681f`, task `bcfa44cf-ffe2-476c-ab6c-3c9d132b042f` |
| Chat "Delete the task ArmorIQ denial demo" → `delete_task` | **403 `policy_denied`**, reason `tool_not_in_allowlist`, blocked at the proxy | trace `4e74b21d-2ac8-49f5-99fd-850d03212cc4` |
| Demo task after the denied delete | still present, `pending`, `updated_at` unchanged (`2026-10-17T13:14:52.68856+00:00`) | task `bcfa44cf-…` |
| Chat "Mark … as done" → `complete_task` | 403 `policy_denied` (`tool_not_in_allowlist`): expected until the new MCP is deployed and re-registered | trace `e2ead3ce-631d-4601-acde-aae58c6eb10c` |
| `scripts/security_demo.py` | PASS: list allowed, delete (all-zero UUID) denied | traces `50a28502-bc15-479e-a879-ea9abb7ede4b` (allow), `bc692e78-a515-4723-8e43-d01c66b7af9c` (deny) |
| Trace ingest | `POST https://api.armoriq.ai/observability/spans` → HTTP 200; a probe recorded `{"accepted":1,"rejected":0}` | probe traces `330bea53-…`, `cbac7083-…` |

**Not yet verified:** that these traces are *visible* in the dashboard (this needs your login), and which runs produced the nine earlier denials on the overview chart.

The demo task "ArmorIQ denial demo" was created for this evidence and is still in your task list. Delete it from the task board whenever you like; the board's delete is a direct user action, not an AI action.

## Demo checklist

- [ ] API and MCP `/health` return `ok` (wake free instances a minute before the demo).
- [ ] Reminder migration has been run in Supabase (the Reminders tab loads without a "tables are missing" message).
- [ ] Logged in to <https://platform.armoriq.ai> in the organization that owns `ARMORIQ_API_KEY`.
- [ ] `armoriq.yaml` open at the `policy:` block.

## 1. Allowed action

1. In the chat, type **"Add a task called Demo submission due tomorrow"**.
2. Expect: the reply *Added "Demo submission…" to your tasks*, the line **Authorized by ArmorIQ · create_task · trace `<id>`** under it, and the task appearing on the board.
3. Copy the trace ID shown under the reply.

## 2. Denied action

1. In the chat, type **"Delete the task Demo submission"**.
2. Expect: a red bubble labeled **Blocked by ArmorIQ policy** with *Nothing was changed* and **Reason: tool_not_in_allowlist · trace `<id>`**.
3. Refresh the board (↻): the task is still there with the same status.
4. Optional terminal proof: `backend\.venv\Scripts\python.exe scripts\security_demo.py` prints both outcomes with session and trace IDs.

## 3. Dashboard evidence to capture

Open **Observability → Traces** (<https://platform.armoriq.ai/dashboard/observability/traces>). It shows the last 7 days and the first 100 traces, and refreshes about every 30 seconds.

- [ ] Screenshot of the trace list showing an `iap.plan` trace with status **OK** and one with status **Denied** (use the Status filter).
- [ ] Open the denied trace and capture its spans: `iap.plan.start`, `taskmate.mcp.invoke` (status `denied`), and the `policy_call` span with **decision `deny`**, **source `proxy`**, **reason `tool_not_in_allowlist`**.
- [ ] Open the allowed trace and capture its `policy_call` span with decision `allow`.
- [ ] Note that the trace IDs match the ones shown in the chat. The session ID equals the `request_id` in the backend log.
- [ ] Overview page showing the decision counts.

If the traces do not appear: confirm the dashboard organization matches the API key, wait for the next poll, and check the backend log for `[observability] shipper got 4xx` warnings. Each trace ID is also in the backend log line `request=… trace=…`.

## 4. Screenshots to include

1. Chat with the allowed reply and its ArmorIQ line.
2. Chat with the blocked reply.
3. Task board after the denied delete (task still present).
4. ArmorIQ trace list (OK + Denied) and the denied trace's `policy_call` span.
5. `armoriq.yaml` policy block.
6. Terminal output of `scripts/security_demo.py`.
7. Reminders tab with a reminder card and the "WhatsApp not configured" notice.

## 5. Reminder demo (optional)

1. Create a task due in about 2 hours on the board.
2. Open the **Reminders** tab: a "Due soon" card appears ("… is due today at … Have you completed it?").
3. Click **Remind me in 1h** (the card disappears until then), or **Completed** (the task is marked done and the card clears).
4. Settings show the time zone and that WhatsApp is not configured. No message is claimed as sent.

## What remains unverified

- Dashboard visibility of the traces listed above (requires your login).
- Origin of the nine earlier denials on the overview chart.
- `complete_task` being allowed (needs the MCP redeploy and `armoriq register`).
- The reminder flow against real Supabase tables (needs the migration). It is covered by automated tests only.
- The deployed Render frontend and API (nothing has been deployed by this work).
- WhatsApp delivery (no Meta account configured).
