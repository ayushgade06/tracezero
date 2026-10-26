import { useEffect } from "react";
import type { CaseResult, Evaluation, Exclusion, Reading, Step } from "./types";
import { BASIS, CitationView, Evidence, KV, None, NOT_PROVIDED, Status, caseTone } from "./ui";

type Files = Map<string, string>;
const evId = (resource: string) => `ev-${resource}`;

/** Open an evaluation's panel and bring it into view (from a step or blocker that names its resource). */
function reveal(resource: string) {
  const el = document.getElementById(evId(resource)) as HTMLDetailsElement | null;
  if (!el) return;
  el.open = true;
  el.scrollIntoView?.({ block: "start", behavior: "smooth" });
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
  el.querySelector("summary")?.focus({ preventScroll: true });
}

function ResourceLink({ id, known }: { id: string; known: Set<string> }) {
  if (!known.has(id)) return <span className="mono">{id}</span>;
  return (
    <button type="button" className="link mono" onClick={() => reveal(id)} title="Show this resource's policy evaluation">
      {id}
    </button>
  );
}

const PHASES = ["FINDING", "REMEDIATION", "VERIFICATION"];
const PHASE_LABEL: Record<string, string> = {
  FINDING: "Findings: cited residual data",
  REMEDIATION: "Remediations: one per finding",
  VERIFICATION: "Verifications: checks that must pass afterwards",
};

function Steps({ steps, files, known }: { steps: Step[]; files: Files; known: Set<string> }) {
  if (!steps.length) return <None>The output row has no steps (steps_json is []).</None>;
  const phases = [...PHASES, ...new Set(steps.map((s) => s.phase).filter((p) => !PHASES.includes(p)))];
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th className="num">#</th>
            <th>Operation</th>
            <th>Resource</th>
            <th>System</th>
            <th>State</th>
            <th>Evidence</th>
          </tr>
        </thead>
        <tbody>
          {phases.flatMap((phase) => {
            const rows = steps.filter((s) => s.phase === phase);
            if (!rows.length) return [];
            return [
              <tr key={phase} className="group">
                <td colSpan={6}>{PHASE_LABEL[phase] ?? phase}</td>
              </tr>,
              ...rows.map((s) => (
                <tr key={s.step_no}>
                  <td className="num mono">{s.step_no}</td>
                  <td className="mono">{s.operation}</td>
                  <td><ResourceLink id={s.resource_id} known={known} /></td>
                  <td className="mono">{s.system_id}</td>
                  <td className="mono">{s.state}</td>
                  <td><Evidence id={s.evidence_id} files={files} /></td>
                </tr>
              )),
            ];
          })}
        </tbody>
      </table>
    </div>
  );
}

function Readings({ title, rows, files, empty }: { title: string; rows: Reading[]; files: Files; empty: string }) {
  return (
    <div>
      <h4>{title} <span className="count">({rows.length})</span></h4>
      {rows.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Reference · file</th><th>Time</th><th>Locator</th><th>Facts read</th></tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={r.ref + i}>
                  <td>
                    <span className="mono">{r.ref}</span>
                    <span className="path">{files.get(r.evidence_id) ?? r.evidence_id}</span>
                    {r.kind && <span className="path">{[r.kind, r.copy].filter(Boolean).join(" · ")}</span>}
                  </td>
                  <td className="mono time">{r.time ?? "—"}</td>
                  <td><KV value={r.locator} /></td>
                  <td><KV value={r.facts} />{r.fields?.length ? <span className="path">fields: {r.fields.join(", ")}</span> : null}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <None>{empty}</None>}
    </div>
  );
}

