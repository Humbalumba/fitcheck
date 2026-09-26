"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, ImageOff, Sparkles, X } from "lucide-react";
import type { Item, Outfit } from "@/lib/types";
import { categoryKey, categoryLabel, templateLabel } from "@/lib/constants";
import { mediaUrl } from "@/lib/api";
import { cn, money, pct, titleCase } from "@/lib/format";

const CATEGORY_ORDER = ["outerwear", "top", "dress", "bottom", "shoes", "accessory", "other"];

/** Order pieces the way the template reads ("top+bottom+outerwear+shoes"), falling back to head-to-toe. */
function orderItems(outfit: Outfit): Item[] {
  const slots = String(outfit.template ?? "").split("+").map((s) => categoryKey(s));
  const rank = (it: Item) => {
    const k = categoryKey(it.category ?? it.attributes?.category);
    const i = slots.indexOf(k);
    return i >= 0 ? i : slots.length + CATEGORY_ORDER.indexOf(k);
  };
  return [...outfit.items].sort((a, b) => rank(a) - rank(b));
}

function itemTitle(it: Item): string {
  const a = it.attributes ?? {};
  const t = [a.primary_color, a.subcategory].filter(Boolean).join(" ");
  return titleCase(t || String(it.label || a.description || "") || categoryLabel(it.category ?? a.category));
}

