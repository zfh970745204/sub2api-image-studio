import { useEffect, useRef, useState } from "react";
import { ArrowRight, ListTodo } from "lucide-react";
import { api, type ImageJob } from "./user-api";
import { ImageThumbnail } from "./ImageThumbnail";
import { usePageVisible } from "./usePageVisible";
import "./studio-task-queue.css";

const active = (job: ImageJob) => ["queued", "running", "retry_wait"].includes(job.status);
const labels: Record<string, string> = { queued: "排队中", running: "执行中", retry_wait: "等待重试", succeeded: "已完成", failed: "失败", cancelled: "已取消", timed_out: "已超时" };

interface Props {
  jobs: ImageJob[];
  focusedJobId: string | null;
  concurrency: number;
  onUpdate: (job: ImageJob) => void;
  onSettled: () => void;
  onOpen: (id: string) => void;
  nameOf: (code: string) => string;
}

/** Background jobs only update their own row; they never replace the editing draft. */
function QueueItem({ job, focused, onUpdate, onSettled, onOpen, nameOf }: Omit<Props, "jobs" | "focusedJobId" | "concurrency"> & { job: ImageJob; focused: boolean }) {
  const visible = usePageVisible();
  const pending = active(job);
  const [error, setError] = useState(false);
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
        setError(false);
        callbacks.current.onUpdate(updated);
        if (active(updated)) timer = window.setTimeout(poll, Math.max(2000, next_poll_after_ms || 3000));
        else callbacks.current.onSettled();
      } catch {
        if (!cancelled) { setError(true); timer = window.setTimeout(poll, 4000); }
      }
    }
    void poll();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [job.id, pending, focused, visible]);
  return <button className="studio-queue-item" type="button" onClick={() => onOpen(job.id)} aria-label={`查看任务 ${job.id}`}>
    <ImageThumbnail id={job.output_asset_id || job.source_asset_id} />
    <span><strong>{nameOf(job.operation_code)}</strong><small>{job.id.slice(-8)}{error ? " · 状态获取失败，自动重试中" : job.refund_status === "refunded" ? " · 积分已退回" : ""}</small></span>
    <em className={job.status}>{labels[job.status] || job.status}</em><ArrowRight size={15} />
  </button>;
}

export function StudioTaskQueue(props: Props) {
  if (!props.jobs.length) return null;
  const running = props.jobs.filter((job) => job.status === "running").length;
  const queued = props.jobs.filter((job) => job.status === "queued" || job.status === "retry_wait").length;
  return <section className="studio-task-queue" aria-label="本次任务列表">
    <header><span><ListTodo size={16} /><strong>本次任务</strong></span><small>执行 {running} · 等待 {queued}{props.concurrency ? ` · 会员最多同时执行 ${props.concurrency} 项` : ""}</small></header>
    <p>可继续提交，超过并发上限自动排队。后台完成的任务点击查看，全部记录保存在任务中心。</p>
    <div>{props.jobs.map((job) => <QueueItem key={job.id} job={job} focused={job.id === props.focusedJobId} onUpdate={props.onUpdate} onSettled={props.onSettled} onOpen={props.onOpen} nameOf={props.nameOf} />)}</div>
  </section>;
}
