"use client";

import { useCallback, useEffect, useState } from "react";
import { ExternalLink, ImageOff, RefreshCw, Shirt, ShoppingBag } from "lucide-react";
import { ApiError, api, mediaUrl } from "@/lib/api";
import type { Suggestion, SuggestionsResponse } from "@/lib/types";
import { cn, money, titleCase } from "@/lib/format";
import { Card } from "./ui";
import { OutfitModal } from "./OutfitModal";
import { LeafChip } from "./Sustainability";

const TITLES: Record<string, { title: string; sub: string }> = {
  SKIP: {
    title: "Better picks instead",
    sub: "Same type, different colors & textures — scored against your closet",
  },
  BUY: {
    title: "Pairs well with this",
    sub: "Real products that go with it and your closet — each a BUY too",
  },
};

// One in-flight request per evaluation (survives StrictMode double effects and remounts).
const inflight = new Map<string, Promise<SuggestionsResponse>>();
function load(evaluationId: string, refresh = false) {
  if (refresh) inflight.delete(evaluationId);
  let p = inflight.get(evaluationId);
  if (!p) {
    p = api.suggestions(evaluationId, refresh);
    inflight.set(evaluationId, p);
    p.catch(() => inflight.delete(evaluationId)); // allow retry after errors
  }
  return p;
}

