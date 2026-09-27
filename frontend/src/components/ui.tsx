"use client";

import { useEffect, useRef, useState } from "react";
import { X, Loader2, ImageOff } from "lucide-react";
import { cn } from "@/lib/format";
import { mediaUrl } from "@/lib/api";
import { colorToCss } from "@/lib/constants";
import { isImageLoaded, markImageLoaded } from "@/lib/closetCache";

type BtnProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger" | "accent";
  size?: "sm" | "md" | "lg";
  loading?: boolean;
};

export function Button({ variant = "primary", size = "md", loading, className, children, disabled, ...rest }: BtnProps) {
  return (
    <button
      {...rest}
      disabled={disabled || loading}
      className={cn(
        "inline-flex items-center justify-center gap-2 rounded-full font-semibold transition active:scale-[0.98] disabled:opacity-50 disabled:pointer-events-none select-none",
        size === "sm" && "h-9 px-3.5 text-sm",
        size === "md" && "h-11 px-5 text-[15px]",
        size === "lg" && "h-14 px-6 text-base w-full",
        variant === "primary" && "bg-ink text-white hover:bg-black",
        variant === "accent" && "bg-accent text-white hover:bg-indigo-700",
        variant === "secondary" && "bg-white text-ink border border-black/10 hover:bg-black/[0.03]",
        variant === "ghost" && "text-ink hover:bg-black/5",
        variant === "danger" && "bg-rose-50 text-rose-700 border border-rose-200 hover:bg-rose-100",
        className,
      )}
    >
      {loading && <Loader2 className="size-4 animate-spin" />}
      {children}
    </button>
  );
}

export function Chip({
  active,
  onClick,
  children,
  count,
  className,
}: {
  active?: boolean;
  onClick?: () => void;
  children: React.ReactNode;
  count?: number;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "shrink-0 inline-flex items-center gap-1.5 rounded-full border px-3.5 h-9 text-sm font-medium transition whitespace-nowrap",
        active ? "bg-ink text-white border-ink" : "bg-white text-black/70 border-black/10 hover:border-black/25",
        className,
      )}
    >
      {children}
      {count !== undefined && (
        <span
          className={cn(
            "rounded-full px-1.5 text-[11px] leading-[18px] tabular-nums",
            active ? "bg-white/20 text-white" : "bg-black/5 text-black/50",
          )}
        >
          {count}
        </span>
      )}
    </button>
  );
}

export function Sheet({
  open,
  onClose,
  title,
  children,
  footer,
}: {
  open: boolean;
  onClose: () => void;
  title?: React.ReactNode;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = prev;
      window.removeEventListener("keydown", onKey);
    };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="slideup relative w-full sm:max-w-lg max-h-[92dvh] flex flex-col rounded-t-3xl sm:rounded-3xl bg-card shadow-2xl">
        <div className="flex items-center justify-between px-5 pt-4 pb-2">
          <div className="font-semibold text-lg">{title}</div>
          <button onClick={onClose} className="grid place-items-center size-9 rounded-full hover:bg-black/5" aria-label="Close">
            <X className="size-5" />
          </button>
        </div>
        <div className="overflow-y-auto px-5 pb-4 flex-1">{children}</div>
        {footer && <div className="border-t border-black/5 px-5 py-3 pb-safe">{footer}</div>}
      </div>
    </div>
  );
}

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cn("size-5 animate-spin", className)} />;
}

/** Pulsing placeholder block (loading state). Give it a size / radius via className. */
export function Skeleton({ className, soft }: { className?: string; soft?: boolean }) {
  return <div aria-hidden className={cn("skeleton", soft && "skeleton-soft", className)} />;
}

/**
 * <img> that shows a shimmer skeleton until it has loaded, then fades in (~200ms). Fills its (relative) parent:
 * pass sizing / object-fit classes via className. On error the shimmer stops and `onError` lets the caller
 * swap in a fallback (or a neutral placeholder is left behind).
 */
