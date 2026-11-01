export interface QueueItem {
  document_id: string;
  state: string;
  version: number;
  reason: string;
  original_filename: string;
  created_at: string;
}

export interface Candidate {
  matter_id: string;
  matter_number: string;
  client_name: string;
  rank: number;
  score: string;
  closed: boolean;
  features: Record<string, string>;
  explanation: string;
}

export interface AuditEvent {
  from_state: string | null;
  to_state: string;
  stage: string | null;
  actor: string;
  reason: string;
  details: Record<string, unknown>;
  created_at: string;
}

export interface ReviewDetail {
  document_id: string;
  state: string;
  version: number;
  reason: string;
  original_filename: string;
  mime_type: string;
  fields: Record<string, unknown>;
  classification: { document_type: string; confidence: string; method: string } | null;
  candidates: Candidate[];
  events: AuditEvent[];
}

export interface Matter {
  id: string;
  matter_number: string;
  client_name: string;
  status: string;
}

export interface Corrections {
  document_type?: string;
  case_number?: string;
  document_date?: string;
}

export interface ResolveBody {
  action: "APPROVE" | "ASSIGN" | "MARK_UNMATCHED";
  expected_version: number;
  matter_id?: string;
  corrections?: Corrections;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

function describe(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => String(item?.msg ?? "Invalid value")).join("; ");
  }
  return "The request failed";
}

async function request<T>(path: string, apiKey: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: { "X-API-Key": apiKey, "Content-Type": "application/json" },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(response.status, describe(body?.detail));
  }
  return (await response.json()) as T;
}

export const api = {
  queue: (apiKey: string) => request<QueueItem[]>("/reviews", apiKey),
  detail: (apiKey: string, id: string) => request<ReviewDetail>(`/reviews/${id}`, apiKey),
  events: (apiKey: string, id: string) =>
    request<AuditEvent[]>(`/documents/${id}/events`, apiKey),
  matters: (apiKey: string, query: string) =>
    request<Matter[]>(`/matters?q=${encodeURIComponent(query)}`, apiKey),
  resolve: (apiKey: string, id: string, body: ResolveBody) =>
    request<{ state: string; version: number }>(`/reviews/${id}/resolve`, apiKey, {
      method: "POST",
      body: JSON.stringify(body),
    }),
};
