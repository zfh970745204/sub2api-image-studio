import { useEffect, useLayoutEffect, useRef, useState, type PointerEvent } from "react";
import { createPortal } from "react-dom";
import { Circle, Crop, RotateCcw, X } from "lucide-react";
import { api, type Asset, type CropOptions } from "./user-api";
import "./crop-editor.css";

const clamp = (n: number, low: number, high: number) => Math.max(low, Math.min(high, Math.round(n)));
export function clampCrop(crop: CropOptions, width: number, height: number): CropOptions {
  const w = clamp(crop.width, 1, width), h = clamp(crop.height, 1, height);
  const size = Math.min(w, h);
  const next = { ...crop, width: crop.shape === "circle" ? size : w, height: crop.shape === "circle" ? size : h };
  return { ...next, x: clamp(crop.x, 0, width - next.width), y: clamp(crop.y, 0, height - next.height) };
}

export function CropEditor({ asset, previewUrl, onClose, onSaved }: { asset: Asset; previewUrl?: string | null; onClose: () => void; onSaved: (asset: Asset) => void }) {
  const width = asset.width || 1, height = asset.height || 1;
  const dialog = useRef<HTMLDialogElement>(null);
  const stage = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; y: number; crop: CropOptions; resize: boolean; scaleX: number; scaleY: number } | null>(null);
  const savingRef = useRef(false);
  const [crop, setCrop] = useState<CropOptions>({ x: 0, y: 0, width, height, shape: "rectangle" });
  const [url, setUrl] = useState<string | null>(previewUrl || null);
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useLayoutEffect(() => {
    const element = dialog.current!;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    if (element.showModal) element.showModal(); else element.setAttribute("open", "");
    return () => { element.close?.(); document.body.style.overflow = overflow; if (previous?.isConnected) previous.focus(); };
  }, []);
  useEffect(() => {
    let alive = true;
    setLoaded(false); setError("");
    if (previewUrl && !retry) setUrl(previewUrl);
    else void api.downloadUrl(asset.id).then((value) => { if (alive) setUrl(value.url); }, (reason: unknown) => { if (alive) setError(reason instanceof Error ? reason.message : "原图载入失败"); });
    return () => { alive = false; };
  }, [asset.id, previewUrl, retry]);
  function reset(shape = crop.shape) {
    const size = Math.min(width, height);
    setCrop(shape === "circle" ? { shape, x: Math.floor((width - size) / 2), y: Math.floor((height - size) / 2), width: size, height: size } : { shape, x: 0, y: 0, width, height });
  }
  function begin(event: PointerEvent<HTMLDivElement>, resize: boolean) {
    if (!loaded || savingRef.current || event.button !== 0) return;
    event.preventDefault(); event.stopPropagation();
    const rect = stage.current!.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    drag.current = { x: event.clientX, y: event.clientY, crop, resize, scaleX: width / rect.width, scaleY: height / rect.height };
    event.currentTarget.setPointerCapture(event.pointerId);
  }
  function move(event: PointerEvent<HTMLDivElement>) {
    const start = drag.current;
    if (!start) return;
    const dx = (event.clientX - start.x) * start.scaleX, dy = (event.clientY - start.y) * start.scaleY;
    if (!start.resize) setCrop(clampCrop({ ...start.crop, x: start.crop.x + dx, y: start.crop.y + dy }, width, height));
    else {
      const w = clamp(start.crop.width + dx, 1, width - start.crop.x);
      const h = clamp(start.crop.height + dy, 1, height - start.crop.y);
      const size = clamp(start.crop.width + (Math.abs(dx) > Math.abs(dy) ? dx : dy), 1, Math.min(width - start.crop.x, height - start.crop.y));
      setCrop({ ...start.crop, width: crop.shape === "circle" ? size : w, height: crop.shape === "circle" ? size : h });
    }
  }
  async function save() {
    if (!loaded || savingRef.current) return;
    savingRef.current = true; setSaving(true); setError("");
    try { onSaved((await api.crop(asset.id, crop)).asset); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "裁切保存失败，请重试"); }
    finally { savingRef.current = false; setSaving(false); }
  }
  const selectionStyle = { left: `${crop.x / width * 100}%`, top: `${crop.y / height * 100}%`, width: `${crop.width / width * 100}%`, height: `${crop.height / height * 100}%`, borderRadius: crop.shape === "circle" ? "50%" : 0 };
  return createPortal(<dialog ref={dialog} className="crop-editor" aria-labelledby="crop-title" onCancel={(event) => { event.preventDefault(); if (!savingRef.current) onClose(); }}>
    <header><div><h2 id="crop-title">裁切图片</h2><p>拖动选框调整位置，拖动右下角调整大小。</p></div><button aria-label="关闭裁切" type="button" disabled={saving} onClick={onClose}><X size={18} /></button></header>
    <div className="crop-layout">
      <div className="crop-workspace"><div ref={stage} className="crop-stage" style={{ aspectRatio: `${width}/${height}`, width: `min(100%, ${48 * width / height}dvh)` }}>
        {url && <img src={url} alt="裁切原图" draggable={false} onLoad={(event) => { const image = event.currentTarget; if (image.naturalWidth !== width || image.naturalHeight !== height) { setLoaded(false); setError("原图尺寸不匹配，请重新载入"); } else { setLoaded(true); setError(""); } }} onError={() => { setLoaded(false); setError("原图载入失败，请重试"); }} />}
        {!loaded && <span className="crop-loading">正在载入原图…</span>}
        {loaded && <div className="crop-selection" style={selectionStyle} role="group" aria-label="裁切选框" tabIndex={0} onPointerDown={(event) => begin(event, false)} onPointerMove={move} onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }} onLostPointerCapture={() => { drag.current = null; }} onKeyDown={(event) => {
          if (saving || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
          event.preventDefault(); const step = event.shiftKey ? 10 : 1;
          setCrop(clampCrop({ ...crop, x: crop.x + (event.key === "ArrowLeft" ? -step : event.key === "ArrowRight" ? step : 0), y: crop.y + (event.key === "ArrowUp" ? -step : event.key === "ArrowDown" ? step : 0) }, width, height));
        }}><div className="crop-handle" role="presentation" onPointerDown={(event) => begin(event, true)} /></div>}
      </div></div>
      <aside><div className="crop-shapes" role="group" aria-label="裁切形状"><button type="button" disabled={saving} aria-pressed={crop.shape === "rectangle"} onClick={() => reset("rectangle")}><Crop size={15} />矩形</button><button type="button" disabled={saving} aria-pressed={crop.shape === "circle"} onClick={() => reset("circle")}><Circle size={15} />圆形</button></div>
        <div className="crop-values">{([ ["x", "左边距"], ["y", "上边距"], ["width", crop.shape === "circle" ? "直径" : "宽度"], ...(crop.shape === "circle" ? [] : [["height", "高度"]]) ] as ["x" | "y" | "width" | "height", string][]).map(([key, label]) => <label key={key}>{label}<input type="number" min={key === "x" || key === "y" ? 0 : 1} max={key === "x" ? width - crop.width : key === "y" ? height - crop.height : key === "width" ? width : height} value={crop[key]} disabled={saving} onChange={(event) => { if (!Number.isFinite(event.target.valueAsNumber)) return; const value = event.target.valueAsNumber; setCrop(clampCrop({ ...crop, [key]: value, ...(crop.shape === "circle" && key === "width" ? { height: value } : {}) }, width, height)); }} /></label>)}</div>
        <div className="crop-result"><span>裁切效果 · {crop.width} × {crop.height}</span><div className="crop-result-frame" style={{ aspectRatio: `${crop.width}/${crop.height}`, width: `min(100%, ${20 * crop.width / crop.height}dvh)` }}><div style={{ borderRadius: crop.shape === "circle" ? "50%" : 0 }}>{url && <img src={url} alt="裁切效果预览" style={{ width: `${width / crop.width * 100}%`, height: `${height / crop.height * 100}%`, left: `${-crop.x / crop.width * 100}%`, top: `${-crop.y / crop.height * 100}%` }} />}</div></div></div>
        <button type="button" className="crop-reset" disabled={saving} onClick={() => reset()}><RotateCcw size={14} />重置选框</button>
      </aside>
    </div>
    {error && <p role="alert" className="crop-error">{error}{!loaded && <button type="button" onClick={() => setRetry((value) => value + 1)}>重新载入</button>}</p>}
    <footer><span>{crop.shape === "circle" ? "圆形外侧透明 · " : ""}保存为新版本 · 不扣积分</span><div><button type="button" disabled={saving} onClick={onClose}>取消</button><button type="button" className="crop-save" disabled={!loaded || saving} onClick={() => void save()}>{saving ? "正在保存…" : "保存裁切"}</button></div></footer>
  </dialog>, document.body);
}
