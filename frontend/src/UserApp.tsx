import { createPortal } from "react-dom";
import { useSiteBranding } from "./SiteBranding";
import { Pagination, useCursorPage } from "./Pagination";
import { ImageThumbnail } from "./ImageThumbnail";
import { JobProgress } from "./JobProgress";
import { estimatedPoints, jobOutputIds, downloadJob, uploadAssets } from "./user-api";
import { BusyDialog } from "./BusyDialog";
import { usePageVisible } from "./usePageVisible";
import {
  AlertCircle,
  ArrowRight,
  BadgeCheck,
  Bell,
  Brush,
  Check,
  CheckCircle2,
  ChevronDown,
  Clock3,
  Coins,
  Download,
  Eye,
  EyeOff,
  FileImage,
  Grid2X2,
  History,
  ImagePlus,
  Images,
  LayoutDashboard,
  List,
  ListTodo,
  LoaderCircle,
  LockKeyhole,
  LogIn,
  LogOut,
  Menu,
  Maximize2,
  Minimize2,
  MonitorSmartphone,
  Moon,
  MoreHorizontal,
  Palette,
  PenTool,
  Plus,
  RefreshCw,
  Scaling,
  Scissors,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  Sun,
  Trash2,
  Type,
  Upload,
  UserRound,
  WandSparkles,
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
import {
  ApiError,
  api,
  type Asset,
  type BootstrapData,
  type ImageJob,
  type MembershipPlan,
  type Operation,
  type PointTransaction,
  type Quote,
  type SessionInfo,
  type UserNotification,
} from "./user-api";
import { MaskCanvas, type MaskCanvasHandle } from "./MaskCanvas";
import { ComparisonPreview } from "./ComparisonPreview";
import { ToastMessage } from "./Toast";
import { LandingPage } from "./LandingPage";

type AppRoute = "home" | "studio" | "assets" | "jobs" | "points" | "membership" | "profile";

const ROUTES: Record<AppRoute, string> = {
  home: "/app",
  studio: "/app/studio",
  assets: "/app/assets",
  jobs: "/app/jobs",
  points: "/app/points",
  membership: "/app/membership",
  profile: "/app/profile",
};

const NAV_ITEMS: Array<{
  id: AppRoute;
  label: string;
  icon: ComponentType<{ size?: number; strokeWidth?: number }>;
}> = [
  { id: "home", label: "工作台", icon: LayoutDashboard },
  { id: "studio", label: "图片编辑器", icon: WandSparkles },
  { id: "assets", label: "素材库", icon: Images },
  { id: "jobs", label: "任务中心", icon: ListTodo },
  { id: "points", label: "积分流水", icon: Coins },
  { id: "membership", label: "会员权益", icon: BadgeCheck },
  { id: "profile", label: "账号与安全", icon: UserRound },
];

const ADMIN_PERMISSION_CODES = new Set([
  "admin.dashboard.read",
  "users.read",
  "users.manage",
  "roles.read",
  "roles.manage",
  "memberships.read",
  "memberships.manage",
  "points.read",
  "points.adjust",
  "pricing.read",
  "pricing.manage",
  "tasks.read",
  "tasks.manage",
  "assets.read",
  "assets.manage",
  "config.read",
  "config.manage",
  "config.test",
  "audit.read",
  "audit.export",
  "security.events.read",
  "security.policies.manage",
  "system.health.read",
  "system.diagnostics.read",
  "system.maintenance.execute",
]);

const OPERATION_META: Record<
  string,
  { label: string; description: string; icon: ComponentType<{ size?: number }>; source: boolean }
> = {
  "ai.generate": { label: "AI 生成", description: "根据描述创建新图案", icon: Sparkles, source: false },
  "ai.ecommerce": { label: "电商主图", description: "一组产品，多张主图。参考产品外观，统一设计与色彩。", icon: Images, source: false },
  "ai.redraw": { label: "高清重绘", description: "保留内容并提升清晰度", icon: WandSparkles, source: true },
  "ai.extract_print": { label: "印花提取", description: "从产品照片还原印花，保留设计与色彩，输出透明 PNG", icon: FileImage, source: true },
  "cutout.smart": { label: "智能抠图", description: "输出透明 PNG", icon: Scissors, source: true },
  "upscale.2x": { label: "2x 放大", description: "保真放大两倍", icon: Scaling, source: true },
  "upscale.4x": { label: "4x 放大", description: "保真放大四倍", icon: Scaling, source: true },
  "ai.repair": { label: "局部修复", description: "按遮罩修复局部", icon: Brush, source: true },
  "ai.text_fix": { label: "文字修正", description: "按遮罩校正文字", icon: Type, source: true },
  "color.effect": { label: "颜色处理", description: "灰度、黑白或单色", icon: Palette, source: true },
  "ai.variant": { label: "生成变体", description: "生成相近视觉版本", icon: ImagePlus, source: true },
  "vectorize.svg": { label: "矢量化", description: "转换为 SVG 文件", icon: PenTool, source: true },
};

const JOB_STATUS: Record<string, { label: string; tone: string }> = {
  queued: { label: "排队中", tone: "waiting" },
  running: { label: "处理中", tone: "running" },
  retry_wait: { label: "等待重试", tone: "waiting" },
  succeeded: { label: "已成功", tone: "success" },
  failed: { label: "失败", tone: "danger" },
  cancelled: { label: "已取消", tone: "muted" },
  timed_out: { label: "已超时", tone: "danger" },
};

function navigate(path: string): void {
  if (window.location.pathname === path) return;
  window.history.pushState({}, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

function usePathname(): string {
  const [pathname, setPathname] = useState(window.location.pathname + window.location.search);
  useEffect(() => {
    const update = () => setPathname(window.location.pathname + window.location.search);
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);
  return pathname;
}

function routeFromPath(path: string): AppRoute {
  return (Object.entries(ROUTES).find(([, value]) => value === path)?.[0] as AppRoute) || "home";
}

function messageOf(reason: unknown, fallback = "请求失败，请稍后重试"): string {
  if (reason instanceof ApiError && reason.status >= 500 && reason.requestId) {
    return `${reason.message}（请求编号：${reason.requestId}）`;
  }
  return reason instanceof Error ? reason.message : fallback;
}

function dateTime(value?: string | null): string {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function formatBytes(value: number): string {
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function operationName(code: string): string {
  if (code === "upload") return "上传原图";
  return OPERATION_META[code]?.label || code;
}

function UserApp() {
  const pathname = usePathname().split("?")[0];
  if (pathname === "/") return <LandingPage />;
  if (pathname === "/forgot-password") return <ForgotPasswordPage />;
  if (pathname === "/login") return <LoginPage />;
  if (pathname === "/register") return <RegisterPage />;
  return <ProtectedApp pathname={pathname} />;
}

function LoginPage() {
  const [registrationEnabled, setRegistrationEnabled] = useState(false);
  useEffect(() => {
    let active = true;
    void api.authOptions().then((options) => { if (active) setRegistrationEnabled(options.registration_enabled); }).catch(() => undefined);
    return () => { active = false; };
  }, []);
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.login(identifier.trim(), password);
      navigate("/app");
    } catch (reason) {
      setError(messageOf(reason, "登录失败"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="user-auth-page">
      <section className="user-auth-panel" aria-labelledby="login-title">
        <Brand />
        <div className="user-auth-heading">
          <span>图片生产工作台</span>
          <h1 id="login-title">登录账号</h1>
          <p>继续处理素材、版本和图片任务。</p>
        </div>
        <form onSubmit={(event) => void submit(event)}>
          <label>
            <span>邮箱或用户名</span>
            <input
              autoComplete="username"
              autoFocus
              onChange={(event) => setIdentifier(event.target.value)}
              placeholder="name@example.com"
              required
              value={identifier}
            />
          </label>
          <label>
            <span>密码</span>
            <div className="user-password-field">
              <input
                autoComplete="current-password"
                onChange={(event) => setPassword(event.target.value)}
                required
                type={showPassword ? "text" : "password"}
                value={password}
              />
              <button
                aria-label={showPassword ? "隐藏密码" : "显示密码"}
                onClick={() => setShowPassword((value) => !value)}
                title={showPassword ? "隐藏密码" : "显示密码"}
                type="button"
              >
                {showPassword ? <EyeOff size={17} /> : <Eye size={17} />}
              </button>
            </div>
          </label>
          {error && <InlineMessage tone="error">{error}</InlineMessage>}
          <button className="user-primary user-auth-submit" disabled={busy} type="submit">
            {busy ? <LoaderCircle className="spin" size={18} /> : <LogIn size={18} />}
            {busy ? "正在登录" : "登录"}
          </button>
        </form>
        {registrationEnabled && <button className="user-text-button" onClick={() => navigate("/register")} type="button">没有账号？创建账号<ArrowRight size={15} /></button>}
        <button className="user-text-button" onClick={() => navigate("/forgot-password")} type="button">
          忘记密码
          <ArrowRight size={15} />
        </button>
      </section>
      <aside className="user-auth-context" aria-label="工作台能力">
        <BrandArtwork place="login" className="user-auth-artwork" />
        <div className="user-auth-showcase"><span>SUB2IMAGE / CREATIVE STUDIO</span><h2>让想象成形。<br /><em>让细节出众。</em></h2><p>从第一道灵感，到最后一处精修。<br />你的下一件作品，从这里开始。</p></div>
        <div className="user-auth-caption"><span>01 — LIGHT IN MOTION</span><span>构想 · 提取 · 精修</span></div>
      </aside>
    </main>
  );
}

function RegisterPage() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [code, setCode] = useState("");
  const [sending, setSending] = useState(false);
  const [sentTo, setSentTo] = useState("");
  const [retryAt, setRetryAt] = useState(0);
  const [secondsLeft, setSecondsLeft] = useState(0);
  const emailRef = useRef<HTMLInputElement>(null);
  const sendInFlight = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const submitting = useRef(false);
  useEffect(() => {
    const tick = () => setSecondsLeft(Math.max(0, Math.ceil((retryAt - Date.now()) / 1000)));
    tick();
    if (!retryAt) return;
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [retryAt]);
  async function sendCode() {
    if (sendInFlight.current || retryAt > Date.now() || !emailRef.current?.reportValidity()) return;
    sendInFlight.current = true; setSending(true); setError("");
    const recipient = email.trim().toLowerCase();
    try {
      const result = await api.sendRegistrationCode(recipient);
      setSentTo(recipient);
      setSecondsLeft(result.retry_after_seconds);
      setRetryAt(Date.now() + result.retry_after_seconds * 1000);
    } catch (reason) {
      setError(messageOf(reason, "验证码发送失败"));
      if (reason instanceof ApiError && reason.retryAfter) { setSecondsLeft(reason.retryAfter); setRetryAt(Date.now() + reason.retryAfter * 1000); }
      if (reason instanceof ApiError && reason.code === "REGISTRATION_CLOSED") setEnabled(false);
    } finally { sendInFlight.current = false; setSending(false); }
  }
  useEffect(() => {
    let active = true;
    setError("");
    void api.authOptions().then((options) => { if (active) setEnabled(options.registration_enabled); })
      .catch((reason) => { if (active) setError(messageOf(reason, "无法获取注册状态")); });
    return () => { active = false; };
  }, [revision]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current || !enabled) return;
    if (password !== confirmation) { setError("两次输入的密码不一致"); return; }
    submitting.current = true;
    setBusy(true); setError("");
    try { await api.register(email.trim(), name.trim(), password, code); navigate("/app"); }
    catch (reason) {
      setError(messageOf(reason, "注册失败"));
      if (reason instanceof ApiError && reason.code === "REGISTRATION_CLOSED") setEnabled(false);
    } finally { submitting.current = false; setBusy(false); }
  }
  return <main className="user-auth-page user-register-page"><section className="user-auth-panel" aria-labelledby="register-title">
    <Brand />
    <div className="user-auth-heading"><span>让创意成为作品</span><h1 id="register-title">创建账号</h1><p>验证你的邮箱，开启图片创作工作台。</p></div>
    {enabled === false ? <InlineMessage tone="warning">管理员已关闭注册，请联系管理员开通账号。</InlineMessage>
      : enabled === null ? !error && <MiniLoading /> : <form onSubmit={(event) => void submit(event)}>
        <label><span>显示名称</span><input autoComplete="nickname" maxLength={120} required value={name} onChange={(event) => setName(event.target.value)} /></label>
        <label><span>邮箱</span><input ref={emailRef} disabled={sending || busy} autoComplete="email" type="email" maxLength={320} required value={email} onChange={(event) => { setEmail(event.target.value); setCode(""); setSentTo(""); }} /></label>
        <div className="user-verification-field"><label><span>邮箱验证码</span><input autoComplete="one-time-code" inputMode="numeric" pattern="[0-9]{6}" maxLength={6} required value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))} placeholder="6 位数字" /></label><button className="user-secondary" disabled={sending || busy || secondsLeft > 0 || !email.trim()} onClick={() => void sendCode()} type="button">{sending ? "正在发送…" : secondsLeft > 0 ? `${secondsLeft} 秒后重发` : sentTo ? "重新发送" : "获取验证码"}</button></div>
        {sentTo && <InlineMessage tone="success">验证码已发送至 {sentTo}，10 分钟内有效。未收到时请检查垃圾邮件。</InlineMessage>}
        <label><span>密码</span><input autoComplete="new-password" type="password" minLength={12} maxLength={128} required value={password} onChange={(event) => setPassword(event.target.value)} /><small>12–128 个字符，建议使用较长的独立密码。</small></label>
        <label><span>确认密码</span><input autoComplete="new-password" type="password" minLength={12} maxLength={128} required value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label>
        <button className="user-primary user-auth-submit" disabled={busy || sending} type="submit">{busy ? <LoaderCircle className="spin" size={18} /> : <UserRound size={18} />}{busy ? "正在创建" : "注册并进入工作台"}</button>
      </form>}
    {error && <InlineMessage tone="error">{error}</InlineMessage>}
    {error && enabled === null && <button className="user-secondary" onClick={() => setRevision((value) => value + 1)} type="button">重新加载注册状态</button>}
    <button className="user-text-button" onClick={() => navigate("/login")} type="button">已有账号？返回登录<ArrowRight size={15} /></button>
  </section><aside className="user-auth-context" aria-label="创作空间配图"><BrandArtwork place="register" className="user-auth-artwork" /><div className="user-auth-showcase"><span>YOUR NEXT CHAPTER</span><h2>从一份灵感，<br /><em>开启无限可能。</em></h2><p>为每一次创作，留出想象的空间。</p></div><div className="user-auth-caption"><span>02 — FORM & POSSIBILITY</span><span>灵感 · 成形</span></div></aside></main>;
}

function ForgotPasswordPage() {
  const [identifier, setIdentifier] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.forgotPassword(identifier.trim());
      setSent(true);
    } catch (reason) {
      setError(messageOf(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="user-auth-page compact">
      <section className="user-auth-panel" aria-labelledby="forgot-title">
        <Brand />
        <button className="user-back" onClick={() => navigate("/login")} type="button">
          <ArrowRight size={15} />返回登录
        </button>
        <div className="user-auth-heading">
          <span>账号恢复</span>
          <h1 id="forgot-title">找回密码</h1>
          <p>输入邮箱或用户名，我们会在重置功能可用时发送安全链接。</p>
        </div>
        {sent ? (
          <InlineMessage tone="success">若账号存在，重置说明将发送到已验证邮箱。</InlineMessage>
        ) : (
          <form onSubmit={(event) => void submit(event)}>
            <label>
              <span>邮箱或用户名</span>
              <input autoFocus onChange={(event) => setIdentifier(event.target.value)} required value={identifier} />
            </label>
            {error && <InlineMessage tone="error">{error}</InlineMessage>}
            <button className="user-primary user-auth-submit" disabled={busy} type="submit">
              {busy ? <LoaderCircle className="spin" size={18} /> : <LockKeyhole size={18} />}
              提交申请
            </button>
          </form>
        )}
      </section>
    </main>
  );
}

function ProtectedApp({ pathname }: { pathname: string }) {
  const [bootstrap, setBootstrap] = useState<BootstrapData | null>(null);
  const [error, setError] = useState("");
  const [forbidden, setForbidden] = useState(false);

  const load = useCallback(async () => {
    setError("");
    setForbidden(false);
    try {
      const payload = await api.bootstrap();
      setBootstrap(payload);
      const selectedTheme = payload.preferences.theme;
      const dark = selectedTheme === "dark" ||
        (selectedTheme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
      document.documentElement.dataset.userTheme = dark ? "dark" : "light";
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) {
        navigate("/login");
        return;
      }
      if (reason instanceof ApiError && reason.status === 403) setForbidden(true);
      else setError(messageOf(reason, "无法载入工作台"));
    }
  }, []);

  useEffect(() => void load(), [load]);

  if (forbidden) {
    return <FullPageState icon={ShieldCheck} title="账号无权访问" description="当前账号未激活或缺少工作台权限。" />;
  }
  if (error) {
    return <FullPageState icon={AlertCircle} title="工作台连接失败" description={error} action={load} />;
  }
  if (!bootstrap) return <AppLoading />;
  const normalizedPath = Object.values(ROUTES).includes(pathname) ? pathname : "/app";
  return (
    <AppShell
      bootstrap={bootstrap}
      onBootstrap={setBootstrap}
      route={routeFromPath(normalizedPath)}
    />
  );
}

function AppShell({
  bootstrap,
  onBootstrap,
  route,
}: {
  bootstrap: BootstrapData;
  onBootstrap: (value: BootstrapData) => void;
  route: AppRoute;
}) {
  const [mobileNav, setMobileNav] = useState(false);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const adminAccess = bootstrap.permissions.some((permission) => ADMIN_PERMISSION_CODES.has(permission));

  function openRoute(next: AppRoute) {
    navigate(ROUTES[next]);
    setMobileNav(false);
  }

  async function logout() {
    try {
      await api.logout();
    } finally {
      navigate("/login");
    }
  }

  const page = (() => {
    if (route === "studio") return <StudioPage key={window.location.search} bootstrap={bootstrap} onBootstrap={onBootstrap} />;
    if (route === "assets") return <AssetsPage bootstrap={bootstrap} />;
    if (route === "jobs") return <JobsPage />;
    if (route === "points") return <PointsPage bootstrap={bootstrap} />;
    if (route === "membership") return <MembershipPage bootstrap={bootstrap} />;
    if (route === "profile") return <ProfilePage bootstrap={bootstrap} onBootstrap={onBootstrap} />;
    return <DashboardPage bootstrap={bootstrap} />;
  })();

  return (
    <div className="user-shell">
      <aside className={`user-sidebar${mobileNav ? " open" : ""}`}>
        <div className="user-sidebar-brand"><Brand inverse /></div>
        <nav aria-label="用户端主导航">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            return (
              <button className={route === item.id ? "active" : ""} key={item.id} onClick={() => openRoute(item.id)} type="button">
                <Icon size={18} /><span>{item.label}</span>
              </button>
            );
          })}
        </nav>
        <div className="user-sidebar-foot">
          <a href="/"><LayoutDashboard size={17} /><span>网站首页</span><ArrowRight size={14} /></a>
          {adminAccess && (
            <a href="/admin"><ShieldCheck size={17} /><span>管理后台</span><ArrowRight size={14} /></a>
          )}
          <div className="user-sidebar-account">
            <Avatar name={bootstrap.user.display_name} />
            <span><strong>{bootstrap.user.display_name}</strong><small>{bootstrap.user.email}</small></span>
          </div>
        </div>
      </aside>
      {mobileNav && <button aria-label="关闭导航" className="user-nav-scrim" onClick={() => setMobileNav(false)} type="button" />}
      <section className="user-main">
        <header className="user-topbar">
          <div className="user-topbar-leading">
            <span className="user-breadcrumb">工作空间 <span>/</span> <strong>{NAV_ITEMS.find((item) => item.id === route)?.label}</strong></span>
            <button aria-label="打开导航" className="user-icon-button mobile-only" onClick={() => setMobileNav(true)} title="打开导航" type="button"><Menu size={20} /></button>
            <span className="user-mobile-brand"><Brand /></span>
          </div>
          <div className="user-top-actions">
            <button className="user-balance" onClick={() => openRoute("points")} title="查看积分流水" type="button">
              <Coins size={16} /><strong>{bootstrap.points.balance.toLocaleString("zh-CN")}</strong><span>积分</span>
            </button>
            <span className="user-plan-badge"><BadgeCheck size={15} />{bootstrap.membership.plan.name}</span>
            <div className="user-notification-wrap">
              <button aria-label="任务通知" aria-expanded={notificationsOpen} aria-controls="user-notifications" className="user-icon-button" onClick={() => setNotificationsOpen((value) => !value)} title="任务通知" type="button">
                <Bell size={18} />
                {bootstrap.notifications.unread_count > 0 && <i>{Math.min(99, bootstrap.notifications.unread_count)}</i>}
              </button>
              {notificationsOpen && (
                <NotificationPanel bootstrap={bootstrap} onBootstrap={onBootstrap} onClose={() => setNotificationsOpen(false)} />
              )}
            </div>
            <button aria-label="账号设置" className="user-avatar-button" onClick={() => openRoute("profile")} title="账号设置" type="button">
              <Avatar name={bootstrap.user.display_name} />
            </button>
            <button aria-label="退出登录" className="user-icon-button wide-only" onClick={() => void logout()} title="退出登录" type="button"><LogOut size={18} /></button>
          </div>
        </header>
        {bootstrap.service.status !== "ok" && (
          <div className="user-service-banner" role="status"><AlertCircle size={17} />图片服务正在维护，浏览功能仍可使用，暂时无法提交新任务。</div>
        )}
        <main className={`user-page user-page-${route}`}>{page}</main>
      </section>
    </div>
  );
}

function NotificationPanel({
  bootstrap,
  onBootstrap,
  onClose,
}: {
  bootstrap: BootstrapData;
  onBootstrap: (value: BootstrapData) => void;
  onClose: () => void;
}) {
  const [items, setItems] = useState<UserNotification[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    let active = true;
    const previous = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") onCloseRef.current(); };
    const closeOutside = (event: PointerEvent) => { if (event.target instanceof Element && !event.target.closest(".user-notification-wrap")) onCloseRef.current(); };
    document.addEventListener("keydown", closeOnEscape);
    document.addEventListener("pointerdown", closeOutside);
    void api.notifications().then((payload) => { if (active) setItems(payload.items); }).catch((reason) => { if (active) setError(messageOf(reason)); });
    return () => { active = false; document.removeEventListener("keydown", closeOnEscape); document.removeEventListener("pointerdown", closeOutside); previous?.focus(); };
  }, []);

  async function read(item: UserNotification) {
    setError("");
    if (!item.read_at) {
      await api.readNotification(item.id);
      setItems((current) => current?.map((value) => value.id === item.id ? { ...value, read_at: new Date().toISOString() } : value) || []);
      onBootstrap({
        ...bootstrap,
        notifications: { unread_count: Math.max(0, bootstrap.notifications.unread_count - 1) },
      });
    }
    if (item.target_url?.startsWith("/app")) navigate(item.target_url);
    onClose();
  }

  async function readAll() {
    setError("");
    await api.readAllNotifications();
    setItems((current) => current?.map((item) => ({ ...item, read_at: item.read_at || new Date().toISOString() })) || []);
    onBootstrap({ ...bootstrap, notifications: { unread_count: 0 } });
  }

  return (
    <><button className="user-notification-scrim" aria-label="关闭通知遮罩" onClick={onClose} type="button" /><section id="user-notifications" className="user-notifications" aria-label="通知" role="dialog">
      <header><strong>通知</strong><div><button disabled={busy || !bootstrap.notifications.unread_count} onClick={() => { setBusy(true); void readAll().catch((reason) => setError(messageOf(reason))).finally(() => setBusy(false)); }} type="button">全部已读</button><button ref={closeRef} aria-label="关闭通知" onClick={onClose} type="button"><X size={18} /></button></div></header>
      {error && <InlineMessage tone="error">{error}</InlineMessage>}
      {!items ? !error && <MiniLoading /> : items.length === 0 ? <EmptyState icon={Bell} title="暂无通知" description="任务进展和账户变化会显示在这里。" compact /> : (
        <div className="user-notification-list">
          {items.map((item) => (
            <button disabled={busy} className={item.read_at ? "" : "unread"} key={item.id} onClick={() => { setBusy(true); void read(item).catch((reason) => setError(messageOf(reason))).finally(() => setBusy(false)); }} type="button">
              <span><strong>{item.title}</strong><small>{item.body}</small></span><time>{dateTime(item.created_at)}</time>
            </button>
          ))}
        </div>
      )}
    </section></>
  );
}

