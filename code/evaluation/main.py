"""Evaluation runner (M7): scores the serialized output, never in-memory objects.

Usage: python code/evaluation/main.py [--output output.csv] [--dataset dataset] [--json metrics.json]

Three separate kinds of result, never interchangeable:
  1. STRUCTURAL   the strict validator on output.csv (518 rows) and on the sample predictions.
  2. AGREEMENT    sample predictions (code/main.py run on sample_deletion_requests.csv, same joint
                  pipeline run) compared with dataset/sample_output.csv. The sample answers are read
                  here only, as references; nothing in the generator reads them.
  3. EVIDENCE     checks that need no labels: every cited file exists, predates audit_at (validator),
                  and is grounded (its parsed content names the cited resource), plus the custom cases.
Metric definitions (ours; the grader's are hidden):
  status accuracy        share of sample cases whose deletion_status equals the reference
  step P/R/F1            micro-averaged over steps as multisets of (phase, operation, state, system_id,
                         resource_id, evidence_id) per case; step_no ignored; also without evidence_id
  field accuracy         steps aligned by (phase, resource_id) present in both; share of aligned steps
                         whose system_id / operation / state / evidence_id match; coverage = aligned /
                         reference steps
  exact sequence         share of cases whose full step list (step_no included) is identical
  normalized edit dist.  Levenshtein distance between the ordered step-tuple lists / max(len), mean per
                         case (0 = identical, 1 = nothing in common)
"""
import argparse
import csv
import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(1, str(ROOT / "code"))  # after this directory: "main" stays this file

import custom_cases  # noqa: E402
import validate_output  # noqa: E402

FIELDS = ("phase", "operation", "state", "system_id", "resource_id", "evidence_id")


def read_output(path):
    with open(path, encoding="utf-8", newline="") as f:
        return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in csv.DictReader(f)}


def _tuple(s, with_evidence=True):
    return tuple(s[k] for k in FIELDS if with_evidence or k != "evidence_id")


def _prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(2 * p * r / (p + r), 4) if p + r else 0.0,
            "tp": tp, "fp": fp, "fn": fn}


def _levenshtein(a, b):
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def score(pred, gold):
    """Agreement of predictions with reference answers (both {case_id: (status, steps)})."""
    cases = sorted(gold, key=lambda c: int(c.split("_")[1]))
    m = {"cases": len(cases), "missing_predictions": [c for c in cases if c not in pred]}
    p = {c: pred.get(c, ("<missing>", [])) for c in cases}
    m["status_accuracy"] = round(sum(p[c][0] == gold[c][0] for c in cases) / len(cases), 4)
    m["status_confusion"] = {f"{g}->{q}": n for (g, q), n in sorted(Counter((gold[c][0], p[c][0]) for c in cases).items())}
    for label, ev in (("steps", True), ("steps_without_evidence_id", False)):
        tp = fp = fn = 0
        for c in cases:
            P, G = Counter(_tuple(s, ev) for s in p[c][1]), Counter(_tuple(s, ev) for s in gold[c][1])
            hit = sum((P & G).values())
            tp, fp, fn = tp + hit, fp + sum(P.values()) - hit, fn + sum(G.values()) - hit
        m[label] = _prf(tp, fp, fn)
    aligned, same, ref = 0, Counter(), 0
    for c in cases:
        P = {(s["phase"], s["resource_id"]): s for s in p[c][1]}
        G = {(s["phase"], s["resource_id"]): s for s in gold[c][1]}
        ref += len(G)
        for k in P.keys() & G.keys():
            aligned += 1
            for f in ("system_id", "operation", "state", "evidence_id"):
                same[f] += P[k][f] == G[k][f]
    m["field_accuracy"] = {f: round(same[f] / aligned, 4) if aligned else 0.0 for f in ("system_id", "operation", "state", "evidence_id")}
    m["field_alignment"] = {"aligned_steps": aligned, "reference_steps": ref, "coverage": round(aligned / ref, 4) if ref else 0.0}
    m["exact_sequence_accuracy"] = round(sum(p[c][1] == gold[c][1] for c in cases) / len(cases), 4)
    dist = [_levenshtein([_tuple(s) for s in p[c][1]], [_tuple(s) for s in gold[c][1]]) / max(len(p[c][1]), len(gold[c][1]), 1)
            for c in cases]
    m["mean_normalized_edit_distance"] = round(sum(dist) / len(dist), 4)
    for status in ("COMPLETE", "INCOMPLETE"):
        sub = [c for c in cases if gold[c][0] == status]
        m[f"status_accuracy_on_reference_{status}"] = f"{sum(p[c][0] == status for c in sub)}/{len(sub)}"
    return m


