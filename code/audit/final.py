"""M7: request-level deletion status and steps_json, assembled from M5 evaluations only.

No evidence is interpreted here: every step is derived from an M5 Evaluation, Finding or
Remediation, and every citation is the citation M5 already chose. Approved decisions:

STATUS
  NOT_APPLICABLE  the request text does not ask for deletion (M0 intent); steps [].
  INCOMPLETE      at least one M5 finding: findings (cited), exactly one remediation per finding
                  (uncited), then the after-the-fix checks of D4 (uncited).
  COMPLETE        no finding, no blocking evaluation, at least one compliant evaluation, and (D2) an
                  evidence-backed compliant state of the requester's account or tombstone
                  (USER_PROFILE / ACCOUNT_TOMBSTONE). Steps: one verification per COMPLIANT
                  evaluation, citing M5's citation (a deciding admissible reading <= audit_at).
  INCOMPLETE      otherwise (D1): no finding but completion is not proven; verification-only,
                  uncited after-the-fix checks; never a fabricated finding or remediation.
BLOCKING (any one prevents COMPLETE)
  - an INCONCLUSIVE evaluation (always attributable in M5): unknown / conflicting / stale /
    historical-only / post-audit-only / weak-absence / activity-only state, unknown or conflicting
    category, missing retained record, expired retention (D3 revised: a security audit record or
    legal hold present with no supplied end is retained)
  - an OWNERSHIP_CONFLICT evaluation
  - (D6) a COMPLIANT evaluation noted CATEGORY_POST_AUDIT_ONLY or CATEGORY_SOURCES_DISAGREE
  - a COMPLIANT evaluation noted CATEGORY_FROM_WORKER_OPERATION (category from a workflow claim)
  - a COMPLIANT evaluation noted CAPTURE_TIME_DISPUTED or UNRESOLVED_IDENTIFIER (caveats on the
    proof itself; none occur on compliant evaluations in this corpus)
  NOT_ATTRIBUTABLE evaluations (not shown to be the requester's) neither block nor produce steps.
VERIFICATION SET for INCOMPLETE (D4): the remediated resources (the remediation's target state),
  COMPLIANT resources (their established state), and INCONCLUSIVE resources with a known category
  (the state the policy requires). These are checks that must pass, not observations.
ORDER: phase, then the contract's operation table, then system_id, resource_id; one step per
  (phase, system_id, resource_id); step_no 1..n. A missing citation raises.
SYSTEM_ID: M4's resolved system. If M4 has none, the single system recorded on admissible direct
  M2 links for the resource (catalog rows carry target_system; discovery.py) is used and noted in the
  trace; none or several distinct systems raise AssemblyError rather than dropping the step.
"""
from audit import intent, policy

PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")
OPERATIONS = ("REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK",
              "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD",
              "VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED", "VERIFY_RECORD_ANONYMIZED",
              "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED")
VERIFY_STATE = {"ABSENT": "VERIFY_RECORD_ABSENT", "PRESENT": "VERIFY_RECORD_PRESENT", "REDACTED": "VERIFY_RECORD_REDACTED",
                "ANONYMIZED": "VERIFY_RECORD_ANONYMIZED", "DISABLED": "VERIFY_DATA_FLOW_DISABLED", "BLOCKED": "VERIFY_RESTORE_BLOCKED"}
COMPLIANT_STATE = {"RECORD_ABSENT": "ABSENT", "RECORD_ABSENT_NO_EXPOSURE": "ABSENT", "RECORD_REDACTED": "REDACTED",
                   "RECORD_ANONYMIZED": "ANONYMIZED", "RETAINED_WITHIN_PERIOD": "PRESENT", "RETAINED_NO_PERIOD_SUPPLIED": "PRESENT", "FLOW_DISABLED": "DISABLED",
                   "RESTORE_BLOCKED": "BLOCKED"}
STATE_OF = {"REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT", "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
            "REPORT_RESTORE_RISK": "ACTIVE", "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED", "DELETE_RECORD": "ABSENT",
            "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED", "VERIFY_RECORD_ABSENT": "ABSENT",
            "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED", "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
            "VERIFY_DATA_FLOW_DISABLED": "DISABLED", "VERIFY_RESTORE_BLOCKED": "BLOCKED"}
ACCOUNT_CATEGORIES = ("USER_PROFILE", "ACCOUNT_TOMBSTONE")
CAVEATS_BLOCKING_PROOF = ("CATEGORY_POST_AUDIT_ONLY", "CATEGORY_SOURCES_DISAGREE", "CATEGORY_FROM_WORKER_OPERATION", "CAPTURE_TIME_DISPUTED", "UNRESOLVED_IDENTIFIER")


class AssemblyError(ValueError):
    """A required step field is missing; raised instead of silently dropping the step."""


def required_state(category):
    """The state the policy requires for a category (after-the-fix check of an unresolved resource)."""
    _, action, _, _ = policy.POLICY[category]
    if action == policy.RETAIN:
        return "BLOCKED" if category == "BACKUP_ARCHIVE" else "PRESENT"
    return {policy.DELETE: "ABSENT", policy.REDACT: "REDACTED", policy.ANONYMIZE: "ANONYMIZED", policy.FLOW: "DISABLED"}[action]


def _caveats(e):
    return [n for n in e.notes if n.split(":")[0] in CAVEATS_BLOCKING_PROOF]


