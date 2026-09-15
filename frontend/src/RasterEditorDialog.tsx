import { useEffect, useLayoutEffect, useRef, useState, type PointerEvent } from "react";
import { createPortal } from "react-dom";
import { Brush, CircleDashed, Eraser, Hand, Lasso, PaintBucket, Pipette, Redo2, SquareDashed, Undo2, WandSparkles, X } from "lucide-react";
import { api, selectionPixels, type Asset } from "./user-api";
import { InfoHint } from "./InfoHint";
import { RasterLayersPanel } from "./RasterLayersPanel";
import type { Point, SelectionMode } from "./raster-editor";
import type { RasterCommand, RasterReply, RasterState } from "./raster-editor.worker";
import "./raster-editor.css";

type Tool = "rectangle" | "ellipse" | "lasso" | "wand" | "bucket" | "brush" | "eraser" | "eyedropper" | "hand";
const TOOLS = [
  { code: "rectangle", label: "矩形选框", key: "M", Icon: SquareDashed },
  { code: "ellipse", label: "椭圆选框", key: "U", Icon: CircleDashed },
  { code: "lasso", label: "自由套索", key: "L", Icon: Lasso },
  { code: "wand", label: "魔棒选区", key: "W", Icon: WandSparkles },
  { code: "bucket", label: "油漆桶", key: "G", Icon: PaintBucket },
  { code: "brush", label: "画笔", key: "B", Icon: Brush },
  { code: "eraser", label: "橡皮擦", key: "E", Icon: Eraser },
  { code: "eyedropper", label: "吸管取色", key: "I", Icon: Pipette },
  { code: "hand", label: "抓手移动", key: "H", Icon: Hand },
] as const;
const HINTS: Record<Tool, string> = {
  rectangle: "拖动框选矩形区域；Shift 加选，Alt 减选。", ellipse: "拖动框选椭圆区域；Shift 加选，Alt 减选。",
  lasso: "按住拖动圈出区域，松开后自动闭合。", wand: "点击相近颜色选区，可调整容差并选择是否仅限相连区域。",
  bucket: "点击填充相近颜色；只修改当前图层，遵守现有选区。取样所有可见图层可在空白新图层上沿原图区域填色。",
  brush: "按住涂画；存在选区时只修改选区内像素。", eraser: "按住擦除为透明；存在选区时只擦除选区内像素。",
  eyedropper: "点击图片吸取颜色，可从原图或当前编辑图读取。", hand: "拖动平移画布；滚轮以鼠标位置为中心缩放。",
};
type Gesture = { tool: Tool; points: Point[]; mode: SelectionMode; pointerId: number; client: Point; scroll: Point };
const limit = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));