export function LoadingImg({
  src,
  alt = "",
  className,
  onError,
  lazy = true,
}: {
  src: string;
  alt?: string;
  className?: string;
  onError?: () => void;
  lazy?: boolean;
}) {
  const ref = useRef<HTMLImageElement>(null);
  // Images preloaded earlier in this session (e.g. by the landing intro) show at once: no skeleton, no fade.
  const [loadedSrc, setLoadedSrc] = useState<string | null>(() => (isImageLoaded(src) ? src : null));
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  const loaded = loadedSrc === src || isImageLoaded(src);
  const failed = failedSrc === src;
  const onErrorRef = useRef(onError);
  useEffect(() => {
    onErrorRef.current = onError;
  });
  const handleLoad = () => {
    markImageLoaded(src);
    setLoadedSrc(src);
  };
  const handleError = () => {
    setFailedSrc(src);
    onErrorRef.current?.();
  };
  // Cached images can finish (or fail) before React attaches onLoad/onError (e.g. before hydration),
  // so check the element itself once mounted, or the shimmer would never go away.
  useEffect(() => {
    const el = ref.current;
    if (!el || !el.complete) return;
    let live = true;
    if (el.naturalWidth) {
      const t = setTimeout(() => {
        markImageLoaded(src);
        setLoadedSrc(src);
      }, 0);
      return () => clearTimeout(t);
    }
    // complete with no pixels: broken, or an SVG without intrinsic size. decode() tells them apart.
    el.decode().then(
      () => {
        if (!live) return;
        markImageLoaded(src);
        setLoadedSrc(src);
      },
      () => {
        if (!live) return;
        setFailedSrc(src);
        onErrorRef.current?.();
      },
    );
    return () => {
      live = false;
    };
  }, [src]);
  return (
    <>
      {!loaded && <span aria-hidden className={cn("img-shimmer absolute inset-0", failed && "img-shimmer-off")} />}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        ref={ref}
        src={src}
        alt={alt}
        loading={lazy ? "lazy" : undefined}
        decoding="async"
        onLoad={handleLoad}
        onError={handleError}
        className={cn(
          "transition-opacity duration-200 ease-out",
          loaded ? "opacity-100" : "opacity-0",
          failed && "invisible",
          className,
        )}
      />
    </>
  );
}

export function ItemImage({
  src,
  alt,
  className,
  pad = true,
  tone = "bg-white",
  blend = false,
}: {
  src?: string | null;
  alt?: string;
  className?: string;
  pad?: boolean;
  /** background class behind the picture */
  tone?: string;
  /** multiply the picture over the tone (white photo backgrounds melt into it) */
  blend?: boolean;
}) {
  const [err, setErr] = useState(false);
  const url = mediaUrl(src);
  return (
    <div className={cn("relative overflow-hidden grid place-items-center", tone, className)}>
      {url && !err ? (
        <LoadingImg
          src={url}
          alt={alt ?? ""}
          onError={() => setErr(true)}
          className={cn("absolute inset-0 size-full object-contain", pad && "p-[8%]", blend && "mix-blend-multiply")}
        />
      ) : (
        <ImageOff className="size-6 text-black/20" />
      )}
    </div>
  );
}

export function ColorDot({ color, className }: { color?: string | null; className?: string }) {
  const css = colorToCss(color);
  if (!css) return null;
  return (
    <span
      className={cn("inline-block size-3 rounded-full ring-1 ring-black/15 shrink-0", className)}
      style={{ background: css }}
    />
  );
}

export function FormalityDots({ value }: { value?: number | null }) {
  const v = Math.max(0, Math.min(5, Math.round(Number(value) || 0)));
  return (
    <span className="inline-flex gap-0.5" title={`Formality ${v}/5`}>
      {[1, 2, 3, 4, 5].map((i) => (
        <span key={i} className={cn("h-1.5 w-2.5 rounded-full", i <= v ? "bg-ink" : "bg-black/10")} />
      ))}
    </span>
  );
}

export function EmptyState({
  icon,
  title,
  body,
  action,
}: {
  icon: React.ReactNode;
  title: string;
  body?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center text-center py-14 px-6">
      <div className="grid place-items-center size-16 rounded-3xl bg-card border border-black/5 mb-4 text-black/40">{icon}</div>
      <div className="font-semibold text-lg">{title}</div>
      {body && <p className="text-sm text-black/55 mt-1 max-w-xs">{body}</p>}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

export function PageTitle({
  title,
  subtitle,
  right,
  center,
}: {
  title: string;
  subtitle?: string;
  right?: React.ReactNode;
  center?: boolean;
}) {
  return (
    <div className={cn("flex items-end gap-3 mb-4", center ? "justify-center text-center" : "justify-between")}>
      <div>
        <h1 className="text-[26px] font-bold tracking-tight leading-tight">{title}</h1>
        {subtitle && <p className="text-sm text-black/55 mt-0.5">{subtitle}</p>}
      </div>
      {right}
    </div>
  );
}

export function Card({ className, children, ...rest }: React.HTMLAttributes<HTMLDivElement>) {
  return <div {...rest} className={cn("rounded-3xl bg-card border border-black/5 shadow-[0_1px_2px_rgba(60,40,20,0.05)]", className)}>{children}</div>;
}

export function ErrorBanner({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="rounded-2xl border border-rose-200 bg-rose-50 text-rose-800 text-sm px-4 py-3 flex items-center justify-between gap-3">
      <span>{message}</span>
      {onRetry && (
        <button onClick={onRetry} className="font-semibold underline underline-offset-2 shrink-0">
          Retry
        </button>
      )}
    </div>
  );
}
