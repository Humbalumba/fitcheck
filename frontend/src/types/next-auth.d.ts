import type { DefaultSession } from "next-auth";

declare module "next-auth" {
  interface Session {
    ver?: number;
    user: { id: string; firstName?: string | null } & DefaultSession["user"];
  }
}