function DashboardPage({ bootstrap }: { bootstrap: BootstrapData }) {
  const [jobs, setJobs] = useState<ImageJob[] | null>(null);
  const [assets, setAssets] = useState<Asset[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    Promise.all([api.jobs(), api.assets()])
      .then(([jobPayload, assetPayload]) => {
        setJobs(jobPayload.items);
        setAssets(assetPayload.items);
      })
      .catch((reason) => setError(messageOf(reason)));
  }, []);

  const activeJobs = jobs?.filter((item) => ["queued", "running", "retry_wait"].includes(item.status)).length || 0;
  return (
    <>
      <PageHeader eyebrow="YOUR WORKSPACE" title={`你好，${bootstrap.user.display_name}`} description="灵感、素材与作品，都在这里。">
        <button className="user-primary" onClick={() => navigate("/app/studio")} type="button"><Plus size={17} />新建图片任务</button>
      </PageHeader>
      <section className="user-creative-hero">
        <div><span><i className="user-creative-dot" />A SPACE FOR YOUR NEXT IDEA</span><h2>创意，自有光芒。<br /><em>把想象精雕成作品。</em></h2><p>从图像生成到印花提取，让每一处细节，<br className="wide-only" />都成为你的设计语言。</p><button className="user-primary" onClick={() => navigate("/app/studio?tool=ai.generate")} type="button">开始新的创作<ArrowRight size={17} /></button><small className="user-hero-footnote">你的灵感，你的创作空间。</small></div>
        <BrandArtwork place="home" className="user-collection-art" />
      </section>
      <section className="user-quick-tools" aria-label="快捷创作">
        {(["ai.generate", "ai.extract_print", "ai.redraw"] as const).map((code) => { const item = OPERATION_META[code]; const Icon = item.icon; return <button key={code} onClick={() => navigate(`/app/studio?tool=${code}`)} type="button"><span><Icon size={22} /></span><div><strong>{item.label}</strong><small>{item.description}</small></div><ArrowRight size={17} /></button>; })}
      </section>
      <section className="user-stat-strip" aria-label="账户概览">
        <button onClick={() => navigate("/app/points")} type="button"><span><Coins size={18} />可用积分</span><strong>{bootstrap.points.balance.toLocaleString("zh-CN")}</strong><small>累计消费 {bootstrap.points.lifetime_spent.toLocaleString("zh-CN")}</small></button>
        <button onClick={() => navigate("/app/jobs")} type="button"><span><Clock3 size={18} />进行中任务</span><strong>{activeJobs}</strong><small>最近 100 条任务中的进行项</small></button>
        <button onClick={() => navigate("/app/assets")} type="button"><span><Images size={18} />最近素材</span><strong>{assets?.length ?? "--"}</strong><small>结果自动进入素材库</small></button>
        <button onClick={() => navigate("/app/membership")} type="button"><span><BadgeCheck size={18} />当前会员</span><strong>{bootstrap.membership.plan.name}</strong><small>{bootstrap.membership.ends_at ? `有效至 ${dateTime(bootstrap.membership.ends_at)}` : "长期有效"}</small></button>
      </section>
      {error && <InlineMessage tone="error">{error}</InlineMessage>}
      <div className="user-dashboard-columns">
        <section className="user-content-section">
          <SectionHeader icon={ListTodo} title="最近任务" action={<button onClick={() => navigate("/app/jobs")} type="button">查看全部<ArrowRight size={15} /></button>} />
          {!jobs ? <MiniLoading /> : jobs.length === 0 ? <EmptyState icon={ListTodo} title="还没有图片任务" description="从编辑器提交后，任务进度会在这里持续更新。" compact /> : (
            <div className="user-compact-list">
              {jobs.slice(0, 5).map((job) => <JobRow job={job} key={job.id} />)}
            </div>
          )}
        </section>
        <section className="user-content-section">
          <SectionHeader icon={Images} title="最近素材" action={<button onClick={() => navigate("/app/assets")} type="button">打开素材库<ArrowRight size={15} /></button>} />
          {!assets ? <MiniLoading /> : assets.length === 0 ? <EmptyState icon={Images} title="素材库为空" description="上传原图或完成一次图片任务即可建立素材版本。" compact /> : (
            <div className="user-recent-assets">
              {assets.slice(0, 6).map((asset) => <AssetThumb asset={asset} key={asset.id} onClick={() => navigate(`/app/studio?source=${asset.id}`)} />)}
            </div>
          )}
        </section>
      </div>
    </>
  );
}

