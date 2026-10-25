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
by the evidence SQL dump header. Changing them would change behaviour.

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

**Open decision for the owner:** TraceZero has no license yet, so it is all rights reserved by default.
Choose one before publishing or accepting contributions.

**Secrets.** None found. `ANTHROPIC_API_KEY` is read only from the environment by the anthropic client,
and `.env` / `.env.*` are git-ignored.
