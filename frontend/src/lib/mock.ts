/* Mock backend used when NEXT_PUBLIC_MOCK=1. In-memory, resets on reload. */
import type {
  Attributes,
  DetectResponse,
  DetectedItem,
  EvaluateResponse,
  Health,
  Item,
  Outfit,
  Settings,
  SuggestionsResponse,
  Sustainability,
} from "./types";
import { categoryKey, colorToCss } from "./constants";

const SHAPES: Record<string, string> = {
  top: "M60 40 L85 30 Q100 46 115 30 L140 40 L172 72 L152 92 L140 82 L140 172 L60 172 L60 82 L48 92 L28 72 Z",
  bottom: "M64 28 L136 28 L148 176 L110 176 L100 82 L90 176 L52 176 Z",
  outerwear:
    "M60 34 L86 26 L100 70 L114 26 L140 34 L170 60 L176 160 L154 160 L148 86 L146 176 L54 176 L52 86 L46 160 L24 160 L30 60 Z",
  dress: "M80 24 L120 24 L117 62 L152 178 L48 178 L83 62 Z",
  shoes: "M24 128 L26 92 L66 98 Q96 114 150 118 Q178 122 178 142 L178 152 L24 152 Z",
  accessory: "M50 80 L150 80 L160 170 L40 170 Z M75 80 Q75 40 100 40 Q125 40 125 80",
};

export function garmentSvg(category: string, color = "gray", pattern = "solid"): string {
  const k = categoryKey(category);
  const shape = SHAPES[k] ?? SHAPES.top;
  let fill = colorToCss(color) ?? "#8a8a8a";
  if (fill.startsWith("conic")) fill = "#c86";
  const stroke = "#00000022";
  const stripes =
    pattern === "striped"
      ? `<defs><pattern id="p" width="12" height="12" patternUnits="userSpaceOnUse"><rect width="12" height="12" fill="${fill}"/><rect width="12" height="4" fill="#ffffff88"/></pattern></defs>`
      : pattern === "plaid" || pattern === "checked"
        ? `<defs><pattern id="p" width="20" height="20" patternUnits="userSpaceOnUse"><rect width="20" height="20" fill="${fill}"/><rect width="20" height="5" fill="#00000030"/><rect width="5" height="20" fill="#00000030"/></pattern></defs>`
        : "";
  const f = stripes ? "url(#p)" : fill;
  const extra =
    k === "accessory"
      ? `<path d="M75 80 Q75 40 100 40 Q125 40 125 80" fill="none" stroke="${fill}" stroke-width="8"/>`
      : k === "outerwear"
        ? `<line x1="100" y1="70" x2="100" y2="176" stroke="#00000033" stroke-width="2"/>`
        : "";
  const d = k === "accessory" ? "M50 80 L150 80 L160 170 L40 170 Z" : shape;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">${stripes}<path d="${d}" fill="${f}" stroke="${stroke}" stroke-width="2" stroke-linejoin="round"/>${extra}</svg>`;
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
}

let seq = 100;
const nid = (p: string) => `${p}_${(seq++).toString(36)}${Math.random().toString(36).slice(2, 6)}`;

function mk(category: string, sub: string, color: string, extra: Partial<Attributes> = {}): Item {
  const pattern = (extra.pattern as string) ?? "solid";
  const url = garmentSvg(category, color, pattern);
  return {
    id: nid("itm"),
    status: "closet",
    category,
    image_url: url,
    cutout_url: url,
    created_at: new Date().toISOString(),
    attributes: {
      category,
      subcategory: sub,
      primary_color: color,
      secondary_colors: [],
      pattern,
      fabric_guess: "cotton",
      formality: 2,
      seasons: ["spring", "fall"],
      style_tags: ["casual"],
      gender_presentation: "unisex",
      brand: null,
      price: null,
      currency: "USD",
      description: `${color} ${sub}`,
      ...extra,
    },
  };
}

