import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AdminApp from "./AdminApp";
import { AdminEditor } from "./AdminEditors";
import type { AdminRow } from "./admin-api";

const permissions = new Set(["config.read", "config.manage", "config.test", "pricing.read", "pricing.manage", "memberships.read", "memberships.manage", "users.read", "users.manage", "points.read", "points.adjust", "roles.read", "roles.manage"]);
const defaults = { enabled: false, base_url: "", image_model: "gpt-image-2", timeout_seconds: 180 };
const configRow = { id: "config-1", code: "sub2api", name: "Sub2API", active_version: null, active: null, defaults };
const response = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });

function mockAdmin(items: AdminRow[], allowed = [...permissions]) {
  return vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/v1/auth/me") return response({ user: { id: "admin-1", display_name: "管理员", roles: ["super_admin"], status: "active" }, permissions: allowed });
    if (url.includes("saved-views")) return response({ items: [] });
    if (init?.method === "PUT") return response({ version: { status: "active" } });
    return response({ items });
  });
}

describe("administrator configuration workflows", () => {
  beforeEach(() => window.history.replaceState({}, "", "/admin/settings"));

  it("saves the registration switch directly with the configured defaults", async () => {
    const fetchMock = mockAdmin([]);
    vi.stubGlobal("fetch", fetchMock);
    render(<AdminEditor kind="settings" row={{ code: "general", name: "通用设置", active_version: 4, active: { values: { registration_enabled: true, default_membership_code: "free", default_points: 50, max_upload_mb: 20, max_image_megapixels: 40, signed_url_ttl_seconds: 600, task_concurrency: 2 } } }} permissions={permissions} onClose={vi.fn()} onSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole("checkbox", { name: /开放用户注册/ }));
    fireEvent.click(screen.getByRole("button", { name: "保存并生效" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(body).toMatchObject({ base_version: 4, values: { registration_enabled: false, default_points: 50 } });
  });

  it("keeps dashboard metrics available when only the trend request fails", async () => {
    window.history.replaceState({}, "", "/admin");
    const defaults = mockAdmin([], ["admin.dashboard.read"]);
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (url.includes("dashboard/summary")) return response({ users: { active: 12, registrations: 3 }, jobs: { total: 8, queue_length: 1, success_rate: .75, by_status: {}, failure_reasons: [] }, points: {}, storage: { bytes: 1024, new_asset_count: 2, quarantined: 0, deletion_failures: 0 }, sub2api: { configured: false, requests: 0, success_rate: null }, system: { r2_configured: false, services: {} } });
      if (url.includes("dashboard/timeseries")) return response({ message: "趋势暂不可用" }, 503);
      return defaults(url, init);
    }));
    render(<AdminApp />);
    expect(await screen.findByText("12")).toBeInTheDocument();
    expect(screen.getByText("趋势暂不可用")).toBeInTheDocument();
    expect(screen.getByText("任务成功率")).toBeInTheDocument();
  });

  it("opens real config fields for a super admin and saves without an approval request", async () => {
    const fetchMock = mockAdmin([configRow]);
    vi.stubGlobal("fetch", fetchMock);
    render(<AdminApp />);
    fireEvent.click(await screen.findByRole("cell", { name: "Sub2API" }));
    expect(screen.getByRole("dialog", { name: "配置 Sub2API" })).toBeInTheDocument();
    expect(screen.queryByText(/提交.*申请/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("启用 Sub2API"));
    fireEvent.change(screen.getByLabelText(/接口地址/), { target: { value: "https://api.example.test/v1" } });
    fireEvent.change(screen.getByLabelText(/API Key/), { target: { value: "new-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "保存并生效" }));
    await screen.findByText("配置已保存并生效");
    const save = fetchMock.mock.calls.find(([url, init]) => url === "/api/v1/admin/config/sub2api" && init?.method === "PUT");
    expect(JSON.parse(String(save?.[1]?.body))).toEqual({ values: { ...defaults, enabled: true, base_url: "https://api.example.test/v1" }, secrets: { api_key: "new-secret" }, base_version: null });
    expect(fetchMock.mock.calls.some(([url]) => url.includes("action-requests"))).toBe(false);
  });

  it("keeps configuration editable after a failed save and leaves masked keys out of requests", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => response({ message: "配置已被其他管理员修改，请刷新后重试" }, 409));
    vi.stubGlobal("fetch", fetchMock);
    render(<AdminEditor kind="settings" row={{ ...configRow, active_version: 2, active: { values: { ...defaults, base_url: "https://saved.example.test" }, secrets: { api_key: { has_value: true, last_four: "1234" } } } }} permissions={permissions} onClose={vi.fn()} onSaved={vi.fn()} />);
    expect(screen.getByLabelText(/API Key/)).toHaveValue("");
    fireEvent.change(screen.getByLabelText("图片模型"), { target: { value: "new-model" } });
    fireEvent.click(screen.getByRole("button", { name: "保存并生效" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("配置已被其他管理员修改");
    expect(screen.getByLabelText("图片模型")).toHaveValue("new-model");
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/admin/config/sub2api", expect.objectContaining({ body: expect.stringContaining('"secrets":{}') }));
  });

  it("does not show edit or save controls to a read-only administrator", async () => {
    vi.stubGlobal("fetch", mockAdmin([configRow], ["config.read"]));
    render(<AdminApp />);
    fireEvent.click(await screen.findByRole("cell", { name: "Sub2API" }));
    expect(screen.queryByLabelText("图片模型")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存并生效" })).not.toBeInTheDocument();
  });

  it("saves price and operation settings together without dropping surcharge rules", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => response({ operation: {} }));
    vi.stubGlobal("fetch", fetchMock);
    const rules = { rules: [{ parameter: "size", type: "choice", points: { large: 5 } }] };
    const saved = vi.fn();
    render(<AdminEditor kind="pricing" row={{ id: "op-1", code: "ai.generate", name: "AI 生成", enabled: true, timeout_seconds: 240, max_attempts: 3, current_price: { base_points: 20, parameter_rules: rules } }} permissions={permissions} onClose={vi.fn()} onSaved={saved} />);
    fireEvent.change(screen.getByLabelText("基础积分单价"), { target: { value: "37" } });
    fireEvent.click(screen.getByRole("button", { name: "保存价格与开关" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/admin/operations/ai.generate/configuration", expect.objectContaining({ method: "PUT" }));
    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(body.base_points).toBe(37);
    expect(body.parameter_rules).toEqual(rules);
  });

  it("creates a usable active membership plan with correctly converted discount", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => response({ plan: {} }));
    vi.stubGlobal("fetch", fetchMock);
    const saved = vi.fn();
    render(<AdminEditor kind="memberships" row={{}} permissions={permissions} onClose={vi.fn()} onSaved={saved} />);
    fireEvent.change(screen.getByLabelText(/套餐代码/), { target: { value: "team" } });
    fireEvent.change(screen.getByLabelText("套餐名称"), { target: { value: "团队会员" } });
    fireEvent.change(screen.getByLabelText(/积分计价比例/), { target: { value: "85" } });
    fireEvent.click(screen.getByRole("button", { name: "保存套餐" }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(body).toMatchObject({ code: "team", status: "active", operation_discount_bps: 8500 });
    expect(body).not.toHaveProperty("periodic_points");
  });

  it("adjusts an identified user's balance and reuses the command key after a network error", async () => {
    let attempts = 0;
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method !== "POST") return response({ account: { balance: 20 } });
      if (++attempts === 1) throw new TypeError("网络中断");
      return response({ adjustment: { status: "applied" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    const saved = vi.fn();
    render(<AdminEditor kind="points" row={{ id: "user-1", email: "member@example.test" }} permissions={permissions} onClose={vi.fn()} onSaved={saved} />);
    await screen.findByText("20");
    fireEvent.change(screen.getByLabelText(/调整积分/), { target: { value: "10000" } });
    fireEvent.change(screen.getByLabelText("调整原因"), { target: { value: "运营赠送" } });
    fireEvent.click(screen.getByRole("button", { name: "确认调整积分" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "确认调整积分" }));
    await waitFor(() => expect(saved).toHaveBeenCalledWith("积分已调整并记入流水"));
    const writes = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(writes[0][0]).toBe("/api/v1/admin/users/user-1/points/adjustments");
    expect(writes[0][1]?.headers).toEqual(writes[1][1]?.headers);
    expect(JSON.parse(String(writes[0][1]?.body))).toEqual({ amount: 10000, reason: "运营赠送" });
  });

  it("exposes membership, points and role management from the user detail", async () => {
    window.history.replaceState({}, "", "/admin/users");
    vi.stubGlobal("fetch", mockAdmin([{ id: "user-1", email: "member@example.test", display_name: "测试用户", status: "active" }]));
    render(<AdminApp />);
    fireEvent.click(await screen.findByRole("cell", { name: "测试用户" }));
    expect(screen.getByRole("button", { name: "分配 / 续期会员" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "调整积分" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "分配角色" })).toBeInTheDocument();
    expect(screen.queryByText(/提交.*申请/)).not.toBeInTheDocument();
  });
});
