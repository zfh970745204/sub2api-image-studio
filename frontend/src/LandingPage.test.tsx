import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { LandingPage } from "./LandingPage";

const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });

describe("public landing session and feature navigation", () => {
  it("reads the existing cookie session and links directly to the workspace after returning home", async () => {
    const fetcher = vi.fn(async (url: string) => url.endsWith("/me")
      ? response({ user: { id: "u1", display_name: "设计师" } }) : response({ registration_enabled: true }));
    vi.stubGlobal("fetch", fetcher);
    const first = render(<LandingPage />);
    expect(await screen.findByText("你好，设计师")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "登录" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "开始创作" }).every(link => link.getAttribute("href") === "/app/studio")).toBe(true);
    expect(screen.getByRole("link", { name: "试试印花提取" })).toHaveAttribute("href", "/app/studio?tool=ai.extract_print");
    first.unmount();
    render(<LandingPage />);
    expect(await screen.findByText("你好，设计师")).toBeInTheDocument();
    expect(fetcher.mock.calls.filter(([url]) => url.endsWith("/me"))).toHaveLength(2);
    expect(fetcher.mock.calls.some(([url]) => url.endsWith("/login"))).toBe(false);
  });

  it("respects the registration switch for guests and checks for session changes when returning to the tab", async () => {
    let signedIn = false;
    let registration = false;
    vi.stubGlobal("fetch", vi.fn(async (url: string) => url.endsWith("/me")
      ? signedIn ? response({ user: { id: "u1", display_name: "设计师" } }) : response({ message: "未登录" }, 401)
      : response({ registration_enabled: registration })));
    render(<LandingPage />);
    expect(await screen.findByRole("link", { name: "登录" })).toHaveAttribute("href", "/login");
    expect(screen.queryByRole("link", { name: "开始使用" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "登录并开始创作" })[0]).toHaveAttribute("href", "/login");
    signedIn = true;
    act(() => window.dispatchEvent(new Event("focus")));
    expect(await screen.findByText("你好，设计师")).toBeInTheDocument();
    signedIn = false; registration = true;
    act(() => window.dispatchEvent(new Event("pageshow")));
    expect(await screen.findByRole("link", { name: "开始使用" })).toHaveAttribute("href", "/register");
    expect(screen.queryByText("你好，设计师")).not.toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "创建账号，开始创作" })).toHaveLength(2);
    for (const link of screen.getAllByRole("link", { name: "创建账号，开始创作" })) {
      expect(link).toHaveAttribute("href", "/register");
    }
  });

  it("keeps a direct workspace entry during session lookup failures and switches effect examples with the keyboard", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => url.endsWith("/me") ? response({}, 503) : response({ registration_enabled: true })));
    render(<LandingPage />);
    await waitFor(() => expect(screen.getAllByRole("link", { name: "进入工作台" })[0]).toHaveAttribute("href", "/app"));
    expect(screen.queryByRole("link", { name: "登录" })).not.toBeInTheDocument();
    const extraction = screen.getByRole("tab", { name: "印花提取" });
    fireEvent.keyDown(extraction, { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "电商主图" })).toHaveFocus();
    expect(within(screen.getByRole("tabpanel")).getByRole("img", { name: "电商主图" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "试试电商主图" })).toHaveAttribute("href", "/app/studio?tool=ai.ecommerce");
    fireEvent.click(screen.getByRole("tab", { name: "高清重绘" }));
    expect(within(screen.getByRole("tabpanel")).getByRole("img", { name: "高清重绘结果" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "AI 生图" }));
    expect(within(screen.getByRole("tabpanel")).getByRole("img", { name: "AI 生成结果" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "印花提取" }));
    const extractionPanel = within(screen.getByRole("tabpanel"));
    expect(extractionPanel.getByRole("img", { name: "真实 T 恤原图" })).toHaveAttribute("src", "/brand/shirt-source-v3-small.webp");
    expect(extractionPanel.getByRole("img", { name: "透明印花 PNG" })).toHaveAttribute("srcset", "/brand/shirt-print-v3-small.webp 640w, /brand/shirt-print-v3.png 1200w");
  });
});
