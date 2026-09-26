"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Check, ChevronDown, ChevronUp, Loader2, RotateCcw, Sparkles, X } from "lucide-react";
import { api } from "@/lib/api";
import { prepareImage } from "@/lib/image";
import type { Attributes, DetectResponse, DetectedItem } from "@/lib/types";
import { categoryLabel } from "@/lib/constants";
import { cn } from "@/lib/format";
import { Button, Card, FormalityDots, ItemImage, PageTitle } from "@/components/ui";
import { FilePicker } from "@/components/FilePicker";
import { PhotoWithBoxes, boxColor } from "@/components/PhotoWithBoxes";
import { AttributeEditor, AttributeSummary, diffAttributes } from "@/components/AttributeEditor";
import { useToast } from "@/components/Toast";

type Status = "queued" | "detecting" | "ready" | "adding" | "added" | "error";

interface Batch {
  key: string;
  file: File;
  preview: string;
  status: Status;
  error?: string;
  result?: DetectResponse;
  include: Record<string, boolean>;
  drafts: Record<string, Attributes>;
  addedCount?: number;
  startedAt?: number;
}

export default function AddPage() {
  const toast = useToast();
  const [batches, setBatches] = useState<Batch[]>([]);
  const running = useRef(false);

  const update = useCallback((key: string, patch: Partial<Batch> | ((b: Batch) => Partial<Batch>)) => {
    setBatches((bs) => bs.map((b) => (b.key === key ? { ...b, ...(typeof patch === "function" ? patch(b) : patch) } : b)));
  }, []);

  const addFiles = (files: File[]) => {
    const now = Date.now();
    setBatches((bs) => [
      ...bs,
      ...files.map((f, i) => ({
        key: `${now}-${i}-${f.name}`,
        file: f,
        preview: URL.createObjectURL(f),
        status: "queued" as Status,
        include: {},
        drafts: {},
      })),
    ]);
  };

  // Sequential detection queue
  useEffect(() => {
    if (running.current) return;
    const next = batches.find((b) => b.status === "queued");
    if (!next) return;
    running.current = true;
    update(next.key, { status: "detecting", startedAt: Date.now(), error: undefined });
    (async () => {
      try {
        const { blob, name } = await prepareImage(next.file);
        const res = await api.detect(blob, "closet", name);
        const include: Record<string, boolean> = {};
        const drafts: Record<string, Attributes> = {};
        for (const it of res.items ?? []) {
          include[it.id] = true;
          drafts[it.id] = { ...it.attributes };
        }
        update(next.key, { status: "ready", result: res, include, drafts });
      } catch (e) {
        update(next.key, { status: "error", error: (e as Error).message });
      } finally {
        running.current = false;
        // trigger effect again for the next queued photo
        setBatches((bs) => [...bs]);
      }
    })();
  }, [batches, update]);

  const addBatch = async (b: Batch) => {
    if (!b.result) return;
    const ids = b.result.items.filter((it) => b.include[it.id]).map((it) => it.id);
    if (!ids.length) return;
    const overrides: Record<string, Partial<Attributes>> = {};
    for (const it of b.result.items) {
      if (!b.include[it.id]) continue;
      const d = diffAttributes(it.attributes, b.drafts[it.id] ?? it.attributes);
      if (Object.keys(d).length) overrides[it.id] = d;
    }
    update(b.key, { status: "adding" });
    try {
      const added = await api.addToCloset(ids, overrides);
      update(b.key, { status: "added", addedCount: added.length || ids.length });
      toast(`Added ${added.length || ids.length} item${ids.length === 1 ? "" : "s"} to your closet`);
    } catch (e) {
      update(b.key, { status: "ready" });
      toast((e as Error).message, "error");
    }
  };

  const queued = batches.filter((b) => b.status === "queued" || b.status === "detecting").length;
  const totalAdded = batches.reduce((s, b) => s + (b.addedCount ?? 0), 0);

  return (
    <div>
      <PageTitle
        title="Add to closet"
        subtitle={batches.length ? "Keep snapping — each photo is processed in order." : "Build your digital closet in minutes."}
      />

      {batches.length === 0 ? (
        <Card className="p-5 mb-4 overflow-hidden relative">
          <div className="absolute -right-10 -top-10 size-40 rounded-full bg-accent-soft" />
          <div className="relative">
            <div className="inline-flex items-center gap-1.5 rounded-full bg-accent-soft text-accent text-xs font-semibold px-2.5 py-1 mb-3">
              <Sparkles className="size-3.5" /> AI detects every piece
            </div>
            <h2 className="text-xl font-bold tracking-tight">Lay out a few clothes, snap one photo</h2>
            <ol className="mt-3 space-y-2 text-sm text-black/65">
              {[
                "Spread 2–6 items flat on your bed or floor, not overlapping.",
                "Shoot from above in good light.",
                "Review the cut-outs, fix any tags, and add them.",
              ].map((t, i) => (
                <li key={i} className="flex gap-2.5">
                  <span className="grid place-items-center size-5 rounded-full bg-ink text-white text-[11px] font-bold shrink-0 mt-px">
                    {i + 1}
                  </span>
                  {t}
                </li>
              ))}
            </ol>
          </div>
        </Card>
      ) : null}

      <div className="mb-5">
        <FilePicker onFiles={addFiles} compact={batches.length > 0} cameraLabel={batches.length ? "Another photo" : "Take photo"} />
      </div>

      {batches.length > 0 && (
        <div className="flex items-center justify-between text-sm mb-3 px-1">
          <span className="text-black/55">
            {queued > 0 ? (
              <span className="inline-flex items-center gap-1.5">
                <Loader2 className="size-3.5 animate-spin" /> Processing {queued} photo{queued === 1 ? "" : "s"}…
              </span>
            ) : (
              `${batches.length} photo${batches.length === 1 ? "" : "s"} processed`
            )}
          </span>
          {totalAdded > 0 && (
            <Link href="/closet" className="font-semibold text-accent">
              {totalAdded} added · View closet →
            </Link>
          )}
        </div>
      )}

      <div className="space-y-5">
        {batches.map((b, idx) => (
          <BatchCard
            key={b.key}
            index={idx}
            batch={b}
            onToggle={(id) => update(b.key, (x) => ({ include: { ...x.include, [id]: !x.include[id] } }))}
            onDraft={(id, a) => update(b.key, (x) => ({ drafts: { ...x.drafts, [id]: a } }))}
            onAdd={() => addBatch(b)}
            onRetry={() => update(b.key, { status: "queued", error: undefined })}
            onRemove={() => setBatches((bs) => bs.filter((x) => x.key !== b.key))}
          />
        ))}
      </div>
    </div>
  );
}

