import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App, { parseRoute } from "./App";
import { AUDIT_ID, HOSTILE_EXCERPT, completed, result } from "./test/fixture";
import type { Job } from "./types";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

type Routes = Record<string, (init?: RequestInit) => Response>;
function api(routes: Routes) {
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${url.replace(/^\/api/, "")}`;
    const route = routes[key];
    if (!route) throw new TypeError("Failed to fetch");
    return route(init);
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}
const healthy = (extra: Routes = {}) => api({
  "GET /health": () => json(200, { status: "ok" }),
  "GET /datasets": () => json(200, { datasets: [{ dataset_id: "demo" }, { dataset_id: "synthetic" }] }),
  ...extra,
});
const withJob = (job: Job) => healthy({ [`GET /audits/${job.audit_id}`]: () => json(200, job) });

async function open(hash: string) {
  window.location.hash = hash;
  render(<App />);
}

beforeEach(() => { sessionStorage.clear(); window.location.hash = ""; });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

describe("routing", () => {
  it("parses audit, request and diagnostics routes", () => {
    expect(parseRoute("")).toEqual({ tab: { name: "requests" } });
    expect(parseRoute("#/audits/abc/requests/case_1")).toEqual({ auditId: "abc", tab: { name: "requests", caseId: "case_1" } });
    expect(parseRoute("#/audits/abc/diagnostics")).toEqual({ auditId: "abc", tab: { name: "diagnostics" } });
  });
});

describe("start", () => {
  it("shows API health and the server's datasets, with an empty session", async () => {
    healthy();
    await open("");
    expect(await screen.findByText("API connected")).toBeTruthy();
    const select = (await screen.findByLabelText("Dataset")) as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).toEqual(["demo", "synthetic"]);
    expect(screen.getByText(/No audits run from this tab yet/)).toBeTruthy();
  });

  it("says so when the API is unreachable, and disables running", async () => {
    api({});
    await open("");
    expect(await screen.findByText("The TraceZero API is not reachable")).toBeTruthy();
    expect(screen.getByText("API unreachable")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Run audit" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("handles a dataset root with no datasets", async () => {
    api({ "GET /health": () => json(200, { status: "ok" }), "GET /datasets": () => json(200, { datasets: [] }) });
    await open("");
    expect(await screen.findByText(/has no datasets/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Run audit" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("reports a busy server without navigating", async () => {
    healthy({ "POST /audits": () => json(503, { error: { code: "busy", message: "too many audits queued or running; retry later" } }) });
    await open("");
    await screen.findByLabelText("Dataset");
    fireEvent.click(screen.getByRole("button", { name: "Run audit" }));
    expect(await screen.findByText(/server is busy/)).toBeTruthy();
    expect(window.location.hash).toBe("");
  });

  it("submits once, polls through the states and shows the result only when completed", async () => {
    const states: Job[] = [
      { audit_id: AUDIT_ID, dataset_id: "demo", status: "queued" },
      { audit_id: AUDIT_ID, dataset_id: "demo", status: "running" },
      { ...completed, dataset_id: "demo" },
    ];
    let polls = 0;
    const fetch = healthy({
      "POST /audits": () => json(202, { audit_id: AUDIT_ID, status: "queued" }),
      [`GET /audits/${AUDIT_ID}`]: () => json(200, states[Math.min(polls++, 2)]),
    });
    await open("");
    await screen.findByLabelText("Dataset");
    const button = screen.getByRole("button", { name: "Run audit" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(await screen.findByText(/Waiting for a free worker/)).toBeTruthy();
    expect(await screen.findByText(/is reading the evidence/, {}, { timeout: 3000 })).toBeTruthy();
    expect(screen.queryByLabelText("Audit summary from the engine")).toBeNull();
    expect(await screen.findByLabelText("Audit summary from the engine", {}, { timeout: 3000 })).toBeTruthy();
    expect(fetch.mock.calls.filter(([, i]) => i?.method === "POST")).toHaveLength(1);
    expect(JSON.parse(sessionStorage.getItem("tracezero.session")!)[0]).toMatchObject({ audit_id: AUDIT_ID, status: "completed" });
  });
});

describe("completed audit", () => {
  it("shows the engine's summary counts and warnings, and nothing it did not compute", async () => {
    withJob(completed);
    await open(`#/audits/${AUDIT_ID}`);
    const summary = await screen.findByLabelText("Audit summary from the engine");
    expect(summary.textContent).toContain("3 requests");
    expect(summary.textContent).toContain("4 output steps");
    expect(summary.textContent).toContain("INCOMPLETE 2");
    expect(summary.textContent).toContain("BLOCKED 1");
    expect(screen.getByText(/sql_point_in_time_header/)).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/%|confidence score|score/i);
  });

  it("lists requests, filters them, and opens one", async () => {
    withJob(completed);
    await open(`#/audits/${AUDIT_ID}`);
    const table = await screen.findByRole("table", { name: "Deletion requests" });
    expect(within(table).getAllByRole("link").map((a) => a.textContent)).toEqual(["case_a", "case_b", "case_c"]);
    fireEvent.change(screen.getByLabelText("Filter by status"), { target: { value: "NOT_APPLICABLE" } });
    expect(within(table).getAllByRole("link").map((a) => a.textContent)).toEqual(["case_c"]);
    fireEvent.change(screen.getByLabelText("Search requests"), { target: { value: "nothing-like-this" } });
    expect(within(table).getByText("No request matches these filters.")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Filter by status"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Search requests"), { target: { value: "account_2" } });
    expect(within(table).getAllByRole("link").map((a) => a.textContent)).toEqual(["case_b"]);
    act(() => { window.location.hash = `#/audits/${AUDIT_ID}/requests/case_b`; window.dispatchEvent(new HashChangeEvent("hashchange")); });
    expect(await screen.findByRole("heading", { name: /case_b/ })).toBeTruthy();
  });

  it("renders the evidence trace with citations, locators and evidence lists kept apart", async () => {
    withJob(completed);
    await open(`#/audits/${AUDIT_ID}/requests/case_a`);
    const panel = (await screen.findByText("search_1", { selector: ".rid" })).closest("details")!;
    const p = within(panel as HTMLElement);
    expect(panel.textContent).toContain("VIOLATION");
    expect(p.getByText(/requires DELETE \(rule_03\)/)).toBeTruthy();
    expect(p.getAllByText("ev_2:docs[1]").length).toBeGreaterThan(0);
    expect(panel.textContent).toContain("table=docs");
    expect(panel.textContent).toContain("evidence/databases/snap.sql");
    expect(p.getByText(/Several readings share the latest time/)).toBeTruthy();
    expect(p.getByText("Contradicting readings").parentElement!.textContent).toContain("(1)");
    expect(panel.textContent).toContain("ev_3:L7");
    expect(p.getByText("Inadmissible evidence").parentElement!.textContent).toContain("(1)");
    expect(panel.textContent).toContain("FILE_CAPTURED_AFTER_AUDIT");
    const withheld = (screen.getByText("crm_1", { selector: ".rid" })).closest("details")!;
    expect(withheld.textContent).toContain("Withheld finding");
    expect(withheld.textContent).toContain("user_77");
    expect(document.body.textContent).toContain("SEARCH_INDEX_ENTRY requires DELETE under rule_03");
    expect(document.body.textContent).toContain("CITATION_TIE");
  });

  it("shows neutral fallbacks for missing optional fields", async () => {
    withJob(completed);
    await open(`#/audits/${AUDIT_ID}/requests/case_b`);
    expect((await screen.findAllByText("STATE_UNKNOWN:HISTORICAL_READINGS_ONLY")).length).toBe(2); // blocker and reason
    expect(screen.getByText("No citation in the result for this evaluation.")).toBeTruthy();
    expect(screen.getByText("system not recorded")).toBeTruthy();
    expect(screen.getByText(/No remediation/)).toBeTruthy();
    expect(screen.getByText(/does not carry the requester/)).toBeTruthy();
  });

  it("handles a request with no steps or evaluations", async () => {
    withJob(completed);
    await open(`#/audits/${AUDIT_ID}/requests/case_c`);
    expect(await screen.findByText(/steps_json is \[\]/)).toBeTruthy();
    expect(screen.getByText("No resource was linked to this request.")).toBeTruthy();
  });

  it("handles a result with no cases", async () => {
    withJob({ ...completed, result: { ...result, cases: [], summary: { rows: 0, status: {}, basis: {}, steps: 0 } } });
    await open(`#/audits/${AUDIT_ID}`);
    expect(await screen.findByText("The request file has no rows.")).toBeTruthy();
    expect(screen.getByText(/nothing to investigate/)).toBeTruthy();
  });
});

