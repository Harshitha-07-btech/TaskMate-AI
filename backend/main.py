from fastapi.middleware.cors import CORSMiddleware
import asyncio
import hmac
import json
import logging
import os
import threading
import uuid
from contextlib import asynccontextmanager
from time import perf_counter
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Literal

import httpx
from armoriq_sdk import (
    ArmorIQClient,
    ConfigurationException,
)
from armoriq_sdk.config import (
    ArmorIQConfigError,
    load_armoriq_config,
    resolve_env_reference,
)
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from google.genai._gaos.lib import compat_errors as gemini_compat_errors
from pydantic import BaseModel, Field, ValidationError, field_validator
from supabase import Client, create_client

import armoriq_runtime
from armoriq_runtime import MCP_SERVER_ID, ProtectedActionError
from notifications import WhatsAppCloudProvider
from reminders import (
    ReminderService,
    RemindersUnavailable,
    SupabaseReminderStore,
    is_valid_timezone,
    resolve_timezone,
    to_iso,
    utcnow,
)

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
ARMORIQ_CONFIG = os.path.join(PROJECT_ROOT, "armoriq.yaml")
logger = logging.getLogger("taskmate")
_armoriq_client: ArmorIQClient | None = None
_armoriq_client_lock = threading.Lock()
_whatsapp_provider = WhatsAppCloudProvider()

# The Gemini SDK sleeps for whatever Retry-After a retryable response sends,
# with no cap: a quota 429 carried Retry-After of ~34,000 s and held a chat
# request (and the UI) for hours. So: never retry 429, retry transient errors
# once, bound each HTTP call, and enforce a wall-clock deadline on the whole call.
GEMINI_TIMEOUT_SECONDS = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "60"))
GEMINI_DEADLINE_SECONDS = float(os.getenv("GEMINI_DEADLINE_SECONDS", "90"))
GEMINI_HTTP_OPTIONS = genai_types.HttpOptions(
    timeout=int(GEMINI_TIMEOUT_SECONDS * 1000),
    retry_options=genai_types.HttpRetryOptions(
        attempts=1, http_status_codes=[408, 500, 502, 503, 504]
    ),
)
DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"
AGENT_ACTIONS = {"create_task", "list_tasks", "complete_task", "delete_task"}

db: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    db = create_client(SUPABASE_URL, SUPABASE_KEY)


def env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def run_reminder_job(deliver: bool) -> dict | None:
    if db is None:
        return None
    try:
        return reminder_service().sync(deliver=deliver)
    except RemindersUnavailable as exc:
        logger.warning("Reminder job skipped: %s", exc)
    except Exception as exc:  # noqa: BLE001 - one failed run must not stop the loop
        logger.error("Reminder job failed (%s)", type(exc).__name__)
    return None


async def reminder_loop(interval: int) -> None:
    while True:
        summary = await asyncio.to_thread(run_reminder_job, True)
        if summary and any(summary.values()):
            logger.info("Reminder job: %s", summary)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # The in-process loop only runs while this instance is awake. Reminder
    # rows are idempotent, so overlapping runs from several instances, the
    # external cron endpoint, and GET /reminders catch-up are all safe.
    task = None
    if db is not None and env_flag("REMINDER_SCHEDULER_ENABLED", True):
        interval = max(60, int(os.getenv("REMINDER_SCHEDULER_INTERVAL_SECONDS", "300")))
        task = asyncio.create_task(reminder_loop(interval))
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
        await asyncio.to_thread(armoriq_runtime.wait_for_telemetry, 10.0)


app = FastAPI(
    title="TaskMate AI",
    description="Backend API for the TaskMate AI task manager",
    version="0.3.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.getenv("CORS_ALLOWED_ORIGINS", DEFAULT_CORS_ORIGINS).split(",")
        if origin.strip()
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
    expose_headers=["X-Request-ID"],
)


