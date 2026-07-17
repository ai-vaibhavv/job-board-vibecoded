import type { JobSummary } from "../types";
import { JobCard } from "./JobCard";
import { Spinner } from "./ui";

export function JobList({
  jobs,
  total,
  selectedId,
  onSelect,
  isLoading,
  isError,
}: {
  jobs: JobSummary[];
  total: number;
  selectedId: string | null;
  onSelect: (id: string) => void;
  isLoading: boolean;
  isError: boolean;
}) {
  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center text-ink-subtle">
        <Spinner className="h-6 w-6" />
      </div>
    );
  }
  if (isError) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-1 p-8 text-center">
        <p className="text-sm font-medium text-rose-500">Could not load jobs.</p>
        <p className="text-xs text-ink-subtle">Is the API running on port 7860?</p>
      </div>
    );
  }
  if (jobs.length === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-1 p-8 text-center">
        <p className="text-sm font-medium text-ink">No jobs match these filters.</p>
        <p className="text-xs text-ink-subtle">Try clearing the search or lowering the min score.</p>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-border px-4 py-2 text-xs font-medium text-ink-subtle">
        {total} {total === 1 ? "position" : "positions"}
      </div>
      <div className="scroll-thin flex-1 space-y-2 overflow-y-auto p-3">
        {jobs.map((job) => (
          <JobCard key={job.id} job={job} selected={job.id === selectedId} onSelect={onSelect} />
        ))}
      </div>
    </div>
  );
}
