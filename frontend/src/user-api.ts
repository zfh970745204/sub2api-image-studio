export class ApiError extends Error {
  status: number;
  code: string;
  requestId?: string;
  retryAfter?: number;

  constructor(status: number, code: string, message: string, requestId?: string) {
    super(message);
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

export async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(url, { ...init, headers });
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as {
      code?: string;
      message?: string;
      request_id?: string;
      detail?: string | { message?: string };
    };
    const detail = typeof payload.detail === "string" ? payload.detail : payload.detail?.message;
    const error = new ApiError(
      response.status,
      payload.code || `HTTP_${response.status}`,
      payload.message || detail || `请求失败（${response.status}）`,
      payload.request_id,
    );
    const retryAfter = Number(response.headers.get("Retry-After"));
    if (Number.isFinite(retryAfter) && retryAfter > 0) error.retryAfter = retryAfter;
    throw error;
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export interface UserSummary {
  id: string;
  email: string;
  username: string | null;
  display_name: string;
  avatar_asset_id: string | null;
  status: string;
}

export interface UserPreferences {
  user_id: string;
  theme: "light" | "dark" | "system";
  locale: "zh-CN" | "en-US";
  studio_layout: {
    nav_collapsed?: boolean;
    asset_view?: "grid" | "list";
    panel_position?: "right" | "bottom";
    panel_width?: number;
    canvas_fit?: "contain" | "actual";
    last_tool?: string;
    print_output_mode?: "transparent" | "opaque";
  };
  notification_preferences: Record<string, boolean>;
  updated_at: string;
}

export interface BootstrapData {
  user: UserSummary;
  permissions: string[];
  membership: {
    id: string;
    status: string;
    starts_at: string;
    ends_at: string | null;
    plan: { id: string; code: string; name: string; level: number };
    entitlements: {
      discount_bps: number;
      max_concurrent_jobs: number;
      max_upload_mb: number;
      max_image_megapixels: number;
      retention_days: number;
      periodic_points: number;
      extra: Record<string, unknown>;
    };
  };
  points: {
    balance: number;
    status: string;
    lifetime_earned: number;
    lifetime_spent: number;
  };
  service: {
    status: string;
    features: Record<string, boolean>;
  };
  notifications: { unread_count: number };
  preferences: UserPreferences;
}

export interface UserNotification {
  id: string;
  type: string;
  title: string;
  body: string;
  target_url: string | null;
  read_at: string | null;
  created_at: string;
}

export interface PriceRule {
  parameter: string; type: "choice" | "per_unit";
  points: Record<string, number> | number; included?: number; unit?: number;
}

export interface Operation {
  id: string;
  code: string;
  name: string;
  engine_type: string;
  enabled: boolean;
  member_base_points?: number;
  current_price: { base_points: number; parameter_rules?: { rules?: PriceRule[] } } | null;
}

export interface Quote {
  id: string;
  operation_code: string;
  source_asset_id: string | null;
  base_points: number;
  discount_points: number;
  surcharge_points: number;
  final_points: number;
  expires_at: string;
}

export interface ImageJob {
  id: string;
  operation_code: string;
  source_asset_id: string | null;
  output_asset_id: string | null;
  output_asset_ids?: string[];
  status: string;
  refund_status: string;
  parameters: Record<string, unknown>;
  charged_points: number;
  attempt_count: number;
  progress: number;
  error_code: string | null;
  error_message: string | null;
  queued_at: string;
  started_at: string | null;
  completed_at: string | null;
  refunded_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface Asset {
  id: string;
  root_asset_id: string;
  parent_asset_id: string | null;
  source_job_id: string | null;
  kind: string;
  operation_code: string;
  original_filename: string | null;
  mime_type: string;
  extension: string;
  size_bytes: number;
  width: number | null;
  height: number | null;
  has_alpha: boolean | null;
  status: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface CropOptions { x: number; y: number; width: number; height: number; shape: "rectangle" | "circle" }

export interface SelectionContext {
  width: number;
  height: number;
  restore_limited: boolean;
  has_initial_selection: boolean;
  source_url: string;
  result_url: string;
}

export async function selectionPixels(url: string, signal: AbortSignal) {
  const response = await fetch(url, { signal, credentials: "same-origin", cache: "no-store" });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.message || "修边图片读取失败，请重试");
  }
  return response.blob();
}

export interface PointTransaction {
  id: string;
  entry_type: string;
  delta: number;
  balance_before: number;
  balance_after: number;
  reference_type: string;
  reference_id: string;
  description: string;
  created_at: string;
}

export interface MembershipPlan {
  id: string;
  code: string;
  name: string;
  description: string;
  level: number;
  periodic_points: number;
  operation_discount_bps: number;
  max_concurrent_jobs: number;
  max_upload_mb: number;
  max_image_megapixels: number;
  asset_retention_days: number;
  entitlements: Record<string, unknown>;
}

export interface SessionInfo {
  id: string;
  user_agent: string;
  last_seen_at: string;
  created_at: string;
  expires_at: string;
  current: boolean;
}

export interface PageOptions { cursor?: string | null; limit?: number; [key: string]: string | number | null | undefined }
function pageQuery(options: PageOptions = {}) {
  return new URLSearchParams(Object.entries({ limit: 100, ...options }).filter(([, value]) => value !== null && value !== undefined && value !== "").map(([key, value]) => [key, String(value)])).toString();
}

export function estimatedPoints(operation: Operation | undefined, parameters: Record<string, unknown>): number | null {
  if (!operation?.current_price) return null;
  let total = operation.member_base_points ?? operation.current_price.base_points;
  for (const rule of operation.current_price.parameter_rules?.rules || []) {
    const value = parameters[rule.parameter];
    if (value === undefined) continue;
    if (rule.type === "choice" && typeof rule.points === "object") total += rule.points[String(value)] || 0;
    else if (rule.type === "per_unit" && typeof rule.points === "number") total += Math.ceil(Math.max(0, Number(value) - (rule.included || 0)) / (rule.unit || 1)) * rule.points;
  }
  return total * (operation.code === "ai.ecommerce" ? Number(parameters.image_count || 1) : 1);
}

export const jobOutputIds = (job: ImageJob) => job.output_asset_ids?.length ? job.output_asset_ids : job.output_asset_id ? [job.output_asset_id] : [];

export async function downloadJob(jobId: string) {
  const response = await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}/download`);
  if (!response.ok) { const payload = await response.json().catch(() => ({})); throw new Error(payload.message || "整组下载失败，请重试"); }
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a"); link.href = url; link.download = `studio-${jobId}.zip`; link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 60000);
}

export const api = {
  currentUser: () => request<{ user: UserSummary }>("/api/v1/auth/me"),
  authOptions: () => request<{ registration_enabled: boolean }>("/api/v1/auth/options"),
  sendRegistrationCode: (email: string) => request<{ status: string; retry_after_seconds: number; expires_in_seconds: number }>("/api/v1/auth/register/email-code", {
    method: "POST", body: JSON.stringify({ email }),
  }),
  register: (email: string, display_name: string, password: string, verification_code: string) =>
    request<{ user: UserSummary }>("/api/v1/auth/register", {
      method: "POST", body: JSON.stringify({ email, display_name, password, verification_code }),
    }),
  login: (identifier: string, password: string) =>
    request<{ user: UserSummary }>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ identifier, password }),
    }),
  logout: () => request<void>("/api/v1/auth/logout", { method: "POST" }),
  forgotPassword: (identifier: string) =>
    request<{ status: string }>("/api/v1/auth/password/forgot", {
      method: "POST",
      body: JSON.stringify({ identifier }),
    }),
  bootstrap: () => request<BootstrapData>("/api/v1/app/bootstrap"),
  updateProfile: (payload: { display_name: string; username: string | null }) =>
    request<{ user: UserSummary }>("/api/v1/auth/me", {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  changePassword: (current_password: string, new_password: string) =>
    request<void>("/api/v1/auth/password/change", {
      method: "POST",
      body: JSON.stringify({ current_password, new_password }),
    }),
  sessions: () =>
    request<{ items: SessionInfo[]; recent_failed_logins: unknown[] }>(
      "/api/v1/auth/sessions",
    ),
  revokeSession: (id: string) =>
    request<void>(`/api/v1/auth/sessions/${id}`, { method: "DELETE" }),
  preferences: () => request<{ preferences: UserPreferences }>("/api/v1/me/preferences"),
  updatePreferences: (payload: Record<string, unknown>) =>
    request<{ preferences: UserPreferences }>("/api/v1/me/preferences", {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  notifications: (unreadOnly = false) =>
    request<{ items: UserNotification[]; next_cursor: string | null }>(
      `/api/v1/me/notifications?limit=30&unread_only=${unreadOnly}`,
    ),
  readNotification: (id: string) =>
    request<{ notification: UserNotification }>(`/api/v1/me/notifications/${id}/read`, {
      method: "POST",
    }),
  readAllNotifications: () =>
    request<{ updated: number }>("/api/v1/me/notifications/read-all", { method: "POST" }),
  operations: () => request<{ items: Operation[] }>("/api/v1/operations"),
  quote: (operation_code: string, source_asset_id: string | null, parameters: Record<string, unknown>) =>
    request<{ quote: Quote }>("/api/v1/jobs/quote", {
      method: "POST",
      body: JSON.stringify({ operation_code, source_asset_id, parameters }),
    }),
  createJob: (quote_id: string, parameters: Record<string, unknown>) =>
    request<{ job: ImageJob; created: boolean; dispatched: boolean }>("/api/v1/jobs", {
      method: "POST",
      headers: { "Idempotency-Key": `studio:${quote_id}` },
      body: JSON.stringify({ quote_id, parameters }),
    }),
  jobs: (status = "", page: PageOptions = {}) =>
    request<{ items: ImageJob[]; next_cursor: string | null }>(
      `/api/v1/jobs?${pageQuery({ ...page, status })}`,
    ),
  job: (id: string) => request<{ job: ImageJob }>(`/api/v1/jobs/${id}`),
  jobEvents: (id: string) =>
    request<{ job: ImageJob; next_poll_after_ms: number | null }>(`/api/v1/jobs/${id}/events`),
  cancelJob: (id: string) =>
    request<{ job: ImageJob }>(`/api/v1/jobs/${id}/cancel`, { method: "POST" }),
  assets: (kind = "", page: PageOptions = {}) =>
    request<{ items: Asset[]; next_cursor: string | null }>(
      `/api/v1/assets?${pageQuery({ ...page, kind })}`,
    ),
  asset: (id: string) => request<{ asset: Asset }>(`/api/v1/assets/${id}`),
  selection: (id: string, signal?: AbortSignal) => request<SelectionContext>(`/api/v1/assets/${id}/selection`, { signal }),
  crop: (id: string, options: CropOptions) => request<{ asset: Asset }>(`/api/v1/assets/${id}/crop`, { method: "POST", body: JSON.stringify(options) }),
  saveSelection: (id: string, image: Blob) => {
    const body = new FormData();
    body.append("image", image, "selection-refined.png");
    return request<{ asset: Asset }>(`/api/v1/assets/${id}/selection`, { method: "POST", body });
  },
  printBackground: (id: string, point?: { x: number; y: number }) =>
    request<{ color: string; confidence: number; method: string }>(`/api/v1/assets/${id}/print-background${point ? `?x=${point.x}&y=${point.y}` : ""}`),
  lineage: (id: string) => request<{ items: Asset[] }>(`/api/v1/assets/${id}/lineage`),
  uploadAsset: (file: File, kind = "original") => {
    const body = new FormData();
    body.append("image", file);
    body.append("kind", kind);
    return request<{ asset: Asset }>("/api/v1/assets/upload", { method: "POST", body });
  },
  downloadUrl: (id: string) =>
    request<{ url: string; expires_at: string }>(`/api/v1/assets/${id}/download-url`, {
      method: "POST",
    }),
  deleteAsset: (id: string) =>
    request<{ asset: Asset }>(`/api/v1/assets/${id}`, { method: "DELETE" }),
  pointBalance: () =>
    request<{
      account: {
        balance: number;
        lifetime_earned: number;
        lifetime_spent: number;
        status: string;
      };
    }>("/api/v1/points/balance"),
  pointTransactions: (page: PageOptions = {}) =>
    request<{ items: PointTransaction[]; next_cursor: string | null }>(
      `/api/v1/points/transactions?${pageQuery(page)}`,
    ),
  membership: () => request<{ membership: BootstrapData["membership"] }>("/api/v1/membership/me"),
  membershipPlans: () => request<{ items: MembershipPlan[] }>("/api/v1/membership/plans"),
};

/** Limit bandwidth pressure while keeping input order and each successful upload. */
export async function uploadAssets(files: File[], onSettled: (completed: number) => void) {
  const results: PromiseSettledResult<Asset>[] = new Array(files.length);
  let next = 0;
  let completed = 0;
  async function worker() {
    while (next < files.length) {
      const index = next++;
      try { results[index] = { status: "fulfilled", value: (await api.uploadAsset(files[index])).asset }; }
      catch (reason) { results[index] = { status: "rejected", reason }; }
      onSettled(++completed);
    }
  }
  await Promise.all(Array.from({ length: Math.min(2, files.length) }, () => worker()));
  return results;
}
