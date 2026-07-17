import type { JobSummary } from "../types";
import { prettyLabel, relativeTime, STATUS_META } from "../lib/format";
import { Chip, Logo, ScoreBadge } from "./ui";

export function JobCard({
  job,
  selected,
  onSelect,
}: {
  job: JobSummary;
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  const status = STATUS_META[job.status] ?? { label: job.status, classes: "bg-surface-sunken text-ink-muted" };
  const place = job.location || job.city || job.country || "Location unknown";

  return (
    <button
      type="button"
      onClick={() => onSelect(job.id)}
      aria-pressed={selected}
      className={`w-full rounded-xl border px-4 py-3.5 text-left transition ${
        selected
          ? "border-accent bg-accent/5 ring-1 ring-accent"
          : "border-border bg-surface-raised hover:border-accent/40 hover:bg-surface-sunken/40"
      }`}
    >
      <div className="flex gap-3">
        <Logo src={job.logo} name={job.organization} size={44} />
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2">
            <h3 className="min-w-0 truncate text-sm font-semibold text-ink">{job.title}</h3>
            <ScoreBadge score={job.relevance_score} color={job.score_color} />
          </div>
          <p className="mt-0.5 truncate text-sm text-ink-muted">
            {job.organization || "Unknown organization"}
          </p>
          <p className="mt-0.5 truncate text-xs text-ink-subtle">
            {place}
            {job.remote_status !== "unknown" && ` · ${job.remote_status.replace("_", "-")}`}
          </p>

          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${status.classes}`}>
              {status.label}
            </span>
            {job.opportunity_type && (
              <span className="rounded-full bg-accent/10 px-2 py-0.5 text-[11px] font-medium text-accent">
                {prettyLabel(job.opportunity_type)}
              </span>
            )}
            {job.hidden && (
              <span className="rounded-full bg-surface-sunken px-2 py-0.5 text-[11px] text-ink-subtle">
                Hidden
              </span>
            )}
            <Chip muted>{job.source}</Chip>
            {job.language === "de" && <Chip muted>DE</Chip>}
            {job.matched_keywords.slice(0, 2).map((k) => (
              <Chip key={k}>{k}</Chip>
            ))}
            <span className="ml-auto text-[11px] text-ink-subtle">
              {relativeTime(job.published_at || job.notified_at)}
            </span>
          </div>
        </div>
      </div>
    </button>
  );
}
