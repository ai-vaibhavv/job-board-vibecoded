import { useState } from "react";
import { useHealth, useSaveSettings, useSettings } from "../hooks/queries";
import { useCheckLinks } from "../hooks/useCheckLinks";
import { useToast } from "../components/Toast";
import { Spinner } from "../components/ui";
import type { SettingsStatus } from "../types";

const SECRET_FIELDS: { key: string; label: string; placeholder?: string }[] = [
  { key: "discord_webhook_url", label: "Discord webhook URL", placeholder: "https://discord.com/api/webhooks/…" },
  { key: "search_api_key", label: "Search API key" },
  { key: "google_cse_id", label: "Google CSE ID (only for google_cse)" },
  { key: "apify_token", label: "Apify token (enables LinkedIn source)" },
  { key: "colab_api_key", label: "LLM API key (optional bearer)" },
];

export default function SettingsPage() {
  const settings = useSettings();
  if (settings.isLoading) {
    return <div className="flex flex-1 items-center justify-center text-ink-subtle"><Spinner className="h-6 w-6" /></div>;
  }
  if (settings.isError || !settings.data) {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-rose-500">
        Could not load settings.
      </div>
    );
  }
  return <SettingsForm status={settings.data} />;
}

function SettingsForm({ status }: { status: SettingsStatus }) {
  const toast = useToast();
  const health = useHealth();
  const save = useSaveSettings();
  const links = useCheckLinks();

  // Secret inputs start empty (their current value is masked); a typed value
  // replaces the stored secret. Non-secret fields are pre-filled.
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [provider, setProvider] = useState(status.search_api_provider);
  const [colabUrl, setColabUrl] = useState(status.colab_base_url);

  function submit() {
    const payload: Record<string, string> = {};
    for (const [k, v] of Object.entries(secrets)) {
      if (v.trim()) payload[k] = v.trim();
    }
    if (provider !== status.search_api_provider) payload.search_api_provider = provider;
    if (colabUrl.trim() !== status.colab_base_url) payload.colab_base_url = colabUrl.trim();

    if (Object.keys(payload).length === 0) {
      toast("Nothing changed.", "info");
      return;
    }
    save.mutate(payload, {
      onSuccess: () => {
        setSecrets({});
        toast("Settings saved.", "success");
      },
      onError: (e) => toast(`Save failed: ${(e as Error).message}`, "error"),
    });
  }

  return (
    <div className="scroll-thin flex-1 overflow-y-auto">
      <div className="mx-auto max-w-2xl space-y-8 p-6">
        <div>
          <h1 className="text-xl font-bold text-ink">Settings</h1>
          <p className="mt-1 text-sm text-ink-muted">
            Secrets are stored locally in your database volume (not in the code), masked here, and
            take effect immediately — no restart or file editing.
          </p>
        </div>

        {/* Secrets */}
        <section className="space-y-4 rounded-xl border border-border bg-surface-raised p-5">
          <h2 className="text-sm font-semibold text-ink">Secrets & keys</h2>
          {SECRET_FIELDS.map((f) => {
            const st = status.secrets[f.key];
            return (
              <div key={f.key}>
                <label className="mb-1 flex items-center justify-between text-xs font-medium text-ink-muted">
                  <span>{f.label}</span>
                  {st?.set ? (
                    <span className="text-emerald-600 dark:text-emerald-400">set · {st.hint}</span>
                  ) : (
                    <span className="text-ink-subtle">not set</span>
                  )}
                </label>
                <input
                  type="password"
                  autoComplete="new-password"
                  value={secrets[f.key] ?? ""}
                  onChange={(e) => setSecrets((s) => ({ ...s, [f.key]: e.target.value }))}
                  placeholder={st?.set ? "•••• (leave blank to keep)" : f.placeholder ?? ""}
                  className="w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-ink outline-none focus:border-accent"
                />
              </div>
            );
          })}

          <div>
            <label className="mb-1 block text-xs font-medium text-ink-muted">Search provider</label>
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              className="w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-ink outline-none focus:border-accent"
            >
              {status.providers.map((p) => (
                <option key={p || "none"} value={p}>
                  {p || "(disabled)"}
                </option>
              ))}
            </select>
          </div>
        </section>

        {/* LLM tunnel */}
        <section className="space-y-3 rounded-xl border border-border bg-surface-raised p-5">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-ink">LLM tunnel</h2>
            <span className="flex items-center gap-1.5 text-xs text-ink-muted">
              <span
                className={`h-2 w-2 rounded-full ${
                  health.data?.llm_online ? "bg-emerald-500" : "bg-rose-500"
                }`}
              />
              {health.data?.llm_online ? "online" : "offline"}
            </span>
          </div>
          <p className="text-xs text-ink-subtle">
            The self-hosted OpenAI-compatible URL (Colab/vLLM tunnel). Changes every session; used
            for German translation and new searches.
          </p>
          <input
            value={colabUrl}
            onChange={(e) => setColabUrl(e.target.value)}
            placeholder="https://your-tunnel.trycloudflare.com"
            className="w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-ink outline-none focus:border-accent"
          />
        </section>

        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={submit}
            disabled={save.isPending}
            className="rounded-lg bg-accent px-5 py-2 text-sm font-medium text-accent-ink transition hover:opacity-90 disabled:opacity-50"
          >
            {save.isPending ? "Saving…" : "Save settings"}
          </button>
        </div>

        {/* Maintenance */}
        <section className="space-y-3 rounded-xl border border-border bg-surface-raised p-5">
          <h2 className="text-sm font-semibold text-ink">Maintenance</h2>
          <p className="text-xs text-ink-subtle">
            Check every posting link and hide the ones that have expired (a 404/410). Polite and
            rate-limited. LinkedIn postings can't be verified and are left as-is.
          </p>
          <button
            type="button"
            onClick={links.start}
            disabled={links.running}
            className="inline-flex items-center gap-2 rounded-lg border border-border px-4 py-2 text-sm font-medium text-ink transition hover:bg-surface-sunken disabled:opacity-50"
          >
            {links.running && <Spinner className="h-4 w-4" />}
            {links.running ? "Checking links…" : "🧹 Clean up expired links"}
          </button>
          {links.task && links.task.status !== "running" && (
            <p
              className={`text-xs ${
                links.task.status === "error"
                  ? "text-rose-600 dark:text-rose-400"
                  : "text-emerald-600 dark:text-emerald-400"
              }`}
            >
              {links.task.status === "done" ? links.task.result : `Failed: ${links.task.error}`}
            </p>
          )}
        </section>
      </div>
    </div>
  );
}
