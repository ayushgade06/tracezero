"""M3 report: request-to-activity matching, confidence/abstention, reuse cases, coverage, and
sample agreement.

Usage: python code/evaluation/matching_report.py [--dataset dataset] [--examples 10] [--json decisions.json]

Decisions are produced first from the audit inputs only. sample_output.csv is read afterwards,
here, solely to report agreement; it never feeds the matcher.
"""
import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

from audit import discovery, identity, ingest, matching  # noqa: E402


def read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def run(dataset):
    _, obs, _ = ingest.ingest(dataset)
    index = identity.load_identities(dataset)
    links = discovery.build_links(obs, index, discovery.load_catalog_links(dataset))
    rows = read(dataset / "sample_deletion_requests.csv") + read(dataset / "deletion_requests.csv")
    requests, candidates, decisions = matching.match(rows, matching.load_events(dataset), links, index)
    return obs, links, requests, candidates, decisions


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", default=ROOT / "dataset", type=Path)
    p.add_argument("--examples", default=10, type=int)
    p.add_argument("--json", type=Path, help="write all decisions (with candidates and provenance) as JSON")
    a = p.parse_args(argv)
    obs, links, requests, candidates, decisions = run(a.dataset)
    sample_ids = {r["case_id"] for r in read(a.dataset / "sample_deletion_requests.csv")}

    print(f"Requests considered: {len(requests)} ({len(sample_ids)} sample + {len(requests) - len(sample_ids)} eval; "
          f"sample requests compete for the same global activity)")
    print(f"  by family: {dict(Counter(r.family for r in requests))}")
    allc = [c for cs in candidates.values() for c in cs]
    feas = Counter(sum(c.feasible for c in candidates[r.case_id]) for r in requests)
    print(f"\nCandidates: {len(allc)} total, {sum(c.feasible for c in allc)} feasible; "
          f"rejected by reason {dict(Counter(c.rejected_reason for c in allc if not c.feasible))}")
    nums = sorted(sum(c.feasible for c in candidates[r.case_id]) for r in requests)
    print(f"  feasible per request: min {nums[0]}, median {nums[len(nums) // 2]}, max {nums[-1]}; distribution {dict(sorted(feas.items()))}")
    contested = Counter(c.activity_id for c in allc if c.feasible)
    print(f"  events wanted by >1 request: {sum(v > 1 for v in contested.values())} (resolved by the global assignment)")

    for label, ids in (("eval", {r.case_id for r in requests} - sample_ids), ("sample", sample_ids)):
        ds = [decisions[i] for i in sorted(ids, key=lambda x: int(x.split("_")[1]))]
        print(f"\nDecisions ({label}, {len(ds)}):")
        for (fam, conf, reason, method), n in sorted(Counter((d.family, d.confidence, d.reason, d.method) for d in ds).items()):
            print(f"  {fam:<23} {conf:<9} {reason:<34} {method:<18} {n:>4}")
        matched = [d for d in ds if d.status == "MATCHED"]
        print(f"  matched {len(matched)}: evidence-backed (explicit link) {sum(d.chosen.explicit for d in matched)}, "
              f"indirect (activity timing + result) {sum(not d.chosen.explicit for d in matched)}; unmatched {len(ds) - len(matched)}")

    print("\nLOW and UNMATCHED deletion decisions (eval), with nearest candidates:")
    shown = 0
    for d in sorted(decisions.values(), key=lambda d: int(d.case_id.split("_")[1])):
        if d.case_id in sample_ids or d.family != "account_deletion" or d.confidence in ("HIGH", "MEDIUM"):
            continue
        if shown < a.examples:
            near = sorted((c for c in d.considered if c.feasible), key=lambda c: c.cost)[:3]
            print(f"  {d.case_id} {d.confidence} {d.reason} {d.resource_id or ''} margin={d.margin_minutes} | "
                  + "; ".join(f"{c.resource_id} delay={c.delay_minutes} cost={c.cost:g}" for c in near))
        shown += 1
    print(f"  ({shown} in total)")

    notes = [(c, n.code) for c in allc for n in c.notes]
    print(f"\nHistorical / reused-resource signals: {dict(Counter(code for _, code in notes))}")
    for c, code in notes[:a.examples]:
        print(f"  {c.case_id} {c.resource_id} {c.activity_id} at {c.event_time}: {code} feasible={c.feasible} {c.rejected_reason or ''}")

    res = discovery.classify_resources(obs, links)
    matched_acc = {d.resource_id for d in decisions.values() if d.status == "MATCHED" and d.family == "account_deletion"}
    unlinked = {r for r, v in res.items() if v["status"] == "unlinked" and r.startswith("account_")}
    activity_only = {r for r in matched_acc if r not in res}
    print(f"\nCoverage of accounts M2 could not link: matched {len(matched_acc & unlinked)}/{len(unlinked)} evidence-unlinked accounts; "
          f"{len(activity_only)} matched accounts appear only in the activity stream")
    print("  not matched by design: analytics/search/support activity carries only timing (no identity or request link)")

    # ---- sample agreement, computed after all decisions exist
    gold = {}
    for r in read(a.dataset / "sample_output.csv"):
        acc = [s["resource_id"] for s in json.loads(r["steps_json"]) if s["system_id"] == "account_service"]
        gold[r["case_id"]] = acc[0] if acc else None
    agree = Counter()
    for cid, g in gold.items():
        d = decisions[cid]
        outcome = "abstained" if d.status == "UNMATCHED" else ("agrees" if d.resource_id == g else "differs")
        agree[(d.confidence, outcome)] += 1
    print("\nSample agreement (account in the sample answer vs M3 decision; agreement, not ground-truth ownership accuracy):")
    for (conf, outcome), n in sorted(agree.items()):
        print(f"  {conf:<9} {outcome:<9} {n:>3}")
    for cid, g in gold.items():
        d = decisions[cid]
        if d.status == "UNMATCHED" or d.resource_id != g:
            present = any(c.resource_id == g for c in d.considered)
            print(f"  {cid}: answer {g}; decision {d.confidence} {d.reason} {d.resource_id or ''}; answer among candidates: {present}")

    print("\nKnown risks: an inferred (MEDIUM) match rests on timing + result consistency only; a wrong neighbour"
          " decision can propagate through the global assignment; thresholds are fixed constants calibrated on the"
          " explicit-link matches of this corpus.")
    if a.json:
        a.json.write_text(json.dumps({k: v.to_dict() for k, v in sorted(decisions.items())}, sort_keys=True, default=str, indent=1),
                          encoding="utf-8")
        print(f"\nwrote {len(decisions)} decisions to {a.json}")


if __name__ == "__main__":
    main()
