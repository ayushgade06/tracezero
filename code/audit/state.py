"""M4: evidence-supported resource state at each request's audit_at.

Rule sources: [SPEC] the audit output contract, [DATA] observed evidence semantics,
[ASSUME] conservative engineering choice. Raw observations, links and matches are never
modified; everything here is derived.

STATE MODEL. Each (request, resource) assessment holds four independent aspects:
  existence  PRESENT | ABSENT | CONFLICTING | UNKNOWN            (all resources)
  exposure   IDENTIFIABLE | REDACTED | ANONYMIZED | CONFLICTING | UNKNOWN  (identifier content)
  flow       ACTIVE | DISABLED | CONFLICTING | UNKNOWN              (data_flow_*)
  restore    ALLOWED | BLOCKED | CONFLICTING | UNKNOWN              (backup_*)
each with a basis: OBSERVED, OBSERVED_STALE (a later admissible operation/recreation claim
may have changed an observed or inferred state), INFERRED (scoped absence only), or NONE. Redaction or anonymization
never implies absence; the aspects are resolved separately. [SPEC]
Summary roll-up: ABSENT, PRESENT, RESIDUAL (present but redacted/anonymized), CONFLICTING,
UNKNOWN.

ADMISSIBILITY (per request)
  A1 [SPEC]   only files captured <= audit_at establish state (FILE_CAPTURED_AFTER_AUDIT)
  A2 [SPEC]   a record/observation time after audit_at is excluded (RECORD_AFTER_AUDIT)
  A3 [ASSUME] a record dated after its own file's capture is kept but flagged
              TIMESTAMP_INCONSISTENT and cannot establish state on its own
  A4 [DATA]   post-audit files may have revealed the resource (M2) but never its state
  A5 [SPEC]   activity events are claims: they never set state; after audit_at they are ignored
  A6 [DATA]   lookup attempts (*_started log lines) carry no result (LOOKUP_ATTEMPT_NO_RESULT)
  A7 [DATA]   observations naming only other people describe another instance of the ID
              (OTHER_SUBJECT_INSTANCE)
  A8 [M6]     screenshot units count only when OCR-VERIFIED (else OCR_TEXT_ONLY); they establish
              existence PRESENT and, if an email/phone is visible, exposure IDENTIFIABLE; never
              absence, redaction, flow or restore. Their time is the manifest captured_at.
  A9 [M6.1]   readings dated before requested_at are HISTORICAL: kept as context (note HISTORICAL),
              never deciding. Per copy: a copy whose latest reading is historical is UNKNOWN; the
              aspect stays decided only if a current copy (requested_at <= t <= audit_at) shows
              PRESENT / IDENTIFIABLE (R2), else UNKNOWN (HISTORICAL_READINGS_ONLY /
              HISTORICAL_COPY_UNRESOLVED). Applies to PRESENT and ABSENT alike; no evidence type
              is treated as durable (the spec defines no validity periods; expiry/retention fields
              are forward-looking obligations, not proof of later state).
  A10 [M6.1]  conflicting_owners: registered users other than the requester with a direct link
              dated in [requested_at - 60d, audit_at] (inclusive) from a capture admissible at
              audit_at, and not in an observation that also names the requester (co-subject).
              Unregistered identifiers in that window are ambiguity (UNRESOLVED_IDENTIFIER:...),
              never a rival owner. Owners outside the window are possible ID reuse.

READING KINDS (semantics-aware R1). snapshot = a row of a captured table/collection (time =
snapshot capture time); lookup = a point read/ledger/handover result (time = its own
timestamp); scan = a PII/identity scan line; scoped_absence = an R4 absence. Only these
establish state. Operation results and other activity are claims (Claim) and can only mark a
reading stale or corroborate it; a newer claim never supersedes an observed state.

RECONCILIATION
  R1 [DATA]   within one copy of one aspect, the latest admissible reading decides; different
              values at that same latest time -> CONFLICTING (both kept)
  R2 [SPEC]   existence: any copy PRESENT -> PRESENT (replica drift kept per copy); exposure:
              any copy IDENTIFIABLE -> IDENTIFIABLE ("every relevant copy")
  R3 [ASSUME] copies are never merged into one timeline; the newest copy does not override another
  R4 [SPEC+DATA] INFERRED ABSENT comes only from
              (a) an EMPTY scoped snapshot of the resource's system/collection (a JSON snapshot
                  with no records, or a snapshot_scope system with no rows of that system in the
                  file) whose capture concerns the requester: a record in that file links to them.
                  A capture that names no known person but falls in the audit-evidence window
                  (reported_at, audit_at] yields only a WEAK absence: kept as context, never
                  deciding (WEAK_ABSENCE_ONLY) [ASSUME: such a capture cannot be attributed];
              (b) copy comparison inside one SQL dump: listed in one copy's table (e.g. read
                  replica) but missing from its sibling table (e.g. primary) -> that copy ABSENT
                  (dump headers: "Compare the primary and any replica tables").
              (c) R4e (after M7, approved): a point-in-time database snapshot (dump header "point-in-
                  time database snapshot") that concerns the requester (a row in it links to them):
                  a resource missing from every copy of a table -> each copy ABSENT. The table
                  covers its own system, or every system when rows carry origin_system. [DATA: no
                  such omission is contradicted by any other reading in the corpus; the requester's
                  other records in the same snapshot are already read from it.]
              Inventory dumps (no such header) and bundles are partial cohort extracts: they omit
              named users' audit records seen minutes later, so a non-empty listing that omits a
              resource is "not found", never an explicit negative.
  R5 [SPEC]   absence from unscoped or partial listings (logs, ledgers, handover, lookup exports,
              inventory dumps, non-empty extracts, snapshots about other people) proves nothing
  R6          nothing admissible -> UNKNOWN with one primary reason: WEAK_ABSENCE_ONLY /
              ONLY_INCONSISTENT_TIMESTAMPS / POST_AUDIT_EVIDENCE_ONLY / HISTORICAL_READINGS_ONLY /
              NO_ADMISSIBLE_STATE_OBSERVATION / ACTIVITY_ONLY / NO_EVIDENCE
"""
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from functools import lru_cache

