"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { CircleUser, Shirt, ShoppingBag, type LucideIcon } from "lucide-react";
import { ToastProvider } from "./Toast";
import { cn } from "@/lib/format";

type Tab = { href: string; label: string; icon: LucideIcon; match: string[] };

const CLOSET: Tab = { href: "/closet", label: "Closet", icon: Shirt, match: ["/closet", "/add"] };
const BUY: Tab = { href: "/buy", label: "Should I buy?", icon: ShoppingBag, match: ["/buy"] };
const PROFILE: Tab = { href: "/profile", label: "Profile", icon: CircleUser, match: ["/profile", "/preferences", "/settings"] };

const isActive = (t: Tab, pathname: string | null) => !!pathname && t.match.some((m) => pathname.startsWith(m));

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  // The landing page is a full-screen intro with no header or nav.
  if (pathname === "/") return <ToastProvider>{children}</ToastProvider>;
  return (
    <ToastProvider>
      <div className="min-h-dvh flex flex-col">
        <AppHeader />
        <main className={MAIN_CLS}>{children}</main>
        <MainNav pathname={pathname} />
      </div>
    </ToastProvider>
  );
}

/** Classes of the page body under the header (also used by the landing intro's closet preview). */
export const MAIN_CLS = "flex-1 mx-auto w-full max-w-2xl lg:max-w-6xl px-4 lg:px-8 pt-4 lg:pt-6 main-nav-pad";

export function AppHeader() {
  return (
    <header className="sticky top-0 z-30 bg-paper/85 backdrop-blur border-b border-black/5">
      <div className="mx-auto max-w-2xl lg:max-w-6xl px-4 lg:px-8 h-14 flex items-center">
        <Link href="/" aria-label="FitCheck home" className="flex items-center gap-2 font-semibold tracking-tight text-[17px]">
          <span className="grid place-items-center size-8 rounded-xl bg-ink text-white text-sm font-bold">F✓</span>
          FitCheck
        </Link>
      </div>
    </header>
  );
}

/** Floating bottom nav. It is position:fixed, so inside a transformed/contained box it pins to that box instead. */
export function MainNav({ pathname, className }: { pathname: string | null; className?: string }) {
  return (
    <nav
      aria-label="Main"
      className={cn("fixed bottom-0 inset-x-0 z-40 flex justify-center px-5 nav-float-pad pointer-events-none", className)}
    >
      <div className="pointer-events-auto flex items-center gap-2.5 p-1.5 rounded-full bg-white shadow-[0_12px_30px_-10px_rgba(74,47,24,0.35),0_2px_8px_rgba(74,47,24,0.08)] ring-1 ring-black/[0.04]">
        <SideTab tab={CLOSET} active={isActive(CLOSET, pathname)} />
        <MiddleTab tab={BUY} active={isActive(BUY, pathname)} />
        <SideTab tab={PROFILE} active={isActive(PROFILE, pathname)} />
      </div>
    </nav>
  );
}

function SideTab({ tab, active }: { tab: Tab; active: boolean }) {
  const Icon = tab.icon;
  return (
    <Link
      href={tab.href}
      aria-label={tab.label}
      aria-current={active ? "page" : undefined}
      title={tab.label}
      className={cn(
        "grid place-items-center size-12 rounded-full transition-colors duration-200",
        active ? "bg-[#efe9e2] text-ink" : "text-black/40 hover:text-black/70 hover:bg-black/[0.03]",
      )}
    >
      <Icon className="size-[22px]" strokeWidth={active ? 2.2 : 1.9} />
    </Link>
  );
}

function MiddleTab({ tab, active }: { tab: Tab; active: boolean }) {
  const Icon = tab.icon;
  return (
    <Link
      href={tab.href}
      aria-label={tab.label}
      aria-current={active ? "page" : undefined}
      title={tab.label}
      className={cn(
        "grid place-items-center size-12 rounded-full bg-ink text-white transition duration-200 active:scale-95",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 focus-visible:ring-offset-2",
        active ? "ring-[3px] ring-[#e3d6c6]" : "hover:bg-black",
      )}
    >
      <Icon className="size-[22px]" strokeWidth={active ? 2.3 : 2} />
    </Link>
  );
}
