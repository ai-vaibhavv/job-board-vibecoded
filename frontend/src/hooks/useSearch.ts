import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { keys, useInvalidateJobs } from "./queries";
import type { SearchTask } from "../types";

/** Drives the "run a new search" flow: kick off the pipeline in the background
 * and poll its task until it settles, invalidating the jobs list on completion. */
export function useSearch() {
  const invalidate = useInvalidateJobs();
  const [taskId, setTaskId] = useState<string | null>(null);
  const settledRef = useRef(false);

  const preview = useMutation({
    mutationFn: ({ keywords, topics }: { keywords: string; topics: string[] }) =>
      api.searchPreview(keywords, topics),
  });

  const run = useMutation({
    mutationFn: ({
      keywords,
      topics,
      locations,
    }: {
      keywords: string;
      topics: string[];
      locations: string[];
    }) => api.searchRun(keywords, topics, locations),
    onSuccess: (task) => {
      settledRef.current = false;
      setTaskId(task.task_id);
    },
  });

  const task = useQuery({
    queryKey: keys.searchTask(taskId ?? ""),
    queryFn: () => api.searchStatus(taskId as string),
    enabled: !!taskId,
    refetchInterval: (query) => {
      const data = query.state.data as SearchTask | undefined;
      return data && data.status !== "running" ? false : 1500;
    },
  });

  // When the background run finishes, refresh the job list exactly once.
  useEffect(() => {
    const status = task.data?.status;
    if ((status === "done" || status === "error") && !settledRef.current) {
      settledRef.current = true;
      if (status === "done") invalidate();
    }
  }, [task.data?.status, invalidate]);

  const isRunning = run.isPending || task.data?.status === "running";

  function reset() {
    setTaskId(null);
    settledRef.current = false;
    preview.reset();
    run.reset();
  }

  return { preview, run, task: task.data ?? null, isRunning, reset };
}