describe("diagnostics", () => {
  it("groups parser diagnostics by severity and shows coverage as reported", async () => {
    withJob(completed);
    await open(`#/audits/${AUDIT_ID}/diagnostics`);
    const errors = (await screen.findByText("Errors")).closest("section")!;
    expect(errors.textContent).toContain("manifest entry has no file");
    expect(errors.textContent).not.toContain("unparsed log line");
    const warnings = screen.getByText("Warnings").closest("section")!;
    expect(warnings.textContent).toContain("unparsed log line");
    expect(warnings.textContent).toContain("line=4");
    expect(document.body.textContent).toContain("Unreadable files");
    expect(document.body.textContent).toContain("ev_3 L2: ticket_88");
    expect(document.body.textContent).toMatch(/not audit findings/);
  });

  it("renders evidence excerpts as plain text, never as HTML", async () => {
    withJob(completed);
    await open(`#/audits/${AUDIT_ID}/diagnostics`);
    await screen.findByText("Errors");
    const excerpts = [...document.querySelectorAll("pre.excerpt")].map((e) => e.textContent);
    expect(excerpts).toContain(HOSTILE_EXCERPT);
    expect(document.querySelector("main img, main script")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it("says so when the parsers reported nothing", async () => {
    withJob({ ...completed, result: { ...result, diagnostics: { ...result.diagnostics, parser: [] } } });
    await open(`#/audits/${AUDIT_ID}/diagnostics`);
    expect(await screen.findByText(/The parsers reported nothing/)).toBeTruthy();
  });
});

describe("failures", () => {
  it("shows a failed audit's error code and every listed problem", async () => {
    const message = "x/evidence_files.csv: file not found\nx/deletion_requests.csv: missing required column(s)";
    withJob({ audit_id: AUDIT_ID, dataset_id: "x", status: "failed", error: { code: "invalid_input", message } });
    await open(`#/audits/${AUDIT_ID}`);
    expect(await screen.findByText("x", { selector: "h2 .mono" })).toBeTruthy();
    expect(screen.getByText(/rejected the dataset's input files/)).toBeTruthy();
    expect(document.querySelector(".notice pre")!.textContent).toBe(message);
    expect(screen.queryByLabelText("Audit summary from the engine")).toBeNull();
  });

  it("explains an audit lost on restart and drops it from the session", async () => {
    sessionStorage.setItem("tracezero.session", JSON.stringify([{ audit_id: AUDIT_ID, dataset_id: "demo", submitted_at: new Date().toISOString(), status: "running" }]));
    healthy({ [`GET /audits/${AUDIT_ID}`]: () => json(404, { error: { code: "audit_not_found", message: "gone" } }) });
    await open(`#/audits/${AUDIT_ID}`);
    expect(await screen.findByText("Audit not found on the server")).toBeTruthy();
    await waitFor(() => expect(JSON.parse(sessionStorage.getItem("tracezero.session")!)).toEqual([]));
  });

  it("separates a connection failure from an audit result", async () => {
    api({});
    await open(`#/audits/${AUDIT_ID}`);
    expect(await screen.findByText("Could not load this audit")).toBeTruthy();
    expect(screen.getByText(/not reachable/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Check again" })).toBeTruthy();
  });
});
