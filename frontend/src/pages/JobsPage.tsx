import { useMemo } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { FilterSidebar } from "../components/FilterSidebar";
import { JobList } from "../components/JobList";
import { JobDetailPanel } from "../components/JobDetailPanel";
import { StatsBar } from "../components/StatsBar";
import { useBoard } from "../components/AppShell";
import { useJobs } from "../hooks/queries";
import { useDebounced } from "../hooks/useDebounced";

export default function JobsPage() {
  const { filters, setFilters, meta, openSearch } = useBoard();
  const navigate = useNavigate();
  const { id } = useParams();
  const selectedId = id ?? null;

  const debouncedText = useDebounced(filters.text, 300);
  const effective = useMemo(() => ({ ...filters, text: debouncedText }), [filters, debouncedText]);
  const jobs = useJobs(effective);

  const select = (jobId: string) => navigate(`/jobs/${encodeURIComponent(jobId)}`);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Board toolbar */}
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3 border-b border-border bg-surface-raised/60 px-4 py-3">
        <StatsBar stats={meta?.stats} />
        <div className="ml-auto flex items-center gap-2">
          <div className="relative">
            <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-subtle">
              ⌕
            </span>
            <input
              value={filters.text}
              onChange={(e) => setFilters({ text: e.target.value })}
              placeholder="Search title, org, location…"
              className="w-60 rounded-lg border border-border bg-surface py-1.5 pl-8 pr-3 text-sm text-ink outline-none focus:border-accent"
            />
          </div>
          <button
            type="button"
            onClick={openSearch}
            className="rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-accent-ink transition hover:opacity-90"
          >
            🔎 Search new
          </button>
        </div>
      </div>

      <div className="flex min-h-0 flex-1">
        <FilterSidebar filters={filters} onChange={setFilters} meta={meta} />

        <main className="flex min-w-0 flex-1 flex-col border-r border-border lg:max-w-md xl:max-w-lg">
          <JobList
            jobs={jobs.data?.jobs ?? []}
            total={jobs.data?.total ?? 0}
            selectedId={selectedId}
            onSelect={select}
            isLoading={jobs.isLoading}
            isError={jobs.isError}
          />
        </main>

        <section className="hidden min-w-0 flex-1 bg-surface-raised lg:block">
          <JobDetailPanel jobId={selectedId} onClose={() => navigate("/")} />
        </section>

        {selectedId && (
          <div className="fixed inset-0 z-30 flex lg:hidden">
            <button
              type="button"
              aria-label="Close details"
              className="flex-1 bg-black/40"
              onClick={() => navigate("/")}
            />
            <div className="w-full max-w-md bg-surface-raised shadow-2xl">
              <JobDetailPanel jobId={selectedId} onClose={() => navigate("/")} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