class AgentActionError(Exception):
    """An agent request that failed, with a stable error_type for the client."""

    def __init__(self, status_code: int, error_type: str, detail: str, *,
                 request_id: str | None = None, trace_id: str | None = None, reason: str | None = None):
        super().__init__(detail)
        self.status_code = status_code
        self.error_type = error_type
        self.detail = detail
        self.request_id = request_id
        self.trace_id = trace_id
        self.reason = reason


@app.exception_handler(AgentActionError)
async def agent_action_error_handler(_request: Request, exc: AgentActionError):
    body = {"detail": exc.detail, "error_type": exc.error_type}
    if exc.request_id:
        body["request_id"] = exc.request_id
    if exc.trace_id:
        body["trace_id"] = exc.trace_id
    if exc.reason:
        body["reason"] = exc.reason
    headers = {"X-Request-ID": exc.request_id} if exc.request_id else None
    return JSONResponse(status_code=exc.status_code, content=body, headers=headers)


def get_db() -> Client:
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured. Check backend/.env.",
        )
    return db


def require_uuid(value: str, label: str = "Task") -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(status_code=404, detail=f"{label} not found.") from None


def reminder_service() -> ReminderService:
    return ReminderService(SupabaseReminderStore(get_db()), providers={"whatsapp": _whatsapp_provider})


def refresh_reminders_quietly() -> None:
    """Cancel or supersede reminders right after a task changes.

    Best-effort: the scheduled sync reconciles the same state if this fails or
    the reminder tables haven't been migrated yet.
    """
    if db is not None:
        try:
            reminder_service().sync(deliver=False)
        except Exception as exc:  # noqa: BLE001
            logger.info("Reminder refresh skipped (%s)", type(exc).__name__)


def reminders_unavailable(exc: RemindersUnavailable) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


def default_timezone() -> tzinfo:
    name = os.getenv("TASKMATE_DEFAULT_TIMEZONE")
    if name and is_valid_timezone(name):
        return resolve_timezone(name)
    return datetime.now().astimezone().tzinfo


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
    timezone: str | None = Field(default=None, max_length=64)

    @field_validator("timezone")
    @classmethod
    def timezone_must_exist(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_timezone(value):
            raise ValueError("Unknown time zone.")
        return value


class ReminderPreferencesUpdate(BaseModel):
    timezone: str = Field(max_length=64)
    in_app_enabled: bool = True
    before_deadline_hours: int = Field(default=24, ge=1, le=168)
    deadline_day_hour: int = Field(default=9, ge=0, le=23)
    overdue_interval_hours: int = Field(default=24, ge=1, le=168)
    max_overdue_reminders: int = Field(default=3, ge=0, le=30)
    whatsapp_enabled: bool = False
    whatsapp_number: str | None = Field(default=None, pattern=r"^\+[1-9][0-9]{7,14}$")
    whatsapp_consent: bool = False

    @field_validator("timezone")
    @classmethod
    def timezone_must_exist(cls, value: str) -> str:
        if not is_valid_timezone(value):
            raise ValueError("Unknown time zone.")
        return value


class ReminderResponse(BaseModel):
    action: Literal["completed", "not_yet", "snooze", "change_deadline"]
    snooze_minutes: int = Field(default=60, ge=5, le=7 * 24 * 60)
    due_at: datetime | None = None


TASK_TITLE_PARAMETER = {
    "type": "object",
    "properties": {
        "task_title": {
            "type": "string",
            "minLength": 1,
            "maxLength": 250,
            "description": "Title of the existing task, as the user named it.",
        }
    },
    "required": ["task_title"],
    "additionalProperties": False,
}

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
    {
        "type": "function",
        "name": "complete_task",
        "description": "Mark one existing task as completed when the user explicitly says it is done.",
        "parameters": TASK_TITLE_PARAMETER,
    },
    {
        "type": "function",
        "name": "delete_task",
        "description": (
            "Request permanent deletion of one existing task, only when the user explicitly "
            "asks to delete or remove it. TaskMate's security policy decides whether the "
            "deletion is permitted."
        ),
        "parameters": TASK_TITLE_PARAMETER,
    },
]


