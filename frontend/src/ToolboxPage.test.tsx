import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ToolboxPage } from "./ToolboxPage";
import type { BootstrapData } from "./user-api";

vi.mock("./ToolboxPreview", () => ({ ToolboxPreview: () => <div>布局预览</div> }));

const bootstrap = {
  user: { id: "toolbox-user" }, permissions: ["studio.use", "tasks.create", "assets.read_own", "assets.write_own"],
  membership: { entitlements: { max_upload_mb: 20, max_image_megapixels: 16 } }, points: { balance: 200 },
} as BootstrapData;
const source = (id: string) => ({ id, original_filename: `${id}.png`, width: 1200, height: 800, kind: "original", status: "ready", extension: "png" });
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });

describe("toolbox batch workflow", () => {
  let fetcher: ReturnType<typeof vi.fn<(url: string, init?: RequestInit) => Promise<Response>>>;
  let submit: ReturnType<typeof vi.fn<(body: { items: { parameters: Record<string, unknown> }[] }) => Promise<Response>>>;
  let jobItems: Record<string, unknown>[];
  beforeEach(() => {
    window.history.replaceState({}, "", "/app/toolbox");
    sessionStorage.clear();
    jobItems = [];
    submit = vi.fn(async (body: { items: { parameters: Record<string, unknown> }[] }) => {
      jobItems = body.items.map((item, index) => ({ id: `job-${index}`, parameters: item.parameters, status: "queued", source_asset_id: `source-${index}`, progress: 0, operation_code: "image.toolbox" }));
      return response({ items: jobItems });
    });
    fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      const query = new URL(url, "http://localhost").searchParams;
      if (url === "/api/v1/operations") return response({ items: [{ code: "image.toolbox", enabled: true }] });
      if (url.startsWith("/api/v1/assets?")) return response({ items: query.has("cursor") ? [source("later")] : [source("first")], next_cursor: query.has("cursor") ? null : "page-2" });
      if (url.startsWith("/api/v1/jobs?")) return response({ items: jobItems, next_cursor: null });
      if (url === "/api/v1/toolbox/quote") {
        const body = JSON.parse(String(init?.body));
        return response({ batch_id: "batch-1", total_points: 0, items: body.asset_ids.map((id: string, index: number) => ({ source_asset_id: id, parameters: { options: body.options, batch_id: "batch-1", batch_name: body.batch_name, output_name: `${body.filename_prefix || id}-${index}.${body.options.format}` }, quote: { id: `quote-${index}`, final_points: 0 } })) });
      }
      if (url === "/api/v1/toolbox/submit") return submit(JSON.parse(String(init?.body)));
      if (url === "/api/v1/points/balance") return response({ account: { balance: 200 } });
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetcher);
  });
  const show = () => render(<ToolboxPage bootstrap={bootstrap} onBootstrap={vi.fn()} />);
  async function selectImages() {
    fireEvent.click(screen.getByRole("button", { name: "素材库" }));
    fireEvent.click(await screen.findByRole("button", { name: "选择 first.png" }));
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    fireEvent.click(await screen.findByRole("button", { name: "选择 later.png" }));
    fireEvent.click(screen.getByRole("button", { name: "确认选择" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "处理 2 张图片" })).toBeEnabled());
  }
  it("shares composed settings across tools and submits all cross-page selections, then allows more work", async () => {
    show(); await selectImages();
    fireEvent.click(screen.getByRole("button", { name: /尺寸与画布/ }));
    fireEvent.change(screen.getByLabelText("缩放方式"), { target: { value: "fill" } });
    fireEvent.change(screen.getByLabelText("宽度 px"), { target: { value: "1000" } });
    fireEvent.change(screen.getByLabelText("高度 px"), { target: { value: "800" } });
    fireEvent.click(screen.getByRole("button", { name: /旋转翻转/ }));
    fireEvent.change(screen.getByLabelText("顺时针旋转"), { target: { value: "90" } });
    fireEvent.click(screen.getByRole("button", { name: /批量处理/ }));
    fireEvent.change(screen.getByLabelText("统一文件名前缀"), { target: { value: "summer" } });
    fireEvent.change(screen.getByLabelText("输出格式"), { target: { value: "jpg" } });
    fireEvent.click(screen.getByRole("button", { name: "处理 2 张图片" }));
    const dialog = await screen.findByRole("dialog", { name: "确认批量处理" });
    expect(dialog).toHaveTextContent("summer-0.jpg");
    const quoteCall = fetcher.mock.calls.find(([url]) => url === "/api/v1/toolbox/quote")!;
    expect(JSON.parse(String(quoteCall[1]?.body))).toMatchObject({ asset_ids: ["first", "later"], filename_prefix: "summer", options: { resize: "fill", width: 1000, height: 800, rotation: 90, format: "jpg" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认提交" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "处理 2 张图片" })).toBeEnabled();
    expect(screen.getAllByText("排队中")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "素材库" })).toBeEnabled();
    expect(sessionStorage.getItem("toolbox-pending:toolbox-user")).toBeNull();
    expect(fetcher.mock.calls.filter(([url]) => url.startsWith("/api/v1/jobs?")).at(-1)?.[0]).toContain("batch_id=batch-1");
  });
  it("restores an uncertain submission after remount and retries the exact quotes without preparing new ones", async () => {
    submit.mockRejectedValueOnce(new TypeError("网络连接中断"));
    const view = show(); await selectImages();
    fireEvent.click(screen.getByRole("button", { name: "处理 2 张图片" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认提交" }));
    const retry = await screen.findByRole("button", { name: "重试提交" });
    expect(retry).toBeEnabled();
    expect(within(screen.getByRole("dialog")).getByRole("button", { name: "取消" })).toBeDisabled();
    expect(sessionStorage.getItem("toolbox-pending:toolbox-user")).not.toBeNull();
    view.unmount(); show();
    fireEvent.click(await screen.findByRole("button", { name: "重试提交" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(submit).toHaveBeenCalledTimes(2);
    expect(submit.mock.calls[0][0]).toEqual(submit.mock.calls[1][0]);
    expect(fetcher.mock.calls.filter(([url]) => url === "/api/v1/toolbox/quote")).toHaveLength(1);
    expect(sessionStorage.getItem("toolbox-pending:toolbox-user")).toBeNull();
  });
  it("allows correction after a definitive rejection and discards an invalid saved draft", async () => {
    sessionStorage.setItem("toolbox-pending:toolbox-user", JSON.stringify({ items: [{}] }));
    submit.mockResolvedValueOnce(response({ code: "JOB_QUOTE_EXPIRED", message: "报价已过期" }, 409));
    show(); await selectImages();
    fireEvent.click(screen.getByRole("button", { name: "处理 2 张图片" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认提交" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("报价已过期");
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "取消" }));
    expect(screen.getByRole("button", { name: "处理 2 张图片" })).toBeEnabled();
    expect(sessionStorage.getItem("toolbox-pending:toolbox-user")).toBeNull();
  });

  it("submits adjustment and text watermark settings as one composed toolbox request", async () => {
    show(); await selectImages();
    fireEvent.click(screen.getByRole("button", { name: /调色增强/ }));
    fireEvent.change(screen.getByLabelText("亮度"), { target: { value: "35" } });
    fireEvent.change(screen.getByLabelText("模糊"), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: "灰度" }));
    fireEvent.click(screen.getByRole("button", { name: /添加水印/ }));
    fireEvent.click(screen.getByRole("button", { name: "文字" }));
    fireEvent.change(screen.getByLabelText("水印文字"), { target: { value: "SAMPLE" } });
    fireEvent.change(screen.getByLabelText("水印位置"), { target: { value: "top-left" } });
    fireEvent.change(screen.getByLabelText("水印透明度"), { target: { value: "70" } });
    fireEvent.click(screen.getByRole("button", { name: "处理 2 张图片" }));
    await screen.findByRole("dialog", { name: "确认批量处理" });
    const quoteCall = fetcher.mock.calls.filter(([url]) => url === "/api/v1/toolbox/quote").at(-1)!;
    expect(JSON.parse(String(quoteCall[1]?.body)).options).toMatchObject({
      brightness: 35, blur: 3, grayscale: true, watermark: "text", watermark_text: "SAMPLE",
      watermark_position: "top-left", watermark_opacity: 70,
    });
  });

  it("selects an image watermark from the paginated asset picker", async () => {
    show(); await selectImages();
    fireEvent.click(screen.getByRole("button", { name: /添加水印/ }));
    fireEvent.click(screen.getByRole("button", { name: "图片" }));
    fireEvent.click(screen.getByRole("button", { name: "从素材库选择水印图" }));
    fireEvent.click(await screen.findByRole("button", { name: "选择 first.png" }));
    fireEvent.click(screen.getByRole("button", { name: "确认选择" }));
    fireEvent.click(screen.getByRole("button", { name: "处理 2 张图片" }));
    await screen.findByRole("dialog", { name: "确认批量处理" });
    const quoteCall = fetcher.mock.calls.filter(([url]) => url === "/api/v1/toolbox/quote").at(-1)!;
    expect(JSON.parse(String(quoteCall[1]?.body)).options).toMatchObject({ watermark: "image", watermark_asset_id: "first" });
  });

  it("keeps every file in a full batch downloadable after retries add another history page", async () => {
    window.history.replaceState({}, "", "/app/toolbox?batch=batch-1");
    const completed = Array.from({ length: 50 }, (_, index) => ({
      id: `job-${index}`, source_asset_id: `source-${index}`, output_asset_id: `out-${index}`, status: "succeeded", operation_code: "image.toolbox",
      parameters: { batch_id: "batch-1", batch_name: "完整批次", output_name: `file-${index}.webp` },
    }));
    const originalFetch = fetcher.getMockImplementation()!;
    fetcher.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.startsWith("/api/v1/jobs?")) return response(new URL(url, "http://localhost").searchParams.has("cursor")
        ? { items: completed.slice(49), next_cursor: null }
        : { items: [completed[0], { ...completed[0], id: "old-failed", status: "failed", output_asset_id: null }, ...completed.slice(1, 49)], next_cursor: "older" });
      return originalFetch(url, init);
    });
    show();
    expect(await screen.findByRole("button", { name: "打包已完成 (50)" })).toBeEnabled();
    expect(screen.getAllByRole("article")).toHaveLength(50);
    expect(screen.queryByRole("button", { name: "重试此图" })).not.toBeInTheDocument();
  });
});
