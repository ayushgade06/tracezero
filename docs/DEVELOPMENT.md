# Development and operation

Everything the [README](../README.md) leaves out to stay short: install details, CLI options, the dataset
format, the optional caches, the web client's configuration, and the test setup.

## Install

- Python 3.10+. Verified on 3.12 (Windows and Linux) and 3.13 (Windows); 3.10 and 3.11 are only
  syntax-checked.
- Runtime dependencies, installed automatically: `pypdf` and `scipy`.
- Optional extras: `ocr` (`rapidocr-onnxruntime`, Python 3.12 or older), only to regenerate the OCR cache;
  `llm` (`anthropic`), only to refresh intent readings. Install with `pip install -e ".[ocr]"` or `".[llm]"`.

The install is editable on purpose. The engine resolves its defaults (`./dataset`, `./output.csv`,
`code/cache/`) from its own location in the checkout, and `tracezero` stops with an explicit message if it
is installed any other way. Without installing, `python -m pip install -r code/requirements.txt` and
`python code/main.py` work the same way.

On Windows, set `PYTHONIOENCODING=utf-8` if the console mangles non-ASCII output.

## Command line

```bash
tracezero --dataset <dataset-dir> --out output.csv
python code/evaluation/validate_output.py --strict --output output.csv --requests <dataset-dir>/deletion_requests.csv
```

Options: `[--dataset DIR] [--requests CSV] [--out CSV] [--policy CSV] [--settings JSON] [--refresh-intent-cache]`.
The defaults are `./dataset` and `./output.csv` at the repository root. `tracezero` is the same as
`python code/main.py`. Policy, settings, request files and input validation are in
[`CONFIGURATION.md`](CONFIGURATION.md).

## Library

The CLI is a thin wrapper over `audit.engine.audit`. After `pip install -e .`, any Python program can call it:

```python
from audit.engine import audit

result = audit("code/tests/fixtures/demo")       # raises audit.inputs.InputError for invalid input
print(result.summary)                            # {'rows': 4, 'status': {...}, 'basis': {...}, 'steps': 13}
case = result.case("case_901")
print(case.deletion_status, case.trace["blockers"], case.evaluations[0]["citation"])
```

Signature: `audit(dataset, requests=None, *, out=None, policy=None, settings=None) -> AuditResult`. `out` writes
the CSV; `result.to_dict()` is JSON-compatible. The result schema and error contract are in
[`ARCHITECTURE.md`](ARCHITECTURE.md#library-interface).

## Dataset format

TraceZero does not ship evidence data. A dataset directory contains:

| File | Contents |
|---|---|
| `deletion_requests.csv` | `case_id, email, requested_at, reported_status, reported_at, audit_at, request_text` |
| `sample_deletion_requests.csv` | Same schema. Optional; when present it is matched jointly with `deletion_requests.csv`. Either file may be absent, but not both |
| `evidence_files.csv` | Evidence manifest: `file_id, file_path, origin, captured_at` |
| `evidence/…` | The files the manifest names (`.sql`, `.json`, `.log`, `.txt`, `.pdf`, `.png`) |
| `user_identities.csv` | Current identity snapshot: `user_id, name, email, phone, email_hash, updated_at` |
| `user_audit_log.csv` | Identifier history: `audit_id, user_id, event_time, field_name, old_value, new_value` |
| `system_activity.csv` | Workflow activity events: `activity_id, event_time, event_type, source_system, target_system, resource_id, reported_result, details` |

`sample_output.csv` (reference answers) is read only by the evaluation runner and the reference tests.

## Caches: OCR and intent readings

Screenshot evidence is read from an OCR cache in `code/cache/ocr/`, and Claude's intent readings from
`code/cache/llm/intent.json`. Both are derived from a specific dataset, so they are git-ignored and not
shipped. Without a cache entry, a screenshot contributes no observations unless the OCR engine is installed,
and intent falls back to the rule reader. To build the caches for your dataset:

```bash
python -m pip install -e ".[ocr]" && python code/evaluation/ocr_report.py --dataset <dataset-dir> --refresh-ocr
python -m pip install -e ".[llm]"
export ANTHROPIC_API_KEY=...   # PowerShell: $env:ANTHROPIC_API_KEY = "..."
tracezero --dataset <dataset-dir> --refresh-intent-cache
```

## HTTP API and web client

The API's endpoints, configuration (`TRACEZERO_API_DATASETS`, `_HOST`, `_PORT`, `_WORKERS`, `_MAX_PENDING`),
errors and limitations are in [`API.md`](API.md).

The web client needs Node.js ^22.22.2 or ^24.15 for its tests (jsdom); the dev server and build alone run on
20.19+. Verified on 22.22.3.

| Variable | Where | Default | Meaning |
|---|---|---|---|
| `TRACEZERO_API_URL` | `npm run dev` / `npm run preview` (Node side, not bundled) | `http://127.0.0.1:8000` | Where the dev server forwards `/api` |
| `VITE_TRACEZERO_API_BASE` | build time (bundled) | `/api` | Base URL the browser calls. Change it only if something else serves the API under another path |

`npm run build` type-checks and writes `web/dist/`, which `npm run preview` serves with the same proxy. There
is no production server. Press Ctrl+C to stop the API or the dev server; the API drops queued audits and
exits when any running audit finishes.

## Tests

```bash
python -m unittest discover -s code/tests     # engine, library and API
cd web && npm test                            # web client
```

A clean checkout needs no private data: 308 Python tests, of which 254 run (synthetic data, including the
end-to-end demo tests in `test_fixture_demo.py`) and 54 skip with the reason `reference dataset not
available`.

The skipped corpus tests run against the private reference dataset. Put it at `./dataset`, or point
`TRACEZERO_DATASET` at its root (the directory holding `deletion_requests.csv`, `sample_output.csv`,
`evidence_files.csv` and `evidence/`). Both caches must also be present in `code/cache/`, because the
recorded expectations depend on them. If `TRACEZERO_DATASET` is set but files are missing, those tests
fail and list the missing paths; they never fall back to another location. With everything present, all
308 run and only the live-OCR test skips when the engine isn't installed.

| Tests | Covers |
|---|---|
| `test_m*.py`, `test_fixture_demo.py` | Engine stages and the end-to-end demo |
| `test_p2_*.py` | Custom policy, request files, input validation, settings ([`CONFIGURATION.md`](CONFIGURATION.md#tests)) |
| `test_p3_engine.py` | Library result, serialisation, sequential audits, errors, CLI delegation |
| `test_p4_api.py`, `test_p5_datasets.py` | HTTP API: validation, path containment, failures, job states, concurrency, dataset list |
| `test_p6_api.py` | Release hardening: manifest containment, path redaction, job retention, Host check, stalled clients |

Run one group with `-p`, for example `python -m unittest discover -s code/tests -p "test_p6_*.py"`.

CI (`.github/workflows/ci.yml`) runs the Python tests, the strictly validated demo audit, and the web tests and
build on every push and pull request, with no private data, caches, API keys or OCR engine.