def get_armoriq_client() -> ArmorIQClient:
    global _armoriq_client
    if _armoriq_client is None:
        with _armoriq_client_lock:
            if _armoriq_client is None:
                _armoriq_client = build_armoriq_client(ARMORIQ_CONFIG)
    return _armoriq_client


def mcp_credentials_from_config(config) -> dict:
    """Resolve each MCP server's auth from armoriq.yaml for the X-Armoriq-MCP-Auth header.

    ArmorIQClient.from_config() (SDK 0.6.10) ignores mcp_servers[].auth, and
    `armoriq register` uploads the token as the literal "$ENV_NAME" string, so
    without this the proxy reaches the MCP server with the wrong bearer token
    and the MCP server's 401 surfaces as InvalidTokenException.
    """
    credentials = {}
    for server in config.mcp_servers:
        if server.auth.type == "bearer":
            token = resolve_env_reference(server.auth.token or "")
            if not token:
                raise ConfigurationException(
                    f"Bearer token for MCP server '{server.id}' resolved to an empty value."
                )
            credentials[server.id] = {"authType": "bearer", "token": token}
        elif server.auth.type == "api_key":
            api_key = resolve_env_reference(server.auth.api_key or "")
            if not api_key:
                raise ConfigurationException(
                    f"API key for MCP server '{server.id}' resolved to an empty value."
                )
            credentials[server.id] = {"authType": "api_key", "apiKey": api_key}
    return credentials


def build_armoriq_client(config_path: str) -> ArmorIQClient:
    # Mirrors ArmorIQClient.from_config, adding the per-MCP credentials it omits.
    try:
        config = load_armoriq_config(config_path)
    except ArmorIQConfigError as exc:
        raise ConfigurationException(f"Invalid ArmorIQ config: {exc}") from exc
    api_key = config.identity.resolved_api_key()
    if not api_key:
        raise ConfigurationException("API key resolved to empty value from config/environment.")
    return ArmorIQClient(
        api_key=api_key,
        user_id=config.identity.user_id,
        agent_id=config.identity.agent_id,
        proxy_endpoint=config.proxy.url,
        timeout=float(config.proxy.timeout),
        max_retries=int(config.proxy.max_retries),
        use_production=(config.environment == "production"),
        mcp_credentials=mcp_credentials_from_config(config),
    )


def normalize_task_arguments(arguments: dict, zone: tzinfo | None = None) -> dict:
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

    zone = zone or default_timezone()
    normalized = dict(arguments)
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
                date_value, local_time, tzinfo=zone
            ).isoformat()

    task = TaskCreate.model_validate(normalized)
    # A date-time without an offset means the user's local time, not UTC.
    for field in ("due_at", "scheduled_start", "scheduled_end"):
        value = getattr(task, field)
        if value is not None and value.tzinfo is None:
            setattr(task, field, value.replace(tzinfo=zone))
    if (
        task.scheduled_start is not None
        and task.scheduled_end is not None
        and task.scheduled_end <= task.scheduled_start
    ):
        raise ValueError("Planned end must be after its start.")
    return task.model_dump(mode="json", exclude_unset=True, exclude_none=True)


def extract_mcp_result(invocation, request_id: str | None = None, trace_id: str | None = None) -> object:
    def tool_error(detail: str) -> AgentActionError:
        return AgentActionError(502, "mcp_tool_error", detail, request_id=request_id, trace_id=trace_id)

    if invocation.status != "success" or invocation.verified is not True:
        raise tool_error("The authorized TaskMate MCP action returned an error. Nothing was confirmed.")

    result = invocation.result
    if not isinstance(result, dict):
        return result
    if result.get("isError") or result.get("is_error") or result.get("error"):
        raise tool_error("The authorized TaskMate MCP action returned an error. Nothing was confirmed.")

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
            raise tool_error("The authorized TaskMate MCP action returned an error. Nothing was confirmed.")
        return parsed.get("result", parsed)
    raise tool_error("The authorized TaskMate MCP action returned no task result.")


