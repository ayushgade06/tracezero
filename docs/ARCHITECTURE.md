# TraceZero architecture (current implementation)

This describes the code as it exists today: a single-process Python CLI in `code/`. Per-stage rules and
recorded decisions live in [`code/DESIGN.md`](../code/DESIGN.md) and in each module's docstring, which is the
most detailed and authoritative description of that stage.

## Execution flow

```text
code/main.py  main() ─► run(dataset, requests, out)
  │
  ├─ audit/pipeline.py  run(dataset)            one deterministic pass over ALL requests
  │    1 ingest.ingest          evidence_files.csv + evidence/* ─► Observations + diagnostics
  │        parsers.py           SQL (literal values only, never executed), JSON record walker,
  │                             logs (JSON, bracketed, pipe, syslog, key=value; trace context), TXT, PDF pages (pypdf)
  │        ocr.py               PNG screenshots ─► layout units ─► Observations (reads code/cache/ocr)
  │    2 identity.load_identities   user_identities.csv + user_audit_log.csv ─► IdentityIndex
  │                                  (identifiers with validity windows; exact matching only)
  │    3 discovery.build_links      Observations × identities (+ catalog rows in system_activity.csv)
  │                                  ─► Links (direct: catalog/record/trace; candidate: trace_multi/
  │                                  related_mention/page);  classify_resources ─► ownership status
  │    4 matching.match             requests (sample + main files) × system_activity.csv events
  │                                  ─► one-to-one assignment (scipy linear_sum_assignment per
  │                                  component), HIGH/MEDIUM/LOW confidence, explicit abstention
  │    5 state.reconstruct          per (request, resource): existence / exposure / flow / restore
  │                                  at audit_at from admissible readings ─► Assessments
  │    6 policy.run                 category ─► VIOLATION | COMPLIANT | INCONCLUSIVE |
  │                                  NOT_ATTRIBUTABLE; Findings + one Remediation each
  │
  ├─ audit_cases()  per requested row
  │    intent.classify (rules) + llm_intent.labels (cache only) ─► intent.decide
  │    final.assemble ─► (deletion_status, steps, trace)
  │
  └─ output.write_output ─► output.csv   (case_id, deletion_status, steps_json)
```

Requests in `sample_deletion_requests.csv` and `deletion_requests.csv` are always matched together because
the event assignment is global (one event per request, one request per event). Either file may be absent
(`pipeline.request_files`). `--requests` only selects which rows are written. Before any of this,
`inputs.validate` checks the CSV inputs, and `--policy` / `--settings` are loaded and validated
(see [`CONFIGURATION.md`](CONFIGURATION.md)).

## Modules

| Module | Responsibility |
|---|---|
| `code/main.py` | CLI: argument parsing, intent decision per row, output writing, `--refresh-intent-cache` |
| `audit/pipeline.py` | Runs stages 1–6 in order; holds no interpretation logic |
| `audit/observation.py` | `Observation` schema, timestamp normalisation, identifier and resource-id extraction |
| `audit/parsers.py` | Format parsers returning observations and diagnostics (nothing dropped silently) |
| `audit/ingest.py` | Manifest loading, per-file dispatch, resource-id coverage audit |
| `audit/ocr.py` | OCR engine wrapper, content-keyed cache, screenshot layout units, trust rules |
| `audit/identity.py` | Identity model with historical identifiers; exact, case-insensitive email resolution |
| `audit/discovery.py` | Evidence links and resource ownership classification |
| `audit/matching.py` | Request ↔ deletion-event matching with hard constraints H1–H4 and abstention |
| `audit/state.py` | Temporal state reconstruction; admissibility rules A1–A10; staleness |
| `audit/policy.py` | Category resolution, policy matrix, ownership/conflict gates, citations, remediation |
| `audit/final.py` | Request status and `steps_json` assembly; blockers; step ordering |
| `audit/intent.py` | Multilingual rule-based intent reader and `decide()` |
| `audit/llm_intent.py` | Claude intent reader (batched, JSON schema), content-keyed cache, usage accounting |
| `audit/output.py` | CSV writer with compact, key-sorted JSON |
| `evaluation/validate_output.py` | Independent validator; reads only the serialised CSV |
| `evaluation/main.py` | Evaluation runner: strict validation, agreement with reference answers, citation grounding, custom cases |
| `evaluation/custom_cases.py` | Seven synthetic end-to-end cases (no dataset needed) |
| `evaluation/*_report.py` | Per-stage diagnostic reports (ingest, discovery, matching, state, policy, OCR, gaps) |
| `tests/test_*.py` | `unittest` suites per stage, plus corpus and determinism tests |

## Output contract

One CSV row per request: `case_id, deletion_status, steps_json`.

- `deletion_status`: `COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE`.
- `steps_json`: a JSON array of steps. Each step has `step_no`, `phase` (`FINDING` | `REMEDIATION` |
  `VERIFICATION`), `system_id`, `resource_id`, `operation`, `state`, and `evidence_id` (a manifest file id,
  `activity_*` or `audit_*`, or empty when uncited).
- Findings pair with remediations: `REMAINING → DELETE`, `EXPOSED_PII → REDACT | ANONYMIZE`,
  `ACTIVE_DATA_FLOW → DISABLE`, `RESTORE_RISK → BLOCK`.
- `COMPLETE` requires a cited verification of the requester's account or tombstone.

The full rule set enforced is in the docstring of `evaluation/validate_output.py`.

## Determinism and external calls

- No network calls during a normal run. Claude is called only with `--refresh-intent-cache`, and the
  OCR engine only with `ocr_report.py --refresh-ocr`.
- Output order is fixed (input order, then phase / operation / system / resource within a row), and the JSON
  is key-sorted. Tests check byte-identical output across processes and hash seeds.

## Known limitations and technical debt

Verified in the code during the P0 audit:

- **Dataset-shaped inputs.** File names are fixed (`pipeline.REQUEST_FILES`, `inputs.SCHEMAS`). Since P2,
  either request file may be absent, and CSV columns, ids and request/event timestamps are validated up
  front (`audit/inputs.py`).
- **Policy (P2: configurable).** The default matrix is `code/audit/deletion_rules.csv`, and `--policy`
  selects another. Category assignment (collection names, id prefixes, worker operations in
  `audit/policy.py`) is still code.
- **Format-specific heuristics.** The R4e SQL dump header and the screenshot template marker are settings
  (`audit/settings.py`, `--settings`) since P2. The template geometry in `ocr.layout` (pixel bands for one
  admin-console layout), the log parser's five line formats and the name-based category maps are still
  code.
- **Corpus tests need the private reference dataset.** They run when it's at `./dataset` or
  `TRACEZERO_DATASET`, with both caches, and skip otherwise (`code/tests/refdata.py`). A synthetic demo dataset
  (`code/tests/fixtures/demo`) exercises the full pipeline without it.
- **Caches are dataset-specific.** Without `code/cache/ocr`, screenshots produce no observations.
- **Checkout-only install.** `pyproject.toml` installs only the `tracezero` wrapper
  (`code/tracezero_cli.py`), which runs `code/main.py` from the checkout. Defaults are resolved from file
  locations, so only editable installs are supported. There is no lockfile or linting; CI runs the tests and
  the demo.
- **Single-shot batch.** The whole corpus is processed in memory in one pass (about 13 s for 518 requests and
  94 evidence files). There is no incremental or per-request API.
- **Known accuracy gaps** against the reference answers: status accuracy 0.875 and step F1 0.907 on 32
  sample cases. `case_61` is an evidence-gap row (`INCOMPLETE` with no steps).
