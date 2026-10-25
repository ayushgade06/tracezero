"""M3: evidence-grounded matching of requests to activity events.

DESIGN NOTE
Matching unit. A request is paired with at most one event of its family:
  account_deletion        deletion (or ambiguous-intent) requests  <-> DELETE_RECORD on account_service
  request_classification  non-deletion requests                    <-> CLASSIFY_REQUEST
The event's resource_id is then the request's account. Other activity (analytics anonymize,
search deletes, ...) carries no request or identity information beyond timing, so it is not
matched here (temporal proximity alone is not ownership).

Schema facts this rests on (system_activity.csv): events have activity_id, event_time,
event_type, source/target system, resource_id, reported_result, details. No event names a
request, and only LINK_RESOURCE_IDENTITY details name a person. So the only explicit
request-to-event association is: the event's account is tied to the requester by an M2
direct evidence link.

Hard constraints
  H1 window   requested_at <= event_time <= reported_at + 15 min (a job runs after the request
              and before the workflow reports on it; 15 min covers the -8 min seen in explicit matches)
  H2 capacity one event per request and one request per event (every account-deletion event
              has a distinct account; no explicitly linked request has two such events)
  H3 exclusion an account linked by direct evidence to someone else within 60 days of the event
              is not a candidate for this requester (unless the requester is linked too)
  H4 family   deletion requests never take classification events and vice versa
  An "unmatched" option always exists, so no request is forced onto a weak candidate.

Objective (lexicographic, minimized globally per connected component):
  cost = 1e6 * [no contemporaneous explicit link]
         + 1e4 * (number of calibrated contradictions: result vs reported_status, atypical delay)
         + |reported_at - event_time| in minutes
Signals, calibrated on the 252 explicit-link matches (no sample answers used):
  explicit link        M2 direct link from the requester to the account within 60 days (max
                       observed distance 28 days)
  result consistency   SUCCESS/PARTIAL/FAILED vs COMPLETE/PARTIALLY_COMPLETE/NOT_STARTED:
                       252/252 consistent on explicit matches
  report delay         -8..75 min on explicit matches (p5-p95 46..74); outside -15..90 is atypical

Confidence and abstention (reason codes in the Decision):
  HIGH    EXPLICIT_LINK_IN_WINDOW: explicit link, in window, no contradiction, clear margin
  MEDIUM  EXPLICIT_LINK_WITH_CONTRADICTION, or UNIQUE_FEASIBLE_CANDIDATE /
          ASSIGNMENT_CLEAR_MARGIN: no explicit link, consistent result, typical delay
  LOW     INFERRED_WITH_CONTRADICTION: no explicit link and a result mismatch or atypical delay
  UNMATCHED  AMBIGUOUS_NEAR_TIE (another assignment costs < 30 min more: the p5-p95 spread of
          report delay, so timing cannot separate them), NO_CANDIDATE_IN_WINDOW,
          ALL_CANDIDATES_EXCLUDED, CANDIDATES_TAKEN, SUBJECT_UNRESOLVED
Ties are never broken by input order or identifier text: a near-tie abstains.

History and reuse. Links far from the event (> 60 days) are kept as notes
(HISTORICAL_SELF_LINK / HISTORICAL_OTHER_SUBJECT_LINK) and neither establish nor exclude a
match: a March association does not own a September event. Links come from M2 and may be
observed in files captured after audit_at; they are used only to associate, never to claim
deletion state. Nothing here says whether a deletion succeeded.
"""
import csv
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment

from audit import identity, intent

WINDOW_SLACK = timedelta(minutes=15)
CONTEMPORANEOUS = timedelta(days=60)
TYPICAL_DELAY_MIN = (-15, 90)
TIE_MARGIN_MIN = 30
RESULT_FOR_STATUS = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}
FAMILIES = {
    "account_deletion": lambda e: e.event_type == "DELETE_RECORD" and e.target_system == "account_service",
    "request_classification": lambda e: e.event_type == "CLASSIFY_REQUEST",
}
_EXPLICIT, _CONTRADICTION, _UNMATCHED, _INFEASIBLE = 1e6, 1e4, 1e8, 1e12


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass
class Request:
    case_id: str
    email: str
    requested_at: str
    reported_status: str
    reported_at: str
    audit_at: str
    intent: str
    family: str
    user_ids: list
    resolution: str


@dataclass
class Event:
    activity_id: str
    event_time: str
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str


@dataclass
class Signal:
    """Evidence for or against a candidate. evidence = provenance of the supporting records."""
    code: str
    detail: str
    evidence: list = field(default_factory=list)


@dataclass
class Candidate:
    case_id: str
    activity_id: str
    resource_id: str
    family: str
    event_time: str
    request_window: tuple
    delay_minutes: float
    explicit: bool
    feasible: bool
    rejected_reason: str = None
    supports: list = field(default_factory=list)
    contradictions: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    cost: float = None


