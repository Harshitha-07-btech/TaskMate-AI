// Production builds must set VITE_API_BASE_URL; only the dev server falls back
// to the local backend so a deployed site never calls localhost.
export const API = (
  import.meta.env.VITE_API_BASE_URL || (import.meta.env.DEV ? "http://127.0.0.1:8000" : "")
).replace(/\/$/, "");

export const API_CONFIGURED = Boolean(API);

// FastAPI returns `detail` as a string, or as a list of field errors on 422.
export function errorDetail(data, fallback) {
  const detail = data?.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail.map((item) => item?.msg).filter(Boolean).join(" ") || fallback;
  }
  return fallback;
}

export class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

export async function apiRequest(path, { method = "GET", body, fallback = "Request failed." } = {}) {
  if (!API_CONFIGURED) {
    throw new ApiError("The backend URL isn't configured for this build (VITE_API_BASE_URL).", 0, {});
  }
  let response;
  try {
    response = await fetch(`${API}${path}`, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError("Cannot reach the TaskMate backend.", 0, {});
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(errorDetail(data, fallback), response.status, data);
  return data;
}

export function browserTimeZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}
