import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import UserApp from "./UserApp";

describe("UserApp authentication", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/login");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ registration_enabled: false }))));
  });

  it("renders a labelled login form and toggles password visibility", () => {
    render(<UserApp />);

    expect(screen.getByRole("heading", { name: "登录账号" })).toBeInTheDocument();
    expect(screen.getByLabelText("邮箱或用户名")).toHaveAttribute("autocomplete", "username");
    const password = screen.getByLabelText("密码");
    expect(password).toHaveAttribute("type", "password");

    fireEvent.click(screen.getByRole("button", { name: "显示密码" }));
    expect(password).toHaveAttribute("type", "text");
    expect(screen.getByRole("button", { name: "隐藏密码" })).toBeInTheDocument();
  });

  it("submits credentials and enters the authenticated workspace", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      new Response(JSON.stringify({ user: { id: "user-1" } }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<UserApp />);

    fireEvent.change(screen.getByLabelText("邮箱或用户名"), {
      target: { value: "member@example.test" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "correct horse battery staple" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => expect(window.location.pathname).toBe("/app"));
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/auth/login",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("announces the server error and preserves the login route", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () =>
        new Response(
          JSON.stringify({ code: "INVALID_CREDENTIALS", message: "账号或密码错误" }),
          { status: 401, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    render(<UserApp />);

    fireEvent.change(screen.getByLabelText("邮箱或用户名"), {
      target: { value: "member@example.test" },
    });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "wrong password" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("账号或密码错误");
    expect(window.location.pathname).toBe("/login");
  });

  it("hides registration when disabled and explains a closed direct registration URL", async () => {
    window.history.replaceState({}, "", "/register");
    render(<UserApp />);
    expect(await screen.findByText(/管理员已关闭注册/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "注册并进入工作台" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /返回登录/ }));
    expect(screen.queryByRole("button", { name: /创建账号/ })).not.toBeInTheDocument();
  });

  it("registers with matching passwords and handles a switch closed after the page was opened", async () => {
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => url.endsWith("/options")
      ? new Response(JSON.stringify({ registration_enabled: true }))
      : new Response(JSON.stringify({ code: "REGISTRATION_CLOSED", message: "管理员已关闭注册" }), { status: 403 }));
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", "/register");
    render(<UserApp />);
    fireEvent.change(await screen.findByLabelText("显示名称"), { target: { value: "设计师" } });
    fireEvent.change(screen.getByLabelText("邮箱"), { target: { value: "new@example.test" } });
    fireEvent.change(screen.getByLabelText("邮箱验证码"), { target: { value: "123456" } });
    fireEvent.change(screen.getByLabelText(/^密码/), { target: { value: "new secure password" } });
    fireEvent.change(screen.getByLabelText("确认密码"), { target: { value: "another secure password" } });
    fireEvent.click(screen.getByRole("button", { name: "注册并进入工作台" }));
    expect(await screen.findByText("两次输入的密码不一致")).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([url]) => url.endsWith("/register"))).toHaveLength(0);
    fireEvent.change(screen.getByLabelText("确认密码"), { target: { value: "new secure password" } });
    fireEvent.click(screen.getByRole("button", { name: "注册并进入工作台" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "注册并进入工作台" })).not.toBeInTheDocument());
    const body = fetchMock.mock.calls.find(([url]) => url.endsWith("/register"))?.[1]?.body;
    expect(JSON.parse(String(body))).toEqual({ email: "new@example.test", display_name: "设计师", password: "new secure password", verification_code: "123456" });
  });

  it("sends to the normalized email, prevents repeated sends, and clears code on email change", async () => {
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => new Response(JSON.stringify(url.endsWith("/options") ? { registration_enabled: true } : { status: "sent", retry_after_seconds: 60, expires_in_seconds: 600 })));
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", "/register");
    render(<UserApp />);
    fireEvent.change(await screen.findByLabelText("邮箱"), { target: { value: "NEW@Example.test" } });
    fireEvent.click(screen.getByRole("button", { name: "获取验证码" }));
    expect(await screen.findByRole("status")).toHaveTextContent("new@example.test");
    expect(screen.getByRole("button", { name: /秒后重发/ })).toBeDisabled();
    expect(JSON.parse(String(fetchMock.mock.calls.find(([url]) => url.endsWith("/email-code"))?.[1]?.body))).toEqual({ email: "new@example.test" });
    fireEvent.change(screen.getByLabelText("邮箱验证码"), { target: { value: "123456" } });
    fireEvent.change(screen.getByLabelText("邮箱"), { target: { value: "another@example.test" } });
    expect(screen.getByLabelText("邮箱验证码")).toHaveValue("");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("shows email delivery errors and respects the server resend cooldown", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => url.endsWith("/options")
      ? new Response(JSON.stringify({ registration_enabled: true }))
      : new Response(JSON.stringify({ code: "CODE_SEND_TOO_SOON", message: "请稍后再试" }), { status: 429, headers: { "Retry-After": "90" } })));
    window.history.replaceState({}, "", "/register");
    render(<UserApp />);
    fireEvent.change(await screen.findByLabelText("邮箱"), { target: { value: "new@example.test" } });
    fireEvent.click(screen.getByRole("button", { name: "获取验证码" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("请稍后再试");
    expect(screen.getByRole("button", { name: /秒后重发/ })).toBeDisabled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
