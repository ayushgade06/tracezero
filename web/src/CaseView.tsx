import { useEffect, type ReactNode } from "react";
import {
  actionText, blockerParts, caseLabel, dateText, exclusionText, factsText, fileName, locatorText, outcomeLabel, reasonText,
  sourceText, stepText, verdict, words,
} from "./plain";
import type { CaseResult, Evaluation, Exclusion, Reading, Step } from "./types";
import { BASIS, CitationView, Evidence, KV, None, NOT_PROVIDED, Status, tone } from "./ui";

type Files = Map<string, string>;
const evId = (resource: string) => `ev-${resource}`;

/** Open a resource's evidence card and bring it into view. */
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
    <button type="button" className="link mono" onClick={() => reveal(id)} title="Show the evidence for this resource">
      {id}
    </button>
  );
}

/** One row of a plain list: a numbered mark, what it is, where, and an optional source line and action. */
function Item({ n, t, what, where, source, action }: { n: number; t: string; what: ReactNode; where?: ReactNode; source?: ReactNode; action?: ReactNode }) {
  return (
    <li>
      <span className={`mark tone-${t}`} aria-hidden="true">{n}</span>
      <div>
        <div className="what">{what}</div>
        {where && <div className="where">{where}</div>}
        {source && <div className="source">{source}</div>}
      </div>
      {action && <div className="go">{action}</div>}
    </li>
  );
}

const In = ({ s }: { s: Step }) => <>in <span className="mono">{s.system_id}</span> · <span className="mono">{s.resource_id}</span></>;

function seeProof(resource: string, known: Set<string>) {
  return known.has(resource) ? <button type="button" className="link" onClick={() => reveal(resource)}>See the evidence</button> : null;
}

/** "Found in search_snapshot.sql, table docs, row 1 · seen 6 Sep 2026" for a step's citation. */
function source(s: Step, e: Evaluation | undefined, files: Files) {
  const c = e?.finding?.citation ?? e?.citation ?? null;
  if (c && c.evidence_id === s.evidence_id) return `Found in ${sourceText(c, files)}${c.time ? ` · seen ${dateText(c.time)}` : ""}`;
  return s.evidence_id ? `Found in ${fileName(files.get(s.evidence_id)) || s.evidence_id}` : undefined;
}

// ---- Technical detail: the engine's own fields, verbatim ----

const PHASES = ["FINDING", "REMEDIATION", "VERIFICATION"];
const PHASE_LABEL: Record<string, string> = {
  FINDING: "Findings: cited residual data",
  REMEDIATION: "Remediations: one per finding",
  VERIFICATION: "Verifications: checks that must pass afterwards",
};

