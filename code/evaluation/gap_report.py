"""M7.4 upstream gap report (read-only; changes no behaviour).

Usage: python code/evaluation/gap_report.py [--dataset dataset]

Quantifies why deletion requests cannot be proven COMPLETE, by gap class, over the 518 evaluation
requests. G7 reads dataset/sample_output.csv to explain sample disagreements (evaluation only).
  G1 account state known only from activity claims (no admissible reading)
  G2 no account matched by M3 (abstention) -> no account proof
  G3 anonymized analytics records that no identifier links to anyone
  G4 retained records without retention metadata still blocking (RETENTION_UNVERIFIABLE: legal holds only since D3 was revised)
  G5 resources whose category cannot be determined
  G6 evidence-gap rows (INCOMPLETE with no statable check)
  G7 sample cases whose reference status differs, with the blocking reasons
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(1, str(ROOT / "code"))

from audit import final, intent, pipeline, state  # noqa: E402
from audit.matching import _ts  # noqa: E402
from audit.state import _iso  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", default=ROOT / "dataset", type=Path)
    a = p.parse_args(argv)
    r = pipeline.run(a.dataset)
    reqs = pipeline.read_requests(a.dataset / "deletion_requests.csv")
    ids = {x["case_id"] for x in reqs}
    E, A = defaultdict(list), {(x.case_id, x.resource_id): x for x in r["A"]}
    for e in r["E"]:
        E[e.case_id].append(e)
    remediation_of = {x.finding_id: x for x in r["R"]}
    rows = {x["case_id"]: final.assemble(x["case_id"], intent.classify(x["request_text"]), E[x["case_id"]], remediation_of,
                                         _iso(_ts(x["audit_at"])), final.link_systems(r["links"])) for x in reqs}
    si = state.StateIndex(r["obs"], r["links"], [])

    # G1: activity-only accounts, and the absence-capable evidence that exists for them
    profiles, captured = defaultdict(set), {}
    for o in r["obs"]:
        if o.file_format == "sql" and str(o.locator.get("table", "")).startswith("customer_profiles"):
            profiles[(o.file_id, o.locator["table"])].add(o.resource_id)
            captured[o.file_id] = o.file_captured_at
    g1 = Counter()
    for c in ids:
        for e in E[c]:
            if e.reason == "STATE_UNKNOWN:ACTIVITY_ONLY" and e.resource_id.startswith("account_"):
                x = A[(c, e.resource_id)]
                rq, au = _iso(_ts(x.requested_at)), _iso(_ts(x.audit_at))
                scopes = [s for s in si.scopes if s["empty"] and si.covers(s, e.resource_id) and s["captured"] <= au]
                omitted = any(rq <= captured[f] <= au and e.resource_id not in listed for (f, _), listed in profiles.items())
                g1[("empty scoped snapshot naming only other people" if scopes else "no empty scoped snapshot",
                    "omitted from a customer_profiles table captured in [requested_at, audit_at]" if omitted else "not in such a table")] += 1
    print("G1 account evaluations with activity-only state:", sum(g1.values()))
    for k, n in sorted(g1.items()):
        print(f"   {n:4d}  {k[0]}; {k[1]}")

    dec = r["dec"]
    deletion = [x for x in reqs if intent.classify(x["request_text"]) != intent.NON_DELETION]
    print("G2 M3 decisions for deletion requests:", dict(Counter(f"{dec[x['case_id']].status}/{dec[x['case_id']].reason}" for x in deletion)))
    print("   rows INCOMPLETE because no account proof (D2):", sum(t["basis"] == "NO_ACCOUNT_PROOF" for _, _, t in rows.values()))

    unlinked = {k for k, v in r["res"].items() if k.startswith("analytics_") and v["status"] == "unlinked"}
    nulled = sum(1 for o in r["obs"] if o.resource_id in unlinked and o.file_format == "json"
                 and all(o.attrs.get(f) is None for f in ("email", "email_hash", "phone") if f in o.attrs))
    with open(a.dataset / "system_activity.csv", encoding="utf-8", newline="") as f:
        acts = [x for x in csv.DictReader(f) if x["resource_id"] in unlinked]
    print(f"G3 unlinked analytics resources: {len(unlinked)}; JSON observations with identifier fields all null: {nulled}; "
          f"activity on them: {dict(Counter(x['event_type'] for x in acts))}, naming a person: "
          f"{sum(bool(re.search(r'user_[0-9]+|@', x['details'])) for x in acts)}")

    g4 = [e for c in ids for e in E[c] if e.reason == "RETENTION_UNVERIFIABLE"]
    with_field = {o.resource_id for o in r["obs"] if o.resource_id and any(k in o.attrs for k in ("retention_until", "retained_until"))}
    print(f"G4 RETENTION_UNVERIFIABLE evaluations: {len(g4)} {dict(Counter(e.resource_id.rsplit('_', 1)[0] for e in g4))}; "
          f"of which the record has a retention field anywhere: {sum(e.resource_id in with_field for e in g4)}; "
          f"no-finding rows blocked only by it: "
          f"{sum(t['basis'] == 'BLOCKED' and all(b.endswith(':RETENTION_UNVERIFIABLE') for b in t['blockers']) for _, _, t in rows.values())}")

    g5 = sorted({(e.resource_id, e.outcome) for c in ids for e in E[c] if e.reason in ("CATEGORY_UNKNOWN", "CATEGORY_CONFLICTING")})
    print(f"G5 resources with undetermined category: {len(g5)} {g5}")
    print("G6 evidence-gap rows (INCOMPLETE, no statable check):", sorted((c for c, (s, st, _) in rows.items() if s == "INCOMPLETE" and not st),
                                                                     key=lambda c: int(c.split("_")[1])))

    with open(a.dataset / "sample_output.csv", encoding="utf-8", newline="") as f:  # evaluation only
        gold = {x["case_id"]: x["deletion_status"] for x in csv.DictReader(f)}
    print("G7 sample cases whose reference status differs (evaluation only):")
    for x in pipeline.read_requests(a.dataset / "sample_deletion_requests.csv"):
        c = x["case_id"]
        status, _, t = final.assemble(c, intent.classify(x["request_text"]), E[c], remediation_of, _iso(_ts(x["audit_at"])),
                                      final.link_systems(r["links"]))
        if status != gold[c]:
            print(f"   {c}: reference {gold[c]}, ours {status} ({t['basis']}); M3 {dec[c].status}/{dec[c].reason}; blockers {t['blockers']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
