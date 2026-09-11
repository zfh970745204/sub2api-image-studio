import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { api, type Asset, uploadAssets } from "./user-api";
import UserApp from "./UserApp";

const response = (body: unknown) => new Response(JSON.stringify(body));
const bootstrap = {
  user: { id: "u1", display_name: "设计师", email: "test@example.test" }, permissions: ["studio.use", "tasks.create"],
  membership: { plan: { name: "Free" }, entitlements: { max_upload_mb: 20 } },
  points: { balance: 200, lifetime_spent: 0, lifetime_earned: 200, status: "active" },
  service: { status: "ok", features: { sub2api_configured: true } }, notifications: { unread_count: 0 }, preferences: { theme: "light", studio_layout: { last_tool: "ai.redraw" } },
};
const asset = { id: "uploaded", width: 1000, height: 1000, kind: "original", status: "ready", mime_type: "image/png", size_bytes: 50, created_at: "2026-09-10T00:00:00Z" } as Asset;

describe("upload feedback", () => {
  it("uploads two at a time, preserves input order, and retains successes on partial failure", async () => {
    const pending: { resolve: (value: { asset: Asset }) => void; reject: (reason: Error) => void }[] = [];
    const upload = vi.spyOn(api, "uploadAsset").mockImplementation(() => new Promise((resolve, reject) => pending.push({ resolve, reject })));
    const progress = vi.fn();
    const finished = uploadAssets(["one", "two", "three", "four"].map((name) => new File(["image"], `${name}.png`)), progress);
    expect(upload).toHaveBeenCalledTimes(2);
    pending[1].resolve({ asset: { ...asset, id: "two" } });
    await waitFor(() => expect(upload).toHaveBeenCalledTimes(3));
    pending[2].reject(new Error("连接中断"));
    await waitFor(() => expect(upload).toHaveBeenCalledTimes(4));
    pending[3].resolve({ asset: { ...asset, id: "four" } });
    pending[0].resolve({ asset: { ...asset, id: "one" } });
    const results = await finished;
    expect(results.map((result) => result.status === "fulfilled" ? result.value.id : "failed")).toEqual(["one", "two", "failed", "four"]);
    expect(progress.mock.calls.map(([count]) => count)).toEqual([1, 2, 3, 4]);
  });

  it.each([false, true])("closes the upload dialog on settlement, failure=%s", async (failure) => {
    sessionStorage.clear();
    window.history.replaceState({}, "", "/app/studio");
    let settle!: (value: { asset: Asset }) => void;
    let reject!: (reason: Error) => void;
    vi.spyOn(api, "uploadAsset").mockImplementation(() => new Promise((resolve, onReject) => { settle = resolve; reject = onReject; }));
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.endsWith("bootstrap")) return response(bootstrap);
      if (url.endsWith("operations")) return response({ items: [{ id: "ai.redraw", code: "ai.redraw", name: "高清重绘", engine_type: "sub2api", enabled: true, member_base_points: 20, current_price: { base_points: 20 } }] });
      if (url.endsWith("download-url")) return response({ url: "/test.png", expires_at: "2099-01-01T00:00:00Z" });
      return response({ items: [] });
    }));
    const { container } = render(<UserApp />);
    await waitFor(() => expect(screen.getByRole("button", { name: "开始创作" })).toBeEnabled());
    const input = container.querySelector<HTMLInputElement>('input[type="file"][accept="image/png,image/jpeg,image/webp"]')!;
    expect(input).toBeTruthy();
    fireEvent.change(input, { target: { files: [new File(["image"], "photo.png", { type: "image/png" })] } });
    expect(await screen.findByRole("dialog", { name: "正在上传图片" })).toHaveTextContent("photo.png");
    expect(screen.getByRole("progressbar", { name: "正在上传图片" })).not.toHaveAttribute("aria-valuenow");
    expect(screen.getByRole("button", { name: "开始创作" })).toBeDisabled();
    await act(async () => { if (failure) reject(new Error("连接中断，请重新上传")); else settle({ asset }); });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "正在上传图片" })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "开始创作" })).toBeEnabled();
    if (failure) expect(screen.getByRole("alert")).toHaveTextContent("连接中断");
    else expect(await screen.findByRole("img", { name: "来源素材预览" })).toBeInTheDocument();
  });
});