interface StudioFormState {
  platform: string;
  imageCount: number;
  prompt: string;
  size: string;
  quality: string;
  colorMode: string;
  color: string;
  maxColors: number;
}

function StudioPage({
  bootstrap,
  onBootstrap,
}: {
  bootstrap: BootstrapData;
  onBootstrap: (value: BootstrapData) => void;
}) {
  const initialSource = new URLSearchParams(window.location.search).get("source");
  const initialJob = new URLSearchParams(window.location.search).get("job");
  const [operations, setOperations] = useState<Operation[]>([]);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [operationCode, setOperationCode] = useState(
    new URLSearchParams(window.location.search).get("tool") || bootstrap.preferences.studio_layout.last_tool || (initialSource ? "ai.redraw" : "ai.generate"),
  );
  const [sourceId, setSourceId] = useState(initialSource || "");
  const [referenceIds, setReferenceIds] = useState<string[]>(initialSource ? [initialSource] : []);
  const [batchResults, setBatchResults] = useState<Asset[]>([]);
  const [downloadingBatch, setDownloadingBatch] = useState(false);
  const [maskId, setMaskId] = useState("");
  const [form, setForm] = useState<StudioFormState>({
    platform: "amazon",
    imageCount: 4,
    prompt: "",
    size: "1024x1024",
    quality: "high",
    colorMode: "grayscale",
    color: "#171c1b",
    maxColors: 6,
  });
  const [quote, setQuote] = useState<Quote | null>(null);
  const quoteParameters = useRef<Record<string, unknown>>({});
  const submitting = useRef(false);
  const brushRef = useRef<MaskCanvasHandle>(null);
  const [maskRevision, setMaskRevision] = useState(0);
  const [pollError, setPollError] = useState("");
  const [previewRevision, setPreviewRevision] = useState(0);
  const [expanded, setExpanded] = useState(false);
  const bootstrapRef = useRef(bootstrap);
  bootstrapRef.current = bootstrap;
  const [busyState, setBusy] = useState<"loading" | "upload" | "quote" | "submit" | "mask" | null>("loading");
  const [uploadDetail, setUploadDetail] = useState("");
  const pageVisible = usePageVisible();
  const [restoringJob, setRestoringJob] = useState(Boolean(initialJob));
  const busy = restoringJob ? "loading" : busyState;
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [activeJob, setActiveJob] = useState<ImageJob | null>(null);
  const [resultAsset, setResultAsset] = useState<Asset | null>(null);
  const [lineage, setLineage] = useState<Asset[]>([]);
  const [previewMode, setPreviewMode] = useState<"transparent" | "white" | "dark" | "color" | "image">("transparent");
  const [previewColor, setPreviewColor] = useState("#e8c7b5");
  const [previewImage, setPreviewImage] = useState<string | null>(null);
  const uploadRef = useRef<HTMLInputElement>(null);
  const maskRef = useRef<HTMLInputElement>(null);
  const previewRef = useRef<HTMLInputElement>(null);

  const loadStudio = useCallback(async () => {
    setBusy("loading");
    setError("");
    try {
      const [operationPayload, assetPayload] = await Promise.all([api.operations(), api.assets()]);
      setOperations(operationPayload.items.filter((item) => item.enabled));
      setAssets((current) => [...assetPayload.items, ...current.filter((item) => !assetPayload.items.some((loaded) => loaded.id === item.id))]);
      if (!initialJob) setOperationCode((current) =>
        operationPayload.items.some((item) => item.enabled && item.code === current)
          ? current
          : operationPayload.items.find((item) => item.enabled)?.code || "ai.generate",
      );
      if (initialSource && !assetPayload.items.some((item) => item.id === initialSource)) {
        const payload = await api.asset(initialSource);
        setAssets((current) => [payload.asset, ...current]);
      }
    } catch (reason) {
      setError(messageOf(reason, "无法载入编辑器"));
    } finally {
      setBusy(null);
    }
  }, []);

  useEffect(() => void loadStudio(), [loadStudio]);
  useEffect(() => {
    if (!expanded) return;
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") setExpanded(false); };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [expanded]);
  useEffect(() => () => {
    if (previewImage) URL.revokeObjectURL(previewImage);
  }, [previewImage]);

  const selectedOperation = operations.find((item) => item.code === operationCode);
  const meta = OPERATION_META[operationCode] || OPERATION_META["ai.redraw"];
  const isGeneration = operationCode === "ai.generate" || operationCode === "ai.ecommerce";
  const compareSource = meta.source || (isGeneration && Boolean(sourceId));
  const source = assets.find((item) => item.id === sourceId) || null;
  const displayAsset = resultAsset || (compareSource ? source : null);
  const sourceUrl = useSignedAssetUrl(compareSource ? source?.id || null : null, previewRevision, (reason) => setError(messageOf(reason, "原图预览地址获取失败")));
  const resultUrl = useSignedAssetUrl(resultAsset?.id || null, previewRevision, (reason) => setError(messageOf(reason, "结果预览地址获取失败")));
  const needsMask = operationCode === "ai.repair" || operationCode === "ai.text_fix";
  const canCreate = bootstrap.permissions.includes("studio.use") && bootstrap.permissions.includes("tasks.create");
  const maintenance = bootstrap.service.status !== "ok";
  const providerUnavailable =
    selectedOperation?.engine_type === "sub2api" &&
    !bootstrap.service.features.sub2api_configured;
  const jobRunning = Boolean(activeJob && ["queued", "running", "retry_wait"].includes(activeJob.status));
  const selectableAssets = assets.filter((item) => item.status === "ready" && !["mask", "thumbnail", "vector"].includes(item.kind));

  function refreshBalance() {
    void api.pointBalance().then(({ account }) => onBootstrap({ ...bootstrapRef.current, points: account })).catch(() => undefined);
  }

  useEffect(() => {
    setMaskId("");
    setMaskRevision(0);
    setQuote(null);
  }, [sourceId]);

  useEffect(() => {
    let cancelled = false;
    let jobId = initialJob;
    try {
      // Explicit task/source links always take precedence over a background task.
      if (!jobId && !initialSource) jobId = sessionStorage.getItem(`studio-job:${bootstrap.user.id}`);
    } catch { /* Storage may be disabled by the browser. */ }
    if (jobId) {
      setRestoringJob(true);
      void api.job(jobId).then(async ({ job }) => {
        if (cancelled) return;
        setOperationCode(job.operation_code);
        const refs = Array.isArray(job.parameters.reference_asset_ids) ? job.parameters.reference_asset_ids.map(String) : job.source_asset_id ? [job.source_asset_id] : [];
        setReferenceIds(refs);
        setSourceId(job.source_asset_id || refs[0] || "");
        for (const id of refs.filter((id) => id !== job.source_asset_id)) {
          try { const { asset } = await api.asset(id); if (!cancelled) setAssets((current) => [asset, ...current.filter((item) => item.id !== id)]); }
          catch { if (!cancelled) setError("部分参考图已过期或被删除，重新提交前请替换。"); }
        }
        setForm((current) => ({
          ...current,
          platform: String(job.parameters.platform || "amazon"),
          imageCount: Number(job.parameters.image_count || 4),
          prompt: String(job.parameters.prompt || job.parameters.instruction || ""),
          size: String(job.parameters.size || current.size),
          quality: String(job.parameters.quality || current.quality),
          colorMode: String(job.parameters.mode || current.colorMode),
          color: String(job.parameters.color || current.color),
          maxColors: Number(job.parameters.max_colors || current.maxColors),
        }));
        if (job.source_asset_id) {
          try {
            const { asset } = await api.asset(job.source_asset_id);
            if (!cancelled) setAssets((current) => [asset, ...current.filter((item) => item.id !== asset.id)]);
          } catch (reason) {
            if (!cancelled) setError(`原图暂不可用，可能已过期或被删除。${messageOf(reason)}`);
          }
        }
        if (!cancelled) setActiveJob(job);
      }).catch((reason) => { if (!cancelled) setError(messageOf(reason, "无法载入任务")); })
        .finally(() => { if (!cancelled) setRestoringJob(false); });
    }
    return () => { cancelled = true; };
  }, [bootstrap.user.id, initialJob, initialSource]);

  useEffect(() => {
    let cancelled = false;
    if (!displayAsset) {
      setLineage([]);
      return;
    }
    api.lineage(displayAsset.id).then((payload) => { if (!cancelled) setLineage(payload.items); }).catch(() => { if (!cancelled) setLineage([displayAsset]); });
    return () => { cancelled = true; };
  }, [displayAsset?.id]);

  useEffect(() => {
    if (!activeJob || !pageVisible) return;
    const jobId = activeJob.id;
    let cancelled = false;
    let timer: number;
    async function poll() {
      try {
        const { job, next_poll_after_ms } = await api.jobEvents(jobId);
        if (cancelled) return;
        // Results are published after the completion transaction. Retry retrieval
        // until available instead of stopping forever at the first 404/503.
        if (job.status === "succeeded" && job.output_asset_id) {
          const results = await Promise.all(jobOutputIds(job).map(async (id) => (await api.asset(id)).asset));
          const asset = results[0];
          if (cancelled) return;
          setResultAsset(asset);
          setBatchResults(results);
          setAssets((current) => [...results, ...current.filter((item) => !results.some((output) => output.id === item.id))]);
          setNotice("");
        }
        setActiveJob(job);
        setPollError("");
        if (["queued", "running", "retry_wait"].includes(job.status)) {
          timer = window.setTimeout(poll, Math.max(1000, next_poll_after_ms || 2000));
        } else {
          setNotice("");
          refreshBalance();
          try {
            if (sessionStorage.getItem(`studio-job:${bootstrap.user.id}`) === jobId) sessionStorage.removeItem(`studio-job:${bootstrap.user.id}`);
          } catch { /* Optional storage. */ }
        }
      } catch (reason) {
        if (cancelled) return;
        setPollError(`暂时无法获取任务或结果，正在自动重试。${messageOf(reason)}`);
        timer = window.setTimeout(poll, 4000);
      }
    }
    void poll();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [activeJob?.id, pageVisible]);

  function parameters(): Record<string, unknown> {
    if (isGeneration) {
      return { prompt: form.prompt.trim(), size: form.size, quality: form.quality, output_format: "png", reference_asset_ids: referenceIds, ...(operationCode === "ai.ecommerce" ? { platform: form.platform, image_count: form.imageCount } : {}) };
    }
    if (operationCode === "ai.redraw" || operationCode === "ai.variant" || operationCode === "ai.extract_print") {
      return { instruction: form.prompt.trim(), size: "auto", quality: form.quality };
    }
    if (operationCode === "ai.repair" || operationCode === "ai.text_fix") {
      return { instruction: form.prompt.trim(), mask_asset_id: maskId, size: "auto", quality: form.quality };
    }
    if (operationCode.startsWith("upscale.")) return { sharpen: true };
    if (operationCode === "color.effect") return { mode: form.colorMode, color: form.color };
    if (operationCode === "vectorize.svg") return { max_colors: form.maxColors };
    return {};
  }

  async function upload(file?: File) {
    if (!file) return;
    if (busy || jobRunning) return;
    if (file.size > bootstrap.membership.entitlements.max_upload_mb * 1024 * 1024) {
      setError(`图片超过当前会员 ${bootstrap.membership.entitlements.max_upload_mb} MB 上传限制。`);
      return;
    }
    setBusy("upload");
    setUploadDetail(`${file.name} · 正在上传并保存到素材库`);
    setError("");
    try {
      const payload = await api.uploadAsset(file);
      setAssets((current) => [payload.asset, ...current]);
      setSourceId(payload.asset.id);
      setResultAsset(null);
      if (!meta.source) selectOperation("ai.redraw");
      setNotice("原图已安全上传到素材库。 ");
    } catch (reason) {
      setError(messageOf(reason, "上传失败"));
    } finally {
      setBusy(null);
      if (uploadRef.current) uploadRef.current.value = "";
    }
  }

  function changeReferences(ids: string[]) {
    setReferenceIds(ids); setSourceId(ids[0] || ""); setQuote(null); setResultAsset(null); setBatchResults([]);
  }

  async function uploadReferences(files: File[]) {
    if (busy || jobRunning || !files.length) return;
    if (referenceIds.length + files.length > 6) { setError("最多添加 6 张参考图，可先移除不需要的图片。"); return; }
    if (files.some((file) => file.size > bootstrap.membership.entitlements.max_upload_mb * 1024 * 1024)) { setError("参考图超过会员上传大小限制。"); return; }
    setBusy("upload"); setError("");
    setUploadDetail(`已完成 0 / ${files.length} 张 · 正在上传并保存参考图`);
    try {
      const results = await uploadAssets(files, (completed) => setUploadDetail(`已处理 ${completed} / ${files.length} 张 · 正在保存参考图`));
      const uploaded = results.flatMap((result) => result.status === "fulfilled" ? [result.value] : []);
      const failed = results.find((result) => result.status === "rejected");
      setAssets((current) => [...uploaded, ...current]);
      if (uploaded.length) changeReferences([...referenceIds, ...uploaded.map((asset) => asset.id)]);
      if (failed?.status === "rejected") setError(`已上传 ${uploaded.length} / ${files.length} 张，失败的图片可重新选择上传。${messageOf(failed.reason, "上传失败")}`);
      else setNotice(`${uploaded.length} 张参考图已上传。`);
    } catch (reason) { setError(messageOf(reason, "参考图上传失败")); }
    finally {
      setBusy(null); if (uploadRef.current) uploadRef.current.value = "";
    }
  }

  async function uploadMask(file?: File) {
    if (!file || busy || jobRunning) return;
    setBusy("mask");
    setError("");
    try {
      const payload = await api.uploadAsset(file, "mask");
      if (payload.asset.width !== source?.width || payload.asset.height !== source?.height) throw new Error("遮罩尺寸必须与来源图片一致，请重新选择。");
      setMaskId(payload.asset.id);
      setNotice("修复遮罩已上传。 ");
    } catch (reason) {
      setError(messageOf(reason, "遮罩上传失败"));
    } finally {
      setBusy(null);
      if (maskRef.current) maskRef.current.value = "";
    }
  }

  async function prepareQuote() {
    setError("");
    setNotice("");
    if (isGeneration && !form.prompt.trim() && !referenceIds.length) {
      setError("请输入希望生成的图片内容。");
      return;
    }
    if (meta.source && !sourceId) {
      setError("请先上传或选择一个来源素材。");
      return;
    }
    if (operationCode === "ai.text_fix" && !form.prompt.trim()) {
      setError("请输入需要替换的正确文字。");
      return;
    }
    setBusy("quote");
    try {
      let selectedMaskId = maskId;
      if (needsMask && !selectedMaskId) {
        const file = await brushRef.current?.exportMask();
        if (!file) throw new Error("请在图片上涂抹需要修改的区域，或上传透明 PNG 遮罩。");
        const { asset } = await api.uploadAsset(file, "mask");
        selectedMaskId = asset.id;
        setMaskId(asset.id);
      }
      const snapshot = { ...parameters(), ...(needsMask ? { mask_asset_id: selectedMaskId } : {}) };
      const payload = await api.quote(operationCode, meta.source || isGeneration ? sourceId || null : null, snapshot);
      quoteParameters.current = snapshot;
      setQuote(payload.quote);
    } catch (reason) {
      setError(messageOf(reason, "无法获取任务报价"));
    } finally {
      setBusy(null);
    }
  }

  async function submitJob() {
    if (!quote || submitting.current) return;
    submitting.current = true;
    setBusy("submit");
    setError("");
    try {
      const payload = await api.createJob(quote.id, quoteParameters.current);
      setActiveJob(payload.job);
      setQuote(null);
      setResultAsset(null);
      setBatchResults([]);
      try { sessionStorage.setItem(`studio-job:${bootstrap.user.id}`, payload.job.id); } catch { /* Optional storage. */ }
      setNotice(payload.dispatched ? "任务已进入处理队列，可继续浏览其他页面。" : "任务已保存，调度器将尽快处理。 ");
      refreshBalance();
    } catch (reason) {
      if (reason instanceof ApiError && ["JOB_QUOTE_EXPIRED", "JOB_QUOTE_ALREADY_USED", "JOB_QUOTE_MISMATCH"].includes(reason.code)) setQuote(null);
      setError(messageOf(reason, "任务提交失败"));
    } finally {
      submitting.current = false;
      setBusy(null);
    }
  }

  function selectOperation(code: string) {
    setOperationCode(code);
    setQuote(null);
    setError("");
    setNotice("");
    setResultAsset(null);
    setBatchResults([]);
    if (code === "ai.generate" || code === "ai.ecommerce") { setReferenceIds([]); setSourceId(""); }
    if (!jobRunning) setActiveJob(null);
    api.updatePreferences({ studio_layout: { last_tool: code } }).catch(() => undefined);
  }

  function choosePreviewImage(file?: File) {
    if (!file) return;
    if (previewImage) URL.revokeObjectURL(previewImage);
    setPreviewImage(URL.createObjectURL(file));
    setPreviewMode("image");
  }

  const previewStyle = previewMode === "image" && previewImage
    ? { backgroundImage: `url(${previewImage})` }
    : previewMode === "color"
      ? { backgroundColor: previewColor }
      : undefined;

  if (!canCreate) return <ForbiddenState title="无权创建图片任务" description="当前账号缺少工作台或任务创建权限。" />;
  return (
    <div className={`user-studio-page${expanded ? " preview-expanded" : ""}`}>
      <BusyDialog title={busy === "upload" ? "正在上传图片" : busy === "mask" ? "正在上传遮罩" : busy === "quote" ? "正在准备任务" : downloadingBatch ? "正在打包下载" : null} detail={busy === "upload" ? uploadDetail : busy === "mask" ? "正在保存遮罩并核对图片尺寸" : busy === "quote" ? "正在准备素材并核算本次积分" : "正在整理整组图片，准备完成后自动开始下载"} />
      <PageHeader eyebrow="IMAGE STUDIO" title="图片编辑器" description="选择工具，上传图片，细节交给我们。">
        <button className="user-secondary" onClick={() => navigate("/app/jobs")} type="button"><ListTodo size={17} />任务中心</button>
      </PageHeader>
      <div className="user-studio-layout">
        <nav className="user-toolrail" aria-label="图片工具">
          {operations.map((operation) => {
            const item = OPERATION_META[operation.code] || { label: operation.name, icon: Settings2 };
            const Icon = item.icon;
            return <button aria-label={item.label} aria-pressed={operationCode === operation.code} disabled={Boolean(busy) || jobRunning} className={operationCode === operation.code ? "active" : ""} key={operation.code} onClick={() => selectOperation(operation.code)} title={`${item.label} · 预计 ${operation.member_base_points ?? operation.current_price?.base_points ?? 0} 积分起`} type="button"><Icon size={19} /><span>{item.label}</span></button>;
          })}
        </nav>
        <section className="user-canvas-column" onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); if (isGeneration) void uploadReferences(Array.from(event.dataTransfer.files)); else void upload(event.dataTransfer.files[0]); }}>
          <header className="user-canvas-head">
            <span><FileImage size={17} /><strong>{compareSource ? "原图与结果" : "创作预览"}</strong></span>
            <div className="user-preview-actions"><button aria-label={expanded ? "收起画布" : "展开画布"} className="user-icon-button" onClick={() => setExpanded((value) => !value)} title={expanded ? "收起画布（Esc）" : "展开画布"} type="button">{expanded ? <Minimize2 size={16} /> : <Maximize2 size={16} />}</button>{(source || resultAsset) && <button aria-label="重新载入预览" className="user-icon-button" onClick={() => { setError(""); setPreviewRevision((value) => value + 1); }} title="重新载入预览" type="button"><RefreshCw size={15} /></button>}</div>
            {resultAsset && <div className="user-canvas-actions"><button className="user-primary compact" onClick={() => void downloadAsset(resultAsset.id).catch((reason) => setError(messageOf(reason)))} type="button"><Download size={16} />下载</button></div>}
          </header>
          <ComparisonPreview key={`${sourceId}:${resultAsset?.id}:${operationCode}`} compare={compareSource} source={source ? { asset: source, url: sourceUrl } : null} result={resultAsset ? { asset: resultAsset, url: resultUrl } : null}
            backgroundClass={`preview-${previewMode}`} backgroundStyle={previewStyle}
            onError={() => setError("图片预览加载失败，可尝试重新载入预览或下载图片。")}
            sourceOverlay={needsMask && !resultAsset && source?.width && source?.height ? <MaskCanvas key={`${sourceId}:${maskRevision}`} ref={brushRef} disabled={Boolean(busy) || jobRunning} width={source.width} height={source.height} onChange={() => { setMaskId(""); setQuote(null); }} /> : undefined}
            empty={busy === "loading" ? <MiniLoading /> : isGeneration ? (
              <div className="user-canvas-empty generation"><span><Sparkles size={32} /></span><small>YOUR NEXT CREATION</small><strong>把想象，变成看得见的作品</strong><p>在右侧写下你的想法，<br />选择尺寸与质量，即可开始创作。</p><div className="user-prompt-examples">{["极简植物线稿，米白背景，适合装饰画", "复古山脉与落日，丝网印刷风格"].map((prompt) => <button key={prompt} onClick={() => setForm({ ...form, prompt })} type="button">{prompt}<ArrowRight size={14} /></button>)}</div></div>
            ) : (
              <button className="user-canvas-empty" disabled={Boolean(busy) || jobRunning} onClick={() => uploadRef.current?.click()} type="button"><span><ImagePlus size={32} /></span><strong>放入图片，开始创作</strong><p>拖拽图片到这里，或点击上传</p><small>PNG / JPEG / WebP · 最大 {bootstrap.membership.entitlements.max_upload_mb} MB</small></button>
            )} />
          {batchResults.length > 1 && <section className="studio-result-gallery" aria-label="本次任务全部结果"><header><strong>本次结果 <small>{batchResults.length} 张</small></strong><button type="button" className="user-secondary" disabled={downloadingBatch} onClick={async () => { if (!activeJob) return; setDownloadingBatch(true); try { await downloadJob(activeJob.id); } catch (reason) { setError(messageOf(reason)); } finally { setDownloadingBatch(false); } }}><Download size={14} />{downloadingBatch ? "打包中…" : "下载整组 ZIP"}</button></header><div>{batchResults.map((asset, index) => <button type="button" key={asset.id} aria-label={`查看第 ${index + 1} 张结果`} aria-pressed={resultAsset?.id === asset.id} onClick={() => setResultAsset(asset)}><ImageThumbnail id={asset.id} /><span>{String(index + 1).padStart(2, "0")}</span></button>)}</div></section>}
            {activeJob && ["queued", "running", "retry_wait"].includes(activeJob.status) && (
              <JobProgress job={activeJob} />
            )}
          {(resultAsset || needsMask) && <div className="user-canvas-foot"><span>{needsMask && !resultAsset ? "紫色涂抹区域将被修改，其他区域保留" : "原图保留 · 结果为独立版本"}</span>{resultAsset && resultAsset.kind !== "vector" && <button disabled={Boolean(busy)} onClick={() => { setSourceId(resultAsset.id); setResultAsset(null); setActiveJob(null); selectOperation("ai.redraw"); }} type="button">继续编辑结果<ArrowRight size={14} /></button>}</div>}
          {error && !quote && <InlineMessage tone="error">{error}</InlineMessage>}
          {notice && !error && <InlineMessage tone="success">{notice}</InlineMessage>}
          {pollError && <InlineMessage tone="warning">{pollError}</InlineMessage>}
          {activeJob && !["queued", "running", "retry_wait"].includes(activeJob.status) && activeJob.status !== "succeeded" && (
            <InlineMessage tone="error">{activeJob.error_message || "任务未能完成"}{activeJob.refund_status === "refunded" ? "，已退回本次积分。" : "。"}</InlineMessage>
          )}
          {lineage.length > 0 && (
            <section className="user-version-strip">
              <header><History size={16} /><strong>版本链</strong><span>{lineage.length}</span></header>
              <div>{lineage.map((asset, index) => <AssetThumb asset={asset} key={asset.id} label={`V${index + 1}`} onClick={() => { if (busy || jobRunning || asset.kind === "vector") return; setSourceId(asset.id); setResultAsset(null); }} />)}</div>
            </section>
          )}
        </section>
        <aside className="user-studio-controls">
          <header><span>创作设置</span><h2>{meta.label}</h2><p>{meta.description}</p></header>
          <fieldset className="user-studio-fields" disabled={Boolean(busy) || jobRunning}>
          {isGeneration && <section className="studio-references"><header><span>参考图片 <small>可选 · {referenceIds.length}/6</small></span><button type="button" onClick={() => uploadRef.current?.click()} disabled={referenceIds.length >= 6}><Plus size={14} />添加</button></header><div>{referenceIds.map((id, index) => <div key={id}><button type="button" aria-label={`查看参考图 ${index + 1}`} aria-pressed={sourceId === id} onClick={() => { setSourceId(id); setReferenceIds([id, ...referenceIds.filter((value) => value !== id)]); setQuote(null); }}><ImageThumbnail id={id} /><small>{index === 0 ? "主参考" : `参考 ${index + 1}`}</small></button><button className="studio-reference-remove" type="button" aria-label={`移除参考图 ${index + 1}`} onClick={() => changeReferences(referenceIds.filter((value) => value !== id))}><X size={12} /></button></div>)}{!referenceIds.length && <button className="studio-reference-empty" type="button" onClick={() => uploadRef.current?.click()}><ImagePlus size={20} /><span>上传产品或灵感图<small>支持多选，也可拖入画布</small></span></button>}</div><select aria-label="从素材库添加参考图" value="" disabled={referenceIds.length >= 6} onChange={(event) => { if (event.target.value) changeReferences([...referenceIds, event.target.value]); }}><option value="">从素材库添加</option>{selectableAssets.filter((asset) => !referenceIds.includes(asset.id)).map((asset) => <option key={asset.id} value={asset.id}>{asset.original_filename || operationName(asset.operation_code)} · {dateTime(asset.created_at)}</option>)}</select></section>}
          {operationCode === "ai.ecommerce" && <div className="studio-commerce-fields"><label className="user-field"><span>电商平台</span><select value={form.platform} onChange={(event) => setForm({ ...form, platform: event.target.value })}><option value="amazon">Amazon</option><option value="etsy">Etsy</option><option value="shopify">Shopify</option><option value="taobao">淘宝 / 天猫</option><option value="jd">京东</option><option value="douyin">抖音电商</option></select></label><label className="user-field"><span>生成张数</span><select value={form.imageCount} onChange={(event) => setForm({ ...form, imageCount: Number(event.target.value) })}>{Array.from({ length: 8 }, (_, i) => <option value={i + 1} key={i}>{i + 1} 张</option>)}</select></label></div>}
          {meta.source && (
            <div className="studio-source-picker"><label className="user-field"><span>来源素材</span><select onChange={(event) => { setSourceId(event.target.value); setResultAsset(null); }} value={sourceId}><option value="">从素材库选择</option>{selectableAssets.map((asset) => <option key={asset.id} value={asset.id}>{asset.original_filename || operationName(asset.operation_code)} · {dateTime(asset.created_at)}</option>)}</select></label><button className="user-secondary" aria-label={source ? "替换 / 上传图片" : "上传图片"} title={source ? "替换 / 上传图片" : "上传图片"} disabled={busy === "upload"} onClick={() => uploadRef.current?.click()} type="button">{busy === "upload" ? <LoaderCircle className="spin" size={17} /> : <Upload size={17} />}</button></div>
          )}
          <input accept="image/png,image/jpeg,image/webp" hidden multiple={isGeneration} onChange={(event) => { if (isGeneration) void uploadReferences(Array.from(event.target.files || [])); else void upload(event.target.files?.[0]); }} ref={uploadRef} type="file" />
          {(operationCode.startsWith("ai.") || operationCode === "ai.generate") && (
            <label className="user-field"><span>{operationCode === "ai.generate" ? "图片描述" : operationCode === "ai.text_fix" ? "正确文字" : "补充要求（可选）"}</span><textarea maxLength={1500} onChange={(event) => setForm({ ...form, prompt: event.target.value })} placeholder={operationCode === "ai.generate" ? "例如：适合丝网印刷的复古山脉图案" : "说明需要保留或调整的内容"} rows={3} value={form.prompt} /><small>{form.prompt.length} / 1500</small></label>
          )}
          {isGeneration && <label className="user-field"><span>画布尺寸</span><select onChange={(event) => setForm({ ...form, size: event.target.value })} value={form.size}><option value="1024x1024">方形 · 1024 × 1024</option><option value="1024x1536">竖版 · 1024 × 1536</option><option value="1536x1024">横版 · 1536 × 1024</option><option value="auto">自动</option></select></label>}
          {(operationCode.startsWith("ai.")) && <Segmented label="生成质量" value={form.quality} options={[["medium", `标准 · ${estimatedPoints(selectedOperation, { quality: "medium" }) ?? "--"} 积分`], ["high", `精细 · ${estimatedPoints(selectedOperation, { quality: "high" }) ?? "--"} 积分`]]} onChange={(value) => setForm({ ...form, quality: value })} />}
          {needsMask && <div className="user-field"><span>修改区域</span><p className="user-mask-hint">直接在预览图上涂抹。也可上传与原图同尺寸的 PNG，透明区域表示需要修改的部分。</p><button className={maskId ? "user-file-ready" : "user-file-input"} onClick={() => maskRef.current?.click()} type="button">{maskId ? <Check size={17} /> : <Brush size={17} />}{maskId ? "遮罩已就绪 · 点击替换" : "上传透明 PNG 遮罩"}</button><input accept="image/png" hidden onChange={(event) => { setMaskRevision((value) => value + 1); void uploadMask(event.target.files?.[0]); }} ref={maskRef} type="file" /></div>}
          {operationCode === "color.effect" && <><Segmented label="颜色效果" value={form.colorMode} options={[["grayscale", "灰度"], ["threshold", "黑白"], ["invert", "反色"], ["monochrome", "单色"]]} onChange={(value) => setForm({ ...form, colorMode: value })} />{form.colorMode === "monochrome" && <label className="user-color-field"><input aria-label="单色颜色" onChange={(event) => setForm({ ...form, color: event.target.value })} type="color" value={form.color} /><span><strong>目标颜色</strong><small>{form.color.toUpperCase()}</small></span></label>}</>}
          {operationCode === "vectorize.svg" && <label className="user-field"><span>最大颜色数</span><input max="12" min="2" onChange={(event) => setForm({ ...form, maxColors: Number(event.target.value) })} type="number" value={form.maxColors} /></label>}
          {operationCode === "ai.extract_print" && <details className="studio-tool-help"><summary>印花提取使用建议</summary><p>适合衣服、杯子、帆布袋等产品照片。还原平面印花、修复褶皱和透视，保留原设计、文字与色彩，输出透明 PNG。先裁切到图案附近，效果更稳定。</p></details>}
          {operationCode === "ai.redraw" && <details className="studio-tool-help"><summary>高清重绘与印花提取的区别</summary><p>重绘只提升清晰度，保留主体、背景与构图。需要去除产品、单独还原图案，请使用“印花提取”。</p></details>}
          {resultAsset?.has_alpha && <details className="studio-tool-help"><summary>预览背景（不影响导出）</summary><PreviewBackgroundControls color={previewColor} mode={previewMode} onColor={setPreviewColor} onImage={choosePreviewImage} onMode={setPreviewMode} previewRef={previewRef} /></details>}
          </fieldset>
          <div className="user-studio-submit">
          <div className="user-quote-summary"><span>预计积分</span><strong>{estimatedPoints(selectedOperation, parameters()) ?? "--"}<small>积分</small></strong></div>
          <button className="user-primary user-submit-operation" disabled={Boolean(busy) || jobRunning || !selectedOperation || maintenance || providerUnavailable} onClick={() => void prepareQuote()} type="button">{busy === "quote" || jobRunning ? <LoaderCircle className="spin" size={18} /> : <Sparkles size={18} />}{jobRunning ? "正在处理图片" : busy === "quote" ? "正在计算报价" : "开始创作"}<ArrowRight size={16} /></button>
          <small className="user-submit-note">{operationCode === "ai.ecommerce" && `共 ${form.imageCount} 张 · `}确认报价后扣费 · 失败自动退还积分</small>
          {(maintenance || providerUnavailable) && <small className="user-maintenance-note">{maintenance ? "服务维护期间暂不接受新任务" : "AI 图片服务尚未配置"}</small>}
          {!selectedOperation && !busy && <small className="user-maintenance-note">没有可用工具，请检查后台的功能定价配置。</small>}
          </div>
        </aside>
      </div>
      {quote && <QuoteDialog balance={bootstrap.points.balance} busy={busy === "submit"} error={error} operation={selectedOperation} quote={quote} onCancel={() => { if (!submitting.current) setQuote(null); }} onConfirm={() => void submitJob()} />}
    </div>
  );
}

