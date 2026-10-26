# TraceZero roadmap

An incremental path from the current CLI to a maintained product with a backend API and a web interface.
Every phase keeps the audit engine's behaviour fixed unless that phase explicitly says otherwise.

**Standing guardrail (every phase).** On the reference dataset, which is kept locally and never committed,
`python code/main.py` with both caches must still produce SHA-256
`3e8aa75b6a771a8a8e76936fdce1f5b2b5239fcf12c431769dd94b943a969db1`, and strict validation must report 0
errors. A deliberate change of that hash needs a recorded reason and a new baseline entry in `BASELINE.md`.

> These phases are provisional. The phase list in the P0 brief was cut off, so they are proposed here and
> should be confirmed or replaced.

## P0: audit, baseline, migration (this phase, done)

- The original implementation was audited, and its baseline recorded in `BASELINE.md`.
- Source code was migrated without logic changes. Third-party data, caches and generated reports were
  excluded.
- README, `ARCHITECTURE.md` and this roadmap were added.

## P1: make the engine self-contained and testable

Goal: a fresh clone can run its full test suite and a demo audit with no external files.

- Build a small synthetic fixture dataset under `code/tests/fixtures/` from the generators in
  `evaluation/custom_cases.py`, covering each evidence format.
- Corpus tests skip, with a clear reason, when the reference dataset is absent. Add a
  `TRACEZERO_DATASET` environment variable so they can be pointed at it.
- Add packaging: `pyproject.toml`, a `tracezero` console script that wraps `code/main.py` unchanged, and
  pinned dependency ranges.
- Add CI: unit tests and the synthetic demo on every push.
- Choose and add a license (open decision from P0).

Exit: green CI on a clean clone; the guardrail hash is unchanged.

Status: done; the license is Apache-2.0. CI is verified locally in
a `python:3.12` container but has not yet run on GitHub (see `BASELINE.md`, P1).

## P2: separate engine from dataset conventions

Goal: an organisation can run TraceZero on its own evidence without editing code.

- Load the policy from `deletion_rules.csv`, or a documented equivalent, instead of the hard-coded
  `policy.POLICY`. A test asserts that the loaded matrix equals the current one.
- Make the second request file optional; keep joint matching whenever several request files are given.
- Resolve default paths (`./dataset`, `./output.csv`, `code/cache/`) from the working directory or settings
  instead of source-file locations. Found in P1: this is what currently limits installs to editable mode.
- Validate input schemas up front and report clear errors for missing columns or files.
- Move format-specific heuristics (for example, the SQL dump header key used by rule R4e, and the
  screenshot template) behind named, documented settings that default to today's behaviour.

Exit: the guardrail hash is unchanged with default settings; the fixture dataset runs with a custom policy file.

Status: done for policy, request files, validation and the two named heuristics (see `CONFIGURATION.md`
and `BASELINE.md`, P2). Default-path resolution (the third bullet) is not done: it was not in the P2 brief
and still limits installs to editable mode. The OCR template geometry and the name-based category maps
remain code.

## P3: library interface and structured results

Goal: one function the API and UI can call, with results that explain themselves.

- `audit(dataset, requests=None) -> AuditResult`, built on `pipeline.run` and `main.audit_cases`.
- Serialise the per-request trace that `final.assemble` already returns: evaluations, blockers, citations
  with locators, and intent readings. Do this alongside `output.csv`, not instead of it.
- Report diagnostics (unparsed lines, ingest coverage) as structured data.

Exit: the CLI is a thin wrapper over the library, and `output.csv` is byte-identical.

Status: done. The entry point is `audit.engine.audit(dataset, requests=None, *, out=None, policy=None,
settings=None)`; the result schema is in `ARCHITECTURE.md` and the verification in `BASELINE.md`, P3. Not
included: the independent validator's report (`evaluation/validate_output.py` still reads only the CSV)
and a structured error object (errors are raised; P4 decides how to report them over HTTP).

