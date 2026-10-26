import { useMemo, useState } from "react";
import { ApiError, describeError } from "./api";
import CaseView from "./CaseView";
import Diagnostics from "./Diagnostics";
import { caseLabel, caseSummary } from "./plain";
import type { AuditResult, CaseResult, Job } from "./types";
import { Status } from "./ui";

export type Tab = { name: "requests"; caseId?: string } | { name: "diagnostics" };

type Group = "found" | "unsure" | "deleted" | "na";
const groupOf = (c: CaseResult): Group | null =>
  c.deletion_status === "COMPLETE" ? "deleted"
    : c.deletion_status === "NOT_APPLICABLE" ? "na"
      : c.deletion_status === "INCOMPLETE" ? (c.trace?.basis === "FINDINGS" ? "found" : "unsure") : null;

const GROUPS: { key: Group; tone: "bad" | "warn" | "ok" | "neutral"; chip: string; one: string; many: string }[] = [
  { key: "found", tone: "bad", chip: "Data still found", one: "still has data left", many: "still have data left" },
  { key: "unsure", tone: "warn", chip: "Can't confirm", one: "can't be confirmed", many: "can't be confirmed" },
  { key: "deleted", tone: "ok", chip: "Deleted", one: "was really deleted", many: "were really deleted" },
  { key: "na", tone: "neutral", chip: "Not a deletion request", one: "wasn't a deletion request", many: "weren't deletion requests" },
];

/** One sentence and a few counts, each a plain count of the result's own cases. */
function Summary({ r }: { r: AuditResult }) {
  const count = (g: Group) => r.cases.filter((c) => groupOf(c) === g).length;
  const parts = GROUPS.filter((g) => count(g.key)).map((g) => `${count(g.key)} ${count(g.key) === 1 ? g.one : g.many}`);
  const sentence = parts.length > 1 ? `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}` : parts[0];
  return (
    <div className="stack" style={{ gap: 12 }} aria-label="Audit summary from the engine">
      <p className="lead">
        {r.cases.length ? <>Out of {r.cases.length} {r.cases.length === 1 ? "request" : "requests"}, {sentence}.</> : "The request file has no requests to check."}
      </p>
      <div className="counts">
        {GROUPS.filter((g) => count(g.key)).map((g) => <Status key={g.key} value={g.key} t={g.tone}>{g.chip} · {count(g.key)}</Status>)}
      </div>
      <details className="tech">
        <summary>Technical summary</summary>
        <p className="kv-line">
          {r.summary.rows} requests · {r.summary.steps} output steps · status{" "}
          {Object.entries(r.summary.status).map(([k, n]) => `${k} ${n}`).join(", ")} · basis{" "}
          {Object.entries(r.summary.basis).map(([k, n]) => `${k} ${n}`).join(", ")} · {r.requests}
        </p>
      </details>
    </div>
  );
}

const STATUS_FILTER: Record<string, string> = {
  COMPLETE: "Deleted", INCOMPLETE: "Not deleted or can't confirm", NOT_APPLICABLE: "Not a deletion request",
};

