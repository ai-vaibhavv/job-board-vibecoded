import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { useInvalidateJobs } from "./queries";
import type { SearchTask } from "../types";

/** Kick off the background "clean up expired links" sweep and poll it to
 * completion, refreshing the job list when it finishes (jobs may be hidden). */
export function useCheckLinks() {
  const invalidate = useInvalidateJobs();
  const [taskId, setTaskId] = useState<string | null>(null);

  const start = useMutation({
    mutationFn: () => api.checkLinks(),
    onSuccess: (task) => setTaskId(task.task_id),
  });

  const task = useQuery({
    queryKey: ["check-links-task", taskId],
    queryFn: () => api.searchStatus(taskId as string),
    enabled: !!taskId,
    refetchInterval: (q) => {
      const data = q.state.data as SearchTask | undefined;
      if (data && data.status !== "running") {
        if (data.status === "done") invalidate();
        return false;
      }
      return 2000;
    },
  });

  const running = start.isPending || task.data?.status === "running";
  return { start: () => start.mutate(), task: task.data ?? null, running };
}
