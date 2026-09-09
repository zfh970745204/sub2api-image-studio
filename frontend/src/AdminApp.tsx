import {
  Activity,
  ArrowLeft,
  ArrowRight,
  BadgeCheck,
  Ban,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Clock3,
  Coins,
  Download,
  Eye,
  Images,
  LayoutDashboard,
  ListFilter,
  ListTodo,
  LogIn,
  LogOut,
  Menu,
  Plus,
  RefreshCw,
  Save,
  Search,
  Settings,
  ShieldAlert,
  ShieldCheck,
  SlidersHorizontal,
  Tags,
  UserRound,
  Users,
  X,
  XCircle,
} from "lucide-react";
import {
  type ComponentType,
  type FormEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

type Row = Record<string, unknown>;

interface UserInfo {
  id: string;
  email: string;
  display_name: string;
  roles?: string[];
  status: string;
}

interface SessionInfo {
  user: UserInfo;
  permissions: string[];
}

interface PageResponse {
  items: Row[];
  next_cursor?: string | null;
}

interface SearchResult {
  type: string;
  id: string;
  title: string;
  subtitle: string;
  href: string;
}

interface NavItem {
  id: ModuleId;
  label: string;
  permission: string;
  icon: ComponentType<{ size?: number; strokeWidth?: number }>;
}

type ModuleId =
  | "dashboard"
  | "users"
  | "memberships"
  | "points"
  | "pricing"
  | "jobs"
  | "assets"
  | "settings"
  | "roles"
  | "audit";

interface Column {
  key: string;
  label: string;
  render?: (row: Row) => ReactNode;
  className?: string;
}

interface ModuleDefinition {
  title: string;
  eyebrow: string;
  endpoint: string;
  permission: string;
  managePermission?: string;
  columns: Column[];
  filters?: Array<{ key: string; label: string; options: Array<[string, string]> }>;
  exportModule?: string;
  riskAction?: {
    actionType: string;
    targetType: string;
    label: string;
    riskLevel: "high" | "critical";
  };
}

class ApiRequestError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function apiRequest<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as {
      message?: string;
      detail?: string | { message?: string };
    };
    const detail = typeof payload.detail === "string" ? payload.detail : payload.detail?.message;
    throw new ApiRequestError(response.status, payload.message || detail || `请求失败（${response.status}）`);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const NAV_ITEMS: NavItem[] = [
  { id: "dashboard", label: "总览", permission: "admin.dashboard.read", icon: LayoutDashboard },
  { id: "users", label: "用户", permission: "users.read", icon: Users },
  { id: "memberships", label: "会员", permission: "memberships.read", icon: BadgeCheck },
  { id: "points", label: "积分", permission: "points.read", icon: Coins },
  { id: "pricing", label: "价格", permission: "pricing.read", icon: Tags },
  { id: "jobs", label: "任务", permission: "tasks.read", icon: ListTodo },
  { id: "assets", label: "资产", permission: "assets.read", icon: Images },
  { id: "settings", label: "系统配置", permission: "config.read", icon: Settings },
  { id: "roles", label: "角色权限", permission: "roles.read", icon: ShieldCheck },
  { id: "audit", label: "审计日志", permission: "audit.read", icon: Activity },
];

function nested(row: Row, path: string): unknown {
  return path.split(".").reduce<unknown>((value, key) => {
    if (value && typeof value === "object" && key in value) {
      return (value as Row)[key];
    }
    return undefined;
  }, row);
}

function shortId(value: unknown): string {
  const text = String(value ?? "");
  return text.length > 13 ? `${text.slice(0, 8)}…${text.slice(-4)}` : text || "—";
}

function dateTime(value: unknown): string {
  if (!value) return "—";
  const parsed = new Date(String(value));
  if (Number.isNaN(parsed.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed);
}

function bytes(value: unknown): string {
  const amount = Number(value || 0);
  if (amount < 1024) return `${amount} B`;
  if (amount < 1024 ** 2) return `${(amount / 1024).toFixed(1)} KB`;
  if (amount < 1024 ** 3) return `${(amount / 1024 ** 2).toFixed(1)} MB`;
  return `${(amount / 1024 ** 3).toFixed(2)} GB`;
}

function valueText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (Array.isArray(value)) return value.length ? value.join("、") : "—";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

const STATUS_GOOD = new Set(["active", "ready", "succeeded", "approved", "executed", "published", "resolved"]);
const STATUS_BAD = new Set(["failed", "disabled", "rejected", "quarantined", "timed_out", "locked"]);
const STATUS_WAIT = new Set(["pending", "queued", "running", "retry_wait", "scheduled", "draft"]);

function StatusValue({ value }: { value: unknown }) {
  const status = String(value || "unknown");
  const Icon = STATUS_GOOD.has(status)
    ? CheckCircle2
    : STATUS_BAD.has(status)
      ? CircleAlert
      : STATUS_WAIT.has(status)
        ? Clock3
        : Activity;
  const tone = STATUS_GOOD.has(status)
    ? "good"
    : STATUS_BAD.has(status)
      ? "bad"
      : STATUS_WAIT.has(status)
        ? "wait"
        : "neutral";
  return (
    <span className={`admin-status admin-status-${tone}`}>
      <Icon size={13} />
      {status}
    </span>
  );
}

function RiskValue({ value }: { value: unknown }) {
  const risk = String(value || "normal");
  const Icon = risk === "critical" ? CircleAlert : risk === "high" ? Clock3 : CheckCircle2;
  return (
    <span className={`admin-status admin-risk-${risk}`}>
      <Icon size={13} />
      {risk}
    </span>
  );
}

const MODULES: Record<Exclude<ModuleId, "dashboard">, ModuleDefinition> = {
  users: {
    title: "用户",
    eyebrow: "账号、角色与业务摘要",
    endpoint: "/api/v1/admin/users",
    permission: "users.read",
    managePermission: "users.manage",
    exportModule: "users",
    riskAction: {
      actionType: "user.disable",
      targetType: "user",
      label: "禁用账号",
      riskLevel: "high",
    },
    filters: [
      {
        key: "status",
        label: "全部状态",
        options: [
          ["active", "正常"],
          ["pending", "待激活"],
          ["disabled", "已禁用"],
          ["locked", "已锁定"],
        ],
      },
    ],
    columns: [
      { key: "display_name", label: "用户" },
      { key: "email", label: "邮箱" },
      { key: "status", label: "状态", render: (row) => <StatusValue value={row.status} /> },
      { key: "roles", label: "角色", render: (row) => valueText(row.roles) },
      { key: "last_login_at", label: "最近登录", render: (row) => dateTime(row.last_login_at) },
      { key: "created_at", label: "注册时间", render: (row) => dateTime(row.created_at) },
    ],
  },
  memberships: {
    title: "会员",
    eyebrow: "套餐、权益与用户周期",
    endpoint: "/api/v1/admin/membership-plans",
    permission: "memberships.read",
    managePermission: "memberships.manage",
    exportModule: "memberships",
    riskAction: {
      actionType: "membership.downgrade",
      targetType: "membership_plan",
      label: "立即降级",
      riskLevel: "high",
    },
    filters: [
      {
        key: "status",
        label: "全部状态",
        options: [
          ["active", "启用"],
          ["draft", "草稿"],
          ["inactive", "停用"],
        ],
      },
    ],
    columns: [
      { key: "name", label: "套餐" },
      { key: "code", label: "代码" },
      { key: "status", label: "状态", render: (row) => <StatusValue value={row.status} /> },
      { key: "billing_period", label: "周期" },
      { key: "periodic_points", label: "周期积分" },
      {
        key: "operation_discount_bps",
        label: "计价比例",
        render: (row) => `${(Number(row.operation_discount_bps || 0) / 100).toFixed(0)}%`,
      },
      { key: "updated_at", label: "更新时间", render: (row) => dateTime(row.updated_at) },
    ],
  },
  points: {
    title: "积分",
    eyebrow: "流水、调整与异常核对",
    endpoint: "/api/v1/admin/points/transactions",
    permission: "points.read",
    managePermission: "points.adjust",
    exportModule: "points",
    riskAction: {
      actionType: "points.adjust",
      targetType: "point_transaction",
      label: "人工调整",
      riskLevel: "high",
    },
    filters: [
      {
        key: "entry_type",
        label: "全部流水",
        options: [
          ["grant", "发放"],
          ["consume", "消费"],
          ["refund", "退款"],
          ["adjust", "调整"],
          ["reversal", "冲正"],
        ],
      },
    ],
    columns: [
      { key: "id", label: "流水", render: (row) => shortId(row.id) },
      { key: "user_id", label: "用户", render: (row) => shortId(row.user_id) },
      { key: "entry_type", label: "类型" },
      {
        key: "delta",
        label: "变动",
        className: "admin-number",
        render: (row) => <strong className={Number(row.delta) >= 0 ? "delta-up" : "delta-down"}>{Number(row.delta) >= 0 ? "+" : ""}{String(row.delta)}</strong>,
      },
      { key: "balance_after", label: "余额", className: "admin-number" },
      { key: "reference_type", label: "业务来源" },
      { key: "created_at", label: "发生时间", render: (row) => dateTime(row.created_at) },
    ],
  },
  pricing: {
    title: "价格",
    eyebrow: "操作开关与生效版本",
    endpoint: "/api/v1/admin/operations",
    permission: "pricing.read",
    managePermission: "pricing.manage",
    exportModule: "pricing",
    riskAction: {
      actionType: "pricing.change",
      targetType: "operation",
      label: "发布价格",
      riskLevel: "high",
    },
    columns: [
      { key: "name", label: "操作" },
      { key: "code", label: "代码" },
      { key: "enabled", label: "可用", render: (row) => <StatusValue value={row.enabled ? "active" : "disabled"} /> },
      { key: "engine_type", label: "执行引擎" },
      { key: "current_price.base_points", label: "基础积分", className: "admin-number" },
      { key: "current_price.version", label: "价格版本", className: "admin-number" },
      { key: "updated_at", label: "更新时间", render: (row) => dateTime(row.updated_at) },
    ],
  },
  jobs: {
    title: "任务",
    eyebrow: "队列、执行与退款状态",
    endpoint: "/api/v1/admin/jobs",
    permission: "tasks.read",
    managePermission: "tasks.manage",
    exportModule: "jobs",
    riskAction: {
      actionType: "job.refund",
      targetType: "image_job",
      label: "强制退款",
      riskLevel: "high",
    },
    filters: [
      {
        key: "status",
        label: "全部状态",
        options: [
          ["queued", "排队"],
          ["running", "执行中"],
          ["retry_wait", "等待重试"],
          ["succeeded", "成功"],
          ["failed", "失败"],
          ["timed_out", "超时"],
          ["cancelled", "已取消"],
        ],
      },
    ],
    columns: [
      { key: "id", label: "任务", render: (row) => shortId(row.id) },
      { key: "operation_code", label: "操作" },
      { key: "status", label: "状态", render: (row) => <StatusValue value={row.status} /> },
      { key: "progress", label: "进度", render: (row) => `${row.progress ?? 0}%` },
      { key: "charged_points", label: "积分", className: "admin-number" },
      { key: "attempt_count", label: "尝试", className: "admin-number" },
      { key: "error_code", label: "错误" },
      { key: "created_at", label: "创建时间", render: (row) => dateTime(row.created_at) },
    ],
  },
  assets: {
    title: "资产",
    eyebrow: "存储、保留与删除队列",
    endpoint: "/api/v1/admin/assets",
    permission: "assets.read",
    managePermission: "assets.manage",
    exportModule: "assets",
    riskAction: {
      actionType: "asset.quarantine",
      targetType: "asset",
      label: "隔离资产",
      riskLevel: "high",
    },
    filters: [
      {
        key: "status",
        label: "全部状态",
        options: [
          ["ready", "可用"],
          ["uploading", "上传中"],
          ["quarantined", "已隔离"],
          ["deleted", "已删除"],
        ],
      },
      {
        key: "kind",
        label: "全部类型",
        options: [
          ["original", "原图"],
          ["result", "结果"],
          ["mask", "蒙版"],
          ["thumbnail", "缩略图"],
          ["vector", "矢量"],
        ],
      },
    ],
    columns: [
      { key: "original_filename", label: "文件", render: (row) => valueText(row.original_filename || row.id) },
      { key: "kind", label: "类型" },
      { key: "status", label: "状态", render: (row) => <StatusValue value={row.status} /> },
      { key: "size_bytes", label: "大小", render: (row) => bytes(row.size_bytes), className: "admin-number" },
      { key: "owner_id", label: "所有者", render: (row) => shortId(row.owner_id) },
      { key: "retention_until", label: "保留至", render: (row) => dateTime(row.retention_until) },
      { key: "created_at", label: "创建时间", render: (row) => dateTime(row.created_at) },
    ],
  },
  settings: {
    title: "系统配置",
    eyebrow: "连接状态、版本与发布历史",
    endpoint: "/api/v1/admin/config",
    permission: "config.read",
    managePermission: "config.manage",
    riskAction: {
      actionType: "config.publish",
      targetType: "config_group",
      label: "发布配置",
      riskLevel: "critical",
    },
    columns: [
      { key: "name", label: "配置组" },
      { key: "code", label: "代码" },
      {
        key: "active_version",
        label: "生效版本",
        render: (row) => row.active_version ? `v${row.active_version}` : "未发布",
      },
      {
        key: "active.status",
        label: "生效状态",
        render: (row) => <StatusValue value={nested(row, "active.status") || "pending"} />,
      },
      {
        key: "latest_draft.status",
        label: "草稿",
        render: (row) => valueText(nested(row, "latest_draft.status")),
      },
      { key: "updated_at", label: "更新时间", render: (row) => dateTime(row.updated_at) },
    ],
  },
  roles: {
    title: "角色权限",
    eyebrow: "权限点与用户授权",
    endpoint: "/api/v1/admin/roles",
    permission: "roles.read",
    managePermission: "roles.manage",
    riskAction: {
      actionType: "role.permissions_change",
      targetType: "role",
      label: "变更角色权限",
      riskLevel: "critical",
    },
    columns: [
      { key: "name", label: "角色" },
      { key: "code", label: "代码" },
      { key: "is_system", label: "类型", render: (row) => row.is_system ? "系统角色" : "自定义角色" },
      { key: "permissions", label: "权限数", render: (row) => Array.isArray(row.permissions) ? row.permissions.length : 0, className: "admin-number" },
      { key: "description", label: "说明" },
      { key: "updated_at", label: "更新时间", render: (row) => dateTime(row.updated_at) },
    ],
  },
  audit: {
    title: "审计日志",
    eyebrow: "操作者、动作与请求链路",
    endpoint: "/api/v1/admin/audit-logs",
    permission: "audit.read",
    exportModule: "audit",
    filters: [
      {
        key: "result",
        label: "全部结果",
        options: [
          ["success", "成功"],
          ["denied", "拒绝"],
          ["failed", "失败"],
        ],
      },
    ],
    columns: [
      { key: "action", label: "动作" },
      { key: "actor_user_id", label: "操作者", render: (row) => shortId(row.actor_user_id) },
      { key: "target_type", label: "对象类型" },
      { key: "target_id", label: "对象", render: (row) => shortId(row.target_id) },
      { key: "result", label: "结果", render: (row) => <StatusValue value={row.result === "success" ? "succeeded" : "failed"} /> },
      { key: "request_id", label: "请求 ID", render: (row) => shortId(row.request_id) },
      { key: "occurred_at", label: "发生时间", render: (row) => dateTime(row.occurred_at) },
    ],
  },
};

function currentModule(): ModuleId {
  const segment = window.location.pathname.replace(/^\/admin\/?/, "").split("/")[0];
  return NAV_ITEMS.some((item) => item.id === segment) ? (segment as ModuleId) : "dashboard";
}

function AdminApp() {
  const [session, setSession] = useState<SessionInfo | null | undefined>(undefined);
  const [module, setModule] = useState<ModuleId>(currentModule);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [error, setError] = useState("");

  const loadSession = useCallback(async () => {
    setError("");
    try {
      setSession(await apiRequest<SessionInfo>("/api/v1/auth/me"));
    } catch (caught) {
      if (caught instanceof ApiRequestError && caught.status === 401) setSession(null);
      else {
        setSession(null);
        setError(caught instanceof Error ? caught.message : "无法读取登录状态");
      }
    }
  }, []);

  useEffect(() => {
    void loadSession();
  }, [loadSession]);

  useEffect(() => {
    const onPopState = () => setModule(currentModule());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const navigate = (next: ModuleId) => {
    const path = next === "dashboard" ? "/admin" : `/admin/${next}`;
    window.history.pushState({}, "", path);
    setModule(next);
    setSidebarOpen(false);
  };

  if (session === undefined) return <AdminBoot />;
  if (session === null) return <AdminLogin onSuccess={loadSession} error={error} />;

  const permissions = new Set(session.permissions);
  const visibleNavigation = NAV_ITEMS.filter((item) => permissions.has(item.permission));
  const currentNavigation = NAV_ITEMS.find((item) => item.id === module);
  const canView = Boolean(currentNavigation && permissions.has(currentNavigation.permission));

  return (
    <div className="admin-shell">
      <button
        className={`admin-sidebar-scrim ${sidebarOpen ? "is-open" : ""}`}
        aria-label="关闭导航"
        onClick={() => setSidebarOpen(false)}
      />
      <aside className={`admin-sidebar ${sidebarOpen ? "is-open" : ""}`}>
        <div className="admin-brand">
          <span className="admin-brand-mark"><Images size={19} /></span>
          <span><strong>Sub2Image</strong><small>管理后台</small></span>
        </div>
        <nav className="admin-nav" aria-label="后台导航">
          {visibleNavigation.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                className={module === item.id ? "is-active" : ""}
                onClick={() => navigate(item.id)}
              >
                <Icon size={17} />
                <span>{item.label}</span>
                {module === item.id && <ChevronRight size={15} className="admin-nav-arrow" />}
              </button>
            );
          })}
        </nav>
        <div className="admin-sidebar-user">
          <span className="admin-avatar">{session.user.display_name.slice(0, 1).toUpperCase()}</span>
          <span className="admin-user-copy">
            <strong>{session.user.display_name}</strong>
            <small>{session.user.roles?.join(" · ") || "管理员"}</small>
          </span>
          <button
            className="admin-icon-button"
            title="退出登录"
            aria-label="退出登录"
            onClick={async () => {
              await apiRequest<void>("/api/v1/auth/logout", { method: "POST" });
              setSession(null);
            }}
          >
            <LogOut size={17} />
          </button>
        </div>
      </aside>
      <div className="admin-workspace">
        <header className="admin-topbar">
          <button className="admin-menu-button" aria-label="打开导航" onClick={() => setSidebarOpen(true)}>
            <Menu size={20} />
          </button>
          <GlobalSearch onNavigate={(href) => {
            window.history.pushState({}, "", href);
            setModule(currentModule());
          }} />
          <a className="admin-studio-link" href="/">
            <Images size={16} />
            <span>返回工作台</span>
          </a>
        </header>
        <main className="admin-main">
          {canView ? (
            module === "dashboard" ? (
              <Dashboard />
            ) : module === "audit" ? (
              <AuditWorkspace
                definition={MODULES.audit}
                permissions={permissions}
                currentUserId={session.user.id}
              />
            ) : (
              <ModuleTable
                key={module}
                module={module}
                definition={MODULES[module]}
                permissions={permissions}
              />
            )
          ) : (
            <AccessDenied navigation={visibleNavigation} onNavigate={navigate} />
          )}
        </main>
      </div>
    </div>
  );
}

function AdminBoot() {
  return (
    <div className="admin-boot">
      <span className="admin-brand-mark"><Images size={21} /></span>
      <RefreshCw className="spin" size={18} />
    </div>
  );
}

function AdminLogin({ onSuccess, error: initialError }: { onSuccess: () => Promise<void>; error: string }) {
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(initialError);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      await apiRequest("/api/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ identifier, password }),
      });
      await onSuccess();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "登录失败");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="admin-login-page">
      <section className="admin-login-panel">
        <div className="admin-login-brand"><span className="admin-brand-mark"><Images size={21} /></span><span><strong>Sub2Image</strong><small>管理后台</small></span></div>
        <form onSubmit={submit}>
          <h1>管理员登录</h1>
          <label>邮箱或用户名<input autoFocus autoComplete="username" value={identifier} onChange={(event) => setIdentifier(event.target.value)} required /></label>
          <label>密码<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
          {error && <div className="admin-form-error"><CircleAlert size={15} />{error}</div>}
          <button className="admin-primary-button" disabled={submitting}>
            {submitting ? <RefreshCw className="spin" size={16} /> : <LogIn size={16} />}
            登录
          </button>
        </form>
        <a href="/">返回图片工作台</a>
      </section>
    </div>
  );
}

