"""Deadline reminders: scheduling, idempotent persistence, and delivery.

Reminders are derived from task deadlines, not queued in memory. Every sync
computes, for each open task with a deadline, the most recent reminder slot
whose time has arrived and inserts it under a unique idempotency key. Running
the sync twice, from two workers, or after a restart therefore never creates
a duplicate: the database's unique constraint on ``idempotency_key`` decides.

Slots for one deadline, in the user's time zone:
  before_deadline  due_at - before_deadline_hours
  deadline_day     deadline_day_hour on the due date (or local midnight when
                   that hour is already past the deadline)
  overdue #n       due_at + n * overdue_interval_hours, n < max_overdue_reminders

Only the latest slot that has arrived is materialized, so a service that
slept for days posts one current reminder instead of a backlog. The key
contains the deadline itself, so changing a deadline yields new keys while
reminders for the old deadline are marked ``superseded``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from postgrest.exceptions import APIError

from notifications import NotificationProvider

logger = logging.getLogger(__name__)

MIGRATION_FILE = "supabase/migrations/20261017000000_task_reminders.sql"
GRANTS_MIGRATION_FILE = "supabase/migrations/20261017000100_task_reminders_grants.sql"
ACTIVE_STATUSES = ("pending", "sending", "sent", "failed")
MAX_DELIVERY_ATTEMPTS = 3
STALE_SENDING_AFTER = timedelta(minutes=10)
EXTERNAL_DELIVERY_MAX_AGE = timedelta(hours=24)
RETRY_BACKOFF = (timedelta(minutes=5), timedelta(minutes=30))

DEFAULT_PREFERENCES: dict[str, Any] = {
    "timezone": "UTC",
    "in_app_enabled": True,
    "before_deadline_hours": 24,
    "deadline_day_hour": 9,
    "overdue_interval_hours": 24,
    "max_overdue_reminders": 3,
    "whatsapp_enabled": False,
    "whatsapp_number": None,
    "whatsapp_consent_at": None,
}


class RemindersUnavailable(RuntimeError):
    """The reminder tables are missing or the database refused the query."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def is_valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


@dataclass(frozen=True)
class ReminderSlot:
    kind: str
    sequence: int
    scheduled_for: datetime


def reminder_slots(due_at: datetime, created_at: datetime | None, prefs: dict) -> list[ReminderSlot]:
    zone = resolve_timezone(prefs.get("timezone"))
    slots: list[ReminderSlot] = []

    before = due_at - timedelta(hours=int(prefs["before_deadline_hours"]))
    slots.append(ReminderSlot("before_deadline", 0, before))

    local_due = due_at.astimezone(zone)
    day_hour = datetime.combine(local_due.date(), time(int(prefs["deadline_day_hour"])), tzinfo=zone)
    if day_hour >= local_due:
        day_hour = datetime.combine(local_due.date(), time.min, tzinfo=zone)
    if day_hour < local_due:
        slots.append(ReminderSlot("deadline_day", 0, day_hour.astimezone(timezone.utc)))

    interval = timedelta(hours=int(prefs["overdue_interval_hours"]))
    for sequence in range(int(prefs["max_overdue_reminders"])):
        slots.append(ReminderSlot("overdue", sequence, due_at + interval * sequence))

    # A heads-up whose time passed before the task existed would only be noise
    # (e.g. "due tomorrow" for a task created an hour before its deadline).
    if created_at is not None:
        slots = [s for s in slots if s.kind == "overdue" or s.scheduled_for >= created_at]
    return sorted(slots, key=lambda slot: slot.scheduled_for)


def current_slot(due_at: datetime, created_at: datetime | None, prefs: dict, now: datetime) -> ReminderSlot | None:
    arrived = [slot for slot in reminder_slots(due_at, created_at, prefs) if slot.scheduled_for <= now]
    return arrived[-1] if arrived else None


def next_slot(due_at: datetime, created_at: datetime | None, prefs: dict, now: datetime) -> ReminderSlot | None:
    upcoming = [slot for slot in reminder_slots(due_at, created_at, prefs) if slot.scheduled_for > now]
    return upcoming[0] if upcoming else None


