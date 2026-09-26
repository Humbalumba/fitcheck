export function money(n: number | null | undefined, currency = "USD", digits?: number): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: currency || "USD",
      maximumFractionDigits: digits ?? (Math.abs(n) >= 100 ? 0 : 2),
      minimumFractionDigits: digits ?? 0,
    }).format(n);
  } catch {
    return `$${n.toFixed(2)}`;
  }
}

export function pct(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const v = n <= 1 ? n * 100 : n;
  return `${Math.round(v)}%`;
}

export function titleCase(s?: string | null): string {
  if (!s) return "";
  return s.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}
