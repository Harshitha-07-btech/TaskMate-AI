from fastapi.middleware.cors import CORSMiddleware
import json
import logging
import os
import threading
from datetime import date, datetime, time, timezone
from typing import Literal

import httpx
from armoriq_sdk import (
    ArmorIQClient,
    ArmorIQException,
    ConfigurationException,
    PolicyBlockedException,
    PolicyHoldException,
)
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from google import genai
from google.genai import errors as genai_errors
from pydantic import BaseModel, Field, ValidationError, field_validator
from supabase import Client, create_client

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
ARMORIQ_CONFIG = os.path.join(PROJECT_ROOT, "armoriq.yaml")
logger = logging.getLogger(__name__)
MCP_SERVER_ID = "taskmate-mcp"
_armoriq_client: ArmorIQClient | None = None
_armoriq_client_lock = threading.Lock()

db: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    db = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(
    title="TaskMate AI",
    description="Backend API for the TaskMate AI task manager",
    version="0.2.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)

def get_db() -> Client:
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured. Check backend/.env.",
        )
    return db


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=250)
    description: str = ""
    priority: Literal["low", "normal", "high"] = "normal"
    due_at: datetime | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    document_url: str | None = None

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Task title cannot be blank.")
        return value


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=250)
    description: str | None = None
    status: Literal["pending", "done"] | None = None
    priority: Literal["low", "normal", "high"] | None = None
    due_at: datetime | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    document_url: str | None = None

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Task title cannot be blank.")
        return value


class AgentChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


CHAT_TOOLS = [
    {
        "type": "function",
        "name": "create_task",
        "description": (
            "Create one task in TaskMate only when the user explicitly asks to add, "
            "create, or save a task. Use only details stated or clearly implied. "
            "Do not invent priority or dates. Resolve relative dates from the provided "
            "local date and timezone. If the task intent or title is unclear, respond "
            "with a question instead of calling this function."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 250},
                "description": {"type": "string"},
                "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                "due_at": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Final deadline in RFC 3339 format.",
                },
                "scheduled_start": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Optional planned-work start in RFC 3339 format.",
                },
                "scheduled_end": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Optional planned-work end in RFC 3339 format.",
                },
            },
            "required": ["title"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "list_tasks",
        "description": "Read existing saved TaskMate tasks when the user asks to see or list tasks.",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["all", "pending", "done"],
                    "description": "Optional task status filter. Defaults to all.",
                }
            },
            "additionalProperties": False,
        },
    },
]


def get_armoriq_client() -> ArmorIQClient:
    global _armoriq_client
    if _armoriq_client is None:
        with _armoriq_client_lock:
            if _armoriq_client is None:
                _armoriq_client = ArmorIQClient.from_config(ARMORIQ_CONFIG)
    return _armoriq_client


def normalize_task_arguments(arguments: dict) -> dict:
    allowed_fields = {
        "title",
        "description",
        "priority",
        "due_at",
        "scheduled_start",
        "scheduled_end",
    }
    if set(arguments) - allowed_fields:
        raise ValueError("Gemini returned fields that create_task does not accept.")

    normalized = dict(arguments)
    local_timezone = datetime.now().astimezone().tzinfo
    for field, end_of_day in (
        ("due_at", True),
        ("scheduled_start", False),
        ("scheduled_end", True),
    ):
        value = normalized.get(field)
        if isinstance(value, str) and len(value) == 10:
            try:
                date_value = date.fromisoformat(value)
            except ValueError:
                continue
            local_time = time(23, 59) if end_of_day else time.min
            normalized[field] = datetime.combine(
                date_value, local_time, tzinfo=local_timezone
            ).isoformat()

    task = TaskCreate.model_validate(normalized)
    if (
        task.scheduled_start is not None
        and task.scheduled_end is not None
        and task.scheduled_end <= task.scheduled_start
    ):
        raise ValueError("Planned end must be after its start.")
    return task.model_dump(mode="json", exclude_unset=True, exclude_none=True)


def extract_mcp_result(invocation) -> object:
    if invocation.status != "success" or invocation.verified is not True:
        raise HTTPException(
            status_code=502,
            detail="ArmorIQ did not confirm that the task action completed.",
        )

    result = invocation.result
    if not isinstance(result, dict):
        return result
    if result.get("isError") or result.get("is_error") or result.get("error"):
        raise HTTPException(
            status_code=502,
            detail="The authorized TaskMate MCP action returned an error.",
        )

    content = result.get("content")
    if not isinstance(content, list):
        return result
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        try:
            parsed = json.loads(item.get("text", ""))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict) or parsed.get("error"):
            raise HTTPException(
                status_code=502,
                detail="The authorized TaskMate MCP action returned an error.",
            )
        return parsed.get("result", parsed)
    raise HTTPException(
        status_code=502,
        detail="The authorized TaskMate MCP action returned no task result.",
    )


