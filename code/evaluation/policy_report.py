"""M5 report: policy outcomes, findings, remediations, invariant checks, and (separately)
agreement with sample answers.

Usage: python code/evaluation/policy_report.py [--dataset dataset] [--examples 8] [--json evaluations.json]

Findings and remediations are produced first from the audit inputs only. sample_output.csv
is read afterwards, here, solely to report agreement; it never feeds the policy engine.
"""
import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "code" / "evaluation"))

from audit import ingest, matching, policy  # noqa: E402
import state_report  # noqa: E402


def run(dataset):
    manifest, obs, _ = ingest.ingest(dataset)
    assessments = state_report.run(dataset)
    ctx = policy.PolicyContext(obs, manifest, events=matching.load_events(dataset))
    return (manifest, assessments) + policy.run(assessments, ctx)


def invariants(manifest, assessments, evaluations, findings, remediations):
    """Evidence-based policy validation, independent of the sample answers."""
    captured = {m["file_id"]: m["captured_at"] for m in manifest}
    audit = {a.case_id: a.audit_at for a in assessments}
    fids = {f.finding_id for f in findings}
    problems = Counter()
    for f in findings:
        if not f.establishing or not f.citation:
            problems["finding without establishing evidence"] += 1
        for r in f.establishing:
            if captured.get(r["evidence_id"], "9999") > audit[f.case_id] or r["time"] > audit[f.case_id]:
                problems["establishing evidence after audit_at"] += 1
        if f.attribution["attribution"] not in policy.ATTRIBUTABLE:
            problems["finding on a non-attributable resource"] += 1
        if f.basis not in ("OBSERVED",):
            problems["finding from a non-observed basis"] += 1
    for r in remediations:
        if r.finding_id not in fids:
            problems["remediation without a finding"] += 1
        f = next(x for x in findings if x.finding_id == r.finding_id)
        action = policy.POLICY[f.category][1]
        if action in (policy.RETAIN, policy.REDACT, policy.ANONYMIZE) and r.operation == "DELETE_RECORD":
            problems["DELETE on a retain/redact/anonymize category"] += 1
        if action == policy.DELETE and r.operation in ("REDACT_RECORD", "ANONYMIZE_RECORD"):
            problems["redaction on a delete category"] += 1
    if len(fids) != len(findings):
        problems["duplicate finding ids"] += 1
    if len({(r.case_id, r.resource_id, r.operation) for r in remediations}) != len(remediations):
        problems["duplicate remediation"] += 1
    return problems


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", default=ROOT / "dataset", type=Path)
    p.add_argument("--examples", default=8, type=int)
    p.add_argument("--json", type=Path)
    a = p.parse_args(argv)
    manifest, A, E, F, R = run(a.dataset)

    print(f"Evaluations: {len(E)} over {len({e.case_id for e in E})} deletion requests")
    print(f"  outcomes: {dict(Counter(e.outcome for e in E))}")
    print(f"  category basis: {dict(Counter(e.category_basis for e in E))}")
    print("\nPolicy matrix in use (category -> rule, action) and outcomes:")
    for cat in sorted({e.category for e in E}, key=str):
        rule = policy.POLICY.get(cat, ("-", "-", "-", None))
        c = Counter(e.outcome for e in E if e.category == cat)
        print(f"  {str(cat):<20} {rule[0]:<28} {rule[1]:<9} {dict(c)}")
    print("\nInconclusive reasons:", dict(Counter(e.reason for e in E if e.outcome == "INCONCLUSIVE")))
    print("Not attributable:", dict(Counter(e.reason for e in E if e.outcome == "NOT_ATTRIBUTABLE")),
          f"(potential violations held back: {sum(1 for e in E if e.potential_violation)})")
    print(f"\nFindings: {len(F)} {dict(Counter((f.finding_type, f.conclusion) for f in F))}")
    print(f"Remediations: {len(R)} {dict(Counter(r.operation for r in R))}")
    print(f"  with caveats: {dict(Counter(c.split(':')[0] for r in R for c in r.caveats))}")
    per_req = Counter(f.case_id for f in F)
    print(f"Requests with >=1 finding: {len(per_req)}; without: {len({e.case_id for e in E}) - len(per_req)} "
          f"(absence of findings is not completeness)")
    print(f"Citation ties among findings: {sum(1 for f in F if f.citation['tie'])}")

    probs = invariants(manifest, A, E, F, R)
    print("\nPolicy invariants (evidence-based):", "all hold" if not probs else dict(probs))

    print("\nExamples:")
    for label, xs in (("finding", F[:2]), ("replica drift finding", [f for f in F if any(n.startswith("REPLICA_DRIFT") for n in f.notes)][:2])):
        for f in xs:
            print(f"  [{label}] {f.finding_id} {f.finding_type} {f.aspect}={f.aspect_value} cite={f.citation['evidence_id']} "
                  f"({f.citation['ref']}) identifying={[i['evidence_id'] for i in f.identifying][:3]} notes={f.notes}")
    for e in [e for e in E if e.outcome == "INCONCLUSIVE"][:3] + [e for e in E if e.potential_violation][:2]:
        print(f"  [{e.outcome}] {e.case_id} {e.resource_id} {e.category} {e.reason} potential={e.potential_violation}")

    # ---- sample agreement (evaluation only, after the policy run)
    by = {(f.case_id, f.resource_id): f for f in F}
    rem = {(r.case_id, r.resource_id): r for r in R}
    sample_ids = {r["case_id"] for r in csv.DictReader(open(a.dataset / "sample_deletion_requests.csv", encoding="utf-8"))}
    gf = gr = af = ar = cites = cite_ok = 0
    extra = [f for f in F if f.case_id in sample_ids]
    misses = []
    gold_keys = set()
    for row in csv.DictReader(open(a.dataset / "sample_output.csv", encoding="utf-8")):
        for s in json.loads(row["steps_json"]):
            key = (row["case_id"], s["resource_id"])
            if s["phase"] == "FINDING":
                gf += 1
                gold_keys.add(key)
                f = by.get(key)
                if f and f.finding_type == s["operation"]:
                    af += 1
                    cites += 1
                    cite_ok += f.citation["evidence_id"] == s["evidence_id"]
                else:
                    ev = next((e for e in E if (e.case_id, e.resource_id) == key), None)
                    misses.append((key, s["operation"], (ev.outcome, ev.reason, ev.category) if ev else "not assessed"))
            elif s["phase"] == "REMEDIATION":
                gr += 1
                ar += bool(rem.get(key) and rem[key].operation == s["operation"])
    unexpected = [f for f in extra if (f.case_id, f.resource_id) not in gold_keys]
    print("\nSample agreement (agreement with published examples, not proof of correctness):")
    print(f"  findings matching sample findings: {af}/{gf}; remediations matching: {ar}/{gr}; "
          f"citations equal on matched findings: {cite_ok}/{cites}")
    print(f"  M5 findings on sample cases not in the sample answers: {len(unexpected)}")
    for f in unexpected[:a.examples]:
        print(f"    EXTRA {f.finding_id} {f.finding_type} cite={f.citation['evidence_id']}")
    for m in misses[:a.examples * 2]:
        print(f"    MISS {m[0][0]} {m[0][1]} {m[1]}: M5 {m[2]}")

    if a.json:
        a.json.write_text(json.dumps([e.to_dict() for e in E], sort_keys=True, default=str, indent=1), encoding="utf-8")
        print(f"\nwrote {len(E)} evaluations to {a.json}")


if __name__ == "__main__":
    main()
