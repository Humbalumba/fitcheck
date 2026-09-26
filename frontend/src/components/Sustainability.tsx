"use client";

import { useEffect, useState } from "react";
import { Info, Leaf } from "lucide-react";
import { api } from "@/lib/api";
import type { Sustainability } from "@/lib/types";
import { cn } from "@/lib/format";
import { Card } from "./ui";

const GRADE_STYLE: Record<string, { solid: string; soft: string }> = {
  A: { solid: "bg-emerald-500 text-white", soft: "bg-emerald-50 text-emerald-700 border-emerald-200" },
  B: { solid: "bg-lime-500 text-white", soft: "bg-lime-50 text-lime-700 border-lime-200" },
  C: { solid: "bg-amber-400 text-white", soft: "bg-amber-50 text-amber-700 border-amber-200" },
  D: { solid: "bg-orange-500 text-white", soft: "bg-orange-50 text-orange-700 border-orange-200" },
  E: { solid: "bg-rose-500 text-white", soft: "bg-rose-50 text-rose-700 border-rose-200" },
};
const gradeStyle = (g?: string | null) => GRADE_STYLE[String(g ?? "").toUpperCase()] ?? GRADE_STYLE.C;

const METHOD =
  "Material × typical garment weight, using published life-cycle figures (WRAP 2012 fibre factors; shoes use per-pair LCAs), " +
  "divided by expected wears. Wears start from standard figures for the garment type, rise with the outfits it unlocks " +
  "and fall if you already own something similar. It doesn't change the verdict.";

export function usable(s?: Sustainability | null): s is Sustainability {
  return !!s && s.supported && s.score != null && !!s.grade;
}

const kg = (n: number) => (n >= 100 ? n.toFixed(0) : n >= 1 ? n.toFixed(1) : n >= 0.01 ? n.toFixed(2) : n.toFixed(3));
const litres = (n: number) => Math.round(n).toLocaleString("en-US");

/** "~9.7 kg CO₂e over ~130 expected wears = 0.07 kg per wear" */
export function footprintLine(s: Sustainability): string | null {
  if (s.footprint_kg_co2e == null || s.expected_wears == null || s.per_wear_kg_co2e == null) return null;
  return `~${kg(s.footprint_kg_co2e)} kg CO₂e over ~${Math.round(s.expected_wears)} expected wears = ${kg(
    s.per_wear_kg_co2e,
  )} kg per wear`;
}

/** Compact card for the "Should I buy?" result. Renders nothing for unsupported items (accessories). */
export function SustainabilityCard({ s }: { s?: Sustainability | null }) {
  const [open, setOpen] = useState(false);
  if (!usable(s)) return null;
  const st = gradeStyle(s.grade);
  const line = footprintLine(s);
  return (
    <section data-testid="sustainability-card">
      <Card className="p-4">
        <div className="flex items-start gap-3">
          <div className={cn("grid place-items-center size-16 rounded-2xl border shrink-0", st.soft)}>
            <div className="text-center leading-none">
              <div className="text-3xl font-black tabular-nums">{s.score}</div>
              <div className="text-[10px] font-semibold opacity-70 mt-0.5">/100</div>
            </div>
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-start gap-2">
              <div className="flex items-center gap-x-2 gap-y-1 flex-wrap flex-1 min-w-0">
                <Leaf className="size-4 text-emerald-600" />
                <span className="font-semibold">Sustainability</span>
                <span className={cn("rounded-full px-2 py-0.5 text-[11px] font-bold whitespace-nowrap", st.solid)}>
                  {s.grade} · {s.label}
                </span>
              </div>
              <button
                onClick={() => setOpen((o) => !o)}
                aria-expanded={open}
                aria-label="How this estimate works"
                className="shrink-0 inline-flex items-center gap-1 rounded-full bg-black/5 px-2 py-0.5 text-[11px] font-semibold text-black/55 hover:bg-black/10"
              >
                Estimate <Info className="size-3" />
              </button>
            </div>
            {line && <p className="text-sm font-medium mt-1.5 leading-snug">{line}</p>}
            {s.water_l != null && (
              <p className="text-xs text-black/50 mt-0.5">
                ~{litres(s.water_l)} L water
                {s.water_complete === false ? " (partial: not every fibre has a water figure)" : ""}
                {s.per_wear_water_l != null ? ` · ${s.per_wear_water_l.toFixed(1)} L per wear` : ""}
              </p>
            )}
          </div>
        </div>
        {s.reasons?.length > 0 && (
          <ul className="mt-3 space-y-1">
            {s.reasons.slice(0, 2).map((r, i) => (
              <li key={i} className="flex gap-2 text-xs text-black/65 leading-snug">
                <span className="size-1.5 rounded-full bg-emerald-500/70 mt-[5px] shrink-0" />
                <span>{r.replace(/CO2e/g, "CO₂e")}</span>
              </li>
            ))}
          </ul>
        )}
        {open && <p className="mt-3 rounded-2xl bg-paper px-3 py-2 text-xs text-black/60 leading-snug">{METHOD}</p>}
      </Card>
    </section>
  );
}

/** Small leaf chip with score + grade (suggestion cards, closet sheet). */
export function LeafChip({ s, className }: { s?: Sustainability | null; className?: string }) {
  if (!usable(s)) return null;
  const st = gradeStyle(s.grade);
  const label = `Sustainability estimate ${s.score}/100 (${s.grade}, ${s.label})`;
  return (
    <span
      title={label}
      aria-label={label}
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-bold tabular-nums whitespace-nowrap shrink-0",
        st.soft,
        className,
      )}
    >
      <Leaf className="size-3" />
      {s.score} · {s.grade}
    </span>
  );
}

/** Closet item sheet: grade from the item's latest "Should I buy?" evaluation, if it had one. */
export function ItemSustainability({ itemId }: { itemId: string }) {
  const [s, setS] = useState<(Sustainability & { evaluated_at?: string }) | null>(null);
  useEffect(() => {
    let alive = true;
    api
      .itemSustainability(itemId)
      .then((r) => alive && setS(r))
      .catch(() => alive && setS(null));
    return () => {
      alive = false;
    };
  }, [itemId]);
  if (!usable(s)) return null;
  const when = s.evaluated_at
    ? new Date(s.evaluated_at).toLocaleDateString("en-US", { month: "short", day: "numeric" })
    : null;
  return (
    <div className="mb-4 rounded-2xl bg-paper px-3 py-2.5 flex items-center gap-2.5" data-testid="item-sustainability">
      <LeafChip s={s} />
      <div className="text-xs text-black/60 leading-snug min-w-0">
        {s.per_wear_kg_co2e != null ? `~${kg(s.per_wear_kg_co2e)} kg CO₂e per wear` : s.label}
        <span className="text-black/40"> · estimate from your buy check{when ? ` on ${when}` : ""}</span>
      </div>
    </div>
  );
}
