"use client";

import { useEffect, useId, useState } from "react";
import { ChevronDown, Leaf, Lightbulb } from "lucide-react";
import { api } from "@/lib/api";
import type { Sustainability } from "@/lib/types";
import { cn } from "@/lib/format";
import { baseWears, comparison, materialMix, plainSummary, plainTip, rating, typeName, type Tone } from "@/lib/sustainPlain";
import { Card } from "./ui";

const TONE_STYLE: Record<Tone, { text: string; bar: string; soft: string; icon: string }> = {
  great: { text: "text-emerald-700", bar: "bg-emerald-500", soft: "bg-emerald-50 text-emerald-700 border-emerald-200", icon: "bg-emerald-100 text-emerald-700" },
  good: { text: "text-lime-700", bar: "bg-lime-500", soft: "bg-lime-50 text-lime-700 border-lime-200", icon: "bg-lime-100 text-lime-700" },
  okay: { text: "text-amber-700", bar: "bg-amber-400", soft: "bg-amber-50 text-amber-700 border-amber-200", icon: "bg-amber-100 text-amber-700" },
  meh: { text: "text-orange-700", bar: "bg-orange-500", soft: "bg-orange-50 text-orange-700 border-orange-200", icon: "bg-orange-100 text-orange-700" },
  poor: { text: "text-rose-700", bar: "bg-rose-500", soft: "bg-rose-50 text-rose-700 border-rose-200", icon: "bg-rose-100 text-rose-700" },
};
const toneStyle = (score: number) => TONE_STYLE[rating(score).tone];

const METHOD =
  "How it works: the fabric's footprint for a typical garment of this type (published life-cycle figures, WRAP 2012; " +
  "shoes use per-pair studies), divided by how often you'd likely wear it. Wears start from standard figures for the " +
  "garment type, go up with the outfits it unlocks and down if you already own something similar. It's a rough " +
  "estimate and doesn't change the verdict.";

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

function DetailRow({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex gap-3 py-1.5 border-b border-black/5 last:border-0">
      <dt className="w-24 shrink-0 text-black/45">{k}</dt>
      <dd className="flex-1 min-w-0 text-black/75">{v}</dd>
    </div>
  );
}

/** Compact card for the "Should I buy?" result. Renders nothing for unsupported items (accessories).
 *  Leads with a friendly rating + plain-English "why"; raw numbers live behind "See the details". */
export function SustainabilityCard({ s }: { s?: Sustainability | null }) {
  const [open, setOpen] = useState(false);
  const detailsId = useId();
  if (!usable(s)) return null;
  const score = s.score as number;
  const { label } = rating(score);
  const st = toneStyle(score);
  const summary = plainSummary(s);
  const tip = plainTip(s);
  const cmp = comparison(s);
  const mix = materialMix(s);
  const base = baseWears(s);
  const type = typeName(s.garment_type);
  return (
    <section data-testid="sustainability-card">
      <Card className="p-4">
        <div className="flex items-center gap-3">
          <div className={cn("grid place-items-center size-10 rounded-full shrink-0", st.icon)}>
            <Leaf className="size-5" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-[11px] font-semibold uppercase tracking-wide text-black/45">Sustainability</div>
            <div className={cn("text-lg font-bold leading-tight", st.text)} data-testid="sustainability-rating">
              {label}
            </div>
          </div>
          <div className="w-20 sm:w-28 shrink-0 text-right" aria-label={`Score ${score} out of 100`}>
            <div className="text-[11px] text-black/45 tabular-nums">
              <span className="font-semibold text-black/60">{score}</span>/100
            </div>
            <div className="mt-1 h-1.5 rounded-full bg-black/[0.07] overflow-hidden">
              <div className={cn("h-full rounded-full", st.bar)} style={{ width: `${Math.max(4, score)}%` }} />
            </div>
          </div>
        </div>

        <p className="mt-3 text-sm leading-relaxed text-black/75" data-testid="sustainability-summary">
          {summary}
        </p>

        <div className="mt-2.5 flex gap-2 rounded-2xl bg-sand px-3 py-2 text-xs leading-snug text-black/65">
          <Lightbulb className="size-3.5 mt-px shrink-0 text-amber-500" />
          <span data-testid="sustainability-tip">{tip}</span>
        </div>

        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          aria-controls={detailsId}
          className="mt-2 -ml-1 inline-flex items-center gap-1 rounded-full px-1 py-1 text-xs font-semibold text-black/50 hover:text-black/75"
        >
          {open ? "Hide the details" : "See the details"}
          <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} />
        </button>

        {open && (
          <div id={detailsId} className="mt-1 rounded-2xl border border-black/5 px-3 py-1 text-xs" data-testid="sustainability-details">
            <dl>
              <DetailRow k="Score" v={`${score}/100 (50 = a typical ${type}; higher is better)`} />
              {cmp && <DetailRow k="Compared" v={cmp} />}
              {mix && <DetailRow k="Made of" v={mix} />}
              {s.expected_wears != null && (
                <DetailRow
                  k="Likely wears"
                  v={`~${Math.round(s.expected_wears)}${base != null ? ` (a typical ${type} gets ~${Math.round(base)})` : ""}`}
                />
              )}
              {s.footprint_kg_co2e != null && (
                <DetailRow
                  k="Carbon"
                  v={`~${kg(s.footprint_kg_co2e)} kg CO₂e to make and care for${
                    s.per_wear_kg_co2e != null ? ` · ${kg(s.per_wear_kg_co2e)} kg per wear` : ""
                  }`}
                />
              )}
              {s.water_l != null && (
                <DetailRow
                  k="Water"
                  v={`~${litres(s.water_l)} L${s.per_wear_water_l != null ? ` · ${s.per_wear_water_l.toFixed(1)} L per wear` : ""}${
                    s.water_complete === false ? " (partial: not every fibre has a water figure)" : ""
                  }`}
                />
              )}
            </dl>
            <p className="py-2 text-[11px] leading-snug text-black/45">{METHOD}</p>
          </div>
        )}
      </Card>
    </section>
  );
}

/** Small leaf chip with score + grade (suggestion cards, closet sheet). */
export function LeafChip({ s, className }: { s?: Sustainability | null; className?: string }) {
  if (!usable(s)) return null;
  const st = toneStyle(s.score as number);
  const label = `Sustainability: ${rating(s.score as number).label} (${s.score}/100 estimate)`;
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
      {s.score}
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
    <div className="mb-4 rounded-2xl bg-sand px-3 py-2.5 flex items-center gap-2.5" data-testid="item-sustainability">
      <LeafChip s={s} />
      <div className="text-xs text-black/60 leading-snug min-w-0">
        <span className="font-semibold">{rating(s.score as number).label}</span> for the planet
        <span className="text-black/40"> · estimate from your buy check{when ? ` on ${when}` : ""}</span>
      </div>
    </div>
  );
}
