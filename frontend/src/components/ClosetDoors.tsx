"use client";

import { useEffect, useState } from "react";
import { cn } from "@/lib/format";

const HOLD_MS = 450; // doors stay shut for a beat
const OPEN_MS = 1650; // swing (1.35s) + overlay fade

/**
 * Landing intro: a pair of wooden closet doors swing open to reveal the closet.
 * Server-rendered (so it covers the page from the first paint) and hidden by CSS when the inline <head> script marks
 * <html class="fc-no-doors"> (the first page isn't the closet).
 */
export function ClosetDoors() {
  const [phase, setPhase] = useState<"closed" | "opening" | "done">("closed");

  useEffect(() => {
    const skip = document.documentElement.classList.contains("fc-no-doors");
    if (skip) {
      const t = setTimeout(() => setPhase("done"), 0);
      return () => clearTimeout(t);
    }
    const t1 = setTimeout(() => setPhase("opening"), HOLD_MS);
    const t2 = setTimeout(() => setPhase("done"), HOLD_MS + OPEN_MS);
    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
    };
  }, []);

  if (phase === "done") return null;
  return (
    <div
      className={cn("closet-doors", phase === "opening" && "is-open")}
      aria-hidden="true"
      data-testid="closet-doors"
      onClick={() => setPhase("opening")}
    >
      {(["left", "right"] as const).map((side) => (
        <div key={side} className={`closet-door closet-door--${side}`}>
          <div className="closet-door__panel closet-door__panel--louver" />
          <div className="closet-door__panel closet-door__panel--plain" />
          <span className="closet-door__handle" />
        </div>
      ))}
    </div>
  );
}
