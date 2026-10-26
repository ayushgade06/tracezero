import type { AuditResult, ParserDiagnostic } from "./types";
import { KV, None, Status } from "./ui";

const FORMAT: Record<string, string> = { sql: "Database export", json: "JSON export", log: "Log file", txt: "Text file", pdf: "PDF document", image: "Screenshot" };

const SEVERITIES = ["error", "warning", "info"];
const SEVERITY_LABEL: Record<string, string> = { error: "Errors", warning: "Warnings", info: "Information" };
const SEVERITY_TONE: Record<string, "bad" | "warn" | "neutral"> = { error: "bad", warning: "warn", info: "neutral" };

function ParserGroup({ severity, rows }: { severity: string; rows: ParserDiagnostic[] }) {
  return (
    <section aria-labelledby={`h-sev-${severity}`}>
      <h3 id={`h-sev-${severity}`}>
        <Status value={severity} t={SEVERITY_TONE[severity]}>{SEVERITY_LABEL[severity] ?? severity}</Status>
        <span className="count">({rows.length})</span>
      </h3>
      <div className="table-wrap">
        <table className="fixed">
          <colgroup><col style={{ width: "13%" }} /><col style={{ width: "24%" }} /><col style={{ width: "23%" }} /><col /></colgroup>
          <thead><tr><th>File</th><th>Locator</th><th>Reason</th><th>Excerpt</th></tr></thead>
          <tbody>
            {rows.map((d, i) => (
              <tr key={i}>
                <td className="mono">{d.file_id}</td>
                <td><KV value={d.locator} /></td>
                <td>{d.reason}</td>
                <td>{d.excerpt ? <pre className="excerpt">{d.excerpt}</pre> : <span className="faint">—</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export default function Diagnostics({ result }: { result: AuditResult }) {
  const { files, parser, coverage } = result.diagnostics;
  const severities = [...SEVERITIES, ...new Set(parser.map((d) => d.severity).filter((s) => !SEVERITIES.includes(s)))];
  const others = Object.entries(coverage.other_id_tokens ?? {});
  const withIssues = files.filter((f) => f.diagnostics > 0).length;
  const unused = coverage.unresolved.length;
  return (
    <div className="stack" style={{ gap: 32 }}>
      <div className="card card-pad stack" style={{ gap: 12 }}>
        <h2 style={{ fontSize: "var(--fs-lg)" }}>
          TraceZero read {files.length} evidence {files.length === 1 ? "file" : "files"}.
        </h2>
        <div className="stat-line">
          {withIssues
            ? <Status value="warning" t="warn">{withIssues} {withIssues === 1 ? "file" : "files"} had reading issues</Status>
            : <Status value="ok" t="ok">Every file was read without issues</Status>}
          {coverage.unreadable.length > 0 && <Status value="error" t="bad">{coverage.unreadable.length} could not be read</Status>}
          {unused
            ? <Status value="warning" t="warn">{unused} {unused === 1 ? "id" : "ids"} in the files couldn't be used</Status>
            : <Status value="ok" t="ok">Every id found in the files was used</Status>}
        </div>
        <p className="muted small" style={{ maxWidth: "80ch" }}>
          These are reports about reading the evidence files, not audit findings. A reading issue means a file (or part of it)
          gave no information; it does not mean the data was deleted. Findings are on the request pages.
        </p>
      </div>

      <section aria-labelledby="h-files" className="stack" style={{ gap: 8 }}>
        <h3 id="h-files">Evidence files <span className="count faint small" style={{ fontWeight: 400 }}>({files.length}, from evidence_files.csv)</span></h3>
        {files.length ? (
          <div className="table-wrap">
            <table>
              <thead><tr><th>File</th><th>Path</th><th>Type</th><th className="num">Facts found</th><th className="num">Issues</th></tr></thead>
              <tbody>
                {files.map((f) => (
                  <tr key={f.file_id}>
                    <td className="mono">{f.file_id}</td>
                    <td className="mono" style={{ overflowWrap: "anywhere" }}>{f.file_path}</td>
                    <td>{FORMAT[f.format] ?? f.format}</td>
                    <td className="num">{f.observations}</td>
                    <td className={`num${f.diagnostics ? " tone-warn" : ""}`}>{f.diagnostics}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <None>The manifest lists no evidence files.</None>}
      </section>

      <div className="stack" style={{ gap: 24 }}>
        <h3>Reading issues <span className="faint small" style={{ fontWeight: 400 }}>({parser.length})</span></h3>
        {parser.length ? severities.map((s) => {
          const rows = parser.filter((d) => d.severity === s);
          return rows.length ? <ParserGroup key={s} severity={s} rows={rows} /> : null;
        }) : <None>The parsers reported nothing: no unparsed lines, missing files, unsupported formats or parser errors.</None>}
      </div>

      <details className="tech card card-pad">
      <summary>Technical details: ingest coverage</summary>
      <div className="tech-block">
      <section aria-labelledby="h-cov">
        <h3 id="h-cov">Ingest coverage</h3>
        <dl className="facts">
          <dt>Files with readable text</dt><dd className="mono">{coverage.files}</dd>
          <dt>Resource ids in raw text</dt><dd className="mono">{coverage.tokens} distinct (counted per file)</dd>
          <dt>Represented by an observation</dt><dd className="mono">{coverage.represented}</dd>
          <dt>Unresolved</dt><dd className="mono">{coverage.unresolved.length}</dd>
          <dt>Unreadable files</dt><dd className="mono">{coverage.unreadable.length ? coverage.unreadable.join(", ") : "none"}</dd>
        </dl>
        <p className="faint" style={{ marginTop: 8, fontSize: "var(--fs-meta)" }}>
          An unresolved id appears in a file's text, but no observation from that file mentions it, so the audit could not use it.
        </p>
      </section>

      <section aria-labelledby="h-unres">
        <h3 id="h-unres">Unresolved resource ids <span className="count">({coverage.unresolved.length})</span></h3>
        {coverage.unresolved.length ? (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Token</th><th>File</th><th className="num">Line</th><th>Excerpt</th></tr></thead>
              <tbody>
                {coverage.unresolved.map((u, i) => (
                  <tr key={i}>
                    <td className="mono">{u.token}</td>
                    <td className="mono">{u.file_id}</td>
                    <td className="num mono">{u.line}</td>
                    <td><pre className="excerpt">{u.excerpt}</pre></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <None>Every resource id found in raw text is represented by an observation.</None>}
      </section>

      <section aria-labelledby="h-other">
        <h3 id="h-other">Other id-like tokens <span className="count">({others.length} prefixes)</span></h3>
        {others.length ? (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Prefix</th><th className="num">Occurrences</th><th>First example</th></tr></thead>
              <tbody>
                {others.map(([prefix, n]) => (
                  <tr key={prefix}>
                    <td className="mono">{prefix}</td>
                    <td className="num">{n}</td>
                    <td className="mono">{coverage.other_examples?.[prefix] ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <None>No other id-like tokens.</None>}
      </section>
      </div>
      </details>
    </div>
  );
}
