"use client";

import { useState } from "react";
import { X } from "lucide-react";
import type { Attributes } from "@/lib/types";
import { CATEGORIES, FORMALITY_LABELS, PATTERNS, SEASONS, categoryKey } from "@/lib/constants";
import { cn, titleCase } from "@/lib/format";
import { ColorDot } from "./ui";

const inputCls =
  "w-full h-10 rounded-xl border border-black/10 bg-white px-3 text-[15px] outline-none focus:border-accent focus:ring-2 focus:ring-accent/15";

function Field({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  return (
    <label className={cn("block", className)}>
      <span className="block text-[11px] font-semibold uppercase tracking-wide text-black/45 mb-1">{label}</span>
      {children}
    </label>
  );
}

export function TagInput({
  value,
  onChange,
  placeholder,
  suggestions,
}: {
  value: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
  suggestions?: string[];
}) {
  const [text, setText] = useState("");
  const add = (raw: string) => {
    const parts = raw
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean)
      .filter((s) => !value.includes(s));
    if (parts.length) onChange([...value, ...parts]);
    setText("");
  };
  const unused = (suggestions ?? []).filter((s) => !value.includes(s));
  return (
    <div>
      <div className="flex flex-wrap gap-1.5 rounded-xl border border-black/10 bg-white p-1.5 focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/15">
        {value.map((t) => (
          <span key={t} className="inline-flex items-center gap-1 rounded-full bg-black/[0.06] pl-2.5 pr-1 h-7 text-sm">
            {t}
            <button
              type="button"
              onClick={() => onChange(value.filter((x) => x !== t))}
              className="grid place-items-center size-5 rounded-full hover:bg-black/10"
              aria-label={`Remove ${t}`}
            >
              <X className="size-3" />
            </button>
          </span>
        ))}
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              add(text);
            } else if (e.key === "Backspace" && !text && value.length) {
              onChange(value.slice(0, -1));
            }
          }}
          onBlur={() => text && add(text)}
          placeholder={value.length ? "" : placeholder}
          className="flex-1 min-w-[90px] h-7 px-1.5 text-[15px] outline-none bg-transparent"
        />
      </div>
      {unused.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mt-2">
          {unused.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => onChange([...value, s])}
              className="rounded-full border border-dashed border-black/20 px-2.5 h-7 text-xs text-black/60 hover:bg-black/5"
            >
              + {s}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function AttributeEditor({
  value,
  onChange,
  showPrice = true,
}: {
  value: Attributes;
  onChange: (v: Attributes) => void;
  showPrice?: boolean;
}) {
  const set = <K extends keyof Attributes>(k: K, v: Attributes[K]) => onChange({ ...value, [k]: v });
  const catKey = categoryKey(value.category);
  const seasons = (value.seasons ?? []).map((s) => s.toLowerCase());
  const patternOpts = PATTERNS.includes(String(value.pattern ?? "").toLowerCase()) || !value.pattern
    ? PATTERNS
    : [String(value.pattern), ...PATTERNS];

  return (
    <div className="grid grid-cols-2 gap-3">
      <Field label="Category">
        <select
          className={inputCls}
          value={catKey === "other" ? String(value.category ?? "") : catKey}
          onChange={(e) => set("category", e.target.value)}
        >
          {catKey === "other" && <option value={String(value.category ?? "")}>{titleCase(String(value.category ?? "Other"))}</option>}
          {CATEGORIES.map((c) => (
            <option key={c.key} value={c.key}>
              {c.singular}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Type">
        <input
          className={inputCls}
          value={value.subcategory ?? ""}
          placeholder="e.g. hoodie"
          onChange={(e) => set("subcategory", e.target.value)}
        />
      </Field>
      <Field label="Main color">
        <div className="relative">
          <input
            className={cn(inputCls, "pl-8")}
            value={value.primary_color ?? ""}
            placeholder="e.g. navy"
            onChange={(e) => set("primary_color", e.target.value)}
          />
          <span className="absolute left-3 top-1/2 -translate-y-1/2">
            <ColorDot color={value.primary_color} />
          </span>
        </div>
      </Field>
      <Field label="Pattern">
        <select
          className={inputCls}
          value={String(value.pattern ?? "").toLowerCase() || ""}
          onChange={(e) => set("pattern", e.target.value)}
        >
          <option value="">—</option>
          {patternOpts.map((p) => (
            <option key={p} value={p.toLowerCase()}>
              {titleCase(p)}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Other colors" className="col-span-2">
        <TagInput
          value={value.secondary_colors ?? []}
          onChange={(v) => set("secondary_colors", v)}
          placeholder="Add color, press Enter"
        />
      </Field>
      <Field label="Fabric">
        <input
          className={inputCls}
          value={value.fabric_guess ?? ""}
          placeholder="e.g. cotton"
          onChange={(e) => set("fabric_guess", e.target.value)}
        />
      </Field>
      {showPrice ? (
        <Field label="Price">
          <div className="relative">
            <span className="absolute left-3 top-1/2 -translate-y-1/2 text-black/40">$</span>
            <input
              className={cn(inputCls, "pl-6")}
              type="number"
              inputMode="decimal"
              min={0}
              step="0.01"
              value={value.price ?? ""}
              placeholder="0.00"
              onChange={(e) => set("price", e.target.value === "" ? null : Number(e.target.value))}
            />
          </div>
        </Field>
      ) : (
        <Field label="Brand">
          <input className={inputCls} value={value.brand ?? ""} onChange={(e) => set("brand", e.target.value)} />
        </Field>
      )}
      <Field label={`Formality · ${FORMALITY_LABELS[Number(value.formality) || 0] || "—"}`} className="col-span-2">
        <div className="grid grid-cols-5 gap-1.5">
          {[1, 2, 3, 4, 5].map((n) => (
            <button
              key={n}
              type="button"
              onClick={() => set("formality", n)}
              className={cn(
                "h-9 rounded-xl border text-sm font-semibold transition",
                Number(value.formality) === n
                  ? "bg-ink text-white border-ink"
                  : "bg-white border-black/10 text-black/60 hover:border-black/25",
              )}
            >
              {n}
            </button>
          ))}
        </div>
      </Field>
      <Field label="Seasons" className="col-span-2">
        <div className="grid grid-cols-4 gap-1.5">
          {SEASONS.map((s) => {
            const on = seasons.includes(s) || (s === "fall" && seasons.includes("autumn"));
            return (
              <button
                key={s}
                type="button"
                onClick={() =>
                  set(
                    "seasons",
                    on ? seasons.filter((x) => x !== s && !(s === "fall" && x === "autumn")) : [...seasons, s],
                  )
                }
                className={cn(
                  "h-9 rounded-xl border text-sm font-medium transition",
                  on ? "bg-accent-soft border-accent/40 text-accent" : "bg-white border-black/10 text-black/60",
                )}
              >
                {titleCase(s)}
              </button>
            );
          })}
        </div>
      </Field>
      <Field label="Style tags" className="col-span-2">
        <TagInput
          value={value.style_tags ?? []}
          onChange={(v) => set("style_tags", v)}
          placeholder="e.g. minimal, streetwear"
          suggestions={["casual", "minimal", "classic", "streetwear", "sporty", "preppy", "boho", "edgy"].slice(0, 6)}
        />
      </Field>
    </div>
  );
}

/** One-line human summary of an item's attributes. */
export function AttributeSummary({ a, className }: { a: Attributes; className?: string }) {
  const title = [a.primary_color, a.subcategory || a.category].filter(Boolean).join(" ");
  const meta = [a.pattern && a.pattern !== "solid" ? a.pattern : null, a.fabric_guess].filter(Boolean).join(" · ");
  return (
    <div className={cn("min-w-0", className)}>
      <div className="flex items-center gap-1.5 font-semibold text-[15px] leading-tight">
        <ColorDot color={a.primary_color} />
        <span className="truncate">{titleCase(title) || "Item"}</span>
      </div>
      {meta && <div className="text-xs text-black/50 mt-0.5 truncate">{titleCase(meta)}</div>}
    </div>
  );
}

/** Diff two attribute objects, returning only changed keys. */
export function diffAttributes(orig: Attributes, next: Attributes): Partial<Attributes> {
  const out: Partial<Attributes> = {};
  for (const k of Object.keys(next)) {
    if (JSON.stringify(orig[k]) !== JSON.stringify(next[k])) out[k] = next[k];
  }
  return out;
}
