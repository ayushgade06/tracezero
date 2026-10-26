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

**The representation before P2**, which the loader reproduces exactly. `policy.POLICY` was a Python dict
with 16 entries: `data_category -> (rule_id, required_action, source, assumed_condition)`, all strings
except `assumed_condition`, which is a string or `None`. Rules `rule_01`–`rule_15` come from the
dataset's rules file (`source` `RULES`), and `spec_6.5_no_recreating_sync` (category `DATA_FLOW`, action
`FLOW`) comes from the output spec (`source` `SPEC`). The consumers are:

- `policy.category`: a record's explicit `record_type` counts only if it is a key of the matrix, and
  `PolicyContext` learns id-prefix categories only from such record types;
- `policy.evaluate`: the action selects the check in `_judge`, the `rule_id` is cited in the finding, and
  the `assumed_condition`, if any, becomes a note;
- `policy.plan` and `final.required_state`: the action picks the remediation and the after-the-fix check.

`source` is carried along but changes no decision. Order changes no decision either (categories are
compared after sorting), but the loader keeps file order and the test checks it. Before P2 there was no
"malformed entry" case, because the matrix was code. A category assigned by name, content or worker
operation but missing from the matrix raised `KeyError` mid-run, and a `record_type` missing from the
matrix was silently not treated as an explicit category. The loader turns the first case into the
"required category" check below; the second is still allowed (see the end of this section).

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

Example (`--policy rules.csv`, where line 2 has a bad action and line 3 repeats line 2):

```
invalid policy:
rules.csv:2: required_action 'PURGE' is not one of ['DELETE', 'REDACT', 'ANONYMIZE', 'RETAIN', 'FLOW']
rules.csv:3: duplicate rule_id 'rule_01' (first on line 2); duplicate data_category 'USER_PROFILE' (first on line 2)
rules.csv: no rule for required data_category ['ACCOUNT_TOMBSTONE', ...] (the engine assigns these categories itself)
```

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

The cases are kept apart: a **missing** file is an error (except the optional request files, see
above); a **zero-byte** file or a **header-only** file is valid and has no records; a header **missing a
required column** is an error; a blank value in a column not listed above (an email, a phone, a request
text) is accepted, as before; a **malformed row** (wrong field count, empty id, bad timestamp, duplicate
id) is an error naming its line.

Example:

```
invalid input:
<dataset>/user_identities.csv: file not found
<dataset>/system_activity.csv: header [..., 'details', 'team']: unexpected column(s) ['team']; this file must have exactly ['activity_id', ..., 'details']
<dataset>/sample_deletion_requests.csv:2: requested_at 'yesterday' is not an ISO-8601 timestamp
<dataset>/sample_deletion_requests.csv:3: empty case_id
<dataset>/deletion_requests.csv: header ['id', 'email', ...]: missing required column(s) ['case_id']; expected columns include ['case_id', 'email', 'requested_at', 'reported_status', 'reported_at', 'audit_at', 'request_text']
```

A missing `--requests` file says `request file not found (choose one with --requests)`, and a dataset
with neither request file says `no request file; expected at least one of [...]`.

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

**Missing header diagnostic.** If the dataset has SQL dumps but none carries the configured header line,
the run prints a warning to stderr (and the pipeline result lists it under `warnings`). The warning names
any other values the label has, for example:

```
warning: sql_point_in_time_header: none of the 1 SQL dump(s) has the header line '-- Ghost Data Still Exists: point-in-time database snapshot', so rule R4e (absence from a point-in-time snapshot) never applies. If your dumps mark snapshots differently, set sql_point_in_time_header in --settings (or null).
```

The warning changes no decision. The synthetic demo prints it, because its one dump has no such header.
The reference dataset doesn't (13 of its 24 dumps carry the header). The setting affects only R4e: other
header lines (`Captured at`, topology) and the comparison of sibling copies within one dump (R4b) behave
the same under any value. A test covers this.

**Screenshots.** Screenshot parsing is tested on synthetic OCR boxes (`test_m6.screen`) with the default and
a custom marker. The real OCR engine was not run in P2. The reference run reads the committed OCR cache, so
its screenshots go through `ocr.layout` with the default marker, and the hash checks that path.

For reference, on the reference dataset: `{"sql_point_in_time_header": null}` turns 20 `COMPLETE` rows
into `INCOMPLETE`, and `{"screenshot_template_marker": null}` drops 15 screenshot-derived steps (the same
output as running without the OCR cache).

## Tests

```bash
python -m unittest discover -s code/tests                      # everything
python -m unittest discover -s code/tests -p "test_p2_*.py"    # configuration only
```

| File | Covers |
|---|---|
| `test_p2_policy.py` | default policy equals the pre-P2 matrix; custom policies reach findings and remediations; every malformed-policy case; CLI never falls back |
| `test_p2_requests.py` | both, only one, or neither request file; zero-byte file; joint matching across files; `--requests` rules |
| `test_p2_inputs.py` | each validation rule, the edge cases above, error capping, CLI behaviour |
| `test_p2_settings.py` | defaults equal the old literals; custom and disabled R4e header and template marker; missing-header warning; settings-file validation |
