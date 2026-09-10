import { type FormEvent, type ReactNode, useEffect, useRef, useState } from "react";
import { CircleAlert, RefreshCw, Save, X } from "lucide-react";
import { apiRequest, jsonObject, objectValue, type AdminRow as Row } from "./admin-api";

export type EditorKind = "settings" | "pricing" | "memberships" | "points" | "roles" | "users" | "user-membership" | "user-roles";
interface EditorProps {
  row: Row;
  permissions: Set<string>;
  onClose: () => void;
  onSaved: (message: string) => void;
}

function EditorForm({ title, children, onClose, onSubmit, label = "保存", disabled = false, extra }: {
  title: string; children: ReactNode; onClose: () => void; onSubmit: () => Promise<void>;
  label?: string; disabled?: boolean; extra?: ReactNode;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const inFlight = useRef(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true); setError("");
    try { await onSubmit(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "保存失败，请重试"); }
    finally { inFlight.current = false; setBusy(false); }
  }
  return <div className="admin-drawer-layer">
    <button className="admin-drawer-scrim" aria-label="关闭编辑" disabled={busy} onClick={onClose} />
    <form className="admin-detail-drawer admin-editor" role="dialog" aria-modal="true" aria-label={title} onSubmit={submit}>
      <header><div><p>管理后台</p><h2>{title}</h2></div><button type="button" className="admin-icon-button" aria-label="关闭编辑" disabled={busy} onClick={onClose}><X size={18} /></button></header>
      <div className="admin-drawer-content"><fieldset disabled={busy || disabled}>{children}</fieldset></div>
      {error && <div className="admin-form-error" role="alert"><CircleAlert size={16} />{error}</div>}
      <footer>{extra}<button type="button" className="admin-secondary-button" disabled={busy} onClick={onClose}>关闭</button><button className="admin-primary-button" disabled={busy || disabled}>{busy ? <RefreshCw size={15} className="spin" /> : <Save size={15} />}{busy ? "正在保存…" : label}</button></footer>
    </form>
  </div>;
}

interface FieldDefinition {
  key: string; label: string; type?: "text" | "number" | "checkbox" | "password" | "textarea" | "datetime-local";
  min?: number; max?: number; step?: number; hint?: string; options?: Array<[string, string]>; required?: boolean;
}

