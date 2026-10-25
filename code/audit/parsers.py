"""Format parsers: evidence file text -> (observations, diagnostics).

Each parser takes the manifest metadata of one file plus its content and returns
Observation objects (see observation.py) and diagnostics. A diagnostic is a dict
{"file_id", "locator", "severity", "reason", "excerpt"} for anything that was not
understood; nothing is dropped silently.
"""
import json
import re
import sqlite3
from collections import defaultdict

from audit.observation import (
    NULLS, RESOURCE_RE, Observation, fill_from_attrs, identity_keys, parse_ts, resource_ids_in,
)


def _obs(meta, kind, loc_id, locator, **kw):
    return Observation(
        obs_id=f"{meta['file_id']}:{loc_id}", file_id=meta["file_id"], file_path=meta["file_path"],
        file_format=meta["format"], file_origin=meta["origin"], file_captured_at=meta["captured_at"],
        kind=kind, locator=locator, **kw,
    )


def _diag(meta, locator, reason, excerpt="", severity="warning"):
    return {"file_id": meta["file_id"], "locator": locator, "severity": severity,
            "reason": reason, "excerpt": str(excerpt)[:240]}


def _snake(s):
    return re.sub(r"[^a-z0-9]+", "_", s.strip().lower()).strip("_")


# ---------------------------------------------------------------- SQL (never executed)

