import type { JobFilters, Meta } from "../types";
import { useUnhideAll } from "../hooks/queries";
import { useToast } from "./Toast";

const STATUSES = [
  { value: "all", label: "All" },
  { value: "new", label: "New" },
  { value: "notified", label: "Sent" },
  { value: "rejected", label: "Filtered" },
];

export function FilterSidebar({
  filters,
  onChange,
  meta,
}: {
  filters: JobFilters;
  onChange: (patch: Partial<JobFilters>) => void;
  meta: Meta | undefined;
}) {
  const toast = useToast();
  const unhideAll = useUnhideAll();

  return (
    <aside className="scroll-thin flex w-60 shrink-0 flex-col gap-6 overflow-y-auto border-r border-border bg-surface-raised/50 p-4">
      <div>
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-subtle">Status</h2>
        <div className="flex flex-wrap gap-1.5">
          {STATUSES.map((s) => (
            <button
              key={s.value}
              type="button"
              onClick={() => onChange({ status: s.value })}
              className={`rounded-full px-3 py-1 text-xs font-medium transition ${
                filters.status === s.value
                  ? "bg-accent text-accent-ink"
                  : "bg-surface-sunken text-ink-muted hover:text-ink"
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>
      </div>

      <div>
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-ink-subtle">Min score</h2>
          <span className="text-xs font-semibold text-ink">{filters.min_score}</span>
        </div>
        <input
          type="range"
          min={0}
          max={100}
          step={5}
          value={filters.min_score}
          onChange={(e) => onChange({ min_score: Number(e.target.value) })}
          className="w-full accent-accent"
        />
      </div>

      <div>
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-subtle">Source</h2>
        <select
          value={filters.source}
          onChange={(e) => onChange({ source: e.target.value })}
          className="w-full rounded-lg border border-border bg-surface px-3 py-1.5 text-sm text-ink outline-none focus:border-accent"
        >
          <option value="all">All sources</option>
          {meta?.sources.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </div>

      <label className="flex cursor-pointer items-center gap-2 text-sm text-ink-muted">
        <input
          type="checkbox"
          checked={filters.show_hidden}
          onChange={(e) => onChange({ show_hidden: e.target.checked })}
          className="h-4 w-4 rounded border-border accent-accent"
        />
        Show hidden jobs
      </label>

      <button
        type="button"
        onClick={() =>
          unhideAll.mutate(undefined, {
            onSuccess: (r) => toast(r.message, "info"),
            onError: () => toast("Could not unhide.", "error"),
          })
        }
        disabled={unhideAll.isPending}
        className="mt-auto rounded-lg border border-border px-3 py-1.5 text-xs text-ink-muted transition hover:bg-surface-sunken disabled:opacity-50"
      >
        Unhide all
      </button>
    </aside>
  );
}
