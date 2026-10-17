import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { API, API_CONFIGURED, browserTimeZone, errorDetail } from "./api";
import { ReminderCenter, ReminderSettings, UpcomingReminders, useReminders } from "./Reminders";

const tabs = ["My Tasks", "Reminders", "Calendar", "Documents"];
const CONVERSATION_STORAGE_KEY = "taskmate.ai.conversations.v1";
// Longer than the backend's own bounds (Gemini deadline 60s + ArmorIQ/MCP
// timeouts), so the server normally answers first with a specific error.
const CHAT_TIMEOUT_MS = 150_000;
const DAY_MS = 24 * 60 * 60 * 1000;
// Agent error types that mean ArmorIQ evaluated the action and did not allow it.
const SECURITY_DECISIONS = new Set(["policy_denied", "policy_hold"]);

function readStoredJson(key, fallback) {
  try {
    const value = localStorage.getItem(key);
    return value ? JSON.parse(value) : fallback;
  } catch {
    return fallback;
  }
}

function readConversations() {
  const value = readStoredJson(CONVERSATION_STORAGE_KEY, []);
  return Array.isArray(value)
    ? value.filter(
        (item) =>
          item &&
          typeof item.id === "string" &&
          typeof item.title === "string" &&
          Array.isArray(item.messages)
      )
    : [];
}

function toLocalDateTime(value, endOfDay = false) {
  if (!value) return undefined;
  const date = new Date(`${value}T${endOfDay ? "23:59:00" : "00:00:00"}`);
  return date.toISOString();
}

function formatDate(value, options = { day: "numeric", month: "short" }) {
  if (!value) return "Not set";
  return new Date(value).toLocaleDateString(undefined, options);
}

function deadlineState(task) {
  if (!task.due_at || task.status === "done") return "";
  const remaining = new Date(task.due_at) - new Date();
  if (remaining < 0) return "overdue";
  return remaining <= DAY_MS ? "soon" : "";
}

function formatTime(value) {
  if (!value) return "";
  return new Date(value).toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
}

function formatPlannedPeriod(task) {
  if (task.scheduled_start && task.scheduled_end) {
    return `${formatDate(task.scheduled_start)} – ${formatDate(task.scheduled_end)}`;
  }
  if (task.scheduled_start) return `Starts ${formatDate(task.scheduled_start)}`;
  if (task.scheduled_end) return `Ends ${formatDate(task.scheduled_end)}`;
  return "Not planned";
}

function conversationGroup(updatedAt) {
  const updated = new Date(updatedAt);
  const now = new Date();
  const sameDay =
    updated.getFullYear() === now.getFullYear() &&
    updated.getMonth() === now.getMonth() &&
    updated.getDate() === now.getDate();
  if (sameDay) return "Today";
  return now - updated < 7 * 24 * 60 * 60 * 1000 ? "Previous 7 days" : "Earlier";
}

function conversationTimestamp(updatedAt) {
  const updated = new Date(updatedAt);
  return conversationGroup(updatedAt) === "Today"
    ? updated.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })
    : updated.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function conversationTitle(message) {
  const title = message.trim().replace(/\s+/g, " ");
  return title.length > 42 ? `${title.slice(0, 39)}...` : title;
}

function safeDocumentUrl(value) {
  if (!value) return false;
  try {
    const protocol = new URL(value).protocol;
    return protocol === "http:" || protocol === "https:";
  } catch {
    return false;
  }
}

function RobotMascot({ className = "" }) {
  return (
    <svg className={className} viewBox="0 0 40 40" role="img" aria-label="TaskMate robot">
      <circle cx="20" cy="20" r="20" fill="#07865f" />
      <path d="M20 6v4m-3-1h6" fill="none" stroke="#d9f2e5" strokeLinecap="round" strokeWidth="2" />
      <rect x="8" y="12" width="24" height="20" rx="7" fill="#e9f5ef" />
      <rect x="11" y="15" width="18" height="13" rx="4" fill="#143d32" />
      <circle cx="16" cy="21" r="1.8" fill="#7ce2bd" />
      <circle cx="24" cy="21" r="1.8" fill="#7ce2bd" />
      <path d="M16 25h8" stroke="#7ce2bd" strokeLinecap="round" strokeWidth="1.5" />
      <circle cx="12" cy="33" r="1.3" fill="#d9f2e5" />
      <circle cx="28" cy="33" r="1.3" fill="#d9f2e5" />
    </svg>
  );
}

