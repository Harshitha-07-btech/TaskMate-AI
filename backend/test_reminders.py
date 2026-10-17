import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

import main
from notifications import DeliveryResult
from reminders import (
    DEFAULT_PREFERENCES,
    MAX_DELIVERY_ATTEMPTS,
    ReminderService,
    RemindersUnavailable,
    SupabaseReminderStore,
    parse_timestamp,
    to_iso,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


class InMemoryReminderStore:
    """Mirrors the SQL schema's guarantees: a unique idempotency_key and
    conditional (compare-and-set) updates."""

    def __init__(self, tasks=None, preferences=None):
        self.tasks = {task["id"]: dict(task) for task in (tasks or [])}
        self.preferences = {**DEFAULT_PREFERENCES, **(preferences or {})}
        self.reminders = {}

    def get_preferences(self):
        return dict(self.preferences, persisted=True)

    def save_preferences(self, values):
        self.preferences.update(values)
        return self.get_preferences()

    def list_open_tasks_with_deadline(self):
        return [dict(t) for t in self.tasks.values() if t["status"] == "pending" and t.get("due_at")]

    def list_active_reminders(self):
        return [dict(r) for r in self.reminders.values() if r["status"] in {"pending", "sending", "sent", "failed"}]

    def set_status(self, ids, status, now):
        for reminder_id in ids:
            self.reminders[reminder_id].update(status=status, updated_at=to_iso(now))

    def insert_reminders(self, rows):
        inserted = []
        keys = {r["idempotency_key"] for r in self.reminders.values()}
        for row in rows:
            if row["idempotency_key"] in keys:
                continue
            reminder = {
                "id": str(uuid.uuid4()), "attempt_count": 0, "read_at": None, "dismissed_at": None,
                "snoozed_until": None, "next_attempt_at": None, "updated_at": row["scheduled_for"], **row,
            }
            self.reminders[reminder["id"]] = reminder
            keys.add(row["idempotency_key"])
            inserted.append(dict(reminder))
        return inserted

    def _with_task(self, reminder):
        return {**reminder, "task": dict(self.tasks.get(reminder["task_id"], {}))}

    def list_visible(self, now):
        rows = [
            r for r in self.reminders.values()
            if r["channel"] == "in_app" and r["status"] == "sent" and r["dismissed_at"] is None
            and (r["snoozed_until"] is None or parse_timestamp(r["snoozed_until"]) <= now)
        ]
        return [self._with_task(r) for r in sorted(rows, key=lambda r: r["scheduled_for"], reverse=True)]

    def get_reminder(self, reminder_id):
        reminder = self.reminders.get(reminder_id)
        return dict(reminder) if reminder else None

    def update_reminder(self, reminder_id, values, *, expected_status=None, expected_attempts=None):
        reminder = self.reminders.get(reminder_id)
        if reminder is None:
            return None
        if expected_status is not None and reminder["status"] != expected_status:
            return None
        if expected_attempts is not None and reminder["attempt_count"] != expected_attempts:
            return None
        reminder.update(values)
        return dict(reminder)

    def list_external_candidates(self):
        return [self._with_task(r) for r in self.reminders.values()
                if r["channel"] != "in_app" and r["status"] in {"pending", "failed", "sending"}]

    # helpers
    def rows(self, **filters):
        return [r for r in self.reminders.values() if all(r.get(k) == v for k, v in filters.items())]


class FakeProvider:
    channel = "whatsapp"

    def __init__(self, results=None, configured=True):
        self.results = list(results or [])
        self.configured = configured
        self.sent = []

    def is_configured(self):
        return self.configured

    def send(self, *, to, template_params):
        self.sent.append((to, template_params))
        return self.results.pop(0) if self.results else DeliveryResult(accepted=True, provider_message_id="wamid.1")


def task(due_at, *, status="pending", created_at=None, title="Submit assignment"):
    return {
        "id": str(uuid.uuid4()),
        "title": title,
        "status": status,
        "due_at": to_iso(due_at) if due_at else None,
        "created_at": to_iso(created_at or due_at - timedelta(days=7) if due_at else NOW - timedelta(days=7)),
    }


def service(store, now=NOW, providers=None):
    return ReminderService(store, providers=providers, clock=lambda: now)


class SchedulingTests(unittest.TestCase):
    def test_reminder_a_day_ahead_is_created_once_across_restarts_and_workers(self):
        item = task(NOW + timedelta(hours=20))
        store = InMemoryReminderStore([item])

        first = service(store).sync()
        service(store).sync()                          # a restart
        service(store, NOW + timedelta(minutes=1)).sync()  # a second worker

        self.assertEqual(first["created"], 1)
        [reminder] = store.rows(channel="in_app")
        self.assertEqual(reminder["kind"], "before_deadline")
        self.assertEqual(reminder["status"], "sent")
        self.assertIn("due tomorrow", reminder["message"])
        self.assertIn("Have you completed it?", reminder["message"])

    def test_nothing_is_created_before_the_first_reminder_time(self):
        store = InMemoryReminderStore([task(NOW + timedelta(days=3))])
        self.assertEqual(service(store).sync()["created"], 0)

    def test_missing_deadline_never_creates_reminders(self):
        store = InMemoryReminderStore([task(None)])
        self.assertEqual(service(store).sync()["created"], 0)

    def test_only_the_latest_due_slot_is_posted_after_a_long_sleep(self):
        item = task(NOW - timedelta(days=2, hours=1))
        store = InMemoryReminderStore([item])

        service(store).sync()

        [reminder] = store.rows()
        self.assertEqual((reminder["kind"], reminder["sequence"]), ("overdue", 2))
        self.assertIn("is overdue", reminder["message"])
        self.assertIn("Is it completed?", reminder["message"])

    def test_overdue_reminders_repeat_on_the_interval_up_to_the_maximum(self):
        due = NOW - timedelta(minutes=5)
        store = InMemoryReminderStore([task(due)], {"overdue_interval_hours": 24, "max_overdue_reminders": 2})
        for hours in (0, 12, 24, 48, 96):
            service(store, NOW + timedelta(hours=hours)).sync()

        self.assertEqual(sorted(r["sequence"] for r in store.rows(kind="overdue")), [0, 1])

    def test_completed_task_cancels_reminders_and_stops_new_ones(self):
        item = task(NOW + timedelta(hours=2))
        store = InMemoryReminderStore([item])
        service(store).sync()

        store.tasks[item["id"]]["status"] = "done"
        summary = service(store, NOW + timedelta(days=2)).sync()

        self.assertEqual(summary["cancelled"], 1)
        self.assertEqual(summary["created"], 0)
        self.assertEqual(service(store).visible(), [])

    def test_deleted_task_reminders_are_cancelled(self):
        item = task(NOW + timedelta(hours=2))
        store = InMemoryReminderStore([item])
        service(store).sync()

        del store.tasks[item["id"]]
        self.assertEqual(service(store).sync()["cancelled"], 1)

    def test_changed_deadline_supersedes_old_reminders_and_reschedules(self):
        item = task(NOW + timedelta(hours=2))
        store = InMemoryReminderStore([item])
        service(store).sync()
        [old] = store.rows()

        store.tasks[item["id"]]["due_at"] = to_iso(NOW + timedelta(days=5))
        summary = service(store).sync()
        self.assertEqual(summary["superseded"], 1)
        self.assertEqual(store.reminders[old["id"]]["status"], "superseded")
        self.assertEqual(service(store).visible(), [])
        self.assertEqual(service(store).upcoming()[0]["kind"], "before_deadline")

        later = service(store, NOW + timedelta(days=4, hours=1)).sync()
        self.assertEqual(later["created"], 1)
        self.assertNotEqual(store.rows(status="sent")[0]["idempotency_key"], old["idempotency_key"])

    def test_heads_up_before_the_task_existed_is_skipped(self):
        # Created one hour before its deadline: "due tomorrow" would be noise.
        item = task(NOW + timedelta(hours=1), created_at=NOW - timedelta(minutes=1))
        store = InMemoryReminderStore([item], {"timezone": "UTC", "deadline_day_hour": 9})

        service(store).sync()
        self.assertEqual(store.rows(), [])

        service(store, NOW + timedelta(hours=1)).sync()
        [reminder] = store.rows()
        self.assertEqual((reminder["kind"], reminder["sequence"]), ("overdue", 0))

    def test_in_app_disabled_creates_no_in_app_reminders(self):
        store = InMemoryReminderStore([task(NOW + timedelta(hours=2))], {"in_app_enabled": False})
        service(store).sync()
        self.assertEqual(store.rows(channel="in_app"), [])


class TimeZoneTests(unittest.TestCase):
    def test_deadline_day_reminder_uses_the_users_local_morning(self):
        # Due 17:00 IST on 18 Oct; the 09:00 IST reminder is 03:30 UTC.
        due = datetime(2026, 10, 18, 11, 30, tzinfo=UTC)
        store = InMemoryReminderStore([task(due)], {"timezone": "Asia/Kolkata", "before_deadline_hours": 48})

        service(store, datetime(2026, 10, 18, 3, 29, tzinfo=UTC)).sync()
        self.assertEqual(store.rows(kind="deadline_day"), [])
        service(store, datetime(2026, 10, 18, 3, 30, tzinfo=UTC)).sync()

        [reminder] = store.rows(kind="deadline_day")
        self.assertEqual(reminder["scheduled_for"], "2026-10-18T03:30:00Z")
        self.assertIn("Your deadline is today", reminder["message"])
        self.assertIn("5:00 PM", reminder["message"])

    def test_local_day_boundaries_follow_daylight_saving_time(self):
        # New York leaves DST on 1 Nov 2026: 09:00 local is 13:00 UTC before, 14:00 UTC after.
        prefs = {"timezone": "America/New_York", "before_deadline_hours": 1}
        for due, expected in (
            (datetime(2026, 10, 31, 20, 0, tzinfo=UTC), "2026-10-31T13:00:00Z"),
            (datetime(2026, 11, 2, 20, 0, tzinfo=UTC), "2026-11-02T14:00:00Z"),
        ):
            with self.subTest(expected=expected):
                store = InMemoryReminderStore([task(due)], prefs)
                service(store, due - timedelta(hours=2)).sync()
                self.assertEqual(store.rows(kind="deadline_day")[0]["scheduled_for"], expected)

    def test_deadline_before_the_morning_hour_falls_back_to_local_midnight(self):
        due = datetime(2026, 10, 18, 2, 0, tzinfo=UTC)   # 02:00 UTC = 07:30 IST, before 09:00
        store = InMemoryReminderStore([task(due)], {"timezone": "Asia/Kolkata", "before_deadline_hours": 1})
        service(store, datetime(2026, 10, 17, 19, 0, tzinfo=UTC)).sync()
        self.assertEqual(store.rows(kind="deadline_day")[0]["scheduled_for"], "2026-10-17T18:30:00Z")


class ExternalDeliveryTests(unittest.TestCase):
    def opted_in_store(self, item):
        return InMemoryReminderStore([item], {
            "whatsapp_enabled": True, "whatsapp_number": "+919876543210", "whatsapp_consent_at": to_iso(NOW),
        })

    def test_unconfigured_provider_is_recorded_as_skipped_never_sent(self):
        store = self.opted_in_store(task(NOW + timedelta(hours=2)))
        service(store, providers={"whatsapp": FakeProvider(configured=False)}).sync(deliver=True)

        [whatsapp] = store.rows(channel="whatsapp")
        self.assertEqual(whatsapp["status"], "skipped")
        self.assertIsNone(whatsapp.get("sent_at"))

    def test_no_whatsapp_rows_without_opt_in_consent(self):
        store = InMemoryReminderStore([task(NOW + timedelta(hours=2))],
                                      {"whatsapp_enabled": True, "whatsapp_number": "+919876543210"})
        provider = FakeProvider()
        service(store, providers={"whatsapp": provider}).sync(deliver=True)
        self.assertEqual(store.rows(channel="whatsapp"), [])
        self.assertEqual(provider.sent, [])

    def test_accepted_message_is_sent_once_even_with_concurrent_workers(self):
        store = self.opted_in_store(task(NOW + timedelta(hours=2)))
        provider = FakeProvider()
        for _ in range(3):
            service(store, providers={"whatsapp": provider}).sync(deliver=True)

        self.assertEqual(len(provider.sent), 1)
        [whatsapp] = store.rows(channel="whatsapp")
        self.assertEqual((whatsapp["status"], whatsapp["provider_message_id"]), ("sent", "wamid.1"))
        self.assertEqual(provider.sent[0][0], "+919876543210")

    def test_claim_is_compare_and_set(self):
        store = self.opted_in_store(task(NOW + timedelta(hours=2)))
        service(store).sync()
        [whatsapp] = store.rows(channel="whatsapp")

        first = store.update_reminder(whatsapp["id"], {"status": "sending", "attempt_count": 1},
                                      expected_status="pending", expected_attempts=0)
        second = store.update_reminder(whatsapp["id"], {"status": "sending", "attempt_count": 1},
                                       expected_status="pending", expected_attempts=0)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_retryable_failures_back_off_then_stop_at_the_attempt_limit(self):
        store = self.opted_in_store(task(NOW + timedelta(hours=12)))  # one slot for the whole window
        provider = FakeProvider([DeliveryResult(accepted=False, retryable=True, error="HTTP 503")] * 5)

        clock = NOW
        for _ in range(6):
            service(store, clock, {"whatsapp": provider}).sync(deliver=True)
            clock += timedelta(hours=1)

        [whatsapp] = store.rows(channel="whatsapp")
        self.assertEqual(len(provider.sent), MAX_DELIVERY_ATTEMPTS)
        self.assertEqual(whatsapp["status"], "failed")
        self.assertIsNone(whatsapp["next_attempt_at"])
        self.assertEqual(whatsapp["last_error"], "HTTP 503")

    def test_permanent_failure_is_not_retried(self):
        store = self.opted_in_store(task(NOW + timedelta(hours=12)))  # one slot for the whole window
        provider = FakeProvider([DeliveryResult(accepted=False, retryable=False, error="HTTP 400 (code 132001)")])
        for hours in (0, 2, 4):
            service(store, NOW + timedelta(hours=hours), {"whatsapp": provider}).sync(deliver=True)
        self.assertEqual(len(provider.sent), 1)


class InboxTests(unittest.TestCase):
    def test_snoozed_and_dismissed_reminders_are_hidden_until_due(self):
        store = InMemoryReminderStore([task(NOW + timedelta(hours=2))])
        svc = service(store)
        svc.sync()
        [reminder] = svc.visible()

        svc.mark(reminder["id"], {"snoozed_until": to_iso(NOW + timedelta(hours=1))})
        self.assertEqual(svc.visible(), [])
        self.assertEqual(len(service(store, NOW + timedelta(hours=1)).visible()), 1)

        svc.mark(reminder["id"], {"dismissed_at": to_iso(NOW)})
        self.assertEqual(service(store, NOW + timedelta(hours=5)).visible(), [])


class SupabaseStoreTests(unittest.TestCase):
    def test_missing_tables_raise_a_clear_migration_error(self):
        database = Mock()
        database.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.side_effect = (
            APIError({"code": "PGRST205", "message": "Could not find the table"})
        )
        with self.assertRaises(RemindersUnavailable) as raised:
            SupabaseReminderStore(database).get_preferences()
        self.assertIn("supabase/migrations", str(raised.exception))

    def test_missing_table_privileges_point_to_the_grants_migration(self):
        database = Mock()
        database.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.side_effect = (
            APIError({"code": "42501", "message": "permission denied for table reminder_preferences"})
        )
        with self.assertRaises(RemindersUnavailable) as raised:
            SupabaseReminderStore(database).get_preferences()
        self.assertIn("task_reminders_grants.sql", str(raised.exception))

    def test_inserts_are_idempotent_upserts_on_the_unique_key(self):
        database = Mock()
        database.table.return_value.upsert.return_value.execute.return_value = SimpleNamespace(data=[])
        SupabaseReminderStore(database).insert_reminders([{"idempotency_key": "k"}])
        database.table.return_value.upsert.assert_called_once_with(
            [{"idempotency_key": "k"}], on_conflict="idempotency_key", ignore_duplicates=True
        )


class ReminderApiTests(unittest.TestCase):
    def setUp(self):
        self.http = TestClient(main.app)
        self.item = task(NOW + timedelta(hours=2))
        self.store = InMemoryReminderStore([self.item], {"timezone": "Asia/Kolkata"})
        service(self.store).sync()
        [self.reminder] = self.store.rows()
        self.patches = [
            patch.object(main, "reminder_service", lambda: service(self.store, providers={"whatsapp": FakeProvider()})),
            patch.object(main, "refresh_reminders_quietly", lambda: service(self.store).sync()),
            patch.object(main, "utcnow", lambda: NOW),
            patch.object(main, "db", self.task_db()),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()

    def task_db(self):
        database = Mock()

        def update(values):
            query = Mock()

            def eq(_column, task_id):
                def execute():
                    if task_id not in self.store.tasks:
                        return SimpleNamespace(data=[])
                    self.store.tasks[task_id].update(values)
                    return SimpleNamespace(data=[dict(self.store.tasks[task_id])])
                return SimpleNamespace(execute=execute)

            query.eq.side_effect = eq
            return query

        database.table.return_value.update.side_effect = update
        return database

    def test_inbox_lists_reminder_with_task_context(self):
        response = self.http.get("/reminders")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["unread_count"], 1)
        self.assertEqual(body["reminders"][0]["task"]["title"], "Submit assignment")

    def test_completed_response_completes_the_task_and_clears_the_reminder(self):
        response = self.http.post(f"/reminders/{self.reminder['id']}/respond", json={"action": "completed"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.tasks[self.item["id"]]["status"], "done")
        self.assertEqual(self.http.get("/reminders").json()["reminders"], [])

    def test_snooze_and_not_yet_do_not_change_the_task(self):
        not_yet = self.http.post(f"/reminders/{self.reminder['id']}/respond", json={"action": "not_yet"})
        snoozed = self.http.post(f"/reminders/{self.reminder['id']}/respond",
                                 json={"action": "snooze", "snooze_minutes": 30})

        self.assertEqual((not_yet.status_code, snoozed.status_code), (200, 200))
        self.assertEqual(snoozed.json()["reminder"]["response"], "snoozed")
        self.assertEqual(self.store.tasks[self.item["id"]]["status"], "pending")
        self.assertEqual(self.http.get("/reminders").json()["reminders"], [])

    def test_change_deadline_must_be_future_and_reschedules(self):
        past = self.http.post(f"/reminders/{self.reminder['id']}/respond",
                              json={"action": "change_deadline", "due_at": to_iso(NOW - timedelta(hours=1))})
        self.assertEqual(past.status_code, 422)

        new_due = NOW + timedelta(days=3)
        moved = self.http.post(f"/reminders/{self.reminder['id']}/respond",
                               json={"action": "change_deadline", "due_at": to_iso(new_due)})
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(parse_timestamp(self.store.tasks[self.item["id"]]["due_at"]), new_due)
        self.assertEqual(self.store.reminders[self.reminder["id"]]["status"], "superseded")

    def test_unknown_or_malformed_reminder_ids_return_404(self):
        for reminder_id in ("not-a-uuid", str(uuid.uuid4())):
            self.assertEqual(self.http.post(f"/reminders/{reminder_id}/read").status_code, 404)

    def test_whatsapp_opt_in_requires_number_and_explicit_consent(self):
        base = {"timezone": "Asia/Kolkata", "whatsapp_enabled": True}
        no_number = self.http.put("/reminders/preferences", json=base)
        no_consent = self.http.put("/reminders/preferences", json={**base, "whatsapp_number": "+919876543210"})
        bad_number = self.http.put("/reminders/preferences", json={**base, "whatsapp_number": "98765"})
        ok = self.http.put("/reminders/preferences",
                           json={**base, "whatsapp_number": "+919876543210", "whatsapp_consent": True})
        off = self.http.put("/reminders/preferences", json={"timezone": "Asia/Kolkata", "whatsapp_enabled": False})

        self.assertEqual([r.status_code for r in (no_number, no_consent, bad_number)], [422, 422, 422])
        self.assertEqual(ok.status_code, 200)
        self.assertIsNotNone(ok.json()["whatsapp_consent_at"])
        self.assertIsNone(off.json()["whatsapp_consent_at"])

    def test_invalid_timezone_preference_is_rejected(self):
        response = self.http.put("/reminders/preferences", json={"timezone": "Not/AZone"})
        self.assertEqual(response.status_code, 422)

    def test_reminder_endpoints_report_missing_migration(self):
        broken = Mock()
        broken.sync.side_effect = RemindersUnavailable("Reminder tables are missing. Run supabase/migrations/x.sql")
        with patch.object(main, "reminder_service", return_value=broken):
            response = self.http.get("/reminders")
        self.assertEqual(response.status_code, 503)
        self.assertIn("supabase/migrations", response.json()["detail"])

    def test_cron_endpoint_requires_its_secret(self):
        with patch.dict(os.environ, {"REMINDER_CRON_SECRET": ""}):
            self.assertEqual(self.http.post("/internal/reminders/run").status_code, 404)
        with patch.dict(os.environ, {"REMINDER_CRON_SECRET": "unit-cron-secret"}):
            wrong = self.http.post("/internal/reminders/run", headers={"X-Reminder-Cron-Secret": "nope"})
            right = self.http.post("/internal/reminders/run", headers={"X-Reminder-Cron-Secret": "unit-cron-secret"})
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(right.status_code, 200)
        self.assertIn("summary", right.json())


if __name__ == "__main__":
    unittest.main()


class WhatsAppProviderTests(unittest.TestCase):
    ENV = {
        "WHATSAPP_ACCESS_TOKEN": "unit-wa-token",
        "WHATSAPP_PHONE_NUMBER_ID": "123456",
        "WHATSAPP_TEMPLATE_NAME": "task_deadline_reminder",
    }

    def provider(self, handler):
        import httpx
        from notifications import WhatsAppCloudProvider

        return WhatsAppCloudProvider(httpx.Client(transport=httpx.MockTransport(handler)))

    def test_unconfigured_provider_never_calls_the_api(self):
        calls = []
        with patch.dict(os.environ, {name: "" for name in self.ENV}):
            result = self.provider(lambda request: calls.append(request)).send(to="+911", template_params=[])
        self.assertFalse(result.accepted)
        self.assertEqual(calls, [])

    def test_sends_an_approved_template_and_requires_a_message_id(self):
        import httpx
        import json as json_module

        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers["authorization"]
            seen["body"] = json_module.loads(request.content)
            return httpx.Response(200, json={"messages": [{"id": "wamid.abc"}]})

        with patch.dict(os.environ, self.ENV):
            result = self.provider(handler).send(to="+919876543210", template_params=["Essay", "Fri 17 Oct"])

        self.assertTrue(result.accepted)
        self.assertEqual(result.provider_message_id, "wamid.abc")
        self.assertTrue(seen["url"].endswith("/123456/messages"))
        self.assertEqual(seen["auth"], "Bearer unit-wa-token")
        self.assertEqual(seen["body"]["type"], "template")
        self.assertEqual(seen["body"]["to"], "919876543210")
        self.assertEqual(seen["body"]["template"]["name"], "task_deadline_reminder")
        params = seen["body"]["template"]["components"][0]["parameters"]
        self.assertEqual([p["text"] for p in params], ["Essay", "Fri 17 Oct"])

    def test_errors_are_classified_without_leaking_the_token(self):
        import httpx

        cases = [(429, True), (503, True), (400, False), (401, False)]
        for status, retryable in cases:
            with self.subTest(status=status), patch.dict(os.environ, self.ENV):
                result = self.provider(
                    lambda request, s=status: httpx.Response(s, json={"error": {"code": 131026}})
                ).send(to="+919876543210", template_params=["a", "b"])
                self.assertFalse(result.accepted)
                self.assertEqual(result.retryable, retryable)
                self.assertNotIn("unit-wa-token", result.error)

    def test_http_200_without_message_id_is_not_treated_as_sent(self):
        import httpx

        with patch.dict(os.environ, self.ENV):
            result = self.provider(lambda request: httpx.Response(200, json={})).send(to="+91", template_params=[])
        self.assertFalse(result.accepted)
