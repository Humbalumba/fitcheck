"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  ArrowLeft,
  Check,
  ChevronDown,
  ChevronUp,
  Copy,
  Info,
  Layers,
  RotateCcw,
  ShoppingBag,
  Sparkles,
  ThumbsDown,
  ThumbsUp,
  Wand2,
} from "lucide-react";
import { api } from "@/lib/api";
import { prepareImage } from "@/lib/image";
import type { Attributes, DetectResponse, EvaluateResponse, Item } from "@/lib/types";
import { categoryKey, categoryLabel, templateLabel } from "@/lib/constants";
import { cn, money, pct, titleCase } from "@/lib/format";
import { Button, Card, ErrorBanner, FormalityDots, ItemImage, PageTitle } from "@/components/ui";
import { FilePicker } from "@/components/FilePicker";
import { PhotoWithBoxes, boxColor } from "@/components/PhotoWithBoxes";
import { AttributeEditor, AttributeSummary, diffAttributes } from "@/components/AttributeEditor";
import { OutfitCard } from "@/components/OutfitCard";
import { OutfitModal } from "@/components/OutfitModal";
import { SuggestionsSection } from "@/components/Suggestions";
import { SustainabilityCard } from "@/components/Sustainability";
import { useToast } from "@/components/Toast";

type Step = "start" | "detecting" | "select" | "evaluating" | "result";

