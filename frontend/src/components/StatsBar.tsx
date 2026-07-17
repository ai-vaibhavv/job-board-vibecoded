import type { Stats } from "../types";

function Stat({
  label,
  value,
  tone = "default",
}: {
  label: string;
  value: string | number;
  tone?: "default" | "new" | "sent" | "rejected";
}) {
  const toneClass =
    tone === "new"
      ? "text-emerald-600 dark:text-emerald-400"
      : tone === "sent"
        ? "text-sky-600 dark:text-sky-400"
        : tone === "rejected"
          ? "text-rose-600 dark:text-rose-400"
          : "text-ink";
  return (
    <div className="flex flex-col">
      <span className={`text-lg font-bold leading-none ${toneClass}`}>{value}</span>
      <span className="mt-1 text-[10px] uppercase tracking-wide text-ink-subtle">{label}</span>
    </div>
  );
}

export function StatsBar({ stats }: { stats: Stats | undefined }) {
  if (!stats) return null;
  return (
    <div className="flex items-center gap-6">
      <Stat label="Total" value={stats.total_jobs} />
      <Stat label="New" value={stats.by_status.new ?? 0} tone="new" />
      <Stat label="Sent" value={stats.by_status.notified ?? 0} tone="sent" />
      <Stat label="Filtered" value={stats.by_status.rejected ?? 0} tone="rejected" />
      <Stat label="Avg score" value={stats.average_score ?? "—"} />
    </div>
  );
}
