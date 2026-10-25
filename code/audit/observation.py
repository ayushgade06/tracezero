"""Common observation schema and value extraction shared by the evidence parsers.

One Observation = one statement made by one evidence file about (at most) one resource,
with provenance back to the exact table/row, JSON path, log line(s) or PDF page.
Parsers never merge, de-duplicate or reconcile observations; later stages do that.

Fields (all JSON-serializable; None means "not stated by the evidence"):
  obs_id            stable unique id: "<file_id>:<locator>"
  file_id           manifest evidence id (the citation id); never a filesystem path
  file_path, file_format, file_origin
  file_captured_at  manifest captured_at, normalized to ISO-8601 UTC
  kind              "record" (one row/record/line), "scope" (a table/collection/listing that
                    was captured, with its row count), or "page" (unstructured PDF page)
  locator           format-specific provenance, e.g. {"table","row"}, {"json_path"},
                    {"line","trace_id","trace_lines"}, {"page"}
  context           inherited container metadata (snapshot data_store/collection/captured_at,
                    SQL header comments, ...)
  resource_id       only when the evidence names it in a resource field
  system, system_source        explicit system name and the field it came from
  record_type       explicit record_type/category field only
  state, state_field           explicit result/status value (raw string) and its field
  operation         log event name, when present
  observed_at, observed_at_field   record-level timestamp and its source field
  identity_keys     [{"kind": email|user_id|email_hash|phone, "value", "field"}]; emails and
                    hashes lowercased; only values literally present in the evidence
  related_resource_ids  other resource ids mentioned by the record (relationship unknown)
  attrs             original fields of the record (or parsed log key/values)
  raw               original text for line/page formats
"""
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

# Resource id prefixes, from a survey of every *_<digits> token in the evidence (M1 report).
# object_/marketing_ only appear in screenshots and the activity catalog.
RESOURCE_PREFIXES = (
    "audit_record", "data_flow", "legal_hold", "account", "search", "analytics", "support",
    "export", "backup", "tombstone", "object", "marketing",
)
RESOURCE_RE = re.compile(r"\b(?:%s)_\d+\b" % "|".join(RESOURCE_PREFIXES))
ID_TOKEN_RE = re.compile(r"\b[a-z][a-z_]*_\d+\b")  # any id-like token, for the coverage audit
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
USER_RE = re.compile(r"\buser_\d+\b")
HASH_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")
PHONE_TEXT_RE = re.compile(r"\+\d[\d ()-]{6,}\d")  # only used for unstructured text
# 64-hex values in these fields are content digests (e.g. integrity_seal "sha256:..."), not email hashes.
NOT_IDENTITY_HASH_FIELD = re.compile(r"seal|checksum|digest|fingerprint|signature")

STATE_FIELDS = ("result", "state", "status", "account_status", "status_code", "http_status")
TIME_FIELDS = ("observed_at", "recorded_at", "updated_at", "indexed_at", "captured_at")
SYSTEM_FIELDS = ("system", "origin_system", "data_store", "target_system")
NULLS = (None, "", "null", "NULL", "None")


@dataclass
class Observation:
    obs_id: str
    file_id: str
    file_path: str
    file_format: str
    file_origin: str
    file_captured_at: str
    kind: str
    locator: dict
    context: dict = field(default_factory=dict)
    resource_id: str = None
    system: str = None
    system_source: str = None
    record_type: str = None
    state: str = None
    state_field: str = None
    operation: str = None
    observed_at: str = None
    observed_at_field: str = None
    identity_keys: list = field(default_factory=list)
    related_resource_ids: list = field(default_factory=list)
    attrs: dict = field(default_factory=dict)
    raw: str = None

    def to_dict(self):
        return asdict(self)


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_TEXT_TS = re.compile(r"(\d{1,2}) ([A-Za-z]{3}) (\d{4})(?:\s+(\d{1,2})\s*[:\n]\s*(\d{2})\s*UTC)?")


