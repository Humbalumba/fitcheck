"use client";

import { useEffect, useState } from "react";
import { api, MOCK } from "@/lib/api";
import type { Health } from "@/lib/types";
import { cn } from "@/lib/format";

export function StatusIndicator() {
  const [health, setHealth] = useState<Health | null>(null);
  const [down, setDown] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let alive = true;
    const check = async () => {
      try {
        const h = await api.health();
        if (!alive) return;
        setHealth(h);
        setDown(!h?.ok);
      } catch {
        if (!alive) return;
        setDown(true);
      }
    };
    check();
    const t = setInterval(check, 15000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const state = MOCK ? "mock" : down ? "down" : !health ? "loading" : health.gemini && health.scorer !== "stub" ? "ok" : "degraded";
  const label = { mock: "Mock data", down: "Offline", loading: "Connecting", ok: "Live", degraded: "Live (limited)" }[state];
  const dot = {
    mock: "bg-sky-500",
    down: "bg-rose-500",
    loading: "bg-zinc-400 animate-pulse",
    ok: "bg-emerald-500",
    degraded: "bg-amber-500",
  }[state];

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1.5 rounded-full border border-black/10 bg-white px-2.5 py-1 text-xs font-medium text-black/70"
        aria-label="Backend status"
      >
        <span className={cn("size-2 rounded-full", dot)} />
        {label}
      </button>
      {open && (
        <div className="absolute right-0 mt-2 w-56 rounded-2xl border border-black/10 bg-white p-3 text-xs shadow-xl z-50">
          <div className="font-semibold mb-2">Backend status</div>
          {MOCK ? (
            <p className="text-black/60">Running with built-in fixture data (NEXT_PUBLIC_MOCK=1).</p>
          ) : down ? (
            <p className="text-black/60">Can&apos;t reach the API. Start the backend on :8000.</p>
          ) : (
            <ul className="space-y-1.5 text-black/70">
              <li className="flex justify-between">
                <span>API</span>
                <span className="font-medium text-emerald-600">OK</span>
              </li>
              <li className="flex justify-between">
                <span>Gemini vision</span>
                <span className={cn("font-medium", health?.gemini ? "text-emerald-600" : "text-amber-600")}>
                  {health?.gemini ? "On" : "Off"}
                </span>
              </li>
              <li className="flex justify-between">
                <span>Outfit scorer</span>
                <span className={cn("font-medium", health?.scorer === "stub" ? "text-amber-600" : "text-emerald-600")}>
                  {health?.scorer === "outfit_transformer" ? "Transformer" : health?.scorer ?? "—"}
                </span>
              </li>
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
