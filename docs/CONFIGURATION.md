# Configuration

TraceZero runs on your own evidence and policy without code changes. With no options it behaves exactly as
before (see the guardrail in [`ROADMAP.md`](ROADMAP.md)).

## Deletion policy (`--policy`)

```bash
tracezero --dataset <dir> --policy my_rules.csv
```

Without `--policy`, the default policy [`code/audit/deletion_rules.csv`](../code/audit/deletion_rules.csv)
is used. That file is the only place the default is written down: `audit.policy.POLICY` is loaded from it
when the module is imported. `code/tests/test_p2_policy.py` pins it to the matrix that was hard-coded
before P2 (same categories, rule ids, actions, sources, notes and order), so an edit to the default file
fails that test.

A policy file is a UTF-8 CSV with one row per data category:

| Column | Required | Meaning |
|---|---|---|
| `rule_id` | yes | Identifier cited in findings and remediations. Unique. |
| `data_category` | yes | Category the rule applies to (e.g. `USER_PROFILE`). Unique. |
| `required_action` | yes | `DELETE`, `REDACT`, `ANONYMIZE`, `RETAIN` or `FLOW` (see below). |
| `condition` | no | Free text for readers. The engine does not evaluate it. |
| `source` | no | Where the rule comes from; default `RULES`. |
| `assumed_condition` | no | Note attached to every evaluation under this rule, recording a condition the evidence can't verify (e.g. `CONDITION_ASSUMED_SOLE_SUBJECT`). Default: no note. |

The format is a superset of the dataset convention `deletion_rules.csv`
(`rule_id,data_category,condition,required_action`).

**How each action is evaluated:**

| Action | Violation when | Finding / remediation |
|---|---|---|
| `DELETE` | the record exists at `audit_at` | `REPORT_REMAINING_RECORD` / `DELETE_RECORD` |
| `REDACT`, `ANONYMIZE` | the record exists with identifiers visible | `REPORT_EXPOSED_PII` / `REDACT_RECORD`, `ANONYMIZE_RECORD` |
| `FLOW` | a data flow (sync, connector) is active | `REPORT_ACTIVE_DATA_FLOW` / `DISABLE_DATA_FLOW` |
| `RETAIN` | `BACKUP_ARCHIVE`: its restore path is open. Others: never; the record must exist and its retention end (`retention_until`, or `retained_until` for `LEGAL_HOLD_RECORD`) must lie after `audit_at` | `REPORT_RESTORE_RISK` / `BLOCK_RESTORE` (backups only) |

Some categories get extra handling under particular actions. Under `RETAIN`, a `SECURITY_AUDIT_LOG` or
`LEGAL_HOLD_RECORD` with no supplied retention end counts as retained, and `BACKUP_ARCHIVE` is judged on
its restore path. Under `REDACT` or `ANONYMIZE`, a missing `ACCOUNT_TOMBSTONE` is inconclusive. See
`_judge` in `code/audit/policy.py`.

**Validation.** The whole file is checked before any evidence is read, and every problem is reported
together with the file name and line number. The run then stops (exit 1) and writes no output. It never
falls back to the default policy. A file is rejected for:

- a missing file, unreadable or non-UTF-8 content;
- a missing required column, an unknown column (typo protection) or a duplicated header name;
- a row with more fields than the header, an empty `rule_id` or `data_category`, or an unknown action;
- a duplicate `rule_id` or `data_category`;
- no rule for one of the categories the engine assigns by itself: `ACCOUNT_TOMBSTONE`,
  `ANALYTICS_PROFILE`, `BACKUP_ARCHIVE`, `DATA_FLOW`, `LEGAL_HOLD_RECORD`, `MARKETING_PROFILE`,
  `SEARCH_INDEX_ENTRY`, `SECURITY_AUDIT_LOG`, `SUPPORT_ATTACHMENT`, `SUPPORT_MESSAGE`, `USER_EXPORT`,
  `USER_PROFILE`. Those categories come from collection names, id prefixes, support content and
  deletion-worker operations (`policy.REQUIRED_CATEGORIES`).

Other categories are optional. Leave one out and records that declare it as their `record_type` fall
through to the other category sources, and are usually reported as `CATEGORY_UNKNOWN` (inconclusive). Add
a new category and records whose `record_type` names it are judged under its rule.

