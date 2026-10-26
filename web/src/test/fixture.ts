// Synthetic, fictional audit results shaped like AuditResult.to_dict() (see the demo dataset's real output).
import type { AuditResult, CaseResult, Evaluation, Job } from "../types";

const reading = (ref: string, evidence_id: string, locator: Record<string, unknown>, facts: Record<string, unknown>) => ({
  ref, evidence_id, kind: "snapshot", copy: "primary", time: "2026-09-06T00:00:00Z", locator, fields: ["email"], facts,
});

const violation: Evaluation = {
  case_id: "case_a", resource_id: "search_1", system: "search_service", category: "SEARCH_INDEX_ENTRY",
  category_basis: "collection_name", rule_id: "rule_03", action: "DELETE", outcome: "VIOLATION", reason: "RECORD_REMAINS",
  underlying_outcome: "VIOLATION",
  attribution: { attribution: "EVIDENCE_LINKED", source: "M2", link_evidence: ["ev_2"], competing_owners: [], conflicting_owners: [] },
  establishing: [reading("ev_2:docs[1]", "ev_2", { table: "docs", row: 1, line: 12 }, { existence: "PRESENT" })],
  citation: { evidence_id: "ev_2", ref: "ev_2:docs[1]", locator: { table: "docs", row: 1, line: 12 }, time: "2026-09-06T00:00:00Z", tie: true },
  finding: {
    finding_id: "case_a:search_1:rule_03", finding_type: "REPORT_REMAINING_RECORD", state: "PRESENT", aspect: "existence",
    aspect_value: "PRESENT", basis: "OBSERVED", conclusion: "confirmed",
    establishing: [reading("ev_2:docs[1]", "ev_2", { table: "docs", row: 1, line: 12 }, { existence: "PRESENT" })],
    citation: { evidence_id: "ev_2", ref: "ev_2:docs[1]", locator: { table: "docs", row: 1, line: 12 }, time: "2026-09-06T00:00:00Z", tie: true },
    identifying: [{ evidence_id: "ev_2", role: "M2_LINK" }],
    contradicting: [reading("ev_3:L7", "ev_3", { line: 7 }, { existence: "ABSENT" })],
    inadmissible: [{ ref: "ev_4:L2", evidence_id: "ev_4", reason: "FILE_CAPTURED_AFTER_AUDIT" }],
    reason: "RECORD_REMAINS",
    explanation: "SEARCH_INDEX_ENTRY requires DELETE (rule_03); at 2026-09-10T00:00:00Z the evidence shows existence=PRESENT",
    notes: [],
  },
  potential_violation: null,
  notes: [],
};

const withheld: Evaluation = {
  ...violation, resource_id: "crm_1", system: null, category: "CRM_CONTACT", outcome: "NOT_ATTRIBUTABLE",
  reason: "OWNERSHIP_CONFLICT", underlying_outcome: "VIOLATION", finding: null, citation: null, establishing: [],
  attribution: { attribution: "EVIDENCE_LINKED" },
  potential_violation: { would_be: "REPORT_REMAINING_RECORD", reason: "RECORD_REMAINS", citation: null, conflicting_owners: ["user_77"] },
  notes: ["CONFLICTING_OWNERS:user_77"],
};

