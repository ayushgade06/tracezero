# TraceZero

TraceZero is a deterministic, evidence-based auditor for data-deletion requests. Given a set of deletion
requests and the evidence an organisation collected about its systems (database snapshots, JSON exports,
service logs, PDF documents and console screenshots), it decides for each request whether deletion was
actually completed at the request's audit time, and writes a status with ordered findings, remediations and
verification steps, each tied to the evidence that supports it.

For every request TraceZero:

1. reads the request text to decide whether it actually asks for deletion (rule-based reader, optionally
   combined with cached Claude readings);
2. ingests every evidence file into provenance-preserving observations;
3. resolves the requester's current and historical identifiers (emails, phones, email hashes, user ids);
4. links evidence to the requester and discovers every resource that holds their data;
5. matches the request to at most one deletion event, abstaining when the match is ambiguous;
6. reconstructs each resource's state at `audit_at` (existence, exposure, data flow, restore path) from
   admissible evidence only;
7. applies the deletion policy per data category;
8. assembles `COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE` with cited findings, one remediation per finding,
   and verification steps; an independent validator checks the result.

Normal runs make no network calls and produce byte-identical output for identical inputs. TraceZero is
currently a command-line tool; there is no API or web interface yet (see [`docs/ROADMAP.md`](docs/ROADMAP.md)).

## Requirements

- Python 3.10+ (baseline verified on 3.12.6)
- `pypdf` and `scipy` (`code/requirements.txt`)
- Optional: `anthropic`, only to refresh intent readings (`code/requirements-llm.txt`)
- Optional: `rapidocr-onnxruntime`, only to regenerate the OCR cache, Python 3.12 or older (`code/requirements-ocr.txt`)

## Input dataset

TraceZero does not ship evidence data. Point it at a dataset directory (default: `./dataset`, which is
git-ignored) containing:

| File | Contents |
|---|---|
| `deletion_requests.csv` | `case_id, email, requested_at, reported_status, reported_at, audit_at, request_text` |
| `sample_deletion_requests.csv` | Same schema; read on every run because requests are matched jointly |
| `evidence_files.csv` | Evidence manifest: `file_id, file_path, origin, captured_at` |
| `evidence/…` | The evidence files the manifest names (`.sql`, `.json`, `.log`, `.txt`, `.pdf`, `.png`) |
| `user_identities.csv` | Current identity snapshot: `user_id, name, email, phone, email_hash, updated_at` |
| `user_audit_log.csv` | Identifier history: `audit_id, user_id, event_time, field_name, old_value, new_value` |
| `system_activity.csv` | Workflow activity events: `activity_id, event_time, event_type, source_system, target_system, resource_id, reported_result, details` |

`sample_output.csv` (reference answers) is used only by the evaluation runner.

## Usage

```bash
python -m pip install -r code/requirements.txt
python code/main.py --dataset <dataset-dir> --out output.csv
python code/evaluation/validate_output.py --output output.csv --requests <dataset-dir>/deletion_requests.csv --strict
python code/evaluation/main.py --dataset <dataset-dir> --output output.csv   # needs sample_output.csv
python -m unittest discover -s code/tests                                    # see note below
```

Options: `python code/main.py [--dataset DIR] [--requests CSV] [--out CSV] [--refresh-intent-cache]`.

On Windows use `python` or `py`, and set `PYTHONIOENCODING=utf-8` if the console mangles non-ASCII output.

**Caches.** Screenshot evidence is read from an OCR cache in `code/cache/ocr/`, and Claude's intent readings
from `code/cache/llm/intent.json`. Both are derived from a specific dataset, so they are git-ignored and not
shipped. Without them, screenshots contribute no observations and intent uses the rule reader only. To
build them for your dataset:

```bash
python -m pip install -r code/requirements-ocr.txt && python code/evaluation/ocr_report.py --dataset <dataset-dir> --refresh-ocr
python -m pip install -r code/requirements-llm.txt
export ANTHROPIC_API_KEY=...   # PowerShell: $env:ANTHROPIC_API_KEY = "..."
python code/main.py --dataset <dataset-dir> --refresh-intent-cache
```

**Tests.** 140 of the 180 tests run without a dataset (119 pass). The corpus tests read `./dataset` and
error when it is absent. Results are recorded in [`docs/BASELINE.md`](docs/BASELINE.md).

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): modules and the execution flow from evidence to output
- [`code/DESIGN.md`](code/DESIGN.md): per-stage rules and recorded design decisions
- [`docs/BASELINE.md`](docs/BASELINE.md): baseline commands and their recorded results
- [`docs/ROADMAP.md`](docs/ROADMAP.md): incremental migration plan

## License

No license has been chosen yet; until one is added, all rights are reserved by the author. Third-party
dependencies are installed from PyPI under their own licenses and are not vendored here.
