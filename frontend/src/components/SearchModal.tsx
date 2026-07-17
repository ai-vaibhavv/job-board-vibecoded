import { useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../api/client";
import { useSearch } from "../hooks/useSearch";
import type { Meta } from "../types";
import { useToast } from "./Toast";
import { Spinner } from "./ui";
import { TagField } from "./TagField";

export function SearchModal({ meta, onClose }: { meta: Meta | undefined; onClose: () => void }) {
  const toast = useToast();
  const { preview, run, task, isRunning, reset } = useSearch();

  const [keywords, setKeywords] = useState("");
  const [topics, setTopics] = useState<string[]>([]);
  const [locations, setLocations] = useState<string[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);

  const resume = useMutation({
    mutationFn: (file: File) => api.resume(file),
    onSuccess: (r) => {
      if (r.keywords) setKeywords((k) => (k ? `${k}, ${r.keywords}` : r.keywords));
      if (r.topics.length) setTopics((t) => Array.from(new Set([...t, ...r.topics])));
      toast(r.message, "info");
    },
    onError: (e) => toast(`Resume parsing failed: ${(e as Error).message}`, "error"),
  });

  function close() {
    reset();
    onClose();
  }

  return (
    <div className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-black/50 p-4 backdrop-blur-sm">
      <div className="my-8 w-full max-w-2xl rounded-2xl border border-border bg-surface-raised shadow-2xl">
        <div className="flex items-center justify-between border-b border-border p-5">
          <h2 className="text-lg font-semibold text-ink">🔎 Search new jobs</h2>
          <button type="button" onClick={close} aria-label="Close" className="rounded-lg p-1.5 text-ink-subtle hover:bg-surface-sunken">
            ✕
          </button>
        </div>

        <div className="space-y-4 p-5">
          <p className="rounded-lg bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
            Runs the full pipeline across <strong>all sources, including the paid Apify one</strong>, and
            <strong> stores</strong> results — nothing is sent to Discord. Review and publish afterwards.
          </p>

          <div>
            <label className="mb-1 block text-xs font-semibold uppercase tracking-wide text-ink-subtle">
              Keywords (comma-separated)
            </label>
            <input
              value={keywords}
              onChange={(e) => setKeywords(e.target.value)}
              placeholder="reinforcement learning, computer vision"
              className="w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-ink outline-none focus:border-accent"
            />
          </div>

          <TagField
            label="Topics"
            values={topics}
            suggestions={meta?.topics ?? []}
            onChange={setTopics}
            placeholder="add a topic…"
          />
          <TagField
            label="Locations"
            values={locations}
            suggestions={meta?.locations ?? []}
            onChange={setLocations}
            placeholder="add a location…"
          />

          <div className="rounded-lg border border-dashed border-border p-3">
            <div className="flex items-center gap-3">
              <input ref={fileRef} type="file" accept=".pdf" className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) resume.mutate(f);
                  e.target.value = "";
                }}
              />
              <button
                type="button"
                onClick={() => fileRef.current?.click()}
                disabled={resume.isPending}
                className="rounded-lg border border-border px-3 py-1.5 text-sm text-ink transition hover:bg-surface-sunken disabled:opacity-50"
              >
                {resume.isPending ? "Reading…" : "Extract keywords from résumé (PDF)"}
              </button>
            </div>
            <p className="mt-2 text-[11px] text-ink-subtle">
              The résumé text is sent to the configured LLM endpoint (a public tunnel). Don't upload
              anything you wouldn't send there.
            </p>
          </div>

          {/* Preview + run */}
          <div className="border-t border-border pt-4">
            <button
              type="button"
              onClick={() => preview.mutate({ keywords, topics })}
              disabled={preview.isPending}
              className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-ink transition hover:opacity-90 disabled:opacity-50"
            >
              {preview.isPending ? "Preparing…" : "Prepare search"}
            </button>

            {preview.data && (
              <div className="mt-3 space-y-2">
                <pre className="scroll-thin max-h-32 overflow-auto whitespace-pre-wrap rounded-lg bg-surface-sunken p-3 text-xs text-ink-muted">
                  {preview.data.queries}
                </pre>
                <p className="text-xs text-ink-subtle">{preview.data.scope}</p>
                <button
                  type="button"
                  onClick={() => run.mutate({ keywords, topics, locations })}
                  disabled={isRunning}
                  className="inline-flex items-center gap-2 rounded-lg bg-rose-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-rose-500 disabled:opacity-60"
                >
                  {isRunning && <Spinner className="h-4 w-4" />}
                  {isRunning ? "Running…" : "▶ Confirm & run search"}
                </button>
              </div>
            )}

            {task && (
              <p
                className={`mt-3 rounded-lg px-3 py-2 text-xs ${
                  task.status === "error"
                    ? "bg-rose-500/10 text-rose-600 dark:text-rose-400"
                    : task.status === "done"
                      ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                      : "bg-surface-sunken text-ink-muted"
                }`}
              >
                {task.status === "running" && "Search running… this can take a minute."}
                {task.status === "done" && (task.result || "Done.")}
                {task.status === "error" && `Search failed: ${task.error}`}
              </p>
            )}
            {run.isError && (
              <p className="mt-3 rounded-lg bg-rose-500/10 px-3 py-2 text-xs text-rose-600 dark:text-rose-400">
                {(run.error as Error).message}
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