const closet: Item[] = [
  mk("top", "t-shirt", "white", { style_tags: ["minimal", "casual"], price: 18, seasons: ["spring", "summer", "fall"] }),
  mk("top", "oxford shirt", "light blue", { formality: 3, fabric_guess: "cotton oxford", price: 45 }),
  mk("top", "sweater", "charcoal", { fabric_guess: "wool", seasons: ["fall", "winter"], price: 60 }),
  mk("top", "t-shirt", "black", { price: 20 }),
  mk("top", "flannel shirt", "red", { pattern: "plaid", fabric_guess: "flannel", seasons: ["fall", "winter"] }),
  mk("bottom", "jeans", "denim", { fabric_guess: "denim", price: 70 }),
  mk("bottom", "chinos", "khaki", { formality: 3, price: 50 }),
  mk("bottom", "trousers", "black", { formality: 4, fabric_guess: "wool blend" }),
  mk("bottom", "midi skirt", "olive", { formality: 3, style_tags: ["classic"] }),
  mk("outerwear", "denim jacket", "denim", { fabric_guess: "denim", price: 80 }),
  mk("outerwear", "overcoat", "camel", { formality: 4, fabric_guess: "wool", seasons: ["fall", "winter"] }),
  mk("dress", "slip dress", "black", { formality: 4, fabric_guess: "satin", seasons: ["spring", "summer"] }),
  mk("shoes", "sneakers", "white", { price: 90 }),
  mk("shoes", "boots", "brown", { formality: 3, fabric_guess: "leather" }),
  mk("accessory", "tote bag", "tan", { fabric_guess: "canvas" }),
];

const pending = new Map<string, Item>(); // detected, not yet in closet

let settings: Settings = {
  compat_threshold: 0.5,
  redundancy_similar_threshold: 0.82,
  redundancy_duplicate_threshold: 0.9,
  style_goal: "Minimal, versatile pieces that mix and match",
  occasions: ["work", "casual"],
};

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x));

const DETECT_POOL: Array<[string, string, string, Partial<Attributes>]> = [
  ["top", "hoodie", "grey", { fabric_guess: "fleece", style_tags: ["streetwear"] }],
  ["bottom", "cargo pants", "olive", { fabric_guess: "cotton twill" }],
  ["outerwear", "bomber jacket", "navy", { fabric_guess: "nylon" }],
  ["top", "striped tee", "navy", { pattern: "striped" }],
  ["shoes", "loafers", "burgundy", { formality: 4, fabric_guess: "leather" }],
  ["dress", "shirt dress", "sage", { formality: 3 }],
  ["top", "knit polo", "cream", { formality: 3, fabric_guess: "knit" }],
];

const BOXES: Array<[number, number, number, number]> = [
  [80, 60, 520, 460],
  [120, 540, 640, 940],
  [560, 100, 940, 520],
  [600, 580, 930, 900],
];

function hash(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return ((h >>> 0) % 1000) / 1000;
}