## P4: backend API

Goal: run audits over HTTP.

- A minimal service: upload or point at a dataset, start an audit job, poll its status, and fetch the
  results (CSV plus structured trace).
- Run jobs out of process. A full run takes about 13 s in memory, so requests must not block on it.
- Local file storage only; no database until a concrete need appears.
- Authentication, size limits and retention of uploaded evidence, which contains personal data, are
  designed here before any deployment.

Exit: an API integration test reproduces the guardrail output.

Status: partly done; the minimal API in `code/api.py` is described in `API.md`, and its verification is in
`BASELINE.md`, P4.

- Done: point at a dataset by id, beneath a server-configured root (`POST /audits`); poll status and
  fetch the structured result (`GET /audits/{id}`, `AuditResult.to_dict()`); validated configuration;
  JSON errors; a request-size limit; 30 tests. The API writes no files, so no storage is needed.
- Different from the bullets above: jobs run on an in-process thread pool, not out of process. The P4 brief
  asked for an in-process registry. Request handlers don't block on an audit, but concurrent audits share
  the GIL.
- Not done: dataset upload, a CSV download endpoint (the CSV rows are in the result's cases), authentication,
  and retention of uploaded evidence. Jobs are in memory and lost on restart.
- The exit test exists (`test_p4_api.ReferenceTests`). It skips without the reference dataset and has not
  run yet, because the dataset isn't available on this machine.

## P5: web interface

Goal: reviewers can inspect a finished audit.

- Job list, a per-request status table, and a step view where each citation opens the evidence locator
  it refers to.
- Read-only first; editing policy or requests comes later, if needed.

Status: done for local use; the client is `web/`, described in the README and `ARCHITECTURE.md`, and verified in
`BASELINE.md`, P5.

- Done: a read-only React client over the P4 API. It has a dataset list (new `GET /datasets`, ids only,
  containment-checked, 7 tests), submission and polling, the engine's summary, a filterable per-request table,
  and the request investigation. Every step, blocker and remediation links to its policy evaluation, which shows
  the citation's evidence id, manifest path, ref and locator, the establishing, contradicting and inadmissible
  readings, and attribution. A diagnostics view and 31 client tests complete it.
- Different from the bullets above: the "job list" covers only audits submitted from the current browser tab
  (`sessionStorage`), because the API keeps no listable history. A citation opens its locator (file path,
  table/row/line, JSON path), not the evidence content: the API serves no evidence files, and the result
  quotes evidence only in diagnostics.
- Not done: request fields (requester, dates, text), which `AuditResult` doesn't carry; evidence-file viewing;
  CSV download; authentication and any deployment beyond the local Vite server.

## P6: release readiness for local use

Goal: the local workflow (CLI, library, API, web client) is reproducible from a clean checkout and hardened
against the defects an audit of the whole path finds. No new features.

Status: done; verified in `BASELINE.md`, P6.

- Done: the API refuses datasets whose manifest names files outside the dataset, redacts server paths from
  results, keeps at most 32 finished jobs, rejects non-local `Host` headers (DNS rebinding), drops stalled
  clients, refuses an occupied port, and delivers its `413`. The web client keeps the ticking timer out of its
  live region, keeps a focus indicator in forced-colors mode, meets 3:1 for control borders and survives a
  malformed route. `demo_output.csv` is ignored. The README states scope and limitations.
- Not done: everything a shared or public deployment needs (next section).

## P7: production deployment (only when there is a deployment target)

- Persistence, multi-user access control, audit logging, deployment, and monitoring.
- Optional LLM intent refresh as an explicit, metered admin action.

## Known gaps carried forward (from ARCHITECTURE.md)

- Accuracy against the reference answers: status 0.875, step F1 0.907. Improvements belong in a dedicated,
  measured change that records a new baseline.
- In-memory, single-pass processing; scaling work waits for real input sizes.
