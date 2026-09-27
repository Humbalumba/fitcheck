"use client";

import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import type { Item } from "@/lib/types";
import { getCachedCloset, itemTileSrc, markIntroHandoff, preloadImage, setCachedCloset } from "@/lib/closetCache";
import { AppHeader, MAIN_CLS, MainNav } from "./AppShell";
import { ClosetContent } from "./ClosetView";

/**
 * Landing intro: a dark room with a wardrobe. The wardrobe is sized before the first paint (inline script → CSS
 * variables) and its doors start swinging straight away, with the zoom starting a beat later so the two overlap (both
 * are CSS animations, so they don't even wait for hydration). The inside stays pitch black until the closet + every
 * clothing image has loaded (the loading buffer). Then the black fades away to reveal the real closet page (laid out
 * at full viewport width and scaled down into the wardrobe) and the zoom lands with the inside filling the screen,
 * lined up pixel-for-pixel with /closet, where we navigate. If the data is slow the zoom eases to a crawl near its end
 * until the closet is ready (capped), so we don't land on a black or skeleton closet.
 */

// keep these three in sync with the .wardrobe-door / .wardrobe-zoom animations in globals.css
const DOOR_DELAY_MS = 100; // the doors start swinging (1.1s) this long after the first paint…
const ZOOM_DELAY_MS = 200; // …the zoom starts a beat later…
const ZOOM_MS = 1500; // …and takes this long, so it overlaps nearly the whole swing
const DARK_EARLIEST_MS = 300; // after the doors start: let the black inside show before the closet appears
const DARK_MS = 450; // black fade (keep in sync with .wardrobe-dark)
const QUICK_DARK_MS = 200; // …when the user tapped to skip (.wardrobe-quick)
const HOLD_AT = 0.62; // share of the zoom after which, if the closet isn't ready, the zoom slows to a crawl
const HOLD_RATE = 0.06;
const SKIP_RATE = 3;
const MAX_WAIT_MS = 2600; // reveal anyway this long after the wardrobe appears, even if images are still loading

const WOOD =
  "repeating-linear-gradient(92deg, rgba(0,0,0,0) 0px, rgba(0,0,0,0.06) 2px, rgba(0,0,0,0) 5px, rgba(255,255,255,0.03) 9px, rgba(0,0,0,0) 13px), linear-gradient(180deg, #8d6a4b 0%, #7a5a3f 45%, #6a4c34 100%)";

/**
 * Wardrobe geometry as CSS variables on <html>. Runs as an inline script before the first paint (serialised with
 * toString, so it must stay self-contained) and again on the client for in-app visits and resizes.
 * The interior's aspect (w/h) is never wider than the screen's, so when we zoom until the interior is as wide as the
 * screen it also covers the full height: the preview inside can then line up exactly with the real page.
 */
function applyWardrobeGeometry() {
  const root = document.documentElement;
  const W = root.clientWidth;
  const H = window.innerHeight;
  const aspect = Math.min(0.78, W / H);
  const chrome = 128 + 28 + 20 + 12; // brand line above, margin below, wardrobe top + base
  const maxIH = Math.max(220, H - chrome - 28);
  const iw = Math.round(Math.min(W * 0.78, 360, maxIH * aspect));
  const ih = Math.round(iw / aspect);
  const s = iw / W; // the closet page is laid out at viewport width and scaled by s into the interior
  const pad = Math.round(iw * 0.045);
  const set = (k: string, v: number, unit: string) => root.style.setProperty("--wd-" + k, v + unit);
  set("W", W, "px");
  set("H", H, "px");
  set("iw", iw, "px");
  set("ih", ih, "px");
  set("pad", pad, "px");
  set("fh", Math.max(H, Math.ceil(ih / s)), "px");
  set("s", s, "");
  // zoom target: scale about the interior's top-left corner and move that corner to the screen's top-left, so the
  // interior ends exactly screen-wide (the preview inside is then at 1:1 with the real page). Layout above the
  // interior: brand line block 128px, wardrobe top 20px, then the wood padding. (Re-measured on the client.)
  set("z", W / iw, "");
  set("ox", pad, "px");
  set("oy", 20 + pad, "px");
  set("tx", -((W - iw - 2 * pad) / 2 + pad), "px");
  set("ty", -(128 + 20 + pad), "px");
}
const GEOMETRY_SCRIPT = `(${applyWardrobeGeometry.toString()})()`;

