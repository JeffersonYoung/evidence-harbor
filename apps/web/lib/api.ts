export type Project = {
  id: string;
  name: string;
  description: string;
  created_at: string;
  documents?: Document[];
  sources?: Source[];
  questions?: Question[];
  runs?: Run[];
  proposals?: Proposal[];
  operations?: Operation[];
  evidence?: Evidence[];
};
export type Source = { id: string; title: string; canonical_uri: string; kind: string };
export type Document = {
  id: string;
  title: string;
  kind: string;
  content: string;
  version: number;
  created_at: string;
  updated_at?: string;
  source_id?: string;
  capture_id?: string;
  representation_id?: string;
  locked_sections?: string[];
  blocks?: Block[];
  capture?: Record<string, unknown>;
};
export type Block = {
  id: string;
  text: string;
  start_offset: number;
  end_offset: number;
  locator_json: Record<string, unknown>;
};
export type Operation = {
  id: string;
  status: string;
  kind?: string;
  created_at?: string;
  started_at?: string;
  completed_at?: string;
  error?: string | null;
  result_json?: Record<string, unknown> | null;
};
export type Question = {
  id: string;
  text: string;
  status: string;
  answer?: string;
  evidence_ids?: string[];
  created_at?: string;
};
export type Run = {
  id: string;
  question: string;
  status: string;
  provider: string;
  created_at: string;
  result_json?: Record<string, unknown> | null;
  error?: string;
  events?: RunEvent[];
};
export type RunEvent = {
  id: string;
  sequence: number;
  type: string;
  data: Record<string, unknown>;
  created_at: string;
};
export type Claim = {
  text: string;
  evidence_ids: string[];
  scope?: string;
  as_of?: string;
  status?: string;
  limitations?: string[];
  evidence_links?: { evidence_id: string; relation: string; rationale?: string }[];
};
export type Proposal = {
  id: string;
  title: string;
  content: string;
  status: string;
  claims: Claim[];
  target_document_id?: string | null;
  base_version?: number | null;
  created_at: string;
  published_document_id?: string | null;
};
export type SearchResult = {
  document_id: string;
  block_id: string;
  text: string;
  title: string;
  score: number;
  source_id: string;
  capture_id: string;
  locator_json: Record<string, unknown>;
};
export type Evidence = {
  id: string;
  block_id: string;
  quote: string;
  start_offset: number;
  end_offset: number;
  capture_id: string;
  document_id?: string;
  block?: Block;
  document?: Document;
  locator_json?: Record<string, unknown>;
};
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public detail: unknown,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData))
    headers.set('Content-Type', 'application/json');
  const response = await fetch(`/api${path}`, { ...init, headers, cache: 'no-store' });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: '服务响应异常' }));
    const detail = payload.detail;
    throw new ApiError(
      response.status,
      response.status === 403
        ? '当前账户没有此操作权限，请联系管理员调整角色。'
        : typeof detail === 'string'
          ? detail
          : detail?.message || JSON.stringify(detail || payload),
      detail,
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}
export const post = <T>(path: string, body: unknown, headers?: HeadersInit) =>
  api<T>(path, { method: 'POST', body: JSON.stringify(body), headers });
export function errorText(error: unknown) {
  return error instanceof Error ? error.message : '请求未能完成，请重试';
}
export function displayDate(value?: string) {
  if (!value) return '尚未记录';
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value));
}
export function isPending(status: string) {
  return ['pending', 'running', 'retrying', 'queued', 'in_progress'].includes(status);
}
export function safeHttpUrl(value: string) {
  try {
    const u = new URL(value);
    return ['http:', 'https:'].includes(u.protocol) ? u.href : null;
  } catch {
    return null;
  }
}
