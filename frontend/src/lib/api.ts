/**
 * Central API client for Nautilus Web Interface
 * Handles JWT Bearer auth, automatic retry with exponential backoff,
 * and structured error objects.
 */
import { API_CONFIG } from '../config';

/** Extra options for a request. */
export interface RequestOptions {
  headers?: Record<string, string>;
  /** Retries on 5xx/network errors. Default: 2 for GET, 0 for anything that changes state. */
  retries?: number;
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly detail?: unknown,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

function getAuthHeaders(): Record<string, string> {
  const token = localStorage.getItem('nautilus_token');
  if (token) {
    return { Authorization: `Bearer ${token}` };
  }
  return {};
}

/** Clear session and dispatch event so App can transition to LoginPage without hard reload */
function handleUnauthorized(): void {
  localStorage.removeItem('nautilus_token');
  localStorage.removeItem('nautilus_role');
  // Dispatch a custom event — App.tsx listens for this to trigger re-render
  window.dispatchEvent(new CustomEvent('nautilus:unauthorized'));
}

/** Turn a FastAPI `detail` (string, {error,message} object, or validation list) into readable text. */
function describeDetail(detail: unknown, fallback: string): string {
  if (typeof detail === 'string' && detail) return detail;
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const d = detail as { message?: unknown; error?: unknown };
    if (typeof d.message === 'string' && d.message) return d.message;
    if (typeof d.error === 'string' && d.error) return d.error;
  }
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { msg?: unknown };
    if (typeof first?.msg === 'string') return first.msg;
  }
  return fallback;
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  options: RequestOptions = {},
  timeoutMs = API_CONFIG.TIMEOUT,
): Promise<T> {
  const url = `${API_CONFIG.NAUTILUS_API_URL}${path}`;
  // Never auto-retry requests that change state: a retry after a timeout/5xx can duplicate an order.
  const retries = options.retries ?? (method === 'GET' ? 2 : 0);
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...getAuthHeaders(),
    ...(options.headers ?? {}),
  };

  let lastError: unknown;
  for (let attempt = 0; attempt <= retries; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {
        method,
        headers,
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: controller.signal,
      });

      if (!response.ok) {
        // Token expired or invalid — redirect to login immediately
        if (response.status === 401) {
          handleUnauthorized();
          throw new ApiError(401, 'Session expired. Please log in again.');
        }

        const text = await response.text();
        let detail: unknown;
        try {
          detail = JSON.parse(text)?.detail;
        } catch {
          detail = text;
        }
        const message = describeDetail(detail, `HTTP ${response.status}`);
        // 4xx errors are not retried — they indicate client errors
        if (response.status < 500) {
          throw new ApiError(response.status, message, detail);
        }
        lastError = new ApiError(response.status, message, detail);
      } else {
        const data = await response.json();
        // 202 = accepted but the outcome is not known yet (e.g. order sent, exchange did not answer).
        // Surface it as an error so the UI never reports success or invites a blind retry.
        if (response.status === 202 && data && typeof data === 'object' && 'detail' in data) {
          const detail = (data as { detail: unknown }).detail;
          throw new ApiError(202, describeDetail(detail, 'Outcome unknown — check the order list before retrying'), detail);
        }
        return data as T;
      }
    } catch (err) {
      if (err instanceof ApiError && err.status < 500) throw err;
      lastError = err;
    } finally {
      clearTimeout(timer);
    }

    // Exponential backoff before retry (skip after last attempt)
    if (attempt < retries) {
      await new Promise((r) => setTimeout(r, 500 * 2 ** attempt));
    }
  }

  throw lastError ?? new Error('Request failed');
}

const api = {
  get: <T>(path: string, options?: RequestOptions) => request<T>('GET', path, undefined, options),
  post: <T>(path: string, body?: unknown, options?: RequestOptions) => request<T>('POST', path, body, options),
  put: <T>(path: string, body?: unknown, options?: RequestOptions) => request<T>('PUT', path, body, options),
  delete: <T>(path: string, options?: RequestOptions) => request<T>('DELETE', path, undefined, options),
  /** Fire-and-forget: returns null on failure instead of throwing */
  safeGet: async <T>(path: string, fallback: T): Promise<T> => {
    try {
      return await request<T>('GET', path);
    } catch {
      return fallback;
    }
  },
};

export default api;
