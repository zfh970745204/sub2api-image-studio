export type AdminRow = Record<string, unknown>;

export class ApiRequestError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function apiRequest<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      ...(init?.body && !(init.body instanceof FormData) ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const errors = Array.isArray(payload.detail) ? payload.detail : Array.isArray(payload.details) ? payload.details : [];
    const detail = errors.map((item: { msg?: string; message?: string; field?: string }) =>
      `${item.field ? `${item.field}: ` : ""}${item.message || item.msg || "字段无效"}`).join("；");
    throw new ApiRequestError(response.status,
      [payload.message || (typeof payload.detail === "string" ? payload.detail : payload.detail?.message), detail].filter(Boolean).join("：") || `请求失败（${response.status}）`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function objectValue(value: unknown): AdminRow {
  return value && typeof value === "object" && !Array.isArray(value) ? value as AdminRow : {};
}

export function jsonObject(value: string, label: string): AdminRow {
  let parsed: unknown;
  try { parsed = JSON.parse(value); } catch { throw new Error(`${label}格式无效`); }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error(`${label}必须是对象`);
  return parsed as AdminRow;
}
