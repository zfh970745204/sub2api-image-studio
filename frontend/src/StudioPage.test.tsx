import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import UserApp from "./UserApp";
import type { Asset } from "./user-api";

vi.mock("./BackgroundSelectionEditor", () => ({
  BackgroundSelectionEditor: ({ asset, onSaved }: { asset: Asset; onSaved: (value: Asset) => void }) =>
    <div role="dialog" aria-label="选区修边"><span>修边素材 {asset.id}</span><button onClick={() => onSaved({ ...asset, id: "refined-1", parent_asset_id: asset.id, operation_code: "cutout.refine" })}>测试保存修边</button></div>,
}));

const bootstrap = {
  user: { id: "user-1", display_name: "测试用户", email: "member@example.test" },
  permissions: ["studio.use", "tasks.create"],
  membership: { plan: { name: "Free" }, entitlements: { max_upload_mb: 20 } },
  points: { balance: 200, lifetime_spent: 0, lifetime_earned: 200, status: "active" },
  service: { status: "ok", features: { sub2api_configured: true } },
  notifications: { unread_count: 0 },
  preferences: { theme: "light", studio_layout: { last_tool: "ai.generate" } },
};
const operations = ["ai.generate", "ai.redraw", "color.effect", "ai.extract_print"].map((code) => ({
  id: code, code, name: code, engine_type: code.startsWith("ai") ? "sub2api" : "local", enabled: true,
  member_base_points: 20, current_price: { base_points: 20 },
}));
const quote = { id: "quote-1", operation_code: "ai.generate", base_points: 20, discount_points: 0, surcharge_points: 0, final_points: 20, expires_at: "2099-01-01T00:00:00Z" };
const job = { id: "job-1", operation_code: "ai.generate", status: "queued", progress: 0, charged_points: 20 };
const result = { id: "result-1", width: 1024, height: 1024, kind: "result", status: "ready", mime_type: "image/png", size_bytes: 50, created_at: "2026-09-10T00:00:00Z" };
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("Studio task workflow", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let submit: ReturnType<typeof vi.fn<(url: string, init?: RequestInit) => Promise<Response>>>;
  let assetRead: ReturnType<typeof vi.fn<() => Promise<Response>>>;
  let events: ReturnType<typeof vi.fn<() => Promise<Response>>>;
  let sourceAssets: typeof result[];
  let sourceRead: ReturnType<typeof vi.fn<() => Promise<Response>>>;
  let restoredJob: Record<string, unknown>;

  beforeEach(() => {
    sessionStorage.clear();
    sourceAssets = [];
    sourceRead = vi.fn(async () => response({ asset: { ...result, id: "source-1", kind: "original" } }));
    restoredJob = { ...job, parameters: {} };
    window.history.replaceState({}, "", "/app/studio");
    submit = vi.fn().mockImplementation(() => Promise.resolve(response({ job, created: true, dispatched: true })));
    assetRead = vi.fn().mockImplementation(() => Promise.resolve(response({ asset: result })));
    events = vi.fn().mockImplementation(() => Promise.resolve(response({ job, next_poll_after_ms: 2000 })));
    fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/v1/app/bootstrap") return response(bootstrap);
      if (url === "/api/v1/operations") return response({ items: operations });
      if (url.startsWith("/api/v1/assets?")) return response({ items: sourceAssets });
      if (url === "/api/v1/jobs/quote") return response({ quote: { ...quote, operation_code: JSON.parse(String(init?.body)).operation_code } });
      if (url === "/api/v1/jobs") return submit(url, init);
      if (url.startsWith("/api/v1/jobs?")) return response({ items: [restoredJob] });
      if (url === "/api/v1/jobs/job-1/events") return events();
      if (url === "/api/v1/jobs/job-1") return response({ job: restoredJob });
      if (url === "/api/v1/assets/source-1") return sourceRead();
      if (url === "/api/v1/assets/result-1") return assetRead();
      if (url.endsWith("/lineage")) return response({ items: [result] });
      if (url.includes("/print-background")) return response({ color: "#000000", confidence: .9, method: "product-color-estimate" });
      if (url.endsWith("/download-url")) return response({ url: url.includes("source-1") ? "/original-image.png" : "/test-image.png", expires_at: "2099-01-01T00:00:00Z" });
      if (url === "/api/v1/points/balance") return response({ account: { ...bootstrap.points, balance: 180 } });
      if (url === "/api/v1/me/preferences") return response({ preferences: { ...bootstrap.preferences, studio_layout: { ...bootstrap.preferences.studio_layout, ...JSON.parse(String(init?.body || "{}")).studio_layout } } });
      throw new Error(`Unexpected test request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => { vi.useRealTimers(); });

  async function prepare() {
    render(<UserApp />);
    const prompt = await screen.findByRole("textbox", { name: /图片描述/ });
    fireEvent.change(prompt, { target: { value: "复古山脉海报" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "开始创作" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "开始创作" }));
    return screen.findByRole("dialog", { name: "任务报价" });
  }

  it("quotes and submits the chosen print mode and corrected product color", async () => {
    window.history.replaceState({}, "", "/app/studio?source=source-1&tool=ai.extract_print");
    render(<UserApp />);
    await screen.findByText(/已估计产品底色/);
    expect(screen.getByRole("button", { name: "透明背景" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "原产品底色（不透明）" }));
    fireEvent.change(screen.getByLabelText("产品底色"), { target: { value: "#245eaa" } });
    fireEvent.click(screen.getByRole("button", { name: "开始创作" }));
    const dialog = await screen.findByRole("dialog", { name: "任务报价" });
    expect(within(dialog).getByText("原产品底色（不透明）")).toBeInTheDocument();
    expect(within(dialog).getByText("#245EAA")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "确认提交" }));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
    expect(JSON.parse(String(submit.mock.calls[0][1]?.body)).parameters).toMatchObject({ output_mode: "opaque", background_color: "#245EAA" });
    expect(fetchMock.mock.calls.some(([url, init]) => url === "/api/v1/me/preferences" && String(init?.body).includes('"print_output_mode":"opaque"'))).toBe(true);
  });

  it("restores print mode and product color without replacing them with a new estimate", async () => {
    restoredJob = { ...job, operation_code: "ai.extract_print", source_asset_id: "source-1", output_asset_id: result.id, status: "succeeded", parameters: { output_mode: "opaque", background_color: "#245EAA", quality: "high" } };
    events.mockImplementation(async () => response({ job: restoredJob }));
    window.history.replaceState({}, "", "/app/studio?job=job-1");
    render(<UserApp />);
    await screen.findByRole("img", { name: "图片任务结果" });
    expect(screen.getByRole("button", { name: "原产品底色（不透明）" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("产品底色")).toHaveValue("#245eaa");
    expect(fetchMock.mock.calls.some(([url]) => url.includes("/print-background"))).toBe(false);
  });

  it("refines the generated print, shows the saved version and never submits a new paid job", async () => {
    restoredJob = { ...job, operation_code: "ai.extract_print", source_asset_id: "source-1", output_asset_id: result.id, status: "succeeded", parameters: { output_mode: "transparent", background_color: "#000000" } };
    events.mockImplementation(async () => response({ job: restoredJob }));
    window.history.replaceState({}, "", "/app/studio?job=job-1");
    render(<UserApp />);
    await screen.findByRole("img", { name: "图片任务结果" });
    fireEvent.click(screen.getByRole("button", { name: "选区修边" }));
    expect(screen.getByText("修边素材 result-1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "测试保存修边" }));
    expect(await screen.findByText(/修边已保存为新版本，未扣积分/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "选区修边" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "选区修边" }));
    expect(screen.getByText("修边素材 refined-1")).toBeInTheDocument();
    expect(submit).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.some(([url]) => url.endsWith("/quote"))).toBe(false);
  });

  it("keeps the quote on a lost response and retries with the same key and parameters", async () => {
    submit.mockRejectedValueOnce(new TypeError("网络连接中断"));
    submit.mockResolvedValueOnce(response({ job, created: false, dispatched: false }));
    const dialog = await prepare();
    fireEvent.click(within(dialog).getByRole("button", { name: "确认提交" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("网络连接中断");
    expect(screen.getByRole("textbox", { name: /图片描述/ })).toHaveValue("复古山脉海报");
    fireEvent.click(within(dialog).getByRole("button", { name: "重试提交" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(submit).toHaveBeenCalledTimes(2);
    const first = submit.mock.calls[0][1] as RequestInit;
    const second = submit.mock.calls[1][1] as RequestInit;
    expect(new Headers(first.headers).get("Idempotency-Key")).toBe("studio:quote-1");
    expect(new Headers(second.headers).get("Idempotency-Key")).toBe("studio:quote-1");
    expect(second.body).toBe(first.body);
    await waitFor(() => expect(screen.getByTitle("查看积分流水")).toHaveTextContent("180"));
  });

  it("shows a request ID for server failures without discarding the quote", async () => {
    submit.mockResolvedValueOnce(response({ code: "IMAGE_JOB_SAVE_FAILED", message: "任务保存失败，本次提交未扣费", request_id: "req-test-123" }, 500));
    const dialog = await prepare();
    fireEvent.click(within(dialog).getByRole("button", { name: "确认提交" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("req-test-123");
    expect(within(dialog).getByRole("button", { name: "重试提交" })).toBeEnabled();
  });

  it("asks for a fresh quote after expiry and preserves the creative input", async () => {
    submit.mockResolvedValueOnce(response({ code: "JOB_QUOTE_EXPIRED", message: "任务报价已过期，请重新报价" }, 409));
    const dialog = await prepare();
    fireEvent.click(within(dialog).getByRole("button", { name: "确认提交" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("任务报价已过期");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /图片描述/ })).toHaveValue("复古山脉海报");
  });

  it("retries a result that has not been published yet and then displays it", async () => {
    events.mockImplementation(() => Promise.resolve(response({ job: { ...job, status: "succeeded", output_asset_id: result.id }, next_poll_after_ms: null })));
    assetRead.mockResolvedValueOnce(response({ code: "ASSET_NOT_FOUND", message: "素材尚未就绪" }, 404));
    const dialog = await prepare();
    vi.useFakeTimers();
    await act(async () => { fireEvent.click(within(dialog).getByRole("button", { name: "确认提交" })); });
    expect(screen.getByText(/正在自动重试/)).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(4100); });
    expect(assetRead).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("img", { name: "图片任务结果" })).toHaveAttribute("src", "/test-image.png");
    expect(screen.getByRole("button", { name: "下载" })).toBeEnabled();
    expect(screen.queryByText(/正在自动重试/)).not.toBeInTheDocument();
  });

  it("switches to generation without showing an unrelated source image", async () => {
    sourceAssets = [{ ...result, kind: "original" }];
    window.history.replaceState({}, "", "/app/studio?source=result-1&tool=ai.redraw");
    render(<UserApp />);
    expect(await screen.findByRole("img", { name: "来源素材预览" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^AI 生成$/ }));
    expect(screen.getByText("把想象，变成看得见的作品")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "来源素材预览" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "上传图片" })).not.toBeInTheDocument();
  });

  it("pauses task polling in a hidden tab and resumes when returning", async () => {
    let visibility: DocumentVisibilityState = "visible";
    vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
    const dialog = await prepare();
    vi.useFakeTimers();
    await act(async () => { fireEvent.click(within(dialog).getByRole("button", { name: "确认提交" })); });
    expect(events).toHaveBeenCalledTimes(1);
    act(() => { visibility = "hidden"; document.dispatchEvent(new Event("visibilitychange")); });
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(events).toHaveBeenCalledTimes(1);
    await act(async () => { visibility = "visible"; document.dispatchEvent(new Event("visibilitychange")); });
    expect(events).toHaveBeenCalledTimes(2);
  });

  it("restores the running task with its original tool, input and source after reload", async () => {
    restoredJob = { ...job, operation_code: "ai.redraw", source_asset_id: result.id, parameters: { instruction: "保留原来的字体", quality: "medium" } };
    events.mockImplementation(() => Promise.resolve(response({ job: restoredJob, next_poll_after_ms: 2000 })));
    sessionStorage.setItem("studio-job:user-1", "job-1");
    render(<UserApp />);
    expect(await screen.findByRole("textbox", { name: /补充要求/ })).toHaveValue("保留原来的字体");
    expect(await screen.findByRole("img", { name: "来源素材预览" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "正在处理图片" })).toBeDisabled();
    expect(submit).not.toHaveBeenCalled();
  });

  it("opens an explicit completed task with distinct before/after images and preserves another running task", async () => {
    restoredJob = { ...job, operation_code: "ai.redraw", source_asset_id: "source-1", output_asset_id: result.id, status: "succeeded", parameters: { instruction: "保留字体和色彩", quality: "medium" } };
    events.mockImplementation(async () => response({ job: restoredJob, next_poll_after_ms: null }));
    sessionStorage.setItem("studio-job:user-1", "unrelated-job");
    window.history.replaceState({}, "", "/app/studio?job=job-1");
    render(<UserApp />);
    expect(await screen.findByRole("img", { name: "来源素材预览" })).toHaveAttribute("src", "/original-image.png");
    expect(await screen.findByRole("img", { name: "图片任务结果" })).toHaveAttribute("src", "/test-image.png");
    expect(screen.getByRole("textbox", { name: /补充要求/ })).toHaveValue("保留字体和色彩");
    expect(sessionStorage.getItem("studio-job:user-1")).toBe("unrelated-job");
    expect(fetchMock.mock.calls.some(([url]) => url.includes("unrelated-job"))).toBe(false);
    expect(submit).not.toHaveBeenCalled();
  });

  it("still displays a completed result when its original is unavailable", async () => {
    restoredJob = { ...job, operation_code: "ai.redraw", source_asset_id: "source-1", output_asset_id: result.id, status: "succeeded", parameters: {} };
    sourceRead.mockResolvedValue(response({ message: "素材已删除" }, 404));
    events.mockImplementation(async () => response({ job: restoredJob, next_poll_after_ms: null }));
    window.history.replaceState({}, "", "/app/studio?job=job-1");
    render(<UserApp />);
    expect(await screen.findByRole("img", { name: "图片任务结果" })).toHaveAttribute("src", "/test-image.png");
    expect(screen.getByRole("alert")).toHaveTextContent("原图暂不可用");
    expect(screen.queryByRole("img", { name: "来源素材预览" })).not.toBeInTheDocument();
  });

  it("does not replace a direct source link with a stored background job", async () => {
    sessionStorage.setItem("studio-job:user-1", "job-1");
    window.history.replaceState({}, "", "/app/studio?source=source-1&tool=ai.redraw");
    render(<UserApp />);
    expect(await screen.findByRole("img", { name: "来源素材预览" })).toHaveAttribute("src", "/original-image.png");
    expect(fetchMock.mock.calls.some(([url]) => url === "/api/v1/jobs/job-1")).toBe(false);
    expect(screen.queryByRole("img", { name: "图片任务结果" })).not.toBeInTheDocument();
  });

  it("navigates from task details to the original task comparison", async () => {
    restoredJob = { ...job, operation_code: "ai.redraw", source_asset_id: "source-1", output_asset_id: result.id, status: "succeeded", parameters: {} };
    events.mockImplementation(async () => response({ job: restoredJob, next_poll_after_ms: null }));
    window.history.replaceState({}, "", "/app/jobs");
    render(<UserApp />);
    fireEvent.click(await screen.findByRole("button", { name: "查看任务详情" }));
    fireEvent.click(screen.getByRole("button", { name: "在编辑器中查看前后对比" }));
    expect(window.location.search).toBe("?job=job-1");
    expect(await screen.findByRole("img", { name: "来源素材预览" })).toHaveAttribute("src", "/original-image.png");
    expect(await screen.findByRole("img", { name: "图片任务结果" })).toHaveAttribute("src", "/test-image.png");
  });
});