function Field({ field, value, onChange, disabled = false }: { field: FieldDefinition; value: unknown; onChange: (value: unknown) => void; disabled?: boolean }) {
  const { type = "text" } = field;
  return <label className={`admin-editor-field ${type === "checkbox" ? "is-checkbox" : ""}`}>
    <span>{field.label}</span>
    {field.options ? <select value={String(value ?? "")} disabled={disabled} onChange={(event) => onChange(event.target.value)}>{field.options.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select>
      : type === "textarea" ? <textarea value={String(value ?? "")} disabled={disabled} onChange={(event) => onChange(event.target.value)} />
      : type === "checkbox" ? <input type="checkbox" checked={Boolean(value)} disabled={disabled} onChange={(event) => onChange(event.target.checked)} />
      : <input type={type} value={String(value ?? "")} disabled={disabled} autoComplete={type === "password" ? "new-password" : "off"} min={field.min} max={field.max} step={field.step ?? (type === "number" ? 1 : undefined)} required={field.required ?? type !== "password"} onChange={(event) => onChange(type === "number" && event.target.value !== "" ? Number(event.target.value) : event.target.value)} />}
    {field.hint && <small>{field.hint}</small>}
  </label>;
}

function Fields({ fields, values, setValues }: { fields: FieldDefinition[]; values: Row; setValues: (value: Row) => void }) {
  return <div className="admin-editor-grid">{fields.map((field) => <Field key={field.key} field={field} value={values[field.key]} onChange={(value) => setValues({ ...values, [field.key]: value })} />)}</div>;
}

const CONFIG_FIELDS: Record<string, FieldDefinition[]> = {
  branding: [
    { key: "site_name", label: "网站名称" },
    { key: "logo_url", label: "Logo 图片地址", hint: "上传图片或填写 HTTPS 地址" },
    { key: "login_image_url", label: "登录页配图地址" },
    { key: "register_image_url", label: "注册页配图地址" },
    { key: "home_image_url", label: "首页配图地址" },
  ],
  sub2api: [
    { key: "enabled", label: "启用 Sub2API", type: "checkbox" },
    { key: "base_url", label: "接口地址", hint: "例如 https://api.example.com/v1", required: false },
    { key: "image_model", label: "图片模型" },
    { key: "timeout_seconds", label: "请求超时（秒）", type: "number", min: 1, max: 600 },
  ],
  r2: [
    { key: "enabled", label: "启用 R2 存储", type: "checkbox" },
    { key: "endpoint_url", label: "S3 API 地址", hint: "https://<Account ID>.r2.cloudflarestorage.com", required: false },
    { key: "account_id", label: "Account ID", required: false },
    { key: "bucket", label: "存储桶名称（Bucket）", required: false },
    { key: "region", label: "区域", hint: "Cloudflare R2 使用 auto" },
  ],
  email: [
    { key: "enabled", label: "启用邮件服务", type: "checkbox" },
    { key: "provider", label: "邮件服务类型", options: [["smtp", "SMTP"], ["api", "邮件 API"]] },
    { key: "host", label: "SMTP 主机", required: false },
    { key: "port", label: "SMTP 端口", type: "number", min: 1, max: 65535, hint: "465 自动使用 SSL；587 通常开启 STARTTLS" },
    { key: "username", label: "SMTP 用户名", required: false },
    { key: "use_tls", label: "使用 STARTTLS", type: "checkbox" },
    { key: "api_base_url", label: "邮件 API 地址", required: false, hint: "完整发信接口地址。POST JSON 字段为 from、to（数组）、subject、text；使用 Bearer API Key。" },
    { key: "from_email", label: "发件人邮箱", required: false },
  ],
  general: [
    { key: "registration_enabled", label: "开放用户注册", type: "checkbox", hint: "开启后登录页显示注册入口；关闭后已有账号仍可登录，管理员仍可邀请用户。" },
    { key: "default_membership_code", label: "新用户默认会员代码", hint: "填写已启用的会员套餐代码，例如 free" },
    { key: "default_points", label: "新用户赠送积分", type: "number", min: 0, max: 1000000 },
    { key: "max_upload_mb", label: "上传大小上限（MB）", type: "number", min: 1, max: 100 },
    { key: "max_image_megapixels", label: "图片像素上限（百万像素）", type: "number", min: 1, max: 200 },
    { key: "signed_url_ttl_seconds", label: "下载链接有效期（秒）", type: "number", min: 300, max: 900 },
    { key: "task_concurrency", label: "任务并发上限", type: "number", min: 1, max: 64 },
  ],
};
const SECRET_FIELDS: Record<string, Array<[string, string]>> = {
  sub2api: [["api_key", "API Key"]],
  r2: [["access_key_id", "Access Key ID"], ["secret_access_key", "Secret Access Key"]],
  email: [["password", "SMTP 密码"], ["api_key", "邮件 API Key"]],
  general: [],
};

function SiteImageUpload({ label, url, onUploaded }: { label: string; url: string; onUploaded: (url: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return <div className="admin-brand-upload"><img src={url} alt={`${label}预览`} loading="lazy" /><label>{busy ? "上传中…" : `上传${label}`}<input type="file" accept="image/png,image/jpeg,image/webp" disabled={busy} onChange={(event) => {
    const file = event.target.files?.[0]; if (!file) return;
    event.target.value = ""; setBusy(true); setError("");
    const body = new FormData(); body.append("image", file);
    void apiRequest<{ url: string }>("/api/v1/admin/site-media", { method: "POST", body }).then((result) => onUploaded(result.url)).catch((reason: Error) => setError(reason.message)).finally(() => setBusy(false));
  }} /></label><small>PNG / JPEG / WebP，最大 12 MB。品牌配图公开展示，保存配置后生效。</small>{error && <p role="alert">{error}</p>}</div>;
}

function ConfigEditor({ row, permissions, onClose, onSaved }: EditorProps) {
  const code = String(row.code);
  const active = objectValue(row.active);
  const [values, setValues] = useState<Row>({ ...objectValue(row.defaults), ...objectValue(active.values) });
  const [secrets, setSecrets] = useState<Row>({});
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState("");
  const [history, setHistory] = useState<Row[] | null>(null);
  const [historyError, setHistoryError] = useState("");
  const dirty = JSON.stringify(values) !== JSON.stringify({ ...objectValue(row.defaults), ...objectValue(active.values) }) || Object.values(secrets).some(Boolean);
  const fields = (CONFIG_FIELDS[code] || []).filter((field) => code !== "email" ||
    (values.provider === "api" ? !["host", "port", "username", "use_tls"].includes(field.key) : field.key !== "api_base_url"));
  async function testConnection() {
    setTesting(true); setTestResult("");
    try {
      const result = await apiRequest<{ status: string; message: string }>(`/api/v1/admin/config/${code}/test`, { method: "POST" });
      setTestResult(`${result.status === "succeeded" ? "连接正常" : "连接失败"}：${result.message}`);
    } catch (caught) { setTestResult(caught instanceof Error ? caught.message : "连接测试失败"); }
    finally { setTesting(false); }
  }
  return <EditorForm title={`配置 ${String(row.name)}`} label="保存并生效" onClose={onClose} disabled={testing} onSubmit={async () => {
    await apiRequest(`/api/v1/admin/config/${code}`, { method: "PUT", body: JSON.stringify({ values, secrets: Object.fromEntries(Object.entries(secrets).filter(([, value]) => String(value).trim())), base_version: row.active_version ?? null }) });
    if (code === "branding") window.dispatchEvent(new Event("site-branding-updated"));
    onSaved("配置已保存并生效");
  }}>
    <p className="admin-editor-note">填写配置后点击“保存并生效”。密钥留空保留原值，保存后可测试连接。</p>
    {code === "general" && <p className="admin-editor-note">公开注册必须先通过邮箱验证码验证。请在“邮件服务”配置发信渠道；验证成功后获得普通用户权限、默认会员和赠送积分。邮件自助找回暂未开放。</p>}
    <Fields fields={fields} values={values} setValues={setValues} />
    {code === "branding" && <div className="admin-brand-uploads">{[["logo_url", "Logo"], ["login_image_url", "登录页配图"], ["register_image_url", "注册页配图"], ["home_image_url", "首页配图"]].map(([key, label]) => <SiteImageUpload key={key} label={label} url={String(values[key] || "")} onUploaded={(url) => setValues((current) => ({ ...current, [key]: url }))} />)}</div>}
    {(SECRET_FIELDS[code] || []).filter(([key]) => code !== "email" || key === (values.provider === "api" ? "api_key" : "password")).map(([key, label]) => {
      const stored = objectValue(objectValue(active.secrets)[key]);
      return <Field key={key} field={{ key, label, type: "password", hint: stored.has_value ? `已设置（末尾 ${stored.last_four}），留空保留` : "尚未设置" }} value={secrets[key]} onChange={(value) => setSecrets({ ...secrets, [key]: value })} />;
    })}
    {permissions.has("config.test") && !["general", "branding"].includes(code) && <div className="admin-editor-section"><button type="button" className="admin-secondary-button" disabled={testing || !row.active_version || dirty} onClick={() => void testConnection()}>{testing ? "正在测试…" : "测试已保存的连接"}</button>{(!row.active_version || dirty) && <small>请先保存当前配置。</small>}{testResult && <p role="status">{testResult}</p>}</div>}
    <details className="admin-editor-section" onToggle={(event) => { if (event.currentTarget.open && history === null) void apiRequest<{ items: Row[] }>(`/api/v1/admin/config/${code}/history`).then((result) => { setHistory(result.items); setHistoryError(""); }).catch((error: Error) => setHistoryError(error.message)); }}>
      <summary>配置修改历史</summary>{historyError && <p role="alert">{historyError}</p>}{history?.map((item) => <p key={String(item.id)}>v{String(item.version)} · {item.status === "active" ? "当前生效" : item.status === "draft" ? "未发布草稿" : "历史版本"} · {String(item.change_reason)}</p>)}{history?.length === 0 && <p>暂无修改记录</p>}
    </details>
  </EditorForm>;
}

function PricingEditor({ row, onClose, onSaved }: EditorProps) {
  const current = objectValue(row.current_price);
  const [values, setValues] = useState<Row>({ name: row.name, enabled: row.enabled, timeout_seconds: row.timeout_seconds, max_attempts: row.max_attempts, base_points: current.base_points ?? 0 });
  const [rules, setRules] = useState(JSON.stringify(current.parameter_rules || {}, null, 2));
  const ai = String(row.code).startsWith("ai.");
  const existingRules = objectValue(current.parameter_rules).rules as Row[] | undefined;
  const quality = objectValue(existingRules?.find((rule) => rule.parameter === "quality")?.points);
  const [standard, setStandard] = useState(Number(quality.medium || 0));
  const [fine, setFine] = useState(Number(quality.high ?? Math.max(1, Math.ceil(Number(current.base_points || 0) / 2))));
  return <EditorForm title={`设置价格 · ${String(row.name)}`} label="保存价格与开关" onClose={onClose} onSubmit={async () => {
    const parameterRules = jsonObject(rules, "参数附加计价");
    if (ai) {
      if (fine <= standard) throw new Error("精细质量附加积分必须高于标准");
      parameterRules.rules = [...((parameterRules.rules || []) as Row[]).filter((rule) => rule.parameter !== "quality"), { parameter: "quality", type: "choice", points: { low: Number(quality.low || 0), medium: standard, high: fine, auto: fine } }];
    }
    await apiRequest(`/api/v1/admin/operations/${encodeURIComponent(String(row.code))}/configuration`, { method: "PUT", body: JSON.stringify({ ...values, parameter_rules: parameterRules, reason: "管理员修改操作价格与配置" }) });
    onSaved("价格与操作配置已生效");
  }}>
    <p className="admin-editor-note">单价以积分计。会员折扣作用于基础积分，参数附加积分单独计收；新价格适用于新的任务报价。</p>
    <Fields values={values} setValues={setValues} fields={[
      { key: "name", label: "操作名称" }, { key: "enabled", label: "启用此操作", type: "checkbox" },
      { key: "base_points", label: "基础积分单价", type: "number", min: 0, max: 1000000000 },
      { key: "timeout_seconds", label: "任务超时（秒）", type: "number", min: 30, max: 3600 },
      { key: "max_attempts", label: "最多尝试次数", type: "number", min: 1, max: 10 },
    ]} />
    {ai && <div className="admin-editor-grid"><Field field={{ key: "standard", label: "标准质量附加积分", type: "number", min: 0, max: 1000000000 }} value={standard} onChange={(value) => setStandard(Number(value))} /><Field field={{ key: "fine", label: "精细质量附加积分", type: "number", min: standard + 1, max: 1000000000, hint: "实际收费 = 折后基础积分 + 质量附加积分" }} value={fine} onChange={(value) => setFine(Number(value))} /></div>}
    <details className="admin-editor-section"><summary>高级参数附加计价</summary><Field field={{ key: "rules", label: "参数附加计价（JSON）", type: "textarea", hint: "其他参数规则会保留；质量费用以上方标准与精细设置为准。" }} value={rules} onChange={(value) => setRules(String(value))} /></details>
  </EditorForm>;
}

const PLAN_FIELDS: FieldDefinition[] = [
  { key: "name", label: "套餐名称" },
  { key: "description", label: "套餐说明", type: "textarea" },
  { key: "status", label: "套餐状态", options: [["active", "启用"], ["inactive", "停用"], ["draft", "草稿"]] },
  { key: "level", label: "会员等级", type: "number", min: 0 },
  { key: "billing_period", label: "会员周期", options: [["none", "长期"], ["month", "月度"], ["year", "年度"]] },
  { key: "discount_percent", label: "积分计价比例（%）", type: "number", min: 0, max: 100, step: 0.01, hint: "100 表示原价，85 表示按原价的 85% 收取" },
  { key: "max_concurrent_jobs", label: "同时运行任务数", type: "number", min: 1, max: 1000 },
  { key: "max_upload_mb", label: "上传上限（MB）", type: "number", min: 1, max: 10000 },
  { key: "max_image_megapixels", label: "图片上限（百万像素）", type: "number", min: 1, max: 10000 },
  { key: "asset_retention_days", label: "素材保留天数", type: "number", min: 1, max: 36500 },
  { key: "display_order", label: "显示顺序", type: "number", min: 0 },
];

function MembershipEditor({ row, onClose, onSaved }: EditorProps) {
  const [values, setValues] = useState<Row>({ code: "", name: "", description: "", status: "active", level: 30, billing_period: "month", max_concurrent_jobs: 2, max_upload_mb: 20, max_image_megapixels: 40, asset_retention_days: 30, display_order: 30, ...row, discount_percent: Number(row.operation_discount_bps ?? 10000) / 100 });
  const [extras, setExtras] = useState(JSON.stringify(row.entitlements || {}, null, 2));
  return <EditorForm title={row.id ? `编辑会员套餐 · ${String(row.name)}` : "新建会员套餐"} label="保存套餐" onClose={onClose} onSubmit={async () => {
    const payload = Object.fromEntries(PLAN_FIELDS.filter((field) => field.key !== "discount_percent").map((field) => [field.key, values[field.key]]));
    Object.assign(payload, { operation_discount_bps: Math.round(Number(values.discount_percent) * 100), entitlements: jsonObject(extras, "扩展权益"), reason: "管理员配置会员套餐", ...(!row.id ? { code: values.code } : {}) });
    await apiRequest(`/api/v1/admin/membership-plans${row.id ? `/${row.id}` : ""}`, { method: row.id ? "PATCH" : "POST", body: JSON.stringify(payload) });
    onSaved("会员套餐已保存");
  }}>
    <p className="admin-editor-note">套餐用于设置等级、折扣和使用额度。调整后用于新分配或变更的会员，已有会员保留原权益；用户会员在“用户”详情中分配或续期。</p>
    <Field field={{ key: "code", label: "套餐代码", hint: "英文小写字母、数字、下划线或连字符，创建后不可修改" }} value={values.code} disabled={Boolean(row.id)} onChange={(value) => setValues({ ...values, code: value })} />
    <Fields fields={PLAN_FIELDS} values={values} setValues={setValues} />
    <p className="admin-editor-note">周期自动赠送积分尚未启用。赠送或扣减积分请使用“积分 → 调整积分”。</p>
    <details className="admin-editor-section"><summary>扩展权益</summary><Field field={{ key: "extras", label: "扩展权益（JSON）", type: "textarea" }} value={extras} onChange={(value) => setExtras(String(value))} /></details>
  </EditorForm>;
}

function UserPicker({ selected, onSelect, canSearch }: { selected: Row | null; onSelect: (user: Row) => void; canSearch: boolean }) {
  const [query, setQuery] = useState("");
  const [users, setUsers] = useState<Row[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!canSearch || selected) return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void apiRequest<{ items: Row[] }>(`/api/v1/admin/users?limit=20&query=${encodeURIComponent(query)}`)
        .then((result) => { if (!cancelled) { setUsers(result.items); setError(""); } })
        .catch((caught: Error) => { if (!cancelled) setError(caught.message); });
    }, 250);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [query, canSearch, selected]);
  if (selected) return <p className="admin-editor-note">操作用户：{String(selected.display_name || selected.email || selected.id)}{selected.email ? `（${selected.email}）` : ""}</p>;
  return <section className="admin-editor-section">
    {canSearch ? <><Field field={{ key: "search", label: "搜索用户邮箱或用户名", required: false }} value={query} onChange={(value) => setQuery(String(value))} /><div className="admin-picker-results">{users.map((user) => <button className="admin-secondary-button" type="button" key={String(user.id)} onClick={() => onSelect(user)}>{String(user.display_name)} · {String(user.email)}</button>)}</div>{!users.length && <p>暂无匹配用户</p>}</>
      : <Field field={{ key: "user_id", label: "用户 ID" }} value={query} onChange={(value) => setQuery(String(value))} />}
    {!canSearch && <button className="admin-secondary-button" type="button" disabled={!query.trim()} onClick={() => onSelect({ id: query.trim() })}>选择用户</button>}
    {error && <p role="alert">{error}</p>}
  </section>;
}

