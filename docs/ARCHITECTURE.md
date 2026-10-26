# TraceZero architecture (current implementation)

This describes the code as it exists today: a single-process Python CLI, library and minimal HTTP API in `code/`, and a web client in `web/`. Per-stage rules and
recorded decisions live in [`code/DESIGN.md`](../code/DESIGN.md) and in each module's docstring, which is the
most detailed and authoritative description of that stage.

## Execution flow

```text
code/main.py  main() ─► run(dataset, requests, out)      CLI: arguments, stderr warnings, exit codes
web/          browser ─► Vite /api proxy ─► code/api.py  Web client: renders the API's JSON, computes nothing
code/api.py   POST /audits ─► Jobs ─► thread pool        HTTP API: dataset ids, job registry, JSON errors
  │
audit/engine.py  audit(dataset, requests, out, policy, settings) ─► AuditResult   (the only audit path)
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
  ├─ engine.audit_cases()  per requested row
  │    intent.classify (rules) + llm_intent.labels (cache only) ─► intent.decide
  │    final.assemble ─► (deletion_status, steps, trace)
  │
  ├─ output.write_output ─► output.csv   (case_id, deletion_status, steps_json), when out is given
  └─ AuditResult: the rows above + their M5 evaluations and remediations, warnings, ingest diagnostics
```

Requests in `sample_deletion_requests.csv` and `deletion_requests.csv` are always matched together because
the event assignment is global (one event per request, one request per event). Either file may be absent
(`pipeline.request_files`). `--requests` only selects which rows are written. Before any of this,
`inputs.validate` checks the CSV inputs, and `--policy` / `--settings` are loaded and validated
(see [`CONFIGURATION.md`](CONFIGURATION.md)).

## Library interface

```python
from audit.engine import audit, AuditResult, CaseResult
audit(dataset, requests=None, *, out=None, policy=None, settings=None) -> AuditResult
```

| Argument | Meaning |
|---|---|
| `dataset` | Dataset directory. Every dataset file and evidence path is resolved inside it. A relative path is relative to the working directory, as with any Python file API |
| `requests` | The request file whose rows are reported. Default `<dataset>/deletion_requests.csv`, as `--requests`. Its rows must also be in one of the dataset's request files, which are always matched jointly ([`CONFIGURATION.md`](CONFIGURATION.md#request-files)) |
| `out` | Write the output CSV there, byte-identical to the CLI's. `None` (default) writes nothing |
| `policy` | Policy CSV path, or a matrix from `policy.load_policy`. Default `code/audit/deletion_rules.csv` |
| `settings` | Settings JSON path, or a `settings.Settings`. Default `settings.DEFAULT` |

`main.run`, which the CLI calls, is `audit()` plus printing `result.warnings` to stderr. Argument parsing,
loading `--policy`/`--settings` before any work, the `wrote … rows` line and the exit codes stay in
`code/main.py`. The library does not duplicate any part of the pipeline. Nothing is written to module
state: the policy and settings travel as arguments (`pipeline.run` → `policy.PolicyContext`,
`ingest`/`state`), so sequential audits in one process can't affect each other
(`code/tests/test_p3_engine.py`). The only module-level caches are a pure timestamp memo (`state._ts_memo`)
and the read-only OCR and intent caches under `code/cache/`.

**`AuditResult`** (frozen dataclasses; `to_dict()` returns plain JSON types, paths use forward slashes):

| Field | Content |
|---|---|
| `dataset`, `requests` | The paths audited |
| `output` | The CSV path written, or `None` |
| `cases` | One `CaseResult` per requested row, in CSV order; `result.case(case_id)` finds one |
| `summary` | `rows`, `status` and `basis` counts, total `steps`: the numbers the CLI prints |
| `warnings` | Warning texts in the order the CLI prints them as `warning: …` (the R4e header warning, intent disagreements) |
| `diagnostics.files` | Per manifest entry: `file_id`, `file_path`, `format`, the number of `observations` and `diagnostics` it produced |
| `diagnostics.parser` | The ingest diagnostics: `file_id`, `locator`, `severity` (`info`, `warning`, `error`), `reason`, `excerpt` (≤ 240 characters of the evidence line). They cover unparsed lines, missing evidence files, unsupported formats and parser errors |
| `diagnostics.coverage` | `ingest.coverage`: over the `files` with readable text, distinct resource ids found in the raw text (`tokens`), how many an observation represents (`represented`), the `unresolved` ones with line and excerpt, other id-like tokens by prefix (`other_id_tokens`, `other_examples`), and `unreadable` file ids |