export const mockApi = {
  async health(): Promise<Health> {
    await sleep(100);
    return { ok: true, gemini: false, scorer: "stub", mock: true };
  },

  async detect(file: Blob, purpose: "closet" | "candidate"): Promise<DetectResponse> {
    await sleep(1800);
    const n = purpose === "candidate" ? 1 + Math.floor(Math.random() * 2) : 2 + Math.floor(Math.random() * 3);
    const start = Math.floor(Math.random() * DETECT_POOL.length);
    const items: DetectedItem[] = [];
    for (let i = 0; i < n; i++) {
      const [cat, sub, color, extra] = DETECT_POOL[(start + i) % DETECT_POOL.length];
      const it = mk(cat, sub, color, {
        ...extra,
        price: purpose === "candidate" && i === 0 ? 49.99 : null,
      });
      it.status = purpose === "candidate" ? "candidate" : "detected";
      pending.set(it.id, it);
      items.push({
        id: it.id,
        bbox: BOXES[i % BOXES.length],
        label: `${color} ${sub}`,
        cutout_url: it.cutout_url,
        crop_url: it.cutout_url,
        attributes: clone(it.attributes),
      });
    }
    return { photo_id: nid("ph"), image_url: URL.createObjectURL(file), items };
  },

  async addToCloset(itemIds: string[], overrides?: Record<string, Partial<Attributes>>): Promise<Item[]> {
    await sleep(400);
    const out: Item[] = [];
    for (const id of itemIds) {
      const it = pending.get(id);
      if (!it) continue;
      pending.delete(id);
      const o = overrides?.[id] ?? {};
      it.attributes = { ...it.attributes, ...o };
      it.category = String(it.attributes.category ?? it.category);
      it.status = "closet";
      closet.unshift(it);
      out.push(clone(it));
    }
    return out;
  },

  async listCloset(category?: string): Promise<Item[]> {
    await sleep(250);
    return clone(category ? closet.filter((i) => categoryKey(i.category) === categoryKey(category)) : closet);
  },

  async updateItem(id: string, attributes: Partial<Attributes>): Promise<Item> {
    await sleep(250);
    const it = closet.find((i) => i.id === id) ?? pending.get(id);
    if (!it) throw new Error("Item not found");
    it.attributes = { ...it.attributes, ...attributes };
    if (attributes.category) it.category = String(attributes.category);
    return clone(it);
  },

  async getItem(id: string): Promise<Item> {
    const it = closet.find((x) => x.id === id);
    if (!it) throw new Error("not found");
    return it;
  },

  async renderItem(id: string): Promise<Item> {
    return this.getItem(id);
  },

  async deleteItem(id: string) {
    await sleep(200);
    const i = closet.findIndex((x) => x.id === id);
    if (i >= 0) closet.splice(i, 1);
    return { ok: true };
  },

  async evaluate(itemId: string, price?: number | null): Promise<EvaluateResponse> {
    await sleep(1500);
    const cand = pending.get(itemId) ?? closet.find((i) => i.id === itemId);
    if (!cand) throw new Error("Item not found");
    const p = price ?? (cand.attributes.price as number) ?? 40;
    const k = categoryKey(cand.category);
    const others = closet.filter((i) => i.id !== cand.id);
    const by = (c: string) => others.filter((i) => categoryKey(i.category) === c);
    const outfits: Outfit[] = [];
    let tried = 0; // max possible outfits for this category (every base look tried)
    const push = (template: string, items: Item[]) => {
      tried++;
      const score = 0.3 + 0.7 * hash(items.map((i) => i.id).join("|"));
      if (score >= settings.compat_threshold) outfits.push({ template, items, score });
    };
    if (k === "top") {
      for (const b of by("bottom")) {
        push("top+bottom", [cand, b]);
        for (const o of by("outerwear")) push("top+bottom+outerwear", [cand, b, o]);
      }
    } else if (k === "bottom") {
      for (const t of by("top")) {
        push("top+bottom", [t, cand]);
        for (const o of by("outerwear")) push("top+bottom+outerwear", [t, cand, o]);
      }
    } else if (k === "outerwear") {
      for (const t of by("top")) for (const b of by("bottom")) push("top+bottom+outerwear", [t, b, cand]);
      for (const d of by("dress")) push("dress+outerwear", [d, cand]);
    } else if (k === "dress") {
      push("dress", [cand]);
      for (const o of by("outerwear")) push("dress+outerwear", [cand, o]);
    } else {
      for (const t of by("top")) for (const b of by("bottom")) push("top+bottom", [t, b, cand]);
    }
    outfits.sort((a, b) => b.score - a.score);
    const countBy: Record<string, number> = {};
    for (const o of outfits) countBy[o.template] = (countBy[o.template] ?? 0) + 1;
    const matches = others
      .filter((i) => categoryKey(i.category) === k)
      .map((i) => ({
        item: i,
        similarity:
          i.attributes.primary_color === cand.attributes.primary_color
            ? 0.94
            : 0.55 + 0.3 * hash(i.id + cand.id),
      }))
      .filter((m) => m.similarity >= 0.6)
      .sort((a, b) => b.similarity - a.similarity)
      .slice(0, 3);
    const top = matches[0]?.similarity ?? 0;
    const level =
      top >= settings.redundancy_duplicate_threshold
        ? "near_duplicate"
        : top >= settings.redundancy_similar_threshold
          ? "similar"
          : "none";
    // Simplified mirror of backend/app/verdict.py (mock mode only)
    const weighted = outfits.reduce((s, o) => s + o.score, 0);
    const n = outfits.length;
    const m = Math.max(tried, 2);
    const vers = n ? 0.5 * Math.min(1, Math.log1p(n) / Math.log1p(m)) + 0.5 * Math.min(1, Math.log1p(n) / Math.log1p(Math.min(8, m))) : 0;
    const best = outfits.slice(0, 5);
    const quality = best.length ? best.reduce((s, o) => s + o.score, 0) / best.length : 0;
    const wears = 70 * Math.min(2, 0.5 + 0.25 * Math.log2(1 + n)) * (level === "near_duplicate" ? 0.5 : level === "similar" ? 0.75 : 1);
    const cpw = p / wears;
    const bar = 0.75;
    const cost = Math.max(-0.5, Math.min(1, 0.6 + 0.3 * Math.log2(bar / Math.max(cpw, 1e-6))));
    const score = Math.max(0, Math.min(100, Math.round(100 * (0.4 * vers + 0.2 * quality + 0.4 * cost) + (level === "similar" ? -5 : 0))));
    const decision = level === "near_duplicate" || n === 0 ? "SKIP" : score >= 65 ? "BUY" : score >= 45 ? "CONSIDER" : "SKIP";
    const reasons: string[] = [];
    if (level === "near_duplicate") reasons.push("You already own something almost identical");
    reasons.push(n ? `Makes ${n} of the ${tried} outfits a ${k} can make with your closet` : "Doesn't go with anything in your closet yet");
    reasons.push(`About $${cpw.toFixed(2)} per wear, ${cpw <= bar ? "below" : "above"} a typical $${bar.toFixed(2)}`);
    cand.attributes.price = p;
    return {
      item: clone(cand),
      template_names: Object.keys(countBy),
      redundancy: { level, matches: clone(matches) },
      outfits: clone(outfits.slice(0, 40)),
      outfit_count_by_template: countBy,
      total_new_outfits: n,
      value: {
        price: p,
        currency: "USD",
        cost_per_wear: Math.round(cpw * 100) / 100,
        expected_wears: Math.round(wears),
        price_bar: bar,
        price_bar_source: "default",
        versatility: { new_outfits: n, max_possible: tried, share: tried ? n / tried : null },
        outfit_quality: quality,
        weighted_outfits: weighted,
        value_score: score,
      },
      verdict: {
        decision,
        score,
        reasons: reasons.slice(0, 2),
        components: {
          versatility: { score: Math.round(100 * vers), weight: 0.4 },
          outfit_quality: { score: Math.round(100 * quality), weight: 0.2 },
          cost: { score: Math.round(100 * cost), weight: 0.4 },
          gap_fill: { points: 0 },
          similarity: { points: level === "similar" ? -5 : 0 },
        },
        bands: { BUY: 65, CONSIDER: 45 },
      },
      evaluation_id: nid("ev"),
    };
  },

  async candidateToCloset(itemId: string): Promise<Item> {
    await sleep(300);
    const it = pending.get(itemId);
    if (!it) {
      const c = closet.find((i) => i.id === itemId);
      if (c) return clone(c);
      throw new Error("Item not found");
    }
    pending.delete(itemId);
    it.status = "closet";
    closet.unshift(it);
    return clone(it);
  },

  async suggestions(evaluationId: string): Promise<SuggestionsResponse> {
    await sleep(800);
    return {
      evaluation_id: evaluationId,
      mode: "none",
      title: "Suggestions",
      candidate_verdict: "",
      suggestions: [],
      message: "Shopping suggestions need the real backend (live product search).",
    };
  },

  async itemSustainability(itemId: string): Promise<Sustainability & { evaluated_at?: string }> {
    await sleep(100);
    throw new Error(`No sustainability estimate for ${itemId} in mock mode`);
  },

  async getSettings(): Promise<Settings> {
    await sleep(150);
    return clone(settings);
  },

  async putSettings(patch: Partial<Settings>): Promise<Settings> {
    await sleep(250);
    settings = { ...settings, ...patch };
    return clone(settings);
  },
};