from audit.matching import CONTEMPORANEOUS, _ts
from audit.observation import NULLS, RESOURCE_RE, leaves

ASPECTS = ("existence", "exposure", "flow", "restore")
_EXPLICIT_SYSTEM = ("system", "origin_system", "context.data_store", "json_path", "record_json.system")
_COPY = re.compile(r"(read_replica|reporting_replica|failover_replica|replica_[a-z_]+)")
# Ordered tuples (not sets): field lists end up in the output and must not depend on hash seeds.
_ID_FIELDS = ("email", "phone", "email_hash", "full_name", "name", "display_name", "owner_email", "owner_phone",
              "customer_email", "requester_name", "requester_email", "requester_phone", "matched_value")
_ANALYTICS_LINK_FIELDS = ("email", "phone", "email_hash", "user_id", "customer_reference")
_TOMBSTONE_FIELDS = ("name", "full_name", "email", "phone", "email_hash")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_CLAIMS = {  # event_type -> (aspect, implied value) for the event's own resource
    "DELETE_RECORD": ("existence", "ABSENT"), "ANONYMIZE_RECORD": ("exposure", "ANONYMIZED"),
    "REDACT_RECORD": ("exposure", "REDACTED"), "DISABLE_DATA_FLOW": ("flow", "DISABLED"),
    "BLOCK_RESTORE": ("restore", "BLOCKED"), "CHECK_DATA_FLOW": ("flow", "ACTIVE"),
    "CHECK_RESTORE_ACCESS": ("restore", "ALLOWED"),
}
_RECREATION = ("SYNC_RECORD", "RESTORE_BACKUP")  # "... repopulated downstream resource X"


def _bool(v):
    s = str(v).strip().lower()
    return True if s in ("true", "1") else False if s in ("false", "0") else None


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Reading:
    """One state-bearing statement derived from one observation (or from a scope)."""
    ref: str
    evidence_id: str
    resource_id: str
    kind: str
    copy: str
    time: str
    record_time: str
    file_captured_at: str
    facts: dict
    fields: list
    users: tuple
    unregistered: bool
    locator: dict
    inconsistent: bool = False
    weak: bool = False  # absence from a capture that names no one: context only, never decides


@dataclass
class Claim:
    activity_id: str
    resource_id: str
    time: str
    event_type: str
    result: str
    aspect: str
    value: str

    @property
    def effective(self):  # a FAILED operation claims no change
        return self.result in ("SUCCESS", "PARTIAL")


@dataclass
class AspectState:
    value: str = "UNKNOWN"
    basis: str = "NONE"
    per_copy: dict = field(default_factory=dict)
    deciding: list = field(default_factory=list)
    supporting: list = field(default_factory=list)
    contradicting: list = field(default_factory=list)
    reasons: list = field(default_factory=list)


@dataclass
class Assessment:
    case_id: str
    requested_at: str
    reported_at: str
    audit_at: str
    resource_id: str
    system: str
    system_basis: str
    categories: list
    association: dict
    aspects: dict
    summary: str
    excluded: list
    reasons: list
    ambiguity: list
    categories_admissible: list = None  # M6.1: category inputs from captures <= audit_at (None: not computed)

    def to_dict(self):
        return asdict(self)


# ------------------------------------------------------------------ interpretation

def _content(o):
    """Flat {leaf_name: value} of a record, nested record_json/document/profile included."""
    out = {}
    for path, v in leaves(o.attrs):
        out.setdefault(path.rsplit(".", 1)[-1].split("[")[0], v)
    return out


