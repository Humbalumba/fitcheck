"use client";

import Link from "next/link";
import { useState } from "react";
import { signIn } from "next-auth/react";
import { Button } from "@/components/ui";
import { AuthFrame, Divider, Field, GoogleButton, Notice, passwordHint } from "@/components/AuthUI";
import { accountApi } from "@/lib/api";

export function SignupForm({ google }: { google: boolean }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const hint = password ? passwordHint(password) : `At least 10 characters.`;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (passwordHint(password)) {
      setErr(passwordHint(password));
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await accountApi.signup(email, password, name);
      setDone(true);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (done)
    return (
      <AuthFrame title="Check your email" subtitle={`We sent a confirmation link to ${email}.`}
        footer={<Link href="/login" className="font-semibold text-ink underline underline-offset-2">Back to sign in</Link>}>
        <Notice>Open the link to confirm your email, then sign in. It can take a minute to arrive; check spam too.</Notice>
      </AuthFrame>
    );

  return (
    <AuthFrame
      title="Create your account"
      subtitle="Build your closet and find out what's worth buying."
      footer={
        <>
          Already have an account?{" "}
          <Link href="/login" className="font-semibold text-ink underline underline-offset-2">
            Sign in
          </Link>
        </>
      }
    >
      {err && <Notice tone="error">{err}</Notice>}
      {google && (
        <>
          <GoogleButton onClick={() => signIn("google", { redirectTo: "/" })} />
          <Divider />
        </>
      )}
      <form onSubmit={submit}>
        <Field label="First name" name="name" value={name} onChange={setName} autoComplete="given-name" autoFocus />
        <Field label="Email" type="email" name="email" value={email} onChange={setEmail} autoComplete="email" />
        <Field label="Password" type="password" name="password" value={password} onChange={setPassword}
          autoComplete="new-password" hint={hint ?? "Looks good."} />
        <Button type="submit" size="lg" loading={busy} className="mt-1">
          Create account
        </Button>
      </form>
    </AuthFrame>
  );
}