function StepsTable({ steps, files, known }: { steps: Step[]; files: Files; known: Set<string> }) {
  if (!steps.length) return <None>The output row has no steps (steps_json is []).</None>;
  const phases = [...PHASES, ...new Set(steps.map((s) => s.phase).filter((p) => !PHASES.includes(p)))];
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr><th className="num">#</th><th>Operation</th><th>Resource</th><th>System</th><th>State</th><th>Evidence</th></tr>
        </thead>
        <tbody>
          {phases.flatMap((phase) => {
            const rows = steps.filter((s) => s.phase === phase);
            if (!rows.length) return [];
            return [
              <tr key={phase} className="group"><td colSpan={6}>{PHASE_LABEL[phase] ?? phase}</td></tr>,
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
            <thead><tr><th>Reference · file</th><th>Time</th><th>Locator</th><th>Facts read</th></tr></thead>
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

/** Plain list of the evidence behind an evaluation, kept in three separate groups. */
function PlainEvidence({ title, rows, files, empty }: { title: string; rows: Reading[]; files: Files; empty: string }) {
  return (
    <div>
      <h4>{title} <span className="count">({rows.length})</span></h4>
      {rows.length ? (
        <ul className="stack small" style={{ gap: 6, margin: 0, paddingLeft: "1.1em" }}>
          {rows.map((r, i) => (
            <li key={r.ref + i}>
              <span>{fileName(files.get(r.evidence_id)) || r.evidence_id}</span>
              {locatorText(r.locator) && <span className="faint">, {locatorText(r.locator)}</span>}
              {r.time && <span className="faint"> · {dateText(r.time)}</span>}
              {factsText(r.facts) && <span className="muted">: {factsText(r.facts)}</span>}
            </li>
          ))}
        </ul>
      ) : <p className="none">{empty}</p>}
    </div>
  );
}

function EvaluationCard({ e, files }: { e: Evaluation; files: Files }) {
  const f = e.finding;
  const a = e.attribution ?? { attribution: "" };
  const c = f?.citation ?? e.citation;
  return (
    <details className="ev" id={evId(e.resource_id)}>
      <summary>
        <span className="ev-title">
          <span><span className="rid">{e.resource_id}</span> <span className="faint small">{e.system ? `in ${e.system}` : "system not recorded"}</span></span>
          <span className="plain">{reasonText(e.reason)}</span>
        </span>
        <Status value={e.outcome} t={tone(e.outcome)}>{outcomeLabel(e.outcome)}</Status>
      </summary>
      <div className="ev-body">
        <dl className="facts">
          <dt>What it is</dt><dd>{words(e.category).toLowerCase() || NOT_PROVIDED}</dd>
          <dt>What the rules require</dt><dd>{e.action ? actionText(e.action) : NOT_PROVIDED}</dd>
          <dt>Main evidence</dt>
          <dd>{c ? <>{sourceText(c, files)}{c.time && <span className="faint"> · seen {dateText(c.time)}</span>}</> : "No single piece of evidence decides this."}</dd>
        </dl>
        {e.potential_violation && (
          <div className="notice warn">
            <p>A problem was found here but not counted against this request: the evidence also links this record to someone else.</p>
          </div>
        )}
        <PlainEvidence title="Evidence that shows it" rows={(f ?? e).establishing ?? []} files={files}
          empty="No usable evidence shows its state." />
        {f ? (
          <>
            <PlainEvidence title="Evidence that says otherwise" rows={f.contradicting ?? []} files={files} empty="Nothing contradicts it." />
            <div>
              <h4>Evidence not used <span className="count">({f.inadmissible?.length ?? 0})</span></h4>
              {f.inadmissible?.length ? (
                <ul className="stack small" style={{ gap: 6, margin: 0, paddingLeft: "1.1em" }}>
                  {f.inadmissible.map((r, i) => (
                    <li key={r.ref + i}>{fileName(files.get(r.evidence_id)) || r.evidence_id} <span className="faint">({r.ref})</span>: <span className="muted">{exclusionText(r.reason)}</span></li>
                  ))}
                </ul>
              ) : <p className="none">All the evidence about it could be used.</p>}
            </div>
          </>
        ) : null}

        <details className="tech">
          <summary>Technical details</summary>
          <div className="tech-block">
            <dl className="facts">
              <dt>Outcome</dt><dd className="mono">{e.outcome}</dd>
              <dt>Reason</dt><dd className="mono">{e.reason}</dd>
              {e.underlying_outcome && e.underlying_outcome !== e.outcome && (
                <><dt>Before ownership gate</dt><dd className="mono">{e.underlying_outcome}</dd></>
              )}
              <dt>Category</dt><dd className="mono">{e.category}</dd>
              <dt>Rule</dt><dd className="mono">{e.rule_id} → {e.action}</dd>
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
              <div>
                <h4>Withheld finding</h4>
                <p className="small">
                  The evidence would support <span className="mono">{e.potential_violation.would_be}</span> (
                  <span className="mono">{e.potential_violation.reason}</span>), but the ownership gate kept it from becoming a finding.
                </p>
                {e.potential_violation.conflicting_owners?.length ? <p className="small">Conflicting owners: <Tokens values={e.potential_violation.conflicting_owners} /></p> : null}
              </div>
            )}
            <div><h4>Citation</h4><CitationView c={e.citation} files={files} /></div>
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
      </div>
    </details>
  );
}

export default function CaseView({ c, files }: { c: CaseResult; files: Files }) {
  const known = new Set(c.evaluations.map((e) => e.resource_id));
  const evalOf = new Map(c.evaluations.map((e) => [e.resource_id, e]));
  const t = c.trace ?? { basis: "", blockers: [] };
  const { label, tone: caseTone } = caseLabel(c);
  const v = verdict(c);
  const by = (phase: string) => c.steps.filter((s) => s.phase === phase);
  const [found, fixes, checks] = [by("FINDING"), by("REMEDIATION"), by("VERIFICATION")];
  const complete = c.deletion_status === "COMPLETE";
  useEffect(() => { window.scrollTo?.({ top: 0 }); }, [c.case_id]);

  return (
    <article className="case" aria-labelledby="case-title">
      <header className={`card verdict tone-${caseTone}`}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span className="id">Request {c.case_id}</span>
          <Status value={c.deletion_status} t={caseTone}>{label}</Status>
        </div>
        <h2 id="case-title"><span className="visually-hidden">{c.case_id}: </span>{v.title}</h2>
        {v.detail && <p>{v.detail}</p>}
      </header>

      {found.length > 0 && (
        <section className="section" aria-labelledby="h-found">
          <h3 id="h-found">Where the data was found <span className="count">({found.length})</span></h3>
          <ol className="items">
            {found.map((s, i) => (
              <Item key={s.step_no} n={i + 1} t="bad" what={stepText(s)} where={<In s={s} />}
                source={source(s, evalOf.get(s.resource_id), files)} action={seeProof(s.resource_id, known)} />
            ))}
          </ol>
        </section>
      )}

      {t.blockers?.length > 0 && (
        <section className="section" aria-labelledby="h-blockers">
          <h3 id="h-blockers">Why TraceZero can't confirm it <span className="count">({t.blockers.length})</span></h3>
          <ol className="items">
            {t.blockers.map((b, i) => {
              const { resource, text } = blockerParts(b);
              return (
                <Item key={b} n={i + 1} t="warn" what={text}
                  where={<>about <span className="mono">{resource}</span>{evalOf.get(resource)?.system ? <> in <span className="mono">{evalOf.get(resource)!.system}</span></> : null}</>}
                  action={seeProof(resource, known)} />
              );
            })}
          </ol>
        </section>
      )}

      {complete && checks.length > 0 && (
        <section className="section" aria-labelledby="h-proof">
          <h3 id="h-proof">Proof it's gone <span className="count">({checks.length})</span></h3>
          <ol className="items">
            {checks.map((s, i) => (
              <Item key={s.step_no} n={i + 1} t="ok" what={stepText(s, true)} where={<In s={s} />}
                source={source(s, evalOf.get(s.resource_id), files)} action={seeProof(s.resource_id, known)} />
            ))}
          </ol>
        </section>
      )}

      {!complete && (fixes.length > 0 || checks.length > 0) && (
        <section className="section" aria-labelledby="h-next">
          <h3 id="h-next">What to do next</h3>
          <p className="intro">{fixes.length ? "Fix each problem, then run the checks to make sure it worked." : "These checks would settle whether the data is gone."}</p>
          {fixes.length > 0 && (
            <>
              <h4 className="group-label">Fix these</h4>
              <ol className="items compact">
                {fixes.map((s, i) => <Item key={s.step_no} n={i + 1} t="neutral" what={stepText(s)} where={<In s={s} />} />)}
              </ol>
            </>
          )}
          {checks.length > 0 && (
            <>
              {fixes.length > 0 && <h4 className="group-label">Then check</h4>}
              <ol className="items compact">
                {checks.map((s, i) => <Item key={s.step_no} n={i + 1} t="ok" what={stepText(s)} where={<In s={s} />} />)}
              </ol>
            </>
          )}
        </section>
      )}

      <section className="section" aria-labelledby="h-evals">
        <h3 id="h-evals">Evidence, place by place <span className="count">({c.evaluations.length})</span></h3>
        {c.evaluations.length > 0 && (
          <p className="intro">
            {c.deletion_status === "NOT_APPLICABLE"
              ? "TraceZero looked at these places, but they don't count: this request isn't about deletion."
              : "Every place this person's data was linked to, and what the evidence says about it. Open one to see the proof."}
          </p>
        )}
        {c.evaluations.length ? (
          <div>{c.evaluations.map((e) => <EvaluationCard key={e.resource_id} e={e} files={files} />)}</div>
        ) : <None>No resource was linked to this request.</None>}
      </section>

      <details className="tech card card-pad">
        <summary>Technical details for this request</summary>
        <div className="tech-block">
          <dl className="facts">
            <dt>Status</dt><dd className="mono">{c.deletion_status}</dd>
            <dt>Trace basis</dt><dd className="mono">{t.basis || "—"} {BASIS[t.basis] ? <span className="faint">({BASIS[t.basis]})</span> : null}</dd>
            <dt>Intent</dt>
            <dd className="mono">{t.intent ?? "—"}<span className="faint"> · rule reader {t.intent_rule ?? "—"} · Claude {t.intent_llm ?? "no cached reading"}</span></dd>
            {t.system_from_m2_link?.length ? <><dt>System from link</dt><dd><Tokens values={t.system_from_m2_link} /></dd></> : null}
            <dt>Blockers</dt>
            <dd>{t.blockers?.length ? t.blockers.map((b) => {
              const i = b.indexOf(":");
              return <div key={b}><ResourceLink id={b.slice(0, i)} known={known} /> <span className="mono">{b.slice(i + 1)}</span></div>;
            }) : <span className="faint">none</span>}</dd>
            <dt>Request fields</dt>
            <dd className="faint">The result does not carry the requester, dates or request text; see this row in deletion_requests.csv.</dd>
          </dl>
          <div>
            <h4>Output steps <span className="count">({c.steps.length}, as written to steps_json)</span></h4>
            <StepsTable steps={c.steps} files={files} known={known} />
          </div>
          <div>
            <h4>Remediations <span className="count">({c.remediations.length})</span></h4>
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
          </div>
        </div>
      </details>
    </article>
  );
}
