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
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ drawImage: vi.fn() } as unknown as CanvasRenderingContext2D);
    fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") return new Response(JSON.stringify({ asset: { id: "refined-1" } }));
      if (url.endsWith("/selection")) return new Response(JSON.stringify({ width: 400, height: 300, restore_limited: true, has_initial_selection: true, source_url: "/private/source", result_url: "/private/result" }));
      return new Response(new Blob(["test png"]));
    });
    vi.stubGlobal("fetch", fetchMock);
  });

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
});