def grounding(rows, dataset):
    """Cited files whose parsed observations name the cited resource (direct or related mention)."""
    from audit import ingest
    _, obs, _ = ingest.ingest(dataset)
    named = {}
    for o in obs:
        ids = set(o.related_resource_ids or ()) | ({o.resource_id} if o.resource_id else set())
        named.setdefault(o.file_id, set()).update(ids)
    out = Counter()
    for status, steps in rows.values():
        for s in steps:
            if s["evidence_id"]:
                out[(status, s["phase"], s["resource_id"] in named.get(s["evidence_id"], ()))] += 1
    return {f"{st} {ph} {'names resource' if ok else 'does not name resource'}": n for (st, ph, ok), n in sorted(out.items())}


def summary(rows):
    steps = [s for _, st in rows.values() for s in st]
    return {"rows": len(rows), "status": dict(Counter(s for s, _ in rows.values())),
            "rows_with_findings": sum(any(s["phase"] == "FINDING" for s in st) for _, st in rows.values()),
            "incomplete_without_findings": sum(s == "INCOMPLETE" and not any(x["phase"] == "FINDING" for x in st) for s, st in rows.values()),
            "empty_incomplete_rows": sorted((c for c, (s, st) in rows.items() if s == "INCOMPLETE" and not st), key=lambda c: int(c.split("_")[1])),
            "steps": len(steps), "steps_by_phase": dict(Counter(s["phase"] for s in steps)),
            "operations": dict(sorted(Counter(s["operation"] for s in steps).items())),
            "cited_steps": sum(bool(s["evidence_id"]) for s in steps)}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--output", default=ROOT / "output.csv", type=Path)
    p.add_argument("--dataset", default=ROOT / "dataset", type=Path)
    p.add_argument("--json", type=Path, help="also write all metrics as JSON")
    a = p.parse_args(argv)
    metrics = {}

    errors, warnings = validate_output.validate(a.output, a.dataset / "deletion_requests.csv", strict=True)
    rows = read_output(a.output)
    metrics["output"] = dict(summary(rows), strict_errors=errors, strict_warnings=warnings)

    with tempfile.TemporaryDirectory() as d:
        sample_pred = Path(d) / "sample_predictions.csv"
        subprocess.run([sys.executable, str(ROOT / "code" / "main.py"), "--dataset", str(a.dataset),
                        "--requests", str(a.dataset / "sample_deletion_requests.csv"), "--out", str(sample_pred)],
                       check=True, capture_output=True)
        s_errors, s_warnings = validate_output.validate(sample_pred, a.dataset / "sample_deletion_requests.csv", strict=True)
        pred = read_output(sample_pred)
    gold = read_output(a.dataset / "sample_output.csv")  # reference answers: evaluation only
    metrics["samples"] = dict(score(pred, gold), strict_errors=s_errors, strict_warnings=s_warnings, prediction_summary=summary(pred))
    metrics["grounding"] = grounding(rows, a.dataset)
    metrics["custom_cases"] = [{k: r[k] for k in ("name", "passed", "basis", "blockers", "validator_errors", "expected", "actual")}
                               for r in custom_cases.run_all()]

    o, s = metrics["output"], metrics["samples"]
    print(f"STRUCTURAL output.csv: {o['rows']} rows, strict errors {len(errors)}, warnings {warnings}")
    print(f"  status {o['status']}; rows with findings {o['rows_with_findings']}; INCOMPLETE without findings "
          f"{o['incomplete_without_findings']} (empty: {o['empty_incomplete_rows']}); steps {o['steps']} {o['steps_by_phase']}")
    print(f"STRUCTURAL sample predictions: strict errors {len(s_errors)}, warnings {s_warnings}")
    print(f"AGREEMENT on {s['cases']} sample cases: status accuracy {s['status_accuracy']} {s['status_confusion']}")
    print(f"  steps P/R/F1 {s['steps']}\n  without evidence_id {s['steps_without_evidence_id']}")
    print(f"  field accuracy {s['field_accuracy']} alignment {s['field_alignment']}")
    print(f"  exact sequence accuracy {s['exact_sequence_accuracy']}; mean normalized edit distance {s['mean_normalized_edit_distance']}")
    print(f"EVIDENCE grounding of cited steps: {metrics['grounding']}")
    cc = metrics["custom_cases"]
    print(f"EVIDENCE custom cases: {sum(c['passed'] for c in cc)}/{len(cc)} passed")
    for c in cc:
        print(f"  {'PASS' if c['passed'] else 'FAIL'} {c['name']}: {c['actual'][0]} ({c['basis']})")
    if a.json:
        a.json.write_text(json.dumps(metrics, indent=1, sort_keys=True), encoding="utf-8")
    ok = not errors and not s_errors and all(c["passed"] for c in cc)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