function useCommandKey() {
  const previous = useRef({ body: "", key: "" });
  return (body: unknown) => {
    const serialized = JSON.stringify(body);
    if (serialized !== previous.current.body) previous.current = { body: serialized, key: crypto.randomUUID() };
    return { "Idempotency-Key": previous.current.key };
  };
}

function PointsEditor({ row, permissions, onClose, onSaved }: EditorProps) {
  const [user, setUser] = useState<Row | null>(row.user_id ? { id: row.user_id } : row.id ? row : null);
  const [account, setAccount] = useState<Row | null>(null);
  const [accountError, setAccountError] = useState("");
  const [amount, setAmount] = useState<unknown>(0);
  const [reason, setReason] = useState("");
  const commandHeaders = useCommandKey();
  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    void apiRequest<{ account: Row }>(`/api/v1/admin/users/${user.id}/points?limit=1`)
      .then((result) => { if (!cancelled) setAccount(result.account); })
      .catch((error: Error) => { if (!cancelled) setAccountError(error.message); });
    return () => { cancelled = true; };
  }, [user]);
  return <EditorForm title="调整用户积分" label="确认调整积分" onClose={onClose} onSubmit={async () => {
    if (!user) throw new Error("请先选择用户");
    if (!Number.isInteger(amount) || amount === 0) throw new Error("请输入非零整数；正数赠送，负数扣减");
    if (!reason.trim()) throw new Error("请填写调整原因");
    const body = { amount, reason: reason.trim() };
    const result = await apiRequest<{ adjustment: Row }>(`/api/v1/admin/users/${user.id}/points/adjustments`, { method: "POST", headers: commandHeaders({ user_id: user.id, ...body }), body: JSON.stringify(body) });
    onSaved(result.adjustment.status === "applied" ? "积分已调整并记入流水" : "大额调整已进入积分审批，可在积分页查看");
  }}>
    <UserPicker selected={user} onSelect={setUser} canSearch={permissions.has("users.read")} />
    {account && <p className="admin-editor-balance">当前余额 <strong>{String(account.balance)}</strong> 积分</p>}
    {accountError && <p role="alert">{accountError}</p>}
    <Field field={{ key: "amount", label: "调整积分", type: "number", min: -1000000000, max: 1000000000, hint: "正数增加，负数扣减；超级管理员直接生效。" }} value={amount} onChange={setAmount} />
    <Field field={{ key: "reason", label: "调整原因", type: "textarea" }} value={reason} onChange={(value) => setReason(String(value))} />
  </EditorForm>;
}