A dataset's own `deletion_rules.csv` in the original convention has no `DATA_FLOW` row, so it is rejected
until one is added (the default uses `spec_6.5_no_recreating_sync,DATA_FLOW,...,FLOW`). It also has no
`assumed_condition` column, so rules 05, 06 and 14 would lose their notes. The policy file is only ever
read from `--policy`; TraceZero doesn't pick up `deletion_rules.csv` from the dataset directory.

## Request files

The dataset's request files are `sample_deletion_requests.csv` and `deletion_requests.csv`. Either may be
absent, but not both. The files present are read together, sample file first, and matched jointly: the
one-to-one assignment of requests to deletion events spans all of them. An absent file behaves exactly
like a header-only or zero-byte one. `--requests` (default `<dataset>/deletion_requests.csv`) chooses
which rows to write to the output. Those rows must also be in one of the dataset's request files, so
that they took part in the joint matching. A `case_id` may appear only once across all request files.

## Input validation

Before any evidence is parsed, TraceZero checks its CSV inputs and reports every problem at once, with
file and line, then stops with exit code 1 and writes no output (`code/audit/inputs.py`). Only what the
engine reads is required:

| File | Required columns | Must be non-empty | Must be ISO-8601 | Unique |
|---|---|---|---|---|
| request files | `case_id, email, requested_at, reported_status, reported_at, audit_at, request_text` | `case_id` | `requested_at, reported_at, audit_at` | `case_id`, across files |
| `evidence_files.csv` | `file_id, file_path, origin, captured_at` | `file_id, file_path` | | `file_id` |
| `user_identities.csv` | `user_id, name, email, phone, email_hash, updated_at` | `user_id` | | `user_id` |
| `user_audit_log.csv` | `audit_id, user_id, event_time, field_name, old_value, new_value` | | | |
| `system_activity.csv` | exactly `activity_id, event_time, event_type, source_system, target_system, resource_id, reported_result, details` | `activity_id` | `event_time` | `activity_id` |

Also checked for every file: it exists, is UTF-8, has no duplicated header name, and every row has as many
fields as the header. Extra columns are allowed, except in `system_activity.csv`. A zero-byte file has no
rows. At most 20 row problems are listed per file; the rest are counted.

Not checked up front, on purpose: evidence files named in the manifest, and manifest `captured_at`
values. A missing or unparseable one is already reported as a per-file diagnostic, and the audit goes on
without that file, as before.

## Dataset-convention settings (`--settings`)

```bash
tracezero --dataset <dir> --settings settings.json
```

Two evidence-format conventions of the reference dataset are named settings (`code/audit/settings.py`).
A settings file is a JSON object holding any subset of the keys below. A key you leave out keeps its
default, and the defaults reproduce the behaviour from before P2.

```json
{
  "sql_point_in_time_header": {"label": "Ghost Data Still Exists", "value": "point-in-time database snapshot"},
  "screenshot_template_marker": "Northstar"
}
```

| Setting | Default | Effect |
|---|---|---|
| `sql_point_in_time_header` | label `Ghost Data Still Exists`, value `point-in-time database snapshot` | A SQL dump whose leading comment header contains `-- <label>: <value>` is a point-in-time snapshot. A resource missing from every copy of one of its tables is then read as absent at the capture time (rule R4e, `state.py`). The label matches case-insensitively, ignoring punctuation; the value must match exactly. `null` turns R4e off. |
| `screenshot_template_marker` | `Northstar` | A screenshot is parsed with the admin-console template (`ocr.layout`) only if a box in its left sidebar contains this text. Otherwise it is reported as `UNRECOGNISED_LAYOUT` and contributes context only, never a record or a state. `null` turns screenshot parsing off. |

The template's geometry (pixel bands for the top bar, header, KPI cards and content in `ocr.py`) describes
one specific admin-console layout and is not a setting. Point the marker at a different console only if
its screens use that same layout.

The file is validated before any evidence is read. Unknown keys, wrong types, an empty marker, an empty
header value, or a label that a dump header line can never produce (it must start with a letter and use
only letters, spaces and underscores, at most 41 characters) are reported with the file name. The run then
stops with exit code 1 and doesn't fall back to the defaults.

For reference, on the reference dataset: `{"sql_point_in_time_header": null}` turns 20 `COMPLETE` rows
into `INCOMPLETE`, and `{"screenshot_template_marker": null}` drops 15 screenshot-derived steps (the same
output as running without the OCR cache).
