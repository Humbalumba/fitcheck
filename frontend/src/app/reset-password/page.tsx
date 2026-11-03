"use client";

import Link from "next/link";
import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Button } from "@/components/ui";
import { AuthFrame, Field, Notice, passwordHint } from "@/components/AuthUI";
import { accountApi } from "@/lib/api";

function ResetForm() {
  const token = useSearchParams().get("token") ?? "";
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const h = passwordHint(pw);
    if (h) return setErr(h);
    if (pw !== pw2) return setErr("The two passwords don't match.");
    setBusy(true);
    setErr(null);
    try {
      await accountApi.resetPassword(token, pw);
      setDone(true);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!token)
    return (
      <AuthFrame title="Reset link missing" footer={<Link href="/forgot-password" className="font-semibold underline">Request a new link</Link>}>
        <Notice tone="error">Open the link from your email again.</Notice>
      </AuthFrame>
    );
  if (done)
    return (
      <AuthFrame title="Password updated" footer={null}>
        <Notice tone="success">You&apos;re all set. You&apos;ve been signed out everywhere else.</Notice>
        <Link href="/login" className="inline-flex items-center justify-center w-full h-14 rounded-full bg-ink text-white font-semibold">
          Sign in
        </Link>
      </AuthFrame>
    );
  return (
    <AuthFrame title="Choose a new password">
      {err && <Notice tone="error">{err}</Notice>}
      <form onSubmit={submit}>
        <Field label="New password" type="password" value={pw} onChange={setPw} autoComplete="new-password" autoFocus
          hint={pw ? passwordHint(pw) ?? "Looks good." : "At least 10 characters."} />
        <Field label="Repeat password" type="password" value={pw2} onChange={setPw2} autoComplete="new-password" />
        <Button type="submit" size="lg" loading={busy}>
          Save password
        </Button>
      </form>
    </AuthFrame>
  );
}

export default function ResetPasswordPage() {
  return (
    <Suspense>
      <ResetForm />
    </Suspense>
  );
}