function UserMembershipEditor({ row, permissions, onClose, onSaved }: EditorProps) {
  const [user, setUser] = useState<Row | null>(row.id ? row : null);
  const [plans, setPlans] = useState<Row[]>([]);
  const [memberships, setMemberships] = useState<Row[]>([]);
  const [planId, setPlanId] = useState("");
  const [endsAt, setEndsAt] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const commandHeaders = useCommandKey();
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const requests = [apiRequest<{ items: Row[] }>("/api/v1/admin/membership-plans?status=active"),
      user ? apiRequest<{ items: Row[] }>(`/api/v1/admin/users/${user.id}/memberships`) : Promise.resolve({ items: [] as Row[] })];
    void Promise.all(requests).then(([planResult, membershipResult]) => {
      if (cancelled) return;
      setPlans(planResult.items); setMemberships(membershipResult.items); setError("");
      setPlanId(String(membershipResult.items.find((item) => item.status === "active")?.plan_id || planResult.items[0]?.id || ""));
    }).catch((caught: Error) => { if (!cancelled) setError(caught.message); }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [user]);
  const active = memberships.find((item) => item.status === "active" && (!item.ends_at || new Date(String(item.ends_at)).getTime() > Date.now()));
  const plan = plans.find((item) => item.id === planId);
  const perpetual = plan?.billing_period === "none";
  return <EditorForm title="分配或续期会员" label={active?.plan_id === planId ? "保存会员续期" : "保存用户会员"} disabled={loading || Boolean(error)} onClose={onClose} onSubmit={async () => {
    if (!user || !plan) throw new Error("请选择用户和会员套餐");
    if (!perpetual && !endsAt) throw new Error("请设置会员到期时间");
    const renewing = active?.plan_id === planId;
    if (renewing && perpetual) throw new Error("用户已拥有此长期会员，无需续期");
    const body = { ...(renewing ? {} : { plan_id: planId }), ends_at: perpetual ? null : new Date(endsAt).toISOString(), reason: renewing ? "管理员续期会员" : "管理员分配会员" };
    const url = renewing ? `/api/v1/admin/memberships/${active.id}/renew` : `/api/v1/admin/users/${user.id}/memberships`;
    await apiRequest(url, { method: "POST", headers: commandHeaders({ url, ...body }), body: JSON.stringify(body) });
    onSaved("用户会员已生效");
  }}>
    <UserPicker selected={user} onSelect={setUser} canSearch={permissions.has("users.read")} />
    {error && <p role="alert">{error}</p>}
    {active && <p className="admin-editor-note">当前会员：{String(objectValue(active.plan).name || active.plan_id)}；到期：{active.ends_at ? new Date(String(active.ends_at)).toLocaleString() : "长期有效"}</p>}
    <Field field={{ key: "plan_id", label: "会员套餐", options: [["", "选择套餐"], ...plans.map((item): [string, string] => [String(item.id), String(item.name)])] }} value={planId} onChange={(value) => setPlanId(String(value))} />
    {!perpetual && <Field field={{ key: "ends_at", label: "会员到期时间", type: "datetime-local" }} value={endsAt} onChange={(value) => setEndsAt(String(value))} />}
    <p className="admin-editor-note">选择其他套餐会立即变更会员及权益；选择当前套餐可以延长有效期。</p>
  </EditorForm>;
}