PROTECTED_ACTION_ERRORS = {
    "policy_denied": (403, "ArmorIQ policy blocked this action before it reached your tasks. Nothing was changed."),
    "policy_hold": (403, "ArmorIQ requires approval before this action can run. Nothing was changed."),
    "token_issuance": (502, "ArmorIQ couldn't issue an intent token for this action, so it was not run."),
    "auth_failed": (502, "ArmorIQ or the TaskMate MCP server rejected the service credentials. The task action was not confirmed."),
    "intent_mismatch": (409, "The task action did not match its captured intent plan. No action was confirmed."),
    "timeout": (504, "The task service didn't respond in time; it may be waking up. The task action was not confirmed, so try again in a minute."),
    "mcp_error": (502, "ArmorIQ could not complete the MCP invocation. The task action was not confirmed."),
    "armoriq_error": (502, "ArmorIQ couldn't complete the authorized MCP action. The task action was not confirmed."),
    "unexpected": (500, "TaskMate hit an unexpected error while running the task action. Nothing was confirmed."),
}


def invoke_task_action(request_id: str, model: str, action: str, params: dict, usage=None):
    try:
        client = get_armoriq_client()
    except ConfigurationException as exc:
        logger.error("request=%s ArmorIQ configuration is unavailable for task actions", request_id)
        raise AgentActionError(
            503, "configuration",
            "Protected task actions aren't configured. Check the ArmorIQ service configuration.",
            request_id=request_id,
        ) from exc

    try:
        return armoriq_runtime.run_protected_action(
            client, request_id=request_id, model=model, action=action, params=params, usage=usage
        )
    except ProtectedActionError as exc:
        status_code, detail = PROTECTED_ACTION_ERRORS[exc.kind]
        log = logger.info if exc.kind in {"policy_denied", "policy_hold"} else logger.warning
        log(
            "request=%s trace=%s %s.%s %s at %s (%s%s)",
            request_id, exc.trace_id, MCP_SERVER_ID, action, exc.kind, exc.stage,
            type(exc.cause).__name__,
            f", HTTP {exc.cause.status_code}" if getattr(exc.cause, "status_code", None) else "",
        )
        raise AgentActionError(
            status_code, exc.kind, detail,
            request_id=request_id, trace_id=exc.trace_id,
            reason=exc.reason if exc.kind in {"policy_denied", "policy_hold"} else None,
        ) from exc


def resolve_task_by_title(title: str) -> tuple[dict | None, list[dict]]:
    """Find the one task the user named. Returns (task, candidates)."""
    rows = (
        get_db().table("tasks").select("id,title,status").order("created_at", desc=True).execute().data
        or []
    )
    wanted = " ".join(title.split()).casefold()
    exact = [row for row in rows if " ".join(str(row.get("title", "")).split()).casefold() == wanted]
    if len(exact) == 1:
        return exact[0], exact
    if exact:
        return None, exact
    partial = [row for row in rows if wanted in str(row.get("title", "")).casefold()]
    if len(partial) == 1:
        return partial[0], partial
    return None, partial


def apply_task_update(task_id: str, task: TaskUpdate) -> dict:
    """Shared by PATCH /tasks and reminder responses, so both follow the same rules."""
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
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Could not update the task. Check the Supabase connection and table permissions.",
        ) from exc
    refresh_reminders_quietly()
    return result.data[0]