_SQL_IGNORED = re.compile(r"(?is)^(BEGIN|COMMIT|END|PRAGMA|CREATE\s+(UNIQUE\s+)?INDEX)\b")
_SQL_CREATE = re.compile(r'(?is)^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"?(\w+)"?\s*\((.*)\)\s*;?$')
_SQL_INSERT = re.compile(r'(?is)^INSERT\s+INTO\s+"?(\w+)"?\s*(?:\(([^)]*)\))?\s*VALUES\s*(.*?)\s*;?$')
_SQL_NUMBER = re.compile(r"-?\d+(\.\d+)?([eE][-+]?\d+)?")
_SQL_CONSTRAINTS = {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"}


def _split_top_level(s):
    parts, depth, quote, cur = [], 0, None, []
    for ch in s:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def sql_values(s):
    """Parse literal tuples "('a', 1, NULL), (...)" without executing anything.
    Raises ValueError on anything that is not a plain literal."""
    rows, i, n = [], 0, len(s)

    def ws(i):
        while i < n and s[i] in " \t\r\n":
            i += 1
        return i

    while True:
        i = ws(i)
        if i >= n:
            return rows
        if s[i] != "(":
            raise ValueError(f"expected '(' at {i}")
        i, row = i + 1, []
        while True:
            i = ws(i)
            if s[i] == "'":
                j, buf = i + 1, []
                while True:
                    k = s.index("'", j)
                    buf.append(s[j:k])
                    if k + 1 < n and s[k + 1] == "'":
                        buf.append("'")
                        j = k + 2
                    else:
                        i = k + 1
                        break
                row.append("".join(buf))
            elif s[i:i + 4].upper() == "NULL":
                row.append(None)
                i += 4
            else:
                m = _SQL_NUMBER.match(s, i)
                if not m:
                    raise ValueError(f"unsupported literal at {i}: {s[i:i + 20]!r}")
                row.append(float(m.group()) if m.group(1) or m.group(2) else int(m.group()))
                i = m.end()
            i = ws(i)
            if s[i] == ",":
                i += 1
            elif s[i] == ")":
                i += 1
                break
            else:
                raise ValueError(f"unexpected {s[i]!r} at {i}")
        rows.append(row)
        i = ws(i)
        if i < n and s[i] == ",":
            i += 1


def parse_sql(meta, text):
    obs, diags, context = [], [], {}
    columns, rows, row_lines = {}, defaultdict(list), defaultdict(list)
    statements, buf, start = [], "", 0
    for lineno, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not buf and s.startswith("--"):
            m = re.match(r"--\s*([A-Za-z][A-Za-z _]{0,40}?)\s*[:=]\s*(\S.*)$", s)
            if m:
                context[_snake(m.group(1))] = m.group(2).strip()
            continue
        if not buf and not s:
            continue
        if not buf:
            start = lineno
        buf += line + "\n"
        if sqlite3.complete_statement(buf):  # only used to find statement boundaries
            statements.append((start, buf.strip()))
            buf = ""
    if buf.strip():
        diags.append(_diag(meta, {"line": start}, "incomplete SQL statement", buf))
    order = []
    for lineno, stmt in statements:
        if _SQL_IGNORED.match(stmt):
            continue
        m = _SQL_CREATE.match(stmt)
        if m:
            cols = []
            for part in _split_top_level(m.group(2)):
                name = part.split()[0].strip('"`[]')
                if name.upper() not in _SQL_CONSTRAINTS:
                    cols.append(name)
            columns[m.group(1)] = cols
            order.append(m.group(1))
            continue
        m = _SQL_INSERT.match(stmt)
        if m:
            table = m.group(1)
            cols = [c.strip().strip('"') for c in m.group(2).split(",")] if m.group(2) else columns.get(table)
            if cols is None:
                diags.append(_diag(meta, {"line": lineno}, f"INSERT into undeclared table {table}", stmt))
                continue
            try:
                values = sql_values(m.group(3))
            except (ValueError, IndexError) as e:
                diags.append(_diag(meta, {"line": lineno}, f"unparsed INSERT values: {e}", stmt))
                continue
            for v in values:
                if len(v) != len(cols):
                    diags.append(_diag(meta, {"line": lineno}, "INSERT column/value count mismatch", stmt))
                    continue
                rows[table].append(dict(zip(cols, v)))
                row_lines[table].append(lineno)
            if table not in order:
                order.append(table)
            continue
        diags.append(_diag(meta, {"line": lineno}, "unsupported SQL statement (not executed)", stmt))

    for table in order:
        tctx = dict(context, table=table)
        obs.append(_obs(meta, "scope", table, {"table": table}, context=tctx,
                        attrs={"row_count": len(rows[table]), "columns": columns.get(table)}))
        for i, (row, lineno) in enumerate(zip(rows[table], row_lines[table]), 1):
            attrs = dict(row)
            for k, v in row.items():  # replica tables keep the whole record as JSON text
                if isinstance(v, str) and v.startswith("{"):
                    try:
                        attrs[k] = json.loads(v)
                    except ValueError:
                        diags.append(_diag(meta, {"table": table, "row": i, "line": lineno}, f"column {k} is not valid JSON", v))
            kind = "scope" if table == "snapshot_scope" else "record"
            o = _obs(meta, kind, f"{table}[{i}]", {"table": table, "row": i, "line": lineno}, context=tctx, attrs=attrs)
            fill_from_attrs(o, attrs)
            for k, v in attrs.items():
                if isinstance(v, dict):
                    fill_from_attrs(o, v, prefix=f"{k}.")
            obs.append(o)
    return obs, diags


# ---------------------------------------------------------------- JSON

def _has_record(v):
    if isinstance(v, dict):
        return "resource_id" in v or any(_has_record(x) for x in v.values())
    if isinstance(v, list):
        return any(_has_record(x) for x in v)
    return False


def parse_json(meta, text):
    obs, diags = [], []
    try:
        data = json.loads(text)
    except ValueError as e:
        return [], [_diag(meta, {}, f"invalid JSON: {e}", text[:200], "error")]

    def walk(node, path, ctx, system_hint):
        if isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]", ctx, system_hint)
            return
        if not isinstance(node, dict):
            return
        scalars = {k: v for k, v in node.items() if not isinstance(v, (dict, list))}
        if "resource_id" in node:
            attrs = {k: (v if not _has_record(v) else "<nested records>") for k, v in node.items()}
            o = _obs(meta, "record", path, {"json_path": path}, context=dict(ctx), attrs=attrs)
            fill_from_attrs(o, attrs)
            if o.system is None:
                if ctx.get("data_store") not in NULLS:
                    o.system, o.system_source = ctx["data_store"], "context.data_store"
                elif system_hint:
                    o.system, o.system_source = system_hint, "json_path"
            if o.observed_at is None:
                for f in ("observed_at", "recorded_at", "captured_at"):
                    ts = parse_ts(ctx.get(f)) if isinstance(ctx.get(f), str) else None
                    if ts:
                        o.observed_at, o.observed_at_field = ts, f"context.{f}"
                        break
            obs.append(o)
            child_ctx = dict(ctx, **scalars, parent_resource_id=node["resource_id"])
        else:
            child_ctx = dict(ctx, **scalars)
            if isinstance(node.get("records"), list):
                o = _obs(meta, "scope", path or "$", {"json_path": path or "$"}, context=dict(ctx),
                         attrs=dict(scalars, record_count=len(node["records"])))
                fill_from_attrs(o, scalars)
                obs.append(o)
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                hint = k if path.endswith("service_exports") else system_hint  # service_exports.<system>.<listing>
                sub = f"{path}.{k}" if path else k
                if isinstance(v, list) and path.split(".")[-2:-1] == ["service_exports"]:
                    so = _obs(meta, "scope", sub, {"json_path": sub}, context=dict(child_ctx),
                              system=system_hint, system_source="json_path", attrs={"listing": k, "record_count": len(v)})
                    obs.append(so)
                walk(v, sub, child_ctx, hint)

    walk(data, "", {}, None)
    return obs, diags