function RoleEditor({ row, onClose, onSaved }: EditorProps) {
  const [fields, setFields] = useState<Row>({ code: row.code || "", name: row.name || "", description: row.description || "" });
  const [permissions, setPermissions] = useState<Row[]>([]);
  const [codes, setCodes] = useState<string[]>(Array.isArray(row.permissions) ? row.permissions as string[] : []);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let cancelled = false;
    void apiRequest<{ items: Row[] }>("/api/v1/admin/permissions").then((result) => { if (!cancelled) setPermissions(result.items); }).catch((caught: Error) => { if (!cancelled) setError(caught.message); }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);
  return <EditorForm title={row.id ? "编辑角色权限" : "新建角色"} disabled={Boolean(row.is_system) || loading || Boolean(error)} onClose={onClose} onSubmit={async () => {
    if (row.id) {
      await apiRequest(`/api/v1/admin/roles/${row.id}`, { method: "PATCH", body: JSON.stringify({ name: fields.name, description: fields.description, permission_codes: codes }) });
    } else await apiRequest("/api/v1/admin/roles", { method: "POST", body: JSON.stringify({ ...fields, permission_codes: codes }) });
    onSaved("角色权限已保存");
  }}>
    {Boolean(row.is_system) && <p className="admin-editor-note">内置角色的权限固定；可新建自定义角色，再到用户详情中分配。超级管理员拥有全部权限。</p>}
    {error && <p role="alert">{error}</p>}
    <Field field={{ key: "code", label: "角色代码" }} value={fields.code} disabled={Boolean(row.id)} onChange={(value) => setFields({ ...fields, code: value })} />
    <Fields fields={[{ key: "name", label: "角色名称" }, { key: "description", label: "角色说明", type: "textarea" }]} values={fields} setValues={setFields} />
    <div className="admin-permission-list">{permissions.map((item) => <Field key={String(item.code)} field={{ key: String(item.code), label: `${item.description}（${item.code}）`, type: "checkbox" }} value={codes.includes(String(item.code))} onChange={(checked) => setCodes(checked ? [...codes, String(item.code)] : codes.filter((code) => code !== item.code))} />)}</div>
  </EditorForm>;
}