def idempotency_key(task_id: str, slot: ReminderSlot, due_at: datetime, channel: str) -> str:
    return f"{task_id}:{slot.kind}:{slot.sequence}:{int(due_at.timestamp())}:{channel}"


def format_local(value: datetime, zone: ZoneInfo) -> str:
    local = value.astimezone(zone)
    hour = local.hour % 12 or 12
    return f"{local:%a} {local.day} {local:%b}, {hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"


def format_local_time(value: datetime, zone: ZoneInfo) -> str:
    local = value.astimezone(zone)
    hour = local.hour % 12 or 12
    return f"{hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"


def reminder_message(title: str, slot: ReminderSlot, due_at: datetime, zone: ZoneInfo, now: datetime) -> str:
    due_date: date = due_at.astimezone(zone).date()
    today = now.astimezone(zone).date()
    if slot.kind == "overdue":
        return f'"{title}" is overdue (it was due {format_local(due_at, zone)}). Is it completed?'
    if slot.kind == "deadline_day":
        return (
            f'Your deadline is today: "{title}" is due at {format_local_time(due_at, zone)}. '
            "Remember to submit your work."
        )
    if due_date == today:
        return f'"{title}" is due today at {format_local_time(due_at, zone)}. Have you completed it?'
    if due_date == today + timedelta(days=1):
        return (
            f'You have a task due tomorrow: "{title}" at {format_local_time(due_at, zone)}. '
            "Have you completed it?"
        )
    return (
        f'Your deadline is approaching: "{title}" is due {format_local(due_at, zone)}. '
        "Please update your progress."
    )


class ReminderStore(Protocol):
    def get_preferences(self) -> dict: ...
    def save_preferences(self, values: dict) -> dict: ...
    def list_open_tasks_with_deadline(self) -> list[dict]: ...
    def list_active_reminders(self) -> list[dict]: ...
    def set_status(self, reminder_ids: list[str], status: str, now: datetime) -> None: ...
    def insert_reminders(self, rows: list[dict]) -> list[dict]: ...
    def list_visible(self, now: datetime) -> list[dict]: ...
    def get_reminder(self, reminder_id: str) -> dict | None: ...
    def update_reminder(self, reminder_id: str, values: dict, *, expected_status: str | None = None,
                        expected_attempts: int | None = None) -> dict | None: ...
    def list_external_candidates(self) -> list[dict]: ...


