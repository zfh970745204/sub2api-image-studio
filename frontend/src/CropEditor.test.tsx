import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CropEditor } from "./CropEditor";
import { api, type Asset } from "./user-api";

const asset = { id: "image-1", width: 400, height: 300 } as Asset;
function ready(onSaved = vi.fn()) {
  render(<CropEditor asset={asset} previewUrl="/original.png" onClose={vi.fn()} onSaved={onSaved} />);
  const image = screen.getByAltText("裁切原图");
  Object.defineProperties(image, { naturalWidth: { value: 400 }, naturalHeight: { value: 300 } });
  fireEvent.load(image);
  return image;
}
describe("crop editor", () => {
  beforeEach(() => {
    vi.stubGlobal("PointerEvent", MouseEvent);
    HTMLElement.prototype.setPointerCapture = vi.fn();
  });
  it("uses original pixel coordinates for dragging, resizing and keyboard nudging", () => {
    const image = ready();
    vi.spyOn(image.parentElement!, "getBoundingClientRect").mockReturnValue({ width: 200, height: 150 } as DOMRect);
    fireEvent.change(screen.getByLabelText("宽度"), { target: { value: 100 } });
    fireEvent.change(screen.getByLabelText("高度"), { target: { value: 100 } });
    const box = screen.getByRole("group", { name: "裁切选框" });
    fireEvent.pointerDown(box, { button: 0, clientX: 20, clientY: 20 });
    fireEvent.pointerMove(box, { clientX: 70, clientY: 45 });
    fireEvent.pointerUp(box);
    expect(screen.getByLabelText("左边距")).toHaveValue(100);
    expect(screen.getByLabelText("上边距")).toHaveValue(50);
    fireEvent.keyDown(box, { key: "ArrowRight", shiftKey: true });
    expect(screen.getByLabelText("左边距")).toHaveValue(110);
    fireEvent.pointerDown(box.firstElementChild!, { button: 0, clientX: 0, clientY: 0 });
    fireEvent.pointerMove(box, { clientX: 500, clientY: 500 });
    fireEvent.pointerUp(box);
    expect(screen.getByLabelText("宽度")).toHaveValue(290);
    expect(screen.getByLabelText("高度")).toHaveValue(250);
  });
  it("locks circles to a square, clamps dimensions, and saves only once while pending", async () => {
    let complete!: (value: { asset: Asset }) => void;
    const save = vi.spyOn(api, "crop").mockImplementation(() => new Promise((resolve) => { complete = resolve; }));
    const onSaved = vi.fn();
    ready(onSaved);
    fireEvent.click(screen.getByRole("button", { name: "圆形" }));
    expect(screen.getByLabelText("左边距")).toHaveValue(50);
    fireEvent.change(screen.getByLabelText("直径"), { target: { value: 200 } });
    fireEvent.change(screen.getByLabelText("左边距"), { target: { value: 999 } });
    const submit = screen.getByRole("button", { name: "保存裁切" });
    fireEvent.click(submit); fireEvent.click(submit);
    expect(save).toHaveBeenCalledTimes(1);
    expect(save).toHaveBeenCalledWith("image-1", { x: 200, y: 0, width: 200, height: 200, shape: "circle" });
    await act(async () => { complete({ asset: { ...asset, id: "cropped" } }); });
    expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ id: "cropped" }));
  });
  it("preserves the crop after a failed save and allows retry", async () => {
    const save = vi.spyOn(api, "crop").mockRejectedValueOnce(new Error("存储暂不可用")).mockResolvedValueOnce({ asset });
    const onSaved = vi.fn(); ready(onSaved);
    fireEvent.change(screen.getByLabelText("宽度"), { target: { value: 120 } });
    fireEvent.click(screen.getByRole("button", { name: "保存裁切" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("存储暂不可用");
    expect(screen.getByLabelText("宽度")).toHaveValue(120);
    fireEvent.click(screen.getByRole("button", { name: "保存裁切" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(save).toHaveBeenCalledTimes(2);
  });
  it("blocks saving before the source loads or when its geometry differs", () => {
    render(<CropEditor asset={asset} previewUrl="/original.png" onClose={vi.fn()} onSaved={vi.fn()} />);
    expect(screen.getByRole("button", { name: "保存裁切" })).toBeDisabled();
    fireEvent.load(screen.getByAltText("裁切原图"));
    expect(screen.getByRole("alert")).toHaveTextContent("原图尺寸不匹配");
    expect(screen.getByRole("button", { name: "保存裁切" })).toBeDisabled();
  });
});
