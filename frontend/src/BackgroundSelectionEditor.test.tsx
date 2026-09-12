import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { BackgroundSelectionEditor } from "./BackgroundSelectionEditor";
import type { Asset } from "./user-api";
import type { SelectionReply } from "./background-selection.worker";

class FakeWorker {
  static latest: FakeWorker;
  postMessage = vi.fn();
  terminate = vi.fn();
  onmessage: ((event: { data: SelectionReply }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() { FakeWorker.latest = this; }
  emit(data: SelectionReply) { act(() => { this.onmessage?.({ data }); }); }
}
const bitmap = () => ({ width: 400, height: 300, close: vi.fn() } as unknown as ImageBitmap);
const frame = () => ({ type: "frame" as const, source: bitmap(), preview: bitmap(), overlay: bitmap(), canUndo: false, canRedo: false, canRetune: false, removed: 100, visible: 1000 });
const asset = { id: "print-1", width: 400, height: 300 } as Asset;

describe("background selection editor", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    vi.stubGlobal("Worker", FakeWorker);
    vi.stubGlobal("OffscreenCanvas", class {});
    vi.stubGlobal("createImageBitmap", vi.fn());
    vi.stubGlobal("PointerEvent", MouseEvent);
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(300);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(150);
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ drawImage: vi.fn(), clearRect: vi.fn() } as unknown as CanvasRenderingContext2D);
    fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") return new Response(JSON.stringify({ asset: { id: "refined-1" } }));
      if (url.endsWith("/selection")) return new Response(JSON.stringify({ width: 400, height: 300, restore_limited: true, has_initial_selection: true, source_url: "/private/source", result_url: "/private/result" }));
      return new Response(new Blob(["test png"]));
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  function geometry() {
    const source = screen.getByLabelText("选区画布") as HTMLDivElement;
    const result = screen.getByLabelText("结果画布") as HTMLDivElement;
    for (const [index, viewport] of [source, result].entries()) {
      const left = 10 + index * 400, top = 20;
      vi.spyOn(viewport, "getBoundingClientRect").mockReturnValue({ left, top, width: 300, height: 150 } as DOMRect);
      const image = viewport.firstElementChild as HTMLDivElement;
      const rect = () => {
        const width = Number.parseFloat(image.style.width), height = width * 3 / 4;
        return { left: left + Math.max(0, (300 - width) / 2) - viewport.scrollLeft, top: top - viewport.scrollTop, width, height } as DOMRect;
      };
      vi.spyOn(image, "getBoundingClientRect").mockImplementation(rect);
      for (const canvas of image.querySelectorAll("canvas")) vi.spyOn(canvas, "getBoundingClientRect").mockImplementation(rect);
    }
    return { source, result };
  }

  function wheel(target: HTMLElement, deltaY: number, clientX: number, clientY: number, deltaMode = 0) {
    const event = new WheelEvent("wheel", { bubbles: true, cancelable: true, deltaY, deltaMode, clientX, clientY });
    act(() => { target.dispatchEvent(event); });
    return event;
  }

  async function ready(onSaved = vi.fn()) {
    const view = render(<BackgroundSelectionEditor asset={asset} onClose={vi.fn()} onSaved={onSaved} />);
    await waitFor(() => expect(FakeWorker.latest.postMessage).toHaveBeenCalledWith(expect.objectContaining({ type: "init", hasSelection: true })));
    FakeWorker.latest.emit(frame());
    return view;
  }

