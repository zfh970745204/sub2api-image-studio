import { StrictMode } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AssetPickerDialog } from "./AssetPickerDialog";
import { api, type Asset } from "./user-api";

function asset(id: string, overrides: Partial<Asset> = {}): Asset {
  return {
    id, root_asset_id: id, parent_asset_id: null, source_job_id: null,
    kind: "original", operation_code: "upload", original_filename: `${id}.png`,
    mime_type: "image/png", extension: "png", size_bytes: 5000, width: 1200, height: 800,
    has_alpha: true, status: "ready", metadata: { label: id },
    created_at: "2026-09-10T00:00:00Z", updated_at: "2026-09-10T00:00:00Z", ...overrides,
  };
}
const page = (items: Asset[], next_cursor: string | null = null) => ({ items, next_cursor });
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const confirm = () => screen.getByRole("button", { name: "确认选择" });
const choose = (id: string) => screen.getByRole("button", { name: `选择 ${id}.png` });

describe("AssetPickerDialog", () => {
  it("caps cross-page multiple selections, permits deselection and confirms full assets", async () => {
    const first = asset("first"), later = asset("later"), extra = asset("extra");
    vi.spyOn(api, "assets").mockImplementation(async (_kind, options) => options?.cursor ? page([later, extra]) : page([first], "next"));
    const onSelectMany = vi.fn();
    render(<AssetPickerDialog maxSelection={2} onSelectMany={onSelectMany} onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "选择 first.png" }));
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    fireEvent.click(await screen.findByRole("button", { name: "选择 later.png" }));
    expect(choose("extra")).toBeDisabled();
    expect(screen.getByText("已选择 2 / 2 张图片")).toBeInTheDocument();
    fireEvent.click(choose("later"));
    expect(choose("extra")).toBeEnabled();
    fireEvent.click(choose("extra"));
    fireEvent.click(confirm());
    expect(onSelectMany).toHaveBeenCalledExactlyOnceWith([first, extra]);
  });

  it("queries editable assets with server-side kinds, cursors and page sizes", async () => {
    const fetcher = vi.fn(async (url: string) => {
      const query = new URL(url, "http://localhost").searchParams;
      return new Response(JSON.stringify(query.has("cursor") ? page([asset("later")]) : page([asset("first")], "next/+=")));
    });
    vi.stubGlobal("fetch", fetcher);
    const { container } = render(<AssetPickerDialog title="添加参考图" onSelect={vi.fn()} onClose={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "添加参考图" });
    expect(container).toBeEmptyDOMElement();
    expect(dialog.parentElement).toBe(document.body);
    expect(dialog).toHaveAccessibleDescription("从素材库选择一张图片，确认后使用。");
    await screen.findByRole("button", { name: "选择 first.png" });
    expect(fetcher).toHaveBeenCalledTimes(1);
    const query = () => Object.fromEntries(new URL(fetcher.mock.calls.at(-1)![0], "http://localhost").searchParams);
    expect(query()).toEqual({ limit: "20", editable_only: "true" });
    expect(screen.getByRole("img", { name: "first.png" })).toHaveAttribute("src", "/api/v1/assets/first/thumbnail");
    expect(screen.getByRole("img", { name: "first.png" })).toHaveAttribute("loading", "lazy");
    expect(choose("first")).toHaveTextContent("1200 × 800");
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await screen.findByRole("button", { name: "选择 later.png" });
    expect(query()).toEqual({ limit: "20", cursor: "next/+=", editable_only: "true" });
    expect(screen.getByText("第 2 页")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "原图" }));
    await screen.findByRole("button", { name: "选择 first.png" });
    expect(query()).toEqual({ limit: "20", editable_only: "true", kind: "original" });
    expect(screen.getByText("第 1 页")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await screen.findByRole("button", { name: "选择 later.png" });
    fireEvent.change(screen.getByRole("combobox", { name: "每页" }), { target: { value: "40" } });
    await screen.findByRole("button", { name: "选择 first.png" });
    expect(query()).toEqual({ limit: "40", editable_only: "true", kind: "original" });
    expect(screen.getByText("第 1 页")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "处理结果" }));
    await waitFor(() => expect(query()).toEqual({ limit: "40", editable_only: "true", kind: "result" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "刷新" })).toBeEnabled());
    expect(fetcher.mock.calls.every(([url]) => new URL(url, "http://localhost").pathname === "/api/v1/assets")).toBe(true);
  });

  it("confirms the complete later-page asset even after navigating back and changing kinds", async () => {
    const later = asset("later", { kind: "result", parent_asset_id: "first", source_job_id: "job-2", metadata: { prompt: "retained metadata" } });
    vi.spyOn(api, "assets").mockImplementation(async (_kind, options) => options?.cursor ? page([later]) : page([asset("first")], "next"));
    const onSelect = vi.fn();
    const onClose = vi.fn();
    render(<AssetPickerDialog onSelect={onSelect} onClose={onClose} />);
    await screen.findByRole("button", { name: "选择 first.png" });
    expect(confirm()).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await screen.findByRole("button", { name: "选择 later.png" });
    fireEvent.click(choose("later"));
    expect(choose("later")).toHaveAttribute("aria-pressed", "true");
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "上一页" }));
    await screen.findByRole("button", { name: "选择 first.png" });
    fireEvent.click(screen.getByRole("button", { name: "原图" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "刷新" })).toBeEnabled());
    expect(screen.getByText("已选择：later.png")).toBeInTheDocument();
    expect(choose("first")).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(confirm());
    expect(onSelect).toHaveBeenCalledExactlyOnceWith(later);
    expect(onSelect.mock.calls[0][0]).toBe(later);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("recognizes selectedId on a later page and updates when the parent changes it", async () => {
    const first = asset("first"), later = asset("later");
    vi.spyOn(api, "assets").mockImplementation(async (_kind, options) => options?.cursor ? page([later]) : page([first], "next"));
    const onSelect = vi.fn(), onClose = vi.fn();
    const view = render(<AssetPickerDialog selectedId="later" onSelect={onSelect} onClose={onClose} />);
    await screen.findByRole("button", { name: "选择 first.png" });
    expect(confirm()).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(choose("later")).toHaveAttribute("aria-pressed", "true"));
    fireEvent.click(confirm());
    expect(onSelect).toHaveBeenCalledWith(later);
    view.rerender(<AssetPickerDialog selectedId="first" onSelect={onSelect} onClose={onClose} />);
    expect(confirm()).toBeDisabled();
    expect(choose("later")).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(screen.getByRole("button", { name: "上一页" }));
    await waitFor(() => expect(choose("first")).toHaveAttribute("aria-pressed", "true"));
    fireEvent.click(confirm());
    expect(onSelect).toHaveBeenLastCalledWith(first);
  });

  it("prevents excluded selections, including exclusions added after choosing, without losing pagination", async () => {
    vi.spyOn(api, "assets").mockImplementation(async (_kind, options) => options?.cursor ? page([asset("available")]) : page([asset("existing")], "next"));
    const onSelect = vi.fn(), onClose = vi.fn();
    const view = render(<AssetPickerDialog selectedId="existing" excludedIds={["existing"]} onSelect={onSelect} onClose={onClose} />);
    const excluded = await screen.findByRole("button", { name: "选择 existing.png（已添加）" });
    expect(excluded).toBeDisabled();
    expect(excluded).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(excluded);
    fireEvent.click(confirm());
    expect(confirm()).toBeDisabled();
    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "下一页" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await screen.findByRole("button", { name: "选择 available.png" });
    fireEvent.click(choose("available"));
    expect(confirm()).toBeEnabled();
    view.rerender(<AssetPickerDialog selectedId="existing" excludedIds={["existing", "available"]} onSelect={onSelect} onClose={onClose} />);
    expect(confirm()).toBeDisabled();
    expect(screen.queryByText("已选择：available.png")).not.toBeInTheDocument();
    fireEvent.click(confirm());
    expect(onSelect).not.toHaveBeenCalled();
  });

  it.each(["resolve", "reject"] as const)("ignores a stale later page that %ss after a filter change", async (settlement) => {
    const stale = deferred<ReturnType<typeof page>>();
    const fresh = asset("fresh", { kind: "result" });
    const loader = vi.spyOn(api, "assets")
      .mockResolvedValueOnce(page([asset("first")], "old-next"))
      .mockReturnValueOnce(stale.promise)
      .mockResolvedValueOnce(page([fresh]));
    const onSelect = vi.fn();
    render(<AssetPickerDialog selectedId="stale" onSelect={onSelect} onClose={vi.fn()} />);
    await screen.findByRole("button", { name: "选择 first.png" });
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(screen.getByText("正在加载素材…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "处理结果" }));
    await screen.findByRole("button", { name: "选择 fresh.png" });
    expect(loader).toHaveBeenLastCalledWith("result", { cursor: null, limit: 20, editable_only: "true" });
    await act(async () => {
      if (settlement === "resolve") stale.resolve(page([asset("stale")], "stale-next"));
      else stale.reject(new Error("stale failure"));
    });
    expect(screen.queryByRole("button", { name: "选择 stale.png" })).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("第 1 页")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
    expect(confirm()).toBeDisabled();
    fireEvent.click(choose("fresh"));
    fireEvent.click(confirm());
    expect(onSelect).toHaveBeenCalledExactlyOnceWith(fresh);
  });

  it("retries a failed later page with the same query and retains a previous selection", async () => {
    const first = asset("first");
    const retry = deferred<ReturnType<typeof page>>();
    const loader = vi.spyOn(api, "assets")
      .mockResolvedValueOnce(page([first], "next"))
      .mockRejectedValueOnce(new Error("网络暂时不可用"))
      .mockReturnValueOnce(retry.promise);
    const onSelect = vi.fn();
    render(<AssetPickerDialog onSelect={onSelect} onClose={vi.fn()} />);
    await screen.findByRole("button", { name: "选择 first.png" });
    fireEvent.click(choose("first"));
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("网络暂时不可用");
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(loader).toHaveBeenLastCalledWith("", { cursor: "next", limit: 20, editable_only: "true" });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("正在加载素材…")).toBeInTheDocument();
    await act(async () => retry.resolve(page([asset("later")])));
    expect(screen.getByText("第 2 页")).toBeInTheDocument();
    expect(choose("later")).toBeEnabled();
    expect(screen.getByText("已选择：first.png")).toBeInTheDocument();
    fireEvent.click(confirm());
    expect(onSelect).toHaveBeenCalledExactlyOnceWith(first);
  });

  it("shows empty states and recovers with refresh", async () => {
    vi.spyOn(api, "assets").mockResolvedValueOnce(page([])).mockResolvedValueOnce(page([])).mockResolvedValueOnce(page([asset("new", { original_filename: null, width: null, height: null })]));
    render(<AssetPickerDialog onSelect={vi.fn()} onClose={vi.fn()} />);
    expect(await screen.findByText("暂无可选素材")).toBeInTheDocument();
    expect(confirm()).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "原图" }));
    expect(await screen.findByText("暂无原图")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    const card = await screen.findByRole("button", { name: "选择 原图 new.png" });
    expect(card).toHaveTextContent("尺寸未知");
    fireEvent.click(card);
    expect(confirm()).toBeEnabled();
  });

  it("opens natively under StrictMode, focuses close, handles Escape cancellation and restores focus", async () => {
    vi.spyOn(api, "assets").mockResolvedValue(page([]));
    const showModal = vi.fn(function (this: HTMLDialogElement) { this.setAttribute("open", ""); });
    const close = vi.fn(function (this: HTMLDialogElement) { this.removeAttribute("open"); });
    const createElement = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation((tag, options) => {
      const element = createElement(tag, options);
      if (tag === "dialog") Object.assign(element, { showModal, close });
      return element;
    });
    const triggerView = render(<button type="button">打开素材库</button>);
    const trigger = screen.getByRole("button", { name: "打开素材库" });
    trigger.focus();
    const onClose = vi.fn(), onSelect = vi.fn();
    const view = render(<StrictMode><AssetPickerDialog onSelect={onSelect} onClose={onClose} /></StrictMode>);
    await screen.findByText("暂无可选素材");
    expect(showModal).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "关闭素材选择" })).toHaveFocus();
    expect(onClose).not.toHaveBeenCalled();
    const cancel = new Event("cancel", { cancelable: true });
    fireEvent(screen.getByRole("dialog"), cancel);
    expect(cancel.defaultPrevented).toBe(true);
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onSelect).not.toHaveBeenCalled();
    view.unmount();
    expect(close).toHaveBeenCalled();
    expect(trigger).toHaveFocus();
    expect(onClose).toHaveBeenCalledTimes(1);
    triggerView.unmount();
  });

  it("dismisses from explicit controls and a backdrop click, but not a drag from content", async () => {
    vi.spyOn(api, "assets").mockResolvedValue(page([]));
    const onClose = vi.fn(), onSelect = vi.fn();
    render(<AssetPickerDialog onSelect={onSelect} onClose={onClose} />);
    await screen.findByText("暂无可选素材");
    const dialog = screen.getByRole("dialog");
    const heading = within(dialog).getByRole("heading", { name: "选择素材" });
    fireEvent.pointerDown(heading);
    fireEvent.click(heading);
    fireEvent.pointerDown(heading);
    fireEvent.click(dialog);
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.pointerDown(dialog);
    fireEvent.click(dialog);
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(onClose).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "关闭素材选择" }));
    expect(onClose).toHaveBeenCalledTimes(3);
    expect(onSelect).not.toHaveBeenCalled();
  });
});
