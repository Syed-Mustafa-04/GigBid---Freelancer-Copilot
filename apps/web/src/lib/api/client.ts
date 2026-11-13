/**
 * Typed API client.
 *
 * The response shapes in `schema.d.ts` are GENERATED from the FastAPI OpenAPI
 * document (`npm run gen:api`) and must not be hand-edited. The thin wrapper
 * here adds auth, error normalisation and the SSE reader, because those are
 * the parts a generator cannot produce.
 */

export const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

const TOKEN_KEY = 'gigbid.token';

export function getToken(): string | null {
  if (typeof window === 'undefined') return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  window.localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  window.localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public detail?: unknown,
  ) {
    super(message);
    this.name = 'ApiError';
  }

  /** True when the failure is the marketplace being down, not a bug. */
  get isDegraded(): boolean {
    return this.status === 503;
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const token = getToken();
  const headers = new Headers(init.headers);
  headers.set('Content-Type', 'application/json');
  if (token) headers.set('Authorization', `Bearer ${token}`);

  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, { ...init, headers });
  } catch {
    throw new ApiError(0, 'Cannot reach the GigBid API. Is it running?');
  }

  if (response.status === 401) {
    clearToken();
    if (typeof window !== 'undefined' && !window.location.pathname.startsWith('/login')) {
      window.location.href = '/login';
    }
    throw new ApiError(401, 'Your session expired. Please sign in again.');
  }

  if (!response.ok) {
    let detail: unknown;
    try {
      detail = await response.json();
    } catch {
      detail = await response.text();
    }
    const message =
      typeof detail === 'object' && detail !== null && 'detail' in detail
        ? String((detail as { detail: unknown }).detail)
        : `Request failed (${response.status})`;
    throw new ApiError(response.status, message, detail);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PATCH', body: body ? JSON.stringify(body) : undefined }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
};

/* -------------------------------------------------------------------------
 * Server-Sent Events
 *
 * `fetch` + a reader rather than `EventSource`, because EventSource cannot
 * send an Authorization header or issue a POST, and the run endpoint needs
 * both. The buffering below matters: an SSE frame can arrive split across
 * chunks, and parsing per-chunk silently drops events under load.
 * ---------------------------------------------------------------------- */
export interface StreamEvent {
  event: string;
  thread_id?: string;
  node?: string;
  status?: string;
  summary?: Record<string, unknown>;
  payload?: Record<string, unknown>;
  detail?: string;
  awaiting_approval?: boolean;
}

export async function streamRun(
  path: string,
  body: unknown,
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const token = getToken();
  const response = await fetch(`${API_URL}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok || !response.body) {
    throw new ApiError(response.status, `Could not start the run (${response.status})`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const frames = buffer.split('\n\n');
    buffer = frames.pop() ?? '';
    for (const frame of frames) {
      const line = frame.split('\n').find((l) => l.startsWith('data: '));
      if (!line) continue;
      try {
        onEvent(JSON.parse(line.slice(6)) as StreamEvent);
      } catch {
        /* a malformed frame must not kill the stream */
      }
    }
  }
}

/* ------------------------------------------------------------------------- */
export interface Job {
  job_id: string;
  title: string;
  description: string;
  required_skills: string[];
  budget: { min: number; max: number; currency: string; type: string };
  duration_days?: number;
  screening_questions: { question_id: string; text: string; required: boolean }[];
  connects_cost: number;
  client_id: string;
  posted_at: string;
  proposals_received: number;
  gigbid: {
    thread_id: string | null;
    status: string;
    decision: string | null;
    fit_score: number | null;
    risk_score: number | null;
    price_usd: number | null;
  };
}

export interface Claim {
  claim_id: string;
  text: string;
  location: string;
  status: 'supported' | 'partly_supported' | 'unsupported';
  evidence_ids: string[];
  support_score: number;
  verifier_note: string;
  rewritten_as: string | null;
  removed: boolean;
}

export interface EvidenceItem {
  evidence_id: string;
  type: string;
  title: string;
  text: string;
  skills: string[];
  outcome: string;
  date: string;
  score: number;
}

export interface ApprovalPayload {
  kind: 'proposal' | 'message' | 'connects_spend';
  thread_id: string;
  job_id: string;
  draft: {
    cover_letter: string;
    screening_answers: { question: string; answer: string; evidence_ids: string[] }[];
    evidence_ids: string[];
    price_usd: number;
    word_count: number;
    revision: number;
  } | null;
  reply: { body: string; proposed_slots: { start_iso: string; label: string }[] } | null;
  evidence: EvidenceItem[];
  claims: Claim[];
  risk_report: {
    score: number;
    flags: string[];
    injection_detected: boolean;
    injection_spans: string[];
    explanation: string;
  } | null;
  price_quote: {
    amount_usd: number;
    fair_band_low: number;
    fair_band_high: number;
    market_median: number;
    effective_hourly_usd: number;
    inside_fair_band: boolean;
    rationale: string;
  } | null;
  critique: {
    approved: boolean;
    score: number;
    issues: { kind: string; detail: string; severity: string }[];
  } | null;
  compliance: {
    allowed: boolean;
    violations: { rule_id: string; severity: string; evidence_span: string; remedy: string }[];
  } | null;
  dry_run: {
    tool_name: string;
    server: string;
    arguments: Record<string, unknown>;
    connects_cost: number;
    idempotency_key: string;
    would_call: string;
  } | null;
  budget_snapshot: Record<string, number>;
}

export interface SystemStatus {
  marketplace: { mode: string; adapter: string; available: boolean };
  llm: { profile: string; offline_stub: boolean; warning: string | null };
  rag: { shipped_variant: string; embedding_backend: string };
}