function AssetsPage({ bootstrap }: { bootstrap: BootstrapData }) {
  const initialView = bootstrap.preferences.studio_layout.asset_view || "grid";
  const [view, setView] = useState<"grid" | "list">(initialView);
  const [kind, setKind] = useState("");
  const [dateFilter, setDateFilter] = useState("");
  const [jobFilter, setJobFilter] = useState("");
  const [versionFilter, setVersionFilter] = useState("");
  const pager = useCursorPage(JSON.stringify([kind, dateFilter, jobFilter, versionFilter]), (cursor, limit) => api.assets(kind, { cursor, limit, created_day: dateFilter, job_query: jobFilter, root_query: versionFilter }));
  const { items, error, setError } = pager;
  const [lineage, setLineage] = useState<Asset[] | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Asset | null>(null);

  const filtered = items || [];

  function changeView(next: "grid" | "list") {
    setView(next);
    api.updatePreferences({ studio_layout: { asset_view: next } }).catch(() => undefined);
  }

  async function showLineage(asset: Asset) {
    setLineage(null);
    try {
      const payload = await api.lineage(asset.id);
      setLineage(payload.items);
    } catch (reason) {
      setError(messageOf(reason));
    }
  }

  async function removeAsset() {
    if (!deleteTarget) return;
    try {
      await api.deleteAsset(deleteTarget.id);
      setDeleteTarget(null);
      await pager.load();
    } catch (reason) {
      setDeleteTarget(null);
      setError(messageOf(reason, "无法删除素材，可能仍有运行任务依赖它"));
    }
  }

  return (
    <>
      <PageHeader eyebrow="素材与版本" title="素材库" description="查看原图、任务结果和完整版本关系。">
        <button className="user-primary" onClick={() => navigate("/app/studio")} type="button"><Plus size={17} />新建任务</button>
      </PageHeader>
      <section className="user-filterbar" aria-label="素材筛选">
        <label><span>类型</span><select onChange={(event) => setKind(event.target.value)} value={kind}><option value="">全部类型</option><option value="original">原图</option><option value="result">处理结果</option><option value="mask">遮罩</option><option value="vector">矢量文件</option></select></label>
        <label><span>日期</span><input onChange={(event) => setDateFilter(event.target.value)} type="date" value={dateFilter} /></label>
        <label><span>任务</span><input onChange={(event) => setJobFilter(event.target.value)} placeholder="输入任务 ID" value={jobFilter} /></label>
        <label><span>版本链</span><input onChange={(event) => setVersionFilter(event.target.value)} placeholder="输入版本链 ID" value={versionFilter} /></label>
        <div className="user-view-toggle" aria-label="素材视图">
          <button aria-label="网格视图" className={view === "grid" ? "active" : ""} onClick={() => changeView("grid")} title="网格视图" type="button"><Grid2X2 size={17} /></button>
          <button aria-label="列表视图" className={view === "list" ? "active" : ""} onClick={() => changeView("list")} title="列表视图" type="button"><List size={18} /></button>
        </div>
      </section>
      {error && <InlineMessage tone="error">{error}</InlineMessage>}
      {!items ? <PageLoading label="正在载入素材" /> : filtered.length === 0 ? <EmptyState icon={Images} title="没有符合条件的素材" description="调整筛选条件，或从图片编辑器上传新素材。" /> : (
        <section className={`user-asset-collection ${view}`} aria-label="素材列表">
          {filtered.map((asset) => <AssetItem asset={asset} key={asset.id} onDelete={() => setDeleteTarget(asset)} onLineage={() => void showLineage(asset)} />)}
        </section>
      )}
      <Pagination pager={pager} />
      {lineage && <AssetLineageDrawer items={lineage} onClose={() => setLineage(null)} />}
      {deleteTarget && <ConfirmDialog title="删除素材" description={`删除“${deleteTarget.original_filename || operationName(deleteTarget.operation_code)}”后将进入回收期。运行中任务依赖的素材不能删除。`} confirmLabel="删除素材" danger onCancel={() => setDeleteTarget(null)} onConfirm={() => void removeAsset()} />}
    </>
  );
}

