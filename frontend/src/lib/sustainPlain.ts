/**
 * Plain-English wording for the sustainability estimate (display only).
 * Everything is derived deterministically from fields the backend already returns
 * (app/sustainability.py: score, materials, components, garment_type, ...). No new numbers are invented.
 */
import type { Sustainability } from "./types";

type Material = { material: string; share: number };
type Components = {
  base_wears?: number;
  utility_multiplier?: number;
  n_new_outfits?: number | null;
  redundancy_level?: string | null;
  relative_to_typical?: number;
};

export type Tone = "great" | "good" | "okay" | "meh" | "poor";

/** Friendly rating from the 0-100 score (50 = a typical item of the same type in an average fabric). */
export function rating(score: number): { label: string; tone: Tone } {
  if (score >= 75) return { label: "Great choice", tone: "great" };
  if (score >= 58) return { label: "Pretty good", tone: "good" };
  if (score >= 42) return { label: "Okay", tone: "okay" };
  if (score >= 25) return { label: "Not great", tone: "meh" };
  return { label: "Not great", tone: "poor" };
}

// weight: "heavy" = bigger footprint than an average fabric (carbon or water), "oil" = fossil-based synthetic,
// "light" = lower footprint than average (per the factors in app/data/sustainability_factors.json).
const MAT: Record<string, { name: string; long: string; short: string; weight: "heavy" | "oil" | "light" | "unknown" }> = {
  cotton: { name: "cotton", long: "takes a lot of water and energy to grow and make", short: "takes a lot of water to grow", weight: "heavy" },
  organic_cotton: {
    name: "organic cotton",
    long: "uses far less water than regular cotton, though it still takes energy to make",
    short: "uses less water than regular cotton",
    weight: "heavy",
  },
  polyester: { name: "polyester", long: "is made from oil and sheds tiny plastic fibres in the wash", short: "is made from oil", weight: "oil" },
  recycled_polyester: {
    name: "recycled polyester",
    long: "gives old plastic a second life, though it still sheds tiny plastic fibres in the wash",
    short: "reuses old plastic",
    weight: "light",
  },
  nylon: { name: "nylon", long: "is made from oil and sheds tiny plastic fibres in the wash", short: "is made from oil", weight: "oil" },
  acrylic: { name: "acrylic", long: "is made from oil and is one of the more energy-hungry fabrics to make", short: "is made from oil", weight: "heavy" },
  wool: { name: "wool", long: "has a big footprint because it comes from sheep farming", short: "comes from sheep farming", weight: "heavy" },
  viscose: { name: "viscose", long: "is made from wood pulp, but turning it into fabric takes a lot of energy and water", short: "takes a lot of processing", weight: "heavy" },
  linen: { name: "linen", long: "is one of the lighter-impact fabrics to make", short: "is fairly light on the planet", weight: "light" },
  silk: { name: "silk", long: "takes a lot of water and energy to produce", short: "takes a lot of water to produce", weight: "heavy" },
  polyurethane: { name: "stretchy elastane", long: "is made from oil", short: "is made from oil", weight: "oil" },
  leather: { name: "leather", long: "comes from cattle farming, which has a big footprint", short: "comes from cattle farming", weight: "heavy" },
};

const TYPE_NAME: Record<string, string> = {
  tshirt: "t-shirt",
  shirt: "shirt",
  sweater: "sweater",
  trousers: "pair of trousers",
  jeans: "pair of jeans",
  shorts: "pair of shorts",
  skirt: "skirt",
  dress: "dress",
  jacket: "jacket",
  coat: "coat",
  sneakers: "pair of shoes",
  boots: "pair of boots",
  sandals: "pair of sandals",
};
export const typeName = (t?: string | null) => (t && TYPE_NAME[t]) || "item";

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
const materialsOf = (s: Sustainability) => (Array.isArray(s.materials) ? (s.materials as Material[]) : []);
const componentsOf = (s: Sustainability) => ((s.components as Components | undefined) ?? {}) as Components;

type Wear = "good" | "bad" | "neutral";

function materialSentence(s: Sustainability): { text: string; weight: string } {
  if (s.footprint_basis === "per_pair") {
    return { text: "For shoes we go by typical figures for a pair, not the exact materials.", weight: "unknown" };
  }
  const known = materialsOf(s).filter((m) => m.material !== "unknown" && MAT[m.material]);
  if (known.length === 0) {
    return { text: "We couldn't tell what it's made of, so this assumes a typical fabric.", weight: "unknown" };
  }
  const [a, b] = known;
  const A = MAT[a.material];
  if (b && b.share >= 0.3) {
    const B = MAT[b.material];
    return { text: `It's a mix of ${A.name} and ${B.name}: ${A.name} ${A.short}, and ${B.name} ${B.short}.`, weight: A.weight };
  }
  if (a.share >= 0.95) return { text: `${cap(A.name)} ${A.long}.`, weight: A.weight };
  return { text: `It's ${a.share >= 0.6 ? "mostly" : "partly"} ${A.name}, which ${A.long}.`, weight: A.weight };
}

