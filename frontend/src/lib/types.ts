export type Category =
  | "top"
  | "bottom"
  | "outerwear"
  | "dress"
  | "shoes"
  | "accessory"
  | string;

export interface Attributes {
  category?: Category;
  subcategory?: string | null;
  primary_color?: string | null;
  secondary_colors?: string[];
  pattern?: string | null;
  fabric_guess?: string | null;
  formality?: number | null;
  seasons?: string[];
  style_tags?: string[];
  gender_presentation?: string | null;
  brand?: string | null;
  price?: number | null;
  /** who set `price`: the user, a price tag read by Gemini, or seed/demo data */
  price_source?: "user" | "tag" | "seed" | string | null;
  /** typical new US retail price estimated from brand + type + material (Gemini) or a per-type table */
  estimated_price_usd?: number | null;
  price_confidence?: "high" | "medium" | "low" | string | null;
  price_estimate_source?: "gemini" | "gemini_text" | "table" | string | null;
  currency?: string | null;
  description?: string | null;
  [key: string]: unknown;
}

export interface DetectedItem {
  id: string;
  bbox: [number, number, number, number]; // ymin, xmin, ymax, xmax (0-1000)
  label: string;
  image_url?: string; // cutout on white (preferred for display)
  cutout_url: string; // transparent PNG
  crop_url?: string;
  status?: string;
  category?: Category;
  attributes: Attributes;
}

export interface DetectResponse {
  photo_id: string;
  image_url: string;
  items: DetectedItem[];
}

export interface Item {
  id: string;
  status: string;
  category: Category;
  image_url: string; // display image: clean product render when ready, else cutout on white
  cutout_url: string;
  crop_url?: string | null; // original photo crop
  original_image_url?: string | null; // cutout on white (before the clean render)
  clean_image_url?: string | null; // clean product image (app/render.py), null until ready
  clean_method?: "gemini" | "cleanup" | null; // Gemini redraw (verified) | deterministic cleanup
  render_status?: "pending" | "done" | "failed" | null;
  render_checks?: Record<string, unknown> | null;
  attributes: Attributes;
  created_at: string;
  [key: string]: unknown;
}

export interface RedundancyMatch {
  item: Item;
  similarity: number;
  image_similarity?: number;
  text_similarity?: number;
}

export interface Outfit {
  template: string;
  items: Item[];
  score: number;
}

/** Sustainability estimate (app/sustainability.py). Informational only; never changes the verdict. */
export interface Sustainability {
  supported: boolean;
  is_estimate: boolean;
  score: number | null; // 0-100
  grade: "A" | "B" | "C" | "D" | "E" | string | null;
  label: string | null;
  reasons: string[];
  footprint_kg_co2e: number | null;
  water_l: number | null;
  water_complete?: boolean;
  expected_wears: number | null;
  per_wear_kg_co2e: number | null;
  per_wear_water_l: number | null;
  cost_per_wear: number | null;
  garment_type: string | null;
  footprint_basis?: "per_kg" | "per_pair" | string;
  methodology?: string;
  sources?: string[];
  [key: string]: unknown;
}

export type Decision = "BUY" | "CONSIDER" | "SKIP" | "UNSUPPORTED";

export interface VerdictComponent {
  score?: number | null; // 0-100 (cost can dip below 0 when wildly over your usual cost per wear)
  weight?: number;
  points?: number; // contribution to the final score (gap_fill / similarity: +/- points)
  [key: string]: unknown;
}

/** BUY / CONSIDER / SKIP from one 0-100 score (backend/app/verdict.py). */
export interface Verdict {
  decision: Decision | string;
  score?: number | null;
  uncapped_score?: number | null;
  /** automatic outcomes keep the score inside their band: "near_duplicate" | "no_outfits" | ... */
  capped_by?: string | null;
  /** the two factors that moved the score most */
  reasons: string[];
  all_reasons?: string[];
  components?: Record<"versatility" | "outfit_quality" | "cost" | "gap_fill" | "similarity" | string, VerdictComponent>;
  bands?: { BUY: number; CONSIDER: number };
  formula_version?: number;
  recomputed_on_read?: boolean;
}

