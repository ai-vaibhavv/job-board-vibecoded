/** Human date helpers for the UI. All inputs are ISO strings or null. */

export function formatDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export function relativeTime(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const secs = Math.round((Date.now() - d.getTime()) / 1000);
  const table: [number, Intl.RelativeTimeFormatUnit][] = [
    [60, "second"],
    [3600, "minute"],
    [86400, "hour"],
    [2592000, "day"],
    [31536000, "month"],
    [Infinity, "year"],
  ];
  const divisors = [1, 60, 3600, 86400, 2592000, 31536000];
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  for (let i = 0; i < table.length; i++) {
    if (Math.abs(secs) < table[i][0]) {
      return rtf.format(-Math.round(secs / divisors[i]), table[i][1]);
    }
  }
  return "";
}

// Acronyms kept as-is when a taxonomy value is turned into a display label.
const LABEL_ACRONYMS: Record<string, string> = {
  ml: "ML",
  ai: "AI",
  nlp: "NLP",
  hiwi: "HiWi",
  ee: "EE",
  me: "ME",
  phd: "PhD",
};

/** Taxonomy value ("master_thesis", "ml") → display label ("Master Thesis", "ML"). */
export function prettyLabel(value: string | null | undefined): string {
  if (!value) return "";
  return value
    .split("_")
    .map((w) => LABEL_ACRONYMS[w] ?? w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

/** Status → display label + tailwind classes for the status pill. */
export const STATUS_META: Record<string, { label: string; classes: string }> = {
  new: { label: "New", classes: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400" },
  notified: { label: "Sent", classes: "bg-sky-500/15 text-sky-600 dark:text-sky-400" },
  rejected: { label: "Filtered", classes: "bg-rose-500/15 text-rose-600 dark:text-rose-400" },
};
