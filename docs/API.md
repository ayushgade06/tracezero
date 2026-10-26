# HTTP API (P4)

A minimal HTTP API over the audit library, in [`code/api.py`](../code/api.py). It is for **local and
development use**. It has no authentication, keeps jobs in memory, and returns results that quote evidence
and may contain personal data. Do not expose it to untrusted networks.

Every audit is one call to `audit.engine.audit`, the function the CLI calls (see
[`ARCHITECTURE.md`](ARCHITECTURE.md#http-api)). The API never parses evidence, evaluates policy or computes
summaries itself.

## Start

From a source checkout, after `python -m pip install -e .` (see the [README](../README.md#quick-start)):

```bash
python code/api.py                                   # http://127.0.0.1:8000, datasets in code/tests/fixtures
TRACEZERO_API_DATASETS=/srv/tracezero/datasets python code/api.py
```

PowerShell: `$env:TRACEZERO_API_DATASETS = "D:\datasets"; python code/api.py`. The server uses only the
standard library, so it needs no extra install. If the port is taken (for example by another API instance),
it exits 1 with `cannot listen on <host>:<port>: …`.

Stop it with Ctrl+C. It stops accepting requests at once and drops queued audits. The process exits when any
running audit finishes (a reference-sized audit takes about 13 s).

| Variable | Default | Meaning |
|---|---|---|
| `TRACEZERO_API_DATASETS` | `code/tests/fixtures` (holds the synthetic `demo`) | Dataset root. Each direct subdirectory is a dataset, named by its directory name |
| `TRACEZERO_API_HOST` | `127.0.0.1` | Bind address. Any other value makes the server reachable from other machines, with no authentication; it logs a warning |
| `TRACEZERO_API_PORT` | `8000` | Port, 1–65535 |
| `TRACEZERO_API_WORKERS` | `2` | Audits that run at the same time, 1–32 |
| `TRACEZERO_API_MAX_PENDING` | `4 × workers` | Queued plus running audits accepted before `POST /audits` answers `503 busy`. At least `workers` |

The configuration is checked at startup. An invalid value stops the server with exit 1 and
`invalid configuration: …`. No output directory is needed, because the API writes no files (see
[Data handling](#data-handling)).

## Endpoints

All bodies are JSON (`application/json; charset=utf-8`), including every error.

### `GET /health`

`200 {"status": "ok"}`. The process is alive and serving requests. It says nothing about whether a dataset
is valid or an audit would succeed.

### `GET /datasets`

Added in P5 for the web interface. `200`:

```json
{"datasets": [{"dataset_id": "demo"}]}
```

The ids, sorted, of the dataset root's direct subdirectories that `POST /audits` would accept and that
contain `deletion_requests.csv`, the request file the API audits. Each entry goes through the same check
as a submission: the name must match the `dataset_id` format, and the entry is resolved, following symlinks
and junctions, and must land directly inside the root. Anything else (files, other names, directories
without the request file, links that leave the root) is left out silently. Only ids are returned: no paths,
sizes, times or file contents. Listing doesn't validate a dataset's contents, so a listed dataset can still
end `failed` with `invalid_input`. An unreadable root (for example, deleted after startup) answers
`503 datasets_unavailable`, without the path.

### `POST /audits`

```http
POST /audits
Content-Type: application/json

{"dataset_id": "demo"}
```

`202 Accepted`, with a `Location: /audits/<audit_id>` header:

```json
{"audit_id": "1e338c8e96514a19a9a76cc39d015a45", "status": "queued"}
```

`dataset_id` is the only accepted field. It must be a string of 1–64 letters, digits, `.`, `_` or `-` that
starts and ends with a letter or digit, so it can't contain a path separator, a drive or `..`. It is
resolved beneath the dataset root, following symlinks and junctions, and the result must be a directory
directly inside the root. The audit uses the dataset's `deletion_requests.csv` (the library default),
the default policy and the default settings. Clients can't choose a policy, settings, a request file or an
output path; sending any other field is an `invalid_request`. The submission response always says
`queued`. The audit starts as soon as a worker is free, and the dataset's files are checked then, by the
library: a dataset that exists but is invalid is accepted here and ends `failed` with `invalid_input`.

### `GET /audits/{audit_id}`

`200`, with the job's state:

```json
{"audit_id": "1e33…5a45", "dataset_id": "demo", "status": "running"}
{"audit_id": "1e33…5a45", "dataset_id": "demo", "status": "completed", "result": {"dataset": "demo", "requests": "demo/deletion_requests.csv", "output": null, "cases": […], "summary": {"rows": 4, "status": {"INCOMPLETE": 2, "COMPLETE": 1, "NOT_APPLICABLE": 1}, "basis": {…}, "steps": 13}, "warnings": […], "diagnostics": {…}}}
{"audit_id": "1e33…5a45", "dataset_id": "empty", "status": "failed", "error": {"code": "invalid_input", "message": "empty: no request file; expected at least one of ['sample_deletion_requests.csv', 'deletion_requests.csv']"}}
```

| `status` | Meaning | Extra field |
|---|---|---|
| `queued` | Accepted; waiting for a free worker | none |
| `running` | `audit()` is executing | none |
| `completed` | `audit()` returned | `result` |
| `failed` | `audit()` raised, or its result couldn't be serialised | `error` |

A job moves `queued → running → completed | failed` and never back. `completed` and `failed` are final, and a
job never stays `running` after its audit returns or raises.

`result` is `AuditResult.to_dict()`, whose schema is in [`ARCHITECTURE.md`](ARCHITECTURE.md#library-interface).
The only change: its two path fields, `dataset` and `requests`, name the dataset (`demo`,
`demo/deletion_requests.csv`) instead of the server's directory. Cases, statuses, steps, traces,
evaluations, summary counts, warnings and diagnostics are exactly the library's. `output` is always `null`.
The cases' `deletion_status` and `steps` are the CLI's CSV rows: writing them with `audit.output.write_output`
gives a byte-identical `output.csv` (tested). There is no CSV endpoint.

### Errors

```json
{"error": {"code": "invalid_dataset_id", "message": "dataset_id must be a string"}}
```

| HTTP | `code` | When |
|---|---|---|
| 400 | `invalid_json` | Body missing, not UTF-8, not JSON, or not a JSON object |
| 400 | `invalid_request` | `dataset_id` missing, an unexpected field, or an invalid `Content-Length` |
| 400 | `invalid_dataset_id` | `dataset_id` is not a string, is empty, or doesn't match the format (traversal, absolute paths, separators) |
| 404 | `dataset_not_found` | No such directory directly in the dataset root, including a symlink or junction that resolves outside it |
| 403 | `host_not_allowed` | The `Host` header is neither `localhost` nor an IP literal (DNS-rebinding protection; any port is accepted). A request without `Host` is served |
| 404 | `audit_not_found` | Unknown or malformed audit id, an id dropped by retention (see [Execution model](#execution-model)), or any id from before a restart |
| 404 | `not_found` | Unknown endpoint |
| 405 | `method_not_allowed` | Known endpoint, wrong method |
| 413 | `payload_too_large` | Body over 64 KiB |
| 503 | `datasets_unavailable` | `GET /datasets` can't read the dataset root |
| 503 | `busy` | `TRACEZERO_API_MAX_PENDING` audits are already queued or running, or the server is shutting down |
| 500 | `internal_error` | A failure in the HTTP layer itself |

Errors of a failed job, under `"error"` in `GET /audits/{id}`:

| `code` | When | `message` |
|---|---|---|
| `invalid_input` | The library raised `InputError`, including `PolicyError` and `SettingsError` (missing request file, bad CSV columns or values, …), or the evidence manifest names a file outside the dataset directory (see [Data handling](#data-handling)) | The library's message, listing every problem with file and line. Server paths are replaced by the dataset id (or `<datasets>`). It can quote values from the dataset's CSV files |
| `internal_error` | Any other exception | Fixed text: `the audit failed with an internal error`. The server logs the audit id and the exception type only, never its message or traceback, because both can quote evidence |

## Execution model

- `ThreadingHTTPServer` from the standard library serves each request on its own thread. It is synchronous,
  so there is no event loop to block.
- Audits run on a `ThreadPoolExecutor` with `TRACEZERO_API_WORKERS` threads. A request handler only validates
  the input, registers the job and returns.
- The registry is a dict behind one lock. The lock is held only to read or change job state, never while
  an audit runs, so polling and `/health` stay responsive during an audit.
- Each audit gets its own arguments. The library writes no module state (policy and settings travel as
  arguments, the default ones are never mutated), so concurrent audits don't affect each other
  (`code/tests/test_p4_api.py` runs two at once and compares each with a sequential run).
- Threads share Python's GIL, so concurrent audits interleave rather than run in parallel. A reference
  run takes about 13 s on its own.
- Audit ids are random UUID4 hex strings (32 characters).
- Retention: the registry keeps every queued and running job and the 32 most recently finished ones
  (`KEEP_FINISHED`), about 140 MiB at most for reference-sized results. Finishing a 33rd drops the one that
  finished earliest, so its id answers `404 audit_not_found`. Queued and running jobs are never dropped.
- A client that stalls for 30 s while sending a request is disconnected. A body over 64 KiB but at most 1 MiB is
  read and discarded before the `413`, so the client receives it rather than a connection reset.

## Data handling

- Clients name datasets and never send paths. Containment is checked after resolving symlinks.
- The datasets themselves are operator-supplied content. The engine reads each evidence file at the path
  its `evidence_files.csv` gives, relative to the dataset. Before an audit, the API resolves every manifest
  `file_path`, following symlinks and junctions. If any lands outside the dataset directory (`..`, an absolute
  path elsewhere, a link out), the job fails with `invalid_input` and nothing is read. The CLI and library keep
  the engine's behaviour and read what the manifest names. Datasets can still hold arbitrary content within
  their own directory, so only put datasets you trust under the root.
- Server paths are redacted from every string in a result, not only from `invalid_input` messages. For
  example, an `OSError` quoted in a parser diagnostic names the file relative to the dataset id.
- The API calls `audit()` without `out`, so nothing is written: no CSV, no cache, no change to any dataset,
  including the demo (tested).
- Results quote evidence (`diagnostics.parser[].excerpt`, `coverage.unresolved[].excerpt`, evaluations)
  and may contain personal data. Anyone who can reach the port and has an audit id can read its result.
  Ids are hard to guess, but that is not access control. There is no endpoint that lists audits.
  `GET /datasets` lists dataset ids, which are directory names, so name dataset directories accordingly.
- Routine logs contain the request line (method, path, status), the audit id and its final status, and
  for failures the exception type. They never contain request bodies, evidence or results.

## Limitations

- Jobs and results are kept in memory, the 32 most recently finished ones, until the process exits. A restart
  loses all of them. A reference-sized result is about 4.4 MiB of JSON.
- Separate server processes (several instances or workers behind a proxy) don't share the registry. An id
  from one process is unknown to another.
- No authentication, authorization, TLS or rate limiting. Run it on `127.0.0.1` or behind
  something that provides them.
- No dataset upload. Datasets are directories that the operator places under the root.
- Policy, settings and the request file are always the engine defaults. The API has no setting for a
  custom policy or settings file yet.
- The independent output validator (`evaluation/validate_output.py`) is not run by the API.
- Not production-ready. See [`ROADMAP.md`](ROADMAP.md) for what a deployment phase would add.
- Bound to another address with `TRACEZERO_API_HOST`, the API serves clients that use its IP address, not a
  host name, because only `localhost` and IP-literal `Host` headers are accepted.

## Tests

```bash
python -m unittest discover -s code/tests -p "test_p4_*.py"
```

30 tests, on temporary copies of the demo dataset (plus 7 for `GET /datasets` in `test_p5_datasets.py`,
`-p "test_p5_*.py"`, and 12 for the P6 hardening in `test_p6_api.py`, `-p "test_p6_*.py"`). The API tests call `api.handle` and `api.Jobs`
directly, and one class runs a real server on `127.0.0.1` with an OS-chosen port. Concurrency is controlled
with barriers and events, not sleeps. One test, gated like the corpus tests, runs the reference dataset
through the API and checks the guardrail hash.
