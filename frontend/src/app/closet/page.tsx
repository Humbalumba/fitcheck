"use client";

import { useCallback, useEffect, useState } from "react";
import { Sparkles, Trash2, Wand2 } from "lucide-react";
import { api } from "@/lib/api";
import type { Attributes, Item } from "@/lib/types";
import { categoryLabel } from "@/lib/constants";
import { estMoney, titleCase } from "@/lib/format";
import { cameFromIntro, getCachedCloset, setCachedCloset } from "@/lib/closetCache";
import { Button, ItemImage, Sheet } from "@/components/ui";
import { ClosetContent } from "@/components/ClosetView";
import { AttributeEditor, diffAttributes } from "@/components/AttributeEditor";
import { ItemSustainability } from "@/components/Sustainability";
import { useToast } from "@/components/Toast";

export default function ClosetPage() {
  // Start from the items the landing intro (or an earlier visit) already fetched; refresh in the background.
  const [items, setItems] = useState<Item[] | null>(getCachedCloset);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>("all");
  const [open, setOpen] = useState<Item | null>(null);
  // Right after the intro's zoom the page is already on screen (inside the wardrobe), so don't animate it in again.
  const [seamless] = useState(cameFromIntro);

  const load = useCallback(async () => {
    setError(null);
    try {
      setItems(await api.listCloset());
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // keep the shared cache in step with edits / deletes / polling
  useEffect(() => {
    if (items) setCachedCloset(items);
  }, [items]);

  // Clean product images render in the background after an item is added: poll while any is pending.
  const anyPending = (items ?? []).some((it) => it.render_status === "pending");
  useEffect(() => {
    if (!anyPending) return;
    const t = setInterval(async () => {
      try {
        const fresh = await api.listCloset();
        setItems(fresh);
        setOpen((o) => (o ? (fresh.find((x) => x.id === o.id) ?? o) : o));
      } catch {
        /* keep polling */
      }
    }, 3000);
    return () => clearInterval(t);
  }, [anyPending]);

  const replaceItem = useCallback((u: Item) => {
    setItems((xs) => xs?.map((x) => (x.id === u.id ? u : x)) ?? null);
    setOpen((o) => (o && o.id === u.id ? u : o));
  }, []);

  return (
    <div className={seamless ? undefined : "arrive"}>
      <ClosetContent items={items} error={error} onRetry={load} filter={filter} onFilter={setFilter} onOpen={setOpen} />

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
        onChanged={replaceItem}
      />
    </div>
  );
}

function ItemSheet({
  item,
  onClose,
  onSaved,
  onDeleted,
  onChanged,
}: {
  item: Item | null;
  onClose: () => void;
  onSaved: (i: Item) => void;
  onDeleted: (id: string) => void;
  onChanged: (i: Item) => void;
}) {
  const toast = useToast();
  const [draft, setDraft] = useState<Attributes>({});
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [showOriginal, setShowOriginal] = useState(false);
  const [rendering, setRendering] = useState(false);

  // reset only when a different item opens (background polling refreshes the same item's image fields)
  useEffect(() => {
    if (item) {
      setDraft({ ...item.attributes, category: item.attributes?.category ?? item.category });
      setConfirm(false);
      setShowOriginal(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item?.id]);

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

  const rerender = async () => {
    setRendering(true);
    try {
      onChanged(await api.renderItem(item.id));
      setShowOriginal(false);
      toast("Re-rendering the product image…");
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setRendering(false);
    }
  };

  const pending = item.render_status === "pending";
  const originalSrc = item.crop_url || item.original_image_url || item.cutout_url;
  const mainSrc = showOriginal ? originalSrc : item.clean_image_url || item.image_url || item.cutout_url;
  const methodLabel =
    item.clean_method === "gemini"
      ? "AI product render · verified"
      : item.clean_method === "template"
        ? "Brand-style render · your colours & logos"
        : item.clean_method === "cleanup"
          ? "Auto-cleaned photo"
          : item.render_status === "failed"
            ? "Couldn't polish this photo"
            : null;

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
      <div className="relative rounded-3xl border border-black/5 overflow-hidden mb-2">
        <ItemImage src={mainSrc} className="aspect-[4/3]" tone="bg-sand" blend pad={showOriginal || !item.clean_image_url} />
        {pending && <span className="working-sweep" aria-hidden />}
        {pending && (
          <span className="absolute top-3 left-3 inline-flex items-center gap-1 rounded-full bg-white/90 border border-black/5 px-2.5 py-1 text-xs font-semibold text-black/60">
            <Sparkles className="size-3.5 animate-pulse" /> Polishing product image…
          </span>
        )}
      </div>
      <div className="flex items-center gap-2 mb-4 text-xs">
        <div className="inline-flex rounded-full bg-black/5 p-0.5" role="group" aria-label="Image view">
          <button
            onClick={() => setShowOriginal(false)}
            className={`h-7 px-3 rounded-full font-semibold ${!showOriginal ? "bg-white shadow-sm" : "text-black/50"}`}
          >
            Clean
          </button>
          <button
            onClick={() => setShowOriginal(true)}
            className={`h-7 px-3 rounded-full font-semibold ${showOriginal ? "bg-white shadow-sm" : "text-black/50"}`}
          >
            View original
          </button>
        </div>
        <span className="flex-1 truncate text-black/45">{pending ? "Rendering…" : methodLabel}</span>
        <button
          onClick={rerender}
          disabled={rendering || pending}
          className="inline-flex items-center gap-1 h-7 px-3 rounded-full border border-black/10 bg-white font-semibold disabled:opacity-40"
        >
          <Wand2 className={`size-3.5 ${rendering || pending ? "animate-pulse" : ""}`} /> Re-render
        </button>
      </div>
      {item.attributes?.description && (
        <p className="text-sm text-black/60 mb-4">{String(item.attributes.description)}</p>
      )}
      <ItemSustainability key={item.id} itemId={item.id} />
      {item.attributes?.price == null && item.attributes?.estimated_price_usd != null && (
        <p className="text-xs text-black/50 mb-3" data-testid="closet-est-price">
          Price <b>{estMoney(Number(item.attributes.estimated_price_usd))}</b>, estimated from brand and type. Enter the
          real price below for more accurate verdicts.
        </p>
      )}
      <AttributeEditor value={draft} onChange={setDraft} />
    </Sheet>
  );
}
