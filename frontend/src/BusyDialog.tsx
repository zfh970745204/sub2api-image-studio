import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { LoaderCircle } from "lucide-react";
import "./busy-dialog.css";

/** A pending operation owns this dialog; settling or unmounting closes it automatically. */
export function BusyDialog({ title, detail }: { title?: string | null; detail?: string }) {
  return title ? <ActiveDialog title={title} detail={detail} /> : null;
}

function ActiveDialog({ title, detail }: { title: string; detail?: string }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const detailId = useId();
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const element = dialog.current!;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (typeof element.showModal === "function") element.showModal();
    else element.setAttribute("open", "");
    element.focus();
    const started = Date.now();
    const timer = window.setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => {
      window.clearInterval(timer);
      if (typeof element.close === "function") element.close();
      if (previous?.isConnected) previous.focus();
    };
  }, []);
  return createPortal(<dialog ref={dialog} className="studio-busy-dialog" aria-labelledby={titleId} aria-describedby={detailId} aria-modal="true" tabIndex={-1} onCancel={(event) => event.preventDefault()} onKeyDown={(event) => { if (event.key === "Tab") event.preventDefault(); }}>
    <div className="studio-busy-icon"><LoaderCircle size={25} aria-hidden="true" /></div>
    <h2 id={titleId}>{title}</h2>
    <p id={detailId} role="status">{detail || "正在处理，请稍候。完成后此窗口会自动关闭。"}</p>
    <div className="studio-busy-track" role="progressbar" aria-label={title}><span /></div>
    <small>{seconds < 15 ? "完成后自动关闭，请保持页面打开" : "仍在处理中，大图片或网络较慢时需要更多时间"}{seconds > 0 && ` · ${seconds} 秒`}</small>
  </dialog>, document.body);
}