  it("loads private full-size pixels, exposes restoration limits, maps clicks, and subtracts selections", async () => {
    await ready();
    expect(screen.getByText(/历史结果未保留/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/private/source", expect.objectContaining({ credentials: "same-origin", cache: "no-store" }));
    const canvas = screen.getByLabelText("点击图片编辑背景选区");
    vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({ left: 10, top: 20, width: 200, height: 150 } as DOMRect);
    fireEvent.pointerDown(canvas, { button: 0, clientX: 60, clientY: 70 });
    expect(FakeWorker.latest.postMessage).toHaveBeenLastCalledWith({ type: "action", action: { type: "wand", x: 100, y: 100, tolerance: 12, contiguous: true, mode: "remove" } });
    FakeWorker.latest.emit({ ...frame(), canUndo: true, canRetune: true });
    fireEvent.click(screen.getByRole("button", { name: /保留 \/ 取消误选/ }));
    fireEvent.click(screen.getByLabelText("只选相连区域"));
    fireEvent.pointerDown(canvas, { button: 0, clientX: 60, clientY: 70 });
    expect(FakeWorker.latest.postMessage).toHaveBeenLastCalledWith(expect.objectContaining({ action: expect.objectContaining({ mode: "restore", contiguous: false }) }));
  });

  it("saves the worker PNG once, retries a failed save, and does not quote or create AI jobs", async () => {
    const onSaved = vi.fn();
    await ready(onSaved);
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ message: "存储暂时不可用" }), { status: 503 }));
    const save = screen.getByRole("button", { name: "保存修边结果" });
    fireEvent.click(save); fireEvent.click(save);
    expect(FakeWorker.latest.postMessage.mock.calls.filter(([command]) => command.type === "export")).toHaveLength(1);
    FakeWorker.latest.emit({ type: "export", blob: new Blob(["rgba"], { type: "image/png" }) });
    expect(await screen.findByRole("alert")).toHaveTextContent("存储暂时不可用");
    expect(save).toBeEnabled();
    fireEvent.click(save);
    FakeWorker.latest.emit({ type: "export", blob: new Blob(["rgba"], { type: "image/png" }) });
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith({ id: "refined-1" }));
    const writes = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(writes).toHaveLength(2);
    expect(writes.every(([url]) => url === "/api/v1/assets/print-1/selection")).toBe(true);
    expect((writes[1][1]!.body as FormData).get("image")).toBeInstanceOf(File);
  });

  it("blocks empty output, terminates on close, and offers reload after a worker crash", async () => {
    const view = await ready();
    FakeWorker.latest.emit({ ...frame(), visible: 0 });
    expect(screen.getByRole("button", { name: "保存修边结果" })).toBeDisabled();
    act(() => FakeWorker.latest.onerror?.());
    expect(screen.getByRole("button", { name: "重新载入" })).toBeInTheDocument();
    expect(FakeWorker.latest.terminate).toHaveBeenCalled();
    view.unmount();
  });

  it("shows the optional existing preview immediately and replaces it only when a full-size frame arrives", async () => {
    let resolveMetadata!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise<Response>((resolve) => { resolveMetadata = resolve; }));
    render(<BackgroundSelectionEditor asset={asset} previewUrl="blob:existing-preview" onClose={vi.fn()} onSaved={vi.fn()} />);
    expect(screen.getByRole("img", { name: "已有结果预览" })).toHaveAttribute("src", "blob:existing-preview");
    expect(screen.getByRole("button", { name: "保存修边结果" })).toBeDisabled();
    expect(FakeWorker.latest.postMessage).not.toHaveBeenCalled();
    resolveMetadata(new Response(JSON.stringify({ width: 400, height: 300, has_initial_selection: true, source_url: "/private/same", result_url: "/private/same" })));
    await waitFor(() => expect(FakeWorker.latest.postMessage).toHaveBeenCalled());
    expect(screen.getByRole("img", { name: "已有结果预览" })).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([url]) => url === "/private/same")).toHaveLength(1);
    const [init] = FakeWorker.latest.postMessage.mock.calls[0];
    expect(init).toMatchObject({ type: "init", width: 400, height: 300 });
    expect(init.source).toBeInstanceOf(Blob);
    expect(init.result).toBeUndefined();
    FakeWorker.latest.emit(frame());
    expect(screen.queryByRole("img", { name: "已有结果预览" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存修边结果" })).toBeEnabled();
  });

  it.each([undefined, null, "", "blob:expired-preview"])("loads without a usable optional preview (%s)", async (previewUrl) => {
    render(<BackgroundSelectionEditor asset={asset} previewUrl={previewUrl} onClose={vi.fn()} onSaved={vi.fn()} />);
    if (previewUrl) fireEvent.error(screen.getByRole("img", { name: "已有结果预览" }));
    expect(screen.queryByRole("img", { name: "已有结果预览" })).not.toBeInTheDocument();
    expect(screen.getByText("正在准备透明预览…")).toBeInTheDocument();
    await waitFor(() => expect(FakeWorker.latest.postMessage).toHaveBeenCalled());
    expect(fetchMock.mock.calls.filter(([url]) => url === "/private/source" || url === "/private/result")).toHaveLength(2);
    FakeWorker.latest.emit(frame());
    expect(screen.getByRole("button", { name: "保存修边结果" })).toBeEnabled();
  });

  it("anchors wheel zoom in either pane, shows fractional zoom, and retains original click coordinates and resolution", async () => {
    await ready();
    const { source, result } = geometry();
    const canvas = screen.getByLabelText("点击图片编辑背景选区") as HTMLCanvasElement;
    const commandsBeforeZoom = FakeWorker.latest.postMessage.mock.calls.length;
    // The 200px-wide fitted image is centered in a 300px viewport. Aim at its
    // 75%/50% pixel and grow it past the viewport width.
    const zoomEvent = wheel(source, -Math.log(2) / .002, 210, 95);
    expect(zoomEvent.defaultPrevented).toBe(true);
    expect(source.scrollLeft).toBeCloseTo(100);
    expect(source.scrollTop).toBeCloseTo(75);
    expect(result.scrollLeft).toBeCloseTo(100);
    expect(result.scrollTop).toBeCloseTo(75);
    expect(source.firstElementChild).toHaveStyle({ width: "400px" });
    expect(result.firstElementChild).toHaveStyle({ width: "400px" });
    fireEvent.pointerDown(canvas, { button: 0, clientX: 210, clientY: 95 });
    expect(FakeWorker.latest.postMessage).toHaveBeenLastCalledWith(expect.objectContaining({ action: expect.objectContaining({ x: 300, y: 150 }) }));
    FakeWorker.latest.emit(frame());
    wheel(result, -40, 610, 95);
    const zoom = screen.getByLabelText("修边预览缩放") as HTMLSelectElement;
    expect(Number(zoom.value)).toBeCloseTo(2 * Math.exp(.08));
    expect(zoom.selectedOptions[0].textContent).toBe("216.7%");
    expect(source.scrollLeft).toBeCloseTo(result.scrollLeft);
    expect(source.scrollTop).toBeCloseTo(result.scrollTop);
    fireEvent.pointerDown(canvas, { button: 0, clientX: 210, clientY: 95 });
    const action = FakeWorker.latest.postMessage.mock.calls.at(-1)![0].action;
    expect(action.x).toBeCloseTo(300);
    expect(action.y).toBeCloseTo(150);
    expect(canvas.width).toBe(400);
    expect(canvas.height).toBe(300);
    expect(FakeWorker.latest.postMessage.mock.calls.length).toBe(commandsBeforeZoom + 2);
    FakeWorker.latest.emit(frame());
    fireEvent.click(screen.getByRole("button", { name: "保存修边结果" }));
    expect(FakeWorker.latest.postMessage).toHaveBeenLastCalledWith({ type: "export" });
  });

  it("normalizes wheel modes, clamps zoom without scrolling, and keeps button/preset controls synchronized", async () => {
    await ready();
    const { source, result } = geometry();
    const zoom = screen.getByLabelText("修边预览缩放") as HTMLSelectElement;
    wheel(source, -1, 160, 95, 1);
    expect(Number(zoom.value)).toBeCloseTo(Math.exp(.032));
    wheel(result, -1, 560, 95, 2);
    expect(Number(zoom.value)).toBeCloseTo(Math.exp(.332));
    fireEvent.change(zoom, { target: { value: "8" } });
    expect(screen.getByRole("button", { name: "放大修边预览" })).toBeDisabled();
    expect(wheel(source, -100, 160, 95).defaultPrevented).toBe(true);
    expect(zoom.value).toBe("8");
    fireEvent.change(zoom, { target: { value: "0.25" } });
    expect(screen.getByRole("button", { name: "缩小修边预览" })).toBeDisabled();
    expect(wheel(result, 100, 560, 95).defaultPrevented).toBe(true);
    expect(zoom.value).toBe("0.25");
    fireEvent.click(screen.getByRole("button", { name: "放大修边预览" }));
    expect(zoom.value).toBe("0.3125");
    fireEvent.click(screen.getByRole("button", { name: "缩小修边预览" }));
    expect(zoom.value).toBe("0.25");
    fireEvent.change(zoom, { target: { value: "1" } });
    expect(source.scrollLeft).toBe(0); expect(source.scrollTop).toBe(0);
    expect(result.scrollLeft).toBe(0); expect(result.scrollTop).toBe(0);
    expect(wheel(screen.getByRole("heading", { name: "选区修边" }), -50, 0, 0).defaultPrevented).toBe(false);
    expect(zoom.value).toBe("1");
  });

  it("synchronizes manual scrolling in both directions and restores page scrolling and wheel listeners on unmount", async () => {
    document.body.style.overflow = "clip";
    const view = await ready();
    const { source, result } = geometry();
    expect(document.body.style.overflow).toBe("hidden");
    source.scrollLeft = 140; source.scrollTop = 85;
    fireEvent.scroll(source);
    expect(result.scrollLeft).toBe(140); expect(result.scrollTop).toBe(85);
    result.scrollLeft = 260; result.scrollTop = 110;
    fireEvent.scroll(result);
    expect(source.scrollLeft).toBe(260); expect(source.scrollTop).toBe(110);
    view.unmount();
    expect(document.body.style.overflow).toBe("clip");
    expect(wheel(source, -100, 0, 0).defaultPrevented).toBe(false);
    document.body.style.overflow = "";
  });

  it("does not fetch image bytes after the dialog closes during metadata loading", async () => {
    let resolveMetadata!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise<Response>((resolve) => { resolveMetadata = resolve; }));
    const view = render(<BackgroundSelectionEditor asset={asset} onClose={vi.fn()} onSaved={vi.fn()} />);
    const instance = FakeWorker.latest;
    view.unmount();
    await act(async () => { resolveMetadata(new Response(JSON.stringify({ source_url: "/private/source", result_url: "/private/result" }))); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(instance.terminate).toHaveBeenCalled();
    expect(instance.postMessage).not.toHaveBeenCalled();
  });
});