def invoke_task_action(model: str, prompt: str, action: str, params: dict):
    client = get_armoriq_client()
    plan = client.capture_plan(
        llm=model,
        prompt=prompt,
        plan={
            "goal": f"Complete the user's TaskMate request using {action}.",
            "steps": [{"action": action, "mcp": MCP_SERVER_ID, "params": params}],
        },
    )
    intent_token = client.get_intent_token(plan, validity_seconds=300)
    return client.invoke(
        mcp=MCP_SERVER_ID,
        action=action,
        intent_token=intent_token,
        params=params,
    )


@app.get("/")
def home():
    return {"message": "TaskMate AI backend is running"}


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "TaskMate AI backend",
        "supabase_configured": db is not None,
    }


@app.get("/tasks")
def list_tasks(status: Literal["all", "pending", "done"] = "all"):
    database = get_db()
    try:
        query = database.table("tasks").select("*").order(
            "created_at", desc=True
        )
        if status != "all":
            query = query.eq("status", status)

        result = query.execute()
        return {"tasks": result.data, "count": len(result.data)}
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Could not retrieve tasks. Check the Supabase connection and table permissions.",
        ) from exc


@app.post("/tasks", status_code=201)
def create_task(task: TaskCreate):
    database = get_db()
    values = task.model_dump(mode="json", exclude_none=True)

    if (
        task.scheduled_start is not None
        and task.scheduled_end is not None
        and task.scheduled_end <= task.scheduled_start
    ):
        raise HTTPException(
            status_code=422,
            detail="Scheduled end must be after scheduled start.",
        )

    try:
        result = database.table("tasks").insert(values).execute()
        if not result.data:
            raise HTTPException(
                status_code=500, detail="Task was not returned after saving."
            )
        return result.data[0]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Could not save the task. Check the Supabase connection and table permissions.",
        ) from exc


@app.patch("/tasks/{task_id}")
def update_task(task_id: str, task: TaskUpdate):
    database = get_db()
    values = task.model_dump(mode="json", exclude_unset=True)

    if not values:
        raise HTTPException(
            status_code=400, detail="Provide at least one field to update."
        )

    if "title" in values and values["title"] is None:
        raise HTTPException(status_code=422, detail="Task title cannot be null.")

    values["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        result = (
            database.table("tasks")
            .update(values)
            .eq("id", task_id)
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="Task not found.")
        return result.data[0]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Could not update the task. Check the Supabase connection and table permissions.",
        ) from exc


