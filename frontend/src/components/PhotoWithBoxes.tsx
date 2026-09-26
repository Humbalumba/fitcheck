"use client";

import { mediaUrl } from "@/lib/api";
import { cn } from "@/lib/format";

export interface Box {
  id: string;
  bbox: [number, number, number, number]; // ymin,xmin,ymax,xmax 0-1000
  label?: string;
}

const PALETTE = ["#6366f1", "#10b981", "#f59e0b", "#ec4899", "#06b6d4", "#8b5cf6", "#ef4444", "#84cc16"];
export const boxColor = (i: number) => PALETTE[i % PALETTE.length];

export function PhotoWithBoxes({
  src,
  boxes,
  selectedId,
  highlightId,
  dimmedIds,
  onSelect,
  scanning,
  className,
}: {
  src: string;
  boxes: Box[];
  selectedId?: string | null;
  highlightId?: string | null;
  dimmedIds?: Set<string>;
  onSelect?: (id: string) => void;
  scanning?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("flex justify-center overflow-hidden rounded-3xl bg-sand-deep", className)}>
      <div className="relative w-fit">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={mediaUrl(src)} alt="Uploaded photo" className="block max-w-full h-auto max-h-[62dvh]" />
        {/* Boxes are positioned relative to the image box; wrapper matches image size */}
        <div className="absolute inset-0">
          {boxes.map((b, i) => {
            const [ymin, xmin, ymax, xmax] = b.bbox;
            const color = boxColor(i);
            const sel = selectedId === b.id;
            const hi = highlightId === b.id;
            const dim = dimmedIds?.has(b.id) || (selectedId && !sel);
            return (
              <button
                key={b.id}
                type="button"
                onClick={() => onSelect?.(b.id)}
                className={cn(
                  "absolute rounded-xl transition-all pop",
                  onSelect ? "cursor-pointer" : "cursor-default",
                  dim ? "opacity-45" : "opacity-100",
                )}
                style={{
                  top: `${ymin / 10}%`,
                  left: `${xmin / 10}%`,
                  height: `${(ymax - ymin) / 10}%`,
                  width: `${(xmax - xmin) / 10}%`,
                  border: `${sel || hi ? 4 : 2.5}px solid ${color}`,
                  background: sel ? `${color}22` : hi ? `${color}18` : "transparent",
                  boxShadow: sel ? `0 0 0 3px #fff, 0 0 24px ${color}` : "0 0 0 1px #ffffff99",
                  animationDelay: `${i * 80}ms`,
                }}
                aria-label={b.label ?? `Item ${i + 1}`}
              >
                <span
                  className="absolute -top-px -left-px rounded-br-lg rounded-tl-lg px-1.5 py-0.5 text-[11px] font-bold text-white max-w-full truncate"
                  style={{ background: color }}
                >
                  {i + 1}
                  {b.label ? ` · ${b.label}` : ""}
                </span>
              </button>
            );
          })}
        </div>
        {scanning && <div className="scanline" />}
      </div>
    </div>
  );
}