@app.get("/")
def home():
    return {"message": "TaskMate AI backend is running"}


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "TaskMate AI backend",
        "supabase_configured": db is not None,
        "gemini_configured": bool(os.getenv("GEMINI_API_KEY")),
        "armoriq_configured": bool(os.getenv("ARMORIQ_API_KEY") and os.getenv("TASKMATE_MCP_BEARER_TOKEN")),
        "whatsapp_configured": _whatsapp_provider.is_configured(),
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
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Could not save the task. Check the Supabase connection and table permissions.",
        ) from exc
    refresh_reminders_quietly()
    return result.data[0]


@app.patch("/tasks/{task_id}")
def update_task(task_id: str, task: TaskUpdate):
    return apply_task_update(require_uuid(task_id), task)


@app.delete("/tasks/{task_id}")
def delete_task(task_id: str):
    task_id = require_uuid(task_id)
    database = get_db()
    try:
        result = database.table("tasks").delete().eq("id", task_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Task not found.")
        # Reminder rows are removed by the ON DELETE CASCADE foreign key.
        return {"deleted": True, "task_id": task_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Could not delete the task. Check the Supabase connection and table permissions.",
        ) from exc


@app.get("/reminders")
def list_reminders():
    service = reminder_service()
    try:
        # Catch up on reminders that came due while the service was asleep.
        service.sync(deliver=False)
        reminders = service.visible()
    except RemindersUnavailable as exc:
        raise reminders_unavailable(exc) from exc
    return {
        "reminders": reminders,
        "unread_count": sum(1 for reminder in reminders if not reminder.get("read_at")),
    }


@app.get("/reminders/upcoming")
def upcoming_reminders():
    try:
        return {"upcoming": reminder_service().upcoming()}
    except RemindersUnavailable as exc:
        raise reminders_unavailable(exc) from exc


@app.get("/reminders/preferences")
def get_reminder_preferences():
    try:
        return reminder_service().store.get_preferences()
    except RemindersUnavailable as exc:
        raise reminders_unavailable(exc) from exc


@app.put("/reminders/preferences")
def save_reminder_preferences(preferences: ReminderPreferencesUpdate):
    service = reminder_service()
    try:
        current = service.store.get_preferences()
        values = preferences.model_dump(exclude={"whatsapp_consent"})
        if preferences.whatsapp_enabled:
            if not preferences.whatsapp_number:
                raise HTTPException(status_code=422, detail="Add a WhatsApp number in +country format first.")
            if not preferences.whatsapp_consent:
                raise HTTPException(status_code=422, detail="Confirm that you agree to receive WhatsApp reminders.")
            unchanged = current.get("whatsapp_enabled") and current.get("whatsapp_number") == preferences.whatsapp_number
            values["whatsapp_consent_at"] = current.get("whatsapp_consent_at") if unchanged else to_iso(utcnow())
        else:
            values["whatsapp_consent_at"] = None
        saved = service.store.save_preferences(values)
    except RemindersUnavailable as exc:
        raise reminders_unavailable(exc) from exc
    refresh_reminders_quietly()
    return saved


def load_in_app_reminder(service: ReminderService, reminder_id: str) -> dict:
    reminder = service.store.get_reminder(require_uuid(reminder_id, "Reminder"))
    if reminder is None or reminder.get("channel") != "in_app":
        raise HTTPException(status_code=404, detail="Reminder not found.")
    return reminder


@app.post("/reminders/{reminder_id}/read")
def mark_reminder_read(reminder_id: str):
    service = reminder_service()
    try:
        reminder = load_in_app_reminder(service, reminder_id)
        return service.mark(reminder["id"], {"read_at": to_iso(utcnow())})
    except RemindersUnavailable as exc:
        raise reminders_unavailable(exc) from exc


@app.post("/reminders/{reminder_id}/dismiss")
def dismiss_reminder(reminder_id: str):
    service = reminder_service()
    try:
        reminder = load_in_app_reminder(service, reminder_id)
        now = to_iso(utcnow())
        return service.mark(reminder["id"], {"dismissed_at": now, "read_at": reminder.get("read_at") or now})
    except RemindersUnavailable as exc:
        raise reminders_unavailable(exc) from exc


@app.post("/reminders/{reminder_id}/respond")
def respond_to_reminder(reminder_id: str, response: ReminderResponse):
    service = reminder_service()
    try:
        reminder = load_in_app_reminder(service, reminder_id)
        now = utcnow()
        task = None
        values = {"read_at": to_iso(now), "responded_at": to_iso(now)}
        if response.action == "completed":
            task = apply_task_update(reminder["task_id"], TaskUpdate(status="done"))
            values.update(response="completed", dismissed_at=to_iso(now))
        elif response.action == "not_yet":
            values["response"] = "not_yet"
        elif response.action == "snooze":
            values.update(
                response="snoozed",
                read_at=None,
                snoozed_until=to_iso(now + timedelta(minutes=response.snooze_minutes)),
            )
        else:
            if response.due_at is None:
                raise HTTPException(status_code=422, detail="Choose the new deadline.")
            due_at = response.due_at
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=resolve_timezone(service.store.get_preferences().get("timezone")))
            if due_at <= now:
                raise HTTPException(status_code=422, detail="The new deadline must be in the future.")
            task = apply_task_update(reminder["task_id"], TaskUpdate(due_at=due_at))
            values.update(response="deadline_changed", dismissed_at=to_iso(now))
        updated = service.mark(reminder["id"], values)
    except RemindersUnavailable as exc:
        raise reminders_unavailable(exc) from exc
    return {"reminder": updated, "task": task}