export function OutfitModal({
  outfits,
  index,
  candidateId,
  onIndex,
  onClose,
}: {
  outfits: Outfit[];
  index: number | null;
  candidateId?: string;
  onIndex: (i: number) => void;
  onClose: () => void;
}) {
  const open = index !== null && index >= 0 && index < outfits.length;
  const total = outfits.length;
  const closeRef = useRef<HTMLButtonElement>(null);
  const touch = useRef<{ x: number; y: number } | null>(null);

  const go = useCallback(
    (d: number) => {
      if (index === null || total < 2) return;
      onIndex((index + d + total) % total);
    },
    [index, total, onIndex],
  );

  // Keyboard (Esc / arrows) + body scroll lock + focus management while open.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        go(-1);
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        go(1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, go, onClose]);

  useEffect(() => {
    if (!open) return;
    const body = document.body;
    const html = document.documentElement;
    const prev = { body: body.style.overflow, html: html.style.overflow, pr: body.style.paddingRight };
    const scrollbar = window.innerWidth - html.clientWidth;
    body.style.overflow = "hidden";
    html.style.overflow = "hidden";
    if (scrollbar > 0) body.style.paddingRight = `${scrollbar}px`; // avoid layout shift on desktop
    const lastFocus = document.activeElement as HTMLElement | null;
    closeRef.current?.focus({ preventScroll: true });
    return () => {
      body.style.overflow = prev.body;
      html.style.overflow = prev.html;
      body.style.paddingRight = prev.pr;
      lastFocus?.focus?.({ preventScroll: true });
    };
  }, [open]);

  if (!open) return null;
  const outfit = outfits[index!];
  const items = orderItems(outfit);
  const n = items.length;
  const score = outfit.score;
  const tone = score >= 0.75 ? "bg-emerald-500" : score >= 0.55 ? "bg-lime-500" : "bg-amber-500";
  const toneText = score >= 0.75 ? "Great match" : score >= 0.55 ? "Good match" : "Works";

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center p-3 sm:p-6"
      role="dialog"
      aria-modal="true"
      aria-label={`Outfit ${index! + 1} of ${total}`}
      data-testid="outfit-modal"
    >
      {/* Backdrop — click to close */}
      <div className="fadein absolute inset-0 bg-black/70 backdrop-blur-[2px]" onClick={onClose} aria-hidden="true" />

      {/* Desktop side arrows (on the backdrop) */}
      {total > 1 && (
        <>
          <button
            onClick={() => go(-1)}
            className="hidden xl:grid absolute left-4 lg:left-8 top-1/2 -translate-y-1/2 z-10 place-items-center size-12 rounded-full bg-white/15 text-white hover:bg-white/30 transition backdrop-blur"
            aria-label="Previous outfit"
          >
            <ChevronLeft className="size-7" />
          </button>
          <button
            onClick={() => go(1)}
            className="hidden xl:grid absolute right-4 lg:right-8 top-1/2 -translate-y-1/2 z-10 place-items-center size-12 rounded-full bg-white/15 text-white hover:bg-white/30 transition backdrop-blur"
            aria-label="Next outfit"
          >
            <ChevronRight className="size-7" />
          </button>
        </>
      )}

      {/* Panel */}
      <div
        className="zoomin relative flex w-full max-w-5xl max-h-full flex-col overflow-hidden rounded-[28px] bg-white shadow-2xl"
        onTouchStart={(e) => {
          const t = e.touches[0];
          touch.current = { x: t.clientX, y: t.clientY };
        }}
        onTouchEnd={(e) => {
          const s = touch.current;
          touch.current = null;
          if (!s) return;
          const t = e.changedTouches[0];
          const dx = t.clientX - s.x;
          const dy = t.clientY - s.y;
          if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) go(dx < 0 ? 1 : -1);
        }}
      >
        {/* Header */}
        <div className="flex items-start gap-3 px-4 sm:px-6 pt-4 sm:pt-5 pb-3">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-black/45">
              <Sparkles className="size-3.5 text-accent" />
              <span className="tabular-nums">
                Outfit {index! + 1} of {total}
              </span>
            </div>
            <div className="mt-0.5 font-bold text-[17px] sm:text-xl tracking-tight leading-tight line-clamp-2">
              {templateLabel(outfit.template)}
            </div>
          </div>
          {Number.isFinite(score) && (
            <div className="shrink-0 rounded-2xl bg-paper px-3 py-1.5 text-right">
              <div className="inline-flex items-center gap-1.5 text-lg sm:text-xl font-black tabular-nums leading-none">
                <span className={cn("size-2.5 rounded-full", tone)} />
                {pct(score)}
              </div>
              <div className="text-[10px] font-semibold uppercase tracking-wide text-black/45 mt-0.5">
                <span className="hidden sm:inline">{toneText} · </span>Compatibility
              </div>
            </div>
          )}
          <button
            ref={closeRef}
            onClick={onClose}
            className="shrink-0 grid place-items-center size-10 rounded-full bg-black/5 hover:bg-black/10 transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
            aria-label="Close"
          >
            <X className="size-5" />
          </button>
        </div>

        {/* Pieces */}
        <div className="flex-1 min-h-0 overflow-y-auto px-4 sm:px-6 pt-1.5 pb-4">
          <div
            key={index}
            className={cn(
              "fadein grid gap-3 sm:gap-4",
              n <= 1 ? "grid-cols-1 max-w-sm mx-auto" : "grid-cols-2",
              n === 3 && "sm:grid-cols-3",
              n >= 4 && "sm:grid-cols-4",
            )}
          >
            {items.map((it) => (
              <BigPiece key={it.id} item={it} isNew={!!candidateId && it.id === candidateId} />
            ))}
          </div>
        </div>

        {/* Footer nav (always visible; primary nav on phones) */}
        {total > 1 && (
          <div className="flex items-center justify-between gap-2 border-t border-black/5 px-3 sm:px-5 py-2.5 pb-[max(0.625rem,env(safe-area-inset-bottom))]">
            <button
              onClick={() => go(-1)}
              className="inline-flex items-center gap-1 h-10 pl-2 pr-4 rounded-full text-sm font-semibold hover:bg-black/5 transition"
              aria-label="Previous outfit"
            >
              <ChevronLeft className="size-5" /> Prev
            </button>
            <div className="text-xs font-medium text-black/45 text-center">
              <span className="tabular-nums font-semibold text-black/70">
                {index! + 1} / {total}
              </span>
              <span className="hidden sm:inline"> · use ← → keys</span>
              <span className="sm:hidden"> · swipe</span>
            </div>
            <button
              onClick={() => go(1)}
              className="inline-flex items-center gap-1 h-10 pl-4 pr-2 rounded-full text-sm font-semibold hover:bg-black/5 transition"
              aria-label="Next outfit"
            >
              Next <ChevronRight className="size-5" />
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function BigPiece({ item, isNew }: { item: Item; isNew: boolean }) {
  const [err, setErr] = useState(false);
  const url = mediaUrl(item.image_url || item.cutout_url);
  const a = item.attributes ?? {};
  const brand = a.brand ? String(a.brand) : "";
  const price = a.price != null && isNew ? money(Number(a.price), a.currency || "USD") : "";
  return (
    <figure className="min-w-0">
      <div
        className={cn(
          "relative aspect-square sm:aspect-[3/4] rounded-2xl overflow-hidden bg-[#f7f6f3]",
          isNew ? "ring-[3px] ring-accent ring-offset-2 ring-offset-white" : "ring-1 ring-black/5",
        )}
      >
        {url && !err ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={url}
            alt={itemTitle(item)}
            onError={() => setErr(true)}
            className="absolute inset-0 size-full object-contain p-[7%] mix-blend-multiply"
          />
        ) : (
          <div className="absolute inset-0 grid place-items-center">
            <ImageOff className="size-8 text-black/20" />
          </div>
        )}
        {isNew && (
          <span className="absolute top-2 left-2 rounded-full bg-accent text-white text-[11px] font-bold tracking-wide px-2.5 py-1 shadow-sm">
            NEW
          </span>
        )}
      </div>
      <figcaption className="mt-2 px-0.5">
        <div className={cn("text-[10px] font-semibold uppercase tracking-wide", isNew ? "text-accent" : "text-black/45")}>
          {categoryLabel(item.category ?? a.category)}
          {isNew ? " · the one you're buying" : ""}
        </div>
        <div className="text-sm font-semibold leading-snug line-clamp-2">{itemTitle(item)}</div>
        {(brand || price) && (
          <div className="text-xs text-black/45 truncate">{[brand, price].filter(Boolean).join(" · ")}</div>
        )}
      </figcaption>
    </figure>
  );
}
