// The API contract (docs/API.md) and AuditResult.to_dict() (docs/ARCHITECTURE.md#library-interface).
// Field names are the engine's; nothing here is computed by the browser.

export type JobStatus = "queued" | "running" | "completed" | "failed";

export interface ApiErrorBody {
  code: string;
  message: string;
}

export interface Job {
  audit_id: string;
  dataset_id: string;
  status: JobStatus;
  result?: AuditResult;
  error?: ApiErrorBody;
}

export type Locator = Record<string, unknown>;

export interface Citation {
  evidence_id: string;
  ref: string;
  locator: Locator | null;
  time: string | null;
  tie: boolean;
}

/** An M4 reading (establishing or contradicting). */
export interface Reading {
  ref: string;
  evidence_id: string;
  kind?: string;
  copy?: string;
  time?: string | null;
  locator?: Locator | null;
  fields?: string[];
  facts?: Record<string, unknown>;
  [key: string]: unknown;
}

/** An M4 exclusion: evidence that was not admissible at audit_at. */
export interface Exclusion {
  ref: string;
  evidence_id: string;
  reason: string;
}

export interface Attribution {
  attribution: string;
  source?: string;
  attributed_to_requester?: boolean;
  link_evidence?: string[];
  competing_owners?: string[];
  conflicting_owners?: string[];
  m3?: { confidence: string; reason: string; activity_id: string } | null;
  [key: string]: unknown;
}

export interface Finding {
  finding_id: string;
  finding_type: string;
  state: string;
  aspect: string;
  aspect_value: string;
  basis: string;
  conclusion: string;
  establishing: Reading[];
  citation: Citation | null;
  identifying: { evidence_id: string; role: string }[];
  contradicting: Reading[];
  inadmissible: Exclusion[];
  reason: string;
  explanation: string;
  notes: string[];
}

export type Outcome = "VIOLATION" | "COMPLIANT" | "INCONCLUSIVE" | "NOT_ATTRIBUTABLE";

export interface Evaluation {
  case_id: string;
  resource_id: string;
  system: string | null;
  category: string;
  category_basis: string;
  rule_id: string;
  action: string;
  outcome: Outcome | string;
  reason: string;
  underlying_outcome: string;
  attribution: Attribution;
  establishing: Reading[];
  citation: Citation | null;
  finding: Finding | null;
  potential_violation: { would_be: string; reason: string; citation: Citation | null; conflicting_owners?: string[] } | null;
  notes: string[];
}

export interface Remediation {
  remediation_id: string;
  finding_id: string;
  resource_id: string;
  system: string | null;
  operation: string;
  target_state: string;
  rule_id: string;
  permitted_because: string;
  establishing_evidence: string[];
  caveats: string[];
}

export interface Step {
  phase: "FINDING" | "REMEDIATION" | "VERIFICATION" | string;
  system_id: string;
  resource_id: string;
  operation: string;
  state: string;
  evidence_id: string; // "" when the step is uncited
  step_no: number;
}

export interface Trace {
  basis: string;
  blockers: string[];
  system_from_m2_link?: string[];
  intent?: string | null;
  intent_rule?: string | null;
  intent_llm?: string | null;
}

export interface CaseResult {
  case_id: string;
  deletion_status: "COMPLETE" | "INCOMPLETE" | "NOT_APPLICABLE" | string;
  steps: Step[];
  trace: Trace;
  evaluations: Evaluation[];
  remediations: Remediation[];
}

export interface ParserDiagnostic {
  file_id: string;
  locator: Locator | string | null;
  severity: "info" | "warning" | "error" | string;
  reason: string;
  excerpt: string | null;
}

export interface Coverage {
  files: number;
  tokens: number;
  represented: number;
  unresolved: { file_id: string; token: string; line: number; excerpt: string }[];
  other_id_tokens: Record<string, number>;
  other_examples: Record<string, string>; // prefix -> "file_id L<line>: token"
  unreadable: string[];
}

export interface AuditResult {
  dataset: string;
  requests: string;
  output: string | null;
  cases: CaseResult[];
  summary: { rows: number; status: Record<string, number>; basis: Record<string, number>; steps: number };
  warnings: string[];
  diagnostics: {
    files: { file_id: string; file_path: string; format: string; observations: number; diagnostics: number }[];
    parser: ParserDiagnostic[];
    coverage: Coverage;
  };
}