class SupabaseReminderStore:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _run(query):
        try:
            return query.execute()
        except APIError as exc:
            if exc.code in {"PGRST205", "42P01", "PGRST200"}:
                raise RemindersUnavailable(
                    f"Reminder tables are missing. Run {MIGRATION_FILE} in the Supabase SQL editor."
                ) from exc
            if exc.code == "42501":
                raise RemindersUnavailable(
                    "The backend's Supabase role lacks privileges on the reminder tables. "
                    f"Run {GRANTS_MIGRATION_FILE} in the Supabase SQL editor."
                ) from exc
            raise RemindersUnavailable("The reminder database query failed.") from exc

    def get_preferences(self) -> dict:
        result = self._run(self.db.table("reminder_preferences").select("*").eq("id", "default").limit(1))
        if result.data:
            return {**DEFAULT_PREFERENCES, **result.data[0], "persisted": True}
        return {**DEFAULT_PREFERENCES, "persisted": False}

    def save_preferences(self, values: dict) -> dict:
        row = {**values, "id": "default", "updated_at": to_iso(utcnow())}
        result = self._run(self.db.table("reminder_preferences").upsert(row, on_conflict="id"))
        return {**DEFAULT_PREFERENCES, **(result.data[0] if result.data else row), "persisted": True}

    def list_open_tasks_with_deadline(self) -> list[dict]:
        query = (
            self.db.table("tasks")
            .select("id,title,status,due_at,created_at")
            .eq("status", "pending")
            .not_.is_("due_at", "null")
        )
        return self._run(query).data or []

    def list_active_reminders(self) -> list[dict]:
        query = (
            self.db.table("task_reminders")
            .select("id,task_id,kind,channel,status,due_at_snapshot")
            .in_("status", list(ACTIVE_STATUSES))
        )
        return self._run(query).data or []

    def set_status(self, reminder_ids: list[str], status: str, now: datetime) -> None:
        if reminder_ids:
            self._run(
                self.db.table("task_reminders")
                .update({"status": status, "updated_at": to_iso(now)})
                .in_("id", reminder_ids)
            )

    def insert_reminders(self, rows: list[dict]) -> list[dict]:
        if not rows:
            return []
        query = self.db.table("task_reminders").upsert(
            rows, on_conflict="idempotency_key", ignore_duplicates=True
        )
        return self._run(query).data or []

    def list_visible(self, now: datetime) -> list[dict]:
        query = (
            self.db.table("task_reminders")
            .select("*, task:tasks(id,title,status,due_at,priority)")
            .eq("channel", "in_app")
            .eq("status", "sent")
            .is_("dismissed_at", "null")
            .or_(f"snoozed_until.is.null,snoozed_until.lte.{to_iso(now)}")
            .order("scheduled_for", desc=True)
            .limit(50)
        )
        return self._run(query).data or []

    def get_reminder(self, reminder_id: str) -> dict | None:
        result = self._run(self.db.table("task_reminders").select("*").eq("id", reminder_id).limit(1))
        return result.data[0] if result.data else None

    def update_reminder(self, reminder_id, values, *, expected_status=None, expected_attempts=None):
        query = self.db.table("task_reminders").update(values).eq("id", reminder_id)
        if expected_status is not None:
            query = query.eq("status", expected_status)
        if expected_attempts is not None:
            query = query.eq("attempt_count", expected_attempts)
        result = self._run(query)
        return result.data[0] if result.data else None

    def list_external_candidates(self) -> list[dict]:
        query = (
            self.db.table("task_reminders")
            .select("*, task:tasks(id,title,status,due_at)")
            .neq("channel", "in_app")
            .in_("status", ["pending", "failed", "sending"])
            .limit(100)
        )
        return self._run(query).data or []


