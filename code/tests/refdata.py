"""Where the corpus tests find the private reference dataset, and what happens when it is missing.

TRACEZERO_DATASET=<dir> points at the reference dataset root (the directory that holds
deletion_requests.csv, evidence_files.csv and evidence/). Unset, ./dataset at the repository root is used.
The corpus tests' expectations were recorded with both caches present, so they also need code/cache/ocr/
and code/cache/llm/intent.json (the engine reads those from fixed paths).

- Nothing configured and no ./dataset: those tests skip, saying what to set.
- TRACEZERO_DATASET set but unusable: those tests fail with the list of missing files; nothing falls back.
Tests without the decorator (synthetic data only) always run.
"""
import functools
import os
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
ENV = "TRACEZERO_DATASET"
FILES = ("deletion_requests.csv", "sample_deletion_requests.csv", "sample_output.csv", "evidence_files.csv",
         "user_identities.csv", "user_audit_log.csv", "system_activity.csv", "evidence")
CACHES = (CODE / "cache" / "ocr", CODE / "cache" / "llm" / "intent.json")

CONFIGURED = os.environ.get(ENV)
DATASET = Path(CONFIGURED).expanduser().resolve() if CONFIGURED else CODE.parent / "dataset"


def problems():
    if not DATASET.is_dir():
        return [f"{DATASET} is not a directory"]
    missing = [f"{DATASET / n}" for n in FILES if not (DATASET / n).exists()]
    missing += [str(p) for p in CACHES if not (p.exists() and (p.is_file() or any(p.glob("*.json"))))]
    return missing


PROBLEMS = problems()


def requires_reference(obj):
    """Decorate a test or TestCase class that needs the reference dataset and both caches."""
    if not PROBLEMS:
        return obj
    if not CONFIGURED:
        return unittest.skip(f"reference dataset not available; set {ENV}=<dataset dir> "
                             f"(missing: {'; '.join(PROBLEMS)})")(obj)

    def fail(*_, **__):  # explicitly configured but unusable: report it, never fall back
        raise AssertionError(f"{ENV}={CONFIGURED!r} is unusable; missing: {'; '.join(PROBLEMS)}")
    if isinstance(obj, type):
        obj.setUpClass = classmethod(fail)
        return obj
    return functools.wraps(obj)(fail)