export function SuggestionsSection({ evaluationId, decision }: { evaluationId?: string; decision: string }) {
  const [data, setData] = useState<SuggestionsResponse | null>(null);
  const [error, setError] = useState<{ msg: string; status: number } | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [open, setOpen] = useState<{ s: Suggestion; i: number } | null>(null);
  const closeModal = useCallback(() => setOpen(null), []);

  useEffect(() => {
    if (!evaluationId) return;
    let alive = true;
    setData(null);
    setError(null);
    const start = Date.now();
    const t = setInterval(() => setElapsed(Math.round((Date.now() - start) / 1000)), 500);
    load(evaluationId)
      .then((d) => alive && setData(d))
      .catch((e: unknown) => {
        if (!alive) return;
        const status = e instanceof ApiError ? e.status : 0;
        setError({ msg: (e as Error).message, status });
      })
      .finally(() => clearInterval(t));
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [evaluationId, attempt]);

  const cfg = TITLES[decision];
  if (!cfg || !evaluationId) return null;
  const loading = !data && !error;
  const list = data?.suggestions ?? [];

  return (
    <section aria-busy={loading} aria-live="polite">
      <div className="flex items-center gap-2 mt-2">
        {decision === "BUY" ? <Shirt className="size-4 text-accent" /> : <ShoppingBag className="size-4 text-accent" />}
        <h2 className="font-bold text-lg tracking-tight">{data?.title || cfg.title}</h2>
      </div>
      <p className="text-xs text-black/50 mb-2.5">{cfg.sub}</p>

      {loading && (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            {[0, 1, 2].map((i) => (
              <Card key={i} className="p-3 animate-pulse">
                <div className="flex sm:block gap-3">
                  <div className="size-28 sm:size-auto sm:aspect-square rounded-2xl bg-black/5 shrink-0" />
                  <div className="flex-1 space-y-2 sm:mt-3">
                    <div className="h-3.5 rounded bg-black/10 w-4/5" />
                    <div className="h-3 rounded bg-black/5 w-1/2" />
                    <div className="h-3 rounded bg-black/5 w-full" />
                    <div className="h-3 rounded bg-black/5 w-2/3" />
                  </div>
                </div>
              </Card>
            ))}
          </div>
          <p className="text-xs text-black/45 mt-2">
            Searching live stores and scoring each find with your closet…{" "}
            <span className="tabular-nums">{elapsed}s</span>
          </p>
        </>
      )}

      {error && (
        <Card className="p-4 text-sm text-black/60 flex items-start gap-3">
          <div className="flex-1">
            {error.status === 503
              ? "Shopping picks are taking a break — the free Gemini quota is used up for now (it resets around 3 AM ET)."
              : "Couldn't load shopping picks right now."}
          </div>
          <button
            onClick={() => {
              inflight.delete(evaluationId);
              setAttempt((a) => a + 1);
            }}
            className="inline-flex items-center gap-1 text-xs font-semibold text-accent shrink-0"
          >
            <RefreshCw className="size-3.5" /> Retry
          </button>
        </Card>
      )}

      {data && list.length === 0 && (
        <Card className="p-4 text-sm text-black/55">
          {data.message || "We didn't find anything that beats this for your wardrobe right now."}
        </Card>
      )}

      {list.length > 0 && (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            {list.map((s) => (
              <SuggestionCard key={s.id} s={s} onOpen={s.outfits?.length ? () => setOpen({ s, i: 0 }) : undefined} />
            ))}
          </div>
          <p className="text-[11px] text-black/40 mt-2">
            Live products
            {data?.source === "google_search" ? " via Google Search" : ""} · prices and stock can change · tap a card to
            see its outfits
          </p>
        </>
      )}

      <OutfitModal
        outfits={open?.s.outfits ?? []}
        index={open ? open.i : null}
        candidateId={open?.s.id}
        onIndex={(i) => setOpen((o) => (o ? { ...o, i } : o))}
        onClose={closeModal}
      />
    </section>
  );
}

/** "BUTTON FRONT KNIT CARDIGAN" -> "Button Front Knit Cardigan" (some stores shout their titles). */
function displayName(name: string) {
  return /[a-z]/.test(name) ? name : titleCase(name.toLowerCase());
}

function SuggestionCard({ s, onOpen }: { s: Suggestion; onOpen?: () => void }) {
  const [src, setSrc] = useState(mediaUrl(s.photo_url || s.image_url));
  const [broken, setBroken] = useState(false);
  const buy = String(s.verdict?.decision).toUpperCase() === "BUY";
  const retailer = s.retailer || s.brand || "the store";
  return (
    <div
      role={onOpen ? "button" : undefined}
      tabIndex={onOpen ? 0 : undefined}
      aria-label={onOpen ? `See outfits with ${s.name}` : undefined}
      onClick={onOpen}
      onKeyDown={
        onOpen
          ? (e) => {
              if (e.target !== e.currentTarget) return; // let the product link handle its own keys
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onOpen();
              }
            }
          : undefined
      }
      className={cn(
        "rounded-3xl bg-white border border-black/5 shadow-[0_1px_2px_rgba(0,0,0,0.03)] p-3 flex sm:flex-col gap-3 text-left transition outline-none focus-visible:ring-2 focus-visible:ring-accent/40",
        onOpen && "cursor-pointer hover:shadow-md active:scale-[0.99]",
      )}
    >
      <div className="relative size-28 sm:size-auto sm:aspect-square shrink-0 rounded-2xl bg-paper overflow-hidden grid place-items-center">
        {src && !broken ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={src}
            alt={s.name}
            loading="lazy"
            className="size-full object-contain"
            onError={() => {
              const fallback = mediaUrl(s.image_url);
              if (fallback && fallback !== src) setSrc(fallback);
              else setBroken(true);
            }}
          />
        ) : (
          <ImageOff className="size-6 text-black/20" />
        )}
        <span
          className={cn(
            "absolute top-2 left-2 rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-white",
            buy ? "bg-emerald-500" : "bg-rose-500",
          )}
        >
          {buy ? "Buy" : "Skip"}
        </span>
      </div>
      <div className="flex-1 min-w-0 flex flex-col">
        <div className="font-semibold leading-snug line-clamp-2">{displayName(s.name)}</div>
        <div className="text-xs text-black/50 mt-0.5 truncate">
          {[s.brand, s.retailer && s.retailer !== s.brand ? s.retailer : null].filter(Boolean).join(" · ")}
        </div>
        <div className="flex items-center gap-2 mt-1">
          <span className="text-lg font-bold tabular-nums">{money(s.price, s.currency || "USD")}</span>
          <LeafChip s={s.sustainability} />
        </div>
        <p className="text-xs text-black/65 leading-snug mt-1">{s.reason}</p>
        <div className="mt-auto pt-2">
          <a
            href={s.product_url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            className="inline-flex items-center gap-1 text-sm font-semibold text-accent hover:underline"
          >
            View at {retailer} <ExternalLink className="size-3.5" />
          </a>
        </div>
      </div>
    </div>
  );
}
