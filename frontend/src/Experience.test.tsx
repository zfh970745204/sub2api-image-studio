import { act, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ImageThumbnail } from "./ImageThumbnail";
import { JobProgress } from "./JobProgress";
import { Pagination, useCursorPage } from "./Pagination";
import { defaultBranding, SiteBrandingProvider, useSiteBranding } from "./SiteBranding";
import { estimatedPoints, type ImageJob } from "./user-api";
import UserApp from "./UserApp";
import { AdminEditor } from "./AdminEditors";

const response = (value: unknown) => new Response(JSON.stringify(value));
const bootstrap = {
  user: { id: "u1", display_name: "设计师", email: "test@example.test" }, permissions: ["studio.use", "tasks.create"],
  membership: { plan: { name: "Free" }, entitlements: { max_upload_mb: 20 } },
  points: { balance: 200, lifetime_spent: 0, lifetime_earned: 200, status: "active" },
  service: { status: "ok", features: {} }, notifications: { unread_count: 0 }, preferences: { theme: "light", studio_layout: {} },
};
const asset = { id: "asset-1", operation_code: "upload", mime_type: "image/png", original_filename: "绿色印花.png", width: 1000, height: 1000, kind: "original", status: "ready", size_bytes: 5000, created_at: "2026-09-10T00:00:00Z" };

afterEach(() => vi.useRealTimers());

describe("pagination and lightweight image experience", () => {
  it("uses cursors, resets on filters, and ignores an older request", async () => {
    let oldResolve!: (page: { items: string[]; next_cursor: string | null }) => void;
    const fetcher = vi.fn(async (cursor: string | null) => ({ items: [cursor || "first"], next_cursor: cursor ? null : "next" }));
    const { result, rerender } = renderHook(({ filter }) => useCursorPage(filter, fetcher), { initialProps: { filter: "all" } });
    await waitFor(() => expect(result.current.items).toEqual(["first"]));
    act(() => result.current.next());
    await waitFor(() => expect(result.current.items).toEqual(["next"]));
    expect(result.current.page).toBe(2);
    fetcher.mockImplementationOnce(() => new Promise((resolve) => { oldResolve = resolve; }));
    act(() => { void result.current.load(); });
    rerender({ filter: "result" });
    await waitFor(() => expect(result.current.items).toEqual(["first"]));
    expect(result.current.page).toBe(1);
    await act(async () => oldResolve({ items: ["stale"], next_cursor: null }));
    expect(result.current.items).toEqual(["first"]);
    act(() => result.current.resize(40));
    await waitFor(() => expect(fetcher).toHaveBeenLastCalledWith(null, 40));
  });

  it("shows an accessible pager and does not enable unavailable pages", () => {
    const pager = { page: 1, limit: 20, loading: false, hasNext: false, next: vi.fn(), previous: vi.fn(), resize: vi.fn(), load: vi.fn() };
    render(<Pagination pager={pager} />);
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "40" } });
    expect(pager.resize).toHaveBeenCalledWith(40);
  });

  it("uses lazy thumbnails and never falls back to full images on an error", () => {
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    render(<ImageThumbnail id="a1" />);
    const image = screen.getByRole("img");
    expect(image).toHaveAttribute("src", "/api/v1/assets/a1/thumbnail");
    expect(image).toHaveAttribute("loading", "lazy");
    fireEvent.error(image);
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("continues elapsed feedback without inventing an upstream percentage", async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-09-10T00:00:00Z"));
    render(<JobProgress job={{ id: "j1", status: "running", progress: 20, started_at: "2026-09-10T00:00:00Z" } as ImageJob} />);
    expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(screen.getByText(/已等待 5 秒/)).toBeInTheDocument();
    expect(screen.queryByText("99%")).not.toBeInTheDocument();
  });

  it("loads asset filters and cursors server-side without signing originals", async () => {
    window.history.replaceState({}, "", "/app/assets");
    const fetcher = vi.fn(async (url: string) => url.endsWith("bootstrap") ? response(bootstrap) : response({ items: [asset], next_cursor: url.includes("cursor=") ? null : "cursor1" }));
    vi.stubGlobal("fetch", fetcher);
    render(<UserApp />);
    expect(await screen.findByRole("img", { name: "绿色印花.png" })).toHaveAttribute("src", "/api/v1/assets/asset-1/thumbnail");
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(fetcher.mock.calls.some(([url]) => url.includes("cursor=cursor1"))).toBe(true));
    fireEvent.change(screen.getByLabelText("类型"), { target: { value: "result" } });
    await waitFor(() => expect(fetcher.mock.calls.at(-1)?.[0]).toMatch(/limit=20&kind=result/));
    expect(screen.getByText("第 1 页")).toBeInTheDocument();
    expect(fetcher.mock.calls.some(([url]) => url.includes("download-url"))).toBe(false);
  });

  it("prices standard and fine quality with the same additive rules as quotes", () => {
    const operation = { id: "ai", code: "ai.generate", name: "AI", engine_type: "sub2api", enabled: true, member_base_points: 16, current_price: { base_points: 20, parameter_rules: { rules: [{ parameter: "quality", type: "choice" as const, points: { medium: 0, high: 10 } }] } } };
    expect(estimatedPoints(operation, { quality: "medium" })).toBe(16);
    expect(estimatedPoints(operation, { quality: "high" })).toBe(26);
  });

  it("loads only public branding and refreshes the website name after saving", async () => {
    let name = "云图工作室";
    vi.stubGlobal("fetch", vi.fn(async () => response({ ...defaultBranding, site_name: name })));
    function Name() { return <span>{useSiteBranding().site_name}</span>; }
    render(<SiteBrandingProvider><Name /></SiteBrandingProvider>);
    expect(await screen.findByText(name)).toBeInTheDocument();
    expect(document.title).toContain(name);
    name = "新的工作室";
    act(() => window.dispatchEvent(new Event("site-branding-updated")));
    expect(await screen.findByText(name)).toBeInTheDocument();
  });

  it("uploads a brand image as multipart and saves its public URL directly", async () => {
    const fetcher = vi.fn(async (url: string, _init?: RequestInit) => response(url.endsWith("site-media") ? { url: "/api/v1/site/media/test-image" } : {}));
    vi.stubGlobal("fetch", fetcher);
    const saved = vi.fn();
    render(<AdminEditor kind="settings" row={{ code: "branding", name: "网站名称与品牌配图", defaults: defaultBranding, active_version: null }} permissions={new Set(["config.manage"])} onSaved={saved} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("上传登录页配图"), { target: { files: [new File(["image"], "brand.png", { type: "image/png" })] } });
    await waitFor(() => expect(screen.getByLabelText("登录页配图地址")).toHaveValue("/api/v1/site/media/test-image"));
    const upload = fetcher.mock.calls[0][1];
    expect(upload?.body).toBeInstanceOf(FormData);
    expect(new Headers(upload?.headers).has("Content-Type")).toBe(false);
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "保存并生效" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    expect(fetcher.mock.calls.some(([url, init]) => url.endsWith("config/branding") && init?.method === "PUT")).toBe(true);
  });
});
