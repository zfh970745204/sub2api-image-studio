import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PrintBackgroundControls } from "./PrintBackgroundControls";

const response = (color: string) => new Response(JSON.stringify({ color, confidence: .9, method: "test" }), { headers: { "Content-Type": "application/json" } });

describe("Product background selection", () => {
  it("keeps a manual color when an older automatic estimate finishes", async () => {
    let resolve!: (value: Response) => void;
    const pending = new Promise<Response>((done) => { resolve = done; });
    vi.stubGlobal("fetch", vi.fn(() => pending));
    const changed = vi.fn();
    render(<PrintBackgroundControls sourceId="source-1" sourceUrl="/source.png" color="" onChange={changed} />);
    fireEvent.change(screen.getByLabelText("产品底色"), { target: { value: "#ff0000" } });
    await act(async () => { resolve(response("#000000")); });
    expect(changed.mock.calls).toEqual([["#FF0000"]]);
  });

  it("ignores an automatic result for a replaced source", async () => {
    let resolve!: (value: Response) => void;
    const first = new Promise<Response>((done) => { resolve = done; });
    vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(first).mockResolvedValue(response("#FFFFFF")));
    const oldChanged = vi.fn(), newChanged = vi.fn();
    const view = render(<PrintBackgroundControls key="one" sourceId="one" sourceUrl="/one.png" color="" onChange={oldChanged} />);
    view.rerender(<PrintBackgroundControls key="two" sourceId="two" sourceUrl="/two.png" color="" onChange={newChanged} />);
    await waitFor(() => expect(newChanged).toHaveBeenCalledWith("#FFFFFF"));
    await act(async () => { resolve(response("#000000")); });
    expect(oldChanged).not.toHaveBeenCalled();
  });

  it("samples the original through the authorized API without reading a cross-origin canvas", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response("#445566"));
    vi.stubGlobal("fetch", fetchMock);
    const changed = vi.fn();
    render(<PrintBackgroundControls sourceId="source-1" sourceUrl="/source.png" color="#000000" onChange={changed} />);
    fireEvent.click(screen.getByRole("button", { name: "从原图取色" }));
    const sample = screen.getByRole("button", { name: "选择原图上的底色位置" });
    vi.spyOn(sample, "getBoundingClientRect").mockReturnValue({ width: 100, height: 200, left: 10, top: 20 } as DOMRect);
    fireEvent.click(sample, { clientX: 35, clientY: 170, detail: 1 });
    await waitFor(() => expect(changed).toHaveBeenCalledWith("#445566"));
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/assets/source-1/print-background?x=0.25&y=0.75");
  });
});
