"use client";

import { useRef } from "react";
import { Camera, Images } from "lucide-react";
import { cn } from "@/lib/format";

export function FilePicker({
  onFiles,
  multiple = true,
  disabled,
  compact,
  cameraLabel = "Take photo",
  libraryLabel,
}: {
  onFiles: (files: File[]) => void;
  multiple?: boolean;
  disabled?: boolean;
  compact?: boolean;
  cameraLabel?: string;
  libraryLabel?: string;
}) {
  const cam = useRef<HTMLInputElement>(null);
  const lib = useRef<HTMLInputElement>(null);
  const handle = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []).filter((f) => f.type.startsWith("image/") || f.type === "");
    if (files.length) onFiles(files);
    e.target.value = "";
  };
  return (
    <div className={cn("grid grid-cols-2 gap-2.5", disabled && "opacity-50 pointer-events-none")}>
      <input ref={cam} type="file" accept="image/*" capture="environment" className="hidden" onChange={handle} />
      <input ref={lib} type="file" accept="image/*" multiple={multiple} className="hidden" onChange={handle} />
      <button
        type="button"
        onClick={() => cam.current?.click()}
        className={cn(
          "flex items-center justify-center gap-2 rounded-2xl bg-ink text-white font-semibold active:scale-[0.98] transition",
          compact ? "h-12 text-sm" : "flex-col h-28 text-[15px]",
        )}
      >
        <Camera className={compact ? "size-4" : "size-7"} />
        {cameraLabel}
      </button>
      <button
        type="button"
        onClick={() => lib.current?.click()}
        className={cn(
          "flex items-center justify-center gap-2 rounded-2xl bg-white border border-black/10 font-semibold active:scale-[0.98] transition",
          compact ? "h-12 text-sm" : "flex-col h-28 text-[15px]",
        )}
      >
        <Images className={compact ? "size-4" : "size-7"} />
        {libraryLabel ?? (multiple ? "Choose photos" : "Choose photo")}
      </button>
    </div>
  );
}
