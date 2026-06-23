import type { ApiError, AuthResponse, TokenPair } from "./types";

const ACCESS_KEY = "crucible.access";
const REFRESH_KEY = "crucible.refresh";

export class RequestError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly field: string | null = null,
    readonly requestId: string | null = null,
  ) {
    super(message);
    this.name = "RequestError";
  }
}

export const tokens = {
  access: () => localStorage.getItem(ACCESS_KEY),
  refresh: () => localStorage.getItem(REFRESH_KEY),
  set(pair: TokenPair) {
    localStorage.setItem(ACCESS_KEY, pair.access_token);
    localStorage.setItem(REFRESH_KEY, pair.refresh_token);
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
  },
};

type Listener = () => void;
const signedOutListeners = new Set<Listener>();

/** Notified when the session is gone for good, so the UI can redirect once. */
export function onSignedOut(listener: Listener): () => void {
  signedOutListeners.add(listener);
  return () => signedOutListeners.delete(listener);
}

function announceSignedOut() {
  tokens.clear();
  signedOutListeners.forEach((fn) => fn());
}

// A single in-flight refresh, shared by every request that hits a 401.
//
// Without this, six components mounting at once each fire their own refresh.
// Refresh tokens are single-use and rotate, so the first succeeds and the rest
// present a token that was just revoked -- which the backend correctly treats
// as replay and responds to by revoking the whole family. The user is logged
// out by their own app opening a page.
let refreshInFlight: Promise<boolean> | null = null;

async function refreshTokens(): Promise<boolean> {
  const refresh = tokens.refresh();
  if (!refresh) return false;

  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        const response = await fetch("/api/v1/auth/refresh", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refresh }),
        });
        if (!response.ok) return false;
        tokens.set((await response.json()) as TokenPair);
        return true;
      } catch {
        return false;
      } finally {
        // Cleared in a microtask so concurrent callers all observe the same
        // promise before it is discarded.
        queueMicrotask(() => {
          refreshInFlight = null;
        });
      }
    })();
  }
  return refreshInFlight;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  /** Send an Idempotency-Key so a retried POST cannot create a duplicate. */
  idempotencyKey?: string;
  signal?: AbortSignal;
  /** Skip auth entirely (sign-in, sign-up). */
  anonymous?: boolean;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const send = async (): Promise<Response> => {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (!options.anonymous) {
      const access = tokens.access();
      if (access) headers.Authorization = `Bearer ${access}`;
    }
    if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;

    return fetch(`/api/v1${path}`, {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    });
  };

  let response = await send();

  // One retry after a refresh. Access tokens last 15 minutes, so an expiry
  // mid-session is normal rather than exceptional and should be invisible.
  if (response.status === 401 && !options.anonymous && tokens.refresh()) {
    if (await refreshTokens()) {
      response = await send();
    } else {
      announceSignedOut();
    }
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};

  if (!response.ok) {
    const err = payload as ApiError;
    if (response.status === 401) announceSignedOut();
    throw new RequestError(
      response.status,
      err.error?.code ?? `http_${response.status}`,
      err.error?.message ?? "Something went wrong",
      err.error?.field ?? null,
      err.request_id ?? null,
    );
  }
  return payload as T;
}

export const api = {
  get: <T>(path: string, signal?: AbortSignal) => request<T>(path, { signal }),
  post: <T>(path: string, body?: unknown, opts: Omit<RequestOptions, "method" | "body"> = {}) =>
    request<T>(path, { ...opts, method: "POST", body }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: "PUT", body }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: "PATCH", body }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),

  signIn: (email: string, password: string) =>
    request<AuthResponse>("/auth/signin", {
      method: "POST",
      body: { email, password },
      anonymous: true,
    }),

  signUp: (email: string, display_name: string, password: string) =>
    request<AuthResponse>("/auth/signup", {
      method: "POST",
      body: { email, display_name, password },
      anonymous: true,
    }),

  signOut: async () => {
    const refresh = tokens.refresh();
    if (refresh) {
      // Best effort. A failure here still ends the local session.
      await request("/auth/signout", {
        method: "POST",
        body: { refresh_token: refresh },
      }).catch(() => undefined);
    }
    tokens.clear();
  },
};