def _exposure(prefix, c):
    """Exposure stated by the record's own identifier fields, or None if not determinable."""
    def present(keys):
        return [k for k in keys if k in c]
    def nonnull(keys):
        return [k for k in keys if c.get(k) not in NULLS and c.get(k) != "[REDACTED]"]
    if prefix == "analytics":
        keys = present(_ANALYTICS_LINK_FIELDS)
        if keys:
            return ("IDENTIFIABLE" if nonnull(keys) else "ANONYMIZED"), keys
    elif prefix == "tombstone":
        keys = present(_TOMBSTONE_FIELDS)
        if keys:
            return ("IDENTIFIABLE" if nonnull(keys) else "REDACTED"), keys
    elif prefix == "support":
        requester = present(("requester_name", "requester_email", "requester_phone"))
        text_key = "message_text" if "message_text" in c else "message_excerpt" if "message_excerpt" in c else None
        text = str(c.get(text_key) or "") if text_key else ""
        ids = nonnull(_ID_FIELDS)
        if ids or _EMAIL.search(text):
            return "IDENTIFIABLE", ids + ([text_key] if text_key else [])
        if text == "[REDACTED]" or requester:  # identifier fields present but all removed
            return "REDACTED", requester + ([text_key] if text_key else [])
    else:
        ids = nonnull(_ID_FIELDS)
        if ids:
            return "IDENTIFIABLE", ids
    return None, []


def interpret(o):
    """-> (facts {aspect: value}, fields used) for one observation, or (None, reason)."""
    op = o.operation or ""
    if op.endswith("_started"):
        return None, "LOOKUP_ATTEMPT_NO_RESULT"
    if o.file_format == "image":  # M6: screenshot units (see audit/ocr.py)
        if o.attrs.get("_ocr_trust") != "VERIFIED":
            return None, "OCR_TEXT_ONLY"
        f = {"existence": "PRESENT"}  # the resource is shown in its own row / record view
        if o.attrs.get("_ocr_direct_identifiers"):
            f["exposure"] = "IDENTIFIABLE"  # a visible email or phone; absence never means REDACTED
        used = ["ocr_visible_unit"] + (["ocr_direct_identifiers"] if "exposure" in f else [])
        if o.attrs.get("_ocr_capture_disputed"):
            used.append("capture_time_disputed")  # M6.1: flagged, still admissible (manifest time kept)
        return f, used
    c = _content(o)
    prefix = o.resource_id.rsplit("_", 1)[0]
    f, used = {}, []
    result = str(c.get("result") or "").upper()
    if result:
        used.append("result")
        if result in ("FOUND", "HIT"):
            f["existence"] = "PRESENT"
        elif result in ("NOT_FOUND", "MISS"):
            f["existence"] = "ABSENT"
        elif result in ("IDENTIFIABLE", "ANONYMIZED", "REDACTED"):
            f["existence"], f["exposure"] = "PRESENT", result
        elif result == "ACTIVE":
            f["existence"] = "PRESENT"
            if prefix == "data_flow":
                f["flow"] = "ACTIVE"
            elif prefix == "backup":
                f["restore"] = "ALLOWED"
        elif result == "DISABLED":
            f["existence"], f["flow"] = "PRESENT", "DISABLED"
        elif result == "BLOCKED":
            f["existence"], f["restore"] = "PRESENT", "BLOCKED"
    http, rows = str(c.get("http_status") or c.get("status_code") or ""), str(c.get("rows") or "")
    if "existence" not in f and (http or rows):
        used.append("http_status/rows")
        if http == "404" or rows == "0":
            f["existence"] = "ABSENT"
        elif http == "200" or rows not in ("", "0"):
            f["existence"] = "PRESENT"
    if c.get("matched_value") not in NULLS:
        f.update(existence="PRESENT", exposure="IDENTIFIABLE")
        used.append("matched_value")
    if "direct_identifiers" in c:
        f["existence"] = "PRESENT"
        f.setdefault("exposure", "IDENTIFIABLE" if str(c["direct_identifiers"]) not in ("0", "") else "ANONYMIZED")
        used.append("direct_identifiers")
    if _bool(c.get("available")):  # export download check: the export object exists
        f.setdefault("existence", "PRESENT")
        used.append("available")
    if o.file_format == "sql" or _is_snapshot(o):
        f.setdefault("existence", "PRESENT")  # a row in a captured table/collection
        used.append("snapshot_row")
    elif o.file_format == "json" and o.locator.get("json_path", "").startswith("service_exports") and "existence" not in f:
        f["existence"] = "PRESENT"  # returned by the service in its export listing
        used.append("listed_by_service_export")
    elif o.file_format == "log" and "existence" not in f and re.search(r"(_read_completed|_read|_loaded)$", op):
        f["existence"] = "PRESENT"  # a read that returned the record
        used.append("event")
    if prefix == "data_flow" and _bool(c.get("enabled")) is not None:
        f["flow"] = "ACTIVE" if _bool(c["enabled"]) else "DISABLED"
        used.append("enabled")
    if prefix == "backup":
        if _bool(c.get("restore_allowed")) is not None:
            f["restore"] = "ALLOWED" if _bool(c["restore_allowed"]) else "BLOCKED"
            used.append("restore_allowed")
        elif _bool(c.get("eligible")) is not None and _bool(c.get("deletion_block")) is not None:
            f["restore"] = "ALLOWED" if _bool(c["eligible"]) and not _bool(c["deletion_block"]) else "BLOCKED"
            used.append("eligible/deletion_block")
        elif str(c.get("decision") or "").lower() in ("deny", "allow"):
            f["restore"] = "BLOCKED" if c["decision"].lower() == "deny" else "ALLOWED"
            used.append("decision")
    if "exposure" not in f and f.get("existence") == "PRESENT":
        exp, keys = _exposure(prefix, c)
        if exp:
            f["exposure"] = exp
            used += keys
    if f.get("existence") == "ABSENT":
        f.pop("exposure", None)
    return (f, used) if f else (None, "NO_STATE_FIELDS")


