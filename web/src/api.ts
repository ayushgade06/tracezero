// The only module that talks to the TraceZero API (docs/API.md). The base URL is set once, here.
import type { Job, JobStatus } from "./types";

export const API_BASE: string = import.meta.env.VITE_TRACEZERO_API_BASE || "/api";

export const POLL_INTERVAL_MS = 1000;
export const POLL_MAX_WAIT_MS = 10 * 60 * 1000;
const STATUSES: JobStatus[] = ["queued", "running", "completed", "failed"];

/** network: no answer (API stopped, proxy can't reach it). http: the API answered with a JSON error.
 *  malformed: an answer that isn't the documented JSON. timeout: polling gave up. */
export type ErrorKind = "network" | "http" | "malformed" | "timeout";

export class ApiError extends Error {
  constructor(
    readonly kind: ErrorKind,
    message: string,
    readonly status = 0,
    readonly code = "",
  ) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(API_BASE + path, {
      method,
      signal,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    throw new ApiError("network", "The TraceZero API could not be reached.");
  }
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    // The dev proxy answers 5xx without JSON when nothing listens on the API port.
    if (response.status >= 500) throw new ApiError("network", "The TraceZero API could not be reached.", response.status);
    throw new ApiError("malformed", `The API answered ${response.status} without a JSON body.`, response.status);
  }
  if (!response.ok) {
    const err = (data as { error?: { code?: unknown; message?: unknown } })?.error;
    if (typeof err?.code !== "string") throw new ApiError("malformed", `The API answered ${response.status} without an error object.`, response.status);
    throw new ApiError("http", String(err.message ?? ""), response.status, err.code);
  }
  return data as T;
}

function check(ok: boolean, what: string) {
  if (!ok) throw new ApiError("malformed", `Unexpected response shape from ${what}.`);
}

export async function getHealth(signal?: AbortSignal): Promise<void> {
  const data = await request<{ status?: unknown }>("GET", "/health", undefined, signal);
  check(data?.status === "ok", "GET /health");
}

export async function listDatasets(signal?: AbortSignal): Promise<string[]> {
  const data = await request<{ datasets?: { dataset_id?: unknown }[] }>("GET", "/datasets", undefined, signal);
  check(Array.isArray(data?.datasets) && data.datasets.every((d) => typeof d?.dataset_id === "string"), "GET /datasets");
  return data.datasets!.map((d) => d.dataset_id as string);
}

export async function submitAudit(datasetId: string, signal?: AbortSignal): Promise<{ audit_id: string; status: JobStatus }> {
  const data = await request<{ audit_id?: unknown; status?: unknown }>("POST", "/audits", { dataset_id: datasetId }, signal);
  check(typeof data?.audit_id === "string" && data.status === "queued", "POST /audits");
  return data as { audit_id: string; status: JobStatus };
}

export async function getAudit(auditId: string, signal?: AbortSignal): Promise<Job> {
  const job = await request<Job>("GET", `/audits/${encodeURIComponent(auditId)}`, undefined, signal);
  check(typeof job?.audit_id === "string" && STATUSES.includes(job.status), "GET /audits/{id}");
  if (job.status === "completed") check(typeof job.result === "object" && job.result !== null && Array.isArray(job.result.cases), "GET /audits/{id}");
  if (job.status === "failed") check(typeof job.error?.code === "string", "GET /audits/{id}");
  return job;
}

export const isFinal = (s: JobStatus) => s === "completed" || s === "failed";

function sleep(ms: number, signal?: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException("aborted", "AbortError"));
    const onAbort = () => (clearTimeout(t), reject(new DOMException("aborted", "AbortError")));
    const t = setTimeout(() => (signal?.removeEventListener("abort", onAbort), resolve()), ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/** Poll GET /audits/{id} until completed or failed. One request at a time: the next waits for the previous
 *  answer plus the interval. Rejects with ApiError (timeout after maxWaitMs) or AbortError. */
export async function pollAudit(
  auditId: string,
  onUpdate: (job: Job) => void,
  { signal, interval = POLL_INTERVAL_MS, maxWaitMs = POLL_MAX_WAIT_MS }: { signal?: AbortSignal; interval?: number; maxWaitMs?: number } = {},
): Promise<Job> {
  const start = Date.now();
  for (;;) {
    const job = await getAudit(auditId, signal);
    onUpdate(job);
    if (isFinal(job.status)) return job;
    if (Date.now() - start >= maxWaitMs) throw new ApiError("timeout", `Stopped waiting after ${Math.round(maxWaitMs / 60000)} minutes; the audit may still be running.`);
    await sleep(interval, signal);
  }
}

/** A sentence for an error, worded for this UI. API codes are from docs/API.md. */
export function describeError(e: unknown): string {
  if (!(e instanceof ApiError)) return "Unexpected error in the web client.";
  if (e.kind === "network") return "The TraceZero API is not reachable. Start it with: python code/api.py";
  if (e.kind === "malformed" || e.kind === "timeout") return e.message;
  switch (e.code) {
    case "busy": return "The server is busy with other audits (or shutting down). Try again shortly.";
    case "dataset_not_found": return "The server has no such dataset. Refresh the dataset list.";
    case "invalid_dataset_id": return `The dataset id was rejected: ${e.message}`;
    case "audit_not_found": return "The server no longer has this audit. Audits live in memory and are lost when the API restarts.";
    case "datasets_unavailable": return "The server can't read its dataset root.";
    default: return `The API rejected the request (${e.status} ${e.code}): ${e.message}`;
  }
}