**`CaseResult`**, copied from the CSV path, not recomputed:

| Field | Source |
|---|---|
| `case_id`, `deletion_status`, `steps` | Exactly the CSV row (`steps` is the `steps_json` array) |
| `trace` | `final.assemble`'s trace: `basis` (`FINDINGS`, `COMPLIANCE_PROVEN`, `BLOCKED`, `NO_ACCOUNT_PROOF`, `NO_COMPLIANT_EVIDENCE`, `NON_DELETION_REQUEST`), `blockers` (`resource:reason`), `system_from_m2_link` if used, and the intent readings `intent`, `intent_rule`, `intent_llm` (`None` without a cached reading) |
| `evaluations` | This request's M5 evaluations (`policy.Evaluation`), sorted by resource as `final.assemble` reads them: category and basis, `rule_id`, required `action`, `outcome` (`VIOLATION`, `COMPLIANT`, `INCONCLUSIVE`, `NOT_ATTRIBUTABLE`), `reason`, `underlying_outcome`, `attribution` (link evidence, M3 match and confidence, competing owners), `establishing` readings with locators, `citation` (`evidence_id`, `ref`, `locator`, `time`, `tie`), `notes` (assumed conditions and caveats), and `finding`, whose `establishing`, `contradicting`, `inadmissible` and `identifying` evidence are kept apart |
| `remediations` | The M5 remediations of this request's findings: operation, target state, rule, `permitted_because`, `establishing_evidence`, `caveats` |

A `NOT_APPLICABLE` row keeps its evaluations: the pipeline ran for it, but `final.assemble` doesn't use
them. The engine computes no confidence score or percentage, so the result has none. Missing evidence
shows up as `INCONCLUSIVE` evaluations, blockers and uncited verification steps, never as a citation.
`diagnostics.parser[].excerpt`, `coverage.unresolved[].excerpt` and the evaluations quote evidence and may
contain personal data. Treat a serialised result like the evidence itself.

**Outcomes and errors.**

| Situation | What the caller sees |
|---|---|
| Completed, no residual data | `AuditResult`; no row has `basis` `FINDINGS` |
| Completed with residual data | `AuditResult`; rows with `basis` `FINDINGS` and `FINDING` steps |
| Completed with warnings | `AuditResult` with non-empty `warnings` (and possibly `error` diagnostics for single evidence files, which the audit skips as before) |
| Invalid input or configuration | `inputs.InputError` raised, nothing written. `policy.PolicyError` and `settings.SettingsError` are subclasses; the message lists every problem with file and line |
| Internal failure | Any other exception (for example `final.AssemblyError`) propagates unchanged, nothing written |

The CLI maps these as before: `invalid policy:` / `invalid settings:` / `invalid input:` and exit 1 for
invalid input, a traceback and exit 1 for an internal failure, exit 2 for bad arguments. One message
changed in P3: requested rows missing from the dataset's request files raised `SystemExit` from inside the
audit. It is now an `InputError`, so the CLI prints it under `invalid input:`, still with exit 1.

## HTTP API

`code/api.py` (P4) is a second caller of `audit()`, beside the CLI. It adds no audit logic. The full
contract is in [`API.md`](API.md).

```text
ThreadingHTTPServer ─► Handler ─► handle(jobs, method, path, body)      routing, body parsing, JSON errors
                                    │
                                    ├─ resolve_dataset(root, dataset_id)  format check, resolve, must be a
                                    │                                     direct child of the root
                                    └─ Jobs.submit ─► ThreadPoolExecutor ─► Jobs._execute
                                                          engine.audit(<root>/<dataset_id>)
                                                          ─► AuditResult.to_dict() | sanitized error
```