function wearSentence(s: Sustainability): { text: string; wear: Wear } {
  // returned lower-case; plainSummary() adds the connector / capital letter
  const c = componentsOf(s);
  const n = c.n_new_outfits;
  const outfits = (k: number) => `${k} outfit${k === 1 ? "" : "s"}`;
  const pair = s.footprint_basis === "per_pair"; // shoes: "they / them"
  const it = pair ? "them" : "it";
  const itGoes = pair ? "they go" : "it goes";
  if (c.redundancy_level === "near_duplicate") {
    return { text: "you already own something very similar, so you'd probably wear this one less.", wear: "bad" };
  }
  if (n == null) {
    return { text: `it's compared with how often a typical ${typeName(s.garment_type)} gets worn.`, wear: "neutral" };
  }
  if (n <= 0) {
    return { text: `${pair ? "they don't" : "it doesn't"} go with much in your closet yet, so ${pair ? "they" : "it"} might not get worn often.`, wear: "bad" };
  }
  if (c.redundancy_level === "similar") {
    return {
      text: `${pair ? "they're" : "it's"} close to something you already own, so ${pair ? "they" : "it"} may get worn a bit less.`,
      wear: "bad",
    };
  }
  const u = c.utility_multiplier ?? 1;
  if (u >= 1.1) {
    return { text: `${itGoes} with ${outfits(n)} from your closet, so you'd likely wear ${it} a lot and the impact gets spread out.`, wear: "good" };
  }
  if (u < 0.95) {
    return { text: `${pair ? "they only go" : "it only goes"} with ${outfits(n)} from your closet so far, so ${pair ? "they" : "it"} may not get worn much.`, wear: "bad" };
  }
  return { text: `${itGoes} with ${outfits(n)} from your closet, so you'd wear ${it} about as often as usual.`, wear: "neutral" };
}

/** One or two everyday sentences explaining the rating. */
export function plainSummary(s: Sustainability): string {
  const m = materialSentence(s);
  const w = wearSentence(s);
  const light = m.weight === "light";
  let second = cap(w.text);
  if (w.wear === "good") second = (light ? "Plus, " : "The good news: ") + w.text;
  else if (w.wear === "bad" && light) second = "The catch: " + w.text;
  else if (w.wear === "bad" && m.weight !== "unknown") second = "On top of that, " + w.text;
  return `${m.text} ${second}`;
}

/** One short practical tip. */
export function plainTip(s: Sustainability): string {
  const c = componentsOf(s);
  if (c.redundancy_level === "near_duplicate" || c.redundancy_level === "similar") {
    return "Already have something like it? Wearing what you own is the greenest choice.";
  }
  if (c.n_new_outfits != null && c.n_new_outfits <= 0) return "Check it works with a few things you own before buying.";
  if (s.score != null && s.score < 42) return "Check secondhand first: buying pre-loved skips the impact of making a new one.";
  if (s.footprint_basis === "per_pair") return "Clean and repair them when needed and they can last for years.";
  const top = materialsOf(s).find((m) => m.material !== "unknown")?.material;
  if (top === "polyester" || top === "nylon" || top === "acrylic" || top === "recycled_polyester" || top === "polyurethane")
    return "Wash it cold and less often to shed fewer tiny plastic fibres.";
  if (top === "wool") return "Air it out between wears; wool rarely needs washing.";
  if (top === "leather") return "Clean and condition it now and then and it can last for years.";
  return "Wash cold and air-dry: it saves energy and helps it last longer.";
}

/** "about 2.1× the impact per wear of a typical pair of jeans" (from relative_to_typical; real number). */
export function comparison(s: Sustainability): string | null {
  const r = componentsOf(s).relative_to_typical;
  if (r == null || !isFinite(r) || r <= 0) return null;
  const t = `a typical ${typeName(s.garment_type)}`;
  if (r >= 0.9 && r <= 1.1) return `About the same impact per wear as ${t}`;
  if (r > 1.1) return `About ${r.toFixed(1)}× the impact per wear of ${t}`;
  return `About ${(1 / r).toFixed(1)}× lower impact per wear than ${t}`;
}

/** "100% cotton" / "50% cotton, 50% unknown fabric" */
export function materialMix(s: Sustainability): string | null {
  const ms = materialsOf(s);
  if (s.footprint_basis === "per_pair" || ms.length === 0) return null;
  return ms
    .map((m) => `${Math.round(m.share * 100)}% ${m.material === "unknown" ? "unidentified fabric" : MAT[m.material]?.name ?? m.material}`)
    .join(", ");
}

export const baseWears = (s: Sustainability) => componentsOf(s).base_wears ?? null;
