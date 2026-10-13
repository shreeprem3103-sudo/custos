/**
 * The API client.
 *
 * Rules this file exists to enforce:
 *
 * 1. **One place knows the base path.** The console is mounted at `/console` by
 *    the API itself, so every call is relative. The same bundle therefore works
 *    under `next dev`, behind a path prefix, or on an air-gapped box, with no
 *    build-time configuration and no chance of a stale absolute URL.
 * 2. **A 401 is a state, not an exception.** The session is refreshed once and
 *    the request retried; if the refresh fails the console is signed out
 *    deliberately, with a reason, instead of rendering empty panels forever.
 * 3. **A failure is information.** Every error carries the API's own message,
 *    the correlation id, and the status code, because "something went wrong" is
 *    useless in a control room at 02:00.
 * 4. **Degraded mode is legible.** `X-CUSTOS-*` response headers are lifted onto
 *    the response object so the shell can show *why* the data is stale.
 */

import type { AuthToken } from "./types";

export const API_ROOT = "/api/v1";

export interface ApiMeta {
  correlationId: string | null;
  degraded: boolean;
  twinDigest: string | null;
  snapshotAgeS: number | null;
  requestId: string | null;
}

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly correlationId: string | null;
  readonly path: string;

  constructor(status: number, detail: string, path: string, correlationId: string | null) {
    super(`${status} ${path}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.correlationId = correlationId;
    this.path = path;
  }

  /** True when the failure is a permission problem, not an outage. */
  get isAuth(): boolean {
    return this.status === 401 || this.status === 403;
  }

  /** True when retrying in a second is reasonable. */
  get isTransient(): boolean {
    return this.status === 0 || this.status === 429 || this.status >= 500;
  }
}

export interface ApiResponse<T> {
  data: T;
  meta: ApiMeta;
}

type Listener = () => void;

/**
 * Session holder. The access token lives in memory and (optionally) in
 * sessionStorage — never localStorage: an unattended control-room browser must
 * not hold a week-long credential on disk.
 */
class Session {
  private token: string | null = null;
  private refreshToken: string | null = null;
  private refreshInFlight: Promise<boolean> | null = null;
  private listeners = new Set<Listener>();
  identity: { subject: string; role: string; permissions: string[] } | null = null;
  signedOutReason: string | null = null;

  static key = "custos.session";

  constructor() {
    if (typeof window === "undefined") return;
    // sessionStorage survives a refresh (a commander hitting F5 mid-incident)
    // but not a closed tab, which is the right trade for a shared console.
    try {
      const raw = window.sessionStorage.getItem(Session.key);
      if (raw) {
        const parsed = JSON.parse(raw) as { token: string; refresh: string | null; identity: Session["identity"] };
        this.token = parsed.token;
        this.refreshToken = parsed.refresh;
        this.identity = parsed.identity;
      }
    } catch {
      this.token = null;
    }
  }

  onChange(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private emit() {
    for (const listener of this.listeners) listener();
  }

  get accessToken(): string | null {
    return this.token;
  }

  get isAuthenticated(): boolean {
    return Boolean(this.token);
  }

  adopt(tokens: AuthToken) {
    this.token = tokens.access_token;
    this.refreshToken = tokens.refresh_token;
    this.identity = { subject: tokens.subject, role: tokens.role, permissions: tokens.permissions };
    this.signedOutReason = null;
    this.persist();
    this.emit();
  }

  signOut(reason: string | null = null) {
    this.token = null;
    this.refreshToken = null;
    this.identity = null;
    this.signedOutReason = reason;
    if (typeof window !== "undefined") window.sessionStorage.removeItem(Session.key);
    this.emit();
  }

  private persist() {
    if (typeof window === "undefined") return;
    if (!this.token) {
      window.sessionStorage.removeItem(Session.key);
      return;
    }
    window.sessionStorage.setItem(
      Session.key,
      JSON.stringify({ token: this.token, refresh: this.refreshToken, identity: this.identity }),
    );
  }

  refresh(): Promise<boolean> {
    if (!this.refreshToken) return Promise.resolve(false);
    if (this.refreshInFlight) return this.refreshInFlight;
    this.refreshInFlight = (async () => {
      try {
        const response = await fetch(`${API_ROOT}/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: this.refreshToken }),
        });
        if (!response.ok) {
          this.signOut("your session expired - sign in again to rejoin the operation");
          return false;
        }
        this.adopt((await response.json()) as AuthToken);
        return true;
      } catch {
        this.signOut("the command API could not be reached to renew your session");
        return false;
      } finally {
        this.refreshInFlight = null;
      }
    })();
    return this.refreshInFlight;
  }
}

