"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import type { Item } from "@/lib/types";
import { getCachedCloset, itemTileSrc, markIntroHandoff, preloadImage, setCachedCloset } from "@/lib/closetCache";
import { AppHeader, MAIN_CLS, MainNav } from "./AppShell";
import { ClosetContent } from "./ClosetView";

/**
 * Landing intro: a dark room with a wardrobe. While the doors stay shut we fetch the closet and preload every
 * clothing image (the loading buffer). Then the doors open, the black inside fades away to reveal the real closet
 * page (rendered at full viewport width and scaled down into the wardrobe), and we zoom in until the inside fills the
 * screen, lined up pixel-for-pixel with /closet, and navigate there.
 */

type Phase = "closed" | "open" | "enter";

const MIN_HOLD_MS = 700; // doors stay shut at least this long…
const MAX_WAIT_MS = 3000; // …and at most this long while data + images load
const OPEN_TO_ZOOM_MS = 1150; // doors swing + the black fades, then…
const ZOOM_MS = 1100; // …walk in (keep in sync with .wardrobe-zoom in globals.css)

const WOOD =
  "repeating-linear-gradient(92deg, rgba(0,0,0,0) 0px, rgba(0,0,0,0.06) 2px, rgba(0,0,0,0) 5px, rgba(255,255,255,0.03) 9px, rgba(0,0,0,0) 13px), linear-gradient(180deg, #8d6a4b 0%, #7a5a3f 45%, #6a4c34 100%)";

/* ---- viewport size (layout width without scrollbar, visible height) ---- */
function subscribeViewport(cb: () => void) {
  window.addEventListener("resize", cb);
  return () => window.removeEventListener("resize", cb);
}
const viewportNow = () => `${document.documentElement.clientWidth}x${window.innerHeight}`;
const viewportServer = () => "";

type Geo = { W: number; H: number; iw: number; ih: number; pad: number; s: number; frameH: number };

/**
 * Wardrobe interior size. Its aspect (w/h) is never wider than the screen's, so when we zoom until the interior is as
 * wide as the screen it also covers the full height: the preview inside can then line up exactly with the real page.
 */
function geometry(W: number, H: number): Geo {
  const aspect = Math.min(0.78, W / H);
  const chrome = 128 + 28 + 20 + 12; // brand line above, margin below, wardrobe top + base
  const maxIH = Math.max(220, H - chrome - 28);
  const iw = Math.round(Math.min(W * 0.78, 360, maxIH * aspect));
  const ih = Math.round(iw / aspect);
  const s = iw / W; // the closet page is laid out at viewport width and scaled by s into the interior
  return { W, H, iw, ih, pad: Math.round(iw * 0.045), s, frameH: Math.max(H, Math.ceil(ih / s)) };
}

