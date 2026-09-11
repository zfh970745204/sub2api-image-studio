import { useEffect, useRef, useState } from "react";
import { api } from "./user-api";
import "./print-background.css";

/** Kept keyed by source so pending samples cannot recolor a replacement image. */
export function PrintBackgroundControls({ sourceId, sourceUrl, color, onChange }: {
  sourceId: string; sourceUrl: string | null; color: string; onChange: (color: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [sampling, setSampling] = useState(false);
  const request = useRef(0);

  async function detect(point?: { x: number; y: number }) {
    const serial = ++request.current;
    setBusy(true);
    setMessage("");
    try {
      const result = await api.printBackground(sourceId, point);
      if (serial !== request.current) return;
      onChange(result.color);
      setMessage(point ? "已从原图取色" : "已估计产品底色，请检查色块；不准确时可手动取色。");
      if (point) setSampling(false);
    } catch {
      if (serial === request.current) setMessage("自动取色暂不可用，请手动选择产品底色。");
    } finally {
      if (serial === request.current) setBusy(false);
    }
  }

  useEffect(() => {
    if (!color) void detect();
    return () => { request.current++; };
  }, [sourceId]);

  function manual(value: string) {
    request.current++;
    setBusy(false);
    onChange(value.toUpperCase());
    setMessage("已使用手动选择的产品底色");
  }

  return <div className="print-background-controls">
    <label className="user-color-field"><input aria-label="产品底色" type="color" value={color || "#000000"} onChange={(event) => manual(event.target.value)} /><span><strong>产品底色</strong><small>{color || "等待识别或手动选择"}</small></span></label>
    <div className="print-color-actions"><button className="user-secondary" type="button" disabled={busy} onClick={() => void detect()}>{busy ? "正在取色…" : "自动识别"}</button><button className="user-secondary" type="button" disabled={!sourceUrl || busy} aria-pressed={sampling} onClick={() => setSampling(!sampling)}>从原图取色</button></div>
    {sampling && sourceUrl && <div className="print-color-sample"><p>点击没有印花的衣服或产品表面。键盘确认可取中心颜色。</p><button type="button" aria-label="选择原图上的底色位置" onClick={(event) => {
      const rect = event.currentTarget.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      void detect(event.detail === 0 ? { x: .5, y: .5 } : { x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)), y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)) });
    }}><img src={sourceUrl} alt="点击产品表面取色" /></button></div>}
    <small role="status">{busy ? "正在读取原图底色…" : message || "确认底色后提取；透明图在同色产品上更容易融合。"}</small>
  </div>;
}
