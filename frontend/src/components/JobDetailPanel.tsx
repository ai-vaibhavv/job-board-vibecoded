import { useEffect, useState } from "react";
import { useHide, useJobDetail, usePublish } from "../hooks/queries";
import { formatDate } from "../lib/format";
import { useToast } from "./Toast";
import { Chip, Logo, ScoreBadge, Spinner } from "./ui";
import type { JobDetail } from "../types";

function MetaRow({ label, value }: { label: string; value: string | null | undefined }) {
  if (!value) return null;
  return (
    <div className="flex gap-2 text-sm">
      <span className="w-28 shrink-0 text-ink-subtle">{label}</span>
      <span className="min-w-0 break-words text-ink">{value}</span>
    </div>
  );
}

export function JobDetailPanel({ jobId, onClose }: { jobId: string | null; onClose?: () => void }) {
  const { data, isLoading, isError } = useJobDetail(jobId);

  if (!jobId) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-center text-sm text-ink-subtle">
        Select a job to see its details.
      </div>
    );
  }
  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center text-ink-subtle">
        <Spinner className="h-6 w-6" />
      </div>
    );
  }
  if (isError || !data?.exists) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-center text-sm text-rose-500">
        Job not found.
      </div>
    );
  }

  return <DetailBody data={data} onClose={onClose} />;
}

function DetailBody({ data, onClose }: { data: JobDetail; onClose?: () => void }) {
  const job = data.job;
  const toast = useToast();
  const publish = usePublish();
  const hide = useHide();
  const [confirm, setConfirm] = useState(false);

  // Reset the confirm tick whenever a different job loads.
  useEffect(() => setConfirm(false), [job.id]);

  const place = job.location || job.city || job.country || null;

  function doPublish() {
    publish.mutate(
      { id: job.id, confirm },
      {
        onSuccess: (r) => toast(r.message, r.message.startsWith("✅") ? "success" : "info"),
        onError: (e) => toast(`Publish failed: ${(e as Error).message}`, "error"),
      },
    );
  }

  function doHide() {
    hide.mutate(job.id, {
      onSuccess: (r) => {
        toast(r.message, "info");
        onClose?.();
      },
      onError: () => toast("Could not hide.", "error"),
    });
  }

  return (
    <div className="scroll-thin flex h-full flex-col overflow-y-auto">
      {/* header */}
      <div className="sticky top-0 z-10 border-b border-border bg-surface-raised/95 p-5 backdrop-blur">
        <div className="flex items-start gap-3">
          <Logo src={job.logo} name={job.organization} size={48} />
          <div className="min-w-0 flex-1">
            <a
              href={job.url}
              target="_blank"
              rel="noreferrer"
              className="text-lg font-semibold leading-snug text-ink hover:text-accent hover:underline"
            >
              {job.title}
            </a>
            <p className="mt-0.5 text-sm text-ink-muted">{job.organization || "Unknown organization"}</p>
          </div>
          <ScoreBadge score={job.relevance_score} color={job.score_color} size="lg" />
          {onClose && (
            <button
              type="button"
              onClick={onClose}
              aria-label="Close details"
              className="ml-1 rounded-lg p-1.5 text-ink-subtle hover:bg-surface-sunken lg:hidden"
            >
              ✕
            </button>
          )}
        </div>

        <div className="mt-4 flex flex-wrap gap-2">
          <a
            href={job.url}
            target="_blank"
            rel="noreferrer"
            className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-ink transition hover:opacity-90"
          >
            View posting ↗
          </a>
          <button
            type="button"
            onClick={doPublish}
            disabled={publish.isPending || (data.needs_confirm && !confirm)}
            className="rounded-lg border border-border px-4 py-2 text-sm font-medium text-ink transition hover:bg-surface-sunken disabled:opacity-50"
          >
            {publish.isPending ? "Publishing…" : "📢 Publish to Discord"}
          </button>
          <button
            type="button"
            onClick={doHide}
            disabled={hide.isPending}
            className="rounded-lg border border-border px-4 py-2 text-sm font-medium text-ink-muted transition hover:bg-surface-sunken disabled:opacity-50"
          >
            🙈 Hide
          </button>
        </div>

        {data.needs_confirm && (
          <label className="mt-3 flex items-start gap-2 rounded-lg bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
            <input
              type="checkbox"
              checked={confirm}
              onChange={(e) => setConfirm(e.target.checked)}
              className="mt-0.5 h-4 w-4 accent-amber-500"
            />
            <span>{data.confirm_label}</span>
          </label>
        )}
      </div>

      {/* body */}
      <div className="space-y-5 p-5">
        <div className="space-y-1.5">
          <MetaRow label="Location" value={place} />
          <MetaRow label="Source" value={job.source} />
          <MetaRow label="Status" value={job.status} />
          <MetaRow label="Employment" value={job.employment_type} />
          <MetaRow label="Deadline" value={job.application_deadline ? formatDate(job.application_deadline) : null} />
          <MetaRow label="Apply to" value={job.contact_email} />
          <MetaRow label="Language" value={job.language} />
          <MetaRow label="Posted" value={job.published_at ? formatDate(job.published_at) : null} />
        </div>

        {job.matched_keywords.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {job.matched_keywords.slice(0, 10).map((k) => (
              <Chip key={k}>{k}</Chip>
            ))}
          </div>
        )}

        <Description data={data} />
      </div>
    </div>
  );
}

function Description({ data }: { data: JobDetail }) {
  const job = data.job;

  if (data.translation) {
    return (
      <div className="space-y-3">
        <h4 className="text-sm font-semibold text-ink">
          English translation
          {data.translation.truncated && <span className="font-normal text-ink-subtle"> (excerpt)</span>}
        </h4>
        <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink-muted">
          {data.translation.description_en}
        </p>
        <details className="group">
          <summary className="cursor-pointer text-xs text-ink-subtle hover:text-ink">Original German</summary>
          <p className="mt-2 whitespace-pre-wrap text-sm leading-relaxed text-ink-subtle">
            {job.description || "(no description)"}
          </p>
        </details>
      </div>
    );
  }

  if (data.translation_unavailable) {
    return (
      <div className="space-y-3">
        <p className="rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-600 dark:text-rose-400">
          ⚠️ English translation unavailable (LLM endpoint not reachable). Showing the original German.
        </p>
        <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink-muted">
          {job.description || "(no description)"}
        </p>
      </div>
    );
  }

  return (
    <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink-muted">
      {job.description || "(no description)"}
    </p>
  );
}