function UserRolesEditor({ row, onClose, onSaved }: EditorProps) {
  const [roles, setRoles] = useState<Row[]>([]);
  const [assignments, setAssignments] = useState<Row[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    void Promise.all([apiRequest<{ items: Row[] }>("/api/v1/admin/roles"), apiRequest<{ roles: Row[] }>(`/api/v1/admin/users/${row.id}/roles`)]).then(([all, user]) => {
      if (cancelled) return;
      setRoles(all.items); setAssignments(user.roles.map((item) => ({ role_id: item.id, expires_at: item.expires_at })));
    }).catch((caught: Error) => { if (!cancelled) setError(caught.message); }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [row.id]);
  return <EditorForm title="分配用户角色" disabled={loading || Boolean(error)} onClose={onClose} onSubmit={async () => {
    await apiRequest(`/api/v1/admin/users/${row.id}/roles`, { method: "PUT", body: JSON.stringify({ roles: assignments }) });
    onSaved("用户角色已保存");
  }}>
    <p className="admin-editor-note">用户：{String(row.email || row.display_name)}。至少保留一个角色，已有角色的到期时间会保留。</p>
    {error && <p role="alert">{error}</p>}
    {roles.map((role) => <Field key={String(role.id)} field={{ key: String(role.id), label: String(role.name), type: "checkbox", hint: String(role.description || "") }} value={assignments.some((item) => item.role_id === role.id)} onChange={(checked) => setAssignments(checked ? [...assignments, { role_id: role.id, expires_at: null }] : assignments.filter((item) => item.role_id !== role.id))} />)}
  </EditorForm>;
}

function UserEditor({ row, onClose, onSaved }: EditorProps) {
  const [values, setValues] = useState<Row>({ email: row.email || "", username: row.username || "", display_name: row.display_name || "" });
  return <EditorForm title="编辑用户资料" onClose={onClose} onSubmit={async () => {
    await apiRequest(`/api/v1/admin/users/${row.id}`, { method: "PATCH", body: JSON.stringify(values) });
    onSaved("用户资料已保存");
  }}><Fields fields={[{ key: "email", label: "邮箱" }, { key: "username", label: "用户名", required: false }, { key: "display_name", label: "显示名称" }]} values={values} setValues={setValues} /></EditorForm>;
}

export function PointAdjustments({ currentUserId, superAdmin, canAdjust, onSaved }: { currentUserId: string; superAdmin: boolean; canAdjust: boolean; onSaved: (message: string) => void }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [error, setError] = useState("");
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<{ row: Row; decision: "approve" | "reject" } | null>(null);
  const [reason, setReason] = useState("");
  const commandHeaders = useCommandKey();
  async function load(cursor?: string) {
    try {
      const result = await apiRequest<{ items: Row[]; next_cursor?: string }>(`/api/v1/admin/point-adjustments?status=pending&limit=25${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
      setRows((previous) => cursor ? [...previous, ...result.items] : result.items); setNextCursor(result.next_cursor || null); setError("");
    } catch (caught) { setError(caught instanceof Error ? caught.message : "积分审批加载失败"); }
  }
  useEffect(() => { void load(); }, []);
  return <section className="admin-related-section admin-editor-note"><h3>待处理积分调整</h3><p>超级管理员的调整直接生效。其他管理员的大额调整在这里处理。</p>
    {error && <p role="alert">{error}<button type="button" className="admin-secondary-button" onClick={() => void load()}>重试</button></p>}
    {rows.map((row) => <div className="admin-adjustment-row" key={String(row.id)}><span>用户 {String(row.user_id)} · {String(row.amount)} 积分 · {String(row.reason)}</span>{canAdjust && (row.requested_by !== currentUserId || superAdmin) ? <><button className="admin-secondary-button" onClick={() => setSelected({ row, decision: "approve" })}>批准并入账</button><button className="admin-secondary-button" onClick={() => setSelected({ row, decision: "reject" })}>拒绝</button></> : <small>等待其他管理员处理</small>}</div>)}
    {!rows.length && !error && <p>没有待处理的积分调整</p>}{nextCursor && <button className="admin-secondary-button" onClick={() => void load(nextCursor)}>加载更多</button>}
    {selected && <EditorForm title={selected.decision === "approve" ? "批准积分调整" : "拒绝积分调整"} onClose={() => setSelected(null)} onSubmit={async () => {
      if (!reason.trim()) throw new Error("请填写处理原因");
      const body = { reason: reason.trim() };
      const url = `/api/v1/admin/point-adjustments/${selected.row.id}/${selected.decision}`;
      await apiRequest(url, { method: "POST", headers: commandHeaders({ url, ...body }), body: JSON.stringify(body) });
      setSelected(null); setReason(""); await load(); onSaved(selected.decision === "approve" ? "积分调整已入账" : "积分调整已拒绝");
    }}><p className="admin-editor-note">变动：{String(selected.row.amount)} 积分；原因：{String(selected.row.reason)}</p><Field field={{ key: "reason", label: "处理原因", type: "textarea" }} value={reason} onChange={(value) => setReason(String(value))} /></EditorForm>}
  </section>;
}

export function DirectActionDialog({ title, endpoint, data = {}, onClose, onSaved }: { title: string; endpoint: string; data?: Row; onClose: () => void; onSaved: (message: string) => void }) {
  const [reason, setReason] = useState("");
  return <EditorForm title={title} label={`确认${title}`} onClose={onClose} onSubmit={async () => {
    if (!reason.trim()) throw new Error("请填写操作原因");
    const result = await apiRequest<Row | undefined>(endpoint, { method: "POST", body: JSON.stringify({ reason, ...data }) });
    onSaved(result && objectValue(result.result).mismatches ? "核对完成，仍有账务差异，请查看任务详情" : `${title}已完成`);
  }}><p className="admin-editor-note">确认后立即执行。</p><Field field={{ key: "reason", label: "操作原因", type: "textarea" }} value={reason} onChange={(value) => setReason(String(value))} /></EditorForm>;
}

export function AdminEditor(props: EditorProps & { kind: EditorKind }) {
  switch (props.kind) {
    case "settings": return <ConfigEditor {...props} />;
    case "pricing": return <PricingEditor {...props} />;
    case "memberships": return <MembershipEditor {...props} />;
    case "points": return <PointsEditor {...props} />;
    case "user-membership": return <UserMembershipEditor {...props} />;
    case "roles": return <RoleEditor {...props} />;
    case "user-roles": return <UserRolesEditor {...props} />;
    case "users": return <UserEditor {...props} />;
  }
}
