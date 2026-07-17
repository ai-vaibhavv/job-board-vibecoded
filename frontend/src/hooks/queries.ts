import {
  useMutation,
  useQuery,
  useQueryClient,
  keepPreviousData,
} from "@tanstack/react-query";
import { api } from "../api/client";
import type { AcademicProfile, JobFilters } from "../types";

export const keys = {
  meta: ["meta"] as const,
  health: ["health"] as const,
  jobs: (f: JobFilters) => ["jobs", f] as const,
  job: (id: string) => ["job", id] as const,
  searchTask: (id: string) => ["search-task", id] as const,
  settings: ["settings"] as const,
  profile: ["profile"] as const,
  match: (id: string) => ["match", id] as const,
  tailoring: (id: string) => ["tailoring", id] as const,
};

export function useMeta() {
  return useQuery({ queryKey: keys.meta, queryFn: api.meta });
}

export function useHealth() {
  // Poll the LLM reachability so the "offline" banner clears on its own.
  return useQuery({
    queryKey: keys.health,
    queryFn: api.health,
    refetchInterval: 30_000,
  });
}

export function useJobs(filters: JobFilters) {
  return useQuery({
    queryKey: keys.jobs(filters),
    queryFn: () => api.jobs(filters),
    // Keep the old list on screen while the next filter query loads — no flash.
    placeholderData: keepPreviousData,
  });
}

export function useJobDetail(id: string | null) {
  return useQuery({
    queryKey: keys.job(id ?? ""),
    queryFn: () => api.job(id as string),
    enabled: !!id,
    // A German job's detail may trigger a slow one-off translation server-side.
    staleTime: 5 * 60_000,
  });
}

/** Invalidate everything derived from the jobs table after a mutation. */
export function useInvalidateJobs() {
  const qc = useQueryClient();
  return () => {
    qc.invalidateQueries({ queryKey: ["jobs"] });
    qc.invalidateQueries({ queryKey: keys.meta });
  };
}

export function usePublish() {
  const invalidate = useInvalidateJobs();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, confirm }: { id: string; confirm: boolean }) =>
      api.publish(id, confirm),
    onSuccess: (_data, { id }) => {
      qc.invalidateQueries({ queryKey: keys.job(id) });
      invalidate();
    },
  });
}

export function useHide() {
  const invalidate = useInvalidateJobs();
  return useMutation({
    mutationFn: (id: string) => api.hide(id),
    onSuccess: invalidate,
  });
}

export function useUnhideAll() {
  const invalidate = useInvalidateJobs();
  return useMutation({ mutationFn: () => api.unhideAll(), onSuccess: invalidate });
}

/** Re-fetch the full posting for one job (fuller description, link check, and a
 * German re-translation). Writes the fresh detail straight into the cache and
 * refreshes the list (a dead-link job may have just been auto-hidden). */
export function useRefreshJob() {
  const qc = useQueryClient();
  const invalidate = useInvalidateJobs();
  return useMutation({
    mutationFn: (id: string) => api.refresh(id),
    onSuccess: (detail, id) => {
      qc.setQueryData(keys.job(id), detail);
      invalidate();
    },
  });
}

export function useSettings() {
  return useQuery({ queryKey: keys.settings, queryFn: api.settings });
}

export function useSaveSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (values: Record<string, string>) => api.saveSettings(values),
    onSuccess: (status) => {
      qc.setQueryData(keys.settings, status);
      qc.invalidateQueries({ queryKey: keys.health });
      qc.invalidateQueries({ queryKey: keys.meta });
    },
  });
}

// --- central academic profile (Phase 3) ---

export function useProfile() {
  return useQuery({ queryKey: keys.profile, queryFn: api.profile });
}

export function useUploadProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => api.uploadProfile(file),
    onSuccess: (data) => {
      if (data.exists) qc.setQueryData(keys.profile, data);
    },
  });
}

export function useUpdateProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (profile: AcademicProfile) => api.updateProfile(profile),
    onSuccess: (data) => qc.setQueryData(keys.profile, data),
  });
}

export function useDeleteProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.deleteProfile(),
    onSuccess: (data) => qc.setQueryData(keys.profile, data),
  });
}

/** Profile↔opportunity match. On-demand (`enabled`) because a fresh analysis can
 * call the slow self-hosted LLM; once fetched it is cached client- and server-side. */
export function useMatch(id: string | null, enabled: boolean) {
  return useQuery({
    queryKey: keys.match(id ?? ""),
    queryFn: () => api.match(id as string),
    enabled: !!id && enabled,
    staleTime: 10 * 60_000,
    retry: false,
  });
}

/** Résumé-tailoring suggestions for one opportunity. On-demand, same reasoning. */
export function useTailoring(id: string | null, enabled: boolean) {
  return useQuery({
    queryKey: keys.tailoring(id ?? ""),
    queryFn: () => api.tailoring(id as string),
    enabled: !!id && enabled,
    staleTime: 10 * 60_000,
    retry: false,
  });
}