export interface EvaluateResponse {
  item: Item;
  template_names: string[];
  redundancy: {
    level: "none" | "similar" | "near_duplicate" | string;
    top_similarity?: number;
    matches: RedundancyMatch[];
  };
  outfits: Outfit[];
  outfit_count_by_template: Record<string, number>;
  total_new_outfits: number;
  value: {
    /** the price used for cost per wear (the user's, a tag's, or the estimate) */
    price: number | null;
    currency?: string;
    /** "user" (typed in), "tag" (read off a price tag), "estimated" (brand + type estimate) */
    price_source?: "user" | "tag" | "estimated" | string | null;
    /** estimates only: high = brand identified; low confidence halves the cost weight */
    price_confidence?: "high" | "medium" | "low" | string | null;
    /** the item's estimated price (also present when the user entered a price) */
    estimated_price?: number | null;
    /** price / expected wears (the same wears model as the sustainability card) */
    cost_per_wear?: number | null;
    expected_wears?: number | null;
    /** "your usual" cost per wear: median of closet items with prices, else a default */
    price_bar?: number | null;
    price_bar_source?: "closet_median" | "closet_estimates" | "default" | string | null;
    priced_closet_items?: number;
    estimated_closet_items?: number;
    versatility?: { new_outfits: number; max_possible: number; share: number | null };
    outfit_quality?: number;
    weighted_outfits?: number;
    value_score: number | null; // 0-100, same as verdict.score
  };
  verdict: Verdict;
  outfits_truncated?: boolean;
  supported?: boolean;
  message?: string | null;
  scorer?: string;
  sustainability?: Sustainability | null;
  evaluation_id: string;
}

export interface Settings {
  compat_threshold: number;
  redundancy_similar_threshold: number;
  redundancy_duplicate_threshold: number;
  style_goal: string;
  occasions: string[];
  match_gender_presentation?: boolean;
  use_shoes_layer?: boolean;
  [key: string]: unknown;
}

export interface Health {
  ok: boolean;
  gemini: boolean;
  scorer: string;
  [key: string]: unknown;
}

/** A real product suggested after a verdict (POST /api/evaluations/{id}/suggestions). */
export interface Suggestion {
  id: string; // hypothetical item (status "suggestion"; never in the closet)
  item: Item;
  name: string;
  brand?: string | null;
  retailer?: string | null;
  price: number | null;
  currency?: string;
  product_url: string;
  link_status?: number | null;
  link_ok?: boolean | null;
  image_url?: string | null; // cutout on white
  photo_url?: string | null; // original product photo (local copy)
  source_image_url?: string | null;
  category: Category;
  subcategory?: string | null;
  color?: string | null;
  material?: string | null;
  reason: string;
  verdict: Verdict;
  value: EvaluateResponse["value"];
  total_new_outfits: number;
  outfits_with_candidate: number;
  outfit_count_by_template: Record<string, number>;
  template_names: string[];
  redundancy: { level: string; top_similarity?: number | null; closest?: string | null };
  similarity_to_candidate?: number | null;
  sustainability?: Sustainability | null;
  outfits: Outfit[];
}

export interface SuggestionsResponse {
  evaluation_id: string;
  mode: "alternatives" | "pairings" | "none" | string;
  title: string;
  candidate_verdict: string;
  suggestions: Suggestion[];
  rejected?: Array<{ name: string; retailer?: string | null; reason: string }>;
  message?: string | null;
  considered?: number;
  with_photo?: number;
  source?: string;
  model?: string | null;
  timing_s?: Record<string, number>;
  generated_at?: string;
  cached?: boolean;
}