function AssetItem({
  asset,
  onDelete,
  onLineage,
}: {
  asset: Asset;
  onDelete: () => void;
  onLineage: () => void;
}) {
  return (
    <article className="user-asset-item">
      <button className="user-asset-preview" onClick={() => navigate(`/app/studio?source=${asset.id}`)} title="在编辑器中打开" type="button">
        <ImageThumbnail id={asset.id} vector={asset.mime_type === "image/svg+xml"} alt={asset.original_filename || "图片素材"} />
        <span>{asset.kind === "original" ? "原图" : asset.kind === "vector" ? "SVG" : "结果"}</span>
      </button>
      <div className="user-asset-copy">
        <strong>{asset.original_filename || operationName(asset.operation_code)}</strong>
        <small>{asset.width && asset.height ? `${asset.width} × ${asset.height} · ` : ""}{formatBytes(asset.size_bytes)}</small>
        <small>{dateTime(asset.created_at)}</small>
      </div>
      <div className="user-asset-actions">
        <button aria-label="查看版本链" onClick={onLineage} title="查看版本链" type="button"><History size={17} /></button>
        <button aria-label="下载素材" onClick={() => void downloadAsset(asset.id)} title="下载素材" type="button"><Download size={17} /></button>
        <button aria-label="删除素材" className="danger" onClick={onDelete} title="删除素材" type="button"><Trash2 size={17} /></button>
      </div>
    </article>
  );
}

