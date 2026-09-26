"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Shirt, Camera, ShoppingBag, SlidersHorizontal } from "lucide-react";
import { StatusIndicator } from "./StatusIndicator";
import { ToastProvider } from "./Toast";
import { cn } from "@/lib/format";

const TABS = [
  { href: "/closet", label: "Closet", icon: Shirt },
  { href: "/add", label: "Add", icon: Camera },
  { href: "/buy", label: "Should I buy?", icon: ShoppingBag },
  { href: "/settings", label: "Settings", icon: SlidersHorizontal },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  return (
    <ToastProvider>
      <div className="min-h-dvh flex flex-col">
        <header className="sticky top-0 z-30 bg-paper/85 backdrop-blur border-b border-black/5">
          <div className="mx-auto max-w-2xl px-4 h-14 flex items-center justify-between">
            <Link href="/closet" className="flex items-center gap-2 font-semibold tracking-tight text-[17px]">
              <span className="grid place-items-center size-8 rounded-xl bg-ink text-white text-sm font-bold">
                F✓
              </span>
              FitCheck
            </Link>
            <StatusIndicator />
          </div>
        </header>
        <main className="flex-1 mx-auto w-full max-w-2xl px-4 pt-4 pb-32">{children}</main>
        <nav className="fixed bottom-0 inset-x-0 z-40 bg-white/95 backdrop-blur border-t border-black/5 pb-safe">
          <div className="mx-auto max-w-2xl grid grid-cols-4">
            {TABS.map((t) => {
              const active = pathname?.startsWith(t.href);
              const Icon = t.icon;
              return (
                <Link
                  key={t.href}
                  href={t.href}
                  className={cn(
                    "flex flex-col items-center gap-1 pt-2.5 pb-2 text-[11px] font-medium transition-colors",
                    active ? "text-ink" : "text-black/40 hover:text-black/70",
                  )}
                >
                  <span
                    className={cn(
                      "grid place-items-center h-8 w-14 rounded-full transition-colors",
                      active && "bg-accent-soft text-accent",
                    )}
                  >
                    <Icon className="size-5" strokeWidth={active ? 2.4 : 2} />
                  </span>
                  {t.label}
                </Link>
              );
            })}
          </div>
        </nav>
      </div>
    </ToastProvider>
  );
}
