import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, selectionPixels, type Asset, type SelectionContext } from "./user-api";
import type { SelectionCommand, SelectionReply } from "./background-selection.worker";
import "./background-selection.css";

type FrameState = { canUndo: boolean; canRedo: boolean; canRetune: boolean; removed: number; visible: number };

export function BackgroundSelectionEditor({ asset, onClose, onSaved }: { asset: Asset; onClose: () => void; onSaved: (asset: Asset) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const sourceCanvas = useRef<HTMLCanvasElement>(null);
  const overlayCanvas = useRef<HTMLCanvasElement>(null);
  const resultCanvas = useRef<HTMLCanvasElement>(null);
  const sourceViewport = useRef<HTMLDivElement>(null);
  const resultViewport = useRef<HTMLDivElement>(null);
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

  useEffect(() => {
    const element = dialog.current!;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (element.showModal) element.showModal(); else element.setAttribute("open", "");
    return () => { element.close?.(); if (previous?.isConnected) previous.focus(); };
  }, []);

  useEffect(() => {
    const viewport = sourceViewport.current;
    if (!viewport || !context) return;
    const fit = () => setFitWidth(Math.min(viewport.clientWidth, viewport.clientHeight * context.width / context.height));
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(viewport);
    return () => observer.disconnect();
  }, [context]);

  useEffect(() => {
    let alive = true;
    const abort = new AbortController();
    let instance: Worker | null = null;
    setError(""); setContext(null); setFrame(null); setProcessing(true); setDirty(false);
    setEdge(0); setTuning(false); queued.current = null; working.current = true;
    const fail = (message: string) => {
      working.current = false; queued.current = null; savingRef.current = false;
      setProcessing(false); setSaving(false); setError(message);
    };
    async function load() {
      try {
        if (!window.Worker || !window.OffscreenCanvas || !window.createImageBitmap) throw new Error("当前浏览器不支持选区修边，请使用新版 Chrome、Edge 或 Firefox");
        const next = await api.selection(asset.id);
        const [source, result] = await Promise.all([selectionPixels(next.source_url, abort.signal), selectionPixels(next.result_url, abort.signal)]);
        if (!alive) return;
        setContext(next);
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
              target.width = bitmap.width; target.height = bitmap.height;
              target.getContext("2d")?.drawImage(bitmap, 0, 0);
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
        instance.postMessage({ type: "init", source, result, width: next.width, height: next.height, hasSelection: next.has_initial_selection } satisfies SelectionCommand);
      } catch (reason) { if (alive) fail(reason instanceof Error ? reason.message : "修边图片读取失败"); }
    }
    void load();
    return () => { alive = false; abort.abort(); window.clearTimeout(timer.current); instance?.terminate(); worker.current = null; };
  }, [asset.id, retry]);

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
    <header><div><small>BACKGROUND SELECTION</small><h2 id="selection-editor-title">选区修边</h2><p>红色区域将被删除。点击补选残留，切换“保留”取消误选。</p></div><button type="button" className="selection-close" aria-label="关闭选区修边" disabled={saving} onClick={close}>×</button></header>
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
    <div className={`selection-panes selection-bg-${background}`}>
      <section><h3>选区 · {mode === "remove" ? "点击删除背景" : "点击保留内容"}</h3><div ref={sourceViewport} className="selection-viewport" onScroll={(event) => { if (resultViewport.current) { resultViewport.current.scrollLeft = event.currentTarget.scrollLeft; resultViewport.current.scrollTop = event.currentTarget.scrollTop; } }}><div className="selection-image" style={{ width: fitWidth * zoom, aspectRatio: context ? `${context.width}/${context.height}` : "1" }}><canvas ref={sourceCanvas} aria-label="去底前原图" /><canvas ref={overlayCanvas} className="selection-overlay" style={{ opacity: showSelection ? 1 : 0 }} aria-label="点击图片编辑背景选区" onPointerDown={(event) => {
        if (disabled || event.button !== 0 || !context) return;
        const rect = event.currentTarget.getBoundingClientRect();
        send({ type: "action", action: { type: "wand", x: (event.clientX - rect.left) / rect.width * context.width, y: (event.clientY - rect.top) / rect.height * context.height, tolerance, contiguous, mode } });
      }} /></div></div></section>
      <section><h3>实时预览 · 透明 PNG</h3><div ref={resultViewport} className="selection-viewport" onScroll={(event) => { if (sourceViewport.current) { sourceViewport.current.scrollLeft = event.currentTarget.scrollLeft; sourceViewport.current.scrollTop = event.currentTarget.scrollTop; } }}><div className="selection-image" style={{ width: fitWidth * zoom, aspectRatio: context ? `${context.width}/${context.height}` : "1" }}><canvas ref={resultCanvas} aria-label="去掉选区后的实时预览" /></div></div></section>
    </div>
    <div className="selection-view-controls"><label>放大检查 <select aria-label="修边预览缩放" value={zoom} onChange={(event) => setZoom(Number(event.target.value))}>{[[1, "适合画布"], [1.5, "150%"], [2, "200%"], [4, "400%"]].map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label>预览底色 <select aria-label="修边预览底色" value={background} onChange={(event) => setBackground(event.target.value)}><option value="checker">透明棋盘格</option><option value="white">白色</option><option value="dark">黑色</option></select></label><span role="status">{saving ? "正在保存原尺寸 PNG…" : processing || tuning ? frame ? "正在更新选区与预览…" : "正在载入原图与智能选区…" : context && frame ? `${context.width} × ${context.height} · 已选 ${Math.round(frame.removed / (context.width * context.height) * 1000) / 10}%` : "载入未完成"}</span></div>
    <p className="selection-hint">{frame?.canRetune ? "拖动容差会重新计算上次点击的范围，不会累积误删。" : "先显示已有抠图选区；原图会尝试识别相连的单色外围背景。"} 取消“只选相连区域”可一次选中全图同色区域，请检查图案内部。细边可尝试收缩 1 像素。</p>
    {error && <p role="alert" className="selection-error">{error} {!frame && <button type="button" onClick={() => setRetry((n) => n + 1)}>重新载入</button>}</p>}
    {confirmClose && <div className="selection-warning">修边尚未保存。<button type="button" onClick={() => setConfirmClose(false)}>继续修边</button><button type="button" onClick={onClose}>放弃修改并关闭</button></div>}
    <footer><span>手动修边不扣积分 · 原图保留 · 另存新版本</span><button type="button" disabled={disabled || !frame?.visible} onClick={() => { if (working.current || savingRef.current || tuning) return; setSaving(true); savingRef.current = true; setError(""); worker.current?.postMessage({ type: "export" } satisfies SelectionCommand); }}>保存修边结果</button></footer>
  </dialog>, document.body);
}