function AssetLineageDrawer({ items, onClose }: { items: Asset[]; onClose: () => void }) {
  return (
    <div className="user-drawer-layer">
      <button aria-label="关闭版本链" className="user-drawer-scrim" onClick={onClose} type="button" />
      <aside className="user-drawer" aria-label="素材版本链">
        <header><span><small>素材关系</small><strong>版本链</strong></span><button aria-label="关闭" onClick={onClose} title="关闭" type="button"><X size={19} /></button></header>
        <div className="user-lineage-list">
          {items.map((asset, index) => (
            <article key={asset.id}><span>V{index + 1}</span><AssetThumb asset={asset} onClick={() => navigate(`/app/studio?source=${asset.id}`)} /><div><strong>{operationName(asset.operation_code)}</strong><small>{asset.width} × {asset.height}</small><small>{dateTime(asset.created_at)}</small></div></article>
          ))}
        </div>
      </aside>
    </div>
  );
}

function JobsPage() {
  const pageVisible = usePageVisible();
  const [status, setStatus] = useState("");
  const pager = useCursorPage(status, (cursor, limit) => api.jobs(status, { cursor, limit }));
  const { items, setItems, error, setError, load } = pager;
  const [selected, setSelected] = useState<ImageJob | null>(null);

  useEffect(() => {
    if (!pageVisible || !items?.some((item) => ["queued", "running", "retry_wait"].includes(item.status))) return;
    const timer = window.setInterval(() => void load(true), 4000);
    return () => window.clearInterval(timer);
  }, [items, load, pageVisible]);

  async function cancel(job: ImageJob) {
    try {
      const payload = await api.cancelJob(job.id);
      setItems((current) => current?.map((item) => item.id === job.id ? payload.job : item) || []);
      setSelected(payload.job);
    } catch (reason) {
      setError(messageOf(reason, "无法取消任务"));
    }
  }

  return (
    <>
      <PageHeader eyebrow="异步处理" title="任务中心" description="跟踪排队、处理、结果和积分退款状态。">
        <button className="user-primary" onClick={() => navigate("/app/studio")} type="button"><Plus size={17} />新建任务</button>
      </PageHeader>
      <div className="user-status-tabs" role="tablist" aria-label="任务状态">
        {[['', '全部'], ['queued', '排队'], ['running', '运行中'], ['succeeded', '成功'], ['failed', '失败'], ['refunded', '已退款']].map(([value, label]) => <button aria-selected={status === value} className={status === value ? "active" : ""} key={value} onClick={() => setStatus(value)} role="tab" type="button">{label}</button>)}
      </div>
      {error && <InlineMessage tone="error">{error}</InlineMessage>}
      {!items ? <PageLoading label="正在载入任务" /> : items.length === 0 ? <EmptyState icon={ListTodo} title="当前没有任务" description="新任务提交后会持续显示排队和处理进度。" /> : (
        <section className="user-job-table" aria-label="图片任务列表">
          <header><span>任务</span><span>状态</span><span>积分</span><span>时间</span><span>操作</span></header>
          {items.map((job) => (
            <article key={job.id}>
              <button className="user-job-name" onClick={() => setSelected(job)} type="button"><span className="user-job-thumbnail"><ImageThumbnail id={job.output_asset_id || job.source_asset_id} /></span><span><strong>{operationName(job.operation_code)}</strong><small>{job.id.slice(0, 8)}</small></span></button>
              <div><StatusBadge status={job.status} />{["queued", "running", "retry_wait"].includes(job.status) && <JobProgress job={job} compact />}</div>
              <strong className="user-job-points">-{job.charged_points}</strong>
              <time>{dateTime(job.created_at)}</time>
              <div className="user-row-actions"><button aria-label="查看任务详情" onClick={() => setSelected(job)} title="查看详情" type="button"><MoreHorizontal size={18} /></button>{job.status === "queued" && <button aria-label="取消任务" className="danger" onClick={() => void cancel(job)} title="取消任务" type="button"><XCircle size={18} /></button>}</div>
            </article>
          ))}
        </section>
      )}
      <Pagination pager={pager} />
      {selected && <JobDrawer job={items?.find((job) => job.id === selected.id) || selected} onCancel={cancel} onClose={() => setSelected(null)} />}
    </>
  );
}

function JobDrawer({ job: initialJob, onCancel, onClose }: { job: ImageJob; onCancel: (job: ImageJob) => Promise<void>; onClose: () => void }) {
  const [job, setJob] = useState(initialJob);
  const pageVisible = usePageVisible();
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => setJob(initialJob), [initialJob]);
  useEffect(() => {
    if (!pageVisible || !["queued", "running", "retry_wait"].includes(job.status)) return;
    let active = true;
    let timer: number;
    async function poll() {
      try { const value = await api.job(initialJob.id); if (active) setJob(value.job); }
      catch { /* The task list remains available; retry without stacking requests. */ }
      finally { if (active) timer = window.setTimeout(poll, 2500); }
    }
    void poll();
    return () => { active = false; window.clearTimeout(timer); };
  }, [initialJob.id, job.status, pageVisible]);
  const failed = ["failed", "timed_out", "cancelled"].includes(job.status);
  const outputIds = jobOutputIds(job);
  return (
    <div className="user-drawer-layer">
      <BusyDialog title={downloading ? "正在打包下载" : null} detail="正在整理整组图片，准备完成后自动开始下载" />
      {error && <InlineMessage tone="error">{error}</InlineMessage>}
      <button aria-label="关闭任务详情" className="user-drawer-scrim" onClick={onClose} type="button" />
      <aside className="user-drawer" aria-label="任务详情">
        <header><span><small>任务详情</small><strong>{operationName(job.operation_code)}</strong></span><button aria-label="关闭" onClick={onClose} title="关闭" type="button"><X size={19} /></button></header>
        <div className="user-job-detail-head"><StatusBadge status={job.status} /><code>{job.id}</code></div>
        <div className="user-job-preview-pair">{job.source_asset_id && <figure><ImageThumbnail id={job.source_asset_id} alt="处理前" /><figcaption>处理前</figcaption></figure>}{outputIds[0] && <figure><ImageThumbnail id={outputIds[0]} alt="处理后" /><figcaption>{outputIds.length > 1 ? `处理后 · ${outputIds.length} 张` : "处理后"}</figcaption></figure>}</div>
        {outputIds.length > 1 && <div className="user-job-output-list" aria-label="全部处理结果">{outputIds.map((id, index) => <ImageThumbnail id={id} alt={`结果 ${index + 1}`} key={id} />)}</div>}
        <JobProgress job={job} />
        <dl className="user-detail-list"><div><dt>处理进度</dt><dd>{job.progress}%</dd></div><div><dt>消耗积分</dt><dd>{job.charged_points}</dd></div><div><dt>尝试次数</dt><dd>{job.attempt_count}</dd></div><div><dt>退款状态</dt><dd>{job.refund_status === "refunded" ? "已退款" : "无退款"}</dd></div><div><dt>提交时间</dt><dd>{dateTime(job.created_at)}</dd></div><div><dt>完成时间</dt><dd>{dateTime(job.completed_at)}</dd></div></dl>
        {failed && <div className="user-failure-box"><AlertCircle size={18} /><span><strong>{job.error_message || (job.status === "cancelled" ? "任务已由你取消" : "图片处理未能完成")}</strong><small>{job.refund_status === "refunded" ? "本次消耗积分已自动退回。" : "系统正在核对退款状态。"}</small></span></div>}
        {job.status === "queued" && <button className="user-danger-button" onClick={() => void onCancel(job)} type="button"><XCircle size={17} />取消任务并退款</button>}
        {failed && job.source_asset_id && <button className="user-primary" onClick={() => navigate(`/app/studio?job=${encodeURIComponent(job.id)}`)} type="button"><RefreshCw size={17} />使用原素材重试</button>}
        {job.status === "succeeded" && outputIds[0] && <><button className="user-primary" onClick={() => navigate(`/app/studio?job=${encodeURIComponent(job.id)}`)} type="button"><ImagePlus size={17} />{job.source_asset_id ? "在编辑器中查看前后对比" : "在编辑器中打开结果"}</button>{outputIds.length > 1 && <button className="user-secondary" disabled={downloading} onClick={async () => { setDownloading(true); setError(""); try { await downloadJob(job.id); } catch (reason) { setError(messageOf(reason, "打包下载失败")); } finally { setDownloading(false); } }} type="button"><Download size={16} />下载整组结果</button>}</>}
      </aside>
    </div>
  );
}