function AccessDenied({ navigation, onNavigate }: { navigation: NavItem[]; onNavigate: (module: ModuleId) => void }) {
  return (
    <section className="admin-empty-state">
      <ShieldCheck size={30} />
      <h1>当前账号无权访问此模块</h1>
      {navigation[0] && <button className="admin-secondary-button" onClick={() => onNavigate(navigation[0].id)}>打开可用模块</button>}
    </section>
  );
}

function GlobalSearch({ onNavigate }: { onNavigate: (href: string) => void }) {
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const close = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  useEffect(() => {
    if (query.trim().length < 2) {
      setItems([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    const timeout = window.setTimeout(() => {
      void apiRequest<{ items: SearchResult[] }>(`/api/v1/admin/search?q=${encodeURIComponent(query.trim())}`)
        .then((payload) => {
          setItems(payload.items);
          setOpen(true);
        })
        .catch(() => setItems([]))
        .finally(() => setLoading(false));
    }, 250);
    return () => window.clearTimeout(timeout);
  }, [query]);

  return (
    <div className="admin-global-search" ref={rootRef}>
      <Search size={17} />
      <input
        aria-label="全局搜索"
        placeholder="搜索用户、任务、资产…"
        value={query}
        onFocus={() => setOpen(true)}
        onChange={(event) => setQuery(event.target.value)}
      />
      {loading && <RefreshCw className="spin" size={15} />}
      {open && query.trim().length >= 2 && (
        <div className="admin-search-results">
          {items.length ? items.map((item) => (
            <button key={`${item.type}-${item.id}`} onClick={() => { onNavigate(item.href); setOpen(false); setQuery(""); }}>
              <span className="admin-result-icon"><Search size={15} /></span>
              <span><strong>{item.title}</strong><small>{item.type} · {item.subtitle}</small></span>
              <ChevronRight size={15} />
            </button>
          )) : !loading && <div className="admin-search-empty">没有匹配结果</div>}
        </div>
      )}
    </div>
  );
}

interface DashboardSummary {
  range: string;
  generated_at: string;
  users: { total: number; active: number; registrations: number; memberships: Record<string, number> };
  jobs: {
    total: number;
    success_rate: number | null;
    p50_duration_ms: number | null;
    p95_duration_ms: number | null;
    queue_length: number;
    by_status: Record<string, number>;
    failure_reasons: Array<{ code: string; count: number }>;
  };
  points: Record<string, number>;
  storage: {
    asset_count: number;
    bytes: number;
    new_asset_count: number;
    new_bytes: number;
    quarantined: number;
    deletion_failures: number;
  };
  sub2api: {
    requests: number;
    success_rate: number | null;
    responses_429: number;
    responses_5xx: number;
    estimated_points: number;
    configured: boolean;
  };
  system: { services: Record<string, number>; r2_configured: boolean; period_days: number };
}

interface TimeseriesPoint extends Row {
  date: string;
}

function percent(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(1)}%`;
}

function Dashboard() {
  const [range, setRange] = useState("7d");
  const [metric, setMetric] = useState("jobs");
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [series, setSeries] = useState<TimeseriesPoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    Promise.all([
      apiRequest<DashboardSummary>(`/api/v1/admin/dashboard/summary?range=${range}`),
      apiRequest<{ points: TimeseriesPoint[] }>(
        `/api/v1/admin/dashboard/timeseries?metric=${metric}&range=${range}`,
      ),
    ])
      .then(([nextSummary, nextSeries]) => {
        if (!active) return;
        setSummary(nextSummary);
        setSeries(nextSeries.points);
      })
      .catch((caught) => active && setError(caught instanceof Error ? caught.message : "总览加载失败"))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [metric, range, reload]);

  return (
    <div className="admin-page">
      <PageHeading eyebrow="运营实时概况" title="总览">
        <div className="admin-segmented" aria-label="统计周期">
          {(["24h", "7d", "30d", "90d"] as const).map((value) => (
            <button key={value} className={range === value ? "is-active" : ""} onClick={() => setRange(value)}>{value}</button>
          ))}
        </div>
        <button className="admin-icon-button bordered" title="刷新" aria-label="刷新" onClick={() => setReload((value) => value + 1)}>
          <RefreshCw className={loading ? "spin" : ""} size={17} />
        </button>
      </PageHeading>
      {error ? <LoadError message={error} onRetry={() => setReload((value) => value + 1)} /> : loading && !summary ? <TableSkeleton /> : summary && (
        <>
          <section className="admin-metrics" aria-label="关键指标">
            <MetricTile label="活跃用户" value={summary.users.active.toLocaleString()} detail={`${summary.users.registrations} 位新增注册`} icon={<Users size={18} />} />
            <MetricTile label="任务成功率" value={percent(summary.jobs.success_rate)} detail={`${summary.jobs.total} 个任务 · ${summary.jobs.queue_length} 个排队`} icon={<CheckCircle2 size={18} />} />
            <MetricTile label="积分净变动" value={Object.values(summary.points).reduce((total, item) => total + item, 0).toLocaleString()} detail={`${summary.points.consume ? Math.abs(summary.points.consume) : 0} 分消费`} icon={<Coins size={18} />} />
            <MetricTile label="存储占用" value={bytes(summary.storage.bytes)} detail={`${summary.storage.new_asset_count} 个新增资产`} icon={<Images size={18} />} />
          </section>
          <section className="admin-dashboard-grid">
            <div className="admin-panel admin-chart-panel">
              <div className="admin-panel-heading">
                <div><h2>业务趋势</h2><p>{range} 聚合</p></div>
                <div className="admin-segmented compact">
                  {[ ["jobs", "任务"], ["users", "用户"], ["points", "积分"], ["assets", "资产"] ].map(([value, label]) => (
                    <button key={value} className={metric === value ? "is-active" : ""} onClick={() => setMetric(value)}>{label}</button>
                  ))}
                </div>
              </div>
              <SeriesChart points={series} metric={metric} />
            </div>
            <div className="admin-panel admin-health-panel">
              <div className="admin-panel-heading"><div><h2>服务状态</h2><p>{dateTime(summary.generated_at)} 更新</p></div></div>
              <HealthRow label="Sub2API" ok={summary.sub2api.configured} detail={`${summary.sub2api.requests} 次请求 · ${percent(summary.sub2api.success_rate)}`} />
              <HealthRow label="Cloudflare R2" ok={summary.system.r2_configured} detail={`${summary.storage.quarantined} 个隔离 · ${summary.storage.deletion_failures} 个删除失败`} />
              <HealthRow label="Worker" ok={(summary.system.services.worker || 0) > 0} detail={`${summary.system.services.worker || 0} 个活跃实例`} />
              <HealthRow label="Scheduler" ok={(summary.system.services.scheduler || 0) > 0} detail={`${summary.system.services.scheduler || 0} 个活跃实例`} />
            </div>
          </section>
          <section className="admin-dashboard-grid lower">
            <div className="admin-panel">
              <div className="admin-panel-heading"><div><h2>任务状态</h2><p>P50 {summary.jobs.p50_duration_ms ?? "—"} ms · P95 {summary.jobs.p95_duration_ms ?? "—"} ms</p></div></div>
              <div className="admin-stat-list">
                {Object.entries(summary.jobs.by_status).length ? Object.entries(summary.jobs.by_status).map(([key, value]) => <div key={key}><StatusValue value={key} /><strong>{value.toLocaleString()}</strong></div>) : <SmallEmpty />}
              </div>
            </div>
            <div className="admin-panel">
              <div className="admin-panel-heading"><div><h2>失败原因</h2><p>任务错误 Top 5</p></div></div>
              <div className="admin-stat-list">
                {summary.jobs.failure_reasons.length ? summary.jobs.failure_reasons.map((item) => <div key={item.code}><span>{item.code}</span><strong>{item.count.toLocaleString()}</strong></div>) : <SmallEmpty />}
              </div>
            </div>
          </section>
        </>
      )}
    </div>
  );
}

function PageHeading({ eyebrow, title, children }: { eyebrow: string; title: string; children?: ReactNode }) {
  return (
    <header className="admin-page-heading">
      <div><p>{eyebrow}</p><h1>{title}</h1></div>
      <div className="admin-heading-actions">{children}</div>
    </header>
  );
}

function MetricTile({ label, value, detail, icon }: { label: string; value: string; detail: string; icon: ReactNode }) {
  return <div className="admin-metric"><span className="admin-metric-icon">{icon}</span><div><span>{label}</span><strong>{value}</strong><small>{detail}</small></div></div>;
}

function HealthRow({ label, ok, detail }: { label: string; ok: boolean; detail: string }) {
  return <div className="admin-health-row"><span className={ok ? "health-dot ok" : "health-dot off"}>{ok ? <Check size={12} /> : <X size={12} />}</span><div><strong>{label}</strong><small>{detail}</small></div><StatusValue value={ok ? "active" : "pending"} /></div>;
}

function SeriesChart({ points, metric }: { points: TimeseriesPoint[]; metric: string }) {
  const valueFor = (point: TimeseriesPoint) => {
    if (metric === "jobs") return Number(point.total || 0);
    if (metric === "users") return Number(point.registrations || 0);
    if (metric === "assets") return Number(point.count || 0);
    return Math.abs(Object.entries(point).filter(([key]) => key !== "date").reduce((total, [, value]) => total + Number(value || 0), 0));
  };
  const maximum = Math.max(1, ...points.map(valueFor));
  const displayed = points.length > 35 ? points.filter((_, index) => index % 3 === 0 || index === points.length - 1) : points;
  return (
    <div className="admin-chart">
      {displayed.map((point) => {
        const value = valueFor(point);
        return <div className="admin-chart-column" key={point.date} title={`${point.date}: ${value}`}><span className="admin-chart-value">{value || ""}</span><span className="admin-chart-bar" style={{ height: `${Math.max(value ? 8 : 2, (value / maximum) * 100)}%` }} /><small>{point.date.slice(5)}</small></div>;
      })}
    </div>
  );
}

function SmallEmpty() {
  return <div className="admin-small-empty">当前周期暂无记录</div>;
}

function LoadError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return <section className="admin-load-error"><CircleAlert size={23} /><div><strong>数据加载失败</strong><p>{message}</p></div><button className="admin-secondary-button" onClick={onRetry}><RefreshCw size={15} />重试</button></section>;
}

function TableSkeleton() {
  return <div className="admin-table-skeleton">{Array.from({ length: 7 }, (_, index) => <span key={index} />)}</div>;
}

function ModuleTable({ module, definition, permissions, embedded = false }: { module: Exclude<ModuleId, "dashboard">; definition: ModuleDefinition; permissions: Set<string>; embedded?: boolean }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [query, setQuery] = useState("");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [cursor, setCursor] = useState<string | null>(null);
  const [cursorHistory, setCursorHistory] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<Row | null>(null);
  const [riskOpen, setRiskOpen] = useState(false);
  const [reload, setReload] = useState(0);
  const [notice, setNotice] = useState("");
  const [savedViews, setSavedViews] = useState<Row[]>([]);
  const [saveOpen, setSaveOpen] = useState(false);
  const [viewName, setViewName] = useState("");
  const paginated = !new Set(["memberships", "pricing", "settings", "roles"]).has(module);

  const loadRows = useCallback(async () => {
    setLoading(true);
    setError("");
    const params = new URLSearchParams();
    if (paginated) {
      params.set("limit", "25");
      params.set("order", order);
    }
    if (module === "users") {
      params.set("offset", String(cursorHistory.length * 25));
      if (query.trim()) params.set("query", query.trim());
    } else if (cursor) params.set("cursor", cursor);
    Object.entries(filters).forEach(([key, value]) => value && params.set(key, value));
    const url = `${definition.endpoint}${params.size ? `?${params}` : ""}`;
    try {
      const payload = await apiRequest<PageResponse>(url);
      setRows(payload.items || []);
      setNextCursor(payload.next_cursor || null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "列表加载失败");
    } finally {
      setLoading(false);
    }
  }, [cursor, cursorHistory.length, definition.endpoint, filters, module, order, paginated, query]);

  useEffect(() => {
    void loadRows();
  }, [loadRows, reload]);

  useEffect(() => {
    void apiRequest<PageResponse>(`/api/v1/admin/saved-views?module=${module}`)
      .then((payload) => setSavedViews(payload.items || []))
      .catch(() => setSavedViews([]));
  }, [module, reload]);

  const canNext = module === "users" ? rows.length === 25 : Boolean(nextCursor);
  const canExport = Boolean(definition.exportModule && permissions.has("audit.export"));
  const canManage = Boolean(definition.managePermission && permissions.has(definition.managePermission));

  const exportRows = async () => {
    if (!definition.exportModule) return;
    try {
      const payload = await apiRequest<{ download_url: string }>(`/api/v1/admin/export/${definition.exportModule}`);
      const link = document.createElement("a");
      link.href = payload.download_url;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setNotice("导出文件已生成");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "导出失败");
    }
  };

  const saveView = async () => {
    if (!viewName.trim()) return;
    try {
      await apiRequest("/api/v1/admin/saved-views", {
        method: "POST",
        body: JSON.stringify({
          module,
          name: viewName.trim(),
          filters: { ...filters, order, ...(query.trim() ? { query: query.trim() } : {}) },
          columns: definition.columns.map((column) => column.key),
        }),
      });
      setSaveOpen(false);
      setViewName("");
      setNotice("视图已保存");
      setReload((value) => value + 1);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "保存视图失败");
    }
  };

  const applySavedView = (id: string) => {
    const view = savedViews.find((item) => String(item.id) === id);
    if (!view || !view.filters || typeof view.filters !== "object") return;
    const next = view.filters as Record<string, unknown>;
    const nextFilters: Record<string, string> = {};
    definition.filters?.forEach(({ key }) => {
      if (next[key]) nextFilters[key] = String(next[key]);
    });
    setFilters(nextFilters);
    setQuery(typeof next.query === "string" ? next.query : "");
    setOrder(next.order === "asc" ? "asc" : "desc");
    setCursor(null);
    setCursorHistory([]);
  };

  return (
    <div className={`admin-page ${embedded ? "is-embedded" : ""}`}>
      {!embedded && <PageHeading eyebrow={definition.eyebrow} title={definition.title}>
        {notice && <span className="admin-notice"><Check size={14} />{notice}</span>}
        {canExport && <button className="admin-secondary-button" onClick={() => void exportRows()}><Download size={15} />导出 CSV</button>}
        <button className="admin-icon-button bordered" title="刷新" aria-label="刷新" onClick={() => setReload((value) => value + 1)}><RefreshCw className={loading ? "spin" : ""} size={17} /></button>
      </PageHeading>}
      <section className="admin-list-panel">
        <div className="admin-list-toolbar">
          <div className="admin-filter-group">
            {module === "users" && <label className="admin-inline-search"><Search size={15} /><input placeholder="搜索邮箱、用户名…" value={query} onChange={(event) => { setQuery(event.target.value); setCursor(null); setCursorHistory([]); }} /></label>}
            {definition.filters?.map((filter) => (
              <label className="admin-select-wrap" key={filter.key}>
                <ListFilter size={14} />
                <select value={filters[filter.key] || ""} onChange={(event) => { setFilters((current) => ({ ...current, [filter.key]: event.target.value })); setCursor(null); setCursorHistory([]); }}>
                  <option value="">{filter.label}</option>
                  {filter.options.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
                </select>
                <ChevronDown size={13} />
              </label>
            ))}
            {paginated && <label className="admin-select-wrap"><Clock3 size={14} /><select value={order} onChange={(event) => { setOrder(event.target.value as "asc" | "desc"); setCursor(null); setCursorHistory([]); }}><option value="desc">最新优先</option><option value="asc">最早优先</option></select><ChevronDown size={13} /></label>}
          </div>
          <div className="admin-view-tools">
            {savedViews.length > 0 && <label className="admin-select-wrap plain"><Eye size={14} /><select defaultValue="" onChange={(event) => applySavedView(event.target.value)}><option value="" disabled>保存的视图</option>{savedViews.map((view) => <option value={String(view.id)} key={String(view.id)}>{String(view.name)}</option>)}</select><ChevronDown size={13} /></label>}
            <button className="admin-icon-button bordered" title="保存当前视图" aria-label="保存当前视图" onClick={() => setSaveOpen((value) => !value)}><Save size={16} /></button>
          </div>
          {saveOpen && <div className="admin-save-view-popover"><input autoFocus placeholder="视图名称" value={viewName} onChange={(event) => setViewName(event.target.value)} onKeyDown={(event) => event.key === "Enter" && void saveView()} /><button className="admin-primary-button compact" onClick={() => void saveView()}><Check size={14} />保存</button></div>}
        </div>
        {error ? <LoadError message={error} onRetry={() => setReload((value) => value + 1)} /> : loading ? <TableSkeleton /> : (
          <DataTable rows={rows} columns={definition.columns} onSelect={setSelected} />
        )}
        {paginated && !error && <div className="admin-pagination"><span>第 {cursorHistory.length + 1} 页</span><div><button className="admin-icon-button bordered" aria-label="上一页" title="上一页" disabled={!cursorHistory.length} onClick={() => { const history = [...cursorHistory]; const previous = history.pop() ?? null; setCursor(previous); setCursorHistory(history); }}><ArrowLeft size={16} /></button><button className="admin-icon-button bordered" aria-label="下一页" title="下一页" disabled={!canNext} onClick={() => { setCursorHistory((history) => [...history, cursor]); setCursor(module === "users" ? `offset-${cursorHistory.length + 1}` : nextCursor); }}><ArrowRight size={16} /></button></div></div>}
      </section>
      {selected && <DetailDrawer row={selected} module={module} permissions={permissions} onClose={() => setSelected(null)} onRisk={canManage && definition.riskAction ? () => setRiskOpen(true) : undefined} riskLabel={definition.riskAction?.label} />}
      {riskOpen && selected && definition.riskAction && <RiskDialog row={selected} action={definition.riskAction} onClose={() => setRiskOpen(false)} onCreated={() => { setRiskOpen(false); setNotice("高风险操作申请已提交"); }} />}
    </div>
  );
}

function DataTable({ rows, columns, onSelect }: { rows: Row[]; columns: Column[]; onSelect: (row: Row) => void }) {
  if (!rows.length) return <div className="admin-table-empty"><Search size={24} /><strong>没有符合条件的记录</strong></div>;
  return (
    <div className="admin-table-scroll">
      <table className="admin-data-table">
        <thead><tr>{columns.map((column) => <th key={column.key} className={column.className}>{column.label}</th>)}<th className="admin-row-action"><span className="sr-only">详情</span></th></tr></thead>
        <tbody>{rows.map((row, index) => <tr key={String(row.id || index)} onClick={() => onSelect(row)}>{columns.map((column) => <td key={column.key} className={column.className}>{column.render ? column.render(row) : valueText(nested(row, column.key))}</td>)}<td className="admin-row-action"><button className="admin-icon-button" aria-label="查看详情" title="查看详情"><ChevronRight size={15} /></button></td></tr>)}</tbody>
      </table>
    </div>
  );
}

const FIELD_LABELS: Record<string, string> = {
  id: "ID",
  user_id: "用户 ID",
  owner_id: "所有者",
  display_name: "显示名称",
  email: "邮箱",
  username: "用户名",
  status: "状态",
  roles: "角色",
  code: "代码",
  name: "名称",
  description: "说明",
  operation_code: "操作",
  target_type: "对象类型",
  target_id: "对象 ID",
  action: "动作",
  action_type: "操作类型",
  risk_level: "风险等级",
  requested_by: "申请人",
  approved_by: "审批人",
  request_id: "请求 ID",
  created_at: "创建时间",
  updated_at: "更新时间",
  completed_at: "完成时间",
  retention_until: "保留至",
  reason: "原因",
  details: "详情",
};

function DetailDrawer({ row, module, permissions, onClose, onRisk, riskLabel }: { row: Row; module: Exclude<ModuleId, "dashboard">; permissions: Set<string>; onClose: () => void; onRisk?: () => void; riskLabel?: string }) {
  return (
    <div className="admin-drawer-layer" role="presentation">
      <button className="admin-drawer-scrim" aria-label="关闭详情" onClick={onClose} />
      <aside className="admin-detail-drawer" aria-label="记录详情">
        <header><div><p>{MODULES[module].title}详情</p><h2>{valueText(row.display_name || row.name || row.action || row.operation_code || shortId(row.id))}</h2></div><button className="admin-icon-button" aria-label="关闭详情" title="关闭" onClick={onClose}><X size={18} /></button></header>
        <div className="admin-drawer-content">
          <section className="admin-detail-fields">
            {Object.entries(row).map(([key, value]) => (
              <div key={key} className={typeof value === "object" && value !== null ? "is-wide" : ""}>
                <dt>{FIELD_LABELS[key] || key.replaceAll("_", " ")}</dt>
                <dd>{key.endsWith("_at") ? dateTime(value) : key === "status" || key === "result" ? <StatusValue value={value} /> : <ValueBlock value={value} />}</dd>
              </div>
            ))}
          </section>
          {module === "users" && typeof row.id === "string" && <UserRelations userId={row.id} permissions={permissions} />}
        </div>
        {onRisk && riskLabel && <footer><button className="admin-danger-button" onClick={onRisk}><CircleAlert size={15} />提交“{riskLabel}”申请</button></footer>}
      </aside>
    </div>
  );
}

function ValueBlock({ value }: { value: unknown }) {
  if (value && typeof value === "object") return <pre>{JSON.stringify(value, null, 2)}</pre>;
  return <>{valueText(value)}</>;
}

interface TimelineItem {
  id: string;
  label: string;
  meta: string;
  created_at: string;
}

function UserRelations({ userId, permissions }: { userId: string; permissions: Set<string> }) {
  const [items, setItems] = useState<TimelineItem[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const requests: Array<Promise<{ type: string; payload: Row }>> = [];
    if (permissions.has("memberships.read")) requests.push(apiRequest<Row>(`/api/v1/admin/users/${userId}/memberships`).then((payload) => ({ type: "会员", payload })));
    if (permissions.has("points.read")) requests.push(apiRequest<Row>(`/api/v1/admin/users/${userId}/points?limit=8`).then((payload) => ({ type: "积分", payload })));
    if (permissions.has("tasks.read")) requests.push(apiRequest<Row>(`/api/v1/admin/jobs?user_id=${userId}&limit=8`).then((payload) => ({ type: "任务", payload })));
    if (permissions.has("assets.read")) requests.push(apiRequest<Row>(`/api/v1/admin/assets?owner_id=${userId}&limit=8`).then((payload) => ({ type: "资产", payload })));
    Promise.allSettled(requests).then((results) => {
      const timeline: TimelineItem[] = [];
      results.forEach((result) => {
        if (result.status !== "fulfilled") return;
        const { type, payload } = result.value;
        const records = Array.isArray(payload.items)
          ? payload.items
          : Array.isArray(payload.transactions)
            ? payload.transactions
            : [];
        records.forEach((record, index) => {
          if (!record || typeof record !== "object") return;
          const item = record as Row;
          timeline.push({
            id: String(item.id || `${type}-${index}`),
            label: `${type} · ${valueText(item.operation_code || item.entry_type || nested(item, "plan.name") || item.kind || item.status)}`,
            meta: valueText(item.status || item.delta || item.description),
            created_at: String(item.created_at || item.starts_at || ""),
          });
        });
      });
      timeline.sort((left, right) => new Date(right.created_at).getTime() - new Date(left.created_at).getTime());
      setItems(timeline.slice(0, 16));
      setLoading(false);
    });
  }, [permissions, userId]);

  return (
    <section className="admin-related-section">
      <div className="admin-panel-heading"><div><h3>关联时间线</h3><p>会员、任务、积分与资产</p></div></div>
      {loading ? <RefreshCw className="spin" size={16} /> : items.length ? <ol className="admin-timeline">{items.map((item) => <li key={`${item.label}-${item.id}`}><span /><div><strong>{item.label}</strong><small>{item.meta}</small></div><time>{dateTime(item.created_at)}</time></li>)}</ol> : <SmallEmpty />}
    </section>
  );
}

function RiskDialog({ row, action, onClose, onCreated }: { row: Row; action: NonNullable<ModuleDefinition["riskAction"]>; onClose: () => void; onCreated: () => void }) {
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const targetId = String(row.id || "");

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      await apiRequest("/api/v1/admin/action-requests", {
        method: "POST",
        body: JSON.stringify({
          action_type: action.actionType,
          target_type: action.targetType,
          target_id: targetId,
          payload: {},
          reason,
          risk_level: action.riskLevel,
          confirmed,
        }),
      });
      onCreated();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "申请提交失败");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="admin-modal-layer">
      <button className="admin-modal-scrim" aria-label="关闭对话框" onClick={onClose} />
      <form className="admin-confirm-dialog" onSubmit={submit}>
        <header><span className="admin-risk-icon"><CircleAlert size={20} /></span><div><p>{action.riskLevel === "critical" ? "关键风险操作" : "高风险操作"}</p><h2>{action.label}</h2></div><button type="button" className="admin-icon-button" aria-label="关闭" onClick={onClose}><X size={18} /></button></header>
        <div className="admin-confirm-target"><span>操作对象</span><code>{targetId}</code></div>
        <label>操作原因<textarea autoFocus value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} required minLength={3} /></label>
        <label className="admin-confirm-check"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /><span><Check size={13} /></span><strong>确认提交“{action.label}”审批申请</strong></label>
        {error && <div className="admin-form-error"><CircleAlert size={15} />{error}</div>}
        <footer><button type="button" className="admin-secondary-button" onClick={onClose}>取消</button><button className="admin-danger-button" disabled={!confirmed || reason.trim().length < 3 || submitting}>{submitting ? <RefreshCw className="spin" size={15} /> : <CircleAlert size={15} />}提交{action.label}申请</button></footer>
      </form>
    </div>
  );
}

function AuditWorkspace({ definition, permissions, currentUserId }: { definition: ModuleDefinition; permissions: Set<string>; currentUserId: string }) {
  type AuditTab = "logs" | "events" | "blocks" | "limits" | "requests";
  const [tab, setTab] = useState<AuditTab>("logs");
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState("");
  const canSeeSecurity = permissions.has("security.events.read");
  const exportAudit = async () => {
    setExporting(true);
    setError("");
    try {
      const response = await fetch("/api/v1/admin/audit-logs/export", { method: "POST" });
      if (!response.ok) throw new Error("审计导出失败");
      const href = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = href;
      link.download = "audit.csv";
      link.click();
      URL.revokeObjectURL(href);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "审计导出失败");
    } finally {
      setExporting(false);
    }
  };
  return (
    <div className="admin-page">
      <PageHeading eyebrow={definition.eyebrow} title={definition.title}>
        {tab === "logs" && permissions.has("audit.export") && <button className="admin-secondary-button" disabled={exporting} onClick={() => void exportAudit()}>{exporting ? <RefreshCw className="spin" size={15} /> : <Download size={15} />}导出审计</button>}
        <div className="admin-tabs admin-audit-tabs">
          <button className={tab === "logs" ? "is-active" : ""} onClick={() => setTab("logs")}>审计记录</button>
          {canSeeSecurity && <button className={tab === "events" ? "is-active" : ""} onClick={() => setTab("events")}>安全事件</button>}
          {canSeeSecurity && <button className={tab === "blocks" ? "is-active" : ""} onClick={() => setTab("blocks")}>访问封禁</button>}
          {canSeeSecurity && <button className={tab === "limits" ? "is-active" : ""} onClick={() => setTab("limits")}>限流策略</button>}
          <button className={tab === "requests" ? "is-active" : ""} onClick={() => setTab("requests")}>操作申请</button>
        </div>
      </PageHeading>
      {error && <div className="admin-form-error"><CircleAlert size={15} />{error}</div>}
      {tab === "logs" && <ModuleTable module="audit" definition={definition} permissions={permissions} embedded />}
      {tab === "events" && <SecurityEventsPanel canManage={permissions.has("security.policies.manage")} />}
      {tab === "blocks" && <SecurityBlocksPanel canManage={permissions.has("security.policies.manage")} />}
      {tab === "limits" && <RateLimitsPanel canManage={permissions.has("security.policies.manage")} />}
      {tab === "requests" && <ActionRequestsPanel permissions={permissions} currentUserId={currentUserId} />}
    </div>
  );
}

const SECURITY_EVENT_COLUMNS: Column[] = [
  { key: "event_type", label: "事件" },
  { key: "severity", label: "级别", render: (row) => <RiskValue value={row.severity} /> },
  { key: "user_id", label: "用户", render: (row) => shortId(row.user_id) },
  { key: "status", label: "状态", render: (row) => <StatusValue value={row.status} /> },
  { key: "request_id", label: "请求 ID", render: (row) => shortId(row.request_id) },
  { key: "created_at", label: "发生时间", render: (row) => dateTime(row.created_at) },
];

function SecurityEventsPanel({ canManage }: { canManage: boolean }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [status, setStatus] = useState("open");
  const [severity, setSeverity] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<Row | null>(null);
  const [resolving, setResolving] = useState<Row | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const params = new URLSearchParams({ limit: "100" });
    if (status) params.set("status", status);
    if (severity) params.set("severity", severity);
    setLoading(true);
    setError("");
    void apiRequest<PageResponse>(`/api/v1/admin/security/events?${params}`)
      .then((payload) => setRows(payload.items || []))
      .catch((caught) => setError(caught instanceof Error ? caught.message : "安全事件加载失败"))
      .finally(() => setLoading(false));
  }, [reload, severity, status]);

  return <section className="admin-list-panel">
    <div className="admin-list-toolbar"><div className="admin-filter-group">
      <label className="admin-select-wrap"><ShieldAlert size={14} /><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="">全部状态</option><option value="open">待处置</option><option value="investigating">调查中</option><option value="resolved">已解决</option><option value="ignored">已忽略</option></select><ChevronDown size={13} /></label>
      <label className="admin-select-wrap"><ListFilter size={14} /><select value={severity} onChange={(event) => setSeverity(event.target.value)}><option value="">全部级别</option><option value="critical">严重</option><option value="high">高</option><option value="medium">中</option><option value="low">低</option></select><ChevronDown size={13} /></label>
    </div><button className="admin-icon-button bordered" title="刷新" aria-label="刷新" onClick={() => setReload((value) => value + 1)}><RefreshCw className={loading ? "spin" : ""} size={16} /></button></div>
    {error ? <LoadError message={error} onRetry={() => setReload((value) => value + 1)} /> : loading ? <TableSkeleton /> : rows.length ? <div className="admin-table-scroll"><table className="admin-data-table"><thead><tr>{SECURITY_EVENT_COLUMNS.map((column) => <th key={column.key}>{column.label}</th>)}<th>处置</th></tr></thead><tbody>{rows.map((row) => <tr key={String(row.id)} onClick={() => setSelected(row)}>{SECURITY_EVENT_COLUMNS.map((column) => <td key={column.key}>{column.render ? column.render(row) : valueText(nested(row, column.key))}</td>)}<td><div className="admin-row-buttons">{canManage && row.status !== "resolved" && row.status !== "ignored" ? <button className="admin-icon-button approve" title="处置事件" aria-label="处置事件" onClick={(event) => { event.stopPropagation(); setResolving(row); }}><Check size={15} /></button> : <ChevronRight size={15} />}</div></td></tr>)}</tbody></table></div> : <div className="admin-table-empty"><ShieldCheck size={24} /><strong>没有符合条件的安全事件</strong></div>}
    {selected && <RecordDetailDrawer eyebrow="安全事件" row={selected} onClose={() => setSelected(null)} />}
    {resolving && <SecurityResolveDialog row={resolving} onClose={() => setResolving(null)} onDone={() => { setResolving(null); setSelected(null); setReload((value) => value + 1); }} />}
  </section>;
}

function SecurityResolveDialog({ row, onClose, onDone }: { row: Row; onClose: () => void; onDone: () => void }) {
  const [resolution, setResolution] = useState<"resolved" | "ignored">("resolved");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setSubmitting(true); setError("");
    try { await apiRequest(`/api/v1/admin/security/events/${row.id}/resolve`, { method: "POST", body: JSON.stringify({ status: resolution, reason }) }); onDone(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "事件处置失败"); }
    finally { setSubmitting(false); }
  };
  return <div className="admin-modal-layer"><button className="admin-modal-scrim" aria-label="关闭" onClick={onClose} /><form className="admin-confirm-dialog" onSubmit={submit}><header><span className="admin-risk-icon approve"><ShieldCheck size={20} /></span><div><p>安全事件处置</p><h2>{valueText(row.event_type)}</h2></div><button type="button" className="admin-icon-button" aria-label="关闭" onClick={onClose}><X size={18} /></button></header><label>处置结果<select value={resolution} onChange={(event) => setResolution(event.target.value as "resolved" | "ignored")}><option value="resolved">已解决</option><option value="ignored">确认忽略</option></select></label><label>处置原因<textarea autoFocus minLength={3} maxLength={500} required value={reason} onChange={(event) => setReason(event.target.value)} /></label>{error && <div className="admin-form-error"><CircleAlert size={15} />{error}</div>}<footer><button type="button" className="admin-secondary-button" onClick={onClose}>取消</button><button className="admin-primary-button" disabled={submitting || reason.trim().length < 3}>{submitting ? <RefreshCw className="spin" size={15} /> : <Check size={15} />}确认处置</button></footer></form></div>;
}

function SecurityBlocksPanel({ canManage }: { canManage: boolean }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [active, setActive] = useState("true");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<Row | null>(null);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    setLoading(true); setError("");
    void apiRequest<PageResponse>(`/api/v1/admin/security/blocks?limit=100${active ? `&active=${active}` : ""}`)
      .then((payload) => setRows(payload.items || []))
      .catch((caught) => setError(caught instanceof Error ? caught.message : "访问封禁加载失败"))
      .finally(() => setLoading(false));
  }, [active, reload]);
  return <section className="admin-list-panel"><div className="admin-list-toolbar"><div className="admin-filter-group"><label className="admin-select-wrap"><Ban size={14} /><select value={active} onChange={(event) => setActive(event.target.value)}><option value="">全部封禁</option><option value="true">生效中</option><option value="false">已失效</option></select><ChevronDown size={13} /></label></div><div className="admin-heading-actions">{canManage && <button className="admin-primary-button compact" onClick={() => setCreating(true)}><Plus size={15} />新增封禁</button>}<button className="admin-icon-button bordered" title="刷新" aria-label="刷新" onClick={() => setReload((value) => value + 1)}><RefreshCw className={loading ? "spin" : ""} size={16} /></button></div></div>
    {error ? <LoadError message={error} onRetry={() => setReload((value) => value + 1)} /> : loading ? <TableSkeleton /> : rows.length ? <div className="admin-table-scroll"><table className="admin-data-table"><thead><tr><th>主体</th><th>原因</th><th>状态</th><th>开始时间</th><th>结束时间</th><th>操作</th></tr></thead><tbody>{rows.map((row) => <tr key={String(row.id)}><td><code>{valueText(row.subject_label)}</code></td><td>{valueText(row.reason)}</td><td><StatusValue value={row.active ? "active" : "resolved"} /></td><td>{dateTime(row.starts_at)}</td><td>{dateTime(row.ends_at)}</td><td>{canManage && row.active ? <button className="admin-icon-button reject" title="撤销封禁" aria-label="撤销封禁" onClick={() => setRevoking(row)}><X size={15} /></button> : "—"}</td></tr>)}</tbody></table></div> : <div className="admin-table-empty"><Ban size={24} /><strong>没有符合条件的封禁记录</strong></div>}
    {creating && <CreateBlockDialog onClose={() => setCreating(false)} onDone={() => { setCreating(false); setReload((value) => value + 1); }} />}
    {revoking && <ReasonDialog title="撤销访问封禁" actionLabel="确认撤销" endpoint={`/api/v1/admin/security/blocks/${revoking.id}/revoke`} onClose={() => setRevoking(null)} onDone={() => { setRevoking(null); setReload((value) => value + 1); }} />}
  </section>;
}

function CreateBlockDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [subjectType, setSubjectType] = useState<"user" | "ip_fingerprint">("user");
  const [subject, setSubject] = useState("");
  const [reason, setReason] = useState("");
  const [hours, setHours] = useState("24");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const submit = async (event: FormEvent) => { event.preventDefault(); setSubmitting(true); setError(""); try { const duration = Number(hours); await apiRequest("/api/v1/admin/security/blocks", { method: "POST", body: JSON.stringify({ subject_type: subjectType, subject, reason, ends_at: duration > 0 ? new Date(Date.now() + duration * 3600000).toISOString() : null }) }); onDone(); } catch (caught) { setError(caught instanceof Error ? caught.message : "封禁创建失败"); } finally { setSubmitting(false); } };
  return <div className="admin-modal-layer"><button className="admin-modal-scrim" aria-label="关闭" onClick={onClose} /><form className="admin-confirm-dialog" onSubmit={submit}><header><span className="admin-risk-icon"><Ban size={20} /></span><div><p>访问策略</p><h2>新增封禁</h2></div><button type="button" className="admin-icon-button" aria-label="关闭" onClick={onClose}><X size={18} /></button></header><label>主体类型<select value={subjectType} onChange={(event) => setSubjectType(event.target.value as "user" | "ip_fingerprint")}><option value="user">用户 ID</option><option value="ip_fingerprint">IP 指纹</option></select></label><label>{subjectType === "user" ? "用户 ID" : "64 位 IP 哈希"}<input value={subject} onChange={(event) => setSubject(event.target.value)} required /></label><label>持续时间<select value={hours} onChange={(event) => setHours(event.target.value)}><option value="1">1 小时</option><option value="24">24 小时</option><option value="168">7 天</option><option value="720">30 天</option><option value="0">永久</option></select></label><label>封禁原因<textarea minLength={3} maxLength={500} required value={reason} onChange={(event) => setReason(event.target.value)} /></label>{error && <div className="admin-form-error"><CircleAlert size={15} />{error}</div>}<footer><button type="button" className="admin-secondary-button" onClick={onClose}>取消</button><button className="admin-danger-button" disabled={submitting || !subject.trim() || reason.trim().length < 3}>{submitting ? <RefreshCw className="spin" size={15} /> : <Ban size={15} />}创建封禁</button></footer></form></div>;
}

function RateLimitsPanel({ canManage }: { canManage: boolean }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<Row | null>(null);
  const [reload, setReload] = useState(0);
  useEffect(() => { setLoading(true); setError(""); void apiRequest<PageResponse>("/api/v1/admin/security/rate-limits").then((payload) => setRows(payload.items || [])).catch((caught) => setError(caught instanceof Error ? caught.message : "限流策略加载失败")).finally(() => setLoading(false)); }, [reload]);
  return <section className="admin-list-panel"><div className="admin-list-toolbar"><div className="admin-filter-group"><span className="admin-toolbar-label"><SlidersHorizontal size={14} />分层限流策略</span></div><button className="admin-icon-button bordered" title="刷新" aria-label="刷新" onClick={() => setReload((value) => value + 1)}><RefreshCw className={loading ? "spin" : ""} size={16} /></button></div>{error ? <LoadError message={error} onRetry={() => setReload((value) => value + 1)} /> : loading ? <TableSkeleton /> : <div className="admin-table-scroll"><table className="admin-data-table"><thead><tr><th>策略</th><th>维度</th><th>阈值</th><th>窗口</th><th>当前计数</th><th>状态</th><th>操作</th></tr></thead><tbody>{rows.map((row) => <tr key={String(row.code)}><td><strong>{valueText(row.name)}</strong><small className="admin-cell-note">{valueText(row.code)}</small></td><td>{valueText(row.scope)}</td><td className="admin-number">{valueText(row.request_limit)}</td><td>{valueText(row.window_seconds)} 秒</td><td className="admin-number">{valueText(row.current_local_count)}</td><td><StatusValue value={row.enabled ? "active" : "disabled"} /></td><td>{canManage ? <button className="admin-icon-button bordered" title="编辑策略" aria-label="编辑策略" onClick={() => setEditing(row)}><Settings size={15} /></button> : "—"}</td></tr>)}</tbody></table></div>}{editing && <RateLimitDialog row={editing} onClose={() => setEditing(null)} onDone={() => { setEditing(null); setReload((value) => value + 1); }} />}</section>;
}

function RateLimitDialog({ row, onClose, onDone }: { row: Row; onClose: () => void; onDone: () => void }) {
  const [limit, setLimit] = useState(String(row.request_limit));
  const [windowSeconds, setWindowSeconds] = useState(String(row.window_seconds));
  const [enabled, setEnabled] = useState(Boolean(row.enabled));
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const submit = async (event: FormEvent) => { event.preventDefault(); setSubmitting(true); setError(""); try { await apiRequest(`/api/v1/admin/security/rate-limits/${row.code}`, { method: "PATCH", body: JSON.stringify({ request_limit: Number(limit), window_seconds: Number(windowSeconds), enabled, reason }) }); onDone(); } catch (caught) { setError(caught instanceof Error ? caught.message : "策略更新失败"); } finally { setSubmitting(false); } };
  return <div className="admin-modal-layer"><button className="admin-modal-scrim" aria-label="关闭" onClick={onClose} /><form className="admin-confirm-dialog" onSubmit={submit}><header><span className="admin-risk-icon approve"><SlidersHorizontal size={20} /></span><div><p>限流策略</p><h2>{valueText(row.name)}</h2></div><button type="button" className="admin-icon-button" aria-label="关闭" onClick={onClose}><X size={18} /></button></header><label>请求阈值<input type="number" min="1" max="1000000" value={limit} onChange={(event) => setLimit(event.target.value)} required /></label><label>时间窗口（秒）<input type="number" min="1" max="86400" value={windowSeconds} onChange={(event) => setWindowSeconds(event.target.value)} required /></label><label className="admin-confirm-check"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} /><span><Check size={13} /></span><strong>启用该策略</strong></label><label>变更原因<textarea minLength={3} maxLength={500} required value={reason} onChange={(event) => setReason(event.target.value)} /></label>{error && <div className="admin-form-error"><CircleAlert size={15} />{error}</div>}<footer><button type="button" className="admin-secondary-button" onClick={onClose}>取消</button><button className="admin-primary-button" disabled={submitting || reason.trim().length < 3}>{submitting ? <RefreshCw className="spin" size={15} /> : <Save size={15} />}保存策略</button></footer></form></div>;
}

function ReasonDialog({ title, actionLabel, endpoint, onClose, onDone }: { title: string; actionLabel: string; endpoint: string; onClose: () => void; onDone: () => void }) {
  const [reason, setReason] = useState(""); const [error, setError] = useState(""); const [submitting, setSubmitting] = useState(false);
  const submit = async (event: FormEvent) => { event.preventDefault(); setSubmitting(true); setError(""); try { await apiRequest(endpoint, { method: "POST", body: JSON.stringify({ reason }) }); onDone(); } catch (caught) { setError(caught instanceof Error ? caught.message : "操作失败"); } finally { setSubmitting(false); } };
  return <div className="admin-modal-layer"><button className="admin-modal-scrim" aria-label="关闭" onClick={onClose} /><form className="admin-confirm-dialog" onSubmit={submit}><header><span className="admin-risk-icon"><CircleAlert size={20} /></span><div><p>安全策略变更</p><h2>{title}</h2></div><button type="button" className="admin-icon-button" aria-label="关闭" onClick={onClose}><X size={18} /></button></header><label>操作原因<textarea autoFocus minLength={3} maxLength={500} required value={reason} onChange={(event) => setReason(event.target.value)} /></label>{error && <div className="admin-form-error"><CircleAlert size={15} />{error}</div>}<footer><button type="button" className="admin-secondary-button" onClick={onClose}>取消</button><button className="admin-danger-button" disabled={submitting || reason.trim().length < 3}>{submitting ? <RefreshCw className="spin" size={15} /> : <Check size={15} />}{actionLabel}</button></footer></form></div>;
}

function RecordDetailDrawer({ eyebrow, row, onClose }: { eyebrow: string; row: Row; onClose: () => void }) {
  return <div className="admin-drawer-layer"><button className="admin-drawer-scrim" aria-label="关闭详情" onClick={onClose} /><aside className="admin-detail-drawer"><header><div><p>{eyebrow}</p><h2>{valueText(row.event_type || row.action || row.id)}</h2></div><button className="admin-icon-button" aria-label="关闭" onClick={onClose}><X size={18} /></button></header><div className="admin-drawer-content"><section className="admin-detail-fields">{Object.entries(row).map(([key, value]) => <div key={key} className={typeof value === "object" && value !== null ? "is-wide" : ""}><dt>{FIELD_LABELS[key] || key.replaceAll("_", " ")}</dt><dd><ValueBlock value={value} /></dd></div>)}</section></div></aside></div>;
}

const ACTION_COLUMNS: Column[] = [
  { key: "action_type", label: "申请动作" },
  { key: "target_type", label: "对象类型" },
  { key: "target_id", label: "对象", render: (row) => shortId(row.target_id) },
  { key: "risk_level", label: "风险", render: (row) => <RiskValue value={row.risk_level} /> },
  { key: "status", label: "状态", render: (row) => <StatusValue value={row.status} /> },
  { key: "requested_by", label: "申请人", render: (row) => shortId(row.requested_by) },
  { key: "created_at", label: "申请时间", render: (row) => dateTime(row.created_at) },
];

function permissionForAction(actionType: string): string {
  if (actionType.startsWith("user.")) return "users.manage";
  if (actionType.startsWith("membership.")) return "memberships.manage";
  if (actionType.startsWith("points.")) return "points.adjust";
  if (actionType.startsWith("pricing.")) return "pricing.manage";
  if (actionType.startsWith("job.") || actionType.startsWith("task.")) return "tasks.manage";
  if (actionType.startsWith("asset.")) return "assets.manage";
  if (actionType.startsWith("config.")) return "config.manage";
  if (actionType.startsWith("role.")) return "roles.manage";
  return "";
}

function ActionRequestsPanel({ permissions, currentUserId }: { permissions: Set<string>; currentUserId: string }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<Row | null>(null);
  const [review, setReview] = useState<{ row: Row; decision: "approve" | "reject" } | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    setLoading(true);
    apiRequest<PageResponse>(`/api/v1/admin/action-requests?limit=50${status ? `&status=${status}` : ""}`)
      .then((payload) => setRows(payload.items || []))
      .catch((caught) => setError(caught instanceof Error ? caught.message : "操作申请加载失败"))
      .finally(() => setLoading(false));
  }, [reload, status]);

  return (
    <section className="admin-list-panel">
      <div className="admin-list-toolbar"><div className="admin-filter-group"><label className="admin-select-wrap"><ListFilter size={14} /><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="">全部状态</option><option value="pending">待审批</option><option value="approved">已批准</option><option value="rejected">已拒绝</option><option value="executed">已执行</option><option value="failed">执行失败</option></select><ChevronDown size={13} /></label></div><button className="admin-icon-button bordered" title="刷新" aria-label="刷新" onClick={() => setReload((value) => value + 1)}><RefreshCw className={loading ? "spin" : ""} size={16} /></button></div>
      {error ? <LoadError message={error} onRetry={() => setReload((value) => value + 1)} /> : loading ? <TableSkeleton /> : rows.length ? <div className="admin-table-scroll"><table className="admin-data-table"><thead><tr>{ACTION_COLUMNS.map((column) => <th key={column.key}>{column.label}</th>)}<th>审批</th></tr></thead><tbody>{rows.map((row) => { const canReview = row.status === "pending" && row.requested_by !== currentUserId && permissions.has(permissionForAction(String(row.action_type))); return <tr key={String(row.id)} onClick={() => setSelected(row)}>{ACTION_COLUMNS.map((column) => <td key={column.key}>{column.render ? column.render(row) : valueText(nested(row, column.key))}</td>)}<td><div className="admin-row-buttons">{canReview ? <><button className="admin-icon-button approve" title="批准申请" aria-label="批准申请" onClick={(event) => { event.stopPropagation(); setReview({ row, decision: "approve" }); }}><Check size={15} /></button><button className="admin-icon-button reject" title="拒绝申请" aria-label="拒绝申请" onClick={(event) => { event.stopPropagation(); setReview({ row, decision: "reject" }); }}><X size={15} /></button></> : <button className="admin-icon-button" title="查看详情" aria-label="查看详情"><ChevronRight size={15} /></button>}</div></td></tr>; })}</tbody></table></div> : <div className="admin-table-empty"><Search size={24} /><strong>没有符合条件的操作申请</strong></div>}
      {selected && <ActionDetailDrawer row={selected} onClose={() => setSelected(null)} />}
      {review && <ReviewDialog row={review.row} decision={review.decision} onClose={() => setReview(null)} onReviewed={() => { setReview(null); setSelected(null); setReload((value) => value + 1); }} />}
    </section>
  );
}

function ActionDetailDrawer({ row, onClose }: { row: Row; onClose: () => void }) {
  return (
    <div className="admin-drawer-layer"><button className="admin-drawer-scrim" aria-label="关闭详情" onClick={onClose} /><aside className="admin-detail-drawer"><header><div><p>操作申请</p><h2>{valueText(row.action_type)}</h2></div><button className="admin-icon-button" aria-label="关闭" onClick={onClose}><X size={18} /></button></header><div className="admin-drawer-content"><section className="admin-detail-fields">{Object.entries(row).map(([key, value]) => <div key={key} className={typeof value === "object" && value !== null ? "is-wide" : ""}><dt>{FIELD_LABELS[key] || key.replaceAll("_", " ")}</dt><dd>{key === "status" ? <StatusValue value={value} /> : <ValueBlock value={value} />}</dd></div>)}</section></div></aside></div>
  );
}

function ReviewDialog({ row, decision, onClose, onReviewed }: { row: Row; decision: "approve" | "reject"; onClose: () => void; onReviewed: () => void }) {
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const approving = decision === "approve";
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      await apiRequest(`/api/v1/admin/action-requests/${row.id}/${decision}`, { method: "POST", body: JSON.stringify({ reason }) });
      onReviewed();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "审批失败");
    } finally {
      setSubmitting(false);
    }
  };
  return <div className="admin-modal-layer"><button className="admin-modal-scrim" aria-label="关闭" onClick={onClose} /><form className="admin-confirm-dialog" onSubmit={submit}><header><span className={`admin-risk-icon ${approving ? "approve" : ""}`}>{approving ? <CheckCircle2 size={20} /> : <XCircle size={20} />}</span><div><p>操作申请审批</p><h2>{approving ? "批准申请" : "拒绝申请"}</h2></div><button type="button" className="admin-icon-button" aria-label="关闭" onClick={onClose}><X size={18} /></button></header><div className="admin-confirm-target"><span>申请动作</span><code>{String(row.action_type)}</code></div><label>审批原因<textarea autoFocus value={reason} onChange={(event) => setReason(event.target.value)} minLength={3} maxLength={500} required /></label>{error && <div className="admin-form-error"><CircleAlert size={15} />{error}</div>}<footer><button type="button" className="admin-secondary-button" onClick={onClose}>取消</button><button className={approving ? "admin-primary-button" : "admin-danger-button"} disabled={reason.trim().length < 3 || submitting}>{submitting ? <RefreshCw className="spin" size={15} /> : approving ? <Check size={15} /> : <X size={15} />}{approving ? "批准申请" : "拒绝申请"}</button></footer></form></div>;
}

export default AdminApp;
