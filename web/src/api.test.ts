import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, describeError, getAudit, listDatasets, pollAudit, submitAudit } from "./api";
import { AUDIT_ID, completed } from "./test/fixture";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function mockFetch(handler: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const fn = vi.fn(async (url: string, init?: RequestInit) => handler(url, init));
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => vi.unstubAllGlobals());

async function rejection(p: Promise<unknown>): Promise<ApiError> {
  try {
    await p;
  } catch (e) {
    return e as ApiError;
  }
  throw new Error("expected a rejection");
}

describe("API client", () => {
  it("uses the configured base and sends only dataset_id", async () => {
    const fetch = mockFetch(() => json(202, { audit_id: AUDIT_ID, status: "queued" }));
    expect(await submitAudit("demo")).toEqual({ audit_id: AUDIT_ID, status: "queued" });
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("/api/audits");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ dataset_id: "demo" });
  });

  it("lists datasets by id", async () => {
    mockFetch(() => json(200, { datasets: [{ dataset_id: "demo" }, { dataset_id: "b" }] }));
    expect(await listDatasets()).toEqual(["demo", "b"]);
  });

  it("returns a completed job with its result", async () => {
    mockFetch(() => json(200, completed));
    expect((await getAudit(AUDIT_ID)).result?.summary.rows).toBe(3);
  });

  it("maps JSON errors to their code", async () => {
    mockFetch(() => json(503, { error: { code: "busy", message: "too many audits queued or running; retry later" } }));
    const e = await rejection(submitAudit("demo"));
    expect([e.kind, e.status, e.code]).toEqual(["http", 503, "busy"]);
    expect(describeError(e)).toMatch(/busy/);
  });

  it("treats a refused connection as the API being unreachable", async () => {
    mockFetch(() => { throw new TypeError("Failed to fetch"); });
    const e = await rejection(getAudit(AUDIT_ID));
    expect(e.kind).toBe("network");
    expect(describeError(e)).toMatch(/python code\/api\.py/);
  });

  it("treats the dev proxy's non-JSON 502 as unreachable", async () => {
    mockFetch(() => new Response("proxy error", { status: 502 }));
    expect((await rejection(getAudit(AUDIT_ID))).kind).toBe("network");
  });

  it("rejects malformed responses", async () => {
    mockFetch(() => json(200, { audit_id: AUDIT_ID, status: "done" }));
    expect((await rejection(getAudit(AUDIT_ID))).kind).toBe("malformed");
    mockFetch(() => json(200, { audit_id: AUDIT_ID, status: "completed" })); // completed without result
    expect((await rejection(getAudit(AUDIT_ID))).kind).toBe("malformed");
    mockFetch(() => new Response("<html>", { status: 200 }));
    expect((await rejection(getAudit(AUDIT_ID))).kind).toBe("malformed");
  });

  it("explains a job lost on restart", async () => {
    mockFetch(() => json(404, { error: { code: "audit_not_found", message: "no audit with this id" } }));
    expect(describeError(await rejection(getAudit(AUDIT_ID)))).toMatch(/lost when the API restarts/);
  });
});

describe("pollAudit", () => {
  const sequence = (...statuses: string[]) => {
    let inFlight = 0, maxInFlight = 0, i = 0;
    const fetch = mockFetch(async () => {
      inFlight++;
      maxInFlight = Math.max(maxInFlight, inFlight);
      await new Promise((r) => setTimeout(r, 1));
      inFlight--;
      const status = statuses[Math.min(i++, statuses.length - 1)];
      return json(200, status === "completed" ? completed
        : status === "failed" ? { audit_id: AUDIT_ID, dataset_id: "x", status, error: { code: "invalid_input", message: "x: file not found" } }
        : { audit_id: AUDIT_ID, dataset_id: "x", status });
    });
    return { fetch, maxInFlight: () => maxInFlight };
  };

  it("moves queued -> running -> completed and then stops, one request at a time", async () => {
    const s = sequence("queued", "running", "running", "completed", "completed");
    const seen: string[] = [];
    const job = await pollAudit(AUDIT_ID, (j) => seen.push(j.status), { interval: 0 });
    expect(seen).toEqual(["queued", "running", "running", "completed"]);
    expect(job.result).toBeDefined();
    expect(s.fetch).toHaveBeenCalledTimes(4);
    expect(s.maxInFlight()).toBe(1);
  });

  it("stops on failed", async () => {
    sequence("running", "failed");
    const job = await pollAudit(AUDIT_ID, () => {}, { interval: 0 });
    expect([job.status, job.error?.code]).toEqual(["failed", "invalid_input"]);
  });

  it("gives up after the maximum wait", async () => {
    sequence("running");
    const e = await rejection(pollAudit(AUDIT_ID, () => {}, { interval: 0, maxWaitMs: 0 }));
    expect(e.kind).toBe("timeout");
  });

  it("stops when aborted, without further requests", async () => {
    const s = sequence("running");
    const ac = new AbortController();
    const p = pollAudit(AUDIT_ID, () => ac.abort(), { interval: 50, signal: ac.signal });
    await expect(p).rejects.toThrow(/abort/i);
    expect(s.fetch).toHaveBeenCalledTimes(1);
  });

  it("surfaces a job lost mid-poll", async () => {
    let n = 0;
    mockFetch(() => (n++ ? json(404, { error: { code: "audit_not_found", message: "gone" } }) : json(200, { audit_id: AUDIT_ID, dataset_id: "x", status: "running" })));
    expect((await rejection(pollAudit(AUDIT_ID, () => {}, { interval: 0 }))).code).toBe("audit_not_found");
  });
});