export default function BuyPage() {
  const toast = useToast();
  const [step, setStep] = useState<Step>("start");
  const [preview, setPreview] = useState<string | null>(null);
  const [det, setDet] = useState<DetectResponse | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<Attributes>({});
  const [price, setPrice] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<EvaluateResponse | null>(null);
  const [editing, setEditing] = useState(false);
  const [added, setAdded] = useState(false);
  const [adding, setAdding] = useState(false);
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (step !== "detecting" && step !== "evaluating") return;
    setElapsed(0);
    const s = Date.now();
    const t = setInterval(() => setElapsed(Math.round((Date.now() - s) / 1000)), 500);
    return () => clearInterval(t);
  }, [step]);

  const selItem = det?.items.find((i) => i.id === selected) ?? null;

  const choose = (id: string) => {
    const it = det?.items.find((i) => i.id === id);
    if (!it) return;
    setSelected(id);
    setDraft({ ...it.attributes });
    setPrice(it.attributes.price != null ? String(it.attributes.price) : "");
    setEditing(false);
  };

  const onFiles = async (files: File[]) => {
    const f = files[0];
    if (!f) return;
    reset(false);
    setPreview(URL.createObjectURL(f));
    setStep("detecting");
    try {
      const { blob, name } = await prepareImage(f);
      const res = await api.detect(blob, "candidate", name);
      setDet(res);
      setStep("select");
      if (res.items.length === 1) {
        const it = res.items[0];
        setSelected(it.id);
        setDraft({ ...it.attributes });
        setPrice(it.attributes.price != null ? String(it.attributes.price) : "");
      }
    } catch (e) {
      setError((e as Error).message);
      setStep("start");
    }
  };

  const reset = (clearPreview = true) => {
    setDet(null);
    setSelected(null);
    setDraft({});
    setPrice("");
    setError(null);
    setResult(null);
    setAdded(false);
    setEditing(false);
    if (clearPreview) setPreview(null);
    setStep("start");
  };

  const priceNum = price === "" ? NaN : Number(price);
  const priceOk = Number.isFinite(priceNum) && priceNum >= 0;

  const evaluate = async () => {
    if (!selItem || !priceOk) return;
    setError(null);
    setStep("evaluating");
    try {
      const changes = diffAttributes(selItem.attributes, draft);
      if (Object.keys(changes).length) {
        try {
          await api.updateItem(selItem.id, changes);
        } catch {
          toast("Couldn't save attribute edits — evaluating with detected tags", "error");
        }
      }
      const r = await api.evaluate(selItem.id, priceNum);
      setResult(r);
      setStep("result");
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (e) {
      setError((e as Error).message);
      setStep("select");
    }
  };

  const addToCloset = async () => {
    if (!result) return;
    setAdding(true);
    try {
      await api.candidateToCloset(result.item?.id ?? selected!);
      setAdded(true);
      toast("Added to your closet 🎉");
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setAdding(false);
    }
  };

  if (step === "result" && result) {
    return (
      <ResultView
        r={result}
        fallbackImage={selItem?.image_url || selItem?.cutout_url}
        onAdd={addToCloset}
        adding={adding}
        added={added}
        onAgain={() => reset()}
        onBack={() => setStep("select")}
      />
    );
  }

  return (
    <div>
      <PageTitle title="Should I buy it?" subtitle="Snap the item in the store — on a hanger, a rack, or on you." />

      {error && (
        <div className="mb-4">
          <ErrorBanner message={error} />
        </div>
      )}

      {step === "start" && (
        <>
          <Card className="p-5 mb-4 relative overflow-hidden">
            <div className="absolute -right-12 -bottom-12 size-44 rounded-full bg-accent-soft" />
            <div className="relative">
              <div className="grid place-items-center size-12 rounded-2xl bg-ink text-white mb-3">
                <ShoppingBag className="size-6" />
              </div>
              <h2 className="text-xl font-bold tracking-tight">Know before you buy</h2>
              <p className="text-sm text-black/60 mt-1 max-w-sm">
                We&apos;ll count how many <b>new outfits</b> it unlocks with your closet, check if you already own
                something like it, and work out the cost per outfit.
              </p>
            </div>
          </Card>
          <FilePicker onFiles={onFiles} multiple={false} cameraLabel="Snap the item" libraryLabel="Upload photo" />
        </>
      )}

      {(step === "detecting" || step === "select" || step === "evaluating") && preview && (
        <div className="space-y-4">
          <PhotoWithBoxes
            src={det?.image_url || preview}
            boxes={(det?.items ?? []).map((i) => ({ id: i.id, bbox: i.bbox, label: i.label }))}
            selectedId={selected}
            onSelect={step === "select" ? choose : undefined}
            scanning={step === "detecting"}
          />

          {step === "detecting" && (
            <LoadingLine
              elapsed={elapsed}
              msgs={["Finding the item…", "Cutting it out…", "Reading color, fabric & price tag…"]}
            />
          )}

          {step === "select" && det && det.items.length === 0 && (
            <Card className="p-5 text-center">
              <p className="font-semibold">We couldn&apos;t find a clothing item</p>
              <p className="text-sm text-black/55 mt-1">Try again with the item filling more of the frame.</p>
              <Button className="mt-4" variant="secondary" onClick={() => reset()}>
                <RotateCcw className="size-4" /> Try another photo
              </Button>
            </Card>
          )}

          {step === "select" && det && det.items.length > 1 && (
            <div>
              <p className="text-sm font-semibold mb-2">
                {selected ? "Considering:" : `We found ${det.items.length} items — which one are you considering?`}
              </p>
              <div className="flex gap-2 overflow-x-auto no-scrollbar -mx-4 px-4 pb-1">
                {det.items.map((it, i) => (
                  <button
                    key={it.id}
                    onClick={() => choose(it.id)}
                    className={cn(
                      "shrink-0 w-24 rounded-2xl border-2 bg-white p-1 transition",
                      selected === it.id ? "border-accent" : "border-transparent",
                    )}
                  >
                    <div className="relative">
                      <ItemImage src={it.image_url || it.cutout_url} className="aspect-square rounded-xl" pad={false} />
                      <span
                        className="absolute top-1 left-1 grid place-items-center size-5 rounded-full text-[10px] font-bold text-white"
                        style={{ background: boxColor(i) }}
                      >
                        {i + 1}
                      </span>
                    </div>
                    <div className="text-[11px] font-medium mt-1 truncate px-0.5">
                      {titleCase(it.attributes.subcategory || it.label)}
                    </div>
                  </button>
                ))}
              </div>
            </div>
          )}

          {(step === "select" || step === "evaluating") && selItem && (
            <Card className="p-4">
              <div className="flex gap-3">
                <ItemImage src={selItem.image_url || selItem.cutout_url} className="size-24 rounded-2xl border border-black/5 shrink-0" pad={false} />
                <div className="flex-1 min-w-0">
                  <AttributeSummary a={draft} />
                  <div className="flex flex-wrap items-center gap-1.5 mt-2">
                    <Tag>{categoryLabel(draft.category)}</Tag>
                    {(draft.seasons ?? []).slice(0, 3).map((s) => (
                      <Tag key={s}>{titleCase(s)}</Tag>
                    ))}
                    {(draft.style_tags ?? []).slice(0, 2).map((s) => (
                      <Tag key={s}>{s}</Tag>
                    ))}
                  </div>
                  <div className="flex items-center gap-2 mt-2 text-xs text-black/50">
                    Formality <FormalityDots value={draft.formality} />
                    {draft.brand ? <span>· {draft.brand}</span> : null}
                  </div>
                </div>
              </div>
              <button
                onClick={() => setEditing((e) => !e)}
                className="mt-3 inline-flex items-center gap-1 text-xs font-semibold text-black/55"
              >
                {editing ? <ChevronUp className="size-3.5" /> : <ChevronDown className="size-3.5" />}
                {editing ? "Hide details" : "Fix details"}
              </button>
              {editing && (
                <div className="mt-3">
                  <AttributeEditor value={draft} onChange={setDraft} showPrice={false} />
                </div>
              )}

              <div className="mt-4">
                <label className="block text-[11px] font-semibold uppercase tracking-wide text-black/45 mb-1">
                  Price {selItem.attributes.price != null ? "· from tag" : "· required"}
                </label>
                <div className="relative">
                  <span className="absolute left-4 top-1/2 -translate-y-1/2 text-lg text-black/40">$</span>
                  <input
                    type="number"
                    inputMode="decimal"
                    min={0}
                    step="0.01"
                    value={price}
                    onChange={(e) => setPrice(e.target.value)}
                    placeholder="What does it cost?"
                    className={cn(
                      "w-full h-14 rounded-2xl border bg-white pl-9 pr-4 text-xl font-semibold outline-none focus:ring-2 focus:ring-accent/15",
                      !priceOk && price !== "" ? "border-rose-300" : "border-black/10 focus:border-accent",
                    )}
                  />
                </div>
              </div>

              <Button
                size="lg"
                variant="accent"
                className="mt-4"
                onClick={evaluate}
                loading={step === "evaluating"}
                disabled={!priceOk}
              >
                {!priceOk ? (
                  "Enter a price to evaluate"
                ) : step === "evaluating" ? (
                  "Evaluating…"
                ) : (
                  <>
                    <Wand2 className="size-5" /> Evaluate
                  </>
                )}
              </Button>
              {step === "evaluating" && (
                <div className="mt-3">
                  <LoadingLine
                    elapsed={elapsed}
                    msgs={["Pairing with your closet…", "Scoring outfit compatibility…", "Checking for look-alikes…"]}
                  />
                </div>
              )}
            </Card>
          )}

          {step === "select" && (
            <button onClick={() => reset()} className="w-full text-sm font-semibold text-black/50 py-2">
              Use a different photo
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function Tag({ children }: { children: React.ReactNode }) {
  return <span className="rounded-full bg-black/5 px-2 py-0.5 text-[11px] font-medium text-black/60">{children}</span>;
}

function LoadingLine({ elapsed, msgs }: { elapsed: number; msgs: string[] }) {
  const i = Math.min(msgs.length - 1, Math.floor(elapsed / 4));
  return (
    <div className="space-y-2">
      <div className="h-1.5 rounded-full bg-black/5 overflow-hidden">
        <div className="h-full bg-accent transition-all duration-500" style={{ width: `${Math.min(92, 10 + elapsed * 7)}%` }} />
      </div>
      <p className="text-xs text-black/50">
        {msgs[i]} <span className="tabular-nums">{elapsed}s</span>
      </p>
    </div>
  );
}

/* ------------------------------ Results ------------------------------ */

function ResultView({
  r,
  fallbackImage,
  onAdd,
  adding,
  added,
  onAgain,
  onBack,
}: {
  r: EvaluateResponse;
  fallbackImage?: string;
  onAdd: () => void;
  adding: boolean;
  added: boolean;
  onAgain: () => void;
  onBack: () => void;
}) {
  const decision = String(r.verdict?.decision ?? "").toUpperCase();
  const buy = decision === "BUY";
  const unsupported = decision === "UNSUPPORTED" || r.supported === false;
  const item = r.item;
  const a = item?.attributes ?? {};
  const currency = r.value?.currency || a.currency || "USD";
  const n = r.total_new_outfits ?? r.outfits?.length ?? 0;
  const vs = r.value?.value_score;
  const vsPct = vs == null ? null : Number(vs); // 0-100

  const groups = useMemo(() => {
    const m = new Map<string, typeof r.outfits>();
    for (const o of r.outfits ?? []) {
      if (!m.has(o.template)) m.set(o.template, []);
      m.get(o.template)!.push(o);
    }
    for (const list of m.values()) list.sort((x, y) => y.score - x.score);
    const names = Array.from(new Set([...(r.template_names ?? []), ...Object.keys(r.outfit_count_by_template ?? {}), ...m.keys()]));
    return names.map((t) => ({ t, list: m.get(t) ?? [], count: r.outfit_count_by_template?.[t] ?? m.get(t)?.length ?? 0 }));
  }, [r]);

  // Flat list (in on-screen order) so the zoom modal can step through every outfit.
  const flat = useMemo(() => groups.flatMap((g) => g.list), [groups]);
  const offsets = useMemo(() => {
    const o: Record<string, number> = {};
    let acc = 0;
    for (const g of groups) {
      o[g.t] = acc;
      acc += g.list.length;
    }
    return o;
  }, [groups]);
  const [zoom, setZoom] = useState<number | null>(null);
  const closeZoom = useCallback(() => setZoom(null), []);

  return (
    <div className="space-y-4">
      <button onClick={onBack} className="inline-flex items-center gap-1 text-sm font-semibold text-black/55 -mt-1">
        <ArrowLeft className="size-4" /> Back
      </button>

      {/* Verdict hero */}
      <div
        className={cn(
          "pop rounded-[28px] p-5 text-white relative overflow-hidden",
          unsupported
            ? "bg-gradient-to-br from-zinc-600 to-zinc-800"
            : buy
              ? "bg-gradient-to-br from-emerald-500 to-teal-600"
              : "bg-gradient-to-br from-rose-500 to-orange-500",
        )}
      >
        <div className="absolute -right-8 -top-8 size-40 rounded-full bg-white/10" />
        <div className="flex items-start gap-4 relative">
          <div className="flex-1">
            <div className="inline-flex items-center gap-1.5 rounded-full bg-white/20 px-2.5 py-1 text-xs font-semibold">
              {unsupported ? <Info className="size-3.5" /> : buy ? <ThumbsUp className="size-3.5" /> : <ThumbsDown className="size-3.5" />}
              {unsupported ? "Heads up" : "Our verdict"}
            </div>
            <div className={cn("font-black tracking-tight mt-2 leading-none", unsupported ? "text-3xl" : "text-6xl")}>
              {unsupported ? "Can't score outfits" : buy ? "BUY" : "SKIP"}
            </div>
            <div className="text-sm text-white/85 mt-2 font-medium">
              {titleCase([a.primary_color, a.subcategory || a.category].filter(Boolean).join(" "))}
              {r.value?.price != null ? ` · ${money(r.value.price, currency)}` : ""}
            </div>
          </div>
          <div className="size-28 rounded-3xl bg-white shadow-lg overflow-hidden shrink-0 rotate-3">
            <ItemImage src={item?.image_url || item?.cutout_url || fallbackImage} className="size-full" />
          </div>
        </div>
        {r.verdict?.reasons?.length > 0 && (
          <ul className="mt-4 space-y-1.5 relative">
            {r.verdict.reasons.map((x, i) => (
              <li key={i} className="flex gap-2 text-sm leading-snug">
                <span className="size-1.5 rounded-full bg-white mt-[7px] shrink-0 opacity-90" />
                <span>{x}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Headline stats */}
      <Card className="p-5">
        <div className="flex items-baseline gap-2">
          <span className="text-5xl font-black tracking-tight tabular-nums">{n}</span>
          <span className="text-lg font-semibold leading-tight">
            new outfit{n === 1 ? "" : "s"}
            <span className="block text-xs font-medium text-black/50">created with your closet</span>
          </span>
        </div>
        <div className="grid grid-cols-3 gap-2 mt-4">
          <Stat label="Cost / outfit" value={money(r.value?.cost_per_outfit, currency, 2)} />
          <Stat
            label="Value score"
            value={vsPct == null ? "—" : `${Math.round(vsPct)}`}
            suffix={vsPct == null ? "" : "/100"}
            bar={vsPct}
          />
          <Stat label="Price" value={money(r.value?.price, currency)} />
        </div>
        {r.value?.budget_remaining != null && (
          <div className="mt-3 flex items-center justify-between rounded-2xl bg-paper px-3 py-2 text-sm">
            <span className="text-black/55">Monthly budget left</span>
            <span className={cn("font-bold tabular-nums", (r.value.price ?? 0) > r.value.budget_remaining ? "text-rose-600" : "")}>
              {money(r.value.budget_remaining, currency)}
              {r.value.price != null && (
                <span className="font-medium text-black/40"> → {money(r.value.budget_remaining - r.value.price, currency)} after</span>
              )}
            </span>
          </div>
        )}
      </Card>

      <RedundancyCard r={r} />

      {/* Outfits */}
      <div>
        <div className="flex items-center gap-2 mb-2 mt-2">
          <Sparkles className="size-4 text-accent" />
          <h2 className="font-bold text-lg tracking-tight">Outfits it unlocks</h2>
        </div>
        {n === 0 ? (
          <Card className="p-5 text-sm text-black/55">
            {unsupported
              ? r.message || "Outfit scoring isn't available for this category yet."
              : "No outfits cleared your match strictness. Try lowering it in Settings, or add more of your closet."}
          </Card>
        ) : (
          <div className="space-y-5">
            {groups
              .filter((g) => g.count > 0 || g.list.length > 0)
              .map((g) => (
                <OutfitGroup
                  key={g.t}
                  template={g.t}
                  list={g.list}
                  count={g.count}
                  candidateId={item?.id}
                  onOpen={(i) => setZoom(offsets[g.t] + i)}
                />
              ))}
            {r.outfits_truncated && (
              <p className="text-xs text-center text-black/45">Showing the top-scoring outfits for each combination.</p>
            )}
          </div>
        )}
      </div>

      {/* Sustainability estimate (informational; hidden for unsupported items such as accessories) */}
      <SustainabilityCard s={r.sustainability} />

      {/* Live-shopping picks: fetched after the verdict renders (SKIP -> alternatives, BUY -> pairings) */}
      {!unsupported && (buy || decision === "SKIP") && (
        <SuggestionsSection key={r.evaluation_id} evaluationId={r.evaluation_id} decision={decision} />
      )}

      <OutfitModal outfits={flat} index={zoom} candidateId={item?.id} onIndex={setZoom} onClose={closeZoom} />

      {/* Actions */}
      <div className="sticky bottom-20 z-10 pt-2">
        <div className="rounded-3xl bg-white/95 backdrop-blur border border-black/5 shadow-xl p-2.5 flex gap-2">
          <Button variant="secondary" onClick={onAgain} className="shrink-0 w-11 !px-0 sm:w-auto sm:!px-5" aria-label="Try another" title="Try another">
            <RotateCcw className="size-4" /> <span className="hidden sm:inline">Try another</span>
          </Button>
          {added ? (
            <Link
              href="/closet"
              className="flex-1 inline-flex items-center justify-center gap-2 h-11 rounded-full bg-emerald-500 text-white font-semibold"
            >
              <Check className="size-4" /> In your closet · View
            </Link>
          ) : (
            <Button onClick={onAdd} loading={adding} className="flex-1 min-w-0 px-3">
              <ShoppingBag className="size-4 shrink-0" /> <span className="truncate">I bought it — add to closet</span>
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value, suffix, bar }: { label: string; value: string; suffix?: string; bar?: number | null }) {
  return (
    <div className="rounded-2xl bg-paper px-3 py-2.5">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-black/45">{label}</div>
      <div className="text-lg font-bold tabular-nums mt-0.5">
        {value}
        {suffix && <span className="text-xs font-medium text-black/40">{suffix}</span>}
      </div>
      {bar != null && (
        <div className="h-1 rounded-full bg-black/10 mt-1 overflow-hidden">
          <div className="h-full bg-accent" style={{ width: `${Math.max(0, Math.min(100, bar))}%` }} />
        </div>
      )}
    </div>
  );
}

function RedundancyCard({ r }: { r: EvaluateResponse }) {
  const level = r.redundancy?.level ?? "none";
  const matches = r.redundancy?.matches ?? [];
  const cfg =
    level === "near_duplicate"
      ? { t: "You basically own this", sub: "Near-duplicate of something in your closet", c: "bg-rose-50 text-rose-700 border-rose-200", icon: Copy }
      : level === "similar"
        ? { t: "Similar to what you own", sub: "Some overlap with your closet", c: "bg-amber-50 text-amber-700 border-amber-200", icon: Layers }
        : { t: "Fills a gap", sub: "Nothing quite like it in your closet", c: "bg-emerald-50 text-emerald-700 border-emerald-200", icon: Sparkles };
  const Icon = cfg.icon;
  return (
    <Card className="p-4">
      <div className="flex items-center gap-3">
        <div className={cn("grid place-items-center size-10 rounded-2xl border", cfg.c)}>
          <Icon className="size-5" />
        </div>
        <div className="flex-1">
          <div className="font-semibold leading-tight">{cfg.t}</div>
          <div className="text-xs text-black/50">{cfg.sub}</div>
        </div>
        <span className={cn("rounded-full border px-2.5 py-1 text-[11px] font-bold uppercase tracking-wide", cfg.c)}>
          {level === "near_duplicate" ? "Duplicate" : level === "similar" ? "Similar" : "Unique"}
        </span>
      </div>
      {matches.length > 0 && (
        <div className="mt-4">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-black/45 mb-2">Closest items you own</div>
          <div className="flex gap-2.5 overflow-x-auto no-scrollbar -mx-1 px-1">
            {matches.map((m) => (
              <MatchTile key={m.item.id} item={m.item} sim={m.similarity} />
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

function MatchTile({ item, sim }: { item: Item; sim: number }) {
  const v = sim <= 1 ? sim * 100 : sim;
  return (
    <div className="shrink-0 w-28">
      <div className="relative rounded-2xl border border-black/5 overflow-hidden">
        <ItemImage src={item.image_url || item.cutout_url} className="aspect-square" />
        <span
          className={cn(
            "absolute bottom-1.5 right-1.5 rounded-full px-1.5 py-0.5 text-[11px] font-bold text-white tabular-nums",
            v >= 90 ? "bg-rose-500" : v >= 75 ? "bg-amber-500" : "bg-black/60",
          )}
        >
          {pct(sim)}
        </span>
      </div>
      <div className="text-xs font-medium mt-1 truncate">
        {titleCase(item.attributes?.subcategory || categoryLabel(item.category))}
      </div>
    </div>
  );
}

function OutfitGroup({
  template,
  list,
  count,
  candidateId,
  onOpen,
}: {
  template: string;
  list: EvaluateResponse["outfits"];
  count: number;
  candidateId?: string;
  onOpen?: (index: number) => void;
}) {
  const [all, setAll] = useState(false);
  const shown = all ? list : list.slice(0, 4);
  const isDress = template.startsWith("dress") || list[0]?.items.some((i) => categoryKey(i.category) === "dress");
  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <div className="font-semibold">{templateLabel(template)}</div>
        <span className="rounded-full bg-ink text-white text-xs font-bold px-2.5 py-0.5 tabular-nums">{count}</span>
      </div>
      {list.length === 0 ? (
        <p className="text-sm text-black/45">{count} combinations (not returned by the server).</p>
      ) : (
        <div className={cn("grid gap-2.5", isDress ? "grid-cols-2 sm:grid-cols-3" : "grid-cols-2 sm:grid-cols-3")}>
          {shown.map((o, i) => (
            <OutfitCard key={i} outfit={o} candidateId={candidateId} onOpen={onOpen ? () => onOpen(i) : undefined} />
          ))}
        </div>
      )}
      {list.length > 4 && (
        <button onClick={() => setAll((x) => !x)} className="w-full mt-2 text-sm font-semibold text-accent py-2">
          {all ? "Show fewer" : `Show all ${list.length}`}
        </button>
      )}
    </div>
  );
}