function RequestList({ r, auditId, selected }: { r: AuditResult; auditId: string; selected?: string }) {
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return r.cases.filter((c) =>
      (!status || c.deletion_status === status) &&
      (!needle || c.case_id.toLowerCase().includes(needle) ||
        c.steps.some((s) => s.resource_id.toLowerCase().includes(needle) || s.system_id.toLowerCase().includes(needle)) ||
        c.evaluations.some((e) => e.resource_id.toLowerCase().includes(needle))));
  }, [r, q, status]);
  return (
    <div className="requests">
      <div className="filters">
        <label className="visually-hidden" htmlFor="q">Search requests</label>
        <input id="q" type="search" placeholder="Search requests or places" value={q} onChange={(e) => setQ(e.target.value)} />
        <label className="visually-hidden" htmlFor="st">Filter by status</label>
        <select id="st" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All</option>
          {Object.keys(r.summary.status).map((s) => <option key={s} value={s}>{STATUS_FILTER[s] ?? s}</option>)}
        </select>
      </div>
      <div className="list">
        <table aria-label="Deletion requests">
          <thead><tr><th>Request</th><th>Result</th></tr></thead>
          <tbody>
            {rows.map((c) => {
              const href = `#/audits/${auditId}/requests/${encodeURIComponent(c.case_id)}`;
              const { label, tone } = caseLabel(c);
              return (
                <tr key={c.case_id} className="req-row" aria-selected={c.case_id === selected}
                  onClick={() => { window.location.hash = href.slice(1); }}>
                  <td>
                    <a href={href} aria-current={c.case_id === selected ? "true" : undefined}>{c.case_id}</a>
                    <span className="sub">{caseSummary(c)}</span>
                  </td>
                  <td><Status value={c.deletion_status} t={tone}>{label}</Status></td>
                </tr>
              );
            })}
            {!rows.length && (
              <tr className="empty-row"><td colSpan={2}>{r.cases.length ? "No request matches these filters." : "The request file has no rows."}</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Completed({ job, tab }: { job: Job; tab: Tab }) {
  const r = job.result!;
  const files = useMemo(() => new Map(r.diagnostics.files.map((f) => [f.file_id, f.file_path])), [r]);
  const selected = tab.name === "requests" ? r.cases.find((c) => c.case_id === tab.caseId) ?? r.cases[0] : undefined;
  const base = `#/audits/${job.audit_id}`;
  const problems = r.diagnostics.parser.filter((d) => d.severity !== "info").length;
  return (
    <div className="stack" style={{ gap: 28 }}>
      <div className="audit-head">
        <div className="title">
          <h1>Audit of <span className="mono" style={{ fontSize: "0.9em" }}>{job.dataset_id}</span></h1>
          <Status value="completed">Finished</Status>
        </div>
        <Summary r={r} />
        <p className="faint small">
          Results quote the evidence and may include personal data. They are kept only until the API restarts.
        </p>
        {r.warnings.length > 0 && (
          <details className="notes-box">
            <summary>{r.warnings.length === 1 ? "1 note" : `${r.warnings.length} notes`} from the engine about this audit</summary>
            {r.warnings.map((w, i) => <p key={i} className="warning-text">{w}</p>)}
          </details>
        )}
      </div>

      <nav className="tabs" aria-label="Audit views">
        <a href={`${base}`} aria-current={tab.name === "requests" ? "page" : undefined}>Requests ({r.cases.length})</a>
        <a href={`${base}/diagnostics`} aria-current={tab.name === "diagnostics" ? "page" : undefined}>
          Files read ({r.diagnostics.files.length}{problems ? `, ${problems} with issues` : ""})
        </a>
      </nav>

      {tab.name === "diagnostics" ? <Diagnostics result={r} /> : (
        <div className="workspace">
          <RequestList r={r} auditId={job.audit_id} selected={selected?.case_id} />
          {selected ? <CaseView key={selected.case_id} c={selected} files={files} /> : (
            <div className="notice"><h2>No requests</h2><p>The audited request file has no rows, so there is nothing to investigate.</p></div>
          )}
        </div>
      )}
    </div>
  );
}

function since(ms: number) {
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

export default function AuditView({ auditId, job, error, startedAt, tab, onRetry }: {
  auditId: string; job?: Job; error?: unknown; startedAt: number; tab: Tab; onRetry: () => void;
}) {
  if (error) {
    const lost = error instanceof ApiError && error.code === "audit_not_found";
    const timeout = error instanceof ApiError && error.kind === "timeout";
    return (
      <div className={`notice ${timeout ? "warn" : "bad"}`} role="alert">
        <h2>{lost ? "Audit not found on the server" : timeout ? "Still not finished" : "Could not load this audit"}</h2>
        <p>{describeError(error)}</p>
        {job && !lost && <p className="faint">Last known status: <span className="mono">{job.status}</span></p>}
        <p className="faint mono small">{auditId}</p>
        <p className="row" style={{ marginTop: 4 }}>
          {!lost && <button type="button" onClick={onRetry}>Check again</button>} <a href="#/">Start a new audit</a>
        </p>
      </div>
    );
  }
  if (!job) return <div className="notice" role="status"><p>Loading audit <span className="mono">{auditId}</span>…</p></div>;
  if (job.status === "failed") {
    return (
      <div className="notice bad" role="alert">
        <h2>Audit of <span className="mono">{job.dataset_id}</span> failed</h2>
        <p>
          {job.error?.code === "invalid_input"
            ? "The engine rejected the dataset's input files. Nothing was audited. Every problem it found is listed below."
            : "The engine stopped with an internal error. The server logs only the error type, never evidence."}
        </p>
        <pre>{job.error?.message}</pre>
        <p className="faint mono small">{job.error?.code} · {job.audit_id}</p>
      </div>
    );
  }
  if (job.status === "completed") return <Completed job={job} tab={tab} />;
  // Only the state is announced; the elapsed-time line below changes every second and stays out of the live region.
  return (
    <div className="notice">
      <div role="status" style={{ display: "grid", gap: "var(--s2)" }}>
        <h2 className="row">
          <Status value={job.status}>{job.status === "queued" ? "Waiting" : "Running"}</Status> Audit of <span className="mono">{job.dataset_id}</span>
        </h2>
        <p>
          {job.status === "queued"
            ? "Accepted. Waiting for a free worker on the server."
            : "TraceZero is reading the evidence and checking every request. The demo takes a few seconds."}
        </p>
      </div>
      <p className="faint small">
        Checking every second · {since(startedAt)} since this page started watching · <span className="mono">{job.audit_id}</span>
      </p>
    </div>
  );
}
