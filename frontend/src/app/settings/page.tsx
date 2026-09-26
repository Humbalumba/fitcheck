"use client";

import { useCallback, useEffect, useState } from "react";
import { Target, SlidersHorizontal, RotateCcw } from "lucide-react";
import { api } from "@/lib/api";
import type { Settings } from "@/lib/types";
import { OCCASION_SUGGESTIONS } from "@/lib/constants";
import { cn, titleCase } from "@/lib/format";
import { Button, Card, ErrorBanner, PageTitle } from "@/components/ui";
import { TagInput } from "@/components/AttributeEditor";
import { useToast } from "@/components/Toast";

const DEFAULTS: Settings = {
  compat_threshold: 0.5,
  redundancy_similar_threshold: 0.82,
  redundancy_duplicate_threshold: 0.9,
  min_new_outfits: 3,
  max_cost_per_outfit: 10,
  monthly_budget: null,
  style_goal: "",
  occasions: [],
};

export default function SettingsPage() {
  const toast = useToast();
  const [orig, setOrig] = useState<Settings | null>(null);
  const [s, setS] = useState<Settings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const r = await api.getSettings();
      const merged = { ...DEFAULTS, ...r };
      setOrig(merged);
      setS(merged);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const set = <K extends keyof Settings>(k: K, v: Settings[K]) => setS((x) => (x ? { ...x, [k]: v } : x));

  const patch: Partial<Settings> = {};
  if (s && orig) {
    for (const k of Object.keys(s) as (keyof Settings)[]) {
      if (JSON.stringify(s[k]) !== JSON.stringify(orig[k])) (patch as Record<string, unknown>)[k as string] = s[k];
    }
  }
  const dirty = Object.keys(patch).length > 0;

  const save = async () => {
    setSaving(true);
    try {
      const r = await api.putSettings(patch);
      const merged = { ...DEFAULTS, ...(r && typeof r === "object" ? r : s) } as Settings;
      setOrig(merged);
      setS(merged);
      toast("Settings saved");
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setSaving(false);
    }
  };

  if (error)
    return (
      <div>
        <PageTitle title="Settings" />
        <ErrorBanner message={error} onRetry={load} />
      </div>
    );
  if (!s)
    return (
      <div>
        <PageTitle title="Settings" />
        <div className="space-y-3">
          <div className="h-48 rounded-3xl skeleton" />
          <div className="h-72 rounded-3xl skeleton" />
        </div>
      </div>
    );

  const dupInvalid = s.redundancy_duplicate_threshold < s.redundancy_similar_threshold;

  return (
    <div className="space-y-4">
      <PageTitle title="Settings" subtitle="Your goals and how picky FitCheck should be." />

      <Card className="p-5 space-y-5">
        <SectionTitle icon={<Target className="size-4" />} title="Goals" />
        <div>
          <Label>Monthly clothing budget</Label>
          <div className="relative">
            <span className="absolute left-3.5 top-1/2 -translate-y-1/2 text-black/40">$</span>
            <input
              type="number"
              inputMode="decimal"
              min={0}
              value={s.monthly_budget ?? ""}
              onChange={(e) => set("monthly_budget", e.target.value === "" ? null : Number(e.target.value))}
              placeholder="e.g. 150"
              className="w-full h-11 rounded-xl border border-black/10 bg-white pl-7 pr-3 text-[15px] outline-none focus:border-accent focus:ring-2 focus:ring-accent/15"
            />
          </div>
        </div>
        <div>
          <Label>Style goal</Label>
          <textarea
            value={s.style_goal ?? ""}
            onChange={(e) => set("style_goal", e.target.value)}
            rows={3}
            placeholder="e.g. A minimal capsule wardrobe in neutrals that works for the office and weekends"
            className="w-full rounded-xl border border-black/10 bg-white px-3 py-2.5 text-[15px] outline-none focus:border-accent focus:ring-2 focus:ring-accent/15 resize-none"
          />
        </div>
        <div>
          <Label>Occasions you dress for</Label>
          <div className="flex flex-wrap gap-2 mb-2">
            {Array.from(new Set([...OCCASION_SUGGESTIONS, ...(s.occasions ?? [])])).map((o) => {
              const on = (s.occasions ?? []).includes(o);
              return (
                <button
                  key={o}
                  type="button"
                  onClick={() => set("occasions", on ? s.occasions.filter((x) => x !== o) : [...(s.occasions ?? []), o])}
                  className={cn(
                    "rounded-full border px-3.5 h-9 text-sm font-medium transition",
                    on ? "bg-ink text-white border-ink" : "bg-white border-black/10 text-black/65",
                  )}
                >
                  {titleCase(o)}
                </button>
              );
            })}
          </div>
          <TagInput
            value={[]}
            onChange={(v) => set("occasions", Array.from(new Set([...(s.occasions ?? []), ...v])))}
            placeholder="Add your own occasion…"
          />
        </div>
      </Card>

      <Card className="p-5 space-y-6">
        <SectionTitle icon={<SlidersHorizontal className="size-4" />} title="Decision thresholds" />
        <SliderRow
          label="Match strictness"
          help="Higher = only stronger outfit matches count. 0.5 is balanced; FitCheck calibrates it per outfit size."
          value={s.compat_threshold}
          min={0}
          max={1}
          step={0.01}
          fmt={(v) => `${v.toFixed(2)} · ${v < 0.35 ? "Relaxed" : v <= 0.65 ? "Balanced" : "Strict"}`}
          range={["0 · Relaxed", "1 · Strict"]}
          onChange={(v) => set("compat_threshold", v)}
        />
        <SliderRow
          label="“Similar” redundancy threshold"
          help="Similarity at which an item counts as similar to something you own."
          value={s.redundancy_similar_threshold}
          min={0}
          max={1}
          step={0.01}
          fmt={(v) => v.toFixed(2)}
          onChange={(v) => set("redundancy_similar_threshold", v)}
        />
        <SliderRow
          label="“Near-duplicate” threshold"
          help="Similarity at which it's basically something you already own."
          value={s.redundancy_duplicate_threshold}
          min={0}
          max={1}
          step={0.01}
          fmt={(v) => v.toFixed(2)}
          onChange={(v) => set("redundancy_duplicate_threshold", v)}
          warn={dupInvalid ? "Should be higher than the similar threshold" : undefined}
        />
        <SliderRow
          label="Minimum new outfits"
          help="Skip anything that unlocks fewer outfits than this."
          value={s.min_new_outfits}
          min={0}
          max={20}
          step={1}
          fmt={(v) => String(v)}
          onChange={(v) => set("min_new_outfits", v)}
        />
        <SliderRow
          label="Max cost per outfit"
          help="Price divided by new outfits must be under this."
          value={s.max_cost_per_outfit}
          min={1}
          max={100}
          step={1}
          fmt={(v) => `$${v}`}
          onChange={(v) => set("max_cost_per_outfit", v)}
        />
        {"use_shoes_layer" in s && (
          <ToggleRow
            label="Include shoes in outfits"
            help="Count outfits with a shoes layer (e.g. Top + Bottom + Shoes)."
            on={!!s.use_shoes_layer}
            onChange={(v) => set("use_shoes_layer", v)}
          />
        )}
        {"match_gender_presentation" in s && (
          <ToggleRow
            label="Keep outfits consistent in cut"
            help="Don't pair menswear-cut with womenswear-cut pieces (unisex items pair with anything)."
            on={!!s.match_gender_presentation}
            onChange={(v) => set("match_gender_presentation", v)}
          />
        )}
      </Card>

      <div className={cn("sticky bottom-20 z-10 transition", dirty ? "opacity-100" : "opacity-0 pointer-events-none")}>
        <div className="rounded-3xl bg-white/95 backdrop-blur border border-black/5 shadow-xl p-2.5 flex gap-2">
          <Button variant="secondary" onClick={() => setS(orig)} disabled={!dirty} className="shrink-0">
            <RotateCcw className="size-4" /> Reset
          </Button>
          <Button onClick={save} loading={saving} disabled={!dirty} className="flex-1">
            {dirty ? "Save settings" : "All changes saved"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function SectionTitle({ icon, title }: { icon: React.ReactNode; title: string }) {
  return (
    <div className="flex items-center gap-2">
      <span className="grid place-items-center size-7 rounded-lg bg-accent-soft text-accent">{icon}</span>
      <h2 className="font-bold tracking-tight">{title}</h2>
    </div>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return <div className="text-[11px] font-semibold uppercase tracking-wide text-black/45 mb-1.5">{children}</div>;
}

function SliderRow({
  label,
  help,
  value,
  min,
  max,
  step,
  fmt,
  onChange,
  warn,
  range,
}: {
  label: string;
  help?: string;
  value: number;
  min: number;
  max: number;
  step: number;
  fmt: (v: number) => string;
  onChange: (v: number) => void;
  warn?: string;
  range?: [string, string];
}) {
  const v = Number(value ?? min);
  return (
    <div>
      <div className="flex items-baseline justify-between gap-3">
        <div className="text-sm font-semibold">{label}</div>
        <div className="text-sm font-bold tabular-nums rounded-lg bg-paper px-2 py-0.5">{fmt(v)}</div>
      </div>
      {help && <div className="text-xs text-black/50 mt-0.5">{help}</div>}
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={v}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full mt-2.5 h-2"
      />
      <div className="flex justify-between text-[10px] text-black/35 -mt-0.5">
        <span>{range?.[0] ?? fmt(min)}</span>
        <span>{range?.[1] ?? fmt(max)}</span>
      </div>
      {warn && <div className="text-xs text-amber-700 mt-1">{warn}</div>}
    </div>
  );
}

function ToggleRow({ label, help, on, onChange }: { label: string; help?: string; on: boolean; onChange: (v: boolean) => void }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <div className="text-sm font-semibold">{label}</div>
        {help && <div className="text-xs text-black/50 mt-0.5">{help}</div>}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={on}
        aria-label={label}
        onClick={() => onChange(!on)}
        className={cn("relative shrink-0 h-7 w-12 rounded-full transition", on ? "bg-accent" : "bg-black/15")}
      >
        <span
          className={cn(
            "absolute top-0.5 size-6 rounded-full bg-white shadow transition-all",
            on ? "left-[22px]" : "left-0.5",
          )}
        />
      </button>
    </div>
  );
}