export const session = new Session();

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  /** Skip the refresh-and-retry dance (used by the login call itself). */
  anonymous?: boolean;
  timeoutMs?: number;
}

function readMeta(response: Response): ApiMeta {
  return {
    correlationId: response.headers.get("X-CUSTOS-Correlation-Id") || response.headers.get("X-Request-Id"),
    degraded: response.headers.get("X-CUSTOS-Degraded") === "true",
    twinDigest: response.headers.get("X-CUSTOS-Twin-Digest"),
    snapshotAgeS: response.headers.get("X-CUSTOS-Snapshot-Age") ? Number(response.headers.get("X-CUSTOS-Snapshot-Age")) : null,
    requestId: response.headers.get("X-Request-Id"),
  };
}

async function parseError(response: Response, path: string): Promise<ApiError> {
  let detail = response.statusText || "request failed";
  let correlationId = response.headers.get("X-CUSTOS-Correlation-Id");
  try {
    const payload = (await response.json()) as { detail?: unknown; correlation_id?: string; path?: string };
    if (typeof payload.detail === "string") detail = payload.detail;
    else if (Array.isArray(payload.detail)) {
      // FastAPI validation errors: say which field, not just "422"
      detail = payload.detail
        .map((item) => {
          const entry = item as { loc?: (string | number)[]; msg?: string };
          return `${(entry.loc ?? []).slice(1).join(".") || "body"}: ${entry.msg ?? "invalid"}`;
        })
        .join("; ");
    }
    if (payload.correlation_id) correlationId = payload.correlation_id;
  } catch {
    /* a non-JSON error body is still an error */
  }
  return new ApiError(response.status, detail, path, correlationId ?? null);
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<ApiResponse<T>> {
  const { method = "GET", body, signal, anonymous = false, timeoutMs = 60_000 } = options;
  const url = `${API_ROOT}${path}`;

  const send = async (): Promise<Response> => {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (!anonymous && session.accessToken) headers.Authorization = `Bearer ${session.accessToken}`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const onAbort = () => controller.abort();
    signal?.addEventListener("abort", onAbort);
    try {
      return await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal });
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
    }
  };

  let response: Response;
  try {
    response = await send();
  } catch (error) {
    if ((error as Error).name === "AbortError" && signal?.aborted) {
      throw new ApiError(0, "request cancelled", url, null);
    }
    // A dead network in an EOC is a *known* failure mode, and it must read as one.
    throw new ApiError(0, "the command API is unreachable from this browser", url, null);
  }

  if (response.status === 401 && !anonymous) {
    const renewed = await session.refresh();
    if (renewed) {
      response = await send();
    } else {
      throw await parseError(response, url);
    }
  }

  if (!response.ok) throw await parseError(response, url);
  const meta = readMeta(response);
  if (response.status === 204) return { data: undefined as T, meta };
  const text = await response.text();
  const data = (text ? JSON.parse(text) : undefined) as T;
  return { data, meta };
}

/** Convenience wrapper when only the payload matters. */
export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return (await request<T>(path, options)).data;
}

export const auth = {
  async login(subject: string, password: string, role: string): Promise<AuthToken> {
    const tokens = await api<AuthToken>("/auth/login", { method: "POST", body: { subject, password, role }, anonymous: true });
    session.adopt(tokens);
    return tokens;
  },
  logout() {
    session.signOut(null);
  },
};

/** Build the WebSocket URL for the live feed, with the token as a query parameter. */
export function streamUrl(sinceSeq: number, topics?: string[]): string {
  const base = typeof window === "undefined" ? "ws://127.0.0.1:8000" : `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}`;
  const params = new URLSearchParams();
  if (session.accessToken) params.set("token", session.accessToken);
  if (sinceSeq > 0) params.set("since_seq", String(sinceSeq));
  if (topics?.length) params.set("topics", topics.join(","));
  return `${base}${API_ROOT}/stream?${params.toString()}`;
}
