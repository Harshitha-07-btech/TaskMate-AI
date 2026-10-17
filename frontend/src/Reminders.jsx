import { useCallback, useEffect, useState } from "react";
import { apiRequest, browserTimeZone } from "./api";

const POLL_MS = 2 * 60 * 1000;
const KIND_LABELS = {
  before_deadline: "Due soon",
  deadline_day: "Due today",
  overdue: "Overdue",
};

function formatWhen(value) {
  if (!value) return "";
  return new Date(value).toLocaleString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "numeric",
    minute: "2-digit",
  });
}

function tomorrowAtNine() {
  const date = new Date();
  date.setDate(date.getDate() + 1);
  date.setHours(9, 0, 0, 0);
  return date;
}

export function useReminders({ onTaskChanged }) {
  const [reminders, setReminders] = useState([]);
  const [upcoming, setUpcoming] = useState([]);
  const [preferences, setPreferences] = useState(null);
  const [channels, setChannels] = useState(null);
  const [unavailable, setUnavailable] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [busyId, setBusyId] = useState(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [inbox, next] = await Promise.all([
        apiRequest("/reminders", { fallback: "Couldn't load reminders." }),
        apiRequest("/reminders/upcoming", { fallback: "Couldn't load upcoming reminders." }),
      ]);
      setReminders(inbox.reminders || []);
      setUpcoming(next.upcoming || []);
      setUnavailable("");
    } catch (err) {
      setUnavailable(err.status === 503 ? err.message : "");
      if (err.status !== 503) setError(err.message);
    } finally {
      setLoaded(true);
    }
  }, []);

  const loadSettings = useCallback(async () => {
    try {
      const [prefs, channelInfo] = await Promise.all([
        apiRequest("/reminders/preferences", { fallback: "Couldn't load reminder settings." }),
        apiRequest("/notifications/channels", { fallback: "Couldn't load notification channels." }),
      ]);
      setChannels(channelInfo);
      if (!prefs.persisted) {
        // First run: store this browser's time zone so reminders use local time.
        const saved = await apiRequest("/reminders/preferences", {
          method: "PUT",
          body: { ...settingsBody(prefs), timezone: browserTimeZone() },
          fallback: "Couldn't save reminder settings.",
        });
        setPreferences(saved);
      } else {
        setPreferences(prefs);
      }
    } catch (err) {
      if (err.status === 503) setUnavailable(err.message);
    }
  }, []);

  useEffect(() => {
    refresh();
    loadSettings();
    const timer = setInterval(refresh, POLL_MS);
    const onFocus = () => refresh();
    window.addEventListener("focus", onFocus);
    return () => {
      clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [refresh, loadSettings]);

  async function run(reminder, path, body, successMessage) {
    setBusyId(reminder.id);
    setError("");
    setNotice("");
    try {
      const result = await apiRequest(`/reminders/${encodeURIComponent(reminder.id)}/${path}`, {
        method: "POST",
        body,
        fallback: "That reminder action didn't complete.",
      });
      if (result?.task) onTaskChanged(result.task);
      setNotice(successMessage);
      await refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusyId(null);
    }
  }

  async function savePreferences(values) {
    setError("");
    setNotice("");
    try {
      const saved = await apiRequest("/reminders/preferences", {
        method: "PUT",
        body: values,
        fallback: "Couldn't save reminder settings.",
      });
      setPreferences(saved);
      setNotice("Reminder settings saved.");
      await refresh();
      return true;
    } catch (err) {
      setError(err.message);
      return false;
    }
  }

  return {
    reminders,
    upcoming,
    preferences,
    channels,
    unavailable,
    loaded,
    busyId,
    notice,
    error,
    unreadCount: reminders.filter((reminder) => !reminder.read_at).length,
    refresh,
    savePreferences,
    markRead: (reminder) => run(reminder, "read", undefined, "Marked as read."),
    dismiss: (reminder) => run(reminder, "dismiss", undefined, "Reminder dismissed."),
    respond: (reminder, body, message) => run(reminder, "respond", body, message),
  };
}

function settingsBody(prefs) {
  return {
    timezone: prefs.timezone,
    in_app_enabled: prefs.in_app_enabled,
    before_deadline_hours: prefs.before_deadline_hours,
    deadline_day_hour: prefs.deadline_day_hour,
    overdue_interval_hours: prefs.overdue_interval_hours,
    max_overdue_reminders: prefs.max_overdue_reminders,
    whatsapp_enabled: prefs.whatsapp_enabled,
    whatsapp_number: prefs.whatsapp_number || null,
    whatsapp_consent: Boolean(prefs.whatsapp_consent_at),
  };
}

function ReminderCard({ reminder, busy, onOpenTask, actions }) {
  const [changingDeadline, setChangingDeadline] = useState(false);
  const [newDeadline, setNewDeadline] = useState("");
  const task = reminder.task || {};

  function submitDeadline(event) {
    event.preventDefault();
    if (!newDeadline) return;
    actions.respond(
      reminder,
      { action: "change_deadline", due_at: new Date(newDeadline).toISOString() },
      "Deadline updated."
    );
  }

  return (
    <article className={`reminder-card ${reminder.kind} ${reminder.read_at ? "read" : "unread"}`}>
      <div className="reminder-card-top">
        <span className={`reminder-kind ${reminder.kind}`}>{KIND_LABELS[reminder.kind] || "Reminder"}</span>
        <time dateTime={reminder.scheduled_for}>{formatWhen(reminder.scheduled_for)}</time>
      </div>
      <p className="reminder-message">{reminder.message}</p>
      <div className="reminder-actions">
        <button type="button" className="reminder-primary" disabled={busy}
          onClick={() => actions.respond(reminder, { action: "completed" }, `Marked "${task.title || "task"}" as completed.`)}>
          ✓ Completed
        </button>
        <button type="button" disabled={busy}
          onClick={() => actions.respond(reminder, { action: "not_yet" }, "Noted. I'll remind you again before the next checkpoint.")}>
          Not yet
        </button>
        <button type="button" disabled={busy}
          onClick={() => actions.respond(reminder, { action: "snooze", snooze_minutes: 60 }, "I'll remind you again in an hour.")}>
          Remind me in 1h
        </button>
        <button type="button" disabled={busy}
          onClick={() => {
            const minutes = Math.max(5, Math.round((tomorrowAtNine() - new Date()) / 60000));
            actions.respond(reminder, { action: "snooze", snooze_minutes: minutes }, "I'll remind you tomorrow morning.");
          }}>
          Tomorrow
        </button>
        <button type="button" disabled={busy} onClick={() => setChangingDeadline((value) => !value)}>
          Change deadline
        </button>
        <button type="button" disabled={busy} onClick={() => onOpenTask(reminder.task_id)}>Open task</button>
        <button type="button" className="reminder-dismiss" disabled={busy} aria-label="Dismiss reminder"
          title="Dismiss" onClick={() => actions.dismiss(reminder)}>
          ×
        </button>
      </div>
      {changingDeadline && (
        <form className="reminder-deadline-form" onSubmit={submitDeadline}>
          <label htmlFor={`deadline-${reminder.id}`}>New deadline</label>
          <input id={`deadline-${reminder.id}`} type="datetime-local" value={newDeadline}
            onChange={(event) => setNewDeadline(event.target.value)} required />
          <button type="submit" disabled={busy || !newDeadline}>Save</button>
        </form>
      )}
    </article>
  );
}

export function ReminderCenter({ state, onOpenTask }) {
  if (state.unavailable) {
    return (
      <div className="panel-empty reminder-unavailable" role="status">
        Reminders aren't available yet: {state.unavailable}
      </div>
    );
  }
  return (
    <>
      {(state.error || state.notice) && (
        <div className={`task-feedback ${state.error ? "error-notice" : ""}`} role={state.error ? "alert" : "status"}>
          {state.error || state.notice}
        </div>
      )}
      {!state.loaded ? (
        <div className="panel-empty">Loading reminders…</div>
      ) : state.reminders.length === 0 ? (
        <div className="panel-empty">No reminders right now. Tasks with deadlines will check in with you here.</div>
      ) : (
        <div className="reminder-list">
          {state.reminders.map((reminder) => (
            <ReminderCard key={reminder.id} reminder={reminder} busy={state.busyId === reminder.id}
              onOpenTask={onOpenTask} actions={state} />
          ))}
        </div>
      )}
    </>
  );
}

export function UpcomingReminders({ state }) {
  if (state.unavailable) return <p className="insight-empty">Reminder storage isn't set up yet.</p>;
  if (!state.upcoming.length) return <p className="insight-empty">No upcoming reminders.</p>;
  return (
    <div className="deadline-list">
      {state.upcoming.slice(0, 4).map((item) => (
        <div className="deadline-item" key={`${item.task_id}-${item.kind}-${item.scheduled_for}`}>
          <span className="deadline-marker" aria-hidden="true">•</span>
          <span>
            <strong>{item.title}</strong>
            <small>{KIND_LABELS[item.kind]} reminder · {formatWhen(item.scheduled_for)}</small>
          </span>
        </div>
      ))}
    </div>
  );
}

export function ReminderSettings({ state }) {
  const [form, setForm] = useState(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (state.preferences) setForm(settingsBody(state.preferences));
  }, [state.preferences]);

  if (state.unavailable || !form) return null;
  const whatsapp = state.channels?.whatsapp;

  function update(field, value) {
    setForm((current) => ({ ...current, [field]: value }));
  }

  async function submit(event) {
    event.preventDefault();
    setSaving(true);
    await state.savePreferences({ ...form, whatsapp_number: form.whatsapp_number?.trim() || null });
    setSaving(false);
  }

  return (
    <section className="task-form-section reminder-settings">
      <div className="section-heading">
        <div><h2>Reminder settings</h2><p>Times use your time zone: {form.timezone}.</p></div>
      </div>
      <form className="task-form" onSubmit={submit}>
        <label className="checkbox-row">
          <input type="checkbox" checked={form.in_app_enabled} onChange={(event) => update("in_app_enabled", event.target.checked)} />
          In-app reminders
        </label>
        <div className="form-split">
          <div>
            <label htmlFor="pref-before">Heads-up before deadline (hours)</label>
            <input id="pref-before" type="number" min="1" max="168" value={form.before_deadline_hours}
              onChange={(event) => update("before_deadline_hours", Number(event.target.value))} />
          </div>
          <div>
            <label htmlFor="pref-day-hour">Deadline-day reminder (hour, 0–23)</label>
            <input id="pref-day-hour" type="number" min="0" max="23" value={form.deadline_day_hour}
              onChange={(event) => update("deadline_day_hour", Number(event.target.value))} />
          </div>
        </div>
        <div className="form-split">
          <div>
            <label htmlFor="pref-overdue">Overdue reminder every (hours)</label>
            <input id="pref-overdue" type="number" min="1" max="168" value={form.overdue_interval_hours}
              onChange={(event) => update("overdue_interval_hours", Number(event.target.value))} />
          </div>
          <div>
            <label htmlFor="pref-overdue-max">Maximum overdue reminders</label>
            <input id="pref-overdue-max" type="number" min="0" max="30" value={form.max_overdue_reminders}
              onChange={(event) => update("max_overdue_reminders", Number(event.target.value))} />
          </div>
        </div>
        <button type="button" className="link-button" onClick={() => update("timezone", browserTimeZone())}>
          Use this browser's time zone ({browserTimeZone()})
        </button>

        <div className="channel-box">
          <strong>WhatsApp reminders</strong>
          {whatsapp?.configured ? (
            <small>Sent with an approved template through the WhatsApp Business Platform, only after you opt in.</small>
          ) : (
            <small className="channel-warning">
              Not configured: WhatsApp messages are not being sent. The server needs a WhatsApp Business
              Platform account ({(whatsapp?.missing_settings || []).join(", ") || "provider settings"}).
            </small>
          )}
          <label htmlFor="pref-wa-number">WhatsApp number (international format)</label>
          <input id="pref-wa-number" type="tel" placeholder="+919876543210" value={form.whatsapp_number || ""}
            onChange={(event) => update("whatsapp_number", event.target.value)} />
          <label className="checkbox-row">
            <input type="checkbox" checked={form.whatsapp_enabled}
              onChange={(event) => update("whatsapp_enabled", event.target.checked)} />
            Send my reminders on WhatsApp
          </label>
          {form.whatsapp_enabled && (
            <label className="checkbox-row">
              <input type="checkbox" checked={form.whatsapp_consent}
                onChange={(event) => update("whatsapp_consent", event.target.checked)} />
              I agree to receive deadline reminders on this number. I can turn this off at any time.
            </label>
          )}
        </div>
        <button className="add-task-button" type="submit" disabled={saving}>{saving ? "Saving…" : "Save settings"}</button>
      </form>
    </section>
  );
}