def _is_snapshot(o):
    if o.file_format == "sql":
        return True
    if o.file_format != "json":
        return False
    path = o.locator.get("json_path", "")
    return bool(o.context.get("collection")) and "parent_resource_id" not in o.context \
        and not path.startswith("service_exports") and "recorded_at" not in o.attrs


def _kind(o):
    if _is_snapshot(o):
        return "snapshot"
    if o.file_format == "log" and (o.operation or "").endswith("_scan"):
        return "scan"
    return "lookup"


def _copy(o):
    m = _COPY.search(o.locator.get("table") or "")
    if m:
        return m.group(1)
    svc = str(o.attrs.get("service") or "") if o.file_format == "log" else ""
    return svc if "replica" in svc else "primary"


def _snapshot_time(o):
    if o.file_format == "sql":
        for k in ("captured_at", "snapshot_completed_at"):
            v = o.context.get(k)
            if v:
                return _iso(_ts(v.strip()))
        return o.file_captured_at
    if (o.observed_at_field or "").startswith("context."):
        return o.observed_at
    v = o.context.get("captured_at")
    return _iso(_ts(v)) if v else o.file_captured_at


# ------------------------------------------------------------------ corpus index

class StateIndex:
    """Readings, scopes and claims precomputed once from M1/M2 outputs and activity."""

    def __init__(self, observations, links, events):
        self.users_by_obs = defaultdict(set)
        self.unregistered_obs = set()
        self.file_users = defaultdict(set)
        for l in links:
            if l.user_id:
                self.users_by_obs[l.obs_id].add(l.user_id)
            else:
                self.unregistered_obs.add(l.obs_id)
        prefix_sys, coll_of_sys, self.collections = defaultdict(Counter), defaultdict(set), defaultdict(set)
        self.collection_captures = defaultdict(list)  # resource -> [(collection, file captured_at)] (category provenance)
        explicit = defaultdict(Counter)
        for o in observations:
            if o.resource_id and o.system and o.system_source in _EXPLICIT_SYSTEM:
                explicit[o.resource_id][o.system] += 1
                prefix_sys[o.resource_id.rsplit("_", 1)[0]][o.system] += 1
        self.explicit_system = {r: c.most_common(1)[0][0] for r, c in explicit.items()}
        self.prefix_system = {p: c.most_common(1)[0][0] for p, c in prefix_sys.items() if len(c) == 1}
        for o in observations:
            # a collection belongs to the system its container declares (JSON data_store, SQL
            # origin_system), and to a resource only when that system is the resource's own
            scope = o.kind == "scope"
            coll = o.attrs.get("collection_name") or o.context.get("collection") or (o.attrs.get("collection") if scope else None)
            owner = o.attrs.get("origin_system") or o.context.get("data_store") or (o.attrs.get("data_store") if scope else None)
            if coll and owner:
                coll_of_sys[owner].add(coll)
                if o.resource_id and self.system(o.resource_id)[0] == owner:
                    self.collections[o.resource_id].add(coll)
                    self.collection_captures[o.resource_id].append((coll, o.file_captured_at))
        self.coll_of_sys = coll_of_sys
        self.sys_of_coll = {c: s for s, cs in coll_of_sys.items() for c in cs}
        self.readings, self.skipped = defaultdict(list), defaultdict(list)
        self.observations_by_res = defaultdict(list)
        for o in observations:
            for u in self.users_by_obs.get(o.obs_id, ()):
                self.file_users[o.file_id].add(u)
            if not o.resource_id or o.kind != "record":
                continue
            self.observations_by_res[o.resource_id].append(o)
            facts, used = interpret(o)
            if facts is None:
                self.skipped[o.resource_id].append((o, used))  # used = reason code here
                continue
            kind = _kind(o)
            time = _snapshot_time(o) if kind == "snapshot" else (o.observed_at or o.file_captured_at)
            self.readings[o.resource_id].append(Reading(
                o.obs_id, o.file_id, o.resource_id, kind, _copy(o), time, o.observed_at, o.file_captured_at, facts, used,
                tuple(sorted(self.users_by_obs.get(o.obs_id, ()))), o.obs_id in self.unregistered_obs, o.locator,
                bool(o.observed_at and o.observed_at > o.file_captured_at)))
        self._build_scopes(observations)
        self.claims = defaultdict(list)
        for e in events:
            if e.event_type in _CLAIMS:
                a, v = _CLAIMS[e.event_type]
                self.claims[e.resource_id].append(Claim(e.activity_id, e.resource_id, e.event_time, e.event_type, e.reported_result, a, v))
            elif e.event_type in _RECREATION:
                for rid in RESOURCE_RE.findall(e.details):
                    if rid != e.resource_id:
                        self.claims[rid].append(Claim(e.activity_id, rid, e.event_time, e.event_type, e.reported_result, "existence", "PRESENT"))
        self.event_resources = {e.resource_id for e in events}

    def system(self, rid):
        if rid in self.explicit_system:
            return self.explicit_system[rid], "explicit"
        s = self.prefix_system.get(rid.rsplit("_", 1)[0])
        return (s, "inferred_from_id_prefix") if s else (None, "unknown")

    def _build_scopes(self, observations):
        """Absence-capable scopes (R4): empty scoped snapshots and sibling copy tables.
        Each: file_id, copy, time, ref, cover {(system, collection|None)}, listed ids, captured,
        empty (True for R4a; False for R4b sibling comparisons, which need `pair`)."""
        rows = defaultdict(list)
        for o in observations:
            if o.kind == "record" and o.resource_id:
                if o.file_format == "sql":
                    rows[(o.file_id, o.locator["table"])].append(o)
                elif _is_snapshot(o):
                    rows[(o.file_id, re.sub(r"\.?records\[\d+\]$", "", o.locator["json_path"]))].append(o)
        self.scopes = []
        file_systems = defaultdict(set)
        for (fid, _), rs in rows.items():
            file_systems[fid].update(self.system(o.resource_id)[0] for o in rs)
        tables = defaultdict(list)
        for o in observations:
            if o.kind != "scope":
                continue
            if o.file_format == "sql" and o.locator.get("table") == "snapshot_scope" and "row" in o.locator:
                # R4a: system declared captured, and the file holds no rows of that system
                if o.system and o.observed_at and o.system not in file_systems[o.file_id]:
                    self.scopes.append(dict(file_id=o.file_id, copy="primary", time=o.observed_at, ref=o.obs_id,
                                            cover={(o.system, None)}, listed=set(), captured=o.file_captured_at, empty=True))
            elif o.file_format == "sql" and "row" not in o.locator and o.locator["table"] != "snapshot_scope":
                tables[o.file_id].append(o)
            elif o.file_format == "json" and o.attrs.get("collection") and o.attrs.get("record_count") == 0:
                coll = o.attrs["collection"]
                sysname = o.system or o.attrs.get("data_store") or self.sys_of_coll.get(coll)
                if sysname:  # R4a: empty JSON snapshot of one collection
                    self.scopes.append(dict(file_id=o.file_id, copy="primary", time=o.observed_at or o.file_captured_at, ref=o.obs_id,
                                            cover={(sysname, coll)}, listed=set(), captured=o.file_captured_at, empty=True))
        for fid, ts in tables.items():  # R4b: sibling copies of the same table inside one dump
            groups = defaultdict(list)
            for t in ts:
                base = _COPY.sub("", t.locator["table"]).replace("primary", "").strip("_")
                groups[base].append(t)
            for sib in groups.values():
                listed = {t.locator["table"]: {r.resource_id for r in rows.get((fid, t.locator["table"]), [])} for t in sib}
                union = set().union(*listed.values())
                if all(t.context.get("ghost_data_still_exists") == "point-in-time database snapshot" for t in sib):
                    mixed = all("origin_system" in (t.attrs.get("columns") or ()) for t in sib)
                    cover = None if mixed else {self.system(r)[0] for r in union}
                    if mixed or (len(cover) == 1 and None not in cover):
                        for t in sib:  # R4e: missing from every copy of a point-in-time table
                            self.scopes.append(dict(file_id=fid, copy=_copy(t), time=_snapshot_time(t), ref=t.obs_id, cover=cover,
                                                    listed=union, captured=t.file_captured_at, empty=False, pit=True))
                if len(sib) < 2:
                    continue
                for t in sib:
                    self.scopes.append(dict(file_id=fid, copy=_copy(t), time=_snapshot_time(t), ref=t.obs_id, cover=None,
                                            listed=listed[t.locator["table"]], pair=union, captured=t.file_captured_at, empty=False))

    def covers(self, scope, rid):
        if scope.get("pit"):  # R4e: the table's own system, or every system of a mixed table
            return scope["cover"] is None or self.system(rid)[0] in scope["cover"]
        if not scope["empty"]:  # sibling comparison: only resources listed in another copy of this dump
            return rid in scope["pair"]
        sysname = self.system(rid)[0]
        colls = self.collections.get(rid) or (self.coll_of_sys.get(sysname) if len(self.coll_of_sys.get(sysname, ())) == 1 else set())
        return any(s == sysname and (c is None or c in colls) for s, c in scope["cover"])



