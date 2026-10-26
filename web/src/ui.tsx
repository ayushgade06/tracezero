import type { ReactNode } from "react";
import type { Citation, Locator } from "./types";

export type Tone = "bad" | "warn" | "ok" | "neutral";

// Engine values -> tone. Unknown values fall back to neutral and keep their verbatim label.
const TONES: Record<string, Tone> = {
  FINDINGS: "bad", BLOCKED: "warn", NO_ACCOUNT_PROOF: "warn", NO_COMPLIANT_EVIDENCE: "warn",
  COMPLIANCE_PROVEN: "ok", NON_DELETION_REQUEST: "neutral",
  COMPLETE: "ok", NOT_APPLICABLE: "neutral",
  VIOLATION: "bad", INCONCLUSIVE: "warn", COMPLIANT: "ok", NOT_ATTRIBUTABLE: "neutral",
  error: "bad", warning: "warn", info: "neutral",
  completed: "ok", failed: "bad", queued: "neutral", running: "warn",
};
export const tone = (value: string): Tone => TONES[value] ?? "neutral";

/** Plain-language meaning of each trace basis, as defined in code/DESIGN.md (M7) and ARCHITECTURE.md. */
export const BASIS: Record<string, string> = {
  FINDINGS: "Residual data found: at least one cited finding",
  COMPLIANCE_PROVEN: "Deletion proven: evidence shows a compliant state for the account",
  BLOCKED: "Undecided: an evaluation blocks a COMPLETE status",
  NO_ACCOUNT_PROOF: "Undecided: no evidence that the account itself is gone",
  NO_COMPLIANT_EVIDENCE: "Undecided: no evidence of any compliant state",
  NON_DELETION_REQUEST: "Not a deletion request: the text does not ask for deletion",
};

/** INCOMPLETE takes its tone from the basis: residual data (bad) differs from "can't prove it" (warn). */
export function caseTone(status: string, basis: string): Tone {
  return status === "INCOMPLETE" ? (basis === "FINDINGS" ? "bad" : "warn") : tone(status);
}

export function Status({ value, t, children }: { value: string; t?: Tone; children?: ReactNode }) {
  return (
    <span className={`status tone-${t ?? tone(value)}`}>
      <span className="mark" aria-hidden="true" />
      {children ?? value}
    </span>
  );
}

export const NOT_PROVIDED = "Not provided by the audit engine.";

export function None({ children = "None recorded." }: { children?: ReactNode }) {
  return <p className="none">{children}</p>;
}

function show(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (Array.isArray(v)) return v.map(show).join(",");
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** key=value pairs exactly as the engine recorded them (locators, facts). */
export function KV({ value }: { value: Locator | Record<string, unknown> | string | null | undefined }) {
  if (value === null || value === undefined) return <span className="faint">—</span>;
  if (typeof value === "string") return <span className="kv">{value}</span>;
  const entries = Object.entries(value);
  if (!entries.length) return <span className="faint">—</span>;
  return (
    <span className="kv">
      {entries.map(([k, v]) => (
        <span key={k}>
          <span className="k">{k}=</span>
          {show(v)}
        </span>
      ))}
    </span>
  );
}

/** An evidence id with the manifest path it names, when the result's file list has it. */
export function Evidence({ id, files }: { id: string; files: Map<string, string> }) {
  if (!id) return <span className="faint">uncited</span>;
  const path = files.get(id);
  return (
    <span>
      <span className="mono">{id}</span>
      {path && <span className="path">{path}</span>}
    </span>
  );
}

export function CitationView({ c, files }: { c: Citation | null; files: Map<string, string> }) {
  if (!c) return <None>No citation in the result for this evaluation.</None>;
  return (
    <dl className="facts">
      <dt>Evidence</dt>
      <dd><Evidence id={c.evidence_id} files={files} /></dd>
      <dt>Reference</dt>
      <dd className="mono">{c.ref}</dd>
      <dt>Locator</dt>
      <dd><KV value={c.locator} /></dd>
      <dt>Reading time</dt>
      <dd className="mono">{c.time ?? "—"}</dd>
      {c.tie && (
        <>
          <dt>Tie</dt>
          <dd>Several readings share the latest time; the engine picked this one by capture time, then evidence id.</dd>
        </>
      )}
    </dl>
  );
}
