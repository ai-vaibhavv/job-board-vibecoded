import { useState } from "react";

/** Multi-select that also accepts custom values — the dashboard search needs to
 * add topics/locations that aren't in the preset list (resume extraction, ad-hoc
 * terms), mirroring the old Gradio `allow_custom_value` dropdowns. */
export function TagField({
  label,
  values,
  suggestions,
  onChange,
  placeholder,
}: {
  label: string;
  values: string[];
  suggestions: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState("");

  function add(value: string) {
    const v = value.trim();
    if (!v || values.some((x) => x.toLowerCase() === v.toLowerCase())) return;
    onChange([...values, v]);
  }
  function remove(value: string) {
    onChange(values.filter((v) => v !== value));
  }

  const unusedSuggestions = suggestions
    .filter((s) => !values.some((v) => v.toLowerCase() === s.toLowerCase()))
    .slice(0, 8);

  return (
    <div>
      <label className="mb-1 block text-xs font-semibold uppercase tracking-wide text-ink-subtle">
        {label}
      </label>
      <div className="flex flex-wrap gap-1.5 rounded-lg border border-border bg-surface p-2">
        {values.map((v) => (
          <span
            key={v}
            className="inline-flex items-center gap-1 rounded-full bg-accent/10 px-2 py-0.5 text-xs text-accent"
          >
            {v}
            <button type="button" onClick={() => remove(v)} aria-label={`Remove ${v}`} className="hover:text-ink">
              ✕
            </button>
          </span>
        ))}
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              add(draft);
              setDraft("");
            }
          }}
          placeholder={values.length === 0 ? placeholder : ""}
          className="min-w-[8rem] flex-1 bg-transparent text-sm text-ink outline-none placeholder:text-ink-subtle"
        />
      </div>
      {unusedSuggestions.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {unusedSuggestions.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => add(s)}
              className="rounded-full bg-surface-sunken px-2 py-0.5 text-[11px] text-ink-muted transition hover:text-ink"
            >
              + {s}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
