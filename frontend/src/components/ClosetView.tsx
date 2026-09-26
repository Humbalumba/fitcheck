"use client";

import { Fragment, useMemo, useSyncExternalStore } from "react";
import Link from "next/link";
import { Plus, Shirt, Sparkles } from "lucide-react";
import type { Item } from "@/lib/types";
import { CATEGORIES, categoryKey, categoryLabel } from "@/lib/constants";
import { estMoney, money, titleCase } from "@/lib/format";
import { Chip, ColorDot, EmptyState, ErrorBanner, ItemImage, PageTitle, Skeleton } from "@/components/ui";

/**
 * The closet page body: title + "Add clothes", category chips, then the clothes hanging on rack rods.
 * Purely presentational so the landing intro can render the exact same thing inside the wardrobe.
 */
export function ClosetContent({
  items,
  error = null,
  onRetry,
  filter = "all",
  onFilter,
  onOpen,
}: {
  items: Item[] | null;
  error?: string | null;
  onRetry?: () => void;
  filter?: string;
  onFilter?: (key: string) => void;
  onOpen?: (item: Item) => void;
}) {
  const cols = useColumns();
  const setFilter = (k: string) => onFilter?.(k);

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
    <>
      <PageTitle
        title="Your closet"
        subtitle={items ? `${items.length} item${items.length === 1 ? "" : "s"}` : "Loading…"}
        right={
          <Link
            href="/add"
            className="inline-flex items-center gap-1.5 h-10 pl-3.5 pr-4 rounded-full bg-ink text-white text-sm font-semibold shadow-[0_6px_14px_-6px_rgba(0,0,0,0.5)] active:scale-[0.97] transition"
            data-testid="add-clothes"
          >
            <Plus className="size-4" strokeWidth={2.5} /> Add clothes
          </Link>
        }
      />

      <div className="-mx-4 px-4 lg:mx-0 lg:px-0 lg:flex-wrap flex gap-2 overflow-x-auto no-scrollbar pb-3">
        <Chip active={filter === "all"} onClick={() => setFilter("all")} count={items?.length}>
          All
        </Chip>
        {CATEGORIES.map((c) => (
          <Chip key={c.key} active={filter === c.key} onClick={() => setFilter(c.key)} count={items ? (counts[c.key] ?? 0) : undefined}>
            {c.label}
          </Chip>
        ))}
        {(counts.other ?? 0) > 0 && (
          <Chip active={filter === "other"} onClick={() => setFilter("other")} count={counts.other}>
            Other
          </Chip>
        )}
      </div>

      {error && <ErrorBanner message={error} onRetry={onRetry} />}

      {items === null && !error ? (
        <Racks cols={cols} count={cols * 2} aria-busy>
          {() => <TileSkeleton />}
        </Racks>
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
        <Racks cols={cols} count={shown.length} keyOf={(i) => shown[i].id}>
          {(i) => <ItemTile item={shown[i]} onClick={() => onOpen?.(shown[i])} />}
        </Racks>
      )}
    </>
  );
}

/* ------------------------------ Racks ------------------------------ */

// Grid columns per breakpoint (widest first): laptop 5, tablet 4, large phone / small tablet 3, phone 2.
const COLUMN_QUERIES: [string, number][] = [
  ["(min-width: 1024px)", 5],
  ["(min-width: 768px)", 4],
  ["(min-width: 640px)", 3],
];
function subscribeColumns(cb: () => void) {
  const ms = COLUMN_QUERIES.map(([q]) => window.matchMedia(q));
  ms.forEach((m) => m.addEventListener("change", cb));
  return () => ms.forEach((m) => m.removeEventListener("change", cb));
}
function columnsNow() {
  return COLUMN_QUERIES.find(([q]) => window.matchMedia(q).matches)?.[1] ?? 2;
}
/** Grid column count for the racks; the server snapshot is 2 so hydration always matches. */
export function useColumns() {
  return useSyncExternalStore(subscribeColumns, columnsNow, () => 2);
}

/** Clothes hang in rows; every row gets a wooden rod running behind the cards. */
function Racks({
  cols,
  count,
  children,
  keyOf = String,
  "aria-busy": busy,
}: {
  cols: number;
  count: number;
  children: (index: number) => React.ReactNode;
  keyOf?: (index: number) => string;
  "aria-busy"?: boolean;
}) {
  const rows: number[][] = [];
  for (let i = 0; i < count; i += cols) rows.push(Array.from({ length: Math.min(cols, count - i) }, (_, k) => i + k));
  return (
    <div className="space-y-6 lg:space-y-8 mt-2 lg:mt-4 pb-2" aria-busy={busy || undefined} data-testid="closet-racks">
      {rows.map((row, r) => (
        <div key={r} className="relative" data-testid="rack-row">
          <div className="rack-rod top-[19%]" aria-hidden />
          <div className="relative grid gap-3.5 lg:gap-5" style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
            {row.map((i) => (
              <Fragment key={keyOf(i)}>{children(i)}</Fragment>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

const TILE =
  "rounded-3xl bg-card border border-black/[0.06] overflow-hidden shadow-[0_10px_18px_-12px_rgba(74,47,24,0.45),0_1px_2px_rgba(74,47,24,0.06)]";

function TileSkeleton() {
  return (
    <div className={TILE}>
      <div className="p-1.5 pb-0">
        <Skeleton className="aspect-square rounded-[20px]" />
      </div>
      <div className="px-3 pb-3 pt-2.5 space-y-2">
        <Skeleton className="h-3.5 w-3/5 rounded-full" />
        <Skeleton className="h-3 w-2/5 rounded-full" soft />
      </div>
    </div>
  );
}

function ItemTile({ item, onClick }: { item: Item; onClick: () => void }) {
  const a = item.attributes ?? {};
  const pending = item.render_status === "pending";
  return (
    <button
      onClick={onClick}
      className={`group text-left ${TILE} transition duration-200 hover:-translate-y-0.5 hover:shadow-[0_16px_24px_-14px_rgba(74,47,24,0.5)] active:scale-[0.98]`}
    >
      <div className="relative p-1.5 pb-0">
        <ItemImage
          src={item.clean_image_url || item.image_url || item.cutout_url}
          alt={a.description ?? ""}
          className="aspect-square rounded-[20px]"
          tone="bg-sand"
          blend
          pad={!item.clean_image_url}
        />
        {pending && (
          <>
            <span className="working-sweep rounded-[20px] m-1.5 mb-0" aria-hidden />
            <span className="absolute top-3.5 left-3.5 inline-flex items-center gap-1 rounded-full bg-white/90 border border-black/5 px-2 py-0.5 text-[10px] font-semibold text-black/60">
              <Sparkles className="size-3 animate-pulse" /> Polishing…
            </span>
          </>
        )}
      </div>
      <div className="px-3 pb-3 pt-2">
        <div className="flex items-center gap-1.5 text-sm font-semibold leading-tight">
          <ColorDot color={a.primary_color} />
          <span className="truncate">{titleCase(a.subcategory || categoryLabel(item.category))}</span>
        </div>
        <div className="text-xs text-black/45 mt-0.5 truncate">
          {titleCase(a.primary_color ?? "")}
          {a.price
            ? ` · ${money(a.price, a.currency ?? "USD")}`
            : a.estimated_price_usd != null
              ? ` · ${estMoney(Number(a.estimated_price_usd))}`
              : ""}
        </div>
      </div>
    </button>
  );
}

