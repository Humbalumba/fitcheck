/** Short-lived backend token for the signed-in user (the FastAPI backend verifies it on every /api call). */
import { SignJWT } from "jose";
import { auth } from "@/auth";

export const dynamic = "force-dynamic";
const TTL_S = 300;

export async function GET() {
  const session = await auth();
  const uid = session?.user?.id;
  if (!session || !uid) return Response.json({ error: "unauthenticated" }, { status: 401, headers: NO_STORE });
  const secret = process.env.BACKEND_JWT_SECRET ?? "";
  if (secret.length < 32) return Response.json({ error: "server auth not configured" }, { status: 500, headers: NO_STORE });
  const ver = session.ver ?? 0;
  const now = Math.floor(Date.now() / 1000);
  const token = await new SignJWT({ ver })
    .setProtectedHeader({ alg: "HS256", typ: "JWT" })
    .setSubject(uid)
    .setIssuer("fitcheck-web")
    .setAudience("fitcheck-api")
    .setIssuedAt(now)
    .setExpirationTime(now + TTL_S)
    .sign(new TextEncoder().encode(secret));
  return Response.json({ token, expires_at: now + TTL_S }, { headers: NO_STORE });
}

const NO_STORE = { "Cache-Control": "no-store, private" };
