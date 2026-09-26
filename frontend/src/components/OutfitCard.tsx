"use client";

import { useState } from "react";
import { ImageOff, Maximize2 } from "lucide-react";
import type { Item, Outfit } from "@/lib/types";
import { categoryKey } from "@/lib/constants";
import { mediaUrl } from "@/lib/api";
import { cn, pct } from "@/lib/format";

export function OutfitCard({
  outfit,
  candidateId,
  onOpen,
}: {
  outfit: Outfit;
  candidateId?: string;
  /** When set, the card is clickable (and keyboard-activatable) and calls this to open the zoomed view. */
  onOpen?: () => void;
}) {
  const main: Item[] = [];
  const side: Item[] = [];
  const order = (i: Item) => ["top", "dress", "bottom"].indexOf(categoryKey(i.category ?? i.attributes?.category));
  for (const it of outfit.items) {
    const k = categoryKey(it.category ?? it.attributes?.category);
    if (k === "top" || k === "bottom" || k === "dress") main.push(it);
    else side.push(it);
  }
  main.sort((a, b) => order(a) - order(b));
  if (main.length === 0) main.push(...side.splice(0, side.length));
  const score = outfit.score;
  const tone = score >= 0.75 ? "bg-emerald-500" : score >= 0.55 ? "bg-lime-500" : "bg-amber-500";

  return (
    <div
      role={onOpen ? "button" : undefined}
      tabIndex={onOpen ? 0 : undefined}
      aria-label={onOpen ? `View outfit larger (${pct(score)} compatibility)` : undefined}
      onClick={onOpen}
      onKeyDown={
        onOpen
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onOpen();
              }
            }
          : undefined
      }
      className={cn(
        "group relative rounded-3xl bg-white border border-black/5 p-2 shadow-[0_1px_2px_rgba(0,0,0,0.03)]",
        onOpen &&
          "cursor-pointer select-none transition duration-200 hover:-translate-y-0.5 hover:border-black/10 hover:shadow-[0_8px_24px_rgba(0,0,0,0.08)] active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60",
      )}
    >
      {onOpen && (
        <span className="pointer-events-none absolute top-3.5 right-3.5 z-10 grid place-items-center size-7 rounded-full bg-white/90 text-black/60 shadow-sm ring-1 ring-black/5 opacity-0 scale-90 transition group-hover:opacity-100 group-hover:scale-100 group-focus-visible:opacity-100 group-focus-visible:scale-100">
          <Maximize2 className="size-3.5" />
        </span>
      )}
      <div className={cn("grid gap-1.5", side.length ? "grid-cols-[1.6fr_1fr]" : "grid-cols-1")}>
        <div className="flex flex-col gap-1.5">
          {main.map((it) => (
            <Piece key={it.id} item={it} isNew={it.id === candidateId} tall={main.length === 1} />
          ))}
        </div>
        {side.length > 0 && (
          <div className="flex flex-col gap-1.5 justify-center">
            {side.map((it) => (
              <Piece key={it.id} item={it} isNew={it.id === candidateId} small />
            ))}
          </div>
        )}
      </div>
      <div className="flex items-center justify-between px-1.5 pt-2 pb-0.5">
        <span className="text-[11px] font-medium text-black/45">Compatibility</span>
        <span className="inline-flex items-center gap-1.5 text-xs font-bold tabular-nums">
          <span className={cn("size-2 rounded-full", tone)} />
          {pct(score)}
        </span>
      </div>
    </div>
  );
}

function Piece({ item, isNew, tall, small }: { item: Item; isNew?: boolean; tall?: boolean; small?: boolean }) {
  const [err, setErr] = useState(false);
  const url = mediaUrl(item.clean_image_url || item.image_url || item.cutout_url);
  return (
    <div
      className={cn(
        "relative rounded-2xl overflow-hidden bg-[#fafaf8]",
        isNew ? "ring-2 ring-accent" : "ring-1 ring-black/5",
        tall ? "aspect-[3/4]" : small ? "aspect-square" : "aspect-[4/3]",
      )}
    >
      {/* Same image treatment as the zoom modal's BigPiece (absolutely filled, contain, multiply over the tile bg).
          Don't pass "absolute" into ItemImage: its wrapper is "relative", cn() doesn't dedupe, and the wrapper
          collapsed to 0px height, hiding the picture. */}
      {url && !err ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={url}
          alt=""
          loading="lazy"
          onError={() => setErr(true)}
          className="absolute inset-0 size-full object-contain p-[8%] mix-blend-multiply"
        />
      ) : (
        <div className="absolute inset-0 grid place-items-center">
          <ImageOff className="size-6 text-black/20" />
        </div>
      )}
      {isNew && (
        <span className="absolute top-1.5 left-1.5 rounded-full bg-accent text-white text-[9px] font-bold tracking-wide px-1.5 py-0.5">
          NEW
        </span>
      )}
    </div>
  );
}
