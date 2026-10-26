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
