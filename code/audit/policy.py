"""M5: policy evaluation (findings) and remediation planning over M4 state assessments.

Rule sources: [SPEC] the audit output contract (policy and remediation rules), [RULES] dataset/deletion_rules.csv,
[DATA] observed evidence semantics, [ASSUME] conservative engineering choice.

Three separate, deterministic stages:
  1. category(assessment)          which deletion_rules category applies (or UNKNOWN)
  2. evaluate(assessment)          one Evaluation per (request, resource): outcome VIOLATION |
                                   COMPLIANT | INCONCLUSIVE | NOT_ATTRIBUTABLE, and a Finding
                                   only for an attributable, admissible, non-stale violation
  3. plan(finding)                 the single remediation the policy permits for that finding

CATEGORY [DATA/ASSUME]: (1) explicit record_type on the resource's observations (disagreement ->
CATEGORY_CONFLICTING); (2) the resource's own collection, mapped by name (COLLECTION_CATEGORY,
[ASSUME]: collection names never co-occur with record_type in the corpus); (3) support content
signals [DATA]: message fields/events (message_text, message_excerpt, message_*) vs attachment
events (attachment_*), from any observation of the resource (a category is a fixed property,
like discovery, not state at the cutoff); (4) the id prefix when explicit record types for that
prefix are unanimous (support_ is not: message vs attachment); (5) name-based prefixes
tombstone_ / marketing_ [ASSUME]; (6) a support_ record's deletion-worker operation
(DELETE_RECORD -> SUPPORT_ATTACHMENT, REDACT_RECORD -> SUPPORT_MESSAGE) [DATA: matches all 111
support records categorised by record_type or collection]. (6) is a workflow claim, so it
plans a finding but never proves compliance: noted CATEGORY_FROM_WORKER_OPERATION, which
blocks COMPLETE (final.py), and never used for M6.1 provenance. Otherwise CATEGORY_UNKNOWN ->
INCONCLUSIVE.
CATEGORY PROVENANCE (M6.1, option B): the category is also resolved from captures admissible at
audit_at only. Outcomes are unchanged; the evaluation (and its finding/remediation) is noted
CATEGORY_POST_AUDIT_ONLY when only later captures establish it, or
CATEGORY_SOURCES_DISAGREE:admissible=X when the admissible sources give another answer. A post-audit
category is never claimed as established at the cutoff.
RETENTION (rule_12/13): a present record is retained within its supplied period; an expired period
blocks (RETENTION_EXPIRED_NO_RULE), and so do supplied ends that disagree about audit_at
(RETENTION_CONFLICTING; none in the corpus). D3 (revised, approved): a present SECURITY_AUDIT_LOG with no
retention period supplied by any observation is retained (RETAINED_NO_PERIOD_SUPPLIED, cited by
its presence); rule_12 gives no end, and the sample answers treat such a record as retained. A
present LEGAL_HOLD_RECORD is itself the supplied hold: with no supplied end it is active (approved).

POLICY MATRIX (POLICY below, loaded from audit/deletion_rules.csv; a custom file via load_policy and
PolicyContext(policy=...)). Required action per category from the dataset's deletion_rules.csv [RULES];
DATA_FLOW is not in that rules file and follows 6.5 "no active sync ... can recreate" [SPEC].
Unverifiable rule conditions (rule_05 shared context, rule_06 attachment belongs only to the
subject, rule_14 restore can be blocked) are treated as met and noted [ASSUME].

OWNERSHIP GATE [SPEC brief]: only EVIDENCE_LINKED and ACTIVITY_MATCH_HIGH/MEDIUM associations
can produce a finding. LOW matches, historical/candidate links and unmatched-request
alternatives are NOT_ATTRIBUTABLE; a would-be finding is kept only as `potential_violation`.
CONFLICT GATE (M6; window and owners narrowed in M6.1, see state.py A10): if another registered
user is a direct owner evidenced in [requested_at - 60d, audit_at] (association
`conflicting_owners`), the owner is uncertain -> NOT_ATTRIBUTABLE / OWNERSHIP_CONFLICT; the
would-be finding and the competing owners are kept. Unregistered identifiers are only noted
(UNRESOLVED_IDENTIFIER:...). A screenshot whose on-screen times postdate its capture adds the note
CAPTURE_TIME_DISPUTED:<file>. PolicyContext(conflict_gate=False) reproduces the pre-M6 behaviour.

UNKNOWN / CONFLICTING / STALE never produce findings (INCONCLUSIVE with a reason). The spec
defines no provisional findings, so none are emitted. A request without findings is not
thereby COMPLETE; completion is decided later (M6/M7).

CITATION ROLES per finding: establishing (deciding admissible M4 readings of the violating
aspect, with locators), citation (the latest establishing reading; ties -> latest file
capture, then lowest evidence id, flagged), identifying (M2 link evidence / M3 activity:
never used to cite state), contradicting (M4), inadmissible (M4 exclusions).
"""
import csv
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from audit.inputs import InputError
from audit.matching import _ts
from audit.observation import leaves
from audit.state import _iso

