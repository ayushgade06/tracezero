// Plain-language wording for engine values. Every sentence restates the documented meaning of a code
// (code/audit/final.py, policy.py, state.py); nothing here adds a fact the result doesn't contain.
// An unknown code is never guessed at: it falls back to the code itself.
import type { CaseResult, Citation, Locator, Step } from "./types";
import type { Tone } from "./ui";

/** The one-word answer for a request, and its colour. */
export function caseLabel(c: CaseResult): { label: string; tone: Tone } {
  if (c.deletion_status === "COMPLETE") return { label: "Deleted", tone: "ok" };
  if (c.deletion_status === "NOT_APPLICABLE") return { label: "Not a deletion request", tone: "neutral" };
  if (c.deletion_status === "INCOMPLETE") {
    return c.trace?.basis === "FINDINGS" ? { label: "Data still found", tone: "bad" } : { label: "Can't confirm", tone: "warn" };
  }
  return { label: c.deletion_status, tone: "neutral" };
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** The headline and the sentence under it, from the trace basis (code/DESIGN.md, M7). */
export function verdict(c: CaseResult): { title: string; detail: string } {
  const found = c.steps.filter((s) => s.phase === "FINDING").length;
  switch (c.trace?.basis) {
    case "FINDINGS":
      return { title: "Not deleted", detail: `This person's data is still in ${plural(found, "place")}.` };
    case "COMPLIANCE_PROVEN":
      return { title: "Deleted", detail: "The evidence shows this person's account is gone, and nothing was left behind." };
    case "BLOCKED":
      return { title: "Can't confirm", detail: "Something in the evidence stops TraceZero from saying the data is gone." };
    case "NO_ACCOUNT_PROOF":
      return { title: "Can't confirm", detail: "Nothing in the evidence shows that the account itself was deleted." };
    case "NO_COMPLIANT_EVIDENCE":
      return { title: "Can't confirm", detail: "No evidence shows that any of this person's data was deleted." };
    case "NON_DELETION_REQUEST":
      return { title: "Nothing to check", detail: "This request didn't ask for data to be deleted." };
    default:
      return { title: caseLabel(c).label, detail: "" };
  }
}

/** A short line for the request list. */
export function caseSummary(c: CaseResult): string {
  const found = c.steps.filter((s) => s.phase === "FINDING").length;
  if (found) return plural(found, "problem");
  if (c.deletion_status === "COMPLETE") return "Proof found";
  if (c.deletion_status === "NOT_APPLICABLE") return "Nothing to check";
  const open = c.trace?.blockers?.length ?? 0;
  return open ? plural(open, "open question") : "Not enough proof";
}

// Output-step operations (final.py OPERATIONS). Verifications read as a to-do on an unfinished request and as a
// confirmation on a COMPLETE one, where they cite the evidence that already shows them.
const PROBLEM: Record<string, string> = {
  REPORT_REMAINING_RECORD: "The record still exists",
  REPORT_EXPOSED_PII: "Personal details can still be seen",
  REPORT_ACTIVE_DATA_FLOW: "Data is still being sent to another system",
  REPORT_RESTORE_RISK: "A backup could bring the data back",
};
const FIX: Record<string, string> = {
  DELETE_RECORD: "Delete the record",
  REDACT_RECORD: "Hide the personal details",
  ANONYMIZE_RECORD: "Remove what identifies the person",
  DISABLE_DATA_FLOW: "Stop sending the data to other systems",
  BLOCK_RESTORE: "Stop the backup from restoring it",
};
const CHECK: Record<string, [todo: string, done: string]> = {
  VERIFY_RECORD_ABSENT: ["Check the record is gone", "The record is gone"],
  VERIFY_RECORD_PRESENT: ["Check the record is still kept, as the rules require", "Kept on purpose, as the rules require"],
  VERIFY_RECORD_REDACTED: ["Check the personal details are hidden", "The personal details are hidden"],
  VERIFY_RECORD_ANONYMIZED: ["Check the record no longer identifies the person", "The record no longer identifies the person"],
  VERIFY_DATA_FLOW_DISABLED: ["Check the data is no longer sent on", "The data is no longer sent on"],
  VERIFY_RESTORE_BLOCKED: ["Check the backup can't restore it", "The backup can't restore it"],
};

export function stepText(s: Step, done = false): string {
  if (s.phase === "FINDING") return PROBLEM[s.operation] ?? s.operation;
  if (s.phase === "REMEDIATION") return FIX[s.operation] ?? s.operation;
  const check = CHECK[s.operation];
  return check ? check[done ? 1 : 0] : s.operation;
}

// Policy actions (deletion_rules.csv required_action).
export const actionText = (action: string) =>
  ({
    DELETE: "delete it",
    REDACT: "hide the personal details",
    ANONYMIZE: "remove what identifies the person",
    RETAIN: "it may be kept, under the retention rules",
    FLOW: "stop sending it to other systems",
  })[action] ?? (words(action).toLowerCase() || action);

export const outcomeLabel = (o: string) =>
  ({ VIOLATION: "Problem", COMPLIANT: "OK", INCONCLUSIVE: "Unclear", NOT_ATTRIBUTABLE: "Not linked to this person" })[o] ?? o;

// Evaluation reasons (policy.py) and the sub-reasons of STATE_UNKNOWN (state.py).
const REASON: Record<string, string> = {
  RECORD_REMAINS: "The record still exists.",
  IDENTIFIERS_EXPOSED: "Personal details can still be seen.",
  ACTIVE_DATA_FLOW: "Data is still being sent to another system.",
  RESTORE_PATH_OPEN: "A backup could still restore the data.",
  RECORD_ABSENT: "The record is gone.",
  RECORD_ABSENT_NO_EXPOSURE: "The record is gone.",
  FLOW_DISABLED: "Data is no longer sent to other systems.",
  RESTORE_BLOCKED: "The backup can't restore the data.",
  TOMBSTONE_MISSING: "There is no proof the account was closed.",
  RETAINED_WITHIN_PERIOD: "It's kept on purpose, and its keep-until date hasn't passed.",
  RETAINED_NO_PERIOD_SUPPLIED: "It's a record that must be kept, and no end date was set, so keeping it is correct.",
  RETAINED_RECORD_MISSING: "It should have been kept, but the evidence doesn't show it.",
  RECORD_REDACTED: "The personal details are hidden.",
  RECORD_ANONYMIZED: "The record no longer identifies the person.",
  RETENTION_UNVERIFIABLE: "It's kept on purpose, but the evidence can't confirm until when.",
  RETENTION_CONFLICTING: "The evidence disagrees on how long it must be kept.",
  RETENTION_EXPIRED_NO_RULE: "Its keep-until date has passed, and no rule says what happens next.",
  OWNERSHIP_CONFLICT: "The evidence links it to more than one person, so it isn't counted.",
  STATE_CONFLICTING: "The evidence disagrees about its current state.",
  STATE_STALE: "The only evidence about it is out of date.",
  CATEGORY_UNKNOWN: "TraceZero can't tell what kind of data this is.",
  CATEGORY_CONFLICTING: "The evidence disagrees about what kind of data this is.",
};
const UNKNOWN_BECAUSE: Record<string, string> = {
  HISTORICAL_READINGS_ONLY: "only older records mention it",
  HISTORICAL_COPY_UNRESOLVED: "only an old copy mentions it",
  POST_AUDIT_EVIDENCE_ONLY: "the only evidence is from after the audit date",
  NO_ADMISSIBLE_STATE_OBSERVATION: "none of the evidence can be used",
  ACTIVITY_ONLY: "only a workflow log mentions it",
  ONLY_INCONSISTENT_TIMESTAMPS: "its timestamps don't add up",
  NO_EVIDENCE: "there is no evidence about it",
};

const EXPOSURE_WORD: Record<string, string> = { IDENTIFIABLE: "visible", REDACTED: "hidden", ANONYMIZED: "anonymised" };

export function reasonText(reason: string): string {
  if (REASON[reason]) return REASON[reason];
  if (reason.startsWith("STATE_UNKNOWN:")) {
    const why = UNKNOWN_BECAUSE[reason.slice("STATE_UNKNOWN:".length)];
    return why ? `Its current state is unknown: ${why}.` : "Its current state is unknown.";
  }
  // Exposure reasons are built at run time (policy.py): EXPOSURE_<state issue>, EXPOSURE_<value>_NOT_<wanted>.
  if (reason.startsWith("EXPOSURE_UNKNOWN:")) {
    const why = UNKNOWN_BECAUSE[reason.slice("EXPOSURE_UNKNOWN:".length)];
    return why ? `It's unknown whether personal details can be seen: ${why}.` : "It's unknown whether personal details can be seen.";
  }
  if (reason === "EXPOSURE_CONFLICTING") return "The evidence disagrees about whether personal details can be seen.";
  if (reason === "EXPOSURE_STALE") return "The only evidence about its personal details is out of date.";
  const not = /^EXPOSURE_([A-Z]+)_NOT_([A-Z]+)$/.exec(reason);
  if (not && EXPOSURE_WORD[not[1]] && EXPOSURE_WORD[not[2]]) {
    return `Its personal details are ${EXPOSURE_WORD[not[1]]}, but they should be ${EXPOSURE_WORD[not[2]]}.`;
  }
  if (reason.startsWith("OWNERSHIP_")) return "The evidence doesn't show it belongs to this person.";
  return reason;
}

/** A blocker is "resource:REASON" (final.py blockers). */
export function blockerParts(b: string): { resource: string; text: string } {
  const i = b.indexOf(":");
  return i < 0 ? { resource: "", text: reasonText(b) } : { resource: b.slice(0, i), text: reasonText(b.slice(i + 1)) };
}

export const exclusionText = (reason: string) =>
  ({
    FILE_CAPTURED_AFTER_AUDIT: "The file was collected after the audit date.",
    RECORD_AFTER_AUDIT: "It is dated after the audit date.",
    LOOKUP_ATTEMPT_NO_RESULT: "A lookup that never showed a result.",
  })[reason] ?? reason;

// Facts a reading established (state.py aspects).
const FACT: Record<string, Record<string, string>> = {
  existence: { PRESENT: "the record exists", ABSENT: "the record is gone" },
  exposure: { IDENTIFIABLE: "personal details are visible", REDACTED: "personal details are hidden", ANONYMIZED: "the record is anonymised" },
  flow: { ACTIVE: "data is being sent on", DISABLED: "data is not sent on" },
  restore: { ALLOWED: "a backup can restore it", BLOCKED: "the backup can't restore it" },
};

export function factsText(facts: Record<string, unknown> | undefined): string {
  return Object.entries(facts ?? {})
    .map(([k, v]) => FACT[k]?.[String(v)] ?? `${k.replace(/_/g, " ")}: ${String(v).toLowerCase()}`)
    .join(", ");
}

/** "table customer_docs, row 1, line 12" from the parts of a locator a person can follow. */
export function locatorText(locator: Locator | null | undefined): string {
  if (!locator) return "";
  const parts: string[] = [];
  for (const key of ["table", "row", "line", "page", "path"]) {
    if (locator[key] !== undefined && locator[key] !== null) parts.push(`${key === "path" ? "at" : key} ${String(locator[key])}`);
  }
  return parts.join(", ");
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "6 Sep 2026" for an ISO timestamp (UTC, as the engine records it); anything else unchanged. */
export function dateText(iso: string | null | undefined): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso ?? "");
  return m ? `${Number(m[3])} ${MONTHS[Number(m[2]) - 1]} ${m[1]}` : iso ?? "";
}

export const fileName = (path: string | undefined) => (path ?? "").split("/").pop() ?? "";

/** "search_snapshot_20260906.sql, table docs, row 1" for a citation. */
export function sourceText(c: Citation | null, files: Map<string, string>): string {
  if (!c) return "";
  return [fileName(files.get(c.evidence_id)) || c.evidence_id, locatorText(c.locator)].filter(Boolean).join(", ");
}

/** "search_service" -> "search service" (identifiers stay readable; nothing is renamed). */
export const words = (id: string | null | undefined) => (id ?? "").replace(/_/g, " ");