function PointsPage({ bootstrap }: { bootstrap: BootstrapData }) {
  const [balance, setBalance] = useState(bootstrap.points);
  const [filter, setFilter] = useState("");
  const pager = useCursorPage(filter, (cursor, limit) => api.pointTransactions({ cursor, limit, category: filter }));
  const { items, error, setError } = pager;
  useEffect(() => {
    api.pointBalance().then((account) => setBalance((current) => ({ ...current, ...account.account }))).catch((reason) => setError(messageOf(reason)));
  }, []);
  const filtered = items || [];
  return (
    <>
      <PageHeader eyebrow="账户资产" title="积分余额与流水" description="每笔消费、退款、赠送和调整均可追溯到关联业务。" />
      <section className="user-points-summary"><div><span>当前可用积分</span><strong>{balance.balance.toLocaleString("zh-CN")}</strong><small>账户状态：{balance.status === "active" ? "正常" : "已冻结"}</small></div><div><span>累计获得</span><strong>{balance.lifetime_earned.toLocaleString("zh-CN")}</strong></div><div><span>累计消费</span><strong>{balance.lifetime_spent.toLocaleString("zh-CN")}</strong></div></section>
      <div className="user-status-tabs" role="tablist" aria-label="积分流水类型">{[["", "全部"], ["consume", "消费"], ["refund", "退款"], ["grant", "赠送"], ["adjust", "调整"]].map(([value, label]) => <button aria-selected={filter === value} className={filter === value ? "active" : ""} key={value} onClick={() => setFilter(value)} role="tab" type="button">{label}</button>)}</div>
      {error && <InlineMessage tone="error">{error}</InlineMessage>}
      {!items ? <PageLoading label="正在载入积分流水" /> : filtered.length === 0 ? <EmptyState icon={Coins} title="暂无积分流水" description="积分消费和账户变化会记录在这里。" /> : (
        <section className="user-ledger" aria-label="积分流水">
          {filtered.map((item) => <article key={item.id}><span className={`user-ledger-icon ${item.delta >= 0 ? "positive" : "negative"}`}><Coins size={17} /></span><div><strong>{pointLabel(item.entry_type)}</strong><small>{item.description} · {item.reference_type} {item.reference_id.slice(0, 8)}</small></div><strong className={item.delta >= 0 ? "positive" : "negative"}>{item.delta > 0 ? "+" : ""}{item.delta}</strong><span><small>余额</small><strong>{item.balance_after}</strong></span><time>{dateTime(item.created_at)}</time></article>)}
        </section>
      )}
      <Pagination pager={pager} />
    </>
  );
}

function pointLabel(type: string): string {
  return { consume: "任务消费", refund: "失败退款", grant: "积分赠送", adjust: "人工调整", renewal: "会员发放", promotion: "活动赠送", reversal: "交易冲正" }[type] || "积分变动";
}

function MembershipPage({ bootstrap }: { bootstrap: BootstrapData }) {
  const [plans, setPlans] = useState<MembershipPlan[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api.membershipPlans().then((payload) => setPlans(payload.items)).catch((reason) => { setError(messageOf(reason)); setPlans([]); });
  }, []);
  const entitlement = bootstrap.membership.entitlements;
  return (
    <>
      <PageHeader eyebrow="会员账户" title="会员与权益" description="当前套餐、有效期和工作台资源限制。" />
      <section className="user-current-plan">
        <div className="user-plan-mark"><BadgeCheck size={28} /></div>
        <div><span>当前方案</span><h2>{bootstrap.membership.plan.name}</h2><p>{bootstrap.membership.ends_at ? `有效至 ${dateTime(bootstrap.membership.ends_at)}` : "当前方案长期有效"}</p></div>
        <StatusBadge status={bootstrap.membership.status} label={bootstrap.membership.status === "active" ? "生效中" : bootstrap.membership.status} />
      </section>
      <section className="user-entitlements" aria-label="当前会员权益">
        <article><Clock3 size={20} /><span><strong>{entitlement.max_concurrent_jobs}</strong><small>并发任务</small></span></article>
        <article><Upload size={20} /><span><strong>{entitlement.max_upload_mb} MB</strong><small>单次上传</small></span></article>
        <article><FileImage size={20} /><span><strong>{entitlement.max_image_megapixels} MP</strong><small>图片像素</small></span></article>
        <article><History size={20} /><span><strong>{entitlement.retention_days} 天</strong><small>素材保留</small></span></article>
        <article><Coins size={20} /><span><strong>{entitlement.periodic_points}</strong><small>周期积分</small></span></article>
        <article><BadgeCheck size={20} /><span><strong>{Math.round((1 - entitlement.discount_bps / 10000) * 100)}%</strong><small>操作优惠</small></span></article>
      </section>
      <SectionHeader icon={BadgeCheck} title="可用方案" />
      {error && <InlineMessage tone="error">{error}</InlineMessage>}
      {!plans ? <PageLoading label="正在载入会员方案" /> : (
        <section className="user-plan-list">
          {plans.map((plan) => {
            const current = plan.code === bootstrap.membership.plan.code;
            return <article className={current ? "current" : ""} key={plan.id}><header><span><strong>{plan.name}</strong><small>{plan.description}</small></span>{current && <span className="user-current-label"><Check size={14} />当前方案</span>}</header><dl><div><dt>周期积分</dt><dd>{plan.periodic_points}</dd></div><div><dt>任务并发</dt><dd>{plan.max_concurrent_jobs}</dd></div><div><dt>上传限制</dt><dd>{plan.max_upload_mb} MB</dd></div><div><dt>素材保留</dt><dd>{plan.asset_retention_days} 天</dd></div></dl></article>;
          })}
        </section>
      )}
      <p className="user-membership-footnote">会员变更由运营人员配置。当前版本未接入在线购买，不会展示无效的购买入口。</p>
    </>
  );
}

