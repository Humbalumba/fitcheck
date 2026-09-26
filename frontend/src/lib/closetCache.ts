import { mediaUrl } from "./api";
import type { Item } from "./types";

/**
 * Tiny client-side cache shared by the landing intro and the closet page.
 * The intro fetches the closet and preloads every image while the wardrobe doors are shut, so /closet can render
 * the real racks straight away (it still refreshes in the background). Module state only ever changes in the
 * browser (effects / event handlers), so server renders always see the empty cache.
 */
let closet: Item[] | null = null;
export const getCachedCloset = () => closet;
export function setCachedCloset(items: Item[]) {
  closet = items;
}

/** The exact URL <ItemImage> loads for a closet tile. */
export const itemTileSrc = (it: Item) => mediaUrl(it.clean_image_url || it.image_url || it.cutout_url);

/* ---- images that are already downloaded + decoded (LoadingImg skips its skeleton/fade for these) ---- */
const loaded = new Set<string>();
export const isImageLoaded = (url: string) => loaded.has(url);
export function markImageLoaded(url: string) {
  if (url) loaded.add(url);
}

/** Download + decode one image; never rejects. */
export function preloadImage(url: string): Promise<void> {
  if (!url || loaded.has(url)) return Promise.resolve();
  return new Promise<void>((resolve) => {
    const img = new Image();
    img.decoding = "async";
    img.onload = () => {
      markImageLoaded(url);
      (img.decode ? img.decode().catch(() => {}) : Promise.resolve()).then(() => resolve());
    };
    img.onerror = () => resolve();
    img.src = url;
  });
}

/* ---- intro -> /closet handoff: the closet skips its "arrive" animation right after the intro zoom ---- */
let handoffAt = -Infinity;
export function markIntroHandoff() {
  handoffAt = performance.now();
}
/** Idempotent (safe for StrictMode double-invoked initializers). */
export function cameFromIntro() {
  return typeof performance !== "undefined" && performance.now() - handoffAt < 2500;
}