# ------------------------------------------------------------------ assessment

def _ref(r, **extra):
    d = {"ref": r.ref, "evidence_id": r.evidence_id, "kind": r.kind, "copy": r.copy, "time": r.time,
         "locator": r.locator, "fields": r.fields, "facts": r.facts}
    d.update(extra)
    return d


def _claim_ref(c, **extra):
    d = {"ref": c.activity_id, "evidence_id": c.activity_id, "kind": "activity_claim", "time": c.time,
         "event_type": c.event_type, "result": c.result, "implies": {c.aspect: c.value}}
    d.update(extra)
    return d


def assess_resource(si, req, rid, subject, association, unresolved=()):
    audit = _iso(_ts(req.audit_at))
    requested = _iso(_ts(req.requested_at))
    lo = _iso(_ts(req.reported_at))
    excluded, usable, inconsistent = [], [], []
    for o, why in si.skipped.get(rid, []):
        excluded.append({"ref": o.obs_id, "evidence_id": o.file_id, "reason": why})
    for r in si.readings.get(rid, []):
        if r.file_captured_at > audit:
            excluded.append({"ref": r.ref, "evidence_id": r.evidence_id, "reason": "FILE_CAPTURED_AFTER_AUDIT"})
        elif (r.record_time and r.record_time > audit) or r.time > audit:
            excluded.append({"ref": r.ref, "evidence_id": r.evidence_id, "reason": "RECORD_AFTER_AUDIT"})
        elif subject and (r.users or r.unregistered) and subject not in r.users:
            excluded.append({"ref": r.ref, "evidence_id": r.evidence_id, "reason": "OTHER_SUBJECT_INSTANCE"})
        elif r.inconsistent:
            inconsistent.append(r)
        else:
            usable.append(r)
    # R4 scoped absence, only from captures that concern this requester
    for s in si.scopes:
        if rid in s["listed"] or s["captured"] > audit or s["time"] > audit or not si.covers(s, rid):
            continue
        weak = False
        if s.get("pit"):
            if subject not in si.file_users.get(s["file_id"], set()):
                continue  # R4e: only a snapshot that concerns this requester (as R4a)
            why = "point-in-time snapshot about the requester; missing from every copy of the table"
        elif s["empty"]:
            fu = si.file_users.get(s["file_id"], set())
            if not ((subject in fu) if fu else (lo < s["time"] <= audit)):
                continue  # an empty extract about other people says nothing about this requester
            weak = not fu
            why = "empty scoped snapshot" + (" naming no one (weak)" if weak else "")
        else:
            why = "listed in a sibling copy of the same dump but not in this copy"
        usable.append(Reading(s["ref"], s["file_id"], rid, "scoped_absence", s["copy"], s["time"], None, s["captured"],
                              {"existence": "ABSENT"}, [why], (), False, {"scope": s["ref"]}, weak=weak))
    claims = sorted((c for c in si.claims.get(rid, []) if c.time <= audit), key=lambda c: (c.time, c.activity_id))
    aspects = {}
    reasons = []
    for aspect in ASPECTS:
        st = _resolve(aspect, [r for r in usable if aspect in r.facts], [r for r in inconsistent if aspect in r.facts],
                      [c for c in claims if c.aspect == aspect], requested)
        aspects[aspect] = st
    ex = aspects["existence"]
    if ex.value == "UNKNOWN" and not ex.reasons:  # one primary reason per unknown
        ex.reasons.append(_unknown_reason(si, rid, excluded, inconsistent))
    if any(e["reason"] in ("FILE_CAPTURED_AFTER_AUDIT", "RECORD_AFTER_AUDIT") for e in excluded):
        reasons.append("POST_AUDIT_EVIDENCE_EXCLUDED")
    for a, st in aspects.items():
        reasons += [f"{a.upper()}_{x}" for x in st.reasons if x not in reasons]
    expo = aspects["exposure"].value
    summary = {"ABSENT": "ABSENT", "CONFLICTING": "CONFLICTING", "UNKNOWN": "UNKNOWN"}.get(ex.value) or \
        ("RESIDUAL" if expo in ("REDACTED", "ANONYMIZED") else "PRESENT")
    ambiguity = []
    if association["attribution"] not in ("EVIDENCE_LINKED", "ACTIVITY_MATCH_HIGH"):
        ambiguity.append(f"ATTRIBUTION_{association['attribution']}")
    ambiguity += [f"{a.upper()}_CONFLICTING" for a, st in aspects.items() if st.value == "CONFLICTING"]
    if len(set(ex.per_copy.values()) - {"UNKNOWN"}) > 1:  # a historical (UNKNOWN) copy is not drift
        ambiguity.append("COPIES_DISAGREE")
    ambiguity += [f"UNRESOLVED_IDENTIFIER:{u}" for u in unresolved]
    system, basis = si.system(rid)
    cats = sorted({o.record_type for o in si.observations_by_res.get(rid, []) if o.record_type} | si.collections.get(rid, set()))
    # T2 (M6.1): the same category inputs restricted to captures admissible at audit_at (provenance only)
    cats_admissible = sorted({o.record_type for o in si.observations_by_res.get(rid, []) if o.record_type and o.file_captured_at <= audit}
                             | {c for c, t in si.collection_captures.get(rid, ()) if t and t <= audit})
    return Assessment(req.case_id, req.requested_at, req.reported_at, req.audit_at, rid, system, basis, cats, association,
                      {a: asdict(s) for a, s in aspects.items()}, summary, excluded, reasons, ambiguity, cats_admissible)


