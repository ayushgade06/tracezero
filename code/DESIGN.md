# Design Notes

Per-stage rules, approved decisions and audit fixes, moved from the README. The README has the overview; `code/evaluation/evaluation_report.md` has the metrics and failure analysis.

| Milestone | Scope |
|---|---|
| M3 | Matches each request to at most one account-deletion (or, for non-deletion requests, classification) event using a global one-to-one assignment with explicit abstention. |
| M4 | Derives, for every associated resource, the evidence-supported state at `audit_at`, using only evidence captured by then. UNKNOWN and CONFLICTING are explicit outcomes. |
| M5 | Applies `deletion_rules.csv` to each state and labels it VIOLATION, COMPLIANT, INCONCLUSIVE or NOT_ATTRIBUTABLE. Only attributable, admissible violations produce a finding, a single permitted remediation, and a proposed citation. |
| M6 | OCRs the screenshots into provenance-preserving observations, and adds an ownership-conflict gate to M5. Details below. |
| M6.1 | Audit fixes: readings from before `requested_at` are history, not current state; category provenance is flagged; the conflict gate counts only registered owners evidenced by `audit_at`; on-screen times later than a screenshot's capture are flagged. Details below. |
| M7 | Turns the M5 evaluations into each request's `deletion_status` and `steps_json`, adds a strict validator, an evaluation runner with custom cases, a gap report, the reports and packaging. Details below. |

**Final output (M7).** `code/audit/final.py` assembles every row from M5 results only; no new evidence interpretation happens there.

- **Statuses:**
  - `NOT_APPLICABLE`: the text doesn't ask for deletion (`[]`).
  - `INCOMPLETE`: at least one finding. Steps are the findings (cited), exactly one remediation per finding, and the checks that must pass afterwards (uncited).
  - `COMPLETE`: no finding, no blocking evaluation, and an evidence-backed compliant state for the requester's account or tombstone. Steps are one cited verification per compliant evaluation.
  - Otherwise `INCOMPLETE` with uncited after-the-fix checks only, and never a fabricated finding.
- **What blocks COMPLETE:**
  - unknown, conflicting, stale, historical-only, post-audit-only or activity-only state;
  - an unknown category;
  - an ended retention period (a security audit record or legal hold present with no supplied end is retained: D3 revised);
  - an ownership conflict;
  - a post-audit-only or disputed category on a compliant record, or a category taken only from the deletion worker's operation.
- **Evidence-gap rows:** one row (case_61) has no statable check at all. It is `INCOMPLETE` `[]`, and the validator always warns about it.
- **Order:** phase, then the contract's operation table, then `system_id`, then `resource_id`; `step_no` runs 1…n.

Results: 518 rows; 10 `NOT_APPLICABLE`, 415 `INCOMPLETE` (350 with findings), 93 `COMPLETE`. On the 32 reference sample cases, status accuracy is 0.875 and step F1 is 0.907 (M7 baseline: 0.844 and 0.872).

**Accuracy review (after M7).** Two evidence-backed changes were accepted, each with regression tests:
- support records with no other category source take it from the deletion worker's operation (it matches all 111 evidence-categorised support records); this plans findings but never proves COMPLETE;
- D3 revised (approved): a present security audit record with no supplied retention period is retained.

A second round added two approved rules:
- R4e: in a point-in-time database snapshot that concerns the requester, a resource missing from every copy of a table is absent. Inventory dumps, which demonstrably omit present records, still prove nothing;
- a present legal hold with no supplied end is active.

A final review audited every status change case by case and kept both rules. It also fixed two latent gaps without changing the output:
- a later recreation claim now makes an inferred absence stale;
- retention ends that disagree about `audit_at` now block COMPLETE.

Rejected experiments are documented in the evaluation report. See `code/evaluation/evaluation_report.md` for metric definitions, failure analysis and known gaps.

**OCR (M6).**
- **Engine:** `rapidocr-onnxruntime==1.4.4`, with its bundled PP-OCRv4 ONNX models, single-threaded and with no preprocessing.
- **Cache:** raw OCR boxes (text, confidence, bounding box) are committed in `code/cache/ocr/`. Each entry is keyed by image SHA-256, engine version, model hashes and configuration, and a mismatched entry is rejected.
- **Clean checkouts** read the cache and need no OCR engine.
- **Layout:** screenshots are parsed into template units (header, KPI cards, table rows, detail panel, activity, search box).
- **What a screenshot can establish:** only a unit with exactly one confidently read resource ID becomes a record. It can establish only that the resource is present, and that an identifier is exposed when an email or phone is visible. A missing identifier never means redaction or absence, and connector or restore state is never read from a screenshot. Everything else is kept as context.
- **Conflict gate in M5:** if other direct owners are evidenced in the request's own period, the case is NOT_ATTRIBUTABLE with reason OWNERSHIP_CONFLICT (narrowed in M6.1, below).

**Audit fixes (M6.1).**
- **Historical readings:** a reading dated before `requested_at` proves only an earlier state. It is kept as context and never decides state, whether it says PRESENT or ABSENT. A copy last read before the request is UNKNOWN; the resource stays PRESENT only if a current copy (`requested_at` to `audit_at`) shows it. No evidence type is treated as durable.
- **Category provenance:** categories are unchanged, but an evaluation is noted `CATEGORY_POST_AUDIT_ONLY` when only captures after `audit_at` establish its category, or `CATEGORY_SOURCES_DISAGREE` when admissible captures say otherwise. The note carries through to the finding and the remediation.
- **Conflict gate:** a rival owner must be a registered user other than the requester, with a direct link dated in [`requested_at` − 60 days, `audit_at`] from a capture admissible at `audit_at`, and not named in the same observation as the requester (a co-subject). Unregistered identifiers are only noted as `UNRESOLVED_IDENTIFIER`.
- **Screenshot times:** a backward-looking on-screen time (Created, Updated, …) later than the manifest capture time raises `OCR_ONSCREEN_TIME_AFTER_CAPTURE`. The capture time is kept, every unit stays, and dependent findings are noted `CAPTURE_TIME_DISPUTED`. Only `evidence_23` triggers it.