# ---------------------------------------------------------------- logs

_LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)\s+(.*)$")
_LOG_BRACKET = re.compile(r"^\[(\w+)\] \[([^\]]+)\] \[([^\]]+)\] (\w+):\s*(.*)$")
_LOG_PIPE = re.compile(r"^\|\s*(\w+) \| (\S+) \| (\S+) \| (\w+) \|\s*(.*)$")
_LOG_SYSLOG = re.compile(r"^<\d+>\d+ (\S+) (\S+) (\S+) (\S+) (\S+)\s*(.*)$")
_KV = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S*)')


def _kv(s):
    out = {}
    for k, v in _KV.findall(s):
        if v.startswith('"'):
            v = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        out[k] = None if v in NULLS else v
    return out


def parse_log_line(line):
    """-> (timestamp_raw, fields) or None. fields carries normalized service/trace_id/event/level."""
    m = _LOG_LINE.match(line)
    if not m:
        return None
    ts, rest = m.groups()
    head = {}
    if rest.startswith("{"):
        try:
            fields = {k: (None if v in NULLS else v) for k, v in json.loads(rest).items()}
        except ValueError:
            return None
    elif _LOG_BRACKET.match(rest):
        level, svc, trace, event, kv = _LOG_BRACKET.match(rest).groups()
        head, fields = {"level": level, "service": svc, "trace_id": trace, "event": event}, _kv(kv)
    elif _LOG_PIPE.match(rest):
        level, svc, trace, event, kv = _LOG_PIPE.match(rest).groups()
        head, fields = {"level": level, "service": svc, "trace_id": trace, "event": event}, _kv(kv)
    elif _LOG_SYSLOG.match(rest):
        host, app, procid, _, _, kv = _LOG_SYSLOG.match(rest).groups()
        head, fields = {"host": host, "service": app, "trace_id": procid}, _kv(kv)
    else:
        fields = _kv(rest)
    if isinstance(fields.get("message"), str) and "=" in fields["message"]:
        fields.update(_kv(fields.pop("message")))  # cloud runtime export nests key/values
    if not fields and not head:
        return None
    norm = dict(head)
    norm.setdefault("service", fields.get("service") or fields.get("function") or fields.get("logger"))
    norm.setdefault("trace_id", fields.get("trace_id") or fields.get("requestId"))
    norm.setdefault("event", fields.get("event"))
    norm.setdefault("level", fields.get("level") or fields.get("severity"))
    for k, v in norm.items():
        if v not in NULLS:
            fields.setdefault(k, v)
    return ts, fields


