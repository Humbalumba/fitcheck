import type {
  Attributes,
  DetectResponse,
  EvaluateResponse,
  Health,
  Item,
  Settings,
  SuggestionsResponse,
  Sustainability,
  WardrobeSuggestionsResponse,
} from "./types";
import { mockApi } from "./mock";

/**
 * API base. Empty string (default) = same-origin relative URLs, which Next
 * rewrites to BACKEND_URL (see next.config.ts). Set NEXT_PUBLIC_API_BASE to
 * e.g. http://localhost:8000 to call the backend directly (needs CORS).
 */
export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE ?? "").replace(/\/$/, "");
export const MOCK = process.env.NEXT_PUBLIC_MOCK === "1" || process.env.NEXT_PUBLIC_MOCK === "true";

/** Turn an API media path ("/media/cutouts/abc.png") into a loadable URL. */
export function mediaUrl(path?: string | null): string {
  if (!path) return "";
  if (/^(https?:|data:|blob:)/.test(path)) return path;
  return `${API_BASE}${path.startsWith("/") ? "" : "/"}${path}`;
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

/* ---- backend auth: a 5-minute Bearer token minted from the httpOnly Auth.js session (app/api/auth/token) ---- */
let apiToken: { token: string; exp: number } | null = null;
let tokenInflight: Promise<string> | null = null;

function goToLogin() {
  if (typeof window === "undefined") return;
  if (window.location.pathname.startsWith("/login")) return;
  const next = window.location.pathname + window.location.search;
  // full navigation on purpose: drops all in-memory closet state of the signed-out session
  // eslint-disable-next-line @next/next/no-location-assign-relative-destination
  window.location.assign(`/login?next=${encodeURIComponent(next)}`);
}

export function clearApiToken() {
  apiToken = null;
}

async function getApiToken(force = false): Promise<string> {
  const now = Date.now() / 1000;
  if (!force && apiToken && apiToken.exp - now > 30) return apiToken.token;
  if (!tokenInflight) {
    tokenInflight = (async () => {
      const r = await fetch("/api/auth/token", { cache: "no-store", credentials: "same-origin" });
      if (r.status === 401) {
        apiToken = null;
        goToLogin();
        throw new ApiError("Please sign in again.", 401);
      }
      if (!r.ok) throw new ApiError("Can't reach the FitCheck server.", r.status);
      const j = (await r.json()) as { token: string; expires_at: number };
      apiToken = { token: j.token, exp: j.expires_at };
      return j.token;
    })().finally(() => {
      tokenInflight = null;
    });
  }
  return tokenInflight;
}

async function request<T>(path: string, init?: RequestInit & { timeoutMs?: number; noAuth?: boolean }): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), init?.timeoutMs ?? 180_000);
  let res: Response;
  try {
    const send = async (force: boolean) =>
      fetch(`${API_BASE}${path}`, {
        ...init,
        signal: controller.signal,
        headers: {
          ...(init?.body && !(init.body instanceof FormData) ? { "Content-Type": "application/json" } : {}),
          ...(init?.noAuth ? {} : { Authorization: `Bearer ${await getApiToken(force)}` }),
          ...(init?.headers || {}),
        },
        cache: "no-store",
      });
    res = await send(false);
    if (res.status === 401 && !init?.noAuth) {
      res = await send(true); // token expired / revoked: get a fresh one once
      if (res.status === 401) goToLogin();
    }
  } catch (e) {
    clearTimeout(timeout);
    if (e instanceof ApiError) throw e;
    if ((e as Error).name === "AbortError") throw new ApiError("Request timed out", 0);
    throw new ApiError("Can't reach the FitCheck server. Is the backend running?", 0);
  }
  clearTimeout(timeout);
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      const d = body?.detail ?? body?.error ?? body?.message;
      if (typeof d === "string") msg = d;
      else if (Array.isArray(d)) msg = d.map((x) => x?.msg ?? JSON.stringify(x)).join("; ");
      else if (d) msg = JSON.stringify(d);
    } catch {
      /* ignore */
    }
    throw new ApiError(msg, res.status);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** Unwrap {items:[...]} or bare arrays. */
function itemsOf(r: unknown): Item[] {
  if (Array.isArray(r)) return r as Item[];
  const o = r as { items?: Item[] };
  return o?.items ?? [];
}

/** Some backends wrap single items: {item: {...}} */
function itemOf(r: unknown): Item {
  const o = r as { item?: Item };
  if (o && typeof o === "object" && o.item && !("id" in (o as object))) return o.item;
  return r as Item;
}

