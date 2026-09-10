import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import UserApp from "./UserApp";

const bootstrap = {
  user: { id: "user-1", display_name: "测试用户", email: "member@example.test" },
  permissions: ["studio.use", "tasks.create"],
  membership: { plan: { name: "Free" }, entitlements: { max_upload_mb: 20 } },
  points: { balance: 200, lifetime_spent: 0, lifetime_earned: 200, status: "active" },
  service: { status: "ok", features: { sub2api_configured: true } },
  notifications: { unread_count: 1 },
  preferences: { theme: "light", studio_layout: {} },
};
const notification = { id: "note-1", title: "图片处理完成", body: "长通知内容与文件名 " + "a".repeat(160), target_url: "/app/jobs", read_at: null, created_at: "2026-09-10T00:00:00Z" };
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

describe("Notifications", () => {
  let markRead: ReturnType<typeof vi.fn<() => Promise<Response>>>;
  beforeEach(() => {
    window.history.replaceState({}, "", "/app");
    markRead = vi.fn(async () => response({ updated: 1 }));
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.endsWith("/bootstrap")) return response(bootstrap);
      if (url.includes("/notifications?")) return response({ items: [notification] });
      if (url.endsWith("/read-all") || url.endsWith("/read")) return markRead();
      return response({ items: [] });
    }));
  });

  async function open() {
    render(<UserApp />);
    const trigger = await screen.findByRole("button", { name: "任务通知" });
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: "通知" });
    await within(dialog).findByText("图片处理完成");
    return { dialog, trigger };
  }

  it("supports explicit close, Escape, outside dismissal and restores trigger focus", async () => {
    const { trigger } = await open();
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: "关闭通知" })).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    for (const close of [() => fireEvent.click(screen.getByRole("button", { name: "关闭通知" })), () => fireEvent.click(screen.getByRole("button", { name: "关闭通知遮罩" })), () => fireEvent.pointerDown(document.body)]) {
      fireEvent.click(trigger);
      close();
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(trigger).toHaveAttribute("aria-expanded", "false");
    }
  });

  it("keeps notifications available after read failure and lets the user retry", async () => {
    markRead.mockResolvedValueOnce(response({ message: "连接失败" }, 503));
    const { dialog } = await open();
    fireEvent.click(within(dialog).getByRole("button", { name: "全部已读" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("连接失败");
    expect(within(dialog).getByText("图片处理完成")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "全部已读" }));
    await waitFor(() => expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument());
    expect(within(dialog).getByRole("button", { name: "全部已读" })).toBeDisabled();
    expect(markRead).toHaveBeenCalledTimes(2);
  });
});
