import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  useHide,
  useJobDetail,
  useMatch,
  useProfile,
  usePublish,
  useResearch,
  useTailoring,
} from "../hooks/queries";
import { formatDate, prettyLabel } from "../lib/format";
import { useToast } from "./Toast";
import { Chip, Logo, ScoreBadge, Spinner } from "./ui";
import type { JobDetail, MatchCategory } from "../types";

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
          <MetaRow label="Opportunity" value={prettyLabel(job.opportunity_type)} />
          <MetaRow label="Level" value={prettyLabel(job.applicant_level)} />
          <MetaRow label="Field" value={prettyLabel(job.academic_field)} />
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

        <MatchSection jobId={job.id} />

        <TailoringSection jobId={job.id} />

        <ResearchSection jobId={job.id} />

        <Description data={data} />
      </div>
    </div>
  );
}

const MATCH_META: Record<MatchCategory, { label: string; classes: string }> = {
  strong: { label: "Strong match", classes: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400" },
  good: { label: "Good match", classes: "bg-sky-500/15 text-sky-600 dark:text-sky-400" },
  stretch: { label: "Stretch", classes: "bg-amber-500/15 text-amber-600 dark:text-amber-400" },
  unlikely: { label: "Unlikely", classes: "bg-rose-500/15 text-rose-600 dark:text-rose-400" },
};

function MatchList({ title, items, tone = "ink" }: { title: string; items: string[]; tone?: string }) {
  if (!items?.length) return null;
  const color = tone === "rose" ? "text-rose-500" : tone === "emerald" ? "text-emerald-600 dark:text-emerald-400" : "text-ink";
  return (
    <div className="space-y-1">
      <h5 className="text-xs font-semibold uppercase tracking-wide text-ink-subtle">{title}</h5>
      <ul className="space-y-1 text-sm">
        {items.map((t, i) => (
          <li key={i} className={`flex gap-1.5 ${color}`}>
            <span className="text-ink-subtle">·</span>
            <span>{t}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function MatchSection({ jobId }: { jobId: string }) {
  const profile = useProfile();
  const [analyze, setAnalyze] = useState(false);
  const { data, isFetching, isError } = useMatch(jobId, analyze);

  // Reset the "analyze" trigger when switching jobs.
  useEffect(() => setAnalyze(false), [jobId]);

  const hasProfile = profile.data?.exists;

  return (
    <div className="rounded-xl border border-border bg-surface-sunken/40 p-4">
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-sm font-semibold text-ink">Your fit</h4>
        {data?.available && data.match && (
          <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${MATCH_META[data.match.category].classes}`}>
            {MATCH_META[data.match.category].label}
          </span>
        )}
      </div>

      {!hasProfile ? (
        <p className="mt-2 text-sm text-ink-subtle">
          <Link to="/profile" className="text-accent hover:underline">Upload your résumé</Link>{" "}
          to see how you fit this opportunity.
        </p>
      ) : !analyze ? (
        <button onClick={() => setAnalyze(true)}
          className="mt-2 rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-accent-ink">
          Analyze my fit
        </button>
      ) : isFetching ? (
        <div className="mt-3 flex items-center gap-2 text-sm text-ink-subtle">
          <Spinner /> Analyzing against your profile — the self-hosted model can take a moment…
        </div>
      ) : isError || !data?.available ? (
        <p className="mt-2 text-sm text-ink-subtle">
          {data?.reason === "llm_unavailable"
            ? "The LLM is offline right now — try again shortly."
            : "Could not analyze this one."}
        </p>
      ) : data.match ? (
        <div className="mt-3 space-y-3">
          {data.match.summary && <p className="text-sm text-ink">{data.match.summary}</p>}
          <div className="flex flex-wrap gap-3 text-xs text-ink-subtle">
            <span>Level {data.match.level_compatible ? "✓" : "✗"}</span>
            <span>Language {data.match.language_compatible ? "✓" : "✗"}</span>
            <span>Confidence: {data.match.confidence}</span>
          </div>
          <MatchList title="Strong matches" items={data.match.strong_matches} tone="emerald" />
          <MatchList title="Partial matches" items={data.match.partial_matches} />
          <MatchList title="Missing / gaps" items={data.match.missing_requirements} tone="rose" />
          <MatchList title="Emphasize when applying" items={data.match.suggested_emphasis} />
          <MatchList title="Concerns" items={data.match.concerns} tone="rose" />
        </div>
      ) : null}
    </div>
  );
}

function TailoringSection({ jobId }: { jobId: string }) {
  const profile = useProfile();
  const toast = useToast();
  const [tailor, setTailor] = useState(false);
  const [dismissed, setDismissed] = useState<Set<number>>(new Set());
  const { data, isFetching, isError } = useTailoring(jobId, tailor);

  useEffect(() => {
    setTailor(false);
    setDismissed(new Set());
  }, [jobId]);

  const hasProfile = profile.data?.exists;
  const plan = data?.plan;

  function copySummary() {
    if (plan?.tailored_summary) {
      navigator.clipboard.writeText(plan.tailored_summary);
      toast("Tailored summary copied.", "success");
    }
  }

  function toggle(i: number) {
    setDismissed((prev) => {
      const next = new Set(prev);
      next.has(i) ? next.delete(i) : next.add(i);
      return next;
    });
  }

  return (
    <div className="rounded-xl border border-border bg-surface-sunken/40 p-4">
      <h4 className="text-sm font-semibold text-ink">Tailor for this role</h4>

      {!hasProfile ? (
        <p className="mt-2 text-sm text-ink-subtle">
          <Link to="/profile" className="text-accent hover:underline">Upload your résumé</Link>{" "}
          to get tailoring suggestions.
        </p>
      ) : !tailor ? (
        <button onClick={() => setTailor(true)}
          className="mt-2 rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-accent-ink">
          Suggest tailoring
        </button>
      ) : isFetching ? (
        <div className="mt-3 flex items-center gap-2 text-sm text-ink-subtle">
          <Spinner /> Drafting suggestions from your profile…
        </div>
      ) : isError || !data?.available ? (
        <p className="mt-2 text-sm text-ink-subtle">
          {data?.reason === "llm_unavailable"
            ? "The LLM is offline right now — try again shortly."
            : "Could not draft suggestions."}
        </p>
      ) : plan ? (
        <div className="mt-3 space-y-4">
          {plan.tailored_summary && (
            <div className="space-y-1">
              <div className="flex items-center justify-between">
                <h5 className="text-xs font-semibold uppercase tracking-wide text-ink-subtle">
                  Tailored summary
                </h5>
                <button onClick={copySummary} className="text-xs text-accent hover:underline">Copy</button>
              </div>
              <p className="rounded-lg bg-surface-raised p-2.5 text-sm text-ink">{plan.tailored_summary}</p>
            </div>
          )}

          <MatchList title="Emphasize" items={plan.emphasize} tone="emerald" />

          {plan.suggestions?.length > 0 && (
            <div className="space-y-1.5">
              <h5 className="text-xs font-semibold uppercase tracking-wide text-ink-subtle">
                Suggestions (accept or dismiss)
              </h5>
              {plan.suggestions.map((s, i) => (
                <div key={i} className={`rounded-lg border border-border p-2.5 text-sm ${dismissed.has(i) ? "opacity-40" : ""}`}>
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-[11px] font-medium uppercase text-accent">{s.kind}{s.section ? ` · ${s.section}` : ""}</span>
                    <button onClick={() => toggle(i)} className="shrink-0 text-xs text-ink-subtle hover:text-ink">
                      {dismissed.has(i) ? "Restore" : "Dismiss"}
                    </button>
                  </div>
                  {s.suggested && <p className="mt-1 text-ink">{s.suggested}</p>}
                  {s.rationale && <p className="mt-0.5 text-xs text-ink-subtle">Why: {s.rationale}</p>}
                </div>
              ))}
            </div>
          )}

          {plan.do_not_fabricate?.length > 0 && (
            <div className="rounded-lg bg-rose-500/10 p-2.5">
              <MatchList title="Don't fabricate — real gaps" items={plan.do_not_fabricate} tone="rose" />
            </div>
          )}
          <MatchList title="Keyword cautions" items={plan.keyword_cautions} />
          <p className="text-xs text-ink-subtle">
            These only rearrange and reword what your profile already contains — nothing here is invented.
          </p>
        </div>
      ) : null}
    </div>
  );
}

function ResearchSection({ jobId }: { jobId: string }) {
  const { data, isFetching } = useResearch(jobId);

  // Auto-loads; stay quiet until there's something to show (no institution match
  // is common and shouldn't clutter the panel).
  if (isFetching) {
    return (
      <div className="flex items-center gap-2 text-xs text-ink-subtle">
        <Spinner /> Looking up the research group…
      </div>
    );
  }
  if (!data?.available || !data.institution) return null;
  const inst = data.institution;
  const works = data.recent_works ?? [];

  return (
    <div className="rounded-xl border border-border bg-surface-sunken/40 p-4">
      <h4 className="text-sm font-semibold text-ink">Research group</h4>
      <p className="mt-1 text-sm text-ink-muted">
        {inst.homepage_url || inst.openalex_url ? (
          <a href={inst.homepage_url || inst.openalex_url || "#"} target="_blank" rel="noreferrer"
            className="text-accent hover:underline">
            {inst.display_name}
          </a>
        ) : (
          inst.display_name
        )}
        {inst.country_code && <span className="text-ink-subtle"> · {inst.country_code}</span>}
        {inst.works_count != null && (
          <span className="text-ink-subtle"> · {inst.works_count.toLocaleString()} works</span>
        )}
        <span className="text-ink-subtle"> · via OpenAlex</span>
      </p>

      {inst.research_areas.length > 0 && (
        <div className="mt-2">
          <Chips items={inst.research_areas} />
        </div>
      )}

      {works.length > 0 && (
        <div className="mt-3 space-y-2">
          <h5 className="text-xs font-semibold uppercase tracking-wide text-ink-subtle">
            Recent papers {data.field_filtered ? "in this field" : ""} — worth a skim before applying
          </h5>
          <ul className="space-y-1.5 text-sm">
            {works.map((w, i) => (
              <li key={i}>
                {w.url ? (
                  <a href={w.url} target="_blank" rel="noreferrer" className="text-ink hover:text-accent hover:underline">
                    {w.title}
                  </a>
                ) : (
                  <span className="text-ink">{w.title}</span>
                )}
                {w.year && <span className="text-ink-subtle"> ({w.year})</span>}
                {w.authors.length > 0 && (
                  <span className="text-ink-subtle"> — {w.authors.slice(0, 3).join(", ")}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function Chips({ items }: { items: string[] }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((t, i) => (
        <Chip key={`${t}-${i}`}>{t}</Chip>
      ))}
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
