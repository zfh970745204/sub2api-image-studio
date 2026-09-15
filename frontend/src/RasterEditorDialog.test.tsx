import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RasterEditorDialog } from "./RasterEditorDialog";
import type { Asset } from "./user-api";
import type { RasterReply } from "./raster-editor.worker";

class FakeWorker {
  static latest: FakeWorker;
  postMessage = vi.fn(); terminate = vi.fn();
  onmessage: ((event: { data: RasterReply }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() { FakeWorker.latest = this; }
  emit(data: RasterReply) { act(() => { this.onmessage?.({ data }); }); }
}
const bitmap = () => ({ width:400, height:300, close:vi.fn() } as unknown as ImageBitmap);
const baseLayer = { id:"base", name:"原图", visible:true, locked:false, opacity:100 };
const frame = (selected: number | null = null, changed = false): RasterReply => ({ type:"frame", preview:bitmap(), overlay:bitmap(), original:bitmap(), state: { canUndo:changed || selected !== null, canRedo:false, changed, selected, visible:1000, activeId:"base", maxLayers:12, layers:[baseLayer] } });
const asset = { id:"photo-1", width:400, height:300 } as Asset;

describe("basic editor workflow", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    vi.stubGlobal("Worker", FakeWorker); vi.stubGlobal("OffscreenCanvas", class {});
    vi.stubGlobal("createImageBitmap", vi.fn());
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
    HTMLElement.prototype.setPointerCapture = vi.fn();
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(400);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(300);
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ drawImage:vi.fn(), clearRect:vi.fn() } as unknown as CanvasRenderingContext2D);
    fetchMock = vi.fn(async (_url: string, init?: RequestInit) => init?.method === "POST" ? new Response(JSON.stringify({ asset: { id:"edited-1" } })) : new Response(new Blob(["pixels"])));
    vi.stubGlobal("fetch", fetchMock);
  });
  async function ready(onSaved = vi.fn(), onClose = vi.fn()) {
    const view = render(<RasterEditorDialog asset={asset} onSaved={onSaved} onClose={onClose} />);
    await waitFor(() => expect(FakeWorker.latest.postMessage).toHaveBeenCalledWith(expect.objectContaining({ type:"init", width:400, height:300 })));
    FakeWorker.latest.emit(frame());
    const canvas = screen.getByRole("application", { name:"图片编辑画布" });
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({ left:10, top:20, width:200, height:150 } as DOMRect);
    return { ...view, canvas };
  }
  it("loads the current version once, maps shape coordinates, and fills only the selection", async () => {
    const { canvas } = await ready();
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/assets/photo-1/selection/result", expect.objectContaining({ credentials:"same-origin", cache:"no-store" }));
    fireEvent.pointerDown(canvas, { button:0, pointerId:1, clientX:20, clientY:30, shiftKey:true });
    fireEvent.pointerUp(canvas, { pointerId:1, clientX:60, clientY:70 });
    expect(FakeWorker.latest.postMessage).toHaveBeenLastCalledWith({ type:"action", action:{ type:"shape", shape:"rectangle", points:[{x:20,y:20},{x:100,y:100}], mode:"add", contentOnly:true } });
    FakeWorker.latest.emit(frame(6400));
    fireEvent.change(screen.getByLabelText("绘制颜色"), { target:{ value:"#112233" } });
    fireEvent.click(screen.getByRole("button", { name:"填充选区" }));
    expect(FakeWorker.latest.postMessage).toHaveBeenLastCalledWith({ type:"action", action:{ type:"fill", color:"#112233", opacity:100 } });
    FakeWorker.latest.emit(frame(0, true));
    expect(screen.getByRole("button", { name:"填充选区" })).toBeDisabled();
  });
  it("samples original colors and prevents editing while comparing the original", async () => {
    const { canvas } = await ready();
    fireEvent.click(screen.getByRole("button", { name:"吸管取色" }));
    fireEvent.pointerDown(canvas, { button:0, clientX:60, clientY:70 });
    expect(FakeWorker.latest.postMessage).toHaveBeenLastCalledWith({ type:"sample", point:{ x:100,y:100 }, original:true });
    FakeWorker.latest.emit({ type:"sample", color:"#1A2B3C" });
    expect(screen.getByLabelText("绘制颜色")).toHaveValue("#1a2b3c");
    fireEvent.click(screen.getByRole("button", { name:"画笔" }));
    fireEvent.click(screen.getByRole("button", { name:"查看原图" }));
    const count = FakeWorker.latest.postMessage.mock.calls.length;
    fireEvent.pointerDown(canvas, { button:0, clientX:60, clientY:70 });
    expect(FakeWorker.latest.postMessage).toHaveBeenCalledTimes(count);
    expect(screen.getByRole("button", { name:"填充全图" })).toBeDisabled();
  });
  it("queues all stroke segments before a cancelled end and does not lose the last stroke", async () => {
    const { canvas } = await ready();
    fireEvent.click(screen.getByRole("button", { name:"橡皮擦" }));
    fireEvent.pointerDown(canvas, { button:0, pointerId:2, clientX:20, clientY:30 });
    fireEvent.pointerMove(canvas, { pointerId:2, clientX:30, clientY:30 });
    fireEvent.pointerMove(canvas, { pointerId:2, clientX:40, clientY:30 });
    fireEvent.pointerCancel(canvas, { pointerId:2 });
    expect(FakeWorker.latest.postMessage).toHaveBeenLastCalledWith(expect.objectContaining({ type:"stroke-begin", options:expect.objectContaining({ erase:true }) }));
    FakeWorker.latest.emit(frame(null, true));
    expect(FakeWorker.latest.postMessage).toHaveBeenLastCalledWith({ type:"stroke-move", points:[{x:40,y:20},{x:60,y:20}] });
    FakeWorker.latest.emit(frame(null, true));
    expect(FakeWorker.latest.postMessage).toHaveBeenLastCalledWith({ type:"stroke-end", cancel:true });
    FakeWorker.latest.emit(frame());
    expect(screen.getByRole("button", { name:"保存编辑结果" })).toBeDisabled();
  });
  it("saves once through the manual endpoint, retries failure, and protects unsaved edits on close", async () => {
    const onSaved = vi.fn(), onClose = vi.fn();
    const view = await ready(onSaved, onClose);
    FakeWorker.latest.emit(frame(null, true));
    fireEvent.click(screen.getByRole("button", { name:"关闭基础编辑" }));
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name:"继续编辑" }));
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ message:"保存失败" }), { status:503 }));
    const save = screen.getByRole("button", { name:"保存编辑结果" });
    fireEvent.click(save); fireEvent.click(save);
    expect(FakeWorker.latest.postMessage.mock.calls.filter(([value]) => value.type === "export")).toHaveLength(1);
    FakeWorker.latest.emit({ type:"export", blob:new Blob(["edited"], { type:"image/png" }), project:new Blob(["layers"]) });
    expect(await screen.findByRole("alert")).toHaveTextContent("保存失败");
    fireEvent.click(save);
    FakeWorker.latest.emit({ type:"export", blob:new Blob(["edited"], { type:"image/png" }), project:new Blob(["layers"]) });
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith({ id:"edited-1" }));
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST").every(([url]) => url === "/api/v1/assets/photo-1/edit")).toBe(true);
    expect((fetchMock.mock.calls.at(-1)![1]!.body as FormData).get("project")).toBeInstanceOf(Blob);
    view.unmount(); expect(FakeWorker.latest.terminate).toHaveBeenCalled();
  });
  it("supports bucket settings, content-aware marquee, layer visibility and opacity", async () => {
    const { canvas } = await ready();
    expect(screen.getByLabelText("只框选当前图层像素")).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name:"油漆桶" }));
    fireEvent.change(screen.getByLabelText("取样范围"), { target:{ value:"layer" } });
    fireEvent.change(screen.getByRole("slider", { name:"容差" }), { target:{ value:"28" } });
    fireEvent.pointerDown(canvas, { button:0, clientX:60, clientY:70 });
    expect(FakeWorker.latest.postMessage).toHaveBeenLastCalledWith({ type:"action", action:{ type:"bucket", point:{x:100,y:100}, tolerance:28, contiguous:true, sampleMerged:false, color:"#FF6600", opacity:100 } });
    FakeWorker.latest.emit(frame(null, true));
    fireEvent.click(screen.getByRole("tab", { name:/图层/ }));
    fireEvent.click(screen.getByRole("button", { name:"隐藏图层 原图" }));
    expect(FakeWorker.latest.postMessage).toHaveBeenLastCalledWith({ type:"action", action:{ type:"layer", operation:"visible", id:"base" } });
    FakeWorker.latest.emit(frame(null, true));
    const opacity = screen.getByRole("slider", { name:"图层不透明度" });
    fireEvent.change(opacity, { target:{ value:"42" } }); fireEvent.pointerUp(opacity);
    expect(FakeWorker.latest.postMessage).toHaveBeenLastCalledWith({ type:"action", action:{ type:"layer", operation:"opacity", value:42 } });
    FakeWorker.latest.emit(frame(null, true));
    fireEvent.click(screen.getByRole("button", { name:"新建图层" }));
    expect(FakeWorker.latest.postMessage).toHaveBeenLastCalledWith({ type:"action", action:{ type:"layer", operation:"add", id:"base" } });
  });
  it("loads the saved layer document with the current composite", async () => {
    render(<RasterEditorDialog asset={{...asset, metadata:{ raster_project_ready:true }}} onSaved={vi.fn()} onClose={vi.fn()} />);
    await waitFor(() => expect(FakeWorker.latest.postMessage).toHaveBeenCalledWith(expect.objectContaining({type:"init", project:expect.any(Blob)})));
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/assets/photo-1/edit/project", expect.objectContaining({ credentials:"same-origin" }));
  });
});