function ProfilePage({
  bootstrap,
  onBootstrap,
}: {
  bootstrap: BootstrapData;
  onBootstrap: (value: BootstrapData) => void;
}) {
  const [displayName, setDisplayName] = useState(bootstrap.user.display_name);
  const [username, setUsername] = useState(bootstrap.user.username || "");
  const [theme, setTheme] = useState(bootstrap.preferences.theme);
  const [notifications, setNotifications] = useState<Record<string, boolean>>({
    task_completed: bootstrap.preferences.notification_preferences.task_completed ?? true,
    task_failed: bootstrap.preferences.notification_preferences.task_failed ?? true,
    points_changed: bootstrap.preferences.notification_preferences.points_changed ?? true,
    membership_changed: bootstrap.preferences.notification_preferences.membership_changed ?? true,
  });
  const [sessions, setSessions] = useState<SessionInfo[] | null>(null);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState<{ tone: "error" | "success"; text: string } | null>(null);

  const loadSessions = useCallback(() => {
    api.sessions().then((payload) => setSessions(payload.items)).catch((reason) => setMessage({ tone: "error", text: messageOf(reason) }));
  }, []);
  useEffect(loadSessions, [loadSessions]);

  async function saveProfile(event: FormEvent) {
    event.preventDefault();
    setBusy("profile");
    setMessage(null);
    try {
      const payload = await api.updateProfile({ display_name: displayName.trim(), username: username.trim() || null });
      onBootstrap({ ...bootstrap, user: { ...bootstrap.user, ...payload.user } });
      setMessage({ tone: "success", text: "个人资料已更新。" });
    } catch (reason) {
      setMessage({ tone: "error", text: messageOf(reason) });
    } finally {
      setBusy("");
    }
  }

  async function savePreferences() {
    setBusy("preferences");
    setMessage(null);
    try {
      const payload = await api.updatePreferences({ theme, notification_preferences: notifications });
      onBootstrap({ ...bootstrap, preferences: payload.preferences });
      const dark = theme === "dark" || (theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
      document.documentElement.dataset.userTheme = dark ? "dark" : "light";
      setMessage({ tone: "success", text: "界面与通知偏好已保存。" });
    } catch (reason) {
      setMessage({ tone: "error", text: messageOf(reason) });
    } finally {
      setBusy("");
    }
  }

  async function changePassword(event: FormEvent) {
    event.preventDefault();
    setBusy("password");
    setMessage(null);
    try {
      await api.changePassword(currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      setMessage({ tone: "success", text: "密码已更新，其他设备的登录已退出。" });
      loadSessions();
    } catch (reason) {
      setMessage({ tone: "error", text: messageOf(reason) });
    } finally {
      setBusy("");
    }
  }

  async function revokeSession(id: string) {
    try {
      await api.revokeSession(id);
      setSessions((items) => items?.filter((item) => item.id !== id) || []);
    } catch (reason) {
      setMessage({ tone: "error", text: messageOf(reason) });
    }
  }

  async function logout() {
    await api.logout().catch(() => undefined);
    navigate("/login");
  }

  return (
    <>
      <PageHeader eyebrow="账号设置" title="个人资料与安全" description="管理资料、界面偏好、密码和已登录设备。" />
      {message && <InlineMessage tone={message.tone}>{message.text}</InlineMessage>}
      <div className="user-settings-layout">
        <section className="user-settings-section">
          <SectionHeader icon={UserRound} title="个人资料" />
          <form className="user-settings-form" onSubmit={(event) => void saveProfile(event)}>
            <label className="user-field"><span>显示名称</span><input maxLength={120} onChange={(event) => setDisplayName(event.target.value)} required value={displayName} /></label>
            <label className="user-field"><span>用户名</span><input maxLength={64} onChange={(event) => setUsername(event.target.value)} placeholder="可选" value={username} /></label>
            <label className="user-field"><span>登录邮箱</span><input disabled value={bootstrap.user.email} /></label>
            <button className="user-primary" disabled={busy === "profile"} type="submit">{busy === "profile" ? <LoaderCircle className="spin" size={17} /> : <Check size={17} />}保存资料</button>
          </form>
        </section>
        <section className="user-settings-section">
          <SectionHeader icon={theme === "dark" ? Moon : Sun} title="界面与通知" />
          <div className="user-settings-form">
            <Segmented label="界面主题" value={theme} options={[["light", "浅色"], ["dark", "深色"], ["system", "跟随系统"]]} onChange={(value) => setTheme(value as typeof theme)} />
            <div className="user-toggle-list">
              {[["task_completed", "任务完成"], ["task_failed", "任务失败"], ["points_changed", "积分变化"], ["membership_changed", "会员变化"]].map(([key, label]) => <label key={key}><span>{label}</span><input checked={notifications[key]} onChange={(event) => setNotifications({ ...notifications, [key]: event.target.checked })} type="checkbox" /></label>)}
            </div>
            <button className="user-primary" disabled={busy === "preferences"} onClick={() => void savePreferences()} type="button">{busy === "preferences" ? <LoaderCircle className="spin" size={17} /> : <Check size={17} />}保存偏好</button>
          </div>
        </section>
        <section className="user-settings-section">
          <SectionHeader icon={LockKeyhole} title="修改密码" />
          <form className="user-settings-form" onSubmit={(event) => void changePassword(event)}>
            <label className="user-field"><span>当前密码</span><input autoComplete="current-password" onChange={(event) => setCurrentPassword(event.target.value)} required type="password" value={currentPassword} /></label>
            <label className="user-field"><span>新密码</span><input autoComplete="new-password" minLength={12} onChange={(event) => setNewPassword(event.target.value)} required type="password" value={newPassword} /><small>至少 12 位，并包含大小写字母、数字和符号。</small></label>
            <button className="user-secondary" disabled={busy === "password"} type="submit">{busy === "password" ? <LoaderCircle className="spin" size={17} /> : <LockKeyhole size={17} />}更新密码</button>
          </form>
        </section>
        <section className="user-settings-section user-device-section">
          <SectionHeader icon={MonitorSmartphone} title="登录设备" />
          {!sessions ? <MiniLoading /> : <div className="user-device-list">{sessions.map((session) => <article key={session.id}><MonitorSmartphone size={19} /><span><strong>{session.user_agent || "未知设备"}{session.current && <em>当前设备</em>}</strong><small>最近活动 {dateTime(session.last_seen_at)} · 到期 {dateTime(session.expires_at)}</small></span>{!session.current && <button aria-label="退出此设备" onClick={() => void revokeSession(session.id)} title="退出此设备" type="button"><LogOut size={17} /></button>}</article>)}</div>}
          <button className="user-danger-link" onClick={() => void logout()} type="button"><LogOut size={17} />退出当前账号</button>
        </section>
      </div>
    </>
  );
}

function QuoteDialog({
  balance,
  busy,
  error,
  operation,
  quote,
  onCancel,
  onConfirm,
}: {
  balance: number;
  busy: boolean;
  error?: string;
  operation?: Operation;
  quote: Quote;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const insufficient = balance < quote.final_points;
  return (
    <div className="user-modal-layer" role="presentation">
      <section aria-labelledby="quote-title" aria-modal="true" className="user-modal" role="dialog">
        <header><span><small>提交前确认</small><h2 id="quote-title">任务报价</h2></span><button aria-label="关闭报价" disabled={busy} onClick={onCancel} title="关闭" type="button"><X size={19} /></button></header>
        <div className="user-quote-operation"><span className="user-operation-icon"><Sparkles size={19} /></span><span><strong>{operation?.name || operationName(quote.operation_code)}</strong><small>报价在 {dateTime(quote.expires_at)} 前有效</small></span></div>
        <dl className="user-quote-lines"><div><dt>基础积分</dt><dd>{quote.base_points}</dd></div><div><dt>会员优惠</dt><dd>-{quote.discount_points}</dd></div>{quote.surcharge_points > 0 && <div><dt>参数附加</dt><dd>+{quote.surcharge_points}</dd></div>}<div className="total"><dt>本次需要</dt><dd>{quote.final_points} 积分</dd></div><div><dt>当前余额</dt><dd>{balance} 积分</dd></div></dl>
        {insufficient && <InlineMessage tone="warning">还差 {quote.final_points - balance} 积分，当前无法提交任务。可前往积分流水查看账户变化。</InlineMessage>}
        {error && <InlineMessage tone="error">{error}</InlineMessage>}
        <footer><button className="user-secondary" disabled={busy} onClick={onCancel} type="button">返回编辑</button>{insufficient ? <button className="user-primary" onClick={() => navigate("/app/points")} type="button"><Coins size={17} />查看积分</button> : <button className="user-primary" disabled={busy} onClick={onConfirm} type="button">{busy ? <LoaderCircle className="spin" size={17} /> : <Check size={17} />}{error ? "重试提交" : "确认提交"}</button>}</footer>
      </section>
    </div>
  );
}

function PreviewBackgroundControls({
  color,
  mode,
  onColor,
  onImage,
  onMode,
  previewRef,
}: {
  color: string;
  mode: "transparent" | "white" | "dark" | "color" | "image";
  onColor: (value: string) => void;
  onImage: (file?: File) => void;
  onMode: (value: "transparent" | "white" | "dark" | "color" | "image") => void;
  previewRef: React.RefObject<HTMLInputElement | null>;
}) {
  return (
    <section className="user-preview-controls">
      <header><span>预览背景</span><em>仅本地预览</em></header>
      <div>{[["transparent", "透明"], ["white", "白色"], ["dark", "深色"]].map(([value, label]) => <button className={mode === value ? "active" : ""} key={value} onClick={() => onMode(value as typeof mode)} type="button"><i className={`preview-${value}`} />{label}</button>)}</div>
      <label className={mode === "color" ? "active" : ""}><input aria-label="自定义背景色" onChange={(event) => { onColor(event.target.value); onMode("color"); }} type="color" value={color} /><span>自定义色</span></label>
      <button className={mode === "image" ? "active" : ""} onClick={() => previewRef.current?.click()} type="button"><ImagePlus size={16} />本地背景图</button>
      <input accept="image/*" hidden onChange={(event) => { onImage(event.target.files?.[0]); event.currentTarget.value = ""; }} ref={previewRef} type="file" />
    </section>
  );
}

function ConfirmDialog({
  title,
  description,
  confirmLabel,
  danger = false,
  onCancel,
  onConfirm,
}: {
  title: string;
  description: string;
  confirmLabel: string;
  danger?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return <div className="user-modal-layer"><section aria-labelledby="confirm-title" aria-modal="true" className="user-modal compact" role="dialog"><header><h2 id="confirm-title">{title}</h2><button aria-label="关闭" onClick={onCancel} title="关闭" type="button"><X size={19} /></button></header><p>{description}</p><footer><button className="user-secondary" onClick={onCancel} type="button">取消</button><button className={danger ? "user-danger-button" : "user-primary"} onClick={onConfirm} type="button">{danger && <Trash2 size={17} />}{confirmLabel}</button></footer></section></div>;
}

function Segmented({ label, value, options, onChange }: { label: string; value: string; options: string[][]; onChange: (value: string) => void }) {
  return <div className="user-field"><span>{label}</span><div className="user-segmented">{options.map(([option, optionLabel]) => <button aria-pressed={value === option} className={value === option ? "active" : ""} key={option} onClick={() => onChange(option)} type="button">{optionLabel}</button>)}</div></div>;
}

function StatusBadge({ status, label }: { status: string; label?: string }) {
  const item = JOB_STATUS[status] || { label: label || status, tone: status === "active" ? "success" : "muted" };
  const Icon = item.tone === "success" ? CheckCircle2 : item.tone === "danger" ? XCircle : item.tone === "running" ? LoaderCircle : Clock3;
  return <span className={`user-status ${item.tone}`}><Icon className={item.tone === "running" ? "spin" : ""} size={14} />{label || item.label}</span>;
}

function JobRow({ job }: { job: ImageJob }) {
  return <button onClick={() => navigate("/app/jobs")} type="button"><span className="user-job-thumbnail"><ImageThumbnail id={job.output_asset_id || job.source_asset_id} /></span><span><strong>{operationName(job.operation_code)}</strong><small>{dateTime(job.created_at)}</small></span><StatusBadge status={job.status} /><ArrowRight size={15} /></button>;
}

function useSignedAssetUrl(assetId: string | null, revision = 0, onError?: (reason: unknown) => void): string | null {
  const [url, setUrl] = useState<string | null>(null);
  const errorRef = useRef(onError);
  errorRef.current = onError;
  useEffect(() => {
    let active = true;
    let timer: number;
    setUrl(null);
    if (!assetId) return;
    const id = assetId;
    async function refresh() {
      try {
        const payload = await api.downloadUrl(id);
        if (!active) return;
        setUrl(payload.url);
        const remaining = Date.parse(payload.expires_at) - Date.now();
        timer = window.setTimeout(refresh, Math.min(600000, Math.max(10000, (Number.isFinite(remaining) ? remaining : 600000) * .8)));
      } catch (reason) {
        if (!active) return;
        errorRef.current?.(reason);
      }
    }
    void refresh();
    return () => { active = false; window.clearTimeout(timer); };
  }, [assetId, revision]);
  return url;
}

function AssetThumb({ asset, label, onClick }: { asset: Asset; label?: string; onClick: () => void }) {
  const [preview, setPreview] = useState<{ left: number; top: number } | null>(null);
  function show(target: HTMLButtonElement) {
    if (!label) return;
    const rect = target.getBoundingClientRect();
    setPreview({ left: Math.max(12, Math.min(rect.left + rect.width / 2 - 130, window.innerWidth - 272)), top: Math.max(12, rect.top - 294) });
  }
  useEffect(() => { if (!preview) return; const close = () => setPreview(null); window.addEventListener("scroll", close, true); window.addEventListener("resize", close); return () => { window.removeEventListener("scroll", close, true); window.removeEventListener("resize", close); }; }, [preview]);
  return <><button className="user-asset-thumb" onClick={() => { setPreview(null); onClick(); }} onMouseEnter={(event) => show(event.currentTarget)} onMouseLeave={() => setPreview(null)} onFocus={(event) => show(event.currentTarget)} onBlur={() => setPreview(null)} aria-label={`${label || ""} ${operationName(asset.operation_code)}`} type="button"><ImageThumbnail id={asset.id} vector={asset.mime_type === "image/svg+xml"} alt={operationName(asset.operation_code)} />{label && <span>{label}</span>}</button>{preview && createPortal(<div className="user-version-preview" style={preview} role="tooltip"><ImageThumbnail id={asset.id} vector={asset.mime_type === "image/svg+xml"} /><strong>{label} · {operationName(asset.operation_code)}</strong><small>{asset.width} × {asset.height}</small></div>, document.body)}</>;
}

async function downloadAsset(assetId: string): Promise<void> {
  const payload = await api.downloadUrl(assetId);
  const anchor = document.createElement("a");
  anchor.href = payload.url;
  anchor.rel = "noopener";
  anchor.click();
}

function PageHeader({ eyebrow, title, description, children }: { eyebrow: string; title: string; description: string; children?: ReactNode }) {
  return <header className="user-page-header"><div><span>{eyebrow}</span><h1>{title}</h1><p>{description}</p></div>{children && <aside>{children}</aside>}</header>;
}

function SectionHeader({ icon: Icon, title, action }: { icon: ComponentType<{ size?: number }>; title: string; action?: ReactNode }) {
  return <header className="user-section-header"><span><Icon size={17} /><strong>{title}</strong></span>{action}</header>;
}

function InlineMessage({ tone, children }: { tone: "error" | "success" | "warning"; children: ReactNode }) {
  return <ToastMessage tone={tone}>{children}</ToastMessage>;
}

function EmptyState({ icon: Icon, title, description, compact = false }: { icon: ComponentType<{ size?: number }>; title: string; description: string; compact?: boolean }) {
  return <div className={`user-empty${compact ? " compact" : ""}`}><Icon size={compact ? 24 : 30} /><strong>{title}</strong><p>{description}</p></div>;
}

function Brand({ inverse = false }: { inverse?: boolean }) {
  const branding = useSiteBranding();
  return <a href="/" aria-label={`${branding.site_name} · 网站首页`} className={`user-brand${inverse ? " inverse" : ""}`}><img src={branding.logo_url} width="34" height="34" alt="" /><strong>{branding.site_name}</strong></a>;
}

function BrandArtwork({ place, className }: { place: "login" | "register" | "home"; className: string }) {
  const branding = useSiteBranding();
  const labels = { login: "印花服装产品摄影", register: "杯子与帆布袋产品摄影", home: "服装与杯子产品摄影" };
  return <img className={className} src={branding[`${place}_image_url`]} alt={labels[place]} fetchPriority="high" decoding="async" />;
}

function Avatar({ name }: { name: string }) {
  const label = name.trim().slice(0, 1).toLocaleUpperCase("zh-CN") || "U";
  return <span className="user-avatar" aria-hidden="true">{label}</span>;
}

function AppLoading() {
  return <main className="user-boot"><Brand /><div><LoaderCircle className="spin" size={20} /><strong>正在载入工作台</strong></div></main>;
}

function PageLoading({ label }: { label: string }) {
  return <div className="user-page-loading"><LoaderCircle className="spin" size={24} /><strong>{label}</strong></div>;
}

function MiniLoading() {
  return <div className="user-mini-loading" role="status"><LoaderCircle className="spin" size={19} />载入中</div>;
}

function FullPageState({
  icon: Icon,
  title,
  description,
  action,
}: {
  icon: ComponentType<{ size?: number }>;
  title: string;
  description: string;
  action?: () => void | Promise<void>;
}) {
  return <main className="user-full-state"><span><Icon size={27} /></span><h1>{title}</h1><p>{description}</p>{action && <button className="user-primary" onClick={() => void action()} type="button"><RefreshCw size={17} />重试</button>}</main>;
}

function ForbiddenState({ title, description }: { title: string; description: string }) {
  return <EmptyState icon={ShieldCheck} title={title} description={description} />;
}

export default UserApp;
