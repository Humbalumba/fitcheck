"use client";

import Link from "next/link";
import { useState } from "react";
import { signIn } from "next-auth/react";
import { Button } from "@/components/ui";
import { AuthFrame, Divider, Field, GoogleButton, Notice, safeNext } from "@/components/AuthUI";
import { accountApi, clearApiToken } from "@/lib/api";

const MESSAGES: Record<string, string> = {
  invalid: "That email and password don't match.",
  locked: "Too many wrong passwords. Your account is locked for 15 minutes, or reset your password now.",
  unverified: "Please confirm your email first. Check your inbox for the link.",
  rate_limited: "Too many sign-in attempts. Please wait a few minutes.",
  Google: "Google sign-in didn't work. Please try again.",
  GoogleEmail: "Your Google account's email isn't verified.",
  OAuthAccountNotLinked: "Sign in with your email and password for this account.",
  AccessDenied: "Sign-in was cancelled.",
  Configuration: "Sign-in isn't set up correctly on the server.",
};

export function LoginForm({ google, next, error }: { google: boolean; next: string | null; error: string | null }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState<string | null>(error);
  const [resent, setResent] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setCode(null);
    setResent(null);
    try {
      const r = await signIn("credentials", { email, password, redirect: false });
      if (r?.error) {
        setCode(r.code && MESSAGES[r.code] ? r.code : "invalid");
        return;
      }
      clearApiToken();
      window.location.assign(safeNext(next));
    } catch {
      setCode("invalid");
    } finally {
      setBusy(false);
    }
  };

  const resend = async () => {
    try {
      const r = await accountApi.resendVerification(email);
      setResent(r.message);
    } catch (e) {
      setResent((e as Error).message);
    }
  };

  return (
    <AuthFrame
      title="Welcome back"
      subtitle="Sign in to your closet."
      footer={
        <>
          New to FitCheck?{" "}
          <Link href="/signup" className="font-semibold text-ink underline underline-offset-2">
            Create an account
          </Link>
        </>
      }
    >
      {code && (
        <Notice tone="error">
          {MESSAGES[code] ?? "Couldn't sign you in. Please try again."}
          {code === "unverified" && email && (
            <button type="button" onClick={resend} className="block mt-1.5 font-semibold underline underline-offset-2">
              Send a new confirmation link
            </button>
          )}
        </Notice>
      )}
      {resent && <Notice tone="success">{resent}</Notice>}
      {google && (
        <>
          <GoogleButton onClick={() => signIn("google", { redirectTo: safeNext(next) })} />
          <Divider />
        </>
      )}
      <form onSubmit={submit} noValidate={false}>
        <Field label="Email" type="email" name="email" value={email} onChange={setEmail} autoComplete="email" autoFocus />
        <Field label="Password" type="password" name="password" value={password} onChange={setPassword} autoComplete="current-password" />
        <div className="flex justify-end -mt-1 mb-4">
          <Link href="/forgot-password" className="text-sm font-semibold text-black/60 hover:text-ink">
            Forgot password?
          </Link>
        </div>
        <Button type="submit" size="lg" loading={busy}>
          Sign in
        </Button>
      </form>
    </AuthFrame>
  );
}
