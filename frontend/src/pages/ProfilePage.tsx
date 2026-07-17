import { useRef, useState } from "react";
import {
  useDeleteProfile,
  useProfile,
  useUpdateProfile,
  useUploadProfile,
} from "../hooks/queries";
import { useToast } from "../components/Toast";
import { Chip, Spinner } from "../components/ui";
import type { AcademicProfile } from "../types";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-subtle">{title}</h3>
      {children}
    </div>
  );
}

function Chips({ items }: { items: string[] }) {
  if (!items?.length) return <p className="text-sm text-ink-subtle">—</p>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((t, i) => (
        <Chip key={`${t}-${i}`}>{t}</Chip>
      ))}
    </div>
  );
}

function ProfileView({ p }: { p: AcademicProfile }) {
  return (
    <div className="space-y-6">
      {p.summary && <p className="text-sm leading-relaxed text-ink">{p.summary}</p>}

      <Section title="Research interests">
        <Chips items={p.research_interests} />
      </Section>

      {p.education?.length > 0 && (
        <Section title="Education">
          <ul className="space-y-1.5">
            {p.education.map((e, i) => (
              <li key={i} className="text-sm">
                <span className="font-medium text-ink">{e.degree || "—"}</span>
                {e.institution && <span className="text-ink-muted"> · {e.institution}</span>}
                {(e.start || e.end) && (
                  <span className="text-ink-subtle"> ({[e.start, e.end].filter(Boolean).join("–")})</span>
                )}
                {e.grade && <span className="text-ink-subtle"> · {e.grade}</span>}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {p.experience?.length > 0 && (
        <Section title="Experience">
          <ul className="space-y-2">
            {p.experience.map((x, i) => (
              <li key={i} className="text-sm">
                <span className="font-medium text-ink">{x.title || "—"}</span>
                {x.organization && <span className="text-ink-muted"> · {x.organization}</span>}
                {x.description && <p className="mt-0.5 text-ink-muted">{x.description}</p>}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {p.projects?.length > 0 && (
        <Section title="Projects">
          <ul className="space-y-2">
            {p.projects.map((pr, i) => (
              <li key={i} className="text-sm">
                <span className="font-medium text-ink">{pr.name || "—"}</span>
                {pr.description && <p className="mt-0.5 text-ink-muted">{pr.description}</p>}
                {pr.technologies?.length > 0 && (
                  <div className="mt-1">
                    <Chips items={pr.technologies} />
                  </div>
                )}
              </li>
            ))}
          </ul>
        </Section>
      )}

      <div className="grid gap-6 sm:grid-cols-2">
        <Section title="Programming">
          <Chips items={p.skills?.programming ?? []} />
        </Section>
        <Section title="Technical / methods">
          <Chips items={[...(p.skills?.technical ?? []), ...(p.skills?.research_methods ?? [])]} />
        </Section>
        <Section title="Languages">
          <Chips items={p.skills?.languages ?? []} />
        </Section>
        <Section title="Links">
          <div className="flex flex-wrap gap-2 text-sm">
            {Object.entries(p.links ?? {})
              .filter(([, v]) => v)
              .map(([k, v]) => (
                <a key={k} href={/^https?:/.test(v) ? v : `https://${v}`} target="_blank"
                  rel="noreferrer" className="text-accent hover:underline">
                  {k}
                </a>
              ))}
          </div>
        </Section>
      </div>

      {p.publications?.length > 0 && (
        <Section title="Publications">
          <ul className="list-disc space-y-1 pl-5 text-sm text-ink-muted">
            {p.publications.map((x, i) => <li key={i}>{x}</li>)}
          </ul>
        </Section>
      )}
      {p.awards?.length > 0 && (
        <Section title="Awards">
          <Chips items={p.awards} />
        </Section>
      )}
    </div>
  );
}

export function ProfilePage() {
  const { data, isLoading } = useProfile();
  const upload = useUploadProfile();
  const update = useUpdateProfile();
  const del = useDeleteProfile();
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");

  function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    upload.mutate(file, {
      onSuccess: (res) => toast(res.message ?? (res.exists ? "Profile extracted." : "Could not read the file."), res.exists ? "success" : "error"),
      onError: (err: Error) => toast(err.message, "error"),
    });
  }

  function startEdit() {
    if (data?.profile) setDraft(JSON.stringify(data.profile, null, 2));
    setEditing(true);
  }

  function saveEdit() {
    let parsed: AcademicProfile;
    try {
      parsed = JSON.parse(draft);
    } catch {
      toast("That is not valid JSON.", "error");
      return;
    }
    update.mutate(parsed, {
      onSuccess: () => {
        setEditing(false);
        toast("Profile saved.", "success");
      },
      onError: (err: Error) => toast(err.message, "error"),
    });
  }

  function onDelete() {
    if (!confirm("Delete your profile and every uploaded résumé? This cannot be undone.")) return;
    del.mutate(undefined, {
      onSuccess: () => toast("Profile deleted.", "success"),
      onError: (err: Error) => toast(err.message, "error"),
    });
  }

  function exportJson() {
    if (!data?.profile) return;
    const blob = new Blob([JSON.stringify(data.profile, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "labscout-profile.json";
    a.click();
    URL.revokeObjectURL(url);
  }

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner />
      </div>
    );
  }

  const exists = data?.exists;
  const p = data?.profile;

  return (
    <div className="mx-auto max-w-3xl space-y-6 p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-bold text-ink">{exists && p?.name ? p.name : "Your profile"}</h1>
          {exists && p?.headline && <p className="text-sm text-ink-muted">{p.headline}</p>}
          {exists && (
            <p className="mt-1 text-xs text-ink-subtle">
              {data?.user_edited ? "Edited by you" : "Extracted by LLM"}
              {data?.source && ` · from ${data.source.filename}`}
            </p>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          <input ref={fileRef} type="file" accept=".pdf,.txt,.md,.markdown,.tex" hidden onChange={onFile} />
          <button onClick={() => fileRef.current?.click()} disabled={upload.isPending}
            className="rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-accent-ink disabled:opacity-50">
            {upload.isPending ? "Reading…" : exists ? "Re-upload résumé" : "Upload résumé"}
          </button>
          {exists && (
            <>
              {editing ? (
                <>
                  <button onClick={saveEdit} disabled={update.isPending}
                    className="rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-accent-ink disabled:opacity-50">
                    Save
                  </button>
                  <button onClick={() => setEditing(false)}
                    className="rounded-lg border border-border px-3 py-1.5 text-sm text-ink-muted">
                    Cancel
                  </button>
                </>
              ) : (
                <button onClick={startEdit}
                  className="rounded-lg border border-border px-3 py-1.5 text-sm text-ink-muted hover:bg-surface-sunken">
                  Edit
                </button>
              )}
              <button onClick={exportJson}
                className="rounded-lg border border-border px-3 py-1.5 text-sm text-ink-muted hover:bg-surface-sunken">
                Export
              </button>
              <a href="/api/profile/original"
                className="rounded-lg border border-border px-3 py-1.5 text-sm text-ink-muted hover:bg-surface-sunken">
                Original
              </a>
              <button onClick={onDelete}
                className="rounded-lg border border-rose-400/40 px-3 py-1.5 text-sm text-rose-500 hover:bg-rose-500/10">
                Delete
              </button>
            </>
          )}
        </div>
      </div>

      {upload.isPending && (
        <p className="text-sm text-ink-subtle">
          Reading your résumé with the LLM — this can take a moment on the self-hosted model.
        </p>
      )}

      {!exists ? (
        <div className="rounded-xl border border-dashed border-border bg-surface-raised p-8 text-center">
          <p className="text-sm text-ink-muted">
            Upload a résumé (PDF, Markdown, LaTeX or text) to build your central academic profile.
          </p>
          <p className="mt-1 text-xs text-ink-subtle">
            The original is stored unchanged; the structured profile is extracted from it and stays
            fully editable. Nothing is fabricated — gaps are yours to fill.
          </p>
          {data?.message && <p className="mt-3 text-xs text-rose-500">{data.message}</p>}
        </div>
      ) : editing ? (
        <div className="space-y-2">
          <p className="text-xs text-ink-subtle">
            Edit the profile JSON. Your edits are saved over the working copy; the original LLM
            extraction is preserved for provenance.
          </p>
          <textarea value={draft} onChange={(e) => setDraft(e.target.value)} spellCheck={false}
            className="h-[28rem] w-full rounded-lg border border-border bg-surface-sunken p-3 font-mono text-xs text-ink" />
        </div>
      ) : (
        p && <div className="rounded-xl border border-border bg-surface-raised p-6"><ProfileView p={p} /></div>
      )}
    </div>
  );
}
