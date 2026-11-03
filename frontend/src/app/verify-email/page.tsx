"use client";

import Link from "next/link";
import { Suspense, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Spinner } from "@/components/ui";
import { AuthFrame, Notice } from "@/components/AuthUI";
import { accountApi } from "@/lib/api";

function Verify() {
  const token = useSearchParams().get("token") ?? "";
  const [state, setState] = useState<"working" | "ok" | "error">(token ? "working" : "error");
  const [msg, setMsg] = useState("This link is missing its code.");
  const ran = useRef(false);

  useEffect(() => {
    if (!token || ran.current) return;
    ran.current = true; // single-use token: never post it twice (StrictMode)
    accountApi
      .verifyEmail(token)
      .then(() => setState("ok"))
      .catch((e) => {
        setMsg((e as Error).message);
        setState("error");
      });
  }, [token]);

  return (
    <AuthFrame title={state === "ok" ? "Email confirmed" : state === "error" ? "Couldn't confirm" : "Confirming…"}>
      {state === "working" && (
        <div className="flex justify-center py-4">
          <Spinner />
        </div>
      )}
      {state === "error" && <Notice tone="error">{msg}</Notice>}
      {state === "ok" && <Notice tone="success">Thanks! Your email is confirmed.</Notice>}
      {state !== "working" && (
        <Link href="/login" className="inline-flex items-center justify-center w-full h-14 rounded-full bg-ink text-white font-semibold">
          Go to sign in
        </Link>
      )}
    </AuthFrame>
  );
}

export default function VerifyEmailPage() {
  return (
    <Suspense>
      <Verify />
    </Suspense>
  );
}
