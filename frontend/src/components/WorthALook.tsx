"use client";

import { useEffect, useState } from "react";
import { ChevronDown, ExternalLink, ImageOff, RefreshCw, Sparkles, Wand2 } from "lucide-react";
import { api, mediaUrl } from "@/lib/api";
import type { WardrobePick, WardrobeSuggestionsResponse } from "@/lib/types";
import { cn, money, titleCase } from "@/lib/format";
import { Card, LoadingImg, Skeleton } from "./ui";

const POLL_MS = 3000;
const GIVE_UP_MS = 4 * 60_000;
const LG_COLS: Record<number, string> = { 1: "lg:grid-cols-3", 2: "lg:grid-cols-3", 3: "lg:grid-cols-3", 4: "lg:grid-cols-4", 5: "lg:grid-cols-5" };

// Last ready answer, kept for this browser session so coming back to the page is instant (the server caches too).
let lastReady: WardrobeSuggestionsResponse | null = null;

/** "Worth a look": 3-5 real products picked to fill gaps in My Closet. Loads on its own; never blocks the page. */
export function WorthALook({ onPick, busyId }: { onPick: (p: WardrobePick) => void; busyId?: string | null }) {
  const [data, setData] = useState<WardrobeSuggestionsResponse | null>(lastReady);
  const [failed, setFailed] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const start = Date.now();
    const tick = async () => {
      try {
        const d = await api.wardrobeSuggestions();
        if (!alive) return;
        if (d.status === "pending") {
          if (Date.now() - start > GIVE_UP_MS) {
            setFailed("This is taking longer than usual.");
            return;
          }
          timer = setTimeout(tick, POLL_MS);
          return;
        }
        if (d.status === "ready") lastReady = d;
        setData(d);
        setFailed(d.status === "error" ? d.reason || "Couldn't look through stores right now." : null);
      } catch {
        if (!alive) return;
        if (Date.now() - start < GIVE_UP_MS) timer = setTimeout(tick, POLL_MS * 2);
        else setFailed("Couldn't look through stores right now.");
      }
    };
    tick();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [attempt]);

  const list = data?.status === "ready" ? data.suggestions : [];
  const loading = !failed && data?.status !== "ready";
  const retry = () => {
    setFailed(null);
    setData(null);
    setAttempt((a) => a + 1);
  };

  return (
    <section className="mt-6 lg:mt-8" aria-busy={loading} aria-live="polite" data-testid="worth-a-look">
      <div className="flex items-center gap-2">
        <Sparkles className="size-4 text-accent" />
        <h2 className="font-bold text-lg tracking-tight">{data?.title || "Worth a look"}</h2>
      </div>
      <p className="text-xs text-black/50 mb-2.5">{data?.subtitle || "Picked to fill gaps in My Closet"}</p>

      {loading && (
        <>
          <div className={cn("grid grid-cols-1 sm:grid-cols-2 gap-3", LG_COLS[4])}>
            {[0, 1, 2, 3].map((i) => (
              <Card key={i} className={cn("p-3", i === 3 && "hidden lg:block")}>
                <div className="flex lg:block gap-3">
                  <Skeleton className="size-24 lg:size-auto lg:aspect-square rounded-2xl shrink-0" />
                  <div className="flex-1 space-y-2 lg:mt-3 pt-1 lg:pt-0">
                    <Skeleton className="h-3.5 rounded-full w-4/5" />
                    <Skeleton className="h-3 rounded-full w-1/2" soft />
                    <Skeleton className="h-4 rounded-full w-1/4" />
                    <Skeleton className="h-3 rounded-full w-full" soft />
                  </div>
                </div>
              </Card>
            ))}
          </div>
          <p className="text-xs text-black/45 mt-2" data-testid="worth-a-look-loading">
            Looking through stores for things that go with your clothes…
          </p>
        </>
      )}

      {failed && (
        <Card className="p-4 text-sm text-black/60 flex items-start gap-3">
          <div className="flex-1">{failed}</div>
          <button onClick={retry} className="inline-flex items-center gap-1 text-xs font-semibold text-accent shrink-0">
            <RefreshCw className="size-3.5" /> Try again
          </button>
        </Card>
      )}

      {!loading && !failed && list.length === 0 && (
        <Card className="p-4 text-sm text-black/55">
          {data?.reason || "Nothing stood out in stores for your closet right now."}
        </Card>
      )}

      {list.length > 0 && (
        <>
          <div className={cn("grid grid-cols-1 sm:grid-cols-2 gap-3", LG_COLS[list.length] ?? "lg:grid-cols-3")}>
            {list.map((p) => (
              <PickCard key={p.id} p={p} onPick={() => onPick(p)} busy={busyId === p.id} disabled={!!busyId} />
            ))}
          </div>
          <p className="text-[11px] text-black/40 mt-2">
            Real products from online stores · prices can change · tap one to check it against your closet
          </p>
        </>
      )}
    </section>
  );
}

