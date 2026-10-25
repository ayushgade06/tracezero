"""M4 report: temporal state assessments at audit_at, exclusions, conflicts, drift, reuse,
ownership limits, and (separately) agreement with sample answers.

Usage: python code/evaluation/state_report.py [--dataset dataset] [--examples 6] [--json assessments.json]

Assessments are produced first from the audit inputs only. sample_output.csv is read
afterwards, here, solely to report agreement; it never feeds state reconstruction.
"""
import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

from audit import discovery, identity, ingest, matching, state  # noqa: E402

# Steps in the sample answers that assert a state at audit_at (findings, and verification
# steps of COMPLETE cases). Future checks in INCOMPLETE cases assert nothing about the cutoff.
EXPECTED = {
    "REPORT_REMAINING_RECORD": ("existence", "PRESENT"), "REPORT_EXPOSED_PII": ("exposure", "IDENTIFIABLE"),
    "REPORT_ACTIVE_DATA_FLOW": ("flow", "ACTIVE"), "REPORT_RESTORE_RISK": ("restore", "ALLOWED"),
    "VERIFY_RECORD_ABSENT": ("existence", "ABSENT"), "VERIFY_RECORD_PRESENT": ("existence", "PRESENT"),
    "VERIFY_RECORD_REDACTED": ("exposure", "REDACTED"), "VERIFY_RECORD_ANONYMIZED": ("exposure", "ANONYMIZED"),
    "VERIFY_DATA_FLOW_DISABLED": ("flow", "DISABLED"), "VERIFY_RESTORE_BLOCKED": ("restore", "BLOCKED"),
}


