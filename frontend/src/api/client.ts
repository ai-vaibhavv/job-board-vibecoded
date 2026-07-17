// Typed fetch wrappers. All requests are same-origin (`/api/...`): the Vite dev
// server proxies to FastAPI, and in production nginx proxies to it — so there is
// no base URL to configure here.

import type {
  AcademicProfile,
  Health,
  JobDetail,
  JobFilters,
  JobsResponse,
  MatchResponse,
  Meta,
  ProfileResponse,
  ResearchResponse,
  ResumeResult,
  TailoringResponse,
  SearchPreview,
  SearchTask,
  SettingsStatus,
} from "../types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.detail ?? detail;
    } catch {
      // non-JSON error body; keep statusText
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  health: () => request<Health>("/health"),

  meta: () => request<Meta>("/meta"),

  jobs: (f: JobFilters) => {
    const q = new URLSearchParams();
    if (f.status && f.status !== "all") q.set("status", f.status);
    if (f.min_score > 0) q.set("min_score", String(f.min_score));
    if (f.source && f.source !== "all") q.set("source", f.source);
    if (f.text.trim()) q.set("text", f.text.trim());
    if (f.show_hidden) q.set("show_hidden", "true");
    const qs = q.toString();
    return request<JobsResponse>(`/jobs${qs ? `?${qs}` : ""}`);
  },

  job: (id: string) => request<JobDetail>(`/jobs/${encodeURIComponent(id)}`),

  match: (id: string) => request<MatchResponse>(`/jobs/${encodeURIComponent(id)}/match`),

  tailoring: (id: string) =>
    request<TailoringResponse>(`/jobs/${encodeURIComponent(id)}/tailoring`),

  research: (id: string) => request<ResearchResponse>(`/jobs/${encodeURIComponent(id)}/research`),

  refresh: (id: string) =>
    request<JobDetail>(`/jobs/${encodeURIComponent(id)}/refresh`, { method: "POST" }),

  publish: (id: string, confirm: boolean) =>
    request<{ message: string }>(`/jobs/${encodeURIComponent(id)}/publish`, {
      method: "POST",
      body: JSON.stringify({ confirm }),
    }),

  hide: (id: string) =>
    request<{ message: string }>(`/jobs/${encodeURIComponent(id)}/hide`, { method: "POST" }),

  unhideAll: () => request<{ message: string }>("/jobs/unhide-all", { method: "POST" }),

  searchPreview: (keywords: string, topics: string[]) =>
    request<SearchPreview>("/search/preview", {
      method: "POST",
      body: JSON.stringify({ keywords, topics }),
    }),

  searchRun: (keywords: string, topics: string[], locations: string[]) =>
    request<SearchTask>("/search/run", {
      method: "POST",
      body: JSON.stringify({ keywords, topics, locations }),
    }),

  searchStatus: (taskId: string) =>
    request<SearchTask>(`/search/run/${encodeURIComponent(taskId)}`),

  settings: () => request<SettingsStatus>("/settings"),

  saveSettings: (values: Record<string, string>) =>
    request<SettingsStatus>("/settings", { method: "POST", body: JSON.stringify(values) }),

  checkLinks: () => request<SearchTask>("/maintenance/check-links", { method: "POST" }),

  resume: async (file: File): Promise<ResumeResult> => {
    const form = new FormData();
    form.append("file", file);
    // No Content-Type header: the browser sets the multipart boundary.
    const res = await fetch("/api/resume", { method: "POST", body: form });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        detail = (await res.json())?.detail ?? detail;
      } catch {
        /* keep statusText */
      }
      throw new ApiError(res.status, detail);
    }
    return (await res.json()) as ResumeResult;
  },

  // --- central academic profile (Phase 3) ---
  profile: () => request<ProfileResponse>("/profile"),

  uploadProfile: async (file: File): Promise<ProfileResponse> => {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch("/api/profile", { method: "POST", body: form });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        detail = (await res.json())?.detail ?? detail;
      } catch {
        /* keep statusText */
      }
      throw new ApiError(res.status, detail);
    }
    return (await res.json()) as ProfileResponse;
  },

  updateProfile: (profile: AcademicProfile) =>
    request<ProfileResponse>("/profile", { method: "PUT", body: JSON.stringify(profile) }),

  deleteProfile: () => request<ProfileResponse>("/profile", { method: "DELETE" }),
};