export function RasterEditorDialog({ asset, previewUrl, onClose, onSaved }: { asset: Asset; previewUrl?: string | null; onClose: () => void; onSaved: (asset: Asset) => void }) {
  const width = asset.width || 1, height = asset.height || 1;
  const dialog = useRef<HTMLDialogElement>(null), viewport = useRef<HTMLDivElement>(null), surface = useRef<HTMLDivElement>(null);
  const preview = useRef<HTMLCanvasElement>(null), overlay = useRef<HTMLCanvasElement>(null), original = useRef<HTMLCanvasElement>(null);
  const worker = useRef<Worker | null>(null), inFlight = useRef(false), pending = useRef<RasterCommand[]>([]);
  const drag = useRef<Gesture | null>(null), savingRef = useRef(false), saved = useRef(onSaved);
  saved.current = onSaved;
  const zoomValue = useRef(1), scaleValue = useRef(1);
  const anchor = useRef<{ point: Point; offset: Point } | null>(null);
  const [state, setState] = useState<RasterState | null>(null), [processing, setProcessing] = useState(true), [saving, setSaving] = useState(false);
  const [tool, setTool] = useState<Tool>("rectangle"), [mode, setMode] = useState<SelectionMode>("replace");
  const [tolerance, setTolerance] = useState(12), [contiguous, setContiguous] = useState(true);
  const [sampleMerged, setSampleMerged] = useState(true), [contentOnly, setContentOnly] = useState(true);
  const [panel, setPanel] = useState<"tools" | "layers">("tools");
  const [color, setColor] = useState("#FF6600"), [colorText, setColorText] = useState("#FF6600");
  const [opacity, setOpacity] = useState(100), [size, setSize] = useState(30), [sampleOriginal, setSampleOriginal] = useState(true);
  const [zoom, setZoom] = useState(1), [fit, setFit] = useState(1), [showSelection, setShowSelection] = useState(true), [showOriginal, setShowOriginal] = useState(false);
  const [guide, setGuide] = useState<Gesture | null>(null), [cursor, setCursor] = useState<Point | null>(null), [drawing, setDrawing] = useState(false);
  const [error, setError] = useState(""), [retry, setRetry] = useState(0), [confirmClose, setConfirmClose] = useState(false);
  const disabled = !state || processing || saving || drawing;
  const paintTool = tool === "brush" || tool === "eraser";
  const selecting = ["rectangle", "ellipse", "lasso", "wand"].includes(tool);
  const activeLayer = state?.layers.find((layer) => layer.id === state.activeId);
  const paintDisabled = disabled || showOriginal || Boolean(activeLayer?.locked || !activeLayer?.visible || !activeLayer?.opacity);
  scaleValue.current = fit * zoom;

  function pump() {
    if (inFlight.current || !worker.current || !pending.current.length) return;
    inFlight.current = true; setProcessing(true);
    worker.current.postMessage(pending.current.shift()!);
  }
  function send(command: RasterCommand) {
    if (!worker.current || savingRef.current) return;
    setError("");
    const last = pending.current.at(-1);
    if (command.type === "stroke-move" && last?.type === "stroke-move") last.points.push(...command.points);
    else pending.current.push(command);
    pump();
  }
  function chooseColor(value: string) { setColor(value); setColorText(value); }
  function close() {
    if (savingRef.current) return;
    if (state?.changed || drawing || pending.current.length) setConfirmClose(true);
    else onClose();
  }
  function save() {
    if (disabled || !state?.changed || !state.visible || savingRef.current) return;
    savingRef.current = true; setSaving(true); setError("");
    inFlight.current = true; worker.current?.postMessage({ type: "export" } satisfies RasterCommand);
  }
  useLayoutEffect(() => {
    const element = dialog.current!, previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow; document.body.style.overflow = "hidden";
    if (element.showModal) element.showModal(); else element.setAttribute("open", "");
    element.querySelector<HTMLButtonElement>("[aria-label='关闭基础编辑']")?.focus({ preventScroll: true });
    return () => { element.close?.(); document.body.style.overflow = overflow; if (previous?.isConnected) previous.focus(); };
  }, []);
  useLayoutEffect(() => {
    const target = viewport.current!;
    const resize = () => setFit(Math.max(.01, Math.min((target.clientWidth - 32) / width, (target.clientHeight - 32) / height, 1)));
    resize(); const observer = new ResizeObserver(resize); observer.observe(target);
    return () => observer.disconnect();
  }, [width, height]);
  function changeZoom(value: number, clientX?: number, clientY?: number) {
    const next = limit(value, .25, 16), view = viewport.current, image = surface.current;
    if (!view || !image || !Number.isFinite(next)) return;
    const rect = image.getBoundingClientRect(), bounds = view.getBoundingClientRect();
    const offset = { x: clientX === undefined ? view.clientWidth / 2 : clientX - bounds.left - view.clientLeft, y: clientY === undefined ? view.clientHeight / 2 : clientY - bounds.top - view.clientTop };
    anchor.current = { point: { x: (offset.x + bounds.left + view.clientLeft - rect.left) / scaleValue.current, y: (offset.y + bounds.top + view.clientTop - rect.top) / scaleValue.current }, offset };
    zoomValue.current = next; setZoom(next);
  }
  useLayoutEffect(() => {
    const value = anchor.current, view = viewport.current;
    if (!value || !view) return;
    anchor.current = null;
    view.scrollLeft = Math.max(0, (view.clientWidth - width * fit * zoom) / 2) + value.point.x * fit * zoom - value.offset.x;
    view.scrollTop = Math.max(16, (view.clientHeight - height * fit * zoom) / 2) + value.point.y * fit * zoom - value.offset.y;
  }, [zoom, fit, width, height]);
  useEffect(() => {
    const view = viewport.current!;
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      if (drag.current) return;
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? view.clientHeight : 1;
      changeZoom(zoomValue.current * Math.exp(-limit(event.deltaY * unit, -500, 500) * .002), event.clientX, event.clientY);
    };
    view.addEventListener("wheel", wheel, { passive: false });
    return () => view.removeEventListener("wheel", wheel);
  }, []);
  useEffect(() => {
    let alive = true; const controller = new AbortController();
    setState(null); setProcessing(true); setError(""); pending.current = []; inFlight.current = true;
    const fail = (message: string) => {
      pending.current = []; inFlight.current = false; savingRef.current = false; drag.current = null;
      setProcessing(false); setSaving(false); setDrawing(false); setGuide(null); setError(message);
    };
    let instance: Worker | null = null;
    async function load() {
      try {
        if (!window.Worker || !window.OffscreenCanvas || !window.createImageBitmap) throw new Error("当前浏览器不支持基础编辑，请使用新版 Chrome、Edge 或 Firefox");
        instance = new Worker(new URL("./raster-editor.worker.ts", import.meta.url), { type: "module" });
        worker.current = instance;
        instance.onerror = () => { if (alive) { instance?.terminate(); worker.current = null; setState(null); fail("编辑器异常，请重新载入；大图可先缩小后重试"); } };
        instance.onmessage = async ({ data }: MessageEvent<RasterReply>) => {
          if (!alive) { if (data.type === "frame") { data.preview.close(); data.overlay.close(); data.original?.close(); } return; }
          if (data.type === "error") { fail(data.message); return; }
          if (data.type === "export") {
            try { const result = await api.saveRasterEdit(asset.id, data.blob, data.project); if (alive) saved.current(result.asset); }
            catch (reason) { if (alive) fail(reason instanceof Error ? reason.message : "保存失败，请重试"); }
            return;
          }
          if (data.type === "sample") chooseColor(data.color);
          if (data.type === "frame") {
            const paint = (target: HTMLCanvasElement | null, bitmap?: ImageBitmap) => {
              if (!bitmap) return;
              if (target) {
                if (target.width !== bitmap.width) target.width = bitmap.width;
                if (target.height !== bitmap.height) target.height = bitmap.height;
                const context = target.getContext("2d"); context?.clearRect(0, 0, target.width, target.height); context?.drawImage(bitmap, 0, 0);
              }
              bitmap.close();
            };
            paint(preview.current, data.preview); paint(overlay.current, data.overlay); paint(original.current, data.original); setState(data.state);
          }
          inFlight.current = false;
          if (pending.current.length) pump(); else setProcessing(false);
        };
        const [blob, project] = await Promise.all([
          selectionPixels(`/api/v1/assets/${asset.id}/selection/result`, controller.signal),
          asset.metadata?.raster_project_ready ? selectionPixels(`/api/v1/assets/${asset.id}/edit/project`, controller.signal) : Promise.resolve(undefined),
        ]);
        if (alive) instance.postMessage({ type: "init", blob, width, height, project } satisfies RasterCommand);
      } catch (reason) { if (alive) fail(reason instanceof Error ? reason.message : "原图读取失败"); }
    }
    void load();
    return () => { alive = false; controller.abort(); instance?.terminate(); worker.current = null; pending.current = []; };
  }, [asset.id, width, height, retry]);

  function point(event: PointerEvent<HTMLDivElement>): Point {
    const rect = event.currentTarget.getBoundingClientRect();
    return { x: limit((event.clientX - rect.left) * width / rect.width, 0, width - 1), y: limit((event.clientY - rect.top) * height / rect.height, 0, height - 1) };
  }
  function begin(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0 || !state || savingRef.current || (processing && tool !== "hand") || (showOriginal && tool !== "hand" && tool !== "eyedropper")) return;
    event.preventDefault(); event.currentTarget.focus({ preventScroll: true });
    const p = point(event), selectionMode = event.altKey ? "subtract" : event.shiftKey ? "add" : mode;
    if ((paintTool || tool === "bucket") && paintDisabled) { setError("当前图层不可绘制，请在图层面板解锁、显示图层并提高不透明度"); return; }
    if (tool === "eyedropper") { send({ type: "sample", point: p, original: showOriginal || sampleOriginal }); return; }
    if (tool === "wand") { send({ type: "action", action: { type: "wand", point: p, mode: selectionMode, tolerance, contiguous } }); return; }
    if (tool === "bucket") { send({ type: "action", action: { type: "bucket", point: p, tolerance, contiguous, sampleMerged, color, opacity } }); return; }
    drag.current = { tool, points: [p], mode: selectionMode, pointerId: event.pointerId, client: { x: event.clientX, y: event.clientY }, scroll: { x: viewport.current!.scrollLeft, y: viewport.current!.scrollTop } };
    event.currentTarget.setPointerCapture(event.pointerId); setDrawing(true);
    if (paintTool) send({ type: "stroke-begin", point: p, options: { color, opacity, size, erase: tool === "eraser" } });
    else if (tool !== "hand") setGuide({ ...drag.current });
  }
  function move(event: PointerEvent<HTMLDivElement>) {
    const p = point(event); setCursor(p);
    const gesture = drag.current;
    if (!gesture || gesture.pointerId !== event.pointerId) return;
    if (gesture.tool === "hand") {
      viewport.current!.scrollLeft = gesture.scroll.x + gesture.client.x - event.clientX;
      viewport.current!.scrollTop = gesture.scroll.y + gesture.client.y - event.clientY;
    } else if (gesture.tool === "brush" || gesture.tool === "eraser") send({ type: "stroke-move", points: [p] });
    else {
      gesture.points = gesture.tool === "lasso" ? [...gesture.points, p] : [gesture.points[0], p];
      setGuide({ ...gesture });
    }
  }
  function end(event: PointerEvent<HTMLDivElement>, cancel = false) {
    const gesture = drag.current;
    if (!gesture || gesture.pointerId !== event.pointerId) return;
    drag.current = null; setDrawing(false); setGuide(null);
    if (gesture.tool === "brush" || gesture.tool === "eraser") {
      if (!cancel) send({ type: "stroke-move", points: [point(event)] });
      send({ type: "stroke-end", cancel });
    } else if (!cancel && (gesture.tool === "rectangle" || gesture.tool === "ellipse" || gesture.tool === "lasso")) {
      const points = gesture.tool === "lasso" ? [...gesture.points, point(event)] : [gesture.points[0], point(event)];
      send({ type: "action", action: { type: "shape", shape: gesture.tool, points, mode: gesture.mode, contentOnly } });
    }
  }
  const first = guide?.points[0], last = guide?.points.at(-1);
  return createPortal(<dialog ref={dialog} className="raster-editor" aria-labelledby="raster-editor-title" onCancel={(event) => { event.preventDefault(); close(); }} onKeyDown={(event) => {
    if (event.target instanceof HTMLElement && event.target.closest("input, textarea, select")) return;
    const key = event.key.toLowerCase(), ctrl = event.ctrlKey || event.metaKey;
    if (disabled) return;
    if (ctrl && ["z", "y", "a", "d", "s"].includes(key)) {
      event.preventDefault();
      if (key === "s") save();
      else send({ type: "action", action: key === "a" || key === "d" ? { type: "selection", operation: key === "a" ? "all" : "none" } : { type: key === "y" || event.shiftKey ? "redo" : "undo" } });
    } else if ((key === "delete" || key === "backspace") && !paintDisabled) { event.preventDefault(); send({ type: "action", action: { type: "delete" } }); }
    else if (!ctrl && !event.altKey) { const chosen = TOOLS.find((item) => item.key.toLowerCase() === key); if (chosen) { event.preventDefault(); setTool(chosen.code); } }
  }}>
    <header className="raster-header"><div><h2 id="raster-editor-title">基础编辑</h2><InfoHint label="基础编辑说明">先框选或用魔棒选区，再填色、涂画或擦除。没有选区时可绘制全图，空选区不会修改图片。撤销 Ctrl+Z，重做 Ctrl+Shift+Z，全选 Ctrl+A，取消选区 Ctrl+D。保存为新素材，不扣积分。</InfoHint></div><button type="button" autoFocus aria-label="关闭基础编辑" disabled={saving} onClick={close}><X size={18} /></button></header>
    <div className="raster-topbar"><div><button type="button" aria-label="撤销编辑" disabled={disabled || !state?.canUndo} onClick={() => send({ type: "action", action: { type: "undo" } })}><Undo2 size={16} />撤销</button><button type="button" aria-label="重做编辑" disabled={disabled || !state?.canRedo} onClick={() => send({ type: "action", action: { type: "redo" } })}><Redo2 size={16} />重做</button><button type="button" disabled={disabled || !state?.changed} onClick={() => send({ type: "action", action: { type: "reset" } })}>还原原图</button></div><div><button type="button" aria-pressed={showOriginal} disabled={!state || drawing} onClick={() => setShowOriginal((value) => !value)}>查看原图</button><label><input type="checkbox" checked={showSelection} onChange={(event) => setShowSelection(event.target.checked)} />显示选区</label></div></div>
    <div className="raster-layout">
      <nav className="raster-tools" aria-label="基础编辑工具">{TOOLS.map(({ code, label, key, Icon }) => <button key={code} type="button" title={`${label} (${key})`} aria-label={label} aria-pressed={tool === code} disabled={disabled} onClick={() => { setTool(code); setCursor(null); }}><Icon size={20} /><span>{label}</span></button>)}</nav>
      <div className="raster-workspace"><div ref={viewport} className="raster-viewport" aria-label="基础编辑视口"><div className="raster-surface-wrap" style={{ minWidth: width * fit * zoom + 32, minHeight: height * fit * zoom + 32 }}><div ref={surface} className={`raster-surface tool-${tool}`} role="application" tabIndex={0} aria-label="图片编辑画布" style={{ width: width * fit * zoom, height: height * fit * zoom }} onPointerDown={begin} onPointerMove={move} onPointerUp={(event) => end(event)} onPointerCancel={(event) => end(event, true)} onLostPointerCapture={(event) => end(event, true)} onPointerLeave={() => setCursor(null)}>
        {!state && previewUrl && <img src={previewUrl} alt="基础编辑原图预览" draggable={false} />}
        <canvas ref={preview} style={{ visibility: state && !showOriginal ? "visible" : "hidden" }} />
        <canvas ref={original} style={{ visibility: state && showOriginal ? "visible" : "hidden" }} />
        <canvas ref={overlay} style={{ visibility: state && showSelection && !showOriginal ? "visible" : "hidden" }} />
        <svg viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
          {guide && first && last && (guide.tool === "lasso" ? <polygon points={guide.points.map((p) => `${p.x},${p.y}`).join(" ")} /> : guide.tool === "ellipse" ? <ellipse cx={(first.x + last.x) / 2} cy={(first.y + last.y) / 2} rx={Math.abs(last.x - first.x) / 2} ry={Math.abs(last.y - first.y) / 2} /> : <rect x={Math.min(first.x, last.x)} y={Math.min(first.y, last.y)} width={Math.abs(last.x - first.x)} height={Math.abs(last.y - first.y)} />)}
          {cursor && paintTool && !showOriginal && <circle className="raster-brush-cursor" cx={cursor.x} cy={cursor.y} r={size / 2} />}
        </svg>
      </div></div>{!state && <div className="raster-loading" role="status">{processing ? "正在载入原图…" : "原图尚未载入"}</div>}</div><div className="raster-status"><span>{width} × {height} · {state?.selected === null ? "未限定选区" : `已选 ${state?.selected?.toLocaleString() ?? 0} 像素`}</span><label>缩放<select aria-label="基础编辑缩放" value={zoom} disabled={drawing} onChange={(event) => changeZoom(Number(event.target.value))}>{[...new Set([.25, .5, 1, 2, 4, 8, 16, zoom])].sort((a, b) => a - b).map((value) => <option key={value} value={value}>{value === 1 ? "适应画布" : `${Math.round(value * 100)}%`}</option>)}</select></label></div></div>
      <aside className="raster-settings"><div className="raster-panel-tabs" role="tablist" aria-label="编辑面板"><button type="button" role="tab" aria-selected={panel === "tools"} aria-controls="raster-tool-panel" id="raster-tool-tab" onClick={() => setPanel("tools")}>工具与颜色</button><button type="button" role="tab" aria-selected={panel === "layers"} aria-controls="raster-layers-panel" id="raster-layers-tab" onClick={() => setPanel("layers")}>图层{state ? ` · ${state.layers.length}` : ""}</button></div>
      <div className="raster-active-layer"><span>当前图层</span><button type="button" onClick={() => setPanel("layers")}>{activeLayer?.name ?? "正在载入"}{activeLayer?.locked ? " · 已锁定" : activeLayer && (!activeLayer.visible || !activeLayer.opacity) ? " · 不可见" : ""}</button></div>
      <div id="raster-layers-panel" role="tabpanel" aria-labelledby="raster-layers-tab" hidden={panel !== "layers"}>{state && <RasterLayersPanel state={state} disabled={disabled || showOriginal} act={(action) => send({ type: "action", action })} />}</div>
      <div id="raster-tool-panel" role="tabpanel" aria-labelledby="raster-tool-tab" hidden={panel !== "tools"}><section><div className="raster-tool-heading"><h3>{TOOLS.find((item) => item.code === tool)?.label}</h3><InfoHint label="当前工具操作说明">{HINTS[tool]}</InfoHint></div>
        {selecting && <label>选区方式<select value={mode} disabled={disabled} onChange={(event) => setMode(event.target.value as SelectionMode)}><option value="replace">新建选区</option><option value="add">添加到选区</option><option value="subtract">从选区减去</option><option value="intersect">与选区交叉</option></select></label>}
        {selecting && tool !== "wand" && <div className="raster-content-option"><label className="raster-check"><input aria-label="只框选当前图层像素" type="checkbox" checked={contentOnly} disabled={disabled} onChange={(event) => setContentOnly(event.target.checked)} />只框选当前图层像素</label><InfoHint label="框选内容说明">自动排除透明区域及内部镂空。需要在空白区域绘制时可关闭此项。</InfoHint></div>}
        {(tool === "wand" || tool === "bucket") && <><label>容差 <output>{tolerance}</output><input aria-label="容差" type="range" min="0" max="100" value={tolerance} disabled={disabled} onChange={(event) => setTolerance(Number(event.target.value))} /></label><label className="raster-check"><input type="checkbox" checked={contiguous} disabled={disabled} onChange={(event) => setContiguous(event.target.checked)} />仅限相连区域</label></>}
        {tool === "bucket" && <label>取样范围<select value={sampleMerged ? "merged" : "layer"} disabled={disabled} onChange={(event) => setSampleMerged(event.target.value === "merged")}><option value="merged">所有可见图层</option><option value="layer">当前图层</option></select></label>}
        {paintTool && <label>笔刷大小（像素）<input type="number" min="1" max="1000" value={size} disabled={disabled} onChange={(event) => { if (Number.isFinite(event.target.valueAsNumber)) setSize(limit(event.target.valueAsNumber, 1, 1000)); }} /></label>}
        {tool === "eyedropper" && <label>取色来源<select value={sampleOriginal ? "original" : "current"} disabled={disabled} onChange={(event) => setSampleOriginal(event.target.value === "original")}><option value="original">原图颜色</option><option value="current">当前编辑图</option></select></label>}
      </section><section><h3>颜色与填充</h3><div className="raster-color"><input aria-label="绘制颜色" type="color" value={color} disabled={disabled} onChange={(event) => chooseColor(event.target.value)} /><input aria-label="颜色值" type="text" value={colorText} maxLength={7} disabled={disabled} onChange={(event) => { const value = event.target.value; setColorText(value); if (/^#[\da-f]{6}$/i.test(value)) setColor(value); }} onBlur={() => setColorText(color)} /><button type="button" aria-label="启用吸管取色" disabled={disabled} onClick={() => setTool("eyedropper")}><Pipette size={17} /></button></div><label>不透明度 <output>{opacity}%</output><input type="range" min="1" max="100" value={opacity} disabled={disabled} onChange={(event) => setOpacity(Number(event.target.value))} /></label><button className="raster-fill" type="button" disabled={paintDisabled || state?.selected === 0} onClick={() => send({ type: "action", action: { type: "fill", color, opacity } })}><PaintBucket size={16} />{state?.selected === null ? "填充全图" : "填充选区"}</button></section>
        <section><h3>选区操作</h3><div className="raster-selection-actions"><button type="button" disabled={disabled} onClick={() => send({ type: "action", action: { type: "selection", operation: "all" } })}>全选</button><button type="button" disabled={disabled || state?.selected === null} onClick={() => send({ type: "action", action: { type: "selection", operation: "none" } })}>取消选区</button><button type="button" disabled={disabled} onClick={() => send({ type: "action", action: { type: "selection", operation: "invert" } })}>反选</button><button type="button" disabled={paintDisabled || !state?.selected} onClick={() => send({ type: "action", action: { type: "delete" } })}>清除选区内容</button></div></section>
      </div></aside>
    </div>
    {error && <div className="raster-error" role="alert">{error}{!state && <button type="button" onClick={() => setRetry((value) => value + 1)}>重新载入</button>}</div>}
    {confirmClose && <div className="raster-confirm" role="alert">有未保存的编辑内容。<button type="button" onClick={() => setConfirmClose(false)}>继续编辑</button><button type="button" onClick={onClose}>放弃修改并关闭</button></div>}
    <footer><span role="status">{saving ? "正在保存图片与图层…" : processing ? "正在更新画布…" : showOriginal ? "正在查看原图，切回后继续绘制" : "保存为新版本 · 保留图层 · 不扣积分"}</span><button type="button" className="raster-save" disabled={disabled || !state?.changed || !state.visible} onClick={save}>{saving ? "正在保存…" : "保存编辑结果"}</button></footer>
  </dialog>, document.body);
}
