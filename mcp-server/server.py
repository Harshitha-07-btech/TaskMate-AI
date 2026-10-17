"""ArmorIQ-compatible TaskMate MCP endpoint using JSON-RPC over SSE."""

import hmac
import json
import os
from pathlib import Path
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from supabase import Client, create_client

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / "backend" / ".env")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
db: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    db = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="TaskMate MCP", version="1.0.0")

TOOLS = [
    {
        "name": "list_tasks",
        "description": "List real TaskMate tasks, optionally filtered by status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["all", "pending", "done"],
                    "description": "Task status filter. Defaults to all.",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "create_task",
        "description": "Create a task in the user's TaskMate database.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 250,
                    "description": "A concise title based on the user's request.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional details explicitly supplied by the user.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high"],
                },
                "due_at": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Optional deadline as an ISO-8601 date-time.",
                },
                "scheduled_start": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Optional start of the planned work period.",
                },
                "scheduled_end": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Optional end of the planned work period.",
                },
            },
            "required": ["title"],
            "additionalProperties": False,
        },
    },
    {
        "name": "complete_task",
        "description": "Mark a task as completed by its database ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "ID of the task to complete.",
                }
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_task",
        "description": "Permanently delete a task by its database ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "ID of the task to delete.",
                }
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
]


class TaskCreateInput(BaseModel):
    title: str = Field(min_length=1, max_length=250)
    description: str | None = None
    priority: Literal["low", "normal", "high"] | None = None
    due_at: datetime | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Task title cannot be blank.")
        return value


def require_database() -> Client:
    if db is None:
        raise RuntimeError("Supabase is not configured.")
    return db


def list_tasks(status: str = "all") -> list[dict[str, Any]]:
    if status not in {"all", "pending", "done"}:
        raise ValueError("status must be all, pending, or done")

    query = require_database().table("tasks").select("*").order(
        "created_at", desc=True
    )
    if status != "all":
        query = query.eq("status", status)
    return query.execute().data


def require_task_id(arguments: dict[str, Any], tool: str) -> str:
    if set(arguments) != {"task_id"} or not isinstance(arguments["task_id"], str):
        raise ValueError(f"{tool} requires a string task_id.")
    try:
        return str(uuid.UUID(arguments["task_id"]))
    except ValueError:
        raise ValueError("Task not found.") from None


def deletes_enabled() -> bool:
    # Defense in depth behind ArmorIQ's deny policy: even if that policy were
    # misconfigured, agent deletions stay off unless explicitly enabled here.
    return os.getenv("TASKMATE_MCP_ALLOW_DELETE", "").strip().lower() in {"1", "true", "yes"}


def complete_task(task_id: str) -> dict[str, Any]:
    result = (
        require_database()
        .table("tasks")
        .update({"status": "done", "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", task_id)
        .execute()
    )
    if not result.data:
        raise ValueError("Task not found.")
    return result.data[0]


def delete_task(task_id: str) -> dict[str, Any]:
    if not deletes_enabled():
        raise ValueError("Task deletion is disabled on this MCP server.")
    result = require_database().table("tasks").delete().eq("id", task_id).execute()
    if not result.data:
        raise ValueError("Task not found.")
    return {"deleted": True, "task_id": task_id}


def create_task(arguments: dict[str, Any]) -> dict[str, Any]:
    task = TaskCreateInput.model_validate(arguments)
    if (
        task.scheduled_start is not None
        and task.scheduled_end is not None
        and task.scheduled_end < task.scheduled_start
    ):
        raise ValueError("Planned work end must not be before its start.")
    values = task.model_dump(mode="json", exclude_none=True)
    result = require_database().table("tasks").insert(values).execute()
    if not result.data:
        raise RuntimeError("Task was not returned after saving.")
    return result.data[0]


def execute_tool(name: str, arguments: dict[str, Any]) -> Any:
    if name == "list_tasks":
        if set(arguments) - {"status"}:
            raise ValueError("Unexpected list_tasks argument.")
        return list_tasks(arguments.get("status", "all"))
    if name == "create_task":
        if set(arguments) - {
            "title",
            "description",
            "priority",
            "due_at",
            "scheduled_start",
            "scheduled_end",
        }:
            raise ValueError("Unexpected create_task argument.")
        try:
            return create_task(arguments)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
    if name == "complete_task":
        return complete_task(require_task_id(arguments, name))
    if name == "delete_task":
        return delete_task(require_task_id(arguments, name))
    raise LookupError("Unknown tool.")


def jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def handle_jsonrpc(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return jsonrpc_error(None, -32600, "Invalid JSON-RPC request.")

    request_id = payload.get("id")
    method = payload.get("method")
    if method == "notifications/initialized" and request_id is None:
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "taskmate-mcp", "version": "1.0.0"},
            },
        }
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": TOOLS},
        }
    if method == "tools/call":
        params = payload.get("params")
        if not isinstance(params, dict):
            return jsonrpc_error(request_id, -32602, "Invalid tool parameters.")
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return jsonrpc_error(request_id, -32602, "Invalid tool arguments.")
        try:
            data = execute_tool(name, arguments)
            content = {"result": data}
            is_error = False
        except LookupError:
            return jsonrpc_error(request_id, -32601, "Unknown tool.")
        except ValueError as exc:
            content = {"error": str(exc)}
            is_error = True
        except Exception:
            content = {"error": "Task operation failed."}
            is_error = True
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(content, default=str)}],
                "isError": is_error,
            },
        }
    return jsonrpc_error(request_id, -32601, "Method not found.")


def require_bearer_auth(request: Request) -> None:
    expected = os.getenv("TASKMATE_MCP_BEARER_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="MCP authentication is not configured.")
    supplied = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not supplied.startswith(prefix) or not hmac.compare_digest(
        supplied[len(prefix) :], expected
    ):
        raise HTTPException(status_code=401, detail="Unauthorized.")


@app.get("/health")
def health():
    return {"status": "ok", "service": "TaskMate MCP", "supabase_configured": db is not None}


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    require_bearer_auth(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body.") from exc

    response_data = handle_jsonrpc(payload)
    if response_data is None:
        return Response(status_code=202)

    async def stream():
        yield f"event: message\ndata: {json.dumps(response_data, default=str)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8001")),
    )
