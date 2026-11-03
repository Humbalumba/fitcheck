import { googleEnabled } from "@/auth";
import { LoginForm } from "./LoginForm";

export const metadata = { title: "Sign in · FitCheck" };

export default async function LoginPage({ searchParams }: { searchParams: Promise<Record<string, string | undefined>> }) {
  const sp = await searchParams;
  return <LoginForm google={googleEnabled} next={sp.next ?? null} error={sp.error ?? null} />;
}
