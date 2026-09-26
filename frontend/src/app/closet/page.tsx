"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Plus, RefreshCw, Shirt, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import type { Attributes, Item } from "@/lib/types";
import { CATEGORIES, categoryKey, categoryLabel } from "@/lib/constants";
import { money, titleCase } from "@/lib/format";
import { Button, Chip, EmptyState, ErrorBanner, ItemImage, PageTitle, Sheet, ColorDot } from "@/components/ui";
import { AttributeEditor, diffAttributes } from "@/components/AttributeEditor";
import { useToast } from "@/components/Toast";

export default function ClosetPage() {
  const [items, setItems] = useState<Item[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>("all");
  const [open, setOpen] = useState<Item | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setItems(await api.listCloset());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const it of items ?? []) {
      const k = categoryKey(it.category ?? it.attributes?.category);
      c[k] = (c[k] ?? 0) + 1;
    }
    return c;
  }, [items]);

  const shown = useMemo(
    () =>
      (items ?? []).filter((it) => filter === "all" || categoryKey(it.category ?? it.attributes?.category) === filter),
    [items, filter],
  );

  return (
    <div>
      <PageTitle
        title="Your closet"
        subtitle={items ? `${items.length} item${items.length === 1 ? "" : "s"}` : "Loading…"}
        right={
          <div className="flex gap-2">
            <button
              onClick={load}
              className="grid place-items-center size-10 rounded-full bg-white border border-black/10"
              aria-label="Refresh"
            >
              <RefreshCw className={`size-4 ${loading ? "animate-spin" : ""}`} />
            </button>
            <Link
              href="/add"
              className="inline-flex items-center gap-1.5 h-10 px-4 rounded-full bg-ink text-white text-sm font-semibold"
            >
              <Plus className="size-4" /> Add
            </Link>
          </div>
        }
      />

      <div className="-mx-4 px-4 flex gap-2 overflow-x-auto no-scrollbar pb-3">
        <Chip active={filter === "all"} onClick={() => setFilter("all")} count={items?.length ?? 0}>
          All
        </Chip>
        {CATEGORIES.map((c) => (
          <Chip key={c.key} active={filter === c.key} onClick={() => setFilter(c.key)} count={counts[c.key] ?? 0}>
            {c.label}
          </Chip>
        ))}
        {(counts.other ?? 0) > 0 && (
          <Chip active={filter === "other"} onClick={() => setFilter("other")} count={counts.other}>
            Other
          </Chip>
        )}
      </div>

      {error && <ErrorBanner message={error} onRetry={load} />}

      {items === null && !error ? (
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 mt-2">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="aspect-[4/5] rounded-3xl skeleton" />
          ))}
        </div>
      ) : items && items.length === 0 ? (
        <EmptyState
          icon={<Shirt className="size-7" />}
          title="Your closet is empty"
          body="Lay a few clothes on your bed or floor and snap a photo — we'll cut out and tag each piece."
          action={
            <Link href="/add" className="inline-flex items-center gap-2 h-11 px-5 rounded-full bg-ink text-white font-semibold">
              <Plus className="size-4" /> Add clothes
            </Link>
          }
        />
      ) : shown.length === 0 && items ? (
        <p className="text-center text-sm text-black/50 py-12">No {categoryLabel(filter, false).toLowerCase()} yet.</p>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 mt-1">
          {shown.map((it) => (
            <ItemTile key={it.id} item={it} onClick={() => setOpen(it)} />
          ))}
        </div>
      )}

      <ItemSheet
        item={open}
        onClose={() => setOpen(null)}
        onSaved={(u) => {
          setItems((xs) => xs?.map((x) => (x.id === u.id ? u : x)) ?? null);
          setOpen(null);
        }}
        onDeleted={(id) => {
          setItems((xs) => xs?.filter((x) => x.id !== id) ?? null);
          setOpen(null);
        }}
      />
    </div>
  );
}

function ItemTile({ item, onClick }: { item: Item; onClick: () => void }) {
  const a = item.attributes ?? {};
  return (
    <button onClick={onClick} className="group text-left rounded-3xl bg-white border border-black/5 overflow-hidden active:scale-[0.98] transition">
      <ItemImage src={item.image_url || item.cutout_url} alt={a.description ?? ""} className="aspect-square" />
      <div className="px-3 pb-3 pt-1">
        <div className="flex items-center gap-1.5 text-sm font-semibold leading-tight">
          <ColorDot color={a.primary_color} />
          <span className="truncate">{titleCase(a.subcategory || categoryLabel(item.category))}</span>
        </div>
        <div className="text-xs text-black/45 mt-0.5 truncate">
          {titleCase(a.primary_color ?? "")}
          {a.price ? ` · ${money(a.price, a.currency ?? "USD")}` : ""}
        </div>
      </div>
    </button>
  );
}

function ItemSheet({
  item,
  onClose,
  onSaved,
  onDeleted,
}: {
  item: Item | null;
  onClose: () => void;
  onSaved: (i: Item) => void;
  onDeleted: (id: string) => void;
}) {
  const toast = useToast();
  const [draft, setDraft] = useState<Attributes>({});
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [confirm, setConfirm] = useState(false);

  useEffect(() => {
    if (item) {
      setDraft({ ...item.attributes, category: item.attributes?.category ?? item.category });
      setConfirm(false);
    }
  }, [item]);

  if (!item) return null;
  const changes = diffAttributes({ ...item.attributes, category: item.attributes?.category ?? item.category }, draft);
  const dirty = Object.keys(changes).length > 0;

  const save = async () => {
    setSaving(true);
    try {
      const updated = await api.updateItem(item.id, changes);
      onSaved(updated?.id ? updated : { ...item, attributes: { ...item.attributes, ...changes } });
      toast("Saved");
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setSaving(false);
    }
  };

  const del = async () => {
    if (!confirm) {
      setConfirm(true);
      return;
    }
    setDeleting(true);
    try {
      await api.deleteItem(item.id);
      onDeleted(item.id);
      toast("Removed from closet");
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setDeleting(false);
    }
  };

  return (
    <Sheet
      open={!!item}
      onClose={onClose}
      title={titleCase(item.attributes?.subcategory || categoryLabel(item.category))}
      footer={
        <div className="flex gap-2">
          <Button variant="danger" onClick={del} loading={deleting} className="shrink-0">
            <Trash2 className="size-4" />
            {confirm ? "Tap to confirm" : "Delete"}
          </Button>
          <Button onClick={save} loading={saving} disabled={!dirty} className="flex-1">
            {dirty ? "Save changes" : "No changes"}
          </Button>
        </div>
      }
    >
      <div className="rounded-3xl border border-black/5 overflow-hidden mb-4">
        <ItemImage src={item.image_url || item.cutout_url} className="aspect-[4/3]" />
      </div>
      {item.attributes?.description && (
        <p className="text-sm text-black/60 mb-4">{String(item.attributes.description)}</p>
      )}
      <AttributeEditor value={draft} onChange={setDraft} />
    </Sheet>
  );
}