function Inadmissible({ rows, files }: { rows: Exclusion[]; files: Files }) {
  return (
    <div>
      <h4>Inadmissible evidence <span className="count">({rows.length})</span></h4>
      {rows.length ? (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Reference</th><th>Evidence</th><th>Why it was excluded</th></tr></thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={r.ref + i}>
                  <td className="mono">{r.ref}</td>
                  <td><Evidence id={r.evidence_id} files={files} /></td>
                  <td className="mono">{r.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <None>No evidence about this resource was excluded.</None>}
    </div>
  );
}

function Tokens({ values }: { values: string[] }) {
  return values.length ? <span className="tokens">{values.map((v) => <span key={v}>{v}</span>)}</span> : <span className="faint">none</span>;
}

function EvaluationPanel({ e, files }: { e: Evaluation; files: Files }) {
  const f = e.finding;
  const a = e.attribution ?? { attribution: "" };
  return (
    <details className="ev" id={evId(e.resource_id)} open={e.outcome !== "COMPLIANT"}>
      <summary>
        <span className="ev-title">
          <span className="rid">{e.resource_id}</span>
          <span className="sub mono">{e.system ?? "system not recorded"}</span>
          <span className="sub mono">{e.category}</span>
          <span className="sub mono">{e.rule_id} → {e.action}</span>
        </span>
        <span><Status value={e.outcome} /></span>
      </summary>
      <div className="ev-body">
        <dl className="facts">
          <dt>Reason</dt><dd className="mono">{e.reason}</dd>
          {e.underlying_outcome && e.underlying_outcome !== e.outcome && (
            <><dt>Before ownership gate</dt><dd><Status value={e.underlying_outcome} /></dd></>
          )}
          <dt>Category basis</dt><dd className="mono">{e.category_basis}</dd>
          <dt>Notes</dt><dd><Tokens values={e.notes ?? []} /></dd>
        </dl>

        {f && (
          <div>
            <h4>Finding <span className="count mono">{f.finding_id}</span></h4>
            <p className="explanation">{f.explanation}</p>
            <dl className="facts" style={{ marginTop: 8 }}>
              <dt>Reported as</dt><dd className="mono">{f.finding_type} · state {f.state}</dd>
              <dt>Violating aspect</dt><dd className="mono">{f.aspect}={f.aspect_value} ({f.basis})</dd>
              <dt>Owner conclusion</dt><dd className="mono">{f.conclusion}</dd>
            </dl>
          </div>
        )}
        {e.potential_violation && (
          <div className="notice warn">
            <h4>Withheld finding</h4>
            <p>
              The evidence would support <span className="mono">{e.potential_violation.would_be}</span> ({" "}
              <span className="mono">{e.potential_violation.reason}</span> ), but the ownership gate kept it from becoming a finding.
            </p>
            {e.potential_violation.conflicting_owners?.length ? <p>Conflicting owners: <Tokens values={e.potential_violation.conflicting_owners} /></p> : null}
          </div>
        )}

        <div>
          <h4>Citation</h4>
          <CitationView c={e.citation} files={files} />
        </div>

        <Readings title="Establishing readings" rows={(f ?? e).establishing ?? []} files={files}
          empty="No admissible reading establishes a state for this resource." />
        {f ? (
          <>
            <Readings title="Contradicting readings" rows={f.contradicting ?? []} files={files} empty="No admissible reading contradicts the finding." />
            <Inadmissible rows={f.inadmissible ?? []} files={files} />
          </>
        ) : (
          <p className="none">Contradicting and inadmissible evidence are recorded only on findings; this evaluation has none.</p>
        )}

        <div>
          <h4>Attribution to the requester</h4>
          <dl className="facts">
            <dt>Association</dt><dd className="mono">{a.attribution || NOT_PROVIDED}{a.source ? ` (via ${a.source})` : ""}</dd>
            <dt>Link evidence</dt><dd><Tokens values={a.link_evidence ?? []} /></dd>
            {a.m3 && <><dt>Matched event</dt><dd className="mono">{a.m3.activity_id} · {a.m3.confidence} · {a.m3.reason}</dd></>}
            {f?.identifying?.length ? <><dt>Identifying</dt><dd><Tokens values={f.identifying.map((i) => `${i.evidence_id} ${i.role}`)} /></dd></> : null}
            <dt>Competing owners</dt><dd><Tokens values={a.competing_owners ?? []} /></dd>
            <dt>Conflicting owners</dt><dd><Tokens values={a.conflicting_owners ?? []} /></dd>
          </dl>
        </div>
      </div>
    </details>
  );
}

export default function CaseView({ c, files }: { c: CaseResult; files: Files }) {
  const known = new Set(c.evaluations.map((e) => e.resource_id));
  const t = c.trace ?? { basis: "", blockers: [] };
  useEffect(() => { window.scrollTo?.({ top: 0 }); }, [c.case_id]);
  return (
    <article className="case" aria-labelledby="case-title">
      <header className="case-head">
        <h2 id="case-title">
          {c.case_id}
          <Status value={c.deletion_status} t={caseTone(c.deletion_status, t.basis)} />
        </h2>
        <p>{BASIS[t.basis] ?? <span className="mono">{t.basis || NOT_PROVIDED}</span>}</p>
        <dl className="facts">
          <dt>Trace basis</dt><dd className="mono">{t.basis || "—"}</dd>
          <dt>Intent</dt>
          <dd className="mono">
            {t.intent ?? "—"}
            <span className="faint"> · rule reader {t.intent_rule ?? "—"} · Claude {t.intent_llm ?? "no cached reading"}</span>
          </dd>
          {t.system_from_m2_link?.length ? <><dt>System from link</dt><dd><Tokens values={t.system_from_m2_link} /></dd></> : null}
          <dt>Request fields</dt>
          <dd className="faint">The result does not carry the requester, dates or request text; see this row in deletion_requests.csv.</dd>
        </dl>
      </header>

      <section aria-labelledby="h-blockers">
        <h3 id="h-blockers">Blockers <span className="count">({t.blockers?.length ?? 0})</span></h3>
        {t.blockers?.length ? (
          <ul className="stack" style={{ gap: 4, margin: 0, paddingLeft: "1.2em" }}>
            {t.blockers.map((b) => {
              const i = b.indexOf(":");
              return (
                <li key={b} className="tone-warn">
                  <span style={{ color: "var(--ink)" }}>
                    <ResourceLink id={b.slice(0, i)} known={known} /> <span className="mono">{b.slice(i + 1)}</span>
                  </span>
                </li>
              );
            })}
          </ul>
        ) : <None>Nothing blocks this row's status.</None>}
      </section>

      <section aria-labelledby="h-steps">
        <h3 id="h-steps">Output steps <span className="count">({c.steps.length}, as written to steps_json)</span></h3>
        <Steps steps={c.steps} files={files} known={known} />
      </section>

      <section aria-labelledby="h-evals">
        <h3 id="h-evals">Policy evaluations <span className="count">({c.evaluations.length}, one per resource)</span></h3>
        {c.deletion_status === "NOT_APPLICABLE" && c.evaluations.length > 0 && (
          <p className="none" style={{ marginBottom: 8 }}>The pipeline evaluated these resources, but a NOT_APPLICABLE row does not use them.</p>
        )}
        {c.evaluations.length ? (
          <div>{c.evaluations.map((e) => <EvaluationPanel key={e.resource_id} e={e} files={files} />)}</div>
        ) : <None>No resource was linked to this request.</None>}
      </section>

      <section aria-labelledby="h-rem">
        <h3 id="h-rem">Remediations <span className="count">({c.remediations.length})</span></h3>
        {c.remediations.length ? (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Operation</th><th>Resource</th><th>Permitted because</th><th>Evidence</th><th>Caveats</th></tr></thead>
              <tbody>
                {c.remediations.map((r) => (
                  <tr key={r.remediation_id}>
                    <td className="mono">{r.operation} → {r.target_state}</td>
                    <td><ResourceLink id={r.resource_id} known={known} /><span className="path">{r.system ?? ""}</span></td>
                    <td>{r.permitted_because}</td>
                    <td><Tokens values={r.establishing_evidence ?? []} /></td>
                    <td><Tokens values={r.caveats ?? []} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <None>No remediation: the engine plans one only for each finding.</None>}
      </section>
    </article>
  );
}