function BatchCard({
  batch: b,
  index,
  onToggle,
  onDraft,
  onAdd,
  onRetry,
  onRemove,
}: {
  batch: Batch;
  index: number;
  onToggle: (id: string) => void;
  onDraft: (id: string, a: Attributes) => void;
  onAdd: () => void;
  onRetry: () => void;
  onRemove: () => void;
}) {
  const [hi, setHi] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (b.status !== "detecting") return;
    const t = setInterval(() => setElapsed(Math.round((Date.now() - (b.startedAt ?? Date.now())) / 1000)), 500);
    return () => clearInterval(t);
  }, [b.status, b.startedAt]);

  const items = b.result?.items ?? [];
  const n = items.filter((it) => b.include[it.id]).length;
  const excluded = new Set(items.filter((it) => !b.include[it.id]).map((it) => it.id));

  return (
    <Card className="overflow-hidden">
      <div className="flex items-center justify-between px-4 pt-3.5 pb-2.5">
        <div className="font-semibold">
          Photo {index + 1}
          <span className="font-normal text-black/50">
            {b.status === "queued" && " · waiting"}
            {b.status === "detecting" && ` · finding clothes… ${elapsed}s`}
            {(b.status === "ready" || b.status === "adding") && ` · ${items.length} item${items.length === 1 ? "" : "s"} found`}
            {b.status === "added" && ` · ${b.addedCount} added`}
            {b.status === "error" && " · failed"}
          </span>
        </div>
        {(b.status === "error" || b.status === "added" || b.status === "queued" || (b.status === "ready" && items.length === 0)) && (
          <button onClick={onRemove} className="grid place-items-center size-8 rounded-full hover:bg-black/5" aria-label="Dismiss">
            <X className="size-4" />
          </button>
        )}
      </div>

      {b.status === "added" ? (
        <div className="px-4 pb-4">
          <div className="flex items-center gap-3 rounded-2xl bg-emerald-50 border border-emerald-200 p-3">
            <div className="grid place-items-center size-9 rounded-full bg-emerald-500 text-white">
              <Check className="size-5" />
            </div>
            <div className="flex-1 text-sm">
              <div className="font-semibold text-emerald-900">Added {b.addedCount} to your closet</div>
              <div className="flex -space-x-2 mt-1.5">
                {items
                  .filter((it) => b.include[it.id])
                  .slice(0, 6)
                  .map((it) => (
                    <ItemImage key={it.id} src={it.cutout_url} className="size-9 rounded-full ring-2 ring-emerald-50" pad={false} />
                  ))}
              </div>
            </div>
            <Link href="/closet" className="text-sm font-semibold text-emerald-800">
              View →
            </Link>
          </div>
        </div>
      ) : (
        <>
          <div className="px-3">
            <PhotoWithBoxes
              src={b.result?.image_url || b.preview}
              boxes={items.map((it) => ({ id: it.id, bbox: it.bbox, label: it.label }))}
              highlightId={hi}
              dimmedIds={excluded}
              onSelect={(id) => onToggle(id)}
              scanning={b.status === "detecting" || b.status === "queued"}
            />
          </div>

          {b.status === "detecting" && (
            <div className="px-4 py-4 space-y-2">
              <div className="h-1.5 rounded-full bg-black/5 overflow-hidden">
                <div
                  className="h-full bg-accent transition-all duration-500"
                  style={{ width: `${Math.min(92, 8 + elapsed * 7)}%` }}
                />
              </div>
              <p className="text-xs text-black/50">
                {elapsed < 4
                  ? "Uploading & detecting garments…"
                  : elapsed < 9
                    ? "Cutting out each item…"
                    : "Tagging colors, fabric & style…"}
              </p>
            </div>
          )}

          {b.status === "error" && (
            <div className="p-4 flex items-center justify-between gap-3">
              <p className="text-sm text-rose-700">{b.error}</p>
              <Button size="sm" variant="secondary" onClick={onRetry}>
                <RotateCcw className="size-4" /> Retry
              </Button>
            </div>
          )}

          {(b.status === "ready" || b.status === "adding") && (
            <>
              {items.length === 0 ? (
                <p className="p-4 text-sm text-black/55">
                  No clothing found in this photo. Try better lighting or spread the items apart.
                </p>
              ) : (
                <div className="p-3 space-y-2">
                  {items.map((it, i) => (
                    <DetectedCard
                      key={it.id}
                      item={it}
                      index={i}
                      included={!!b.include[it.id]}
                      draft={b.drafts[it.id] ?? it.attributes}
                      onToggle={() => onToggle(it.id)}
                      onDraft={(a) => onDraft(it.id, a)}
                      onHover={(on) => setHi(on ? it.id : null)}
                    />
                  ))}
                </div>
              )}
              {items.length > 0 && (
                <div className="px-3 pb-3">
                  <Button size="lg" onClick={onAdd} loading={b.status === "adding"} disabled={n === 0}>
                    {n === 0 ? "Select items to add" : `Add ${n} item${n === 1 ? "" : "s"} to closet`}
                  </Button>
                </div>
              )}
            </>
          )}
        </>
      )}
    </Card>
  );
}

