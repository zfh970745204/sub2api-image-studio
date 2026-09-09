import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import UserApp from "./UserApp";

describe("UserApp authentication", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/login");
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
    const fetchMock = vi.fn().mockResolvedValue(
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
      vi.fn().mockResolvedValue(
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
});
