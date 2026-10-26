import type { AuditResult, ParserDiagnostic } from "./types";
import { KV, None, Status } from "./ui";

const SEVERITIES = ["error", "warning", "info"];
const SEVERITY_LABEL: Record<string, string> = { error: "Errors", warning: "Warnings", info: "Information" };

function ParserGroup({ severity, rows }: { severity: string; rows: ParserDiagnostic[] }) {
  return (
    <section aria-labelledby={`h-sev-${severity}`}>
      <h3 id={`h-sev-${severity}`}>
        <Status value={severity}>{SEVERITY_LABEL[severity] ?? severity}</Status>
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
  return (
    <div className="stack" style={{ gap: 32 }}>
      <p className="muted" style={{ maxWidth: "80ch" }}>
        These are reports about reading the evidence files, not audit findings. A parser error means a file (or part of it)
        contributed no observations; it does not mean the data was deleted. Findings are on the request pages.
      </p>

      <section aria-labelledby="h-files">
        <h3 id="h-files">Evidence files <span className="count">({files.length}, from evidence_files.csv)</span></h3>
        {files.length ? (
          <div className="table-wrap">
            <table>
              <thead><tr><th>File</th><th>Path</th><th>Format</th><th className="num">Observations</th><th className="num">Diagnostics</th></tr></thead>
              <tbody>
                {files.map((f) => (
                  <tr key={f.file_id}>
                    <td className="mono">{f.file_id}</td>
                    <td className="mono" style={{ overflowWrap: "anywhere" }}>{f.file_path}</td>
                    <td className="mono">{f.format}</td>
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
        <h2 style={{ fontSize: "var(--fs-section)" }}>Parser diagnostics <span className="faint" style={{ fontWeight: 400 }}>({parser.length})</span></h2>
        {parser.length ? severities.map((s) => {
          const rows = parser.filter((d) => d.severity === s);
          return rows.length ? <ParserGroup key={s} severity={s} rows={rows} /> : null;
        }) : <None>The parsers reported nothing: no unparsed lines, missing files, unsupported formats or parser errors.</None>}
      </div>

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
  );
}