def parse_log(meta, text):
    obs, diags = [], []
    parsed = []  # (lineno, line, ts, fields)
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        p = parse_log_line(line)
        if p is None:
            diags.append(_diag(meta, {"line": lineno}, "unparsed log line", line))
            continue
        parsed.append((lineno, line, p[0], p[1]))
    traces = defaultdict(list)
    for item in parsed:
        traces[item[3].get("trace_id") or f"line{item[0]}"].append(item)

    for key, items in traces.items():
        trace_id = items[0][3].get("trace_id")
        trace_lines = [i[0] for i in items]
        # identity keys / resource refs from every line of the trace, attributed to the line
        trace_ids, trace_refs = [], []
        for lineno, _, _, f in items:
            body = {k: v for k, v in f.items() if k not in ("trace_id", "requestId")}
            trace_ids += [dict(k, field=f"trace:L{lineno}.{k['field']}") for k in identity_keys(body)]
            trace_refs += resource_ids_in(body)
        resource_lines = [i for i in items if isinstance(i[3].get("resource_id"), str) and RESOURCE_RE.fullmatch(i[3]["resource_id"])]
        targets = resource_lines or ([items[-1]] if (trace_ids or trace_refs) else [])
        for lineno, line, ts, f in targets:
            o = _obs(meta, "record", f"L{lineno}", {"line": lineno, "trace_id": trace_id, "trace_lines": trace_lines},
                     attrs=dict(f), operation=f.get("event"),
                     raw=line if resource_lines else "\n".join(i[1] for i in items))
            fill_from_attrs(o, f)
            if o.system is None and f.get("service"):
                o.system, o.system_source = f["service"], "log.service"
            o.observed_at, o.observed_at_field = parse_ts(ts), "log.timestamp"
            seen = {(k["kind"], k["value"]) for k in o.identity_keys}
            for k in trace_ids:  # keys stated elsewhere in the same trace, e.g. lookup_key
                if (k["kind"], k["value"]) not in seen:
                    seen.add((k["kind"], k["value"]))
                    o.identity_keys.append(k)
            o.related_resource_ids += [r for r in dict.fromkeys(trace_refs) if r != o.resource_id and r not in o.related_resource_ids]
            obs.append(o)
    return obs, diags


# ---------------------------------------------------------------- TXT handover

_TXT_FIELDS = ("timestamp", "system", "resource_id", "record_type", "result", "subject")


def parse_txt(meta, text):
    obs, diags, ctx = [], [], {}
    for lineno, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        m = re.match(r"^([A-Za-z][A-Za-z ]{0,30}):\s*(\S.*)$", s)
        if m and "|" not in s:
            ctx[_snake(m.group(1))] = m.group(2)
            continue
        parts = [p.strip() for p in s.lstrip("-* ").split("|")] if "|" in s else []
        if len(parts) == len(_TXT_FIELDS):
            attrs = dict(zip(_TXT_FIELDS, parts))
            o = _obs(meta, "record", f"L{lineno}", {"line": lineno}, context=dict(ctx), attrs=attrs, raw=line)
            fill_from_attrs(o, attrs)
            o.record_type = attrs["record_type"]
            ts = parse_ts(attrs["timestamp"])
            if ts:
                o.observed_at, o.observed_at_field = ts, "timestamp"
            obs.append(o)
        elif RESOURCE_RE.search(s) or "|" in s:
            diags.append(_diag(meta, {"line": lineno}, "unparsed TXT line", line))
    return obs, diags


# ---------------------------------------------------------------- PDF pages

_PDF_DATE = re.compile(r"([A-Za-z][A-Za-z ]{0,24}?)\s+(\d{1,2} [A-Z][a-z]{2} \d{4}(?:\s+\d{1,2}\s*[:\n]\s*\d{2}\s*UTC)?)")


def parse_pdf_pages(meta, pages):
    """pages: list of extracted page texts (1-based page numbers in provenance)."""
    obs, diags = [], []
    for n, text in enumerate(pages, 1):
        if not text.strip():
            diags.append(_diag(meta, {"page": n}, "PDF page has no extractable text"))
            continue
        dates = [{"label": lbl.strip(), "raw": raw, "value": parse_ts(raw.replace("\n", ":"))}
                 for lbl, raw in _PDF_DATE.findall(text)]
        o = _obs(meta, "page", f"p{n}", {"page": n}, raw=text, attrs={"dates": dates})
        o.identity_keys = identity_keys({"text": text}, text_fields=True)
        o.related_resource_ids = list(dict.fromkeys(RESOURCE_RE.findall(text)))
        obs.append(o)
    return obs, diags