def _unknown_reason(si, rid, excluded, inconsistent):
    if inconsistent:
        return "ONLY_INCONSISTENT_TIMESTAMPS"
    if any(e["reason"] in ("FILE_CAPTURED_AFTER_AUDIT", "RECORD_AFTER_AUDIT") for e in excluded):
        return "POST_AUDIT_EVIDENCE_ONLY"
    if si.readings.get(rid) or si.skipped.get(rid):
        return "NO_ADMISSIBLE_STATE_OBSERVATION"
    if rid in si.event_resources:
        return "ACTIVITY_ONLY"
    return "NO_EVIDENCE"


def _resolve(aspect, readings, inconsistent, claims, requested=None):
    st = AspectState()
    weak = [r for r in readings if r.weak]
    readings = [r for r in readings if not r.weak]
    if weak and not readings:
        st.reasons.append("WEAK_ABSENCE_ONLY")
        st.supporting += [_ref(r, note="absence in a capture that names no one; not attributable, context only") for r in weak]
    if not readings:
        if inconsistent:
            st.contradicting += [_ref(r, note="TIMESTAMP_INCONSISTENT: cannot establish state alone") for r in inconsistent]
        st.supporting += [_claim_ref(c, note="claim only; no state evidence") for c in claims]
        return st
    by_copy = defaultdict(list)
    for r in readings:
        by_copy[r.copy].append(r)
    deciding, historical = [], False
    for copy, rs in sorted(by_copy.items()):
        latest = max(r.time for r in rs)
        if requested and latest < requested:  # T1: this copy was last read before the request
            st.per_copy[copy] = "UNKNOWN"
            historical = True
            st.supporting += [_ref(r, note="HISTORICAL: read before requested_at; proves earlier state only") for r in rs]
            continue
        top = [r for r in rs if r.time == latest]
        values = {r.facts[aspect] for r in top}
        st.per_copy[copy] = values.pop() if len(values) == 1 else "CONFLICTING"
        deciding += top
        for r in rs:
            if r.time < latest:  # superseded history of the same copy
                (st.supporting if r.facts[aspect] == st.per_copy[copy] else st.contradicting).append(_ref(r, note="earlier reading of this copy"))
    vals = set(st.per_copy.values()) - {"UNKNOWN"}
    if historical and not ((aspect == "existence" and "PRESENT" in vals) or (aspect == "exposure" and "IDENTIFIABLE" in vals)):
        # T1: a historical copy may hide the current state; only a current PRESENT/IDENTIFIABLE copy (R2) settles it
        st.reasons.append("HISTORICAL_READINGS_ONLY" if not deciding else "HISTORICAL_COPY_UNRESOLVED")
        st.supporting += [_ref(r, note="current reading; another copy has only historical readings") for r in deciding]
        st.supporting += [_claim_ref(c, note="claim only; no current state evidence") for c in claims]
        st.contradicting += [_ref(r, note="TIMESTAMP_INCONSISTENT: cannot establish state alone") for r in inconsistent]
        st.supporting += [_ref(r, note="absence in a capture that names no one; context only") for r in weak]
        return st
    if aspect == "existence" and "PRESENT" in vals:
        st.value = "PRESENT"
    elif aspect == "exposure" and "IDENTIFIABLE" in vals:
        st.value = "IDENTIFIABLE"
    elif "CONFLICTING" in vals or len(vals) > 1:
        st.value = "CONFLICTING"
    else:
        st.value = vals.pop()
    if st.value == "CONFLICTING":
        st.reasons.append("CONFLICTING_READINGS")
    if aspect == "existence" and len(set(st.per_copy.values()) - {"UNKNOWN"}) > 1 and st.value == "PRESENT":
        st.reasons.append("REPLICA_DRIFT")
    for r in deciding:
        target = st.supporting if r.facts[aspect] == st.value else st.contradicting
        if r.facts[aspect] == st.value:
            st.deciding.append(_ref(r))
        else:
            target.append(_ref(r, note="deciding-time reading with a different value"))
    st.basis = "INFERRED" if all(r.kind == "scoped_absence" for r in deciding if r.facts[aspect] == st.value) else "OBSERVED"
    last = max(r.time for r in deciding)
    for c in claims:  # claims never set state; they corroborate or mark it stale
        if c.time > last and c.effective and c.value != st.value:
            st.contradicting.append(_claim_ref(c, note="later operation/recreation claim; state may have changed"))
            if st.basis in ("OBSERVED", "INFERRED"):  # a scoped absence can be recreated too
                st.basis = "OBSERVED_STALE"
            if "LATER_CLAIM" not in st.reasons:
                st.reasons.append("LATER_CLAIM")
        elif c.effective and c.value == st.value:
            st.supporting.append(_claim_ref(c, note="claim consistent with the observed state"))
        elif c.effective:
            st.contradicting.append(_claim_ref(c, note="earlier claim not reflected in the later observed state"))
        else:
            st.supporting.append(_claim_ref(c, note="failed operation; implies no change"))
    for r in inconsistent:
        (st.supporting if r.facts[aspect] == st.value else st.contradicting).append(_ref(r, note="TIMESTAMP_INCONSISTENT"))
    for r in weak:
        (st.supporting if r.facts[aspect] == st.value else st.contradicting).append(
            _ref(r, note="absence in a capture that names no one; context only"))
    return st