const caseA: CaseResult = {
  case_id: "case_a", deletion_status: "INCOMPLETE",
  steps: [
    { phase: "FINDING", system_id: "search_service", resource_id: "search_1", operation: "REPORT_REMAINING_RECORD", state: "PRESENT", evidence_id: "ev_2", step_no: 1 },
    { phase: "REMEDIATION", system_id: "search_service", resource_id: "search_1", operation: "DELETE_RECORD", state: "ABSENT", evidence_id: "", step_no: 2 },
    { phase: "VERIFICATION", system_id: "search_service", resource_id: "search_1", operation: "VERIFY_RECORD_ABSENT", state: "ABSENT", evidence_id: "", step_no: 3 },
  ],
  trace: { basis: "FINDINGS", blockers: [], intent: "DELETION", intent_rule: "DELETION", intent_llm: null },
  evaluations: [withheld, violation],
  remediations: [{
    remediation_id: "case_a:search_1:rule_03:DELETE_RECORD", finding_id: "case_a:search_1:rule_03", resource_id: "search_1",
    system: "search_service", operation: "DELETE_RECORD", target_state: "ABSENT", rule_id: "rule_03",
    permitted_because: "SEARCH_INDEX_ENTRY requires DELETE under rule_03", establishing_evidence: ["ev_2"], caveats: ["CITATION_TIE"],
  }],
};

// Optional fields missing: no intent readings, no citation, no system, no evaluations' extras.
const caseB: CaseResult = {
  case_id: "case_b", deletion_status: "INCOMPLETE",
  steps: [{ phase: "VERIFICATION", system_id: "account_service", resource_id: "account_2", operation: "VERIFY_RECORD_ABSENT", state: "ABSENT", evidence_id: "", step_no: 1 }],
  trace: { basis: "BLOCKED", blockers: ["account_2:STATE_UNKNOWN:HISTORICAL_READINGS_ONLY"] },
  evaluations: [{
    ...violation, case_id: "case_b", resource_id: "account_2", system: null, outcome: "INCONCLUSIVE",
    reason: "STATE_UNKNOWN:HISTORICAL_READINGS_ONLY", underlying_outcome: "INCONCLUSIVE", establishing: [], citation: null,
    finding: null, attribution: { attribution: "EVIDENCE_LINKED" }, notes: [],
  }],
  remediations: [],
};

const caseC: CaseResult = {
  case_id: "case_c", deletion_status: "NOT_APPLICABLE", steps: [],
  trace: { basis: "NON_DELETION_REQUEST", blockers: [], intent: "NON_DELETION", intent_rule: "NON_DELETION", intent_llm: "NON_DELETION" },
  evaluations: [], remediations: [],
};

const HOSTILE = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script> search_9 here';

export const result: AuditResult = {
  dataset: "synthetic", requests: "synthetic/deletion_requests.csv", output: null,
  cases: [caseA, caseB, caseC],
  summary: { rows: 3, status: { INCOMPLETE: 2, NOT_APPLICABLE: 1 }, basis: { FINDINGS: 1, BLOCKED: 1, NON_DELETION_REQUEST: 1 }, steps: 4 },
  warnings: ["sql_point_in_time_header: none of the 1 SQL dump(s) has the header line"],
  diagnostics: {
    files: [
      { file_id: "ev_2", file_path: "evidence/databases/snap.sql", format: "sql", observations: 2, diagnostics: 0 },
      { file_id: "ev_3", file_path: "evidence/logs/trace.log", format: "log", observations: 3, diagnostics: 1 },
      { file_id: "ev_9", file_path: "evidence/records/missing.json", format: "json", observations: 0, diagnostics: 1 },
    ],
    parser: [
      { file_id: "ev_3", locator: { line: 4 }, severity: "warning", reason: "unparsed log line", excerpt: HOSTILE },
      { file_id: "ev_9", locator: { file_path: "evidence/records/missing.json" }, severity: "error", reason: "manifest entry has no file", excerpt: "" },
    ],
    coverage: {
      files: 2, tokens: 5, represented: 4,
      unresolved: [{ file_id: "ev_3", token: "search_9", line: 4, excerpt: HOSTILE }],
      other_id_tokens: { ticket: 3 }, other_examples: { ticket: "ev_3 L2: ticket_88" }, unreadable: ["ev_9"],
    },
  },
};

export const HOSTILE_EXCERPT = HOSTILE;
export const AUDIT_ID = "a".repeat(32);
export const completed: Job = { audit_id: AUDIT_ID, dataset_id: "synthetic", status: "completed", result };
