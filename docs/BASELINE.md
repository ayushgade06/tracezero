# P0 baseline

Recorded 2026-10-25 before any functional change. Every number below comes from an actual run; nothing is
estimated.

## Environment

| Item | Value |
|---|---|
| OS | Windows 11, Git Bash |
| Python | 3.12.6 (`C:\Python312\python.exe`) |
| pypdf / scipy / numpy | 6.14.2 / 1.17.1 / 2.4.3 |
| anthropic | 2.3.0 installed, not used (no refresh run, no API key needed) |
| rapidocr-onnxruntime | not installed: the live OCR test is skipped and the committed OCR cache is used |
| `PYTHONIOENCODING` | `utf-8` (set for all runs) |

Runs used a scratch copy of the original implementation, with its reference dataset and caches, so its
existing `output.csv` was not overwritten.

## Results: original implementation

| # | Command | Result | Notes |
|---|---|---|---|
| 1 | `python code/main.py --out <tmp>/out_run1.csv` | PASS (exit 0, ~13 s) | 518 rows; `INCOMPLETE` 415, `COMPLETE` 93, `NOT_APPLICABLE` 10; 3043 steps; nothing on stderr |
| 2 | Same again, then `sha256sum` | PASS | Both runs and the pre-existing output: `3e8aa75b6a771a8a8e76936fdce1f5b2b5239fcf12c431769dd94b943a969db1` |
| 3 | `python code/evaluation/validate_output.py --output <tmp>/out_run1.csv --strict` | PASS | 0 errors, 1 warning: `case_61` is an evidence-gap row (`INCOMPLETE`, no steps) |
| 4 | `python code/evaluation/main.py --output <tmp>/out_run1.csv` | PASS | 32 sample cases: status accuracy 0.875; step P/R/F1 0.9595/0.8601/0.9071; field accuracy (system/operation/state/evidence) 1.000/0.994/0.994/0.965; exact sequence 0.469; custom cases 7/7 |
| 5 | `python code/evaluation/custom_cases.py` | PASS | 7/7 |
| 6 | `python -m unittest discover -s code/tests` | PASS | 180 tests, 1 skipped (`test_live_engine_matches_cache`: OCR engine not installed), ~125 s |

## Results: TraceZero tree after migration

| # | Setup | Result |
|---|---|---|
| 7 | Repository as committed, `--dataset` pointing at the external reference dataset, no caches | PASS (exit 0). Strict validation: 0 errors. All 518 statuses match the baseline. 6 rows lose 15 screenshot-derived steps (10 vs 25 steps), because without `code/cache/ocr` screenshots produce no observations. SHA-256 `e3aa4141…` |
| 8 | Scratch copy of the repository plus the dataset and both caches | PASS: SHA-256 `3e8aa75b…969db1`, byte-identical to the baseline |
| 9 | `python -m unittest discover -s code/tests` on setup 8 | PASS: 180 tests, 1 skipped |
| 10 | `python -m unittest discover -s code/tests` without dataset and OCR cache | FAIL as expected: 140 tests run, 20 errors (missing dataset files), 1 skipped. Errors: `setUpClass` of `CorpusTests` in test_m1–m7 and test_m61 (8), plus 12 tests that read `dataset/` or the OCR cache directly. `custom_cases.py` still passes 7/7 |