class ReminderService:
    def __init__(self, store: ReminderStore, providers: dict[str, NotificationProvider] | None = None,
                 clock: Callable[[], datetime] = utcnow):
        self.store = store
        self.providers = providers or {}
        self.clock = clock

    def sync(self, *, deliver: bool = False) -> dict:
        """Reconcile reminders with current tasks, then materialize due ones.

        Safe to run concurrently and repeatedly.
        """
        now = self.clock()
        prefs = self.store.get_preferences()
        tasks = self.store.list_open_tasks_with_deadline()
        open_tasks = {task["id"]: task for task in tasks}
        summary = {"created": 0, "cancelled": 0, "superseded": 0}

        cancelled, superseded = [], []
        for reminder in self.store.list_active_reminders():
            task = open_tasks.get(reminder["task_id"])
            if task is None:
                cancelled.append(reminder["id"])
            elif parse_timestamp(reminder["due_at_snapshot"]) != parse_timestamp(task["due_at"]):
                superseded.append(reminder["id"])
        self.store.set_status(cancelled, "cancelled", now)
        self.store.set_status(superseded, "superseded", now)
        summary["cancelled"], summary["superseded"] = len(cancelled), len(superseded)

        zone = resolve_timezone(prefs.get("timezone"))
        channels = []
        if prefs.get("in_app_enabled", True):
            channels.append("in_app")
        if prefs.get("whatsapp_enabled") and prefs.get("whatsapp_number") and prefs.get("whatsapp_consent_at"):
            channels.append("whatsapp")

        rows = []
        for task in tasks:
            due_at = parse_timestamp(task["due_at"])
            slot = current_slot(due_at, parse_timestamp(task.get("created_at")), prefs, now)
            if slot is None:
                continue
            message = reminder_message(task["title"], slot, due_at, zone, now)
            for channel in channels:
                rows.append({
                    "task_id": task["id"],
                    "kind": slot.kind,
                    "sequence": slot.sequence,
                    "channel": channel,
                    "scheduled_for": to_iso(slot.scheduled_for),
                    "due_at_snapshot": to_iso(due_at),
                    "message": message,
                    # An in-app reminder is delivered by being stored; external
                    # channels stay pending until a provider accepts them.
                    "status": "sent" if channel == "in_app" else "pending",
                    "sent_at": to_iso(now) if channel == "in_app" else None,
                    "recipient": prefs.get("whatsapp_number") if channel == "whatsapp" else None,
                    "idempotency_key": idempotency_key(task["id"], slot, due_at, channel),
                })
        summary["created"] = len(self.store.insert_reminders(rows))
        if deliver:
            summary.update(self.deliver_external(now))
        return summary

    def deliver_external(self, now: datetime | None = None) -> dict:
        now = now or self.clock()
        counts = {"accepted": 0, "failed": 0, "skipped": 0, "cancelled": 0}
        for reminder in self.store.list_external_candidates():
            status, attempts = reminder["status"], int(reminder.get("attempt_count") or 0)
            if status == "failed":
                next_attempt = parse_timestamp(reminder.get("next_attempt_at"))
                if next_attempt is None or next_attempt > now or attempts >= MAX_DELIVERY_ATTEMPTS:
                    continue
            if status == "sending":
                updated = parse_timestamp(reminder.get("updated_at"))
                if updated is None or now - updated < STALE_SENDING_AFTER:
                    continue

            claimed = self.store.update_reminder(
                reminder["id"],
                {"status": "sending", "attempt_count": attempts + 1, "updated_at": to_iso(now)},
                expected_status=status,
                expected_attempts=attempts,
            )
            if claimed is None:
                continue  # another worker claimed it first

            provider = self.providers.get(reminder["channel"])
            task = reminder.get("task") or {}
            outcome = self._deliver(reminder, task, provider, now)
            self.store.update_reminder(reminder["id"], {**outcome, "updated_at": to_iso(now)},
                                       expected_status="sending")
            counts["accepted" if outcome["status"] == "sent" else outcome["status"]] += 1
        return counts

    def _deliver(self, reminder: dict, task: dict, provider, now: datetime) -> dict:
        if provider is None or not provider.is_configured():
            return {"status": "skipped", "last_error": "Provider is not configured."}
        if task.get("status") != "pending":
            return {"status": "cancelled", "last_error": None}
        if now - parse_timestamp(reminder["scheduled_for"]) > EXTERNAL_DELIVERY_MAX_AGE:
            return {"status": "skipped", "last_error": "Reminder became stale before delivery."}
        prefs = self.store.get_preferences()
        zone = resolve_timezone(prefs.get("timezone"))
        due_at = parse_timestamp(reminder["due_at_snapshot"])
        result = provider.send(
            to=reminder["recipient"],
            template_params=[task.get("title") or "Your task", format_local(due_at, zone)],
        )
        if result.accepted:
            return {"status": "sent", "sent_at": to_iso(now), "provider_message_id": result.provider_message_id,
                    "last_error": None, "next_attempt_at": None}
        attempts = int(reminder.get("attempt_count") or 0) + 1
        retry = result.retryable and attempts < MAX_DELIVERY_ATTEMPTS
        backoff = RETRY_BACKOFF[min(attempts - 1, len(RETRY_BACKOFF) - 1)]
        return {"status": "failed", "last_error": result.error,
                "next_attempt_at": to_iso(now + backoff) if retry else None}

    def visible(self) -> list[dict]:
        return self.store.list_visible(self.clock())

    def upcoming(self, limit: int = 10) -> list[dict]:
        now = self.clock()
        prefs = self.store.get_preferences()
        items = []
        for task in self.store.list_open_tasks_with_deadline():
            due_at = parse_timestamp(task["due_at"])
            slot = next_slot(due_at, parse_timestamp(task.get("created_at")), prefs, now)
            if slot is not None:
                items.append({
                    "task_id": task["id"],
                    "title": task["title"],
                    "due_at": to_iso(due_at),
                    "kind": slot.kind,
                    "scheduled_for": to_iso(slot.scheduled_for),
                })
        return sorted(items, key=lambda item: item["scheduled_for"])[:limit]

    def mark(self, reminder_id: str, values: dict) -> dict | None:
        return self.store.update_reminder(reminder_id, {**values, "updated_at": to_iso(self.clock())})
