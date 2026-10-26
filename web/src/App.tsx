import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, describeError, getHealth, isFinal, listDatasets, pollAudit, submitAudit } from "./api";
import AuditView, { type Tab } from "./AuditView";
import type { Job, JobStatus } from "./types";
import { Status } from "./ui";

type Route = { auditId?: string; tab: Tab };
type SessionEntry = { audit_id: string; dataset_id: string; submitted_at: string; status: JobStatus };

export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  if (parts[0] !== "audits" || !parts[1]) return { tab: { name: "requests" } };
  if (parts[2] === "diagnostics") return { auditId: parts[1], tab: { name: "diagnostics" } };
  return { auditId: parts[1], tab: { name: "requests", caseId: parts[2] === "requests" ? parts[3] : undefined } };
}

// This tab's audits. sessionStorage survives a reload of this tab only; the server is what actually keeps jobs.
const SESSION_KEY = "tracezero.session";
function loadSession(): SessionEntry[] {
  try {
    const v = JSON.parse(sessionStorage.getItem(SESSION_KEY) ?? "[]");
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}
function saveSession(entries: SessionEntry[]) {
  try {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify(entries));
  } catch { /* storage unavailable: the list just won't survive a reload */ }
}

function useHash() {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const on = () => setHash(window.location.hash);
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

type Health = "checking" | "ok" | "down";
function useHealth(every = 10000) {
  const [health, setHealth] = useState<Health>("checking");
  useEffect(() => {
    let stop = false;
    let t: ReturnType<typeof setTimeout>;
    const check = async () => {
      try {
        await getHealth();
        if (!stop) setHealth("ok");
      } catch {
        if (!stop) setHealth("down");
      }
      if (!stop) t = setTimeout(check, every);
    };
    check();
    return () => { stop = true; clearTimeout(t); };
  }, [every]);
  return health;
}

function RunBar({ health, session, onSubmitted }: { health: Health; session: SessionEntry[]; onSubmitted: (e: SessionEntry) => void }) {
  const [datasets, setDatasets] = useState<string[] | null>(null);
  const [listError, setListError] = useState<unknown>(null);
  const [dataset, setDataset] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<unknown>(null);

  const load = useCallback(async () => {
    setListError(null);
    try {
      const ids = await listDatasets();
      setDatasets(ids);
      setDataset((d) => (ids.includes(d) ? d : ids[0] ?? ""));
    } catch (e) {
      setDatasets(null);
      setListError(e);
    }
  }, []);
  useEffect(() => { if (health === "ok" && datasets === null) load(); }, [health, datasets, load]);

  const active = session.find((s) => s.dataset_id === dataset && !isFinal(s.status));
  const run = async () => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      const { audit_id, status } = await submitAudit(dataset);
      onSubmitted({ audit_id, dataset_id: dataset, submitted_at: new Date().toISOString(), status });
    } catch (e) {
      setSubmitError(e);
      if (e instanceof ApiError && e.code === "dataset_not_found") load();
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="runbar" role="region" aria-label="Run an audit">
      <label htmlFor="dataset">Dataset</label>
      {datasets === null ? (
        <span className="muted">{listError ? describeError(listError) : health === "down" ? "Unavailable while the API is unreachable." : "Loading datasets…"}</span>
      ) : datasets.length === 0 ? (
        <span className="muted">The server's dataset root has no datasets (directories with deletion_requests.csv).</span>
      ) : (
        <select id="dataset" value={dataset} onChange={(e) => setDataset(e.target.value)}>
          {datasets.map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
      )}
      <button type="button" className="primary" onClick={run}
        disabled={!dataset || submitting || !!active || health !== "ok"}>
        {submitting ? "Submitting…" : "Run audit"}
      </button>
      {listError != null && <button type="button" onClick={load}>Retry</button>}
      {datasets !== null && <button type="button" className="link" onClick={load}>Refresh list</button>}
      <span className="hint" role="status">
        {submitError ? <span className="tone-bad">{describeError(submitError)}</span>
          : active ? <>An audit of <span className="mono">{dataset}</span> is already {active.status}: <a href={`#/audits/${active.audit_id}`}>open it</a>.</>
          : "Runs the engine on the server with its default policy and settings. Nothing is written to disk."}
      </span>
    </div>
  );
}

function Home({ session }: { session: SessionEntry[] }) {
  return (
    <div className="stack" style={{ gap: 32 }}>
      <div className="intro">
        <h1>Audit deletion requests against the evidence</h1>
        <p className="muted">
          TraceZero decides, for each deletion request, whether deletion was actually complete at the request's audit time,
          and ties every finding, remediation and verification step to the evidence that supports it.
        </p>
        <ol>
          <li>Choose a dataset on the server and run an audit.</li>
          <li>Review the requests that need attention: residual data or an undecided state.</li>
          <li>Open a request to read its evidence trace: citations, readings, contradicting and inadmissible evidence.</li>
          <li>Check the diagnostics to see what the parsers couldn't read.</li>
        </ol>
      </div>
      <section className="session" aria-labelledby="h-session">
        <h2 id="h-session">Audits in this browser tab</h2>
        {session.length ? (
          <div className="table-wrap" style={{ maxWidth: 820 }}>
            <table>
              <thead><tr><th>Audit</th><th>Dataset</th><th>Submitted</th><th>Last known status</th></tr></thead>
              <tbody>
                {[...session].reverse().map((s) => (
                  <tr key={s.audit_id}>
                    <td><a className="mono" href={`#/audits/${s.audit_id}`}>{s.audit_id.slice(0, 12)}…</a></td>
                    <td className="mono">{s.dataset_id}</td>
                    <td className="mono">{new Date(s.submitted_at).toLocaleString()}</td>
                    <td><Status value={s.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="none">No audits run from this tab yet. The server keeps no audit history; this list is kept in this tab only.</p>
        )}
      </section>
    </div>
  );
}

export default function App() {
  const hash = useHash();
  const route = parseRoute(hash);
  const health = useHealth();
  const [session, setSession] = useState<SessionEntry[]>(loadSession);
  const [job, setJob] = useState<Job>();
  const [error, setError] = useState<unknown>();
  const [retry, setRetry] = useState(0);
  const startedAt = useRef(Date.now());
  const cache = useRef(new Map<string, Job>()); // final jobs: never change, so never refetched

  const remember = useCallback((update: (s: SessionEntry[]) => SessionEntry[]) => {
    setSession((s) => { const next = update(s); saveSession(next); return next; });
  }, []);

  // Watch the audit in the URL: poll while queued/running, stop on completed/failed, on leaving, or on error.
  useEffect(() => {
    const id = route.auditId;
    setError(undefined);
    if (!id) { setJob(undefined); return; }
    const cached = cache.current.get(id);
    if (cached) { setJob(cached); return; }
    setJob(undefined);
    startedAt.current = Date.now();
    const ac = new AbortController();
    pollAudit(id, (j) => {
      if (ac.signal.aborted) return;
      setJob(j);
      remember((s) => s.map((e) => (e.audit_id === j.audit_id ? { ...e, status: j.status } : e)));
    }, { signal: ac.signal })
      .then((j) => cache.current.set(j.audit_id, j))
      .catch((e) => {
        if (ac.signal.aborted) return;
        setError(e);
        if (e instanceof ApiError && e.code === "audit_not_found") remember((s) => s.filter((x) => x.audit_id !== id));
      });
    return () => ac.abort();
  }, [route.auditId, retry, remember]);

  const onSubmitted = (e: SessionEntry) => {
    remember((s) => [...s, e]);
    window.location.hash = `/audits/${e.audit_id}`;
  };

  return (
    <>
      <header className="topbar">
        <a className="wordmark" href="#/">TraceZero<span>deletion audit</span></a>
        <span className="spacer" />
        <span className="health" role="status" aria-live="polite">
          {health === "ok" ? <Status value="ok" t="ok">API connected</Status>
            : health === "down" ? <Status value="down" t="bad">API unreachable</Status>
            : <Status value="checking" t="neutral">Checking API…</Status>}
          <span className="faint">local, no authentication</span>
        </span>
      </header>
      <RunBar health={health} session={session} onSubmitted={onSubmitted} />
      <main>
        {health === "down" && !route.auditId && (
          <div className="notice bad" role="alert" style={{ marginBottom: 24 }}>
            <h2>The TraceZero API is not reachable</h2>
            <p>Start it from the repository root, then this page reconnects by itself:</p>
            <pre>python code/api.py</pre>
          </div>
        )}
        {route.auditId
          ? <AuditView auditId={route.auditId} job={job} error={error} startedAt={startedAt.current} tab={route.tab} onRetry={() => setRetry((n) => n + 1)} />
          : <Home session={session} />}
      </main>
    </>
  );
}