@app.get("/notifications/channels")
def notification_channels():
    return {
        "in_app": {"available": True},
        "whatsapp": {
            "provider": "WhatsApp Business Platform (Cloud API)",
            "configured": _whatsapp_provider.is_configured(),
            "missing_settings": WhatsAppCloudProvider.missing_settings(),
        },
    }


@app.post("/internal/reminders/run")
def run_reminders_now(x_reminder_cron_secret: str | None = Header(default=None)):
    """Trigger endpoint for an external scheduler while instances sleep."""
    expected = os.getenv("REMINDER_CRON_SECRET")
    if not expected:
        raise HTTPException(status_code=404, detail="Not found.")
    if not x_reminder_cron_secret or not hmac.compare_digest(x_reminder_cron_secret, expected):
        raise HTTPException(status_code=401, detail="Unauthorized.")
    try:
        return {"summary": reminder_service().sync(deliver=True)}
    except RemindersUnavailable as exc:
        raise reminders_unavailable(exc) from exc


def gemini_usage(interaction) -> tuple[int, int] | None:
    usage = getattr(interaction, "usage", None)
    input_tokens = getattr(usage, "total_input_tokens", None)
    output_tokens = getattr(usage, "total_output_tokens", None)
    if isinstance(input_tokens, int) and isinstance(output_tokens, int):
        return input_tokens, output_tokens
    return None


class GeminiDeadlineExceeded(Exception):
    pass


def call_gemini(api_key: str, **kwargs):
    """Run interactions.create with bounded retries and a hard wall-clock deadline."""
    client = genai.Client(api_key=api_key, http_options=GEMINI_HTTP_OPTIONS)
    outcome: dict = {}
    done = threading.Event()

    def run() -> None:
        try:
            outcome["value"] = client.interactions.create(**kwargs)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the request thread
            outcome["error"] = exc
        finally:
            done.set()

    # A daemon thread: if the SDK ever blocks past the deadline, it can't hold
    # up server shutdown (a stuck pre-fix request kept an old server alive
    # after it had stopped listening). It holds no task state.
    threading.Thread(target=run, name="gemini-call", daemon=True).start()
    if not done.wait(GEMINI_DEADLINE_SECONDS):
        raise GeminiDeadlineExceeded()
    if "value" in outcome:
        return outcome["value"]
    try:
        raise outcome["error"]
    except gemini_compat_errors.APIStatusError as exc:
        # The Interactions API raises its own error hierarchy, which is not a
        # genai_errors.APIError; convert it so status-specific handling applies.
        body = exc.body if isinstance(exc.body, dict) else {}
        raise genai_errors.APIError(exc.status_code, body, exc.response) from exc
    except gemini_compat_errors.APITimeoutError as exc:
        raise httpx.ReadTimeout("Gemini request timed out") from exc
    except gemini_compat_errors.APIConnectionError as exc:
        raise httpx.ConnectError("Gemini connection failed") from exc