/** "BUTTON FRONT KNIT CARDIGAN" -> "Button Front Knit Cardigan" (some stores shout their titles). */
function displayName(name: string) {
  const n = name.split(/\s+\|\s+/)[0]; // "The Performance Chino | Uniform | Deep Navy | Slim" -> first part
  return /[a-z]/.test(n) || n.length <= 6 ? n : titleCase(n.toLowerCase());
}

function PickCard({ p, onPick, busy, disabled }: { p: WardrobePick; onPick: () => void; busy: boolean; disabled: boolean }) {
  const [src, setSrc] = useState(mediaUrl(p.photo_url || p.image_url));
  const [broken, setBroken] = useState(false);
  const [open, setOpen] = useState(false);
  const store = p.retailer || p.brand || "the store";
  const brandLine = [p.brand, p.retailer && p.retailer !== p.brand ? p.retailer : null].filter(Boolean).join(" · ");
  const go = () => {
    if (!disabled) onPick();
  };
  return (
    <div
      role="button"
      tabIndex={0}
      aria-label={`Should I buy ${p.name}?`}
      aria-disabled={disabled}
      onClick={go}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          go();
        }
      }}
      data-testid="worth-a-look-card"
      className={cn(
        "rounded-3xl bg-card border border-black/5 shadow-[0_1px_2px_rgba(60,40,20,0.05)] p-3 flex lg:flex-col gap-3 text-left transition outline-none focus-visible:ring-2 focus-visible:ring-accent/40",
        disabled ? (busy ? "ring-2 ring-accent/40" : "opacity-60") : "cursor-pointer hover:shadow-md active:scale-[0.99]",
      )}
    >
      <div className="relative size-24 lg:size-auto lg:aspect-square shrink-0 rounded-2xl bg-white overflow-hidden grid place-items-center">
        {src && !broken ? (
          <LoadingImg
            src={src}
            alt={p.name}
            className="size-full object-contain"
            onError={() => {
              const fallback = mediaUrl(p.image_url);
              if (fallback && fallback !== src) setSrc(fallback);
              else setBroken(true);
            }}
          />
        ) : (
          <ImageOff className="size-6 text-black/20" />
        )}
      </div>
      <div className="flex-1 min-w-0 flex flex-col">
        <div className="font-semibold leading-snug line-clamp-2">{displayName(p.name)}</div>
        {brandLine && <div className="text-xs text-black/50 mt-0.5 truncate">{brandLine}</div>}
        <div className="text-base font-bold tabular-nums mt-0.5">{money(p.price, p.currency || "USD", p.price != null && !Number.isInteger(p.price) ? 2 : 0)}</div>
        <p className="text-xs text-black/70 leading-snug mt-1">{p.reason}</p>
        <p className="text-xs text-black/45 leading-snug mt-0.5">{p.pairs_line}</p>
        {open && <p className="text-[11px] text-black/45 leading-snug mt-1">{p.details}</p>}
        <div className="mt-auto pt-2 flex items-center gap-3 flex-wrap">
          <span className="inline-flex items-center gap-1 text-xs font-semibold text-ink">
            <Wand2 className="size-3.5" /> {busy ? "Checking…" : "Check it"}
          </span>
          <a
            href={p.product_url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            className="inline-flex items-center gap-1 text-xs font-semibold text-accent hover:underline"
            data-testid="worth-a-look-store"
          >
            {store} <ExternalLink className="size-3" />
          </a>
          <button
            onClick={(e) => {
              e.stopPropagation();
              setOpen((o) => !o);
            }}
            className="ml-auto inline-flex items-center text-[11px] font-medium text-black/40"
            aria-expanded={open}
          >
            {open ? "Less" : "Details"}
            <ChevronDown className={cn("size-3 transition", open && "rotate-180")} />
          </button>
        </div>
      </div>
    </div>
  );
}
