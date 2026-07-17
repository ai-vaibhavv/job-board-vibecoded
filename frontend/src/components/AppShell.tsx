import { useState } from "react";
import { NavLink, Outlet, useOutletContext } from "react-router-dom";
import { useHealth, useMeta } from "../hooks/queries";
import { useTheme } from "../hooks/useTheme";
import type { JobFilters, Meta } from "../types";
import { SearchModal } from "./SearchModal";

export const DEFAULT_FILTERS: JobFilters = {
  status: "all",
  min_score: 0,
  source: "all",
  text: "",
  show_hidden: false,
};

/** Shared across routes so filters survive navigating to a job and back. */
export interface BoardContext {
  filters: JobFilters;
  setFilters: (patch: Partial<JobFilters>) => void;
  meta: Meta | undefined;
  openSearch: () => void;
  llmOnline: boolean | undefined;
}

export function useBoard() {
  return useOutletContext<BoardContext>();
}

function navClass({ isActive }: { isActive: boolean }) {
  return `rounded-lg px-3 py-1.5 text-sm font-medium transition ${
    isActive ? "bg-accent/10 text-accent" : "text-ink-muted hover:text-ink"
  }`;
}

export function AppShell() {
  const { theme, toggle } = useTheme();
  const meta = useMeta();
  const health = useHealth();
  const [filters, setFiltersState] = useState<JobFilters>(DEFAULT_FILTERS);
  const [searchOpen, setSearchOpen] = useState(false);

  const setFilters = (patch: Partial<JobFilters>) =>
    setFiltersState((f) => ({ ...f, ...patch }));

  const llmOnline = health.data?.llm_online;
  const ctx: BoardContext = {
    filters,
    setFilters,
    meta: meta.data,
    openSearch: () => setSearchOpen(true),
    llmOnline,
  };

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-4 border-b border-border bg-surface-raised px-4 py-2.5">
        <NavLink to="/" className="flex items-center gap-2">
          <img src="/logo.png" alt="" className="h-6 w-6 rounded" />
          <span className="text-base font-bold text-ink">LabScout</span>
        </NavLink>
        <nav className="flex items-center gap-1">
          <NavLink to="/" end className={navClass}>
            Jobs
          </NavLink>
          <NavLink to="/profile" className={navClass}>
            Profile
          </NavLink>
          <NavLink to="/settings" className={navClass}>
            Settings
          </NavLink>
        </nav>
        <div className="ml-auto flex items-center gap-3">
          <span
            className="flex items-center gap-1.5 text-xs text-ink-muted"
            title="Self-hosted LLM endpoint (translation & search)"
          >
            <span
              className={`h-2 w-2 rounded-full ${
                llmOnline === undefined
                  ? "bg-ink-subtle"
                  : llmOnline
                    ? "bg-emerald-500"
                    : "bg-rose-500"
              }`}
            />
            LLM {llmOnline === undefined ? "…" : llmOnline ? "online" : "offline"}
          </span>
          <button
            type="button"
            onClick={toggle}
            aria-label="Toggle theme"
            className="rounded-lg border border-border p-1.5 text-ink-muted transition hover:bg-surface-sunken"
          >
            {theme === "dark" ? "☀️" : "🌙"}
          </button>
        </div>
      </header>

      {health.data && !health.data.llm_online && (
        <div className="bg-amber-500/15 px-4 py-1.5 text-center text-xs text-amber-700 dark:text-amber-300">
          ⚠️ LLM endpoint offline — translation and new searches are paused until it's back.
          Browsing is unaffected. Set the tunnel URL in{" "}
          <NavLink to="/settings" className="underline">
            Settings
          </NavLink>
          .
        </div>
      )}

      <Outlet context={ctx} />

      {searchOpen && <SearchModal meta={meta.data} onClose={() => setSearchOpen(false)} />}
    </div>
  );
}