const realApi = {
  health: () => request<Health>("/api/health", { timeoutMs: 8000 }),

  detect: (file: Blob, purpose: "closet" | "candidate", filename = "photo.jpg") => {
    const fd = new FormData();
    fd.append("file", file, filename);
    fd.append("purpose", purpose);
    return request<DetectResponse>("/api/detect", { method: "POST", body: fd });
  },

  /** Same as detect, but from a pasted product link (the backend fetches the page and its product photo). */
  detectUrl: (url: string, purpose: "closet" | "candidate" = "candidate") =>
    request<DetectResponse>("/api/detect-url", { method: "POST", body: JSON.stringify({ url, purpose }) }),

  addToCloset: async (itemIds: string[], overrides?: Record<string, Partial<Attributes>>) =>
    itemsOf(
      await request<unknown>("/api/closet/items", {
        method: "POST",
        body: JSON.stringify({ item_ids: itemIds, attributes_overrides: overrides ?? {} }),
      }),
    ),

  listCloset: async (category?: string) =>
    itemsOf(
      await request<unknown>(
        `/api/closet/items${category ? `?category=${encodeURIComponent(category)}` : ""}`,
      ),
    ),

  updateItem: async (id: string, attributes: Partial<Attributes>) =>
    itemOf(
      await request<unknown>(`/api/closet/items/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: JSON.stringify({ attributes }),
      }),
    ),

  getItem: async (id: string) => itemOf(await request<unknown>(`/api/items/${encodeURIComponent(id)}`)),

  /** (Re-)create an item's clean product image in the background; poll getItem until render_status != "pending". */
  renderItem: async (id: string, mode: "auto" | "gemini" | "template" | "cleanup" = "auto") =>
    itemOf(
      await request<unknown>(`/api/items/${encodeURIComponent(id)}/render`, {
        method: "POST",
        body: JSON.stringify({ mode }),
      }),
    ),

  deleteItem: (id: string) =>
    request<{ ok: boolean }>(`/api/closet/items/${encodeURIComponent(id)}`, { method: "DELETE" }),

  evaluate: (itemId: string, price?: number | null) =>
    request<EvaluateResponse>("/api/evaluate", {
      method: "POST",
      body: JSON.stringify(
        price === null || price === undefined ? { item_id: itemId } : { item_id: itemId, price },
      ),
    }),

  candidateToCloset: async (itemId: string) =>
    itemOf(
      await request<unknown>(`/api/candidate/${encodeURIComponent(itemId)}/add-to-closet`, {
        method: "POST",
      }),
    ),

  /** Live-shopping suggestions for an evaluation (cached server-side; first call can take ~30 s). */
  suggestions: (evaluationId: string, refresh = false) =>
    request<SuggestionsResponse>(
      `/api/evaluations/${encodeURIComponent(evaluationId)}/suggestions${refresh ? "?refresh=true" : ""}`,
      { method: "POST", timeoutMs: 150_000 },
    ),

  /** "Worth a look" picks for the current closet. Never blocks: status "pending" while the live search runs. */
  wardrobeSuggestions: (refresh = false) =>
    request<WardrobeSuggestionsResponse>(`/api/suggestions/wardrobe${refresh ? "?refresh=true" : ""}`, {
      timeoutMs: 20_000,
    }),

  /** Sustainability from an item's latest saved evaluation (404 if it was never evaluated). */
  itemSustainability: (itemId: string) =>
    request<Sustainability & { evaluated_at?: string }>(`/api/items/${encodeURIComponent(itemId)}/sustainability`, {
      timeoutMs: 10_000,
    }),

  getSettings: () => request<Settings>("/api/settings"),

  putSettings: (patch: Partial<Settings>) =>
    request<Settings>("/api/settings", { method: "PUT", body: JSON.stringify(patch) }),

  /** The signed-in user. */
  me: () =>
    request<{ id: string; email: string; name: string | null; first_name: string | null; email_verified: boolean;
      has_password: boolean; providers: string[] }>("/api/me", { timeoutMs: 10_000 }),

  /** Permanently delete the account, closet, evaluations and photos. */
  deleteAccount: (password: string) =>
    request<{ ok: boolean }>("/api/account", { method: "DELETE", body: JSON.stringify({ confirm: "DELETE", password }) }),
};

/** Signed-out account flows (sign up, confirm email, reset password). JSON only (CSRF-safe), rate limited server-side. */
export const accountApi = {
  signup: (email: string, password: string, name: string) =>
    request<{ ok: boolean; message: string; verification_required: boolean }>("/api/account/signup", {
      method: "POST", noAuth: true, body: JSON.stringify({ email, password, name }) }),
  verifyEmail: (token: string) =>
    request<{ ok: boolean }>("/api/account/verify-email", { method: "POST", noAuth: true, body: JSON.stringify({ token }) }),
  resendVerification: (email: string) =>
    request<{ ok: boolean; message: string }>("/api/account/resend-verification", {
      method: "POST", noAuth: true, body: JSON.stringify({ email }) }),
  forgotPassword: (email: string) =>
    request<{ ok: boolean; message: string }>("/api/account/forgot-password", {
      method: "POST", noAuth: true, body: JSON.stringify({ email }) }),
  resetPassword: (token: string, password: string) =>
    request<{ ok: boolean }>("/api/account/reset-password", {
      method: "POST", noAuth: true, body: JSON.stringify({ token, password }) }),
};

export type Api = typeof realApi;

export const api: Api = MOCK ? (mockApi as Api) : realApi;
