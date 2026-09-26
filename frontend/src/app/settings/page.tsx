"use client";

import { useCallback, useEffect, useState } from "react";
import { Target, Sparkles, Copy, RotateCcw, Scale } from "lucide-react";
import { api } from "@/lib/api";
import type { Settings } from "@/lib/types";
import { OCCASION_SUGGESTIONS } from "@/lib/constants";
import { cn, titleCase } from "@/lib/format";
import { Button, Card, ErrorBanner, PageTitle } from "@/components/ui";
import { TagInput } from "@/components/AttributeEditor";
import { useToast } from "@/components/Toast";

const DEFAULTS: Settings = {
  compat_threshold: 0.5,
  redundancy_similar_threshold: 0.8,
  redundancy_duplicate_threshold: 0.88,
  style_goal: "",
  occasions: [],
};

// Simple presets for the tuning knobs. Each button maps to exact backend values; the middle one is the default.
type Preset = { key: string; label: string; hint: string };
type MatchPreset = Preset & { value: number };
type DupePreset = Preset & { dup: number; similar: number };

const MATCH_PRESETS: MatchPreset[] = [
  { key: "chill", label: "Chill", value: 0.25, hint: "More outfits, looser matches. Fun combos welcome." },
  { key: "balanced", label: "Balanced", value: 0.5, hint: "The sweet spot: outfits that actually go together." },
  { key: "picky", label: "Picky", value: 0.7, hint: "Only the really strong matches make the cut." },
];

// dup (near-duplicate) must stay above similar
const DUPE_PRESETS: DupePreset[] = [
  { key: "relaxed", label: "Relaxed", dup: 0.92, similar: 0.85, hint: "Only speaks up when it's basically a twin of something you own." },
  { key: "normal", label: "Normal", dup: 0.88, similar: 0.8, hint: "Gives you a heads-up when it's a lot like something you've got." },
  { key: "strict", label: "Strict", dup: 0.85, similar: 0.75, hint: "Flags anything that's even kinda close to stuff you own." },
];

/** Nearest preset, so legacy custom values still highlight a button. */
function nearestMatch(v: number): string {
  const x = Number.isFinite(Number(v)) ? Number(v) : 0.5;
  return MATCH_PRESETS.reduce((a, b) => (Math.abs(b.value - x) < Math.abs(a.value - x) ? b : a)).key;
}

function nearestDupe(dup: number, similar: number): string {
  const d = Number.isFinite(Number(dup)) ? Number(dup) : 0.88;
  const s = Number.isFinite(Number(similar)) ? Number(similar) : 0.8;
  const dist = (p: DupePreset) => Math.abs(p.dup - d) + Math.abs(p.similar - s);
  return DUPE_PRESETS.reduce((a, b) => (dist(b) < dist(a) ? b : a)).key;
}

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

  return (
    <div className="space-y-4">
      <PageTitle title="Settings" subtitle="Your goals and how picky FitCheck should be." />

      <Card className="p-5 space-y-5">
        <SectionTitle icon={<Target className="size-4" />} title="Goals" />
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

      <Card className="p-5 space-y-5" data-testid="match-picker">
        <SectionTitle icon={<Sparkles className="size-4" />} title="How picky should outfit matching be?" />
        <PresetPicker
          label="Outfit matching"
          presets={MATCH_PRESETS}
          active={nearestMatch(s.compat_threshold)}
          onPick={(p) => set("compat_threshold", p.value)}
        />
        {"use_shoes_layer" in s && (
          <ToggleRow
            label="Add shoes to outfits"
            help="Finish each look with your best pair of shoes."
            on={!!s.use_shoes_layer}
            onChange={(v) => set("use_shoes_layer", v)}
          />
        )}
        {"match_gender_presentation" in s && (
          <ToggleRow
            label="Keep the cut consistent"
            help="Don't mix menswear-cut and womenswear-cut pieces (unisex stuff goes with anything)."
            on={!!s.match_gender_presentation}
            onChange={(v) => set("match_gender_presentation", v)}
          />
        )}
      </Card>

      <Card className="p-5 space-y-5" data-testid="dupe-picker">
        <SectionTitle icon={<Copy className="size-4" />} title="How careful about stuff you already own?" />
        <PresetPicker
          label="Duplicate check"
          presets={DUPE_PRESETS}
          active={nearestDupe(s.redundancy_duplicate_threshold, s.redundancy_similar_threshold)}
          onPick={(p) =>
            setS((x) => (x ? { ...x, redundancy_duplicate_threshold: p.dup, redundancy_similar_threshold: p.similar } : x))
          }
        />
      </Card>

      <Card className="p-5 space-y-3" data-testid="verdict-explainer">
        <SectionTitle icon={<Scale className="size-4" />} title="How the verdict works" />
        <p className="text-sm text-black/60 leading-relaxed">
          Every item gets one <b>0–100 score</b>: <b>BUY</b> at 65+, <b>CONSIDER</b> 45–64, <b>SKIP</b> below 45.
          Near-duplicates of something you own are always a skip.
        </p>
        <ul className="text-sm text-black/60 space-y-1.5 list-disc pl-5">
          <li>
            <b>Versatility (40%)</b> — new outfits compared with the most that kind of item could make with your
            closet (a dress can only make a few), with diminishing returns.
          </li>
          <li>
            <b>Match quality (20%)</b> — how well its best outfits go together (based on how picky you set matching above).
          </li>
          <li>
            <b>Cost per wear (40%)</b> — price ÷ expected wears, compared with your usual cost per wear (from prices of
            clothes in your closet, topped up with estimated prices until at least 3 have real prices). No price
            entered? We estimate it from the brand and type (marked &ldquo;est.&rdquo;), and a low-confidence estimate
            counts for less.
          </li>
          <li>
            <b>Gaps</b> — a bonus for your first dress, jacket, etc.; a small penalty if you already own lots of that
            type.
          </li>
        </ul>
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

function PresetPicker<P extends Preset>({
  label,
  presets,
  active,
  onPick,
}: {
  label: string;
  presets: P[];
  active: string;
  onPick: (p: P) => void;
}) {
  const current = presets.find((p) => p.key === active) ?? presets[Math.floor(presets.length / 2)];
  return (
    <div>
      <div role="radiogroup" aria-label={label} className="grid grid-cols-3 gap-1 rounded-full bg-paper p-1">
        {presets.map((p) => {
          const on = p.key === current.key;
          return (
            <button
              key={p.key}
              type="button"
              role="radio"
              aria-checked={on}
              data-preset={p.key}
              onClick={() => onPick(p)}
              className={cn(
                "h-10 rounded-full text-sm font-semibold transition",
                on ? "bg-ink text-white shadow-sm" : "text-black/55 hover:text-black/80 hover:bg-white/70",
              )}
            >
              {p.label}
            </button>
          );
        })}
      </div>
      <div className="text-xs text-black/55 mt-2 px-1" aria-live="polite">
        {current.hint}
      </div>
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