export function OpeningAnimation({ href = "/closet" }: { href?: string }) {
  const router = useRouter();
  const vp = useSyncExternalStore(subscribeViewport, viewportNow, viewportServer);
  const [W, H] = vp ? vp.split("x").map(Number) : [0, 0];
  const geo = W && H ? geometry(W, H) : null;
  const hasGeo = !!geo;

  const [items, setItems] = useState<Item[] | null>(getCachedCloset);
  const [ready, setReady] = useState(false);
  const [phase, setPhase] = useState<Phase>("closed");
  const [zoom, setZoom] = useState<{ origin: string; transform: string } | null>(null);

  const zoomRef = useRef<HTMLDivElement>(null);
  const interiorRef = useRef<HTMLDivElement>(null);
  const timers = useRef<number[]>([]);
  const started = useRef(false);
  const tapped = useRef(false);
  const mountedAt = useRef(0);

  // 1. Loading buffer: fetch the closet + preload every tile image while the doors are shut (capped at MAX_WAIT_MS).
  useEffect(() => {
    let alive = true;
    mountedAt.current = performance.now();
    router.prefetch(href);
    const done = () => alive && setReady(true);
    const cap = window.setTimeout(done, MAX_WAIT_MS);
    api
      .listCloset()
      .then((list) => {
        if (!alive) return;
        setCachedCloset(list);
        setItems(list);
        return Promise.all(list.map((it) => preloadImage(itemTileSrc(it))));
      })
      .catch(() => {}) // the closet page shows the error; the intro just opens
      .finally(done);
    return () => {
      alive = false;
      clearTimeout(cap);
    };
  }, [router, href]);

  useEffect(() => {
    const t = timers.current;
    return () => t.forEach(clearTimeout);
  }, []);

  const start = useCallback(() => {
    if (started.current) return;
    started.current = true;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const zoomAt = reduced ? 50 : OPEN_TO_ZOOM_MS;
    setPhase("open"); // 2. doors swing open while the black inside fades to the closet
    timers.current.push(
      window.setTimeout(() => {
        // 3. walk in: scale about the interior's top-left corner and move it to the screen's top-left, so the
        //    interior ends exactly screen-wide (the preview inside is then at 1:1 with the real page)
        const z = zoomRef.current?.getBoundingClientRect();
        const ir = interiorRef.current?.getBoundingClientRect();
        if (z && ir) {
          const scale = document.documentElement.clientWidth / ir.width;
          setZoom({
            origin: `${ir.left - z.left}px ${ir.top - z.top}px`,
            transform: `translate(${-ir.left}px, ${-ir.top}px) scale(${scale})`,
          });
        }
        setPhase("enter");
      }, zoomAt),
    );
    timers.current.push(
      window.setTimeout(() => {
        markIntroHandoff(); // 4. /closet renders from the cache without its arrive animation: an invisible swap
        router.push(href);
      }, reduced ? 150 : zoomAt + ZOOM_MS + 80),
    );
  }, [router, href]);

  // Auto-start once loaded (after the minimum hold); a tap during loading starts the moment it's ready.
  useEffect(() => {
    if (!ready || !hasGeo) return;
    if (tapped.current) return start();
    const t = window.setTimeout(start, Math.max(0, MIN_HOLD_MS - (performance.now() - mountedAt.current)));
    return () => clearTimeout(t);
  }, [ready, hasGeo, start]);

  const tap = () => {
    if (ready) start();
    else tapped.current = true;
  };

  const cls = ["relative min-h-dvh overflow-hidden bg-[#2e241d]"];
  if (phase !== "closed") cls.push("wardrobe-open");
  if (phase === "enter") cls.push("wardrobe-enter");

  return (
    <div className={cls.join(" ")} aria-busy={!ready || undefined}>
      <div className="wardrobe-fade relative z-20 flex flex-col items-center gap-6 pt-12">
        <div className="flex items-center gap-2.5 text-[#f1ebe0] font-semibold tracking-tight text-2xl">
          <span className="grid place-items-center size-10 rounded-xl bg-[#f1ebe0] text-[#16161a] text-base font-bold">F✓</span>
          FitCheck
        </div>
      </div>

      {geo && (
        <div className="wardrobe-stage fadein relative z-10 flex justify-center mt-10">
          <div
            ref={zoomRef}
            className="wardrobe-zoom relative"
            style={zoom && phase === "enter" ? { transformOrigin: zoom.origin, transform: zoom.transform } : undefined}
          >
            <div className="relative" style={{ width: geo.iw + 2 * geo.pad }}>
              <div className="mx-[-4%] h-5 rounded-t-md bg-[#4e3726]" />
              <div className="relative" style={{ padding: geo.pad, background: WOOD }}>
                {/* inside of the closet: pitch black, fading into the real closet page as the doors open */}
                <div ref={interiorRef} className="relative overflow-hidden bg-black" style={{ width: geo.iw, height: geo.ih }}>
                  <ClosetPreview geo={geo} items={items} />
                  <div className="wardrobe-shade pointer-events-none absolute inset-0 shadow-[inset_0_10px_30px_rgba(0,0,0,0.25)]" />
                  <div className="wardrobe-dark pointer-events-none absolute inset-0 bg-black" />
                </div>
                {/* doors: perspective must sit on their direct parent */}
                <button
                  type="button"
                  onClick={tap}
                  aria-label="Open the closet"
                  style={{ perspective: "1100px", inset: geo.pad }}
                  className="absolute flex"
                >
                  <Door side="left" />
                  <Door side="right" />
                </button>
              </div>
              <div className="mx-[-1.5%] h-3 bg-[#4e3726]" />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * The real closet page (header, title, chips, racks with the user's clothes, floating nav) laid out at the viewport's
 * width and height, then scaled down to the interior's width. Only the top of the page shows through the interior.
 */
function ClosetPreview({ geo, items }: { geo: Geo; items: Item[] | null }) {
  return (
    <div
      aria-hidden
      inert
      className="pointer-events-none absolute left-0 top-0 origin-top-left bg-paper select-none"
      style={{ width: geo.W, height: geo.frameH, transform: `scale(${geo.s})` }}
    >
      <div className="min-h-dvh flex flex-col">
        <AppHeader />
        <main className={MAIN_CLS}>
          <div>
            <ClosetContent items={items} />
          </div>
        </main>
      </div>
      {/* one screen tall: layout containment makes the nav's position:fixed pin to this box, like it pins to the
          real viewport; it fades in as we walk in so it isn't floating mid-wardrobe during the reveal */}
      <div className="absolute inset-x-0 top-0" style={{ height: geo.H, contain: "layout" }}>
        <MainNav pathname="/closet" className="wardrobe-nav" />
      </div>
    </div>
  );
}

function Door({ side }: { side: "left" | "right" }) {
  return (
    <div className={`wardrobe-door ${side} relative h-full w-1/2`} style={{ background: WOOD }}>
      <div className="absolute inset-x-[14%] top-[7%] h-[40%] rounded-[3px] border border-black/25" />
      <div className="absolute inset-x-[14%] bottom-[7%] h-[40%] rounded-[3px] border border-black/25" />
      <span
        className={`absolute top-1/2 -translate-y-1/2 size-3.5 rounded-full bg-[#b8935c] ${
          side === "left" ? "right-[9%]" : "left-[9%]"
        }`}
      />
    </div>
  );
}
