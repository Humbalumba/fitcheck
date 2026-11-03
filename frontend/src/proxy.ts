/**
 * Page protection (Next 16 "proxy", formerly middleware): signed-out visitors only see the auth pages.
 * /api and /media are NOT matched here (the backend authenticates those itself, and the proxy would buffer uploads).
 */
import { NextResponse } from "next/server";
import { auth } from "@/auth";

const PUBLIC = ["/login", "/signup", "/forgot-password", "/reset-password", "/verify-email"];

export default auth((req) => {
  const { pathname, search } = req.nextUrl;
  const isPublic = PUBLIC.some((p) => pathname === p || pathname.startsWith(p + "/"));
  const signedIn = Boolean(req.auth?.user?.id);
  if (!signedIn && !isPublic) {
    const url = new URL("/login", req.nextUrl.origin);
    if (pathname !== "/") url.searchParams.set("next", pathname + search);
    return NextResponse.redirect(url);
  }
  if (signedIn && (pathname === "/login" || pathname === "/signup")) {
    return NextResponse.redirect(new URL("/", req.nextUrl.origin));
  }
  return NextResponse.next();
});

export const config = {
  matcher: ["/((?!api/|media/|_next/|favicon|icon|apple-icon|manifest|.*\\.(?:png|jpg|jpeg|svg|webp|ico|txt|xml|webmanifest)$).*)"],
};
