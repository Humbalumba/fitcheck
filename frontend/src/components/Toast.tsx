"use client";

import { createContext, useCallback, useContext, useState } from "react";
import { CheckCircle2, AlertTriangle } from "lucide-react";

type Toast = { id: number; msg: string; kind: "ok" | "error" };
const Ctx = createContext<(msg: string, kind?: "ok" | "error") => void>(() => {});

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((msg: string, kind: "ok" | "error" = "ok") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, msg, kind }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === "error" ? 5000 : 2800);
  }, []);
  return (
    <Ctx.Provider value={push}>
      {children}
      <div className="fixed inset-x-0 bottom-24 z-[60] flex flex-col items-center gap-2 px-4 pointer-events-none">
        {toasts.map((t) => (
          <div
            key={t.id}
            className="pop pointer-events-auto flex items-center gap-2 rounded-full bg-ink text-white px-4 py-2.5 text-sm shadow-lg max-w-md"
          >
            {t.kind === "ok" ? (
              <CheckCircle2 className="size-4 text-emerald-400 shrink-0" />
            ) : (
              <AlertTriangle className="size-4 text-amber-400 shrink-0" />
            )}
            <span>{t.msg}</span>
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}

export const useToast = () => useContext(Ctx);
