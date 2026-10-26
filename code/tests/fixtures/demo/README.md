# Synthetic demo dataset

A small, fully fictional dataset in the same layout as a real TraceZero input. Every name, email
(`example.org` / `example.net`), phone number, hash and resource id is made up. It is used by
`code/tests/test_fixture_demo.py` and by CI, and it serves as a runnable demo:

```bash
python code/main.py --dataset code/tests/fixtures/demo --out demo_output.csv
```

| Case | Request | What the evidence shows | Result |
|---|---|---|---|
| `case_901` | Deletion (Mira Holt) | Account gone (JSON lookup), but the search document still holds her email (SQL snapshot), a connector is still enabled (log), and a backup can be restored (JSON lookup); a deletion event in `system_activity.csv` is matched | `INCOMPLETE`, 3 findings |
| `case_902` | Deletion, sent from Tomas Reyes's **former** email | Account absent and security audit record retained (TXT handover, keyed by the current email) | `COMPLETE` |
| `case_903` | Deletion (Ines Calder) | Only a lookup from before the request (log) and an older PDF page | `INCOMPLETE`, blocked: historical readings only |
| `case_904` | Copy-of-data request that says not to delete | A data-catalog link and a classification event | `NOT_APPLICABLE` |

Evidence formats covered: `.json`, `.sql`, `.log`, `.txt`, `.pdf`. Screenshots (`.png`) are deliberately
absent, because without a cache entry the OCR engine runs whenever it is installed, which would make the
output depend on the environment. OCR is covered by the synthetic-box tests in `test_m6.py`.

`sample_deletion_requests.csv` is header-only, which audits exactly like leaving the file out (requests in both files are matched jointly).
The PDF is a minimal hand-built, uncompressed PDF, so its text is visible in the file itself.