def parse_ts(value):
    """Normalize a timestamp to 'YYYY-MM-DDTHH:MM:SSZ'. Returns None when unparseable.

    Accepts ISO-8601 with Z/offset/none (naive values are taken as UTC), a space instead of
    'T', and document dates like '18 Jun 2026 10:24 UTC' (also split as '10\\n24 UTC').
    """
    if value in NULLS or not isinstance(value, str):
        return None
    s = value.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        m = _TEXT_TS.fullmatch(s)
        if not m or m.group(2).lower() not in _MONTHS:
            return None
        day, mon, year, hh, mm = m.groups()
        dt = datetime(int(year), _MONTHS[mon.lower()], int(day), int(hh or 0), int(mm or 0))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def leaves(obj, prefix=""):
    """Yield (dotted_field, scalar) for every scalar inside a dict/list tree."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from leaves(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from leaves(v, f"{prefix}[{i}]")
    else:
        yield prefix, obj


def identity_keys(attrs, text_fields=False):
    """Identity keys literally present in a record. Phones are taken only from *phone* fields
    unless text_fields=True (free text such as PDF pages)."""
    keys, seen = [], set()

    def add(kind, value, fld):
        k = (kind, value)
        if k not in seen:
            seen.add(k)
            keys.append({"kind": kind, "value": value, "field": fld})

    for fld, v in leaves(attrs):
        if v in NULLS or isinstance(v, bool):
            continue
        s = str(v)
        name = fld.rsplit(".", 1)[-1]
        for e in EMAIL_RE.findall(s):
            add("email", e.lower(), fld)
        for u in USER_RE.findall(s):
            add("user_id", u, fld)
        if not NOT_IDENTITY_HASH_FIELD.search(name):
            for h in HASH_RE.findall(s):
                add("email_hash", h.lower(), fld)
        if "phone" in name and s != "[REDACTED]":
            add("phone", s, fld)
        elif text_fields:
            for p in PHONE_TEXT_RE.findall(s):
                add("phone", p, fld)
    return keys


def resource_ids_in(attrs, exclude=None):
    found = []
    for _, v in leaves(attrs):
        if isinstance(v, str):
            for r in RESOURCE_RE.findall(v):
                if r != exclude and r not in found:
                    found.append(r)
    return found


def first_field(attrs, names):
    """(value, field) of the first non-null top-level field among names."""
    for n in names:
        v = attrs.get(n)
        if v not in NULLS:
            return v, n
    return None, None


def fill_from_attrs(obs, attrs, *, prefix=""):
    """Populate the explicit-field parts of an observation from a record's attributes.
    Only fields that are literally present are used; nothing is guessed."""
    if obs.resource_id is None:
        rid, _ = first_field(attrs, ("resource_id",))
        if isinstance(rid, str) and RESOURCE_RE.fullmatch(rid):
            obs.resource_id = rid
    if obs.system is None:
        v, f = first_field(attrs, SYSTEM_FIELDS)
        if v is not None:
            obs.system, obs.system_source = str(v), prefix + f
    if obs.record_type is None:
        v, f = first_field(attrs, ("record_type", "category"))
        obs.record_type = None if v is None else str(v)
    if obs.state is None:
        v, f = first_field(attrs, STATE_FIELDS)
        if v is not None:
            obs.state, obs.state_field = str(v), prefix + f
    if obs.observed_at is None:
        v, f = first_field(attrs, TIME_FIELDS)
        ts = parse_ts(v) if isinstance(v, str) else None
        if ts:
            obs.observed_at, obs.observed_at_field = ts, prefix + f
    have = {(k["kind"], k["value"]) for k in obs.identity_keys}
    obs.identity_keys += [k for k in identity_keys(attrs) if (k["kind"], k["value"]) not in have]
    obs.related_resource_ids += [r for r in resource_ids_in(attrs, obs.resource_id) if r not in obs.related_resource_ids]
    return obs
