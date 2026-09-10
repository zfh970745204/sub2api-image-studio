import { useEffect, useState } from "react";
import type { ImageJob } from "./user-api";

export function JobProgress({ job, compact = false }: { job: ImageJob; compact?: boolean }) {
  const [, tick] = useState(0);
  const active = ["queued", "running", "retry_wait"].includes(job.status);
  useEffect(() => { if (!active) return; const timer = window.setInterval(() => tick((value) => value + 1), 1000); return () => window.clearInterval(timer); }, [active]);
  const elapsed = Math.max(0, Math.floor((Date.now() - Date.parse(job.started_at || job.queued_at)) / 1000));
  const stage = job.status === "queued" ? "等待处理资源" : job.status === "retry_wait" ? "等待自动重试" : job.status === "succeeded" ? "处理完成" : !active ? "处理已结束" : job.progress < 20 ? "读取原图" : job.progress < 65 ? "图像引擎处理中" : job.progress < 80 ? "整理图像与透明背景" : job.progress < 90 ? "校验处理结果" : "保存图片与缩略图";
  return <div className={`user-stage-progress ${compact ? "compact" : ""}`}>
    <div className={active ? "active" : ""} role="progressbar" aria-label={stage} aria-valuemin={0} aria-valuemax={100} aria-valuenow={job.status === "succeeded" ? 100 : active ? undefined : job.progress} aria-valuetext={stage}><i style={{ width: `${Math.max(4, job.progress)}%` }} /></div>
    <span>{stage}{active && ` · 已等待 ${elapsed} 秒`}</span>
    {!compact && active && <small>按实际处理阶段更新；引擎计算期间持续等待，完成后自动展示结果。</small>}
  </div>;
}