@app.delete("/tasks/{task_id}")
def delete_task(task_id: str):
    database = get_db()
    try:
        result = database.table("tasks").delete().eq("id", task_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Task not found.")
        return {"deleted": True, "task_id": task_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Could not delete the task. Check the Supabase connection and table permissions.",
        ) from exc


@app.post("/agent/chat")
def agent_chat(request: AgentChatRequest):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="Gemini is not configured.")

    model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip() or "gemini-3.8-flash"
    local_now = datetime.now().astimezone()
    system_instruction = (
        "You are TaskMate AI, a friendly assistant for text conversation and task "
        "management. Use create_task only for a clear request to create a task, and "
        "use list_tasks when the user asks to see existing tasks. Never claim an "
        "action succeeded unless the application confirms the authorized MCP result. "
        "Do not invent task titles, dates, or priorities. Resolve relative dates from "
        f"the current local date and time {local_now.isoformat()} ({local_now.tzname()}); "
        "when a date has no time, use 23:59 local time. For greetings and other "
        "ordinary conversation, reply normally without calling a task function."
    )
    try:
        gemini_client = genai.Client(api_key=api_key)
        interaction = gemini_client.interactions.create(
            model=model,
            system_instruction=system_instruction,
            input=request.message,
            tools=CHAT_TOOLS,
        )
    except genai_errors.APIError as exc:
        logger.warning("Gemini API returned HTTP %s (%s)", exc.code, exc.status)
        if exc.code == 401:
            detail = "Gemini rejected the configured API key. Check the key and its status in backend/.env."
            status_code = 502
        elif exc.code == 403:
            detail = "This Gemini API key does not have access to the requested model or API. Check its project permissions."
            status_code = 502
        elif exc.code == 402:
            detail = "Gemini billing is required before this request can run. Check the Google AI project billing status."
            status_code = 503
        elif exc.code == 404:
            detail = "The configured Gemini model wasn't found or isn't available to this API key. Check GEMINI_MODEL."
            status_code = 502
        elif exc.code == 429:
            detail = "Gemini is rate-limited or its quota is exhausted. Wait a moment and try again."
            status_code = 429
        elif exc.code >= 500:
            detail = "Gemini is temporarily unavailable. Please try again shortly."
            status_code = 503
        else:
            detail = "Gemini couldn't process this message. Please try again with a shorter or clearer prompt."
            status_code = 502
        raise HTTPException(status_code=status_code, detail=detail) from exc
    except httpx.TimeoutException as exc:
        logger.warning("Gemini request timed out")
        raise HTTPException(
            status_code=504,
            detail="Gemini took too long to respond. Please try again.",
        ) from exc
    except httpx.RequestError as exc:
        logger.warning("Gemini network request failed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="TaskMate couldn't reach Gemini. Check your connection and try again.",
        ) from exc
    except Exception as exc:
        logger.error("Unexpected Gemini client error (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail="TaskMate couldn't generate a response. Please try again shortly.",
        ) from exc

    reply = (interaction.output_text or "").strip()
    function_calls = [
        step
        for step in (interaction.steps or [])
        if getattr(step, "type", None) == "function_call"
    ]
    if not function_calls:
        if not reply:
            raise HTTPException(
                status_code=502,
                detail="Gemini returned no text. Please try again with a different message.",
            )
        return {"reply": reply}

    if len(function_calls) != 1:
        return {
            "reply": "I can safely handle one task action per message. Please send the task request separately."
        }

    function_call = function_calls[0]
    action = function_call.name
    raw_params = function_call.arguments or {}
    if action not in {"create_task", "list_tasks"} or not isinstance(raw_params, dict):
        return {
            "reply": "I couldn't safely validate that task request. Please rephrase it and try again."
        }

    try:
        if action == "create_task":
            params = normalize_task_arguments(raw_params)
        else:
            if set(raw_params) - {"status"} or raw_params.get("status", "all") not in {
                "all",
                "pending",
                "done",
            }:
                raise ValueError("Gemini returned an invalid task filter.")
            params = dict(raw_params)
    except (ValueError, ValidationError) as exc:
        logger.warning("Gemini returned invalid %s arguments", action)
        return {
            "reply": "I couldn't validate those task details. Please check the task name and dates, then try again."
        }

    try:
        invocation = invoke_task_action(model, request.message, action, params)
    except PolicyBlockedException as exc:
        logger.warning("ArmorIQ blocked %s.%s", MCP_SERVER_ID, action)
        raise HTTPException(
            status_code=403,
            detail="ArmorIQ policy did not authorize that task action.",
        ) from exc
    except PolicyHoldException as exc:
        logger.warning("ArmorIQ placed %s.%s on hold", MCP_SERVER_ID, action)
        raise HTTPException(
            status_code=403,
            detail="ArmorIQ requires approval before that task action can run.",
        ) from exc
    except ConfigurationException as exc:
        logger.error("ArmorIQ configuration is unavailable for task actions")
        raise HTTPException(
            status_code=503,
            detail="Protected task actions aren't configured. Check the ArmorIQ service configuration.",
        ) from exc
    except ArmorIQException as exc:
        logger.warning("ArmorIQ %s failed for %s.%s", type(exc).__name__, MCP_SERVER_ID, action)
        raise HTTPException(
            status_code=502,
            detail="ArmorIQ couldn't complete the authorized MCP action. The MCP server must be reachable from ArmorIQ's proxy.",
        ) from exc

    tool_data = extract_mcp_result(invocation)
    if action == "create_task":
        if not isinstance(tool_data, dict) or not tool_data.get("id"):
            raise HTTPException(
                status_code=502,
                detail="The MCP server didn't confirm a saved task.",
            )
        saved_title = tool_data.get("title")
        if not isinstance(saved_title, str) or not saved_title.strip():
            raise HTTPException(
                status_code=502,
                detail="The MCP server returned an invalid saved task.",
            )
        return {
            "reply": f'Added "{saved_title}" to your tasks.',
            "action": action,
            "task": tool_data,
        }

    if not isinstance(tool_data, list) or any(not isinstance(task, dict) for task in tool_data):
        raise HTTPException(
            status_code=502,
            detail="The MCP server returned an invalid task list.",
        )
    if not tool_data:
        return {"reply": "You don't have any saved tasks yet.", "action": action}
    task_lines = [
        f"- {task['title']} ({'completed' if task.get('status') == 'done' else 'open'})"
        for task in tool_data
        if isinstance(task.get("title"), str)
    ]
    if len(task_lines) != len(tool_data):
        raise HTTPException(
            status_code=502,
            detail="The MCP server returned an invalid task list.",
        )
    return {
        "reply": f"Here are your saved tasks:\n{chr(10).join(task_lines)}",
        "action": action,
    }
