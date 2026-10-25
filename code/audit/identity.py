"""Identity model: every known identifier of every subject, with provenance and validity window.

Sources: dataset/user_identities.csv (current snapshot) and dataset/user_audit_log.csv
(historical changes: old_value valid until event_time, new_value valid from event_time).

Normalization (justified by the data, see the M2 report):
  email       lowercased: identity tables store lowercase, while requests and evidence also
              use upper-case variants of the same address
  email_hash  lowercased hex (sha256 of the lowercased email; never recomputed here)
  phone       exact string: evidence phones match the identity tables verbatim
  user_id     exact string
Matching is exact on the normalized value. Names are kept for display only and are never
matched; there is no fuzzy, partial or similar-looking matching.
"""
import csv
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("email", "email_hash", "phone", "user_id")


def normalize(kind, value):
    if value is None:
        return None
    v = str(value).strip()
    return v.lower() if kind in ("email", "email_hash") else v


@dataclass(frozen=True)
class Identifier:
    user_id: str
    kind: str
    value: str          # normalized
    original: str       # as written in the source file
    source: str         # e.g. "user_identities.csv:user_07" or "user_audit_log.csv:audit_60:old_value"
    current: bool
    valid_from: str = None   # None = unknown (before the first recorded change)
    valid_to: str = None     # None = still valid at the snapshot

    def status_at(self, ts):
        """'valid', 'superseded', 'not_yet_valid' or 'unknown' for an ISO timestamp."""
        if not ts:
            return "unknown"
        if self.valid_to and ts >= self.valid_to:
            return "superseded"
        if self.valid_from and ts < self.valid_from:
            return "not_yet_valid"
        return "valid"


@dataclass
class Subject:
    user_id: str
    name: str
    snapshot_updated_at: str
    identifiers: list = field(default_factory=list)


class IdentityIndex:
    """(kind, normalized value) -> list of Identifier. Lists keep any ambiguity explicit."""

    def __init__(self, subjects):
        self.subjects = subjects
        self._by_key = {}
        for s in subjects.values():
            for i in s.identifiers:
                self._by_key.setdefault((i.kind, i.value), []).append(i)

    def lookup(self, kind, value):
        return list(self._by_key.get((kind, normalize(kind, value)), []))

    def users(self, kind, value):
        return sorted({i.user_id for i in self.lookup(kind, value)})

    def ambiguous_keys(self):
        return {k: v for k, v in self._by_key.items() if len({i.user_id for i in v}) > 1}


def load_identities(dataset):
    dataset = Path(dataset)
    with open(dataset / "user_identities.csv", encoding="utf-8", newline="") as f:
        current = list(csv.DictReader(f))
    with open(dataset / "user_audit_log.csv", encoding="utf-8", newline="") as f:
        changes = sorted(csv.DictReader(f), key=lambda a: (a["event_time"], a["audit_id"]))
    subjects = {r["user_id"]: Subject(r["user_id"], r["name"], r["updated_at"]) for r in current}
    last_change = {}  # (user, kind) -> event_time of the latest change (start of current validity)
    for a in changes:
        s = subjects.get(a["user_id"])
        if s is None:
            s = subjects[a["user_id"]] = Subject(a["user_id"], None, None)
        kind = a["field_name"]
        prev = last_change.get((a["user_id"], kind))
        s.identifiers.append(Identifier(a["user_id"], kind, normalize(kind, a["old_value"]), a["old_value"],
                                        f"user_audit_log.csv:{a['audit_id']}:old_value", False,
                                        valid_from=prev, valid_to=a["event_time"]))
        last_change[(a["user_id"], kind)] = a["event_time"]
    for r in current:
        s = subjects[r["user_id"]]
        s.identifiers.append(Identifier(r["user_id"], "user_id", r["user_id"], r["user_id"],
                                        f"user_identities.csv:{r['user_id']}", True))
        for kind in ("email", "email_hash", "phone"):
            if r[kind]:
                s.identifiers.append(Identifier(r["user_id"], kind, normalize(kind, r[kind]), r[kind],
                                                f"user_identities.csv:{r['user_id']}", True,
                                                valid_from=last_change.get((r["user_id"], kind))))
    # A new_value is normally the next change's old_value or the current snapshot value. If the
    # tables disagree, keep it as its own historical identifier instead of dropping it.
    for a in changes:
        s, kind = subjects[a["user_id"]], a["field_name"]
        value = normalize(kind, a["new_value"])
        if not any(i.kind == kind and i.value == value for i in s.identifiers):
            s.identifiers.append(Identifier(a["user_id"], kind, value, a["new_value"],
                                            f"user_audit_log.csv:{a['audit_id']}:new_value", False,
                                            valid_from=a["event_time"]))
    return IdentityIndex(subjects)


def resolve_request(index, email):
    """Resolve a request email. Returns dict(status, user_ids, identifiers).
    status: 'current', 'historical', 'current_and_historical' (same user, value both past and
    present), 'ambiguous' (several users) or 'unresolved'."""
    ids = index.lookup("email", email)
    users = sorted({i.user_id for i in ids})
    if not ids:
        status = "unresolved"
    elif len(users) > 1:
        status = "ambiguous"
    elif all(i.current for i in ids):
        status = "current"
    elif not any(i.current for i in ids):
        status = "historical"
    else:
        status = "current_and_historical"
    return {"status": status, "user_ids": users, "identifiers": ids,
            "case_normalized": any(i.original != str(email).strip() for i in ids)}
