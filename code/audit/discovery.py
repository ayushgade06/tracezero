"""Evidence-linked resource discovery: identity keys in M1 observations -> resource links.

A Link says "this evidence ties resource R to identity key K (and so to user U)".
Link types:
  direct     catalog   explicit data-catalog statement (system_activity LINK_RESOURCE_IDENTITY)
             record    key and resource in the same row / JSON record / TXT line / log line
             trace     key on another line of the same log trace, and the trace names
                       exactly one resource
  candidate  trace_multi      as "trace", but the trace names several resources
             related_mention  key in the record of resource A that mentions resource B
             page             key and resource on the same PDF page (no explicit pairing)
Keys that match no known identifier (decoys, unregistered people) produce links with
user_id=None so that competing ownership stays visible. Nothing here decides deletion
state, filters by audit time, or forces one account per request.
"""
import csv
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from audit.observation import EMAIL_RE, RESOURCE_RE, USER_RE

DIRECT = ("catalog", "record", "trace")
CANDIDATE = ("trace_multi", "related_mention", "page")


@dataclass
class Link:
    resource_id: str
    user_id: str            # None: the key matches no known identifier
    link_type: str
    identity_kind: str
    identity_value: str     # normalized
    identity_field: str     # field in the evidence that holds the key ("trace:L2.lookup_key", ...)
    identity_source: str    # identity-table provenance of the key, None if unregistered
    key_status: str         # valid / superseded / not_yet_valid / unknown at observed time
    key_current: bool
    evidence_id: str        # file_id or activity_id (citation id)
    obs_id: str
    locator: dict
    system: str
    observed_at: str        # record-level time, None if the evidence has none
    file_captured_at: str   # manifest capture time (None for activity rows)
    excerpt: str

    @property
    def direct(self):
        return self.link_type in DIRECT

    def to_dict(self):
        return asdict(self)


def _excerpt(o):
    if o.raw:
        return o.raw[:300]
    return json.dumps(o.attrs, sort_keys=True, default=str)[:300]


def _links_for_key(index, rid, link_type, key, o_like):
    ids = index.lookup(key["kind"], key["value"])
    when = o_like["observed_at"] or o_like["file_captured_at"]
    base = dict(resource_id=rid, link_type=link_type, identity_kind=key["kind"], identity_value=key["value"],
                identity_field=key["field"], evidence_id=o_like["evidence_id"], obs_id=o_like["obs_id"],
                locator=o_like["locator"], system=o_like["system"], observed_at=o_like["observed_at"],
                file_captured_at=o_like["file_captured_at"], excerpt=o_like["excerpt"])
    if not ids:
        return [Link(user_id=None, identity_source=None, key_status="unknown", key_current=False, **base)]
    return [Link(user_id=i.user_id, identity_source=i.source, key_status=i.status_at(when), key_current=i.current, **base)
            for i in ids]


def load_catalog_links(dataset):
    """Explicit LINK_RESOURCE_IDENTITY rows from system_activity.csv (other activity is M3)."""
    with open(Path(dataset) / "system_activity.csv", encoding="utf-8", newline="") as f:
        return [r for r in csv.DictReader(f) if r["event_type"] == "LINK_RESOURCE_IDENTITY"]


def build_links(observations, index, catalog_rows=()):
    trace_resources = defaultdict(set)
    for o in observations:
        if o.file_format == "log" and o.locator.get("trace_id") and o.resource_id:
            trace_resources[(o.file_id, o.locator["trace_id"])].add(o.resource_id)
    links = []
    for o in observations:
        ctx = {"evidence_id": o.file_id, "obs_id": o.obs_id, "locator": o.locator, "system": o.system,
               "observed_at": o.observed_at, "file_captured_at": o.file_captured_at, "excerpt": _excerpt(o)}
        if o.kind == "page":
            for rid in o.related_resource_ids:
                for k in o.identity_keys:
                    links += _links_for_key(index, rid, "page", k, ctx)
            continue
        if not o.resource_id:
            continue
        multi = len(trace_resources.get((o.file_id, o.locator.get("trace_id")), ())) > 1
        for k in o.identity_keys:
            lt = ("trace_multi" if multi else "trace") if k["field"].startswith("trace:") else "record"
            links += _links_for_key(index, o.resource_id, lt, k, ctx)
            for rid in o.related_resource_ids:
                links += _links_for_key(index, rid, "related_mention", k, dict(ctx, system=None))
    for r in catalog_rows:
        keys = [{"kind": "email", "value": e.lower(), "field": "details"} for e in dict.fromkeys(EMAIL_RE.findall(r["details"]))]
        keys += [{"kind": "user_id", "value": u, "field": "details"} for u in dict.fromkeys(USER_RE.findall(r["details"]))]
        ctx = {"evidence_id": r["activity_id"], "obs_id": r["activity_id"], "locator": {"activity_id": r["activity_id"]},
               "system": r["target_system"], "observed_at": r["event_time"].replace("+00:00", "Z"),
               "file_captured_at": None, "excerpt": r["details"][:300]}
        if RESOURCE_RE.fullmatch(r["resource_id"]):
            for k in keys:
                links += _links_for_key(index, r["resource_id"], "catalog", k, ctx)
    links.sort(key=lambda l: (l.resource_id, l.user_id or "", l.link_type, l.evidence_id, l.obs_id, l.identity_field))
    return links


