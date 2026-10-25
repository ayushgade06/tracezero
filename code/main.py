"""TraceZero - deletion-audit CLI.

Usage: python code/main.py [--dataset dataset] [--requests dataset/deletion_requests.csv] [--out output.csv]

Runs M1-M5 once over the whole dataset (sample and evaluation requests are matched jointly, see
audit/pipeline.py), then assembles each requested case's deletion_status and steps_json from the M5
evaluations (audit/final.py) and writes the rows in input order (ascending case number).
Deterministic; no network access and no model calls (Claude's intent readings come from the
committed cache code/cache/llm, filled by --refresh-intent-cache). Screenshots are read from the
committed OCR cache (code/cache/ocr); the OCR engine is only needed to regenerate that cache.
"""
import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

from audit import final, intent, llm_intent, pipeline
from audit.matching import _ts
from audit.output import write_output
from audit.state import _iso

ROOT = Path(__file__).resolve().parents[1]


def audit_cases(requests, result):
    """-> [(case_id, status, steps, trace)] for the requested rows, from one pipeline result."""
    known = {r["case_id"] for r in result["rows"]}
    missing = [r["case_id"] for r in requests if r["case_id"] not in known]
    if missing:
        raise SystemExit(f"requests not in the dataset's request files (needed for joint matching): {missing[:5]}")
    evaluations = defaultdict(list)
    for e in result["E"]:
        evaluations[e.case_id].append(e)
    remediation_of = {r.finding_id: r for r in result["R"]}
    fallback = final.link_systems(result["links"])
    llm = llm_intent.labels({r["request_text"] for r in requests})
    out = []
    for req in requests:  # input order is kept: case_33..case_550 ascending by case number
        rule, read = intent.classify(req["request_text"]), llm[req["request_text"]]
        label = intent.decide(rule, read)
        if label == intent.AMBIGUOUS:
            print(f"warning: ambiguous intent audited as deletion: {req['case_id']}", file=sys.stderr)
        elif read not in (None, rule):
            print(f"warning: rule {rule} / LLM {read} -> {label}: {req['case_id']}", file=sys.stderr)
        status, steps, trace = final.assemble(req["case_id"], label, evaluations[req["case_id"]], remediation_of,
                                              _iso(_ts(req["audit_at"])), fallback)
        out.append((req["case_id"], status, steps, dict(trace, intent=label, intent_rule=rule, intent_llm=read)))
    return out


def run(dataset, requests_path, out_path):
    result = pipeline.run(dataset)
    rows = audit_cases(pipeline.read_requests(requests_path), result)
    write_output([(c, s, steps) for c, s, steps, _ in rows], out_path)
    return rows


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", default=ROOT / "dataset", type=Path)
    p.add_argument("--requests", default=None, type=Path, help="default: <dataset>/deletion_requests.csv")
    p.add_argument("--out", default=ROOT / "output.csv", type=Path)
    p.add_argument("--refresh-intent-cache", action="store_true",
                   help="classify uncached request texts with Claude first (needs ANTHROPIC_API_KEY)")
    a = p.parse_args(argv)
    if a.refresh_intent_cache:
        texts = [r["request_text"] for name in pipeline.REQUEST_FILES
                 for r in pipeline.read_requests(a.dataset / name)]
        print(f"intent cache: {llm_intent.refresh(texts)} Claude calls; usage {llm_intent.usage()}")
    rows = run(a.dataset, a.requests or a.dataset / "deletion_requests.csv", a.out)
    print(f"wrote {len(rows)} rows to {a.out}; status={dict(Counter(r[1] for r in rows))}; "
          f"basis={dict(Counter(r[3]['basis'] for r in rows))}; steps={sum(len(r[2]) for r in rows)}")


if __name__ == "__main__":
    main()