- **Boundary.** Clients send a `dataset_id`, never a path. The API calls `engine.audit(dataset_dir)` with
  the library defaults (request file, policy, settings) and no `out`, so it writes nothing. `handle` is the
  whole HTTP contract as a plain function; the socket handler only reads the body and writes the response.
- **Execution.** A bounded thread pool (`TRACEZERO_API_WORKERS`) runs audits. The server itself is synchronous
  and threaded, so there is no event loop. Submissions beyond `TRACEZERO_API_MAX_PENDING` queued or running
  jobs get `503 busy`. The audits share the GIL: they interleave rather than run in parallel.
- **Job registry.** An in-process dict of job states behind one lock, which is held only for state reads and
  changes, never during an audit. Each job moves `queued → running → completed | failed`. `_execute` starts
  from a `failed`/`internal_error` outcome and records the real outcome in a `finally`, so a job can't stay
  `running`. The registry isn't persisted or shared: a restart loses it, and another process has its own.
- **Result contract.** A completed job's `result` is `AuditResult.to_dict()`. Only `dataset` and `requests`
  are rewritten, via `dataclasses.replace`, to name the dataset instead of the server path. Job fields
  (`audit_id`, `dataset_id`, `status`, `error`) sit beside `result`, never inside it. `InputError` becomes
  `invalid_input` with server paths redacted from its message. Any other exception becomes a fixed
  `internal_error`, and the log records only its type.

## Web interface

`web/` (P5) is a third caller, above the API. It never imports or reimplements engine code.

```text
browser (React)  ─► /api/*  ─► Vite dev/preview server  ─► http://127.0.0.1:8000  (code/api.py)
  src/api.ts          only fetch site: GET /health, GET /datasets, POST /audits, GET /audits/{id}
  src/App.tsx         health check, dataset list, submit, hash routes, polling of the open audit
  src/AuditView.tsx   job states, summary, request list      ┐ render AuditResult.to_dict() fields
  src/CaseView.tsx    request investigation                  │ as returned; tones and glosses only
  src/Diagnostics.tsx files, parser diagnostics, coverage    ┘ (code/DESIGN.md, Web interface)
```

- **Boundary.** The browser sends only a `dataset_id` it got from `GET /datasets`. Everything it shows is a field of
  the job or of `AuditResult.to_dict()` (`src/types.ts` mirrors it). The only numbers computed are list lengths
  (steps per phase, blockers). Status, basis and every count in the summary strip are the engine's.
- **Transport.** Same-origin `/api`, forwarded by Vite's proxy (`TRACEZERO_API_URL`). The API has no CORS headers,
  and the client adds none of its own. `VITE_TRACEZERO_API_BASE` is the one place the base URL is set.
- **Polling.** `pollAudit` sends one `GET /audits/{id}`, waits for the answer, then waits 1 s, until the status is
  `completed` or `failed`. It gives up after 10 minutes. It runs only for the audit in the URL and is aborted when
  the user navigates away. Final jobs are cached for the page's lifetime. A `404 audit_not_found` is shown as
  lost on restart, and a network failure as the API being unreachable, never as an audit outcome.
- **State.** No persistence beyond the tab: the list of audits submitted from it is in `sessionStorage`. The
  jobs themselves live, and die, in the API process.

## Modules

| Module | Responsibility |
|---|---|
| `code/main.py` | CLI: argument parsing, policy/settings loading, stderr warnings and exit codes, `--refresh-intent-cache` |
| `code/api.py` | HTTP API (P4): configuration, dataset-id resolution, in-memory job registry and thread pool, JSON errors |
| `audit/engine.py` | Library entry point `audit()`: validation, `pipeline.run`, intent and `final.assemble` per row, `AuditResult`, CSV writing |
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
  94 evidence files). `audit()` runs one whole dataset per call; there is no incremental or per-request API.
- **Known accuracy gaps** against the reference answers: status accuracy 0.875 and step F1 0.907 on 32
  sample cases. `case_61` is an evidence-gap row (`INCOMPLETE` with no steps).
