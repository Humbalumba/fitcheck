"use client";

import Link from "next/link";
import { useState } from "react";
import { Eye, EyeOff } from "lucide-react";
import { cn } from "@/lib/format";

/** Centered card on the paper background with the black F✓ logo: shared by every signed-out page. */
export function AuthFrame({ title, subtitle, children, footer }: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  return (
    <div className="min-h-dvh flex flex-col items-center justify-center px-5 py-10">
      <Link href="/login" aria-label="FitCheck" className="flex items-center gap-2.5 mb-7 font-semibold tracking-tight text-[20px]">
        <span className="grid place-items-center size-11 rounded-2xl bg-ink text-white text-base font-bold">F✓</span>
        FitCheck
      </Link>
      <div className="w-full max-w-sm rounded-3xl bg-card border border-black/5 shadow-[0_12px_30px_-14px_rgba(74,47,24,0.35)] p-6">
        <h1 className="text-[24px] font-bold tracking-tight leading-tight">{title}</h1>
        {subtitle && <p className="text-sm text-black/55 mt-1">{subtitle}</p>}
        <div className="mt-5">{children}</div>
      </div>
      {footer && <div className="mt-5 text-sm text-black/60 text-center">{footer}</div>}
    </div>
  );
}

export function Field({ label, type = "text", value, onChange, autoComplete, placeholder, hint, autoFocus, name }: {
  label: string;
  type?: string;
  value: string;
  onChange: (v: string) => void;
  autoComplete?: string;
  placeholder?: string;
  hint?: string;
  autoFocus?: boolean;
  name?: string;
}) {
  const [show, setShow] = useState(false);
  const isPw = type === "password";
  return (
    <label className="block mb-3.5">
      <span className="block text-[13px] font-semibold text-black/70 mb-1.5">{label}</span>
      <span className="relative block">
        <input
          name={name}
          type={isPw && show ? "text" : type}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          autoComplete={autoComplete}
          placeholder={placeholder}
          autoFocus={autoFocus}
          required
          className={cn(
            "w-full h-12 rounded-xl border border-black/10 bg-white px-3.5 text-[15px] outline-none",
            "focus:border-accent focus:ring-2 focus:ring-accent/15",
            isPw && "pr-11",
          )}
        />
        {isPw && (
          <button
            type="button"
            onClick={() => setShow((s) => !s)}
            aria-label={show ? "Hide password" : "Show password"}
            className="absolute right-2 top-1/2 -translate-y-1/2 grid place-items-center size-8 rounded-full text-black/45 hover:text-black/75"
          >
            {show ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
          </button>
        )}
      </span>
      {hint && <span className="block text-xs text-black/50 mt-1">{hint}</span>}
    </label>
  );
}

export function Notice({ tone = "info", children }: { tone?: "info" | "error" | "success"; children: React.ReactNode }) {
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={cn(
        "rounded-2xl text-sm px-4 py-3 mb-4",
        tone === "error" && "border border-rose-200 bg-rose-50 text-rose-800",
        tone === "success" && "border border-emerald-200 bg-emerald-50 text-emerald-800",
        tone === "info" && "border border-black/5 bg-sand text-black/75",
      )}
    >
      {children}
    </div>
  );
}

export const MIN_PASSWORD = 10;

/** Client-side mirror of the backend rule (the backend is the one that enforces it). */
export function passwordHint(pw: string): string | null {
  if (pw.length < MIN_PASSWORD) return `At least ${MIN_PASSWORD} characters (${pw.length}/${MIN_PASSWORD}).`;
  if (new Set(pw).size < 5) return "Mix in a few more different characters.";
  return null;
}

/** Only same-site relative paths are allowed as post-login destinations. */
export function safeNext(next: string | null | undefined): string {
  return next && next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/\\") ? next : "/";
}

export function GoogleButton({ onClick, loading }: { onClick: () => void; loading?: boolean }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={loading}
      className="w-full h-12 rounded-full bg-white border border-black/10 hover:bg-black/[0.03] font-semibold text-[15px] flex items-center justify-center gap-2.5 disabled:opacity-50"
    >
      <svg viewBox="0 0 48 48" className="size-5" aria-hidden>
        <path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.3 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.4-.4-3.5z" />
        <path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z" />
        <path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.3 0-9.7-3.3-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z" />
        <path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C37 39.2 44 34 44 24c0-1.3-.1-2.4-.4-3.5z" />
      </svg>
      Continue with Google
    </button>
  );
}

export function Divider() {
  return (
    <div className="flex items-center gap-3 my-4 text-xs text-black/40">
      <span className="h-px flex-1 bg-black/10" />
      or
      <span className="h-px flex-1 bg-black/10" />
    </div>
  );
}