def blockers(evaluations):
    """-> sorted list of 'resource:reason' strings that prevent COMPLETE."""
    out = []
    for e in evaluations:
        if e.outcome == "INCONCLUSIVE":
            out.append(f"{e.resource_id}:{e.reason}")
        elif e.reason == "OWNERSHIP_CONFLICT":
            out.append(f"{e.resource_id}:OWNERSHIP_CONFLICT")
        elif e.outcome == "COMPLIANT" and _caveats(e):
            out.append(f"{e.resource_id}:" + "+".join(sorted({c.split(':')[0] for c in _caveats(e)})))
    return sorted(out)


def link_systems(links):
    """resource -> [(time, system)] from direct M2 links that name a system (fallback for system_id)."""
    out = {}
    for l in links:
        t = l.observed_at or l.file_captured_at
        if l.direct and l.system and t:
            out.setdefault(l.resource_id, []).append((max(t, l.file_captured_at or t), l.system))
    return out


def _system(case_id, e, audit_at, fallback, trace):
    if e.system:
        return e.system
    names = sorted({s for t, s in (fallback or {}).get(e.resource_id, ()) if audit_at and t <= audit_at})
    if len(names) != 1:
        raise AssemblyError(f"{case_id}: no system_id for {e.resource_id} (M4 unknown; admissible M2 link systems {names})")
    used = trace.setdefault("system_from_m2_link", [])
    if f"{e.resource_id}:{names[0]}" not in used:
        used.append(f"{e.resource_id}:{names[0]}")
    return names[0]


def _step(case_id, phase, system, resource, operation, evidence_id):
    if not system or not resource:
        raise AssemblyError(f"{case_id}: {phase} {operation} has no system_id/resource_id ({system!r}, {resource!r})")
    return {"phase": phase, "system_id": system, "resource_id": resource, "operation": operation,
            "state": STATE_OF[operation], "evidence_id": evidence_id}


def _cited(case_id, e):
    if not e.citation or not e.citation.get("evidence_id"):
        raise AssemblyError(f"{case_id}: {e.resource_id} {e.outcome}/{e.reason} has no citation")
    return e.citation["evidence_id"]


def _order(case_id, steps):
    steps.sort(key=lambda s: (PHASES.index(s["phase"]), OPERATIONS.index(s["operation"]), s["system_id"], s["resource_id"]))
    keys = [(s["phase"], s["system_id"], s["resource_id"]) for s in steps]
    if len(keys) != len(set(keys)):
        raise AssemblyError(f"{case_id}: duplicate (phase, system_id, resource_id) steps")
    for n, s in enumerate(steps, 1):
        s["step_no"] = n
    return steps


def assemble(case_id, intent_label, evaluations, remediation_of, audit_at=None, fallback_systems=None):
    """-> (deletion_status, steps, trace) for one request.
    evaluations: this request's M5 Evaluations; remediation_of: finding_id -> M5 Remediation;
    audit_at (ISO-Z) and fallback_systems (link_systems()) are used only when M4 has no system."""
    if intent_label == intent.NON_DELETION:
        return "NOT_APPLICABLE", [], {"basis": "NON_DELETION_REQUEST", "blockers": []}
    evaluations = sorted(evaluations, key=lambda e: e.resource_id)
    findings = [e for e in evaluations if e.finding]
    trace = {"blockers": blockers(evaluations)}
    system = lambda e: _system(case_id, e, audit_at, fallback_systems, trace)  # noqa: E731
    steps = []
    if findings:
        for e in findings:
            f, r = e.finding, remediation_of[e.finding.finding_id]
            steps.append(_step(case_id, "FINDING", system(f), f.resource_id, f.finding_type, _cited(case_id, f)))
            steps.append(_step(case_id, "REMEDIATION", system(r), r.resource_id, r.operation, ""))
        status, trace["basis"] = "INCOMPLETE", "FINDINGS"
    else:
        compliant = [e for e in evaluations if e.outcome == "COMPLIANT"]
        account = [e for e in compliant if e.category in ACCOUNT_CATEGORIES]
        if not trace["blockers"] and compliant and account:
            for e in compliant:
                steps.append(_step(case_id, "VERIFICATION", system(e), e.resource_id, VERIFY_STATE[COMPLIANT_STATE[e.reason]],
                                   _cited(case_id, e)))
            trace["basis"] = "COMPLIANCE_PROVEN"
            return "COMPLETE", _order(case_id, steps), trace
        status = "INCOMPLETE"
        trace["basis"] = "BLOCKED" if trace["blockers"] else ("NO_ACCOUNT_PROOF" if compliant else "NO_COMPLIANT_EVIDENCE")
    steps += checks(case_id, evaluations, remediation_of, system)
    return status, _order(case_id, steps), trace


def checks(case_id, evaluations, remediation_of, system=lambda e: e.system):
    """D4 after-the-fix checks (uncited): what must hold once the request is completed."""
    out = []
    for e in evaluations:
        if e.finding:
            r = remediation_of[e.finding.finding_id]
            state = r.target_state
        elif e.outcome == "COMPLIANT":
            state = COMPLIANT_STATE[e.reason]
        elif e.outcome == "INCONCLUSIVE" and e.category is not None:
            state = required_state(e.category)
        else:
            continue  # NOT_ATTRIBUTABLE, or INCONCLUSIVE with unknown/conflicting category: no stated treatment
        out.append(_step(case_id, "VERIFICATION", system(e), e.resource_id, VERIFY_STATE[state], ""))
    return out
