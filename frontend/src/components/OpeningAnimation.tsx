"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

type Phase = "idle" | "open" | "enter";

const WOOD =
  "repeating-linear-gradient(92deg, rgba(0,0,0,0) 0px, rgba(0,0,0,0.06) 2px, rgba(0,0,0,0) 5px, rgba(255,255,255,0.03) 9px, rgba(0,0,0,0) 13px), linear-gradient(180deg, #8d6a4b 0%, #7a5a3f 45%, #6a4c34 100%)";

export function OpeningAnimation({ href = "/closet" }: { href?: string }) {
  const router = useRouter();
  const [phase, setPhase] = useState<Phase>("idle");
  const timers = useRef<number[]>([]);

  useEffect(() => {
    router.prefetch(href);
    const t = timers.current;
    return () => t.forEach(clearTimeout);
  }, [router, href]);

  const enter = () => {
    if (phase !== "idle") return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    setPhase("open"); // 1. doors swing open
    timers.current.push(window.setTimeout(() => setPhase("enter"), reduced ? 50 : 700)); // 2. zoom in
    timers.current.push(window.setTimeout(() => router.push(href), reduced ? 150 : 1900)); // 3. navigate
  };

  const cls = ["relative min-h-dvh overflow-hidden bg-[#2e241d]"];
  if (phase !== "idle") cls.push("wardrobe-open");
  if (phase === "enter") cls.push("wardrobe-enter");

  return (
    <div className={cls.join(" ")}>
      <div className="wardrobe-fade relative z-20 flex flex-col items-center gap-6 pt-12">
        <div className="flex items-center gap-2.5 text-[#f1ebe0] font-semibold tracking-tight text-2xl">
          <span className="grid place-items-center size-10 rounded-xl bg-[#f1ebe0] text-[#16161a] text-base font-bold">F✓</span>
          FitCheck
        </div>
        <button
          onClick={enter}
          className="rounded-full bg-[#f1ebe0] px-7 h-14 text-xl text-[#161412]"
        >
          Enter your closet →
        </button>
      </div>

      <div className="wardrobe-stage relative z-10 flex justify-center mt-10">
        <div className="wardrobe-zoom relative" style={{ transformOrigin: "50% 48%" }}>
          <div className="relative w-[min(78vw,340px)] sm:w-[360px]">
            <div className="mx-[-4%] h-5 rounded-t-md bg-[#4e3726]" />
            <div className="relative aspect-[0.78] p-[4%]" style={{ background: WOOD }}>
              {/* lit interior revealed behind the doors */}
              <div className="absolute inset-[4%] bg-[radial-gradient(90%_70%_at_50%_20%,#f7f4ee_0%,#e7e2da_60%,#d3ccc1_100%)] shadow-[inset_0_10px_30px_rgba(0,0,0,0.25)]">
                <div className="absolute left-[4%] right-[4%] top-[12%] h-[5px] rounded-full bg-[#b8935c]" />
              </div>
              {/* doors: perspective must sit on their direct parent */}
              <button
                type="button"
                onClick={enter}
                aria-label="Open the closet"
                style={{ perspective: "1100px" }}
                className="absolute inset-[4%] flex"
              >
                <Door side="left" />
                <Door side="right" />
              </button>
            </div>
            <div className="mx-[-1.5%] h-3 bg-[#4e3726]" />
          </div>
        </div>
      </div>

      {/* the closet's light fills the screen as you walk in */}
      <div
        aria-hidden
        className={`pointer-events-none absolute inset-0 z-30 bg-paper transition-opacity duration-500 ${
          phase === "enter" ? "opacity-100 delay-[650ms]" : "opacity-0"
        }`}
      />
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