def _owner(link):
    return link.user_id or f"unregistered:{link.identity_kind}:{link.identity_value}"


def _span(links):
    times = [l.observed_at or l.file_captured_at for l in links if l.observed_at or l.file_captured_at]
    return (min(times), max(times)) if times else (None, None)


def classify_resources(observations, links):
    """resource_id -> {"status", "owners": {owner: {"direct", "candidate", "span"}}, "systems", "formats"}.
    status: owned (direct links to exactly one known user), conflicting (direct links to several
    owners, incl. unregistered identities), unregistered_only, candidate_only, unlinked."""
    seen = defaultdict(lambda: {"systems": set(), "formats": set()})
    for o in observations:
        for rid in ([o.resource_id] if o.resource_id else []) + list(o.related_resource_ids):
            seen[rid]["formats"].add(o.file_format)
            if o.system and rid == o.resource_id:
                seen[rid]["systems"].add(o.system)
    by_res = defaultdict(lambda: defaultdict(list))
    for l in links:
        by_res[l.resource_id][_owner(l)].append(l)
        seen[l.resource_id]
    out = {}
    for rid in sorted(seen):
        owners = {}
        for owner, ls in sorted(by_res.get(rid, {}).items()):
            d = [l for l in ls if l.direct]
            owners[owner] = {"direct": len(d), "candidate": len(ls) - len(d), "span": _span(d or ls)}
        direct_owners = [o for o, v in owners.items() if v["direct"]]
        known_direct = [o for o in direct_owners if not o.startswith("unregistered:")]
        # Several unregistered keys (e.g. email + phone of one decoy row) are one unknown party.
        unregistered = len(direct_owners) > len(known_direct)
        if len(known_direct) > 1:
            spans = sorted(owners[o]["span"] for o in known_direct)
            overlap = any(a[1] and b[0] and b[0] <= a[1] for a, b in zip(spans, spans[1:]))
            status, detail = "conflicting", "overlapping_periods" if overlap else "disjoint_periods"
        elif known_direct and unregistered:
            status, detail = "conflicting", "known_and_unregistered_keys"
        elif known_direct:
            status, detail = "owned", None
        elif unregistered:
            status, detail = "unregistered_only", None
        elif owners:
            status, detail = "candidate_only", None
        else:
            status, detail = "unlinked", None
        out[rid] = {"status": status, "conflict": detail, "owners": owners,
                    "systems": sorted(seen[rid]["systems"]), "formats": sorted(seen[rid]["formats"])}
    return out


def discover(user_id, links, resources):
    """Resources linked to one subject, strongest evidence first. Each entry keeps every link
    (direct and candidate) and the resource-wide ownership status, so competing owners stay
    visible to later stages."""
    mine = defaultdict(list)
    for l in links:
        if l.user_id == user_id:
            mine[l.resource_id].append(l)
    found = []
    for rid, ls in sorted(mine.items()):
        r = resources[rid]
        found.append({
            "resource_id": rid,
            "strength": "direct" if any(l.direct for l in ls) else "candidate",
            "resource_status": r["status"],
            "competing_owners": sorted(o for o in r["owners"] if o != user_id and r["owners"][o]["direct"]),
            "systems": r["systems"],
            "links": ls,
        })
    return found