@dataclass
class Decision:
    case_id: str
    family: str
    status: str                 # MATCHED / UNMATCHED
    confidence: str             # HIGH / MEDIUM / LOW / UNMATCHED
    reason: str
    method: str                 # explicit_evidence / unique_candidate / global_assignment / none
    activity_id: str = None
    resource_id: str = None
    margin_minutes: float = None
    chosen: Candidate = None
    considered: list = field(default_factory=list)  # every candidate looked at, incl. rejected

    def to_dict(self):
        return asdict(self)


def make_requests(rows, index):
    out = []
    for r in rows:
        label = intent.classify(r["request_text"])
        res = identity.resolve_request(index, r["email"])
        out.append(Request(r["case_id"], r["email"], r["requested_at"], r["reported_status"], r["reported_at"],
                           r["audit_at"], label,
                           "request_classification" if label == intent.NON_DELETION else "account_deletion",
                           res["user_ids"], res["status"]))
    return out


def load_events(dataset):
    with open(Path(dataset) / "system_activity.csv", encoding="utf-8", newline="") as f:
        return [Event(**r) for r in csv.DictReader(f)]


def _prov(l):
    return {"evidence_id": l.evidence_id, "obs_id": l.obs_id, "locator": l.locator, "user_id": l.user_id,
            "identity_field": l.identity_field, "identity_value": l.identity_value, "link_type": l.link_type,
            "observed_at": l.observed_at, "file_captured_at": l.file_captured_at}


def _when(l):
    return _ts(l.observed_at or l.file_captured_at)


def build_candidates(requests, events, links):
    """All candidates per request, feasible and rejected, each with its supporting and
    contradicting evidence. No choice is made here."""
    direct = defaultdict(list)
    for l in links:
        if l.direct:
            direct[l.resource_id].append(l)
    by_family = {f: sorted((e for e in events if pred(e)), key=lambda e: (e.event_time, e.activity_id))
                 for f, pred in FAMILIES.items()}
    out = {}
    for r in requests:
        lo, hi, rep = _ts(r.requested_at), _ts(r.reported_at) + WINDOW_SLACK, _ts(r.reported_at)
        cands = []
        for e in by_family[r.family]:
            t = _ts(e.event_time)
            ls = direct.get(e.resource_id, [])
            own = [l for l in ls if l.user_id in r.user_ids]
            in_window = lo <= t <= hi
            if not in_window and not own:
                continue  # unrelated event; only explicitly linked out-of-window events are reported
            near = lambda l: abs(_when(l) - t) <= CONTEMPORANEOUS  # noqa: E731
            own_near = [l for l in own if near(l)]
            other = [l for l in ls if l.user_id not in r.user_ids]
            other_near = [l for l in other if near(l)]
            c = Candidate(r.case_id, e.activity_id, e.resource_id, r.family, e.event_time,
                          (r.requested_at, (hi).strftime("%Y-%m-%dT%H:%M:%SZ")),
                          round((rep - t).total_seconds() / 60, 1), bool(own_near), in_window)
            if not in_window:
                c.rejected_reason = "OUT_OF_WINDOW"
            c.supports.append(Signal("EVENT_IN_WINDOW" if in_window else "EVENT_OUTSIDE_WINDOW",
                                     f"{e.event_type} {e.reported_result} at {e.event_time}; request window {c.request_window}",
                                     [{"evidence_id": e.activity_id, "event_time": e.event_time, "fields": ["event_time", "event_type", "target_system", "resource_id"]}]))
            if own_near:
                c.supports.append(Signal("EXPLICIT_LINK", "requester's identifier tied to this account by evidence",
                                         [_prov(l) for l in own_near]))
            if [l for l in own if not near(l)]:
                c.notes.append(Signal("HISTORICAL_SELF_LINK", "requester linked to this account only far from the event time",
                                      [_prov(l) for l in own if not near(l)]))
            if [l for l in other if not near(l)]:
                c.notes.append(Signal("HISTORICAL_OTHER_SUBJECT_LINK", "another subject linked far from the event time; not ownership of this event",
                                      [_prov(l) for l in other if not near(l)]))
            if other_near:
                sig = Signal("OTHER_SUBJECT_LINK", "another subject tied to this account near the event time", [_prov(l) for l in other_near])
                if own_near:
                    c.contradictions.append(sig)  # both linked: keep feasible, flag the conflict
                elif in_window:
                    c.feasible, c.rejected_reason = False, "OTHER_SUBJECT_CONTEMPORANEOUS_LINK"
                    c.contradictions.append(sig)
            if r.family == "account_deletion":
                want = RESULT_FOR_STATUS.get(r.reported_status)
                ok = e.reported_result == want
                (c.supports if ok else c.contradictions).append(Signal(
                    "RESULT_CONSISTENT" if ok else "RESULT_STATUS_MISMATCH",
                    f"event result {e.reported_result} vs reported_status {r.reported_status}",
                    [{"evidence_id": e.activity_id, "fields": ["reported_result"]}]))
                if not TYPICAL_DELAY_MIN[0] <= c.delay_minutes <= TYPICAL_DELAY_MIN[1]:
                    c.contradictions.append(Signal("ATYPICAL_DELAY", f"report delay {c.delay_minutes} min outside {TYPICAL_DELAY_MIN}",
                                                   [{"evidence_id": e.activity_id, "fields": ["event_time"]}]))
            else:
                c.supports.append(Signal("EVENT_CLASSIFIES_NON_DELETION", e.details, [{"evidence_id": e.activity_id, "fields": ["details"]}]))
            if c.feasible:
                penalties = sum(s.code in ("RESULT_STATUS_MISMATCH", "ATYPICAL_DELAY") for s in c.contradictions)
                c.cost = (0 if c.explicit else _EXPLICIT) + _CONTRADICTION * penalties + abs(c.delay_minutes)
            cands.append(c)
        out[r.case_id] = cands
    return out


