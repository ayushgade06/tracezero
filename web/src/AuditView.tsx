import { useMemo, useState } from "react";
import { ApiError, describeError } from "./api";
import CaseView from "./CaseView";
import Diagnostics from "./Diagnostics";
import type { AuditResult, CaseResult, Job } from "./types";
import { Status, caseTone } from "./ui";

export type Tab = { name: "requests"; caseId?: string } | { name: "diagnostics" };

const count = (c: CaseResult, phase: string) => c.steps.filter((s) => s.phase === phase).length;

function Summary({ r }: { r: AuditResult }) {
  return (
    <div className="summary" aria-label="Audit summary from the engine">
      <span><b>{r.summary.rows}</b> requests</span>
      <span><b>{r.summary.steps}</b> output steps</span>
      <span className="group">
        <span>Status</span>
        {Object.entries(r.summary.status).map(([k, n]) => (
          <span key={k}><Status value={k} t={k === "INCOMPLETE" ? "warn" : undefined}>{k} <b>{n}</b></Status></span>
        ))}
      </span>
      <span className="group">
        <span>Basis</span>
        {Object.entries(r.summary.basis).map(([k, n]) => (
          <span key={k}><Status value={k}>{k} <b>{n}</b></Status></span>
        ))}
      </span>
    </div>
  );
}

function RequestList({ r, auditId, selected }: { r: AuditResult; auditId: string; selected?: string }) {
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return r.cases.filter((c) =>
      (!status || c.deletion_status === status) &&
      (!needle || c.case_id.toLowerCase().includes(needle) ||
        c.steps.some((s) => s.resource_id.toLowerCase().includes(needle) || s.operation.toLowerCase().includes(needle)) ||
        c.evaluations.some((e) => e.resource_id.toLowerCase().includes(needle))));
  }, [r, q, status]);
  return (
    <div className="requests">
      <div className="filters">
        <label className="visually-hidden" htmlFor="q">Search requests</label>
        <input id="q" type="search" placeholder="Case, resource or operation" value={q} onChange={(e) => setQ(e.target.value)} />
        <label className="visually-hidden" htmlFor="st">Filter by status</label>
        <select id="st" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All statuses</option>
          {Object.keys(r.summary.status).map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>
      <div className="scroll">
        <table aria-label="Deletion requests">
          <thead>
            <tr>
              <th>Case</th>
              <th>Status · basis</th>
              <th className="num" title="FINDING steps">Findings</th>
              <th className="num">Blockers</th>
              <th className="num">Steps</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => {
              const href = `#/audits/${auditId}/requests/${encodeURIComponent(c.case_id)}`;
              return (
                <tr key={c.case_id} className="req-row" aria-selected={c.case_id === selected}
                  onClick={() => { window.location.hash = href.slice(1); }}>
                  <td><a href={href} aria-current={c.case_id === selected ? "true" : undefined}>{c.case_id}</a></td>
                  <td>
                    <Status value={c.deletion_status} t={caseTone(c.deletion_status, c.trace?.basis ?? "")} />
                    <span className="path mono">{c.trace?.basis}</span>
                  </td>
                  <td className="num">{count(c, "FINDING") || <span className="faint">0</span>}</td>
                  <td className="num">{c.trace?.blockers?.length || <span className="faint">0</span>}</td>
                  <td className="num">{c.steps.length}</td>
                </tr>
              );
            })}
            {!rows.length && (
              <tr className="empty-row"><td colSpan={5}>{r.cases.length ? "No request matches these filters." : "The request file has no rows."}</td></tr>
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
  const diagCount = r.diagnostics.parser.length;
  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="stack" style={{ gap: 8 }}>
        <div className="audit-head">
          <h1>Audit of <span className="mono">{job.dataset_id}</span></h1>
          <Status value="completed" />
          <span className="faint mono" style={{ fontSize: "var(--fs-meta)" }}>{job.audit_id} · {r.requests}</span>
        </div>
        <Summary r={r} />
        <p className="faint" style={{ fontSize: "var(--fs-meta)" }}>
          Results quote evidence and may contain personal data. They live only in the API's memory until it restarts.
        </p>
      </div>

      {r.warnings.length > 0 && (
        <div className="notice warn" role="status">
          <h2>Engine warnings ({r.warnings.length})</h2>
          {r.warnings.map((w, i) => <p key={i} className="warning-text">{w}</p>)}
        </div>
      )}

      <nav className="tabs" aria-label="Audit views">
        <a href={`${base}`} aria-current={tab.name === "requests" ? "page" : undefined}>Requests ({r.cases.length})</a>
        <a href={`${base}/diagnostics`} aria-current={tab.name === "diagnostics" ? "page" : undefined}>
          Diagnostics ({diagCount ? `${diagCount} parser` : "no parser issues"})
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
        <p className="faint mono" style={{ fontSize: "var(--fs-meta)" }}>{auditId}</p>
        <p style={{ marginTop: 4 }}>
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
        <p className="faint mono" style={{ fontSize: "var(--fs-meta)" }}>{job.error?.code} · {job.audit_id}</p>
      </div>
    );
  }
  if (job.status === "completed") return <Completed job={job} tab={tab} />;
  // Only the state is announced; the elapsed-time line below changes every second and stays out of the live region.
  return (
    <div className="notice">
      <div role="status" style={{ display: "grid", gap: "var(--s1)" }}>
        <h2 style={{ display: "flex", gap: 12, alignItems: "center" }}>
          <Status value={job.status} /> Audit of <span className="mono">{job.dataset_id}</span>
        </h2>
        <p>
          {job.status === "queued"
            ? "Accepted. Waiting for a free worker on the server."
            : "audit() is reading the evidence and evaluating every request. The demo takes a few seconds; a large dataset can take longer."}
        </p>
      </div>
      <p className="faint" style={{ fontSize: "var(--fs-meta)" }}>
        Checking every second · {since(startedAt)} since this page started watching · <span className="mono">{job.audit_id}</span>
      </p>
    </div>
  );
}
