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
  image_url: string;
  cutout_url: string;
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
    price: number | null;
    cost_per_outfit: number | null;
    weighted_outfits: number;
    value_score: number | null; // 0-100
    redundancy_factor?: number;
    budget_remaining?: number | null;
    currency?: string;
  };
  verdict: { decision: "BUY" | "SKIP" | "UNSUPPORTED" | string; reasons: string[] };
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
  min_new_outfits: number;
  max_cost_per_outfit: number;
  monthly_budget: number | null;
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
  verdict: { decision: "BUY" | "SKIP" | string; reasons: string[] };
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