def _solve(C):
    rows, cols = linear_sum_assignment(C)
    return dict(zip(rows, cols)), C[rows, cols].sum()


def assign(requests, candidates):
    """Global one-to-one assignment per family and connected component, then per-request
    margins (cost increase if the chosen pair were forbidden) for confidence/abstention."""
    decisions = {}
    for family in FAMILIES:
        reqs = [r for r in requests if r.family == family]
        # connected components over shared feasible events
        parent = {r.case_id: r.case_id for r in reqs}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        holder = {}
        for r in reqs:
            for c in candidates[r.case_id]:
                if c.feasible:
                    if c.activity_id in holder:
                        parent[find(r.case_id)] = find(holder[c.activity_id])
                    else:
                        holder[c.activity_id] = r.case_id
        comps = defaultdict(list)
        for r in reqs:
            comps[find(r.case_id)].append(r)
        for comp in comps.values():
            decisions.update(_decide_component(comp, candidates))
    return decisions


def _decide_component(comp, candidates):
    events = sorted({c.activity_id for r in comp for c in candidates[r.case_id] if c.feasible})
    col = {a: j for j, a in enumerate(events)}
    n, m = len(comp), len(events)
    C = np.full((n, m + n), _INFEASIBLE)
    for i, r in enumerate(comp):
        C[i, m + i] = _UNMATCHED
        for c in candidates[r.case_id]:
            if c.feasible:
                C[i, col[c.activity_id]] = c.cost
    chosen, total = _solve(C)
    taken = {events[j]: comp[i].case_id for i, j in chosen.items() if j < m}
    out = {}
    for i, r in enumerate(comp):
        cands = candidates[r.case_id]
        feas = [c for c in cands if c.feasible]
        j = chosen[i]
        if j >= m:
            if not r.user_ids and r.family == "account_deletion" and not cands:
                reason = "SUBJECT_UNRESOLVED"
            elif feas:
                reason = "CANDIDATES_TAKEN"
            elif cands and any(c.rejected_reason == "OTHER_SUBJECT_CONTEMPORANEOUS_LINK" for c in cands):
                reason = "ALL_CANDIDATES_EXCLUDED"
            else:
                reason = "NO_CANDIDATE_IN_WINDOW"
            out[r.case_id] = Decision(r.case_id, r.family, "UNMATCHED", "UNMATCHED", reason, "none", considered=cands)
            continue
        C2 = C.copy()
        C2[i, j] = _INFEASIBLE
        _, total2 = _solve(C2)
        margin = round(float(total2 - total), 1)
        pick = next(c for c in feas if col[c.activity_id] == j)
        if margin < TIE_MARGIN_MIN:
            out[r.case_id] = Decision(r.case_id, r.family, "UNMATCHED", "UNMATCHED", "AMBIGUOUS_NEAR_TIE", "global_assignment",
                                      margin_minutes=margin, considered=cands)
            continue
        hard_contra = [s for s in pick.contradictions if s.code in ("RESULT_STATUS_MISMATCH", "ATYPICAL_DELAY", "OTHER_SUBJECT_LINK")]
        if pick.explicit:
            conf, reason, method = ("HIGH", "EXPLICIT_LINK_IN_WINDOW", "explicit_evidence") if not hard_contra else \
                ("MEDIUM", "EXPLICIT_LINK_WITH_CONTRADICTION", "explicit_evidence")
        else:
            competitors = any(c.activity_id == pick.activity_id for other in comp if other is not r
                              for c in candidates[other.case_id] if c.feasible)
            method = "global_assignment" if (len(feas) > 1 or competitors) else "unique_candidate"
            if hard_contra:
                conf, reason = "LOW", "INFERRED_WITH_CONTRADICTION"
            else:
                conf, reason = "MEDIUM", "ASSIGNMENT_CLEAR_MARGIN" if method == "global_assignment" else "UNIQUE_FEASIBLE_CANDIDATE"
        out[r.case_id] = Decision(r.case_id, r.family, "MATCHED", conf, reason, method, pick.activity_id, pick.resource_id,
                                  margin, pick, cands)
    return out


def match(request_rows, events, links, index):
    """-> (requests, candidates by case_id, decisions by case_id). Deterministic: inputs are
    sorted by case number / event time + activity_id before solving."""
    def case_key(row):
        n = row["case_id"].rsplit("_", 1)[-1]
        return (int(n) if n.isdigit() else 0, row["case_id"])
    requests = make_requests(sorted(request_rows, key=case_key), index)
    candidates = build_candidates(requests, events, links)
    return requests, candidates, assign(requests, candidates)
