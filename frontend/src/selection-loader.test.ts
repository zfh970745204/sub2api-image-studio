import { describe, expect, it, vi } from "vitest";
import { createSelectionLoader } from "./selection-loader";
import { api } from "./user-api";

describe("selection prefetch", () => {
  it("shares hover/open downloads and discards cached pixels when the editor is cleared", async () => {
    const metadata = vi.spyOn(api, "selection").mockResolvedValue({ width: 20, height: 20, restore_limited: false, has_initial_selection: true, source_url: "/source", result_url: "/result" });
    const fetchMock = vi.fn(async () => new Response(new Blob(["pixels"])));
    vi.stubGlobal("fetch", fetchMock);
    const loader = createSelectionLoader();
    loader.prefetch("asset");
    const first = loader.load("asset");
    await Promise.all([first.source, first.result]);
    await loader.load("asset").source;
    expect(metadata).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    loader.clear();
    await loader.load("asset").source;
    expect(metadata).toHaveBeenCalledTimes(2);
    loader.clear();
  });
  it("retries a failed prefetch when the user opens the editor", async () => {
    vi.spyOn(api, "selection").mockRejectedValueOnce(new Error("网络暂不可用")).mockResolvedValueOnce({ width: 20, height: 20, restore_limited: false, has_initial_selection: false, source_url: "/same", result_url: "/same" });
    const fetchMock = vi.fn(async () => new Response(new Blob(["pixels"])));
    vi.stubGlobal("fetch", fetchMock);
    const loader = createSelectionLoader();
    const first = loader.load("asset");
    await Promise.allSettled([first.source, first.result]);
    const next = loader.load("asset");
    await next.source;
    expect(await next.result).toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    loader.clear();
  });
});
