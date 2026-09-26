"use client";

import type { Item, Outfit } from "@/lib/types";
import { categoryKey } from "@/lib/constants";
import { cn, pct } from "@/lib/format";
import { ItemImage } from "./ui";

export function OutfitCard({ outfit, candidateId }: { outfit: Outfit; candidateId?: string }) {
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
    <div className="rounded-3xl bg-white border border-black/5 p-2 shadow-[0_1px_2px_rgba(0,0,0,0.03)]">
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
  return (
    <div
      className={cn(
        "relative rounded-2xl overflow-hidden bg-[#fafaf8]",
        isNew ? "ring-2 ring-accent" : "ring-1 ring-black/5",
        tall ? "aspect-[3/4]" : small ? "aspect-square" : "aspect-[4/3]",
      )}
    >
      <ItemImage src={item.image_url || item.cutout_url} className="absolute inset-0 bg-transparent" />
      {isNew && (
        <span className="absolute top-1.5 left-1.5 rounded-full bg-accent text-white text-[9px] font-bold tracking-wide px-1.5 py-0.5">
          NEW
        </span>
      )}
    </div>
  );
}
