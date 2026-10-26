"""TraceZero - deletion-audit CLI.

Usage: python code/main.py [--dataset dataset] [--requests dataset/deletion_requests.csv] [--out output.csv]
                           [--policy deletion_rules.csv] [--settings settings.json]

Runs M1-M5 once over the whole dataset (sample and evaluation requests are matched jointly, see
audit/pipeline.py), then assembles each requested case's deletion_status and steps_json from the M5
evaluations (audit/final.py) and writes the rows in input order (ascending case number).
Deterministic; no network access and no model calls (Claude's intent readings come from the
committed cache code/cache/llm, filled by --refresh-intent-cache). Screenshots are read from the
committed OCR cache (code/cache/ocr); the OCR engine is only needed to regenerate that cache.
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

from audit import engine, llm_intent, pipeline, policy, settings

ROOT = Path(__file__).resolve().parents[1]


def run(dataset, requests_path, out_path, policy_matrix=None, conventions=settings.DEFAULT):
    """Audit through the library (audit/engine.py), print its warnings to stderr; -> [(case_id, status, steps, trace)]."""
    result = engine.audit(dataset, requests_path, out=out_path, policy=policy_matrix, settings=conventions)
    for w in result.warnings:
        print(f"warning: {w}", file=sys.stderr)
    return [(c.case_id, c.deletion_status, c.steps, c.trace) for c in result.cases]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", default=ROOT / "dataset", type=Path)
    p.add_argument("--requests", default=None, type=Path, help="default: <dataset>/deletion_requests.csv")
    p.add_argument("--out", default=ROOT / "output.csv", type=Path)
    p.add_argument("--policy", default=None, type=Path,
                   help="deletion policy CSV (default: code/audit/deletion_rules.csv; format in docs/CONFIGURATION.md)")
    p.add_argument("--settings", default=None, type=Path,
                   help="dataset-convention settings JSON (default: the reference conventions; see docs/CONFIGURATION.md)")
    p.add_argument("--refresh-intent-cache", action="store_true",
                   help="classify uncached request texts with Claude first (needs ANTHROPIC_API_KEY)")
    a = p.parse_args(argv)
    try:  # a custom policy or settings file is checked before any work, and never falls back to the default
        matrix = policy.load_policy(a.policy) if a.policy else None
        conventions = settings.load_settings(a.settings) if a.settings else settings.DEFAULT
    except policy.PolicyError as e:
        raise SystemExit(f"invalid policy:\n{e}")
    except settings.SettingsError as e:
        raise SystemExit(f"invalid settings:\n{e}")
    try:
        if a.refresh_intent_cache:
            texts = [r["request_text"] for path in pipeline.request_files(a.dataset) for r in pipeline.read_requests(path)]
            print(f"intent cache: {llm_intent.refresh(texts)} Claude calls; usage {llm_intent.usage()}")
        rows = run(a.dataset, a.requests or a.dataset / "deletion_requests.csv", a.out, matrix, conventions)
    except pipeline.InputError as e:
        raise SystemExit(f"invalid input:\n{e}")
    print(f"wrote {len(rows)} rows to {a.out}; status={dict(Counter(r[1] for r in rows))}; "
          f"basis={dict(Counter(r[3]['basis'] for r in rows))}; steps={sum(len(r[2]) for r in rows)}")


if __name__ == "__main__":
    main()