# ------------------------------------------------------------------ requests -> resources

_ts_memo = lru_cache(maxsize=None)(_ts)  # link timestamps repeat across requests


def associations(req, decision, links, resources):
    """-> ({resource: association}, {resource: unresolved identifiers}) for one deletion request."""
    out = {}
    if not req.user_ids:
        return out, {}
    subject = req.user_ids[0]
    lo, hi = _ts(req.requested_at) - CONTEMPORANEOUS, _ts(req.audit_at) + CONTEMPORANEOUS
    audit = _ts(req.audit_at)
    # A10 (M6.1): direct links of anyone else, dated in [requested_at - 60d, audit_at] and from a
    # capture admissible at audit_at. Links in an observation that also names the requester are
    # co-subjects (a shared record), not rival owners.
    with_subject = {l.obs_id for l in links if l.user_id == subject}
    rivals = defaultdict(list)
    for l in links:
        t = l.observed_at or l.file_captured_at
        if not (l.direct and l.user_id != subject and l.obs_id not in with_subject and t):
            continue
        if lo <= _ts_memo(t) <= audit and _ts_memo(l.file_captured_at or t) <= audit:
            rivals[l.resource_id].append(l)

    def conflicting(rid):
        """Registered users only: an unresolved identifier is not proof of another owner."""
        return sorted({l.user_id for l in rivals.get(rid, ()) if l.user_id})
    mine = defaultdict(list)
    for l in links:
        if l.user_id == subject:
            mine[l.resource_id].append(l)
    for rid, ls in sorted(mine.items()):
        direct = [l for l in ls if l.direct]
        if direct:
            near = [l for l in direct if lo <= _ts(l.observed_at or l.file_captured_at) <= hi]
            attribution = "EVIDENCE_LINKED" if near else "HISTORICAL_LINK"
        else:
            attribution = "CANDIDATE_LINK"
        out[rid] = {"attribution": attribution, "source": "M2", "attributed_to_requester": attribution == "EVIDENCE_LINKED",
                    "link_evidence": sorted({l.evidence_id for l in ls}),
                    "competing_owners": sorted(o for o in resources[rid]["owners"] if o != subject and resources[rid]["owners"][o]["direct"]),
                    "conflicting_owners": conflicting(rid)}
    if decision is not None and decision.status == "MATCHED":
        att = {"HIGH": "ACTIVITY_MATCH_HIGH", "MEDIUM": "ACTIVITY_MATCH_MEDIUM", "LOW": "ACTIVITY_MATCH_LOW"}[decision.confidence]
        entry = out.setdefault(decision.resource_id, {"source": "M3", "link_evidence": [], "competing_owners": [],
                                                      "conflicting_owners": conflicting(decision.resource_id)})
        if entry.get("attribution") != "EVIDENCE_LINKED":
            entry["attribution"] = att
            entry["attributed_to_requester"] = decision.confidence in ("HIGH", "MEDIUM")
        entry["m3"] = {"confidence": decision.confidence, "reason": decision.reason, "activity_id": decision.activity_id}
    elif decision is not None:
        for c in decision.considered:
            if c.feasible and c.resource_id not in out:
                out[c.resource_id] = {"attribution": "UNATTRIBUTED_ALTERNATIVE", "source": "M3", "attributed_to_requester": False,
                                      "link_evidence": [], "competing_owners": [], "conflicting_owners": [],
                                      "m3": {"confidence": "UNMATCHED", "reason": decision.reason, "activity_id": c.activity_id}}
    unresolved = {rid: sorted({f"{l.identity_kind}:{l.identity_value}" for l in rivals.get(rid, ()) if not l.user_id}) for rid in out}
    return out, unresolved


def reconstruct(requests, decisions, observations, links, resources, events):
    """-> list of Assessment for every deletion request's associated resources (deterministic order)."""
    si = StateIndex(observations, links, events)
    out = []
    for req in requests:
        if req.family != "account_deletion":
            continue
        subject = req.user_ids[0] if req.user_ids else None
        assoc, unresolved = associations(req, decisions.get(req.case_id), links, resources)
        for rid, a in sorted(assoc.items()):
            out.append(assess_resource(si, req, rid, subject, a, unresolved.get(rid, ())))
    return out
