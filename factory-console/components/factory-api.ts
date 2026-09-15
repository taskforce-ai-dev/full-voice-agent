export const FACTORY_OPERATIONS = [
  "inspect",
  "plan",
  "approve-knowledge",
  "approve-plan",
  "generate",
  "verify",
  "open-pr",
] as const;

export type FactoryOperation = (typeof FACTORY_OPERATIONS)[number];
export type JobState =
  | "draft"
  | "inspected"
  | "knowledge_review_required"
  | "plan_review_required"
  | "approved_for_generation"
  | "generated"
  | "verified"
  | "pr_ready"
  | "blocked";

export type FactoryReview = {
  status: "knowledge_review_required" | "plan_review_required";
  digest: string;
};

export type FactoryJob = {
  job_id: string;
  state: JobState;
  intake: {
    company_name: string;
    industry: string;
    purpose: string;
    primary_contact: string;
    supported_languages: string[];
  };
  review: FactoryReview | null;
};

export type IntakePayload = FactoryJob["intake"];

function csrfToken() {
  if (typeof document === "undefined") return "";
  const cookie = document.cookie.split(";").map((part) => part.trim()).find((part) => part.startsWith("factory_csrf="));
  return cookie ? decodeURIComponent(cookie.slice("factory_csrf=".length)) : "";
}

export class FactoryApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "FactoryApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-Factory-Console-CSRF": csrfToken(),
      ...init.headers,
    },
  });
  const payload = (await response.json().catch(() => null)) as { error?: string } | null;
  if (!response.ok) throw new FactoryApiError(response.status, payload?.error ?? "Factory request failed");
  return payload as T;
}

export function createJob(payload: IntakePayload) {
  return request<FactoryJob>("/v1/jobs", { method: "POST", body: JSON.stringify(payload) });
}

export function getJob(jobId: string) {
  return request<FactoryJob>(`/v1/jobs/${encodeURIComponent(jobId)}`);
}

export function runOperation(jobId: string, operation: FactoryOperation, digest?: string) {
  const body = digest === undefined ? undefined : JSON.stringify({ digest });
  return request<FactoryJob>(`/v1/jobs/${encodeURIComponent(jobId)}/${operation}`, {
    method: "POST",
    ...(body === undefined ? {} : { body }),
  });
}
