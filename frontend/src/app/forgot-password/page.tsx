"use client";

import Link from "next/link";
import { useState } from "react";
import { Button } from "@/components/ui";
import { AuthFrame, Field, Notice } from "@/components/AuthUI";
import { accountApi } from "@/lib/api";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: "success" | "error"; text: string } | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const r = await accountApi.forgotPassword(email);
      setMsg({ tone: "success", text: r.message });
    } catch (e) {
      setMsg({ tone: "error", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthFrame title="Reset your password" subtitle="We'll email you a link to choose a new one."
      footer={<Link href="/login" className="font-semibold text-ink underline underline-offset-2">Back to sign in</Link>}>
      {msg && <Notice tone={msg.tone}>{msg.text}</Notice>}
      <form onSubmit={submit}>
        <Field label="Email" type="email" name="email" value={email} onChange={setEmail} autoComplete="email" autoFocus />
        <Button type="submit" size="lg" loading={busy}>
          Send reset link
        </Button>
      </form>
    </AuthFrame>
  );
}
