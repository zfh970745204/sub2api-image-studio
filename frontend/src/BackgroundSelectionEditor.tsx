import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, type Asset, type SelectionContext } from "./user-api";
import { createSelectionLoader, type SelectionLoader } from "./selection-loader";
import { InfoHint } from "./InfoHint";
import type { SelectionCommand, SelectionReply } from "./background-selection.worker";
import "./background-selection.css";

type FrameState = { canUndo: boolean; canRedo: boolean; canRetune: boolean; removed: number; visible: number };
const MIN_ZOOM = .25;
const MAX_ZOOM = 8;
const ZOOM_PRESETS = [.25, .5, 1, 1.5, 2, 4, 8];
const zoomLabel = (value: number) => `${Math.round(value * 1000) / 10}%`;

export function BackgroundSelectionEditor({ asset, loader, previewUrl, onClose, onSaved }: { asset: Asset; loader?: SelectionLoader; previewUrl?: string | null; onClose: () => void; onSaved: (asset: Asset) => void }) {
  const ownLoader = useMemo(createSelectionLoader, []);
  const imageLoader = loader || ownLoader;
  const [earlySource, setEarlySource] = useState<string | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const sourceCanvas = useRef<HTMLCanvasElement>(null);
  const overlayCanvas = useRef<HTMLCanvasElement>(null);
  const resultCanvas = useRef<HTMLCanvasElement>(null);
  const sourceViewport = useRef<HTMLDivElement>(null);
  const resultViewport = useRef<HTMLDivElement>(null);
  const sourceImage = useRef<HTMLDivElement>(null);
  const resultImage = useRef<HTMLDivElement>(null);
  const zoomRef = useRef(1);
  const zoomAnchor = useRef<{ x: number; y: number; offsetX: number; offsetY: number; viewport: HTMLDivElement } | null>(null);
  const worker = useRef<Worker | null>(null);
  const working = useRef(true);
  const queued = useRef<SelectionCommand | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const savingRef = useRef(false);
  const savedCallback = useRef(onSaved);
  savedCallback.current = onSaved;
  const [context, setContext] = useState<SelectionContext | null>(null);
  const [frame, setFrame] = useState<FrameState | null>(null);
  const [processing, setProcessing] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [mode, setMode] = useState<"remove" | "restore">("remove");
  const [tolerance, setTolerance] = useState(12);
  const [contiguous, setContiguous] = useState(true);
  const [showSelection, setShowSelection] = useState(true);
  const [edge, setEdge] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [fitWidth, setFitWidth] = useState(300);
  const [background, setBackground] = useState("checker");
  const [retry, setRetry] = useState(0);
  const [dirty, setDirty] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);
  const [tuning, setTuning] = useState(false);
  const [failedPreview, setFailedPreview] = useState<string | null>(null);
  const imageWidth = context?.width || asset.width || 1;
  const imageHeight = context?.height || asset.height || 1;

  useLayoutEffect(() => {
    const element = dialog.current!;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    if (element.showModal) element.showModal(); else element.setAttribute("open", "");
    return () => { document.body.style.overflow = previousOverflow; element.close?.(); if (previous?.isConnected) previous.focus(); };
  }, []);

  useLayoutEffect(() => {
    const viewports = [sourceViewport.current!, resultViewport.current!];
    const fit = () => {
      // A horizontal scrollbar must not change the base fit size mid-zoom.
      const width = Math.min(...viewports.map((viewport) => Math.min(viewport.clientWidth, (viewport.offsetHeight || viewport.clientHeight) * imageWidth / imageHeight)));
      if (width > 0) setFitWidth(width);
    };
    fit();
    const observer = new ResizeObserver(fit);
    viewports.forEach((viewport) => observer.observe(viewport));
    return () => observer.disconnect();
  }, [imageWidth, imageHeight]);

  const changeZoom = useCallback((value: number, viewport = sourceViewport.current, clientX?: number, clientY?: number) => {
    const next = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, value));
    if (!viewport || !Number.isFinite(next) || next === zoomRef.current) return;
    const image = viewport === sourceViewport.current ? sourceImage.current : resultImage.current;
    const rect = image?.getBoundingClientRect();
    const bounds = viewport.getBoundingClientRect();
    const offsetX = clientX === undefined ? viewport.clientWidth / 2 : clientX - bounds.left - viewport.clientLeft;
    const offsetY = clientY === undefined ? viewport.clientHeight / 2 : clientY - bounds.top - viewport.clientTop;
    if (rect?.width && rect.height) {
      zoomAnchor.current = {
        x: Math.max(0, Math.min(1, (bounds.left + viewport.clientLeft + offsetX - rect.left) / rect.width)),
        y: Math.max(0, Math.min(1, (bounds.top + viewport.clientTop + offsetY - rect.top) / rect.height)),
        offsetX, offsetY, viewport,
      };
    }
    zoomRef.current = next;
    setZoom(next);
  }, []);

  useLayoutEffect(() => {
    const anchor = zoomAnchor.current;
    if (!anchor) return;
    zoomAnchor.current = null;
    const width = fitWidth * zoom;
    const height = width * imageHeight / imageWidth;
    // Account for the centered image before it grows wider than its viewport.
    const left = anchor.x * width + Math.max(0, (anchor.viewport.clientWidth - width) / 2) - anchor.offsetX;
    const top = anchor.y * height - anchor.offsetY;
    const viewports = [sourceViewport.current!, resultViewport.current!];
    const scrollLeft = Math.max(0, Math.min(left, ...viewports.map((viewport) => Math.max(0, width - viewport.clientWidth))));
    const scrollTop = Math.max(0, Math.min(top, ...viewports.map((viewport) => Math.max(0, height - viewport.clientHeight))));
    for (const viewport of viewports) { viewport.scrollLeft = scrollLeft; viewport.scrollTop = scrollTop; }
  }, [zoom, fitWidth, imageWidth, imageHeight]);

  useEffect(() => {
    const viewports = [sourceViewport.current!, resultViewport.current!];
    const wheel = (event: WheelEvent) => {
      // React's delegated wheel listener is passive. Cancel natively so neither
      // the viewport nor the page scrolls, including when zoom reaches a limit.
      event.preventDefault();
      event.stopPropagation();
      const viewport = event.currentTarget as HTMLDivElement;
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewport.clientHeight : 1;
      const delta = Math.max(-500, Math.min(500, event.deltaY * unit));
      changeZoom(zoomRef.current * Math.exp(-delta * .002), viewport, event.clientX, event.clientY);
    };
    viewports.forEach((viewport) => viewport.addEventListener("wheel", wheel, { passive: false }));
    return () => viewports.forEach((viewport) => viewport.removeEventListener("wheel", wheel));
  }, [changeZoom]);

  function syncScroll(viewport: HTMLDivElement, other: HTMLDivElement | null) {
    if (!other) return;
    if (other.scrollLeft !== viewport.scrollLeft) other.scrollLeft = viewport.scrollLeft;
    if (other.scrollTop !== viewport.scrollTop) other.scrollTop = viewport.scrollTop;
  }

  useEffect(() => {
    let alive = true;
    let sourceObjectUrl: string | null = null;
    let instance: Worker | null = null;
    setError(""); setContext(null); setFrame(null); setEarlySource(null); setProcessing(true); setDirty(false);
    setEdge(0); setTuning(false); setZoom(1); zoomRef.current = 1; zoomAnchor.current = null;
    setFailedPreview(null); queued.current = null; working.current = true;
    for (const viewport of [sourceViewport.current, resultViewport.current]) if (viewport) { viewport.scrollLeft = 0; viewport.scrollTop = 0; }
    const fail = (message: string) => {
      working.current = false; queued.current = null; savingRef.current = false;
      setProcessing(false); setSaving(false); setError(message);
    };
    async function load() {
      try {
        if (!window.Worker || !window.OffscreenCanvas || !window.createImageBitmap) throw new Error("当前浏览器不支持选区修边，请使用新版 Chrome、Edge 或 Firefox");
        // Start the worker while metadata and PNG bytes are being fetched.
        instance = new Worker(new URL("./background-selection.worker.ts", import.meta.url), { type: "module" });
        worker.current = instance;
        instance.onerror = () => { if (alive) { instance?.terminate(); worker.current = null; setFrame(null); fail("选区处理器异常，请重新载入；大图可先缩小后重试"); } };
        instance.onmessage = async ({ data }: MessageEvent<SelectionReply>) => {
          if (!alive) {
            if (data.type === "frame") { data.source?.close(); data.preview.close(); data.overlay.close(); }
            return;
          }
          if (data.type === "error") { fail(data.message); return; }
          if (data.type === "export") {
            try {
              const saved = await api.saveSelection(asset.id, data.blob);
              if (alive) { savingRef.current = false; savedCallback.current(saved.asset); }
            } catch (reason) { if (alive) fail(reason instanceof Error ? reason.message : "保存失败，请重试"); }
            return;
          }
          const paint = (target: HTMLCanvasElement | null, bitmap?: ImageBitmap) => {
            if (!bitmap) return;
            if (target) {
              if (target.width !== bitmap.width) target.width = bitmap.width;
              if (target.height !== bitmap.height) target.height = bitmap.height;
              const ctx = target.getContext("2d");
              ctx?.clearRect(0, 0, target.width, target.height);
              ctx?.drawImage(bitmap, 0, 0);
            }
            bitmap.close();
          };
          paint(sourceCanvas.current, data.source); paint(resultCanvas.current, data.preview); paint(overlayCanvas.current, data.overlay);
          setFrame({ canUndo: data.canUndo, canRedo: data.canRedo, canRetune: data.canRetune, removed: data.removed, visible: data.visible });
          const nextCommand = queued.current;
          queued.current = null;
          if (nextCommand) instance?.postMessage(nextCommand);
          else { working.current = false; setProcessing(false); }
        };
        if (retry) imageLoader.clear();
        const layers = imageLoader.load(asset.id);
        const next = await layers.context;
        if (!alive) return;
        setContext(next);
        const [source, result] = await Promise.all([
          layers.source.then((blob) => {
            if (alive) { sourceObjectUrl = URL.createObjectURL(blob); setEarlySource(sourceObjectUrl); }
            return blob;
          }),
          layers.result,
        ]);
        if (!alive) return;
        instance.postMessage({ type: "init", source, result, width: next.width, height: next.height, hasSelection: next.has_initial_selection } satisfies SelectionCommand);
      } catch (reason) { if (alive) fail(reason instanceof Error ? reason.message : "修边图片读取失败"); }
    }
    void load();
    return () => { alive = false; if (!loader) ownLoader.clear(); if (sourceObjectUrl) URL.revokeObjectURL(sourceObjectUrl); window.clearTimeout(timer.current); instance?.terminate(); worker.current = null; };
  }, [asset.id, retry, imageLoader, loader, ownLoader]);

  function send(command: SelectionCommand) {
    if (!worker.current || savingRef.current) return;
    setError(""); setDirty(true);
    if (working.current) { queued.current = command; return; }
    working.current = true; setProcessing(true); worker.current.postMessage(command);
  }
  function tune(command: SelectionCommand) {
    window.clearTimeout(timer.current);
    setTuning(true);
    timer.current = window.setTimeout(() => { setTuning(false); send(command); }, 100);
  }
  function close() {
    if (savingRef.current) return;
    if (dirty) setConfirmClose(true); else onClose();
  }
  const disabled = !frame || processing || tuning || saving;

  return createPortal(<dialog ref={dialog} className="selection-editor" aria-labelledby="selection-editor-title" onCancel={(event) => { event.preventDefault(); close(); }}>
    <header><div><h2 id="selection-editor-title">选区修边</h2><InfoHint label="选区修边使用说明">红色区域将被删除，切换“保留”取消误选。容差会重新计算上次点击的范围；取消“只选相连区域”可选全图同色区域。滚轮同步缩放，预览底色只影响右侧。手动修边不扣积分。</InfoHint></div><button type="button" autoFocus className="selection-close" aria-label="关闭选区修边" disabled={saving} onClick={close}>×</button></header>
    {context?.restore_limited && <p className="selection-warning">这张历史结果未保留去底前的印花原图。可以继续清理残留，但已丢失的像素无法恢复；重新提取后的结果支持恢复。</p>}
    <div className="selection-toolbar">
      <div className="selection-modes" role="group" aria-label="选区操作"><button type="button" aria-pressed={mode === "remove"} disabled={!frame || saving} onClick={() => setMode("remove")}>＋ 删除背景</button><button type="button" aria-pressed={mode === "restore"} disabled={!frame || saving} onClick={() => setMode("restore")}>－ 保留 / 取消误选</button></div>
      <label className="selection-range">容差 <output>{tolerance}</output><input aria-label="选区容差" type="range" min="0" max="100" value={tolerance} disabled={!frame || saving} onChange={(event) => { const value = Number(event.target.value); setTolerance(value); if (frame?.canRetune) tune({ type: "action", action: { type: "tolerance", tolerance: value } }); }} /></label>
      <label><input type="checkbox" checked={contiguous} disabled={disabled} onChange={(event) => setContiguous(event.target.checked)} />只选相连区域</label>
      <label><input type="checkbox" checked={showSelection} onChange={(event) => setShowSelection(event.target.checked)} />显示红色选区</label>
    </div>
    <div className="selection-secondary">
      <div><button type="button" disabled={disabled || !frame?.canUndo} onClick={() => send({ type: "action", action: { type: "undo" } })}>撤销</button><button type="button" disabled={disabled || !frame?.canRedo} onClick={() => send({ type: "action", action: { type: "redo" } })}>重做</button><button type="button" disabled={disabled} onClick={() => send({ type: "action", action: { type: "auto" } })}>补选外围背景</button><button type="button" disabled={disabled} onClick={() => { setEdge(0); send({ type: "action", action: { type: "reset" } }); }}>重置初始选区</button><button type="button" disabled={disabled} onClick={() => { setEdge(0); send({ type: "action", action: { type: "clear" } }); }}>取消全部选区</button></div>
      <label>边缘收缩 <select aria-label="边缘收缩" value={edge} disabled={disabled} onChange={(event) => { const value = Number(event.target.value); setEdge(value); send({ type: "edge", value }); }}>{[0, 1, 2, 3].map((n) => <option key={n} value={n}>{n} 像素</option>)}</select></label>
    </div>
    <div className={`selection-panes selection-bg-${background}`} aria-busy={!frame}>
      <section><h3>选区 · {mode === "remove" ? "点击删除背景" : "点击保留内容"}</h3><div ref={sourceViewport} className="selection-viewport" aria-label="选区画布" onScroll={(event) => syncScroll(event.currentTarget, resultViewport.current)}><div ref={sourceImage} className="selection-image" data-ready={Boolean(frame)} style={{ width: fitWidth * zoom, aspectRatio: `${imageWidth}/${imageHeight}` }}>
        {!frame && (earlySource ? <img className="selection-loading-preview" src={earlySource} alt="去底前原图预览" /> : <div className="selection-placeholder">{processing ? "正在载入原尺寸图片…" : "原图尚未载入"}</div>)}
        <canvas ref={sourceCanvas} aria-label="去底前原图" /><canvas ref={overlayCanvas} className="selection-overlay" style={{ opacity: showSelection ? 1 : 0 }} aria-label="点击图片编辑背景选区" onPointerDown={(event) => {
        if (disabled || event.button !== 0 || !context) return;
        const rect = event.currentTarget.getBoundingClientRect();
        send({ type: "action", action: { type: "wand", x: (event.clientX - rect.left) / rect.width * context.width, y: (event.clientY - rect.top) / rect.height * context.height, tolerance, contiguous, mode } });
      }} /></div></div></section>
      <section><h3>实时预览 · 透明 PNG</h3><div ref={resultViewport} className="selection-viewport" aria-label="结果画布" onScroll={(event) => syncScroll(event.currentTarget, sourceViewport.current)}><div ref={resultImage} className="selection-image" data-ready={Boolean(frame)} style={{ width: fitWidth * zoom, aspectRatio: `${imageWidth}/${imageHeight}` }}>
        {!frame && (previewUrl && failedPreview !== previewUrl ? <><img className="selection-loading-preview" src={previewUrl} alt="已有结果预览" onError={() => setFailedPreview(previewUrl)} /><span className="selection-preview-caption">已有预览 · 原图载入后可编辑</span></> : <div className="selection-placeholder">{processing ? "正在准备透明预览…" : "预览尚未载入"}</div>)}
        <canvas ref={resultCanvas} aria-label="去掉选区后的实时预览" /></div></div></section>
    </div>
    <div className="selection-view-controls"><div className="selection-zoom" role="group" aria-label="画布同步缩放">
      <button type="button" aria-label="缩小修边预览" disabled={zoom <= MIN_ZOOM} onClick={() => changeZoom(zoomRef.current / 1.25)}>−</button>
      <label>缩放 <select aria-label="修边预览缩放" title="相对适合画布的缩放比例" value={zoom} onChange={(event) => changeZoom(Number(event.target.value))}>
        {!ZOOM_PRESETS.includes(zoom) && <option value={zoom}>{zoomLabel(zoom)}</option>}
        {ZOOM_PRESETS.map((value) => <option key={value} value={value}>{value === 1 ? "适合画布" : zoomLabel(value)}</option>)}
      </select></label>
      <button type="button" aria-label="放大修边预览" disabled={zoom >= MAX_ZOOM} onClick={() => changeZoom(zoomRef.current * 1.25)}>＋</button>
    </div><label>预览底色 <select aria-label="修边预览底色" value={background} onChange={(event) => setBackground(event.target.value)}><option value="checker">透明棋盘格</option><option value="white">白色</option><option value="dark">黑色</option></select></label><span role="status">{saving ? "正在保存原尺寸 PNG…" : processing || tuning ? frame ? "正在更新选区与预览…" : "正在载入原图与智能选区…" : context && frame ? `${context.width} × ${context.height} · 已选 ${Math.round(frame.removed / (context.width * context.height) * 1000) / 10}%` : "载入未完成"}</span></div>
    {error && <p role="alert" className="selection-error">{error} {!frame && <button type="button" onClick={() => setRetry((n) => n + 1)}>重新载入</button>}</p>}
    {confirmClose && <div className="selection-warning">修边尚未保存。<button type="button" onClick={() => setConfirmClose(false)}>继续修边</button><button type="button" onClick={onClose}>放弃修改并关闭</button></div>}
    <footer><span>手动修边不扣积分 · 原图保留 · 另存新版本</span><button type="button" disabled={disabled || !frame?.visible} onClick={() => { if (working.current || savingRef.current || tuning) return; setSaving(true); savingRef.current = true; setError(""); worker.current?.postMessage({ type: "export" } satisfies SelectionCommand); }}>保存修边结果</button></footer>
  </dialog>, document.body);
}
