export const CATEGORIES = [
  { key: "top", label: "Tops", singular: "Top" },
  { key: "bottom", label: "Bottoms", singular: "Bottom" },
  { key: "outerwear", label: "Outerwear", singular: "Outerwear" },
  { key: "dress", label: "Dresses", singular: "Dress" },
  { key: "shoes", label: "Shoes", singular: "Shoes" },
  { key: "accessory", label: "Accessories", singular: "Accessory" },
] as const;

export type CategoryKey = (typeof CATEGORIES)[number]["key"] | "other";

/** Normalise whatever the backend sends ("tops", "Top", "outer", "footwear"...) */
export function categoryKey(c: unknown): CategoryKey {
  const s = String(c ?? "").toLowerCase().trim();
  if (!s) return "other";
  if (s.startsWith("top") || ["shirt", "t-shirt", "tee", "blouse", "sweater", "knit"].includes(s)) return "top";
  if (s.startsWith("bottom") || ["pants", "jeans", "skirt", "shorts", "trousers"].includes(s)) return "bottom";
  if (s.startsWith("outer") || ["jacket", "coat", "blazer"].includes(s)) return "outerwear";
  if (s.startsWith("dress") || s === "jumpsuit" || s === "one-piece" || s === "onepiece") return "dress";
  if (s.startsWith("shoe") || s === "footwear" || s === "sneakers" || s === "boots") return "shoes";
  if (s.startsWith("access") || ["bag", "hat", "jewelry", "belt", "scarf"].includes(s)) return "accessory";
  return "other";
}

export function categoryLabel(c: unknown, singular = true): string {
  const k = categoryKey(c);
  const found = CATEGORIES.find((x) => x.key === k);
  if (!found) return String(c ?? "Other") || "Other";
  return singular ? found.singular : found.label;
}

export const SEASONS = ["spring", "summer", "fall", "winter"] as const;

export const PATTERNS = [
  "solid",
  "striped",
  "plaid",
  "checked",
  "floral",
  "graphic",
  "polka dot",
  "animal",
  "camo",
  "abstract",
  "colorblock",
  "other",
];

export const FORMALITY_LABELS = ["", "Very casual", "Casual", "Smart casual", "Business", "Formal"];

export const OCCASION_SUGGESTIONS = [
  "work",
  "casual",
  "date night",
  "gym",
  "interviews",
  "weddings",
  "travel",
  "parties",
  "outdoors",
  "school",
];

export const TEMPLATE_LABELS: Record<string, string> = {
  "top+bottom": "Top + Bottom",
  "top+bottom+outerwear": "Top + Bottom + Outerwear",
  dress: "Dress",
  "dress+outerwear": "Dress + Outerwear",
};

export function templateLabel(t: string): string {
  if (TEMPLATE_LABELS[t]) return TEMPLATE_LABELS[t];
  return t
    .split("+")
    .map((p) => p.charAt(0).toUpperCase() + p.slice(1))
    .join(" + ");
}

/** Best-effort CSS color from a free-text color name like "navy blue" */
const COLOR_MAP: Record<string, string> = {
  black: "#111111",
  white: "#fafafa",
  "off-white": "#f4f1ea",
  cream: "#f3ead3",
  ivory: "#f6f1e1",
  beige: "#d9c7a7",
  tan: "#c8a27a",
  camel: "#b98b52",
  khaki: "#b5a57a",
  brown: "#6f4a2f",
  chocolate: "#4b2e1e",
  grey: "#8a8a8a",
  gray: "#8a8a8a",
  charcoal: "#3a3a3a",
  silver: "#c0c0c0",
  navy: "#1f2a4d",
  blue: "#2f5fd0",
  "light blue": "#9cc3ec",
  denim: "#4a6a94",
  teal: "#197a7a",
  turquoise: "#3cc6c0",
  green: "#2f8a4a",
  olive: "#6b6b2f",
  sage: "#a3b18a",
  mint: "#aee6c9",
  "forest green": "#1f4d2e",
  yellow: "#f2cc2f",
  mustard: "#caa12a",
  gold: "#c9a227",
  orange: "#e5762a",
  rust: "#a4481f",
  red: "#c62f2f",
  burgundy: "#6d1f2c",
  maroon: "#5e1a22",
  wine: "#6a1f33",
  pink: "#ee9bb8",
  "hot pink": "#e0417f",
  blush: "#f0c4c4",
  purple: "#6b3fa0",
  lavender: "#b9a6e0",
  lilac: "#c8a2c8",
  multicolor: "conic-gradient(#e33,#fc3,#3c6,#39f,#c3f,#e33)",
  multi: "conic-gradient(#e33,#fc3,#3c6,#39f,#c3f,#e33)",
};

export function colorToCss(name?: string | null): string | null {
  if (!name) return null;
  const s = name.toLowerCase().trim();
  if (s.startsWith("#")) return s;
  if (COLOR_MAP[s]) return COLOR_MAP[s];
  // try last word ("dark navy" -> navy), then any word
  const words = s.split(/[\s/-]+/);
  for (let i = words.length - 1; i >= 0; i--) {
    if (COLOR_MAP[words[i]]) return COLOR_MAP[words[i]];
  }
  return null;
}
