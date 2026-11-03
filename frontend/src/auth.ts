/**
 * Auth.js (NextAuth v5) config: email + password (checked by the FastAPI backend: argon2id, lockout, rate limits,
 * verified email) and Google. Sessions are Auth.js's encrypted JWT in an httpOnly, Secure, SameSite=Lax cookie;
 * Auth.js adds its own CSRF token to sign-in / sign-out. The browser never sees a user id it could tamper with:
 * /api/auth/token mints a 5-minute backend token from this session (see lib/backendToken.ts).
 *
 * Env: AUTH_SECRET (required), AUTH_GOOGLE_ID + AUTH_GOOGLE_SECRET (optional; Google button hidden without them),
 * BACKEND_URL (server-to-server), INTERNAL_API_SECRET (shared with the backend), BACKEND_JWT_SECRET (shared).
 */
import NextAuth, { CredentialsSignin, type NextAuthConfig } from "next-auth";
import Credentials from "next-auth/providers/credentials";
import Google from "next-auth/providers/google";

export const googleEnabled = Boolean(process.env.AUTH_GOOGLE_ID && process.env.AUTH_GOOGLE_SECRET);

const BACKEND_URL = (process.env.BACKEND_URL || "http://localhost:8000").replace(/\/$/, "");

type BackendUser = { id: string; email: string; name: string | null; first_name: string | null; ver: number };

async function internal<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BACKEND_URL}/api/internal/auth/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Internal-Secret": process.env.INTERNAL_API_SECRET ?? "" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`auth backend ${res.status}`);
  return (await res.json()) as T;
}

class LoginError extends CredentialsSignin {
  constructor(code: string) {
    super();
    this.code = code; // "invalid" | "locked" | "unverified" | "rate_limited"
  }
}

const providers: NextAuthConfig["providers"] = [
  Credentials({
    credentials: { email: { type: "email" }, password: { type: "password" } },
    async authorize(creds, request) {
      const email = String(creds?.email ?? "").trim();
      const password = String(creds?.password ?? "");
      if (!email || !password || password.length > 256) throw new LoginError("invalid");
      const h = request?.headers;
      const ip = (h?.get("x-vercel-forwarded-for") || h?.get("x-real-ip") || h?.get("x-forwarded-for") || "")
        .split(",")[0]
        .trim();
      const r = await internal<{ ok: boolean; error?: string; user?: BackendUser }>("credentials", { email, password, ip });
      if (!r.ok || !r.user) throw new LoginError(r.error || "invalid");
      return { id: r.user.id, email: r.user.email, name: r.user.name, firstName: r.user.first_name, ver: r.user.ver };
    },
  }),
];
if (googleEnabled) providers.push(Google({ allowDangerousEmailAccountLinking: false }));

export const { handlers, auth, signIn, signOut } = NextAuth({
  providers,
  trustHost: true,
  session: { strategy: "jwt", maxAge: 30 * 24 * 3600, updateAge: 24 * 3600 },
  pages: { signIn: "/login", error: "/login" },
  callbacks: {
    async signIn({ account, profile, user }) {
      if (account?.provider !== "google") return true;
      if (!profile?.email || !profile.email_verified) return "/login?error=GoogleEmail";
      try {
        const r = await internal<{ ok: boolean; user: BackendUser }>("oauth", {
          provider: "google",
          provider_account_id: account.providerAccountId,
          email: profile.email,
          email_verified: Boolean(profile.email_verified),
          name: profile.name ?? "",
          given_name: (profile as { given_name?: string }).given_name ?? "",
        });
        // hand the BACKEND user id to the jwt callback (Auth.js would otherwise use Google's sub)
        Object.assign(user, { id: r.user.id, firstName: r.user.first_name, ver: r.user.ver, name: r.user.name });
        return true;
      } catch {
        return "/login?error=Google";
      }
    },
    async jwt({ token, user }) {
      if (user) {
        const u = user as typeof user & { firstName?: string | null; ver?: number };
        token.uid = u.id;
        token.ver = u.ver ?? 0;
        token.firstName = u.firstName ?? null;
      }
      return token;
    },
    async session({ session, token }) {
      session.user.id = String(token.uid ?? "");
      session.user.firstName = (token.firstName as string | null) ?? null;
      session.ver = Number(token.ver ?? 0);
      return session;
    },
  },
});