Not run: `--refresh-intent-cache` (needs an API key and spends credits) and `ocr_report.py --refresh-ocr`
(the engine isn't installed). Neither is needed to reproduce the baseline.

## Changes made during migration

Only comments, docstrings and help strings changed. No logic, constant or test changed:

- `code/main.py`: module docstring title (also the `--help` description)
- `audit/output.py`, `audit/policy.py`, `audit/state.py`: docstring wording of rule sources
- `evaluation/validate_output.py`: docstring, one function docstring, the argparse description
- `evaluation/custom_cases.py`, `matching_report.py`, `policy_report.py`, `state_report.py`: docstrings
- `evaluation/discovery_report.py`: one printed explanatory string
- `code/DESIGN.md`: two wording changes

Deliberately unchanged: `state.py:461` and the `test_m4.py:262` fixture, which match a literal field produced
by the evidence SQL dump header. Changing them would change behaviour. (P2 moved that literal, unchanged, to
`audit/settings.py` as the default of `sql_point_in_time_header`; the fixture is untouched.)

## Licensing and repository hygiene review

**Provenance (from the original repository's history).** Every file under `code/` was written by the
TraceZero author. The original repository's starter `code/main.py` and `code/evaluation/main.py` were empty
files. The dataset, the task description, the agent-instruction files and the original README framing
came from a third party.

**Licenses found.** None: there is no LICENSE, COPYING or NOTICE file, no SPDX header, and no
copyright notice anywhere in the original tree, so there is nothing to preserve or remove.

**Decision.**

| Material | Migrated | Why |
|---|---|---|
| `code/**/*.py`, `code/requirements*.txt`, `code/DESIGN.md` | Yes | Authored by the TraceZero author |
| `dataset/` (evidence, CSVs, reference answers) | No | Third-party material with no license; contains realistic personal data |
| Task description and agent-instruction files | No | Third-party, specific to the original setting |
| `code/cache/ocr/*.json` | No (git-ignored) | Raw OCR text of the third-party screenshots |
| `code/cache/llm/intent.json` | No (git-ignored) | Readings derived from the third-party request texts |
| `code/evaluation/evaluation_report.md`, `usage_report.md` | No | Generated reports about that dataset; superseded by this file |
| `output.csv`, `code.zip`, `log.txt`, `__pycache__/` | No | Generated output, archive, session log, bytecode |

**Third-party dependencies** are installed from PyPI and not vendored: pypdf (BSD-3-Clause), SciPy (BSD),
NumPy (BSD-3-Clause, with bundled 0BSD/MIT/Zlib/CC0 parts) and the anthropic SDK (Apache-2.0), as reported by
their installed package metadata. The optional rapidocr-onnxruntime and its bundled PP-OCR models are
Apache-2.0 according to their project pages (not installed here, so not checked locally). None requires an attribution file in this repository while they are not redistributed.

**Open decision for the owner (resolved in P1: Apache-2.0):** TraceZero has no license yet, so it is all rights reserved by default.
Choose one before publishing or accepting contributions.

**Secrets.** None found. `ANTHROPIC_API_KEY` is read only from the environment by the anthropic client,
and `.env` / `.env.*` are git-ignored.

## P1 verification (2026-10-26)

Audit engine unchanged: no file under `code/audit/` and no line of `code/main.py` changed in P1, and no
existing test assertion changed (test edits only add the dataset gate and pass the dataset path explicitly).

| # | Setup | Command | Result |
|---|---|---|---|
| 11 | Repository, no dataset, no caches, no env | `python -m unittest discover -s code/tests` | PASS: 187 tests, 53 skipped (`reference dataset not available; set TRACEZERO_DATASET=…`). Every test that passed in run 10 still passes |
| 12 | Same, `TRACEZERO_DATASET=/d/nope` | same | FAIL by design: the 15 gated tests and classes report `TRACEZERO_DATASET='D:/nope' is unusable; missing: …`; no fallback |
| 13 | Scratch copy plus both caches, `TRACEZERO_DATASET=<copy of reference dataset>`, no `./dataset` | same | PASS: 187 tests, 1 skipped (live OCR engine not installed) |
| 14 | Scratch copy plus caches and `./dataset`, no env | same | PASS: 187 tests, 1 skipped |
| 15 | Setup 14 | `python code/main.py`; `validate_output.py --strict` | SHA-256 `3e8aa75b…969db1` (unchanged); 0 errors, 1 warning (`case_61`) |
| 16 | Clean venv, Python 3.12.6, `pip install -e .` from a fresh clone | install; `tracezero …` | Resolved pypdf 6.21.2, scipy 1.18.1, numpy 2.5.4 (newer than the baseline's). `tracezero` matches `python code/main.py` byte for byte on the demo; exit codes match (0 ok, 2 bad argument, 1 missing dataset) |
| 17 | Setup 16 plus caches and dataset | `python code/main.py` and `tracezero` | Both SHA-256 `3e8aa75b…969db1`; suite 187 tests, 1 skipped |
| 18 | Clean venv, Python 3.13.7, `pip install -e .` | suite; demo; reference run via `tracezero --dataset` | Suite 187 tests, 53 skipped; demo byte-identical to 3.12; reference SHA-256 `3e8aa75b…969db1` |
| 19 | Clean venv, non-editable `pip install <clone>` | `tracezero --help` | Exits 1: `engine sources not found …; install from a source checkout with pip install -e .` |
| 20 | Docker `python:3.12` (3.12.15, Linux), fresh clone of HEAD | the CI workflow's steps | Install OK; 187 tests, 53 skipped; demo audit and strict validation PASS (0 errors, 0 warnings); demo output byte-identical to Windows (`699d7cec…`) |

Not observed: an actual GitHub Actions run, because nothing was pushed. Run 20 reproduces the workflow's
commands on the same base OS and Python minor version.

**Licensing (P1.6): resolved, Apache-2.0.** Git history attributes every file under `code/` to one author.
The owner confirmed holding the rights, with no restricting terms from where the code was first written,
and chose Apache-2.0. `LICENSE` is the official text from apache.org (SHA-256 `cfc7749b…523d30`, the
canonical file). `NOTICE` carries the copyright line, and `pyproject.toml` declares `license = "Apache-2.0"`.
It is compatible with the dependencies' licenses (BSD, Apache-2.0), none of which is vendored.

## P2 verification (2026-10-26)

P2 changes engine code only to take configuration, never to change a decision. `policy.POLICY` is loaded
from `code/audit/deletion_rules.csv`, and `test_p2_policy.py` pins it to the pre-P2 matrix, order included.
The R4e header literal and the `Northstar` template marker moved unchanged into `audit/settings.py` as
defaults. A missing sample request file is read as zero rows, like a header-only one. No pre-existing test
assertion changed.

| # | Setup | Command | Result |
|---|---|---|---|
| 21 | Repository, no dataset, no caches | `python -m unittest discover -s code/tests` | PASS: 232 tests, 53 skipped (45 new P2 tests: policy 19, request files 7, validation 13, settings 6) |
| 22 | Scratch copy plus both caches and the reference dataset at `./dataset` | `python code/main.py`; `validate_output.py --strict` | SHA-256 `3e8aa75b…969db1` (unchanged); 0 errors, 1 warning (`case_61`) |
| 23 | Setup 22 | `python code/main.py --policy code/audit/deletion_rules.csv --settings <{}>` | SHA-256 `3e8aa75b…969db1`: explicit defaults equal the implicit ones |
| 24 | Setup 22 | `python -m unittest discover -s code/tests` | PASS: 232 tests, 1 skipped (live OCR engine not installed) |
| 25 | Setup 22 | `--settings` with `screenshot_template_marker: null`; with `sql_point_in_time_header: null` | SHA-256 `e3aa4141…` (the same as run 7, no OCR cache: 15 fewer steps); and `c2afa3f2…` (20 `COMPLETE` become `INCOMPLETE`). Both settings are wired on the real path |
| 26 | Repository | the CI demo steps, run as `python code/main.py` | Demo output `699d7cec…`, byte-identical to P1; strict validation 0 errors, 0 warnings |

The guardrail was checked after each P2 commit (policy, request files, validation, settings), with the
same hash every time. Not observed: a GitHub Actions run (nothing pushed).

**P2 follow-up** (after the full P2 brief was available: a missing-header warning, expected columns in
errors, duplicate reporting in the policy loader, and more tests). Runs 21–26 were recorded at `c3dc9df`;
these are at `33867dc`:

| # | Setup | Command | Result |
|---|---|---|---|
| 27 | Repository, no dataset, no caches | full suite; `-p "test_p2_*.py"` | PASS: 238 tests, 53 skipped; P2 tests 51 (policy 20, request files 7, validation 16, settings 8) |
| 28 | Setup 22 | `python code/main.py`; strict validation; full suite | SHA-256 `3e8aa75b…969db1`; 0 errors, 1 warning (`case_61`); stderr empty (no R4e header warning); 238 tests, 1 skipped |
| 29 | Fresh clone of `33867dc`, clean venv, Windows, Python 3.12.6 | the CI workflow's steps (`pip install -e .`, unittest, `tracezero` demo, strict validation) | PASS: 238 tests, 53 skipped; demo `699d7cec…`; 0 errors, 0 warnings. Not a Linux container (Docker daemon not running) and not an actual GitHub Actions run |

## P3 verification (2026-10-26)

P3 moves the audit path from `code/main.py` into `audit/engine.py` (`audit()` → `AuditResult`) and makes the CLI
call it. No decision logic changed. The other engine edits: `PolicyError` and `SettingsError` now subclass
`InputError`, and `ingest.coverage` lists an unreadable evidence file instead of raising (it only feeds
diagnostics). Two test edits follow the moved function: `test_m7` calls `engine.audit_cases`, and
`test_p2_requests` expects `InputError` from `main.run` for rows missing from the request files, plus a new
check that the CLI still exits 1 naming the case. No other existing assertion changed.

| # | Setup | Command | Result |
|---|---|---|---|
| 30 | Repository, no dataset, no caches | full suite; `-p "test_p3_*.py"` | PASS: 259 tests, 53 skipped; 21 new P3 tests |
| 31 | Scratch copy plus both caches and the reference dataset at `./dataset` | `python code/main.py`; `validate_output.py --strict`; again with `--policy code/audit/deletion_rules.csv` | SHA-256 `3e8aa75b…969db1` (unchanged), both runs; 0 errors, 1 warning (`case_61`); stderr empty |
| 32 | Setup 31 | full suite | PASS: 259 tests, 1 skipped (live OCR engine not installed) |
| 33 | Setup 31, one Python process | `audit()` with defaults, then `Settings(sql_point_in_time_header=None)`, then defaults again | `3e8aa75b…`, `c2afa3f2…` (73 `COMPLETE`, as run 25), `3e8aa75b…`: no leakage between audits. `to_dict()` JSON: 4.4 MiB, identical across runs. Diagnostics: 0 warnings, 3 parser warnings, coverage 2531/2531 ids over 94 files, 0 unreadable |
| 34 | Clean venv, `pip install -e .`, another working directory | the README library example on the demo | Output `699d7cec…` (as P1/P2); strict validation 0 errors, 0 warnings |
| 35 | Repository | CLI exit codes | Bad argument 2; missing dataset 1; missing `--policy` file 1 (`invalid policy:`) |

The guardrail was checked on the exact tree of each P3 code commit (`e381413`, `d7f0087`). Cost: building
the structured result (evaluation dicts and the coverage scan) adds about 1.3 s to a reference run, measured
before the change (0.5 s and 0.8 s). Not observed: a GitHub Actions run (nothing pushed).
