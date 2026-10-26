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

- Python 3.10+. Verified on 3.12 (Windows and Linux) and 3.13 (Windows); 3.10 and 3.11 are only
  syntax-checked.
- Runtime: `pypdf` and `scipy`, installed automatically.
- Optional extras: `ocr` (`rapidocr-onnxruntime`, Python 3.12 or older), only to regenerate the OCR cache;
  `llm` (`anthropic`), only to refresh intent readings.

## Install

Install from a source checkout, in a virtual environment:

```bash
git clone https://github.com/ayushgade06/tracezero.git && cd tracezero
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
python -m pip install -e .                        # add [ocr] or [llm] for the optional extras
```

The install is editable on purpose. The engine resolves its defaults (`./dataset`, `./output.csv`,
`code/cache/`) from its own location in the checkout, and `tracezero` stops with an explicit message if it
is installed any other way. Without installing, `python -m pip install -r code/requirements.txt` and
`python code/main.py` work exactly as before.

## Run

```bash
tracezero --dataset <dataset-dir> --out output.csv          # same as: python code/main.py ...
python code/evaluation/validate_output.py --strict --output output.csv --requests <dataset-dir>/deletion_requests.csv
```

Options: `[--dataset DIR] [--requests CSV] [--out CSV] [--policy CSV] [--settings JSON] [--refresh-intent-cache]`.
Policy, settings, request files and input validation are described in
[`docs/CONFIGURATION.md`](docs/CONFIGURATION.md). The defaults are
`./dataset` and `./output.csv` at the repository root. On Windows, set `PYTHONIOENCODING=utf-8` if the
console mangles non-ASCII output.

### Synthetic demo

A small, fictional dataset lives in [`code/tests/fixtures/demo`](code/tests/fixtures/demo/README.md). It
covers residual data, a proven deletion, insufficient evidence and a non-deletion request:

```bash
tracezero --dataset code/tests/fixtures/demo --out demo_output.csv
python code/evaluation/validate_output.py --strict --output demo_output.csv --requests code/tests/fixtures/demo/deletion_requests.csv
```

## Input dataset

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

**Caches and optional OCR.** Screenshot evidence is read from an OCR cache in `code/cache/ocr/`, and Claude's
intent readings from `code/cache/llm/intent.json`. Both are derived from a specific dataset, so they are
git-ignored and not shipped. Without a cache entry, a screenshot contributes no observations unless the OCR
engine is installed, and intent falls back to the rule reader. To build the caches for your dataset:

```bash
python -m pip install -e ".[ocr]" && python code/evaluation/ocr_report.py --dataset <dataset-dir> --refresh-ocr
python -m pip install -e ".[llm]"
export ANTHROPIC_API_KEY=...   # PowerShell: $env:ANTHROPIC_API_KEY = "..."
tracezero --dataset <dataset-dir> --refresh-intent-cache
```

## Tests

```bash
python -m unittest discover -s code/tests
```

A clean checkout needs no private data: 238 tests, of which 185 run (synthetic data, including the
end-to-end demo tests in `test_fixture_demo.py`) and 53 skip with the reason `reference dataset not
available`.

The skipped corpus tests run against the private reference dataset. Put it at `./dataset`, or point
`TRACEZERO_DATASET` at its root (the directory holding `deletion_requests.csv`, `sample_output.csv`,
`evidence_files.csv` and `evidence/`). Both caches must also be present in `code/cache/`, because the
recorded expectations depend on them. If `TRACEZERO_DATASET` is set but files are missing, those tests
fail and list the missing paths; they never fall back to another location. With everything present, all
238 run and only the live-OCR test skips when the engine isn't installed.

The configuration tests (custom policy, request files, input validation, settings) are
`code/tests/test_p2_*.py`. Run them alone with `python -m unittest discover -s code/tests -p "test_p2_*.py"`;
see [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md#tests).

CI (`.github/workflows/ci.yml`) runs the tests and the strictly validated demo audit on every push and pull
request, with no private data, caches, API keys or OCR engine.

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): modules and the execution flow from evidence to output
- [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md): custom policy, dataset-convention settings, request files, input validation
- [`code/DESIGN.md`](code/DESIGN.md): per-stage rules and recorded design decisions
- [`docs/BASELINE.md`](docs/BASELINE.md): verification commands and their recorded results
- [`docs/ROADMAP.md`](docs/ROADMAP.md): incremental plan

## License

TraceZero is licensed under the [Apache License 2.0](LICENSE); see also [`NOTICE`](NOTICE). Third-party
dependencies are installed from PyPI under their own licenses and are not vendored here.