function DetectedCard({
  item,
  index,
  included,
  draft,
  onToggle,
  onDraft,
  onHover,
}: {
  item: DetectedItem;
  index: number;
  included: boolean;
  draft: Attributes;
  onToggle: () => void;
  onDraft: (a: Attributes) => void;
  onHover: (on: boolean) => void;
}) {
  const [editing, setEditing] = useState(false);
  return (
    <div
      className={cn(
        "rounded-2xl border transition",
        included ? "border-black/10 bg-white" : "border-black/5 bg-black/[0.02] opacity-60",
      )}
      onMouseEnter={() => onHover(true)}
      onMouseLeave={() => onHover(false)}
    >
      <div className="flex items-center gap-3 p-2.5">
        <button
          type="button"
          onClick={onToggle}
          className={cn(
            "grid place-items-center size-6 rounded-lg border-2 shrink-0 transition",
            included ? "bg-ink border-ink text-white" : "border-black/25 bg-white",
          )}
          aria-label={included ? "Exclude" : "Include"}
        >
          {included && <Check className="size-4" strokeWidth={3} />}
        </button>
        <div className="relative">
          <ItemImage src={item.cutout_url} className="size-16 rounded-xl border border-black/5" pad={false} />
          <span
            className="absolute -top-1.5 -left-1.5 grid place-items-center size-5 rounded-full text-[10px] font-bold text-white ring-2 ring-white"
            style={{ background: boxColor(index) }}
          >
            {index + 1}
          </span>
        </div>
        <div className="flex-1 min-w-0" onClick={() => setEditing((e) => !e)}>
          <AttributeSummary a={draft} />
          <div className="flex items-center gap-2 mt-1.5">
            <span className="rounded-full bg-black/5 px-2 py-0.5 text-[11px] font-medium text-black/60">
              {categoryLabel(draft.category)}
            </span>
            <FormalityDots value={draft.formality} />
          </div>
        </div>
        <button
          type="button"
          onClick={() => setEditing((e) => !e)}
          className="inline-flex items-center gap-0.5 text-xs font-semibold text-black/60 px-2 h-8 rounded-full hover:bg-black/5"
        >
          Edit {editing ? <ChevronUp className="size-3.5" /> : <ChevronDown className="size-3.5" />}
        </button>
      </div>
      {editing && (
        <div className="border-t border-black/5 p-3">
          <AttributeEditor value={draft} onChange={onDraft} />
        </div>
      )}
    </div>
  );
}
