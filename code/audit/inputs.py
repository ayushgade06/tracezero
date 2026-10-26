"""Up-front input checks (P2): every problem in the dataset's CSV files is reported together, before any
evidence is parsed, instead of surfacing later as a KeyError or TypeError deep in the pipeline.

Each schema requires only what the engine itself reads (consumer named per entry); nothing more:
  - required columns must be in the header; extra columns are allowed, except in system_activity.csv,
    whose rows become matching.Event(**row);
  - `filled` columns must be non-empty, `times` columns must parse as ISO-8601 (matching._ts),
    and `unique` ids must not repeat (request case_ids also not across request files);
  - every row has exactly as many fields as the header;
  - a file of zero bytes has no rows (as before P2); a header without the required columns is an error.
Evidence files named by the manifest are not checked here: a missing or unparseable one is already a
per-file diagnostic in ingest.parse_file, and the audit continues without it.
"""
import csv
from datetime import datetime
from pathlib import Path

MAX_ROW_ERRORS = 20  # per file; the rest are counted

REQUESTS = dict(  # matching.make_requests, main.audit_cases; times parsed by matching.build_candidates and main
    columns=("case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"),
    filled=("case_id",), times=("requested_at", "reported_at", "audit_at"), unique="case_id")
SCHEMAS = {
    "evidence_files.csv": dict(  # ingest.load_manifest (an unparseable captured_at is a per-file diagnostic)
        columns=("file_id", "file_path", "origin", "captured_at"), filled=("file_id", "file_path"), unique="file_id"),
    "user_identities.csv": dict(  # identity.load_identities
        columns=("user_id", "name", "email", "phone", "email_hash", "updated_at"), filled=("user_id",), unique="user_id"),
    "user_audit_log.csv": dict(  # identity.load_identities
        columns=("audit_id", "user_id", "event_time", "field_name", "old_value", "new_value")),
    "system_activity.csv": dict(  # matching.load_events (Event(**row)), discovery, ocr.load_aliases
        columns=("activity_id", "event_time", "event_type", "source_system", "target_system", "resource_id",
                 "reported_result", "details"),
        exact=True, filled=("activity_id",), times=("event_time",), unique="activity_id"),
}


class InputError(ValueError):
    """The dataset cannot be audited; the message lists every problem found."""


def _is_time(v):
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def check_csv(path, schema, errors, seen=None):
    """Append every problem with one CSV file to `errors`. `seen` (id -> where) spans several files."""
    seen = {} if seen is None else seen
    columns, unique = schema["columns"], schema.get("unique")
    row_errors = []
    try:
        with open(path, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames
            if header is None:
                return  # zero bytes: no rows
            missing = [c for c in columns if c not in header]
            extra = [c for c in header if c not in columns] if schema.get("exact") else []
            if missing or extra or len(set(header)) != len(header):
                errors.append(f"{path}: header {header}: " + "; ".join(
                    ([f"missing required column(s) {missing}; expected columns include {list(columns)}"] if missing else []) +
                    ([f"unexpected column(s) {extra}; this file must have exactly {list(columns)}"] if extra else []) +
                    (["duplicate column names"] if len(set(header)) != len(header) else [])))
                return
            for r in reader:
                where = f"{path}:{reader.line_num}"
                bad = []
                if None in r or None in r.values():
                    bad.append("number of fields differs from the header")
                bad += [f"empty {c}" for c in schema.get("filled", ()) if not (r[c] or "").strip()]
                bad += [f"{c} {r[c]!r} is not an ISO-8601 timestamp" for c in schema.get("times", ()) if not _is_time(r[c] or "")]
                if unique and r[unique]:
                    if r[unique] in seen:
                        bad.append(f"duplicate {unique} {r[unique]!r} (first at {seen[r[unique]]})")
                    else:
                        seen[r[unique]] = where
                if bad:
                    row_errors.append(f"{where}: " + "; ".join(bad))
    except FileNotFoundError:
        errors.append(f"{path}: file not found")
    except UnicodeDecodeError as e:
        errors.append(f"{path}: not UTF-8 text ({e.reason} at byte {e.start})")
    except csv.Error as e:
        errors.append(f"{path}: not readable as CSV: {e}")
    except OSError as e:
        errors.append(f"{path}: cannot read: {e.strerror or e}")
    errors += row_errors[:MAX_ROW_ERRORS]
    if len(row_errors) > MAX_ROW_ERRORS:
        errors.append(f"{path}: ... and {len(row_errors) - MAX_ROW_ERRORS} more row(s) with problems")


def validate(dataset, request_paths, extra_requests=()):
    """Raise InputError listing every problem in the dataset's CSV inputs. request_paths are the
    dataset's request files (jointly matched: case_ids unique across them); extra_requests are
    other request files to check on their own (e.g. --requests outside the dataset)."""
    errors = []
    for name, schema in SCHEMAS.items():
        check_csv(Path(dataset) / name, schema, errors)
    seen = {}
    for path in request_paths:
        check_csv(path, REQUESTS, errors, seen)
    for path in extra_requests:
        check_csv(path, REQUESTS, errors)
    if errors:
        raise InputError("\n".join(errors))