/* true only while rendering on the server and hydrating: the inline script exists only in the server HTML */
const noSubscribe = () => () => {};
const useServerOrHydrating = () =>
  useSyncExternalStore(
    noSubscribe,
    () => false,
    () => true,
  );

export function OpeningAnimation({ href = "/closet" }: { href?: string }) {
  const router = useRouter();
  const serverOrHydrating = useServerOrHydrating();
  const [items, setItems] = useState<Item[] | null>(getCachedCloset);
  const [ready, setReady] = useState(false);
  const [lit, setLit] = useState(false); // the black inside fades out
  const [quick, setQuick] = useState(false); // tapped to skip

  const rootRef = useRef<HTMLDivElement>(null);
  const zoomRef = useRef<HTMLDivElement>(null);
  const interiorRef = useRef<HTMLDivElement>(null);
  const skipRef = useRef<() => void>(() => {});

  useLayoutEffect(() => {
    const update = () => {
      applyWardrobeGeometry();
      // exact zoom target from layout (offsets ignore the running transforms), in case e.g. the brand line's height
      // differs from the script's assumption
      const z = zoomRef.current;
      const inner = interiorRef.current;
      if (!z || !inner) return;
      // interior offset inside the zoom box (= transform origin), then the zoom box's own top on the page
      let ox = 0;
      let oy = 0;
      for (let el: HTMLElement | null = inner; el && el !== z; el = el.offsetParent as HTMLElement | null) {
        ox += el.offsetLeft;
        oy += el.offsetTop;
      }
      let zTop = 0;
      for (let el: HTMLElement | null = z; el && el !== document.body; el = el.offsetParent as HTMLElement | null) zTop += el.offsetTop;
      const root = document.documentElement;
      const W = root.clientWidth;
      const set = (k: string, v: number, unit = "px") => root.style.setProperty("--wd-" + k, v + unit);
      set("z", W / inner.offsetWidth, "");
      set("ox", ox);
      set("oy", oy);
      set("tx", -((W - z.offsetWidth) / 2 + ox)); // the zoom box is centred (offsetLeft would round)
      set("ty", -(zTop + oy));
    };
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);

  useEffect(() => {
    let alive = true;
    const timers: number[] = [];
    const later = (fn: () => void, ms: number) => {
      const id = window.setTimeout(() => alive && fn(), ms);
      timers.push(id);
      return id;
    };
    router.prefetch(href);
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    let isReady = false;
    let skipping = false;
    let navigated = false;
    let darkDone = false;
    let holding = false;
    let gate = 0;
    let ramp: number[] = [];

    // the CSS animations that make up the intro (doors, brand line, zoom, inner shadow, preview nav)
    const all = () =>
      (rootRef.current?.getAnimations({ subtree: true }) ?? []).filter((a) =>
        /^(wardrobe-|fadein$)/.test((a as CSSAnimation).animationName ?? ""),
      );
    const named = (name: string) => all().find((a) => (a as CSSAnimation).animationName === name);
    const zoom = named("wardrobe-zoom") ?? null;
    const door = named("wardrobe-door-left");
    // ms since the doors started moving (negative during the CSS delay)
    const doorElapsed = () => (door ? Number(door.currentTime ?? 0) - DOOR_DELAY_MS : 10_000);
    const zoomElapsed = () => (zoom ? Number(zoom.currentTime ?? 0) - ZOOM_DELAY_MS : ZOOM_MS);
    let zoomDone = !zoom;

    const finish = () => {
      if (!alive || navigated || !zoomDone || !darkDone) return;
      navigated = true;
      markIntroHandoff(); // /closet renders from the cache without its arrive animation: an invisible swap
      router.push(href);
    };

    // change the zoom's speed smoothly (a few steps) instead of jerking it
    const rampRate = (to: number, ms: number) => {
      ramp.forEach(clearTimeout);
      ramp = [];
      if (!zoom) return;
      const from = zoom.playbackRate;
      const steps = ms > 0 ? 6 : 1;
      for (let i = 1; i <= steps; i++)
        ramp.push(later(() => zoom.updatePlaybackRate(from + ((to - from) * i) / steps), (ms * i) / steps));
    };

    if (zoom && !reduced) {
      zoom.finished
        .then(() => {
          zoomDone = true;
          finish();
        })
        .catch(() => {});
      // not ready late in the zoom → ease it to a crawl so we don't land on a black / skeleton closet
      gate = window.setInterval(() => {
        if (isReady) return clearInterval(gate);
        if (!holding && zoomElapsed() >= HOLD_AT * ZOOM_MS) {
          holding = true;
          rampRate(HOLD_RATE, 260);
        }
      }, 40);
    }

    const reveal = () => {
      const wait = skipping ? 0 : Math.max(0, DARK_EARLIEST_MS - doorElapsed());
      later(() => {
        setLit(true);
        later(
          () => {
            darkDone = true;
            finish();
          },
          (skipping ? QUICK_DARK_MS : DARK_MS) + 30,
        );
        if (holding) rampRate(skipping ? SKIP_RATE : 1, 240);
        holding = false;
      }, wait);
    };

    const markReady = () => {
      if (isReady || !alive) return;
      isReady = true;
      setReady(true);
      if (reduced) {
        // no motion: straight to the closet once it can render fully
        later(() => {
          markIntroHandoff();
          router.push(href);
        }, 120);
        return;
      }
      reveal();
    };

    skipRef.current = () => {
      if (skipping || reduced || navigated) return;
      skipping = true;
      setQuick(true);
      for (const a of all()) if (a !== zoom || !holding) a.updatePlaybackRate(SKIP_RATE);
    };

    // loading buffer: fetch the closet + preload every tile image while the inside is still black (capped)
    // (counted from when the wardrobe first appeared, i.e. the start of the door animation's clock)
    later(markReady, Math.max(300, MAX_WAIT_MS - Number(door?.currentTime ?? 0)));
    api
      .listCloset()
      .then((list) => {
        if (!alive) return;
        setCachedCloset(list);
        setItems(list);
        return Promise.all(list.map((it) => preloadImage(itemTileSrc(it))));
      })
      .catch(() => {}) // the closet page shows the error; the intro just opens
      .finally(markReady);

    return () => {
      alive = false;
      timers.forEach(clearTimeout);
      clearInterval(gate);
    };
  }, [router, href]);

  const cls = ["relative min-h-dvh overflow-hidden bg-[#2e241d]"];
  if (lit) cls.push("wardrobe-lit");
  if (quick) cls.push("wardrobe-quick");

  return (
    <div ref={rootRef} className={cls.join(" ")} aria-busy={!ready || undefined}>
      {serverOrHydrating && <script dangerouslySetInnerHTML={{ __html: GEOMETRY_SCRIPT }} />}
      <div className="wardrobe-fade relative z-20 flex flex-col items-center gap-6 pt-12">
        <div className="flex items-center gap-2.5 text-[#f1ebe0] font-semibold tracking-tight text-2xl">
          <span className="grid place-items-center size-10 rounded-xl bg-[#f1ebe0] text-[#16161a] text-base font-bold">F✓</span>
          FitCheck
        </div>
      </div>

      <div className="wardrobe-stage relative z-10 flex justify-center mt-10">
        <div ref={zoomRef} className="wardrobe-zoom relative">
          <div className="relative" style={{ width: "calc(var(--wd-iw) + 2 * var(--wd-pad))" }}>
            <div className="mx-[-4%] h-5 rounded-t-md bg-[#4e3726]" />
            <div className="relative" style={{ padding: "var(--wd-pad)", background: WOOD }}>
              {/* inside of the closet: pitch black, fading into the real closet page once it has loaded */}
              <div
                ref={interiorRef}
                className="relative overflow-hidden bg-black"
                style={{ width: "var(--wd-iw)", height: "var(--wd-ih)" }}
              >
                <ClosetPreview items={items} />
                <div className="wardrobe-shade pointer-events-none absolute inset-0 shadow-[inset_0_10px_30px_rgba(0,0,0,0.25)]" />
                <div className="wardrobe-dark pointer-events-none absolute inset-0 bg-black" />
              </div>
              {/* doors (tap to skip ahead): perspective must sit on their direct parent */}
              <button
                type="button"
                onClick={() => skipRef.current()}
                aria-label="Open the closet"
                style={{ perspective: "1100px", inset: "var(--wd-pad)" }}
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
    </div>
  );
}

/**
 * The real closet page (header, title, chips, racks with the user's clothes, floating nav) laid out at the viewport's
 * width and height, then scaled down to the interior's width. Only the top of the page shows through the interior.
 */
function ClosetPreview({ items }: { items: Item[] | null }) {
  return (
    <div
      aria-hidden
      inert
      className="wardrobe-preview pointer-events-none absolute left-0 top-0 origin-top-left bg-paper select-none"
      style={{ width: "var(--wd-W)", height: "var(--wd-fh)", transform: "scale(var(--wd-s))" }}
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
      <div className="absolute inset-x-0 top-0" style={{ height: "var(--wd-H)", contain: "layout" }}>
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