def read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def run(dataset):
    _, obs, _ = ingest.ingest(dataset)
    index = identity.load_identities(dataset)
    links = discovery.build_links(obs, index, discovery.load_catalog_links(dataset))
    resources = discovery.classify_resources(obs, links)
    rows = read(dataset / "sample_deletion_requests.csv") + read(dataset / "deletion_requests.csv")
    events = matching.load_events(dataset)
    requests, _, decisions = matching.match(rows, events, links, index)
    return state.reconstruct(requests, decisions, obs, links, resources, events)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", default=ROOT / "dataset", type=Path)
    p.add_argument("--examples", default=6, type=int)
    p.add_argument("--json", type=Path)
    a = p.parse_args(argv)
    A = run(a.dataset)
    print(f"Assessments: {len(A)} (request, resource) pairs over {len({x.case_id for x in A})} deletion requests")
    print(f"  attribution: {dict(Counter(x.association['attribution'] for x in A))}")
    print(f"  summary: {dict(Counter(x.summary for x in A))}")
    for asp in state.ASPECTS:
        rel = [x for x in A if asp in ("existence", "exposure") or (asp == "flow") == x.resource_id.startswith("data_flow")
               and (asp != "restore" or x.resource_id.startswith("backup"))]
        print(f"  {asp:<9} value/basis: {dict(Counter((x.aspects[asp]['value'], x.aspects[asp]['basis']) for x in rel))}")
    print("\nExistence by system:")
    for (sysname, val), n in sorted(Counter((x.system or "unknown", x.aspects["existence"]["value"]) for x in A).items()):
        print(f"  {sysname:<22} {val:<12} {n:>5}")
    print(f"  system from explicit evidence {sum(x.system_basis == 'explicit' for x in A)}, "
          f"inferred from id prefix {sum(x.system_basis == 'inferred_from_id_prefix' for x in A)}, unknown {sum(x.system_basis == 'unknown' for x in A)}")

    print("\nWhy existence is UNKNOWN:", dict(Counter(r for x in A for r in x.aspects["existence"]["reasons"]
                                                     if x.aspects["existence"]["value"] == "UNKNOWN")))
    print("Excluded observations by reason:", dict(Counter(e["reason"] for x in A for e in x.excluded)))
    post = [x for x in A if "POST_AUDIT_EVIDENCE_EXCLUDED" in x.reasons]
    print(f"Assessments with post-audit evidence excluded: {len(post)} "
          f"({sum(x.aspects['existence']['value'] == 'UNKNOWN' for x in post)} of them UNKNOWN as a result)")
    drift = [x for x in A if "EXISTENCE_REPLICA_DRIFT" in x.reasons]
    conflicts = [x for x in A if any(v["value"] == "CONFLICTING" for v in x.aspects.values())]
    stale = [x for x in A if any(v["basis"] == "OBSERVED_STALE" for v in x.aspects.values())]
    print(f"Replica disagreements (drift): {len(drift)}; conflicting aspects: {len(conflicts)}; "
          f"stale (later claim after last reading): {len(stale)}")
    reuse = [x for x in A if any(e["reason"] == "OTHER_SUBJECT_INSTANCE" for e in x.excluded)
             or x.association["attribution"] == "HISTORICAL_LINK" or x.association.get("competing_owners")]
    print(f"Reuse / other-instance cases: {len(reuse)} "
          f"(other-subject observations excluded {sum(any(e['reason'] == 'OTHER_SUBJECT_INSTANCE' for e in x.excluded) for x in A)}, "
          f"historical links {sum(x.association['attribution'] == 'HISTORICAL_LINK' for x in A)})")
    limited = [x for x in A if not x.association["attributed_to_requester"]]
    print(f"Ownership limits attribution: {len(limited)} assessments not attributed to the requester "
          f"{dict(Counter(x.association['attribution'] for x in limited))}")

    print("\nExamples:")
    for label, xs in (("replica drift", drift), ("stale after a later claim", stale), ("conflicting", conflicts),
                      ("weak absence only", [x for x in A if "WEAK_ABSENCE_ONLY" in x.aspects["existence"]["reasons"]]),
                      ("post-audit only", [x for x in A if "POST_AUDIT_EVIDENCE_ONLY" in x.aspects["existence"]["reasons"]])):
        for x in xs[:max(1, a.examples // 3)]:
            e = x.aspects["existence"]
            print(f"  [{label}] {x.case_id} {x.resource_id} ({x.association['attribution']}) existence={e['value']}/{e['basis']} "
                  f"per_copy={e['per_copy']} deciding={[d['ref'] for d in e['deciding']][:3]} "
                  f"contradicting={[d['ref'] for d in e['contradicting']][:3]}")

    # ---- sample agreement (evaluation only, after reconstruction)
    by = {(x.case_id, x.resource_id): x for x in A}
    agree, rows = Counter(), []
    for r in read(a.dataset / "sample_output.csv"):
        for s in json.loads(r["steps_json"]):
            if s["phase"] == "REMEDIATION" or (s["phase"] == "VERIFICATION" and r["deletion_status"] != "COMPLETE"):
                continue
            asp, want = EXPECTED[s["operation"]]
            x = by.get((r["case_id"], s["resource_id"]))
            got = x.aspects[asp]["value"] if x else "NOT_ASSESSED"
            if want == "ANONYMIZED" and got == "UNKNOWN" and x:
                pass
            outcome = "agrees" if got == want else ("not assessed" if not x else ("unknown" if got == "UNKNOWN" else "differs"))
            agree[(asp, outcome)] += 1
            if outcome != "agrees":
                rows.append((r["case_id"], s["operation"], s["resource_id"], want, got,
                             x.aspects[asp]["reasons"] if x else [], x.association["attribution"] if x else None))
    print("\nSample agreement (state asserted by sample findings / COMPLETE verifications vs M4 at audit_at;"
          " agreement with published examples, not ground-truth accuracy):")
    for (asp, outcome), n in sorted(agree.items()):
        print(f"  {asp:<9} {outcome:<13} {n:>3}")
    print(f"  total agree {sum(n for (_, o), n in agree.items() if o == 'agrees')}/{sum(agree.values())}")
    for row in rows[:a.examples * 4]:
        print(f"  {row[0]} {row[1]} {row[2]}: expected {row[3]}, M4 {row[4]} {row[5]} attribution={row[6]}")

    print("\nLimitations: activity-only and post-audit-only resources stay UNKNOWN; anonymized analytics are not"
          " attributed (no identity, not matched by M3); exposure is UNKNOWN when a lookup shows no identifier fields;"
          " lookup/scan semantics are read per field and may miss system-specific nuances.")
    if a.json:
        a.json.write_text(json.dumps([x.to_dict() for x in A], sort_keys=True, default=str, indent=1), encoding="utf-8")
        print(f"\nwrote {len(A)} assessments to {a.json}")


if __name__ == "__main__":
    main()
