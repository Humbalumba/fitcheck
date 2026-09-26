import type {
  Attributes,
  DetectResponse,
  EvaluateResponse,
  Health,
  Item,
  Settings,
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

async function request<T>(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), init?.timeoutMs ?? 180_000);
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        ...(init?.body && !(init.body instanceof FormData) ? { "Content-Type": "application/json" } : {}),
        ...(init?.headers || {}),
      },
      cache: "no-store",
    });
  } catch (e) {
    clearTimeout(timeout);
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

  getSettings: () => request<Settings>("/api/settings"),

  putSettings: (patch: Partial<Settings>) =>
    request<Settings>("/api/settings", { method: "PUT", body: JSON.stringify(patch) }),
};

export type Api = typeof realApi;

export const api: Api = MOCK ? (mockApi as Api) : realApi;