def retry_after_seconds(exc: genai_errors.APIError) -> int | None:
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        value = int(float(headers.get("retry-after", "")))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def describe_wait(seconds: int) -> str:
    if seconds >= 5400:
        return f"about {round(seconds / 3600)} hours"
    if seconds >= 90:
        return f"about {round(seconds / 60)} minutes"
    return "a minute"


@app.post("/agent/chat")
def agent_chat(request: AgentChatRequest):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="Gemini is not configured.")

    request_id = armoriq_runtime.new_request_id()
    started = perf_counter()
    # Never log the message text: it can contain personal task details.
    logger.info("request=%s chat received (%d chars)", request_id, len(request.message))
    model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip() or "gemini-3.5-flash-lite"
    zone = resolve_timezone(request.timezone) if request.timezone else default_timezone()
    local_now = datetime.now(zone)
    system_instruction = (
        "You are TaskMate AI, a friendly assistant for text conversation and task "
        "management. Use create_task only for a clear request to create a task, "
        "list_tasks when the user asks to see existing tasks, complete_task when the "
        "user says a named task is done, and delete_task only when the user explicitly "
        "asks to delete a named task. Never claim an action succeeded unless the "
        "application confirms the authorized MCP result. "
        "Do not invent task titles, dates, or priorities. Resolve relative dates from "
        f"the current local date and time {local_now.isoformat()} ({local_now.tzname()}); "
        "when a date has no time, use 23:59 local time. For greetings and other "
        "ordinary conversation, reply normally without calling a task function."
    )
    logger.info("request=%s gemini call started (model=%s)", request_id, model)
    try:
        interaction = call_gemini(
            api_key,
            model=model,
            system_instruction=system_instruction,
            input=request.message,
            tools=CHAT_TOOLS,
        )
    except GeminiDeadlineExceeded as exc:
        logger.warning("request=%s gemini deadline of %.0fs exceeded", request_id, GEMINI_DEADLINE_SECONDS)
        raise HTTPException(
            status_code=504,
            detail="Gemini didn't respond in time. No task action was taken. Please try again.",
        ) from exc
    except genai_errors.APIError as exc:
        wait = retry_after_seconds(exc)
        logger.warning(
            "request=%s Gemini API returned HTTP %s (%s) after %.1fs, retry-after=%s",
            request_id, exc.code, exc.status, perf_counter() - started, wait,
        )
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
        elif exc.code == 429 and wait and wait > 120:
            detail = (
                "Gemini's quota for this API key is used up, so no task action was taken. "
                f"It should reset in {describe_wait(wait)}; or use a key with available quota."
            )
            status_code = 429
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
        logger.warning("request=%s Gemini request timed out", request_id)
        raise HTTPException(
            status_code=504,
            detail="Gemini took too long to respond. Please try again.",
        ) from exc
    except httpx.RequestError as exc:
        logger.warning("request=%s Gemini network request failed (%s)", request_id, type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="TaskMate couldn't reach Gemini. Check your connection and try again.",
        ) from exc
    except Exception as exc:
        logger.error("request=%s Unexpected Gemini client error (%s)", request_id, type(exc).__name__)
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
    logger.info(
        "request=%s gemini returned in %.1fs (function_calls=%d)",
        request_id, perf_counter() - started, len(function_calls),
    )
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
    if action not in AGENT_ACTIONS or not isinstance(raw_params, dict):
        return {
            "reply": "I couldn't safely validate that task request. Please rephrase it and try again."
        }

    target = None
    try:
        if action == "create_task":
            params = normalize_task_arguments(raw_params, zone)
        elif action == "list_tasks":
            if set(raw_params) - {"status"} or raw_params.get("status", "all") not in {
                "all",
                "pending",
                "done",
            }:
                raise ValueError("Gemini returned an invalid task filter.")
            params = dict(raw_params)
        else:
            title = raw_params.get("task_title")
            if set(raw_params) != {"task_title"} or not isinstance(title, str) or not title.strip():
                raise ValueError("Gemini returned an invalid task reference.")
            target, candidates = resolve_task_by_title(title)
            if target is None:
                if not candidates:
                    return {"reply": f'I couldn\'t find a task called "{title.strip()}". Check the title and try again.'}
                names = ", ".join(f'"{row["title"]}"' for row in candidates[:5])
                return {"reply": f"Several tasks match that name: {names}. Please use the full title."}
            params = {"task_id": target["id"]}
    except (ValueError, ValidationError):
        logger.warning("request=%s Gemini returned invalid %s arguments", request_id, action)
        return {
            "reply": "I couldn't validate those task details. Please check the task name and dates, then try again."
        }

    logger.info("request=%s armoriq protected action started (%s.%s)", request_id, MCP_SERVER_ID, action)
    action_started = perf_counter()
    outcome = invoke_task_action(request_id, model, action, params, usage=gemini_usage(interaction))
    trace_id = outcome.trace_id
    tool_data = extract_mcp_result(outcome.invocation, request_id, trace_id)
    logger.info(
        "request=%s trace=%s %s.%s allowed and completed in %.1fs (total %.1fs)",
        request_id, trace_id, MCP_SERVER_ID, action,
        perf_counter() - action_started, perf_counter() - started,
    )
    meta = {"action": action, "request_id": request_id, "trace_id": trace_id, "decision": "allow"}

    def invalid(detail: str) -> AgentActionError:
        return AgentActionError(502, "mcp_tool_error", detail, request_id=request_id, trace_id=trace_id)

    if action in {"create_task", "complete_task"}:
        if not isinstance(tool_data, dict) or not tool_data.get("id"):
            raise invalid("The MCP server didn't confirm a saved task.")
        saved_title = tool_data.get("title")
        if not isinstance(saved_title, str) or not saved_title.strip():
            raise invalid("The MCP server returned an invalid saved task.")
        if action == "complete_task" and tool_data.get("status") != "done":
            raise invalid("The MCP server didn't confirm the task was completed.")
        refresh_reminders_quietly()
        verb = "Added" if action == "create_task" else "Marked"
        suffix = "to your tasks." if action == "create_task" else "as completed."
        return {"reply": f'{verb} "{saved_title}" {suffix}', "task": tool_data, **meta}

    if action == "delete_task":
        if not isinstance(tool_data, dict) or tool_data.get("deleted") is not True or tool_data.get("task_id") != target["id"]:
            raise invalid("The MCP server didn't confirm the deletion.")
        return {"reply": f'Deleted "{target["title"]}".', "deleted_task_id": target["id"], **meta}

    if not isinstance(tool_data, list) or any(not isinstance(task, dict) for task in tool_data):
        raise invalid("The MCP server returned an invalid task list.")
    if not tool_data:
        return {"reply": "You don't have any saved tasks yet.", **meta}
    task_lines = [
        f"- {task['title']} ({'completed' if task.get('status') == 'done' else 'open'})"
        for task in tool_data
        if isinstance(task.get("title"), str)
    ]
    if len(task_lines) != len(tool_data):
        raise invalid("The MCP server returned an invalid task list.")
    return {
        "reply": f"Here are your saved tasks:\n{chr(10).join(task_lines)}",
        **meta,
    }
