# TaskMate MCP Server

This service exposes the TaskMate task tools in the HTTP JSON-RPC/SSE format required by ArmorIQ. `POST /mcp` requires a bearer token for every request.

## Tools and policy

| Tool | Behavior | ArmorIQ policy |
| --- | --- | --- |
| `list_tasks` | Reads real rows from Supabase and supports `all`, `pending`, and `done` filters. | Allowed |
| `create_task` | Validates task fields and inserts the row into Supabase. | Allowed |
| `delete_task` | Deletes a task by ID. | Denied |

The MCP endpoint implements `initialize`, `tools/list`, and `tools/call`. The server returns 503 until `TASKMATE_MCP_BEARER_TOKEN` is configured; it never starts as an unauthenticated task endpoint.

## Local startup

Set `SUPABASE_URL`, `SUPABASE_KEY`, and a random `TASKMATE_MCP_BEARER_TOKEN` in the ignored `backend/.env`, then run from the repository root:

```powershell
.\backend\.venv\Scripts\python.exe mcp-server/server.py
```

The service listens on port 8001 locally. The `/mcp` route rejects requests unless `TASKMATE_MCP_BEARER_TOKEN` is set and supplied as `Authorization: Bearer ...`.

## Render deployment

The root [`render.yaml`](../render.yaml) describes this existing service. In Render, create a Blueprint from the repository and provide these values for the MCP web service:

| Variable | Purpose |
| --- | --- |
| `SUPABASE_URL` | Supabase project URL. |
| `SUPABASE_KEY` | Supabase server-side key used by the task tools. Keep it in Render's secret environment, never in the Blueprint or frontend. |
| `TASKMATE_MCP_BEARER_TOKEN` | Shared bearer credential required for every `/mcp` request. Use the same secret for the ArmorIQ MCP auth configuration. |

Render supplies `PORT` automatically. The Blueprint starts Uvicorn bound to `0.0.0.0` on that port. Do not set `PORT` yourself.

After the first deployment, check the public `/openapi.json` health path and test `/mcp` using an authenticated JSON-RPC request. A request without the bearer token must receive HTTP 401. The MCP URL for later ArmorIQ validation is `https://<your-render-service>.onrender.com/mcp`; update `armoriq.yaml` only after Render provides the service hostname. The current checked-in ArmorIQ URL is local, and this step does not register or deploy the service.

## Test and demo

Run offline tests without changing Supabase data:

```powershell
.\backend\.venv\Scripts\python.exe -m unittest discover -s mcp-server -p "test_*.py"
.\backend\.venv\Scripts\python.exe -m unittest discover -s backend -p "test_*.py"
```

After configuring an ArmorIQ account, registering the reachable endpoint, and setting the required environment variables, run the live policy check:

```powershell
.\backend\.venv\Scripts\python.exe scripts/security_demo.py
```

The demo reads tasks through the allowed tool and probes the denied tool with the all-zero UUID. It reports a block only when the ArmorIQ SDK raises `PolicyBlockedException`; it does not use a real task ID.
