"use client";

import { useEffect, useState } from "react";
import { ChevronDown, ExternalLink, ImageOff, RefreshCw, Sparkles, Wand2 } from "lucide-react";
import { api, mediaUrl } from "@/lib/api";
import type { WardrobePick, WardrobeSuggestionsResponse } from "@/lib/types";
import { cn, money, titleCase } from "@/lib/format";
import { Card, LoadingImg, Skeleton } from "./ui";

const POLL_MS = 3000;
const GIVE_UP_MS = 4 * 60_000;

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
    <section className="mt-8 lg:mt-10 max-w-2xl mx-auto" aria-busy={loading} aria-live="polite" data-testid="worth-a-look">
      <div className="text-center mb-3">
        <h2 className="font-bold text-base tracking-tight inline-flex items-center gap-1.5">
          <Sparkles className="size-4 text-accent" />
          {data?.title || "Worth a look"}
        </h2>
        <p className="text-xs text-black/50">{data?.subtitle || "Picked to fill gaps in My Closet"}</p>
      </div>

      {loading && (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
            {[0, 1, 2, 3].map((i) => (
              <Card key={i} className={cn("p-2.5 rounded-2xl flex gap-3", i === 3 && "hidden sm:flex")}>
                <Skeleton className="size-[72px] sm:size-20 rounded-xl shrink-0" />
                <div className="flex-1 space-y-2 pt-1">
                  <Skeleton className="h-3 rounded-full w-4/5" />
                  <Skeleton className="h-2.5 rounded-full w-1/2" soft />
                  <Skeleton className="h-2.5 rounded-full w-full" soft />
                </div>
              </Card>
            ))}
          </div>
          <p className="text-xs text-black/45 mt-2 text-center" data-testid="worth-a-look-loading">
            Looking through stores for things that go with your clothes…
          </p>
        </>
      )}

      {failed && (
        <Card className="p-3 rounded-2xl text-sm text-black/60 flex items-start gap-3">
          <div className="flex-1">{failed}</div>
          <button onClick={retry} className="inline-flex items-center gap-1 text-xs font-semibold text-accent shrink-0">
            <RefreshCw className="size-3.5" /> Try again
          </button>
        </Card>
      )}

      {!loading && !failed && list.length === 0 && (
        <Card className="p-3 rounded-2xl text-sm text-black/55 text-center">
          {data?.reason || "Nothing stood out in stores for your closet right now."}
        </Card>
      )}

      {list.length > 0 && (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
            {list.map((p) => (
              <PickCard key={p.id} p={p} onPick={() => onPick(p)} busy={busyId === p.id} disabled={!!busyId} />
            ))}
          </div>
          <p className="text-[11px] text-black/40 mt-2 text-center">
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
        "rounded-2xl bg-card border border-black/5 shadow-[0_1px_2px_rgba(60,40,20,0.05)] p-2.5 flex gap-3 text-left transition outline-none focus-visible:ring-2 focus-visible:ring-accent/40",
        disabled ? (busy ? "ring-2 ring-accent/40" : "opacity-60") : "cursor-pointer hover:shadow-md active:scale-[0.99]",
      )}
    >
      <div className="relative size-[72px] sm:size-20 shrink-0 rounded-xl bg-white overflow-hidden grid place-items-center">
        {src && !broken ? (
          <LoadingImg
            src={src}
            alt={p.name}
            className="size-full object-contain p-1"
            onError={() => {
              const fallback = mediaUrl(p.image_url);
              if (fallback && fallback !== src) setSrc(fallback);
              else setBroken(true);
            }}
          />
        ) : (
          <ImageOff className="size-5 text-black/20" />
        )}
      </div>
      <div className="flex-1 min-w-0 flex flex-col">
        <div className="text-[13px] font-semibold leading-snug line-clamp-1">{displayName(p.name)}</div>
        <div className="text-xs text-black/50 truncate">
          <span className="font-semibold text-black/75 tabular-nums">
            {money(p.price, p.currency || "USD", p.price != null && !Number.isInteger(p.price) ? 2 : 0)}
          </span>
          {brandLine ? ` · ${brandLine}` : ""}
        </div>
        <p className="text-xs text-black/65 leading-snug mt-0.5 line-clamp-2">{p.reason}</p>
        {open && (
          <p className="text-[11px] text-black/45 leading-snug mt-0.5">
            {p.pairs_line} · {p.details}
          </p>
        )}
        <div className="mt-auto pt-1 flex items-center gap-2.5 text-[11px]">
          <span className="inline-flex items-center gap-0.5 font-semibold text-ink shrink-0">
            <Wand2 className="size-3" /> {busy ? "Checking…" : "Check it"}
          </span>
          <a
            href={p.product_url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            className="inline-flex items-center gap-0.5 font-semibold text-accent hover:underline min-w-0"
            data-testid="worth-a-look-store"
            title={`View at ${store}`}
          >
            <span className="truncate">{store}</span> <ExternalLink className="size-3 shrink-0" />
          </a>
          <button
            onClick={(e) => {
              e.stopPropagation();
              setOpen((o) => !o);
            }}
            className="ml-auto inline-flex items-center font-medium text-black/40 shrink-0"
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