DELETE, REDACT, ANONYMIZE, RETAIN, FLOW = "DELETE", "REDACT", "ANONYMIZE", "RETAIN", "FLOW"

ACTIONS = (DELETE, REDACT, ANONYMIZE, RETAIN, FLOW)
DEFAULT_POLICY_FILE = Path(__file__).with_name("deletion_rules.csv")
POLICY_COLUMNS = ("rule_id", "data_category", "required_action")  # required
POLICY_OPTIONAL_COLUMNS = ("condition", "source", "assumed_condition")  # condition is documentation only


class PolicyError(InputError):
    """The policy file cannot be used; the message names the file and every problem row or field."""


def load_policy(path):
    """deletion_rules.csv -> {category: (rule_id, required action, source, noted assumption)}, in file order.

    Every problem is collected and raised together as PolicyError; nothing is skipped or defaulted
    except the optional columns (source -> "RULES", assumed_condition -> no note)."""
    path = Path(path)
    try:
        with open(path, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames or []
            rows = list(reader)
    except OSError as e:
        raise PolicyError(f"{path}: cannot read policy file: {e.strerror or e}") from None
    except UnicodeDecodeError as e:
        raise PolicyError(f"{path}: not UTF-8 text ({e.reason} at byte {e.start})") from None
    missing = [c for c in POLICY_COLUMNS if c not in header]
    unknown = [c for c in header if c not in POLICY_COLUMNS + POLICY_OPTIONAL_COLUMNS]
    if missing or unknown or len(set(header)) != len(header):
        raise PolicyError(f"{path}: header {header}: " + "; ".join(
            ([f"missing required column(s) {missing}"] if missing else []) +
            ([f"unknown column(s) {unknown}"] if unknown else []) +
            (["duplicate column names"] if len(set(header)) != len(header) else [])))
    errors, policy, rule_rows, cat_rows = [], {}, {}, {}
    for n, r in enumerate(rows, 2):  # n = line number, the header is line 1
        if None in r:
            errors.append(f"{path}:{n}: more fields than the header")
        rid, cat, act = ((r.get(c) or "").strip() for c in POLICY_COLUMNS)
        bad = ([] if rid else ["empty rule_id"]) + ([] if cat else ["empty data_category"]) + (
            [] if act in ACTIONS else [f"required_action {act!r} is not one of {list(ACTIONS)}"])
        if rid in rule_rows:
            bad.append(f"duplicate rule_id {rid!r} (first on line {rule_rows[rid]})")
        elif rid:
            rule_rows[rid] = n  # also for a row rejected below, so a later duplicate is still reported
        if cat in cat_rows:
            bad.append(f"duplicate data_category {cat!r} (first on line {cat_rows[cat]})")
        elif cat:
            cat_rows[cat] = n
        if bad:
            errors.append(f"{path}:{n}: " + "; ".join(bad))
            continue
        policy[cat] = (rid, act, (r.get("source") or "").strip() or "RULES", (r.get("assumed_condition") or "").strip() or None)
    gone = [c for c in REQUIRED_CATEGORIES if c not in policy]
    if gone:
        errors.append(f"{path}: no rule for required data_category {gone} (the engine assigns these categories itself)")
    if errors:
        raise PolicyError("\n".join(errors))
    return policy


COLLECTION_CATEGORY = {  # [ASSUME] name-based; only a resource's own collection is used
    "customer_profiles": "USER_PROFILE", "customer_search_documents": "SEARCH_INDEX_ENTRY",
    "support_conversations": "SUPPORT_MESSAGE", "ticket_attachments": "SUPPORT_ATTACHMENT",
    "customer_analytics_reports": "ANALYTICS_PROFILE", "customer_export_jobs": "USER_EXPORT",
    "connector_configurations": "DATA_FLOW", "backup_catalog": "BACKUP_ARCHIVE",
    "security_audit_events": "SECURITY_AUDIT_LOG", "retained_order_records": "LEGAL_HOLD_RECORD",
    "deleted_customer_references": "ACCOUNT_TOMBSTONE",
}
FINDING_FOR = {DELETE: ("REPORT_REMAINING_RECORD", "PRESENT"), REDACT: ("REPORT_EXPOSED_PII", "PRESENT"),
               ANONYMIZE: ("REPORT_EXPOSED_PII", "PRESENT"), FLOW: ("REPORT_ACTIVE_DATA_FLOW", "ACTIVE"),
               RETAIN: ("REPORT_RESTORE_RISK", "ACTIVE")}  # RETAIN: backups only
REMEDIATION_FOR = {("REPORT_REMAINING_RECORD", DELETE): ("DELETE_RECORD", "ABSENT"),
                   ("REPORT_EXPOSED_PII", REDACT): ("REDACT_RECORD", "REDACTED"),
                   ("REPORT_EXPOSED_PII", ANONYMIZE): ("ANONYMIZE_RECORD", "ANONYMIZED"),
                   ("REPORT_ACTIVE_DATA_FLOW", FLOW): ("DISABLE_DATA_FLOW", "DISABLED"),
                   ("REPORT_RESTORE_RISK", RETAIN): ("BLOCK_RESTORE", "BLOCKED")}
PREFIX_NAME_CATEGORY = {"tombstone": "ACCOUNT_TOMBSTONE", "marketing": "MARKETING_PROFILE"}  # [ASSUME] name-based
WORKER_SUPPORT_CATEGORY = {"DELETE_RECORD": "SUPPORT_ATTACHMENT", "REDACT_RECORD": "SUPPORT_MESSAGE"}  # [DATA] source (6)
ATTRIBUTABLE = ("EVIDENCE_LINKED", "ACTIVITY_MATCH_HIGH", "ACTIVITY_MATCH_MEDIUM")
# Categories the engine derives from names, content and workflow claims: a policy must give each a rule.
REQUIRED_CATEGORIES = tuple(sorted({*COLLECTION_CATEGORY.values(), *PREFIX_NAME_CATEGORY.values(), *WORKER_SUPPORT_CATEGORY.values()}))
# category -> (rule_id, required action, source, noted assumption about the rule condition); the
# default policy, loaded from audit/deletion_rules.csv (the only place it is written down).
POLICY = load_policy(DEFAULT_POLICY_FILE)


@dataclass
class Finding:
    finding_id: str
    case_id: str
    resource_id: str
    system: str
    category: str
    category_basis: str
    rule_id: str
    finding_type: str       # output operation, e.g. REPORT_REMAINING_RECORD
    state: str              # output state, e.g. PRESENT / ACTIVE
    aspect: str
    aspect_value: str
    basis: str              # M4 basis of the violating aspect
    conclusion: str         # confirmed (evidence-linked owner) | inferred (activity-matched owner)
    attribution: dict
    establishing: list
    citation: dict
    identifying: list
    contradicting: list
    inadmissible: list
    reason: str
    explanation: str
    notes: list = field(default_factory=list)


@dataclass
class Remediation:
    remediation_id: str
    finding_id: str
    case_id: str
    resource_id: str
    system: str
    operation: str
    target_state: str
    rule_id: str
    permitted_because: str
    establishing_evidence: list
    caveats: list = field(default_factory=list)


@dataclass
class Evaluation:
    case_id: str
    resource_id: str
    system: str
    category: str
    category_basis: str
    rule_id: str
    action: str
    outcome: str            # VIOLATION | COMPLIANT | INCONCLUSIVE | NOT_ATTRIBUTABLE
    reason: str
    underlying_outcome: str  # what the evidence says before the ownership gate
    attribution: dict
    establishing: list
    citation: dict
    finding: Finding = None
    potential_violation: dict = None
    notes: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


class PolicyContext:
    """Corpus facts the policy needs: unanimous id-prefix categories, observation attributes
    (retention fields) and file capture times (citation tie-breaks), and the policy matrix in use
    (default POLICY; a custom one comes from load_policy)."""

    def __init__(self, observations, manifest=None, conflict_gate=True, events=(), policy=None):
        self.conflict_gate = conflict_gate
        self.policy = POLICY if policy is None else policy
        self.worker_ops = defaultdict(set)  # support_ resource -> categories implied by deletion-worker operations
        for ev in events:
            if ev.source_system == "deletion_worker" and ev.resource_id.startswith("support_") and ev.event_type in WORKER_SUPPORT_CATEGORY:
                self.worker_ops[ev.resource_id].add(WORKER_SUPPORT_CATEGORY[ev.event_type])
        types = defaultdict(Counter)
        self.attrs = {}
        self.support_signals = defaultdict(set)
        self.obs_by_resource = defaultdict(list)
        self.notes = {}  # (case_id, resource_id) -> note raised while judging
        for o in observations:
            if o.resource_id and o.record_type in self.policy:
                types[o.resource_id.rsplit("_", 1)[0]][o.record_type] += 1
            self.attrs[o.obs_id] = o.attrs
            if o.resource_id:
                self.obs_by_resource[o.resource_id].append(o.obs_id)
            if o.resource_id and o.resource_id.startswith("support_"):
                op = o.operation or ""
                if "message_text" in o.attrs or "message_excerpt" in o.attrs or op.startswith("message_"):
                    self.support_signals[o.resource_id].add(("SUPPORT_MESSAGE", o.file_captured_at))
                if op.startswith("attachment_"):
                    self.support_signals[o.resource_id].add(("SUPPORT_ATTACHMENT", o.file_captured_at))
        self.prefix_category = {p: next(iter(c)) for p, c in types.items() if len(c) == 1}
        self.captured = {m["file_id"]: m["captured_at"] for m in (manifest or [])}
        self.captured.update({o.file_id: o.file_captured_at for o in observations})


def category(a, ctx, admissible=False):
    """-> (category or None, basis). admissible=True uses only captures <= audit_at (M6.1 provenance)."""
    cats = a.categories_admissible if admissible else a.categories
    explicit = sorted({c for c in cats if c in ctx.policy})
    if len(explicit) > 1:
        return None, "CATEGORY_CONFLICTING"
    if explicit:
        return explicit[0], "explicit_record_type"
    by_coll = sorted({COLLECTION_CATEGORY[c] for c in cats if c in COLLECTION_CATEGORY})
    if len(by_coll) > 1:
        return None, "CATEGORY_CONFLICTING"
    if by_coll:
        return by_coll[0], "collection_name"
    audit = _iso(_ts(a.audit_at))
    signals = sorted({s for s, t in ctx.support_signals.get(a.resource_id, ()) if not admissible or (t and t <= audit)})
    if len(signals) > 1:
        return None, "CATEGORY_CONFLICTING"
    if signals:
        return signals[0], "support_content_signal"
    prefix = a.resource_id.rsplit("_", 1)[0]
    if prefix in ctx.prefix_category:
        return ctx.prefix_category[prefix], "unanimous_id_prefix"
    if prefix in PREFIX_NAME_CATEGORY:
        return PREFIX_NAME_CATEGORY[prefix], "prefix_name"
    ops = sorted(ctx.worker_ops.get(a.resource_id, ())) if not admissible else []
    if len(ops) > 1:
        return None, "CATEGORY_CONFLICTING"
    if ops:
        return ops[0], "worker_operation"
    return None, "CATEGORY_UNKNOWN"


def _cite(refs, ctx):
    if not refs:
        return None
    key = lambda r: (r["time"], ctx.captured.get(r["evidence_id"], ""))  # noqa: E731
    best = max(key(r) for r in refs)
    top = sorted((r for r in refs if key(r) == best), key=lambda r: (r["evidence_id"], r["ref"]))
    r = top[0]
    return {"evidence_id": r["evidence_id"], "ref": r["ref"], "locator": r.get("locator"), "time": r["time"],
            "tie": len({x["evidence_id"] for x in top}) > 1}


def _deciding(a, aspect, value):
    return [d for d in a.aspects[aspect]["deciding"] if d.get("facts", {}).get(aspect) == value]


def _retention_ok(a, ctx, field_name):
    """-> (True/False/None, source). The retention end is the record's own metadata (a policy
    condition, not state at the cutoff), so admissible readings are used first and any other
    observation of the record second (flagged). It never establishes presence."""
    def ends(refs):
        out = []
        for ref in refs:
            for path, v in leaves(ctx.attrs.get(ref, {})):
                if path.rsplit(".", 1)[-1] == field_name and isinstance(v, str):
                    out.append(v)
        return out
    ex = a.aspects["existence"]
    found = ends(d["ref"] for d in ex["deciding"] + ex["supporting"] + ex["contradicting"])
    source = "admissible_reading"
    if not found:
        found, source = ends(ctx.obs_by_resource.get(a.resource_id, ())), "RETENTION_FROM_OTHER_OBSERVATION"
    if not found:
        return None, None
    ok = {end > a.audit_at for end in found}
    return (ok.pop() if len(ok) == 1 else "CONFLICTING"), source  # an expired end is never outvoted by another


def _judge(a, action, cat, ctx):
    """-> (underlying outcome, reason, violating (aspect, value) or None, establishing refs)."""
    ex, expo, flow, rest = (a.aspects[k] for k in ("existence", "exposure", "flow", "restore"))

    def state_issue(asp):
        if asp["value"] == "UNKNOWN":
            return "STATE_UNKNOWN:" + (asp["reasons"][0] if asp["reasons"] else "NO_EVIDENCE")
        if asp["value"] == "CONFLICTING":
            return "STATE_CONFLICTING"
        if asp["basis"] == "OBSERVED_STALE":
            return "STATE_STALE"
        return None

    if action == FLOW:
        issue = state_issue(flow)
        if issue:
            return "INCONCLUSIVE", issue, None, []
        if flow["value"] == "ACTIVE":
            return "VIOLATION", "ACTIVE_DATA_FLOW", ("flow", "ACTIVE"), _deciding(a, "flow", "ACTIVE")
        return "COMPLIANT", "FLOW_DISABLED", None, _deciding(a, "flow", "DISABLED")
    if action == RETAIN and cat == "BACKUP_ARCHIVE":
        issue = state_issue(rest)
        if issue:
            return "INCONCLUSIVE", issue, None, []
        if rest["value"] == "ALLOWED":
            return "VIOLATION", "RESTORE_PATH_OPEN", ("restore", "ALLOWED"), _deciding(a, "restore", "ALLOWED")
        return "COMPLIANT", "RESTORE_BLOCKED", None, _deciding(a, "restore", "BLOCKED")
    issue = state_issue(ex)
    if issue:
        return "INCONCLUSIVE", issue, None, []
    if action == RETAIN:
        if ex["value"] == "ABSENT":
            return "INCONCLUSIVE", "RETAINED_RECORD_MISSING", None, []
        ok, source = _retention_ok(a, ctx, "retained_until" if cat == "LEGAL_HOLD_RECORD" else "retention_until")
        if source == "RETENTION_FROM_OTHER_OBSERVATION":
            ctx.notes[(a.case_id, a.resource_id)] = source
        present = _deciding(a, "existence", "PRESENT")  # kept: the record's presence is still established
        if ok is None:
            if cat in ("SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"):  # D3 (revised): no supplied end, so nothing has ended
                return "COMPLIANT", "RETAINED_NO_PERIOD_SUPPLIED", None, present
            return "INCONCLUSIVE", "RETENTION_UNVERIFIABLE", None, present
        if ok == "CONFLICTING":
            return "INCONCLUSIVE", "RETENTION_CONFLICTING", None, present
        if not ok:
            return "INCONCLUSIVE", "RETENTION_EXPIRED_NO_RULE", None, present
        return "COMPLIANT", "RETAINED_WITHIN_PERIOD", None, _deciding(a, "existence", "PRESENT")
    if action == DELETE:
        if ex["value"] == "PRESENT":
            return "VIOLATION", "RECORD_REMAINS", ("existence", "PRESENT"), _deciding(a, "existence", "PRESENT")
        return "COMPLIANT", "RECORD_ABSENT", None, _deciding(a, "existence", "ABSENT")
    # REDACT / ANONYMIZE
    if ex["value"] == "ABSENT":
        if cat == "ACCOUNT_TOMBSTONE":
            return "INCONCLUSIVE", "TOMBSTONE_MISSING", None, []
        return "COMPLIANT", "RECORD_ABSENT_NO_EXPOSURE", None, _deciding(a, "existence", "ABSENT")
    issue = state_issue(expo)
    if issue:
        return "INCONCLUSIVE", "EXPOSURE_" + issue.replace("STATE_", ""), None, []
    if expo["value"] == "IDENTIFIABLE":
        return "VIOLATION", "IDENTIFIERS_EXPOSED", ("exposure", "IDENTIFIABLE"), _deciding(a, "exposure", "IDENTIFIABLE")
    want = "ANONYMIZED" if action == ANONYMIZE else "REDACTED"
    if expo["value"] == want:
        return "COMPLIANT", f"RECORD_{want}", None, _deciding(a, "exposure", want)
    return "INCONCLUSIVE", f"EXPOSURE_{expo['value']}_NOT_{want}", None, []


def _provenance_notes(a, ctx, cat):
    """M6.1 notes: category provenance (T2) and unresolved identifiers (A10). Never change outcomes."""
    notes = []
    if category(a, ctx)[1] == "worker_operation":
        notes.append("CATEGORY_FROM_WORKER_OPERATION")  # a workflow claim: plans a finding, never proves compliance
    elif a.categories_admissible is not None:
        adm, adm_basis = category(a, ctx, admissible=True)
        if adm is None and cat is not None:
            if adm_basis == "CATEGORY_UNKNOWN":
                notes.append("CATEGORY_POST_AUDIT_ONLY")  # no capture <= audit_at establishes the category
            else:
                notes.append(f"CATEGORY_SOURCES_DISAGREE:admissible={adm_basis}")
        elif adm is not None and adm != cat:
            notes.append(f"CATEGORY_SOURCES_DISAGREE:admissible={adm}")
    return notes + [x for x in a.ambiguity if x.startswith("UNRESOLVED_IDENTIFIER:")]


def evaluate(a, ctx):
    """One Evaluation for one M4 assessment."""
    cat, cat_basis = category(a, ctx)
    att = dict(a.association)
    provenance = _provenance_notes(a, ctx, cat)
    if cat is None:
        return Evaluation(a.case_id, a.resource_id, a.system, None, cat_basis, None, None,
                          "NOT_ATTRIBUTABLE" if att["attribution"] not in ATTRIBUTABLE else "INCONCLUSIVE",
                          cat_basis, "INCONCLUSIVE", att, [], None, notes=provenance)
    rule_id, action, source, assumption = ctx.policy[cat]
    underlying, reason, violating, refs = _judge(a, action, cat, ctx)
    notes = ([assumption] if assumption else []) + ([ctx.notes[(a.case_id, a.resource_id)]] if (a.case_id, a.resource_id) in ctx.notes else [])
    notes += provenance
    if "EXISTENCE_REPLICA_DRIFT" in a.reasons:
        notes.append("REPLICA_DRIFT:" + ",".join(f"{k}={v}" for k, v in sorted(a.aspects["existence"]["per_copy"].items())))
    notes += sorted({f"CAPTURE_TIME_DISPUTED:{r['evidence_id']}" for r in refs if "capture_time_disputed" in r.get("fields", ())})
    citation = _cite(refs, ctx)
    ev = Evaluation(a.case_id, a.resource_id, a.system, cat, cat_basis, rule_id, action, underlying, reason,
                    underlying, att, refs, citation, notes=notes)
    if underlying == "VIOLATION" and not refs:  # defensive: never a finding without admissible evidence
        ev.outcome, ev.reason = "INCONCLUSIVE", "NO_ESTABLISHING_EVIDENCE"
        return ev
    if att["attribution"] not in ATTRIBUTABLE:
        ev.outcome, ev.reason = "NOT_ATTRIBUTABLE", f"OWNERSHIP_{att['attribution']}"
        if underlying == "VIOLATION":
            ev.potential_violation = {"would_be": FINDING_FOR[action][0], "reason": reason, "citation": citation}
        return ev
    if ctx.conflict_gate and att.get("conflicting_owners"):
        ev.outcome, ev.reason = "NOT_ATTRIBUTABLE", "OWNERSHIP_CONFLICT"
        ev.notes.append("CONFLICTING_OWNERS:" + ",".join(att["conflicting_owners"]))
        if underlying == "VIOLATION":
            ev.potential_violation = {"would_be": FINDING_FOR[action][0], "reason": reason, "citation": citation,
                                      "conflicting_owners": att["conflicting_owners"]}
        return ev
    if underlying == "VIOLATION":
        ftype, fstate = FINDING_FOR[action]
        aspect, value = violating
        identifying = [{"evidence_id": e, "role": "M2_LINK"} for e in att.get("link_evidence", [])]
        if att.get("m3"):
            identifying.append({"evidence_id": att["m3"]["activity_id"], "role": f"M3_MATCH_{att['m3']['confidence']}"})
        conclusion = "inferred" if att["attribution"] == "ACTIVITY_MATCH_MEDIUM" else "confirmed"
        ev.finding = Finding(
            f"{a.case_id}:{a.resource_id}:{rule_id}", a.case_id, a.resource_id, a.system, cat, cat_basis, rule_id,
            ftype, fstate, aspect, value, a.aspects[aspect]["basis"], conclusion, att, refs, citation, identifying,
            a.aspects[aspect]["contradicting"], a.excluded, reason,
            f"{cat} requires {action} ({rule_id}); at {a.audit_at} the evidence shows {aspect}={value}",
            notes=list(notes))
    return ev


def plan(finding, policy=None):
    """The single remediation permitted for one finding (raises on a non-permitted pairing)."""
    _, action, _, _ = (POLICY if policy is None else policy)[finding.category]
    op, target = REMEDIATION_FOR[(finding.finding_type, action)]
    caveats = list(finding.notes)
    if finding.conclusion == "inferred":
        caveats.append("OWNER_FROM_ACTIVITY_MATCH_MEDIUM")
    if finding.citation and finding.citation["tie"]:
        caveats.append("CITATION_TIE")
    return Remediation(f"{finding.finding_id}:{op}", finding.finding_id, finding.case_id, finding.resource_id, finding.system,
                       op, target, finding.rule_id,
                       f"{finding.category} requires {action} under {finding.rule_id}; {finding.finding_type} -> {op}",
                       sorted({r["evidence_id"] for r in finding.establishing}), caveats)


def run(assessments, ctx):
    """-> (evaluations, findings, remediations), deterministic (assessment order)."""
    evaluations = [evaluate(a, ctx) for a in assessments]
    findings = [e.finding for e in evaluations if e.finding]
    remediations = [plan(f, ctx.policy) for f in findings]
    return evaluations, findings, remediations
