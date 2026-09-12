import { useEffect, useRef } from "react";
import { api, type ImageJob } from "./user-api";
import { usePageVisible } from "./usePageVisible";

const active = (job: ImageJob) => ["queued", "running", "retry_wait"].includes(job.status);

interface Props {
  jobs: ImageJob[];
  focusedJobId: string | null;
  onUpdate: (job: ImageJob) => void;
  onSettled: () => void;
}

/** Keep detached jobs and balance current without replacing the editing draft. */
function QueueItem({ job, focused, onUpdate, onSettled }: Omit<Props, "jobs" | "focusedJobId"> & { job: ImageJob; focused: boolean }) {
  const visible = usePageVisible();
  const pending = active(job);
  const callbacks = useRef({ onUpdate, onSettled });
  callbacks.current = { onUpdate, onSettled };
  useEffect(() => {
    if (!pending || focused || !visible) return;
    let cancelled = false;
    let timer: number;
    async function poll() {
      try {
        const { job: updated, next_poll_after_ms } = await api.jobEvents(job.id);
        if (cancelled) return;
        callbacks.current.onUpdate(updated);
        if (active(updated)) timer = window.setTimeout(poll, Math.max(2000, next_poll_after_ms || 3000));
        else callbacks.current.onSettled();
      } catch {
        if (!cancelled) timer = window.setTimeout(poll, 4000);
      }
    }
    void poll();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [job.id, pending, focused, visible]);
  return null;
}

export function StudioTaskQueue(props: Props) {
  return <>{props.jobs.map((job) => <QueueItem key={job.id} job={job} focused={job.id === props.focusedJobId} onUpdate={props.onUpdate} onSettled={props.onSettled} />)}</>;
}