function findOverlaps(tasks) {
  const scheduled = tasks
    .filter(
      (task) =>
        task.status !== "done" && task.scheduled_start && task.scheduled_end
    )
    .sort(
      (first, second) =>
        new Date(first.scheduled_start) - new Date(second.scheduled_start)
    );
  const overlaps = [];

  for (let first = 0; first < scheduled.length; first += 1) {
    for (let second = first + 1; second < scheduled.length; second += 1) {
      if (
        new Date(scheduled[second].scheduled_start) >=
        new Date(scheduled[first].scheduled_end)
      ) {
        break;
      }
      overlaps.push([scheduled[first], scheduled[second]]);
    }
  }

  return overlaps;
}

export default function App() {
  const [tasks, setTasks] = useState([]);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [dueDate, setDueDate] = useState("");
  const [timelineStart, setTimelineStart] = useState("");
  const [timelineEnd, setTimelineEnd] = useState("");
  const [documentUrl, setDocumentUrl] = useState("");
  const [priority, setPriority] = useState("normal");
  const [filter, setFilter] = useState("all");
  const [activeTab, setActiveTab] = useState("My Tasks");
  const [conversationSearch, setConversationSearch] = useState("");
  const [draft, setDraft] = useState("");
  const [conversations, setConversations] = useState(readConversations);
  const [activeConversationId, setActiveConversationId] = useState(null);
  const [chatBusy, setChatBusy] = useState(false);
  const [backendStatus, setBackendStatus] = useState("connecting");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [taskError, setTaskError] = useState("");
  const [taskNotice, setTaskNotice] = useState("");
  const chatInFlight = useRef(false);
  const taskSaveInFlight = useRef(false);
  const taskUpdatesInFlight = useRef(new Set());
  const taskDeletesInFlight = useRef(new Set());
  const [updatingTaskIds, setUpdatingTaskIds] = useState(() => new Set());
  const [deletingTaskIds, setDeletingTaskIds] = useState(() => new Set());
  const [highlightedTaskId, setHighlightedTaskId] = useState(null);
  const activeConversation = conversations.find(
    (conversation) => conversation.id === activeConversationId
  );
  const messages = activeConversation?.messages || [];

  const loadTasks = useCallback(async () => {
    setLoading(true);
    setError("");

    if (!API_CONFIGURED) {
      setError("This build has no backend URL. Set VITE_API_BASE_URL and rebuild the frontend.");
      setBackendStatus("offline");
      setLoading(false);
      return;
    }
    let response;
    try {
      response = await fetch(`${API}/tasks?status=all`);
    } catch {
      setError("Cannot reach the backend. Make sure FastAPI is running; TaskMate will reconnect automatically.");
      setBackendStatus("offline");
      setLoading(false);
      return;
    }
    try {
      const data = await response.json().catch(() => ({}));
      // The backend answered, so it is reachable even when the request failed.
      setBackendStatus("online");
      if (!response.ok) throw new Error(errorDetail(data, "Could not load tasks."));
      setTasks(data.tasks || []);
    } catch (err) {
      setError(err.message || "Could not load tasks.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadTasks();
  }, [loadTasks]);

  // Recover on its own after a backend restart instead of keeping a stale banner.
  useEffect(() => {
    if (backendStatus !== "offline" || !API_CONFIGURED) return undefined;
    const timer = setInterval(loadTasks, 10_000);
    return () => clearInterval(timer);
  }, [backendStatus, loadTasks]);

  const reminders = useReminders({ onTaskChanged: updateTaskFromResponse });

  function openTask(taskId) {
    setActiveTab("My Tasks");
    setFilter("all");
    setHighlightedTaskId(taskId);
    requestAnimationFrame(() =>
      document.getElementById(`task-row-${taskId}`)?.scrollIntoView({ block: "center", behavior: "smooth" })
    );
  }

  useEffect(() => {
    try {
      localStorage.setItem(CONVERSATION_STORAGE_KEY, JSON.stringify(conversations));
    } catch {
      setTaskError("Chat history couldn't be saved in this browser.");
    }
  }, [conversations]);

  const pendingTasks = useMemo(
    () => tasks.filter((task) => task.status !== "done"),
    [tasks]
  );
  const pendingCount = pendingTasks.length;
  const visibleTasks = useMemo(() => {
    return tasks.filter((task) => {
      const matchesStatus =
        filter === "all" ||
        (filter === "pending" && task.status !== "done") ||
        (filter === "done" && task.status === "done");
      return matchesStatus;
    });
  }, [tasks, filter]);
  const visibleConversations = useMemo(() => {
    const query = conversationSearch.trim().toLowerCase();
    return [...conversations]
      .filter((conversation) =>
        `${conversation.title} ${conversation.messages.map((message) => message.content).join(" ")}`
          .toLowerCase()
          .includes(query)
      )
      .sort((first, second) => new Date(second.updatedAt) - new Date(first.updatedAt));
  }, [conversations, conversationSearch]);
  const conversationGroups = useMemo(() => {
    const groups = new Map();
    for (const conversation of visibleConversations) {
      const label = conversationGroup(conversation.updatedAt);
      if (!groups.has(label)) groups.set(label, []);
      groups.get(label).push(conversation);
    }
    return [...groups.entries()];
  }, [visibleConversations]);
  const overdueCount = pendingTasks.filter((task) => deadlineState(task) === "overdue").length;
  const overlaps = useMemo(() => findOverlaps(tasks), [tasks]);
  const scheduledCount = tasks.filter(
    (task) => task.scheduled_start && task.scheduled_end
  ).length;
  const documentTasks = tasks.filter((task) => safeDocumentUrl(task.document_url));
  const calendarTasks = tasks
    .filter((task) => task.scheduled_start || task.scheduled_end || task.due_at)
    .sort(
      (first, second) =>
        new Date(first.scheduled_start || first.due_at || first.scheduled_end) -
        new Date(second.scheduled_start || second.due_at || second.scheduled_end)
    );

  async function addTask(event) {
    event.preventDefault();
    if (!title.trim() || taskSaveInFlight.current) return;
    if (timelineStart && timelineEnd && timelineEnd < timelineStart) {
      setTaskError("Planned end date can't be before the start date.");
      return;
    }

    taskSaveInFlight.current = true;
    setSaving(true);
    setTaskError("");
    setTaskNotice("");
    const task = {
      title: title.trim(),
      description: description.trim(),
      priority,
      ...(dueDate ? { due_at: new Date(`${dueDate}T23:59:00`).toISOString() } : {}),
      ...(timelineStart
        ? { scheduled_start: toLocalDateTime(timelineStart) }
        : {}),
      ...(timelineEnd
        ? { scheduled_end: toLocalDateTime(timelineEnd, true) }
        : {}),
      ...(documentUrl.trim() ? { document_url: documentUrl.trim() } : {}),
    };

    try {
      const response = await fetch(`${API}/tasks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(task),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(errorDetail(data, "Could not save task."));
      }

      setTitle("");
      setDescription("");
      setDueDate("");
      setTimelineStart("");
      setTimelineEnd("");
      setDocumentUrl("");
      setPriority("normal");
      setTaskNotice("Task saved.");
      await loadTasks();
    } catch (err) {
      setTaskError(err.message || "Task couldn't be saved.");
    } finally {
      taskSaveInFlight.current = false;
      setSaving(false);
    }
  }

  async function toggleTask(task) {
    if (
      taskUpdatesInFlight.current.has(task.id) ||
      taskDeletesInFlight.current.has(task.id)
    ) return;
    taskUpdatesInFlight.current.add(task.id);
    setUpdatingTaskIds((current) => new Set(current).add(task.id));
    setTaskError("");
    setTaskNotice("");
    try {
      const response = await fetch(
        `${API}/tasks/${encodeURIComponent(task.id)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            status: task.status === "done" ? "pending" : "done",
          }),
        }
      );
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(errorDetail(data, "Could not update task."));
      }
      const updatedTask = await response.json();
      setTasks((current) =>
        current.map((item) => (item.id === updatedTask.id ? updatedTask : item))
      );
      setTaskNotice(updatedTask.status === "done" ? "Task completed." : "Task reopened.");
      reminders.refresh();
    } catch (err) {
      setTaskError(err.message || "Task status couldn't be updated.");
    } finally {
      taskUpdatesInFlight.current.delete(task.id);
      setUpdatingTaskIds((current) => {
        const next = new Set(current);
        next.delete(task.id);
        return next;
      });
    }
  }

  async function deleteTask(task) {
    if (
      taskDeletesInFlight.current.has(task.id) ||
      taskUpdatesInFlight.current.has(task.id)
    ) return;
    if (!window.confirm(`Delete "${task.title}"? This can't be undone.`)) return;

    taskDeletesInFlight.current.add(task.id);
    setDeletingTaskIds((current) => new Set(current).add(task.id));
    setTaskError("");
    setTaskNotice("");
    try {
      const response = await fetch(
        `${API}/tasks/${encodeURIComponent(task.id)}`,
        { method: "DELETE" }
      );
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorDetail(data, "Could not delete task."));
      if (data.deleted !== true || data.task_id !== task.id) {
        throw new Error("The server didn't confirm this task deletion.");
      }
      setTasks((current) => current.filter((item) => item.id !== task.id));
      reminders.refresh();
    } catch (err) {
      setTaskError(err.message || "Task couldn't be deleted.");
    } finally {
      taskDeletesInFlight.current.delete(task.id);
      setDeletingTaskIds((current) => {
        const next = new Set(current);
        next.delete(task.id);
        return next;
      });
    }
  }

  function appendMessage(conversationId, message) {
    const timestamp = new Date().toISOString();
    const storedMessage = { ...message, id: message.id || crypto.randomUUID() };
    setConversations((current) => {
      const existing = current.find((conversation) => conversation.id === conversationId);
      if (existing) {
        return current.map((conversation) =>
          conversation.id === conversationId
            ? {
                ...conversation,
                updatedAt: timestamp,
                messages: [...conversation.messages, storedMessage],
              }
            : conversation
        );
      }
      return [
        {
          id: conversationId,
          title: storedMessage.role === "user" ? conversationTitle(storedMessage.content) : "New conversation",
          createdAt: timestamp,
          updatedAt: timestamp,
          messages: [storedMessage],
        },
        ...current,
      ];
    });
  }

  function updateTaskFromResponse(task) {
    setTasks((current) => {
      if (current.some((item) => item.id === task.id)) {
        return current.map((item) => (item.id === task.id ? task : item));
      }
      return [task, ...current];
    });
  }

  async function sendAgentMessage(content, conversationId) {
    if (chatInFlight.current) return;
    chatInFlight.current = true;
    setChatBusy(true);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), CHAT_TIMEOUT_MS);
    try {
      const response = await fetch(`${API}/agent/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: content, timezone: browserTimeZone() }),
        signal: controller.signal,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(errorDetail(data, "TaskMate couldn't get a response. Please try again."));
        error.data = data;
        throw error;
      }
      if (data.action === "create_task" || data.action === "complete_task") {
        if (!data.task?.id) {
          throw new Error("TaskMate couldn't confirm the saved task.");
        }
        updateTaskFromResponse(data.task);
        reminders.refresh();
      }
      if (data.action === "delete_task" && data.deleted_task_id) {
        setTasks((current) => current.filter((item) => item.id !== data.deleted_task_id));
      }
      if (typeof data.reply !== "string" || !data.reply.trim()) {
        throw new Error("Gemini returned no text. Please try again.");
      }
      appendMessage(conversationId, {
        role: "assistant",
        content: data.reply.trim(),
        ...(data.action
          ? { security: { decision: "allow", action: data.action, traceId: data.trace_id, requestId: data.request_id } }
          : {}),
      });
    } catch (err) {
      const data = err.data || {};
      const message =
        err.name === "AbortError"
          ? "TaskMate didn't respond in time. Refresh your task list before trying again, in case the action still completed."
          : err.message === "Failed to fetch"
          ? "TaskMate couldn't reach the chat service. Check that the backend is running."
          : err.message || "TaskMate couldn't get a response. Please try again.";
      if (err.name === "AbortError") loadTasks();
      appendMessage(conversationId, {
        role: "assistant",
        content: message,
        tone: SECURITY_DECISIONS.has(data.error_type) ? "denied" : "error",
        ...(data.request_id || data.trace_id
          ? { security: { decision: data.error_type || "error", reason: data.reason, traceId: data.trace_id, requestId: data.request_id } }
          : {}),
      });
    } finally {
      clearTimeout(timer);
      chatInFlight.current = false;
      setChatBusy(false);
    }
  }

  function submitMessage(event) {
    event.preventDefault();
    const content = draft.trim();
    if (!content || chatInFlight.current) return;

    const conversationId = activeConversationId || crypto.randomUUID();
    setActiveConversationId(conversationId);
    appendMessage(conversationId, { role: "user", content });
    setDraft("");
    setTaskNotice("");
    setTaskError("");

    void sendAgentMessage(content, conversationId);
  }

  function startNewChat() {
    setDraft("");
    setActiveConversationId(null);
  }

  return (
    <div className="assistant-shell">
      <aside className="conversation-sidebar">
        <a className="brand-lockup" href="#home" aria-label="TaskMate AI home">
          <RobotMascot className="brand-mark" />
          <span>
            <strong>TaskMate AI</strong>
            <small>Your personal task assistant</small>
          </span>
        </a>

        <label className="search-box">
          <span aria-hidden="true">⌕</span>
          <input
            aria-label="Search conversations"
            placeholder="Search conversations"
            value={conversationSearch}
            onChange={(event) => setConversationSearch(event.target.value)}
          />
          {conversationSearch && (
            <button type="button" onClick={() => setConversationSearch("")} aria-label="Clear search">
              ×
            </button>
          )}
        </label>

        <div className="sidebar-section-heading">
          <span>CONVERSATIONS · THIS SESSION</span>
          <span className="quiet-count">{visibleConversations.length}</span>
        </div>
        <button
          className={`conversation-item assistant-conversation ${!activeConversationId ? "selected" : ""}`}
          type="button"
          onClick={startNewChat}
        >
          <RobotMascot className="conversation-robot" />
          <span className="conversation-copy">
            <span className="conversation-title">Task Assistant</span>
            <span className="conversation-preview">{!activeConversationId ? "Start a conversation" : "New conversation"}</span>
          </span>
        </button>
        {conversationGroups.length ? (
          <div className="conversation-list">
            {conversationGroups.map(([group, items]) => (
              <div className="conversation-group" key={group}>
                <div className="conversation-group-label">{group}</div>
                {items.map((conversation) => {
                  const latestMessage = conversation.messages.at(-1);
                  return (
                    <button
                      className={`conversation-item ${conversation.id === activeConversationId ? "selected" : ""}`}
                      key={conversation.id}
                      type="button"
                      onClick={() => setActiveConversationId(conversation.id)}
                    >
                      <span className="conversation-icon" aria-hidden="true">✳</span>
                      <span className="conversation-copy">
                        <span className="conversation-title">{conversation.title}</span>
                        <span className="conversation-preview">{latestMessage?.content || ""}</span>
                      </span>
                      <span className="conversation-time">{conversationTimestamp(conversation.updatedAt)}</span>
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
        ) : (
          <div className="conversation-empty">
            <span className="conversation-icon" aria-hidden="true">✳</span>
            <span>{conversationSearch ? "No matching conversations." : "Your conversations will appear here."}</span>
          </div>
        )}

        <div className="sidebar-section-heading task-group-heading">
          <span>TASK GROUPS</span>
        </div>
        <button
          className={`group-link ${filter === "all" ? "selected" : ""}`}
          onClick={() => setFilter("all")}
          type="button"
        >
          <span className="group-symbol">▤</span>
          <span>All tasks</span>
          <span className="group-count">{tasks.length}</span>
        </button>
        <button
          className={`group-link ${filter === "pending" ? "selected" : ""}`}
          onClick={() => setFilter("pending")}
          type="button"
        >
          <span className="group-symbol">◷</span>
          <span>Open</span>
          <span className="group-count">{pendingCount}</span>
        </button>
        <button
          className={`group-link ${filter === "done" ? "selected" : ""}`}
          onClick={() => setFilter("done")}
          type="button"
        >
          <span className="group-symbol">✓</span>
          <span>Completed</span>
          <span className="group-count">{tasks.length - pendingCount}</span>
        </button>

        <button className="new-chat-button" onClick={startNewChat} type="button">
          <span aria-hidden="true">+</span> New Chat
        </button>
        <div className="sidebar-profile">
          <span className="profile-avatar">H</span>
          <span><strong>My workspace</strong><small>Personal task manager</small></span>
          <span className="profile-menu" aria-hidden="true">···</span>
        </div>
      </aside>

      <main className="chat-panel">
        <header className="chat-header">
          <div className="assistant-avatar"><RobotMascot /></div>
          <div className="chat-heading">
            <h1>Task Assistant</h1>
            <p><span className={`status-dot ${backendStatus}`} />{backendStatus === "online" ? "Online · Tasks connected" : backendStatus === "offline" ? "Backend unavailable" : "Connecting to backend"}</p>
          </div>
          <button
            className="icon-button chat-menu reminder-bell"
            type="button"
            aria-label={`Reminders, ${reminders.unreadCount} unread`}
            title="Reminders"
            onClick={() => setActiveTab("Reminders")}
          >
            <span aria-hidden="true">🔔</span>
            {reminders.unreadCount > 0 && <span className="bell-count">{reminders.unreadCount}</span>}
          </button>
          <button
            className="icon-button"
            type="button"
            aria-label="Refresh tasks"
            title="Refresh tasks"
            onClick={() => { loadTasks(); reminders.refresh(); }}
          >
            ↻
          </button>
        </header>

        <div className="chat-history" aria-live="polite">
          <div className="date-divider"><span>YOUR TASKS, IN ONE PLACE</span></div>
          {messages.length === 0 ? (
            <div className="chat-empty-state">
              <div className="empty-assistant-avatar"><RobotMascot /></div>
              <h2>Hi, I’m your Task Assistant</h2>
              <p>Ask me to list, add, or complete tasks. ArmorIQ policy checks every task action I take before it reaches your list.</p>
              <div className="example-prompts" aria-label="Example prompts">
                {["Show my open tasks", "Add a task to review my project tomorrow", "Mark my project review as done"].map((prompt) => (
                  <button type="button" key={prompt} onClick={() => setDraft(prompt)}>{prompt}</button>
                ))}
              </div>
            </div>
          ) : (
            <div className="message-list">
              {messages.map((message, index) => (
                <div className={`message-row ${message.role}`} key={message.id || `${index}-${message.role}`}>
                  {message.role === "assistant" && <span className="message-avatar"><RobotMascot /></span>}
                  <div className={`message-bubble ${message.tone || ""}`}>
                    {message.tone === "denied" && <span className="security-label">Blocked by ArmorIQ policy</span>}
                    <p>{message.content}</p>
                    {message.security && (
                      <small className="security-meta">
                        {message.security.decision === "allow"
                          ? `Authorized by ArmorIQ · ${message.security.action}`
                          : message.security.reason
                            ? `Reason: ${message.security.reason}`
                            : "Not confirmed"}
                        {message.security.traceId && <> · trace <code>{message.security.traceId}</code></>}
                      </small>
                    )}
                  </div>
                </div>
              ))}
              {chatBusy && <div className="chat-progress" role="status">TaskMate is thinking…</div>}
            </div>
          )}
          {error && <div className="chat-alert error-notice" role="alert">{error}</div>}
        </div>

        <form className="message-composer" onSubmit={submitMessage}>
          <button className="icon-button attachment-button" type="button" aria-label="Document uploads are unavailable" title="Document uploads are not configured" disabled>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 12.5 14.8 5.7a3 3 0 0 1 4.2 4.2l-8.5 8.5a5 5 0 0 1-7.1-7.1l8.2-8.2" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" /></svg>
          </button>
          <input
            aria-label="Message"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Ask TaskMate about your tasks"
          />
          <button className="send-button" type="submit" aria-label="Send message" title="Send message" disabled={chatBusy || !draft.trim()}>
            {chatBusy ? "…" : "↑"}
          </button>
        </form>
      </main>

      <aside className="task-workspace">
        <header className="workspace-topbar">
          <span className="connection-label"><span className="status-dot" /> Live task data</span>
          <span className="profile-avatar small-avatar">H</span>
        </header>

        <nav className="workspace-tabs" aria-label="Task workspace views">
          {tabs.map((tab) => (
            <button
              className={activeTab === tab ? "active" : ""}
              key={tab}
              onClick={() => setActiveTab(tab)}
              type="button"
            >
              <span aria-hidden="true">{tab === "My Tasks" ? "✓" : tab === "Reminders" ? "◷" : tab === "Calendar" ? "▦" : "▧"}</span>
              {tab}
              {tab === "Reminders" && reminders.unreadCount > 0 && <span className="tab-count">{reminders.unreadCount}</span>}
            </button>
          ))}
        </nav>

        {activeTab === "My Tasks" && (
          <>
            <button
              className="workspace-add-button"
              type="button"
              onClick={() => document.getElementById("task-title")?.focus()}
            >
              <span aria-hidden="true">+</span> Add Task
            </button>
            <section className="task-table-section" aria-label="Tasks">
              <div className="table-toolbar">
                <h2>My Tasks <span>{tasks.length}</span>{overdueCount > 0 && <span className="overdue-count">{overdueCount} overdue</span>}</h2>
                <select aria-label="Filter tasks" value={filter} onChange={(event) => setFilter(event.target.value)}>
                  <option value="all">All</option>
                  <option value="pending">Open</option>
                  <option value="done">Done</option>
                </select>
              </div>
              {(taskError || taskNotice) && (
                <div className={`task-feedback ${taskError ? "error-notice" : ""}`} role={taskError ? "alert" : "status"}>
                  {taskError || taskNotice}
                </div>
              )}
              {loading ? (
                <div className="panel-empty">Loading tasks…</div>
              ) : visibleTasks.length === 0 ? (
                <div className="panel-empty">{tasks.length ? "No tasks in this group." : "No tasks yet. Add one here or ask TaskMate."}</div>
              ) : (
                <div className="table-scroll">
                  <table className="task-table">
                    <thead><tr><th className="checkbox-column"><span className="visually-hidden">Complete</span></th><th>Task</th><th>Timeline</th><th>Deadline</th><th>Docs</th><th>Status</th><th><span className="visually-hidden">Actions</span></th></tr></thead>
                    <tbody>
                      {visibleTasks.map((task) => (
                        <tr key={task.id} id={`task-row-${task.id}`} className={highlightedTaskId === task.id ? "highlighted-row" : ""}>
                          <td className="checkbox-column">
                            <input
                              type="checkbox"
                              checked={task.status === "done"}
                              disabled={updatingTaskIds.has(task.id) || deletingTaskIds.has(task.id)}
                              onChange={() => toggleTask(task)}
                              aria-label={`${task.status === "done" ? "Reopen" : "Complete"} ${task.title}`}
                            />
                          </td>
                          <td className="task-name-cell"><span className={task.status === "done" ? "task-complete" : ""}>{task.title}</span></td>
                          <td><span className="date-chip timeline-chip">{formatPlannedPeriod(task)}</span></td>
                          <td><span className={`date-chip deadline-chip ${deadlineState(task)}`} title={task.due_at ? new Date(task.due_at).toLocaleString() : undefined}>{deadlineState(task) === "overdue" ? "Overdue · " : deadlineState(task) === "soon" ? "Due soon · " : ""}{task.due_at ? formatDate(task.due_at, { day: "numeric", month: "short", year: "numeric" }) : "Not set"}</span></td>
                          <td className="document-cell">
                            {safeDocumentUrl(task.document_url) ? <a href={task.document_url} target="_blank" rel="noopener noreferrer" aria-label={`Open document for ${task.title}`} title="Open saved document link">↗</a> : <span aria-label="No safe document link">—</span>}
                          </td>
                          <td><span className={`status-chip ${task.status === "done" ? "done" : "pending"}`}>{task.status === "done" ? "Completed" : "Open"}</span></td>
                          <td className="task-action-cell">
                            <button
                              className="delete-task-button"
                              type="button"
                              aria-label={`Delete ${task.title}`}
                              title="Delete task"
                              disabled={deletingTaskIds.has(task.id)}
                              onClick={() => deleteTask(task)}
                            >
                              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16m-10 4v6m4-6v6M6 7l1 14h10l1-14M9 7V4h6v3" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.7" /></svg>
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            <section className="task-form-section">
              <div className="section-heading">
                <div><h2>Add a task</h2><p>Save directly to your task list.</p></div>
              </div>
              <form className="task-form" onSubmit={addTask}>
                <label htmlFor="task-title">Task <span>*</span></label>
                <input id="task-title" value={title} onChange={(event) => setTitle(event.target.value)} placeholder="e.g. Submit assignment" maxLength={250} required />

                <div className="planned-period-fields">
                  <div className="field-intro"><strong>Planned work period</strong><small>Optional dates for when you expect to work on this task.</small></div>
                  <div className="form-split">
                    <div><label htmlFor="timeline-start">Start date</label><input id="timeline-start" type="date" value={timelineStart} onChange={(event) => setTimelineStart(event.target.value)} /></div>
                    <div><label htmlFor="timeline-end">End date</label><input id="timeline-end" type="date" value={timelineEnd} onChange={(event) => setTimelineEnd(event.target.value)} /></div>
                  </div>
                </div>

                <div className="form-split">
                  <div><label htmlFor="task-deadline">Final deadline</label><small className="field-help">The date this task must be finished.</small><input id="task-deadline" type="date" value={dueDate} onChange={(event) => setDueDate(event.target.value)} /></div>
                  <div><label htmlFor="task-priority">Priority</label><select id="task-priority" value={priority} onChange={(event) => setPriority(event.target.value)}><option value="low">Low</option><option value="normal">Normal</option><option value="high">High</option></select></div>
                </div>

                <label htmlFor="task-description">Description <span className="optional-label">Optional</span></label>
                <textarea id="task-description" value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Add details" rows={2} />
                <label htmlFor="task-document-url">Document link <span className="optional-label">Optional</span></label>
                <input id="task-document-url" type="url" value={documentUrl} onChange={(event) => setDocumentUrl(event.target.value)} placeholder="https://..." />
                <button className="add-task-button" type="submit" disabled={saving}>{saving ? "Saving…" : "+  Add Task"}</button>
              </form>
            </section>
          </>
        )}

        {activeTab === "Reminders" && (
          <>
            <section className="tab-content-section">
              <div className="section-heading"><div><h2>Reminders</h2><p>Deadline check-ins for your open tasks.</p></div></div>
              <ReminderCenter state={reminders} onOpenTask={openTask} />
            </section>
            <ReminderSettings state={reminders} />
          </>
        )}

        {activeTab === "Calendar" && (
          <section className="tab-content-section">
            <div className="section-heading"><div><h2>Calendar</h2><p>Saved task dates only. No external calendar is connected.</p></div></div>
            {calendarTasks.length ? (
              <div className="calendar-task-list">
                {calendarTasks.map((task) => (
                  <article className="calendar-task" key={task.id}>
                    <div className="calendar-date"><strong>{formatDate(task.scheduled_start || task.due_at || task.scheduled_end, { day: "2-digit" })}</strong><span>{formatDate(task.scheduled_start || task.due_at || task.scheduled_end, { month: "short" })}</span></div>
                    <div className="calendar-task-copy">
                      <h3>{task.title}</h3>
                      {task.description && <p>{task.description}</p>}
                      {(task.scheduled_start || task.scheduled_end) && <p><strong>Planned work period:</strong> {formatPlannedPeriod(task)}</p>}
                      {task.due_at && <p><strong>Final deadline:</strong> {formatDate(task.due_at, { day: "numeric", month: "short", year: "numeric" })}</p>}
                    </div>
                    <span className={`status-chip ${task.status === "done" ? "done" : "pending"}`}>{task.status === "done" ? "Done" : "Open"}</span>
                  </article>
                ))}
              </div>
            ) : <div className="panel-empty">No scheduled tasks or deadlines to show.</div>}
          </section>
        )}

        {activeTab === "Documents" && (
          <section className="tab-content-section">
            <div className="section-heading"><div><h2>Documents</h2><p>Documents linked to your saved tasks.</p></div></div>
            {documentTasks.length ? (
              <div className="document-list">
                {documentTasks.map((task) => (
                  <a className="document-item" href={task.document_url} target="_blank" rel="noopener noreferrer" key={task.id}>
                    <span className="document-icon">▧</span><span><strong>{task.title}</strong><small>Linked task document</small></span><span className="document-open">↗</span>
                  </a>
                ))}
              </div>
            ) : <div className="panel-empty">No documents are linked. Uploads are not configured yet.</div>}
          </section>
        )}

        <section className="insight-section">
          <div className="insight-heading"><span className="insight-icon reminder-icon">◷</span><div><h2>Upcoming reminders</h2><p>{reminders.channels?.whatsapp?.configured ? "In-app, plus WhatsApp if you opt in." : "In-app only · WhatsApp is not configured."}</p></div></div>
          <UpcomingReminders state={reminders} />
        </section>

        <section className="insight-section conflict-section">
          <div className="insight-heading"><span className="insight-icon conflict-icon">!</span><div><h2>Schedule conflicts</h2><p>Checked against saved task timelines.</p></div></div>
          {overlaps.length ? (
            <div className="conflict-list">
              {overlaps.map(([first, second]) => <div className="conflict-item" key={`${first.id}-${second.id}`}><strong>{first.title} + {second.title}</strong><small>{formatDate(first.scheduled_start)} · {formatTime(first.scheduled_start)}–{formatTime(first.scheduled_end)} overlaps</small></div>)}
            </div>
          ) : <p className="insight-empty">{scheduledCount ? "No overlapping scheduled tasks." : "Add timeline dates to check for overlaps."}</p>}
        </section>
      </aside>
    </div>
  );
}