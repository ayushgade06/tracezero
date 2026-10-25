"""M2 report: identity resolution coverage, evidence links, ownership status, and resource-link
precision/recall on the sample cases.

Usage: python code/evaluation/discovery_report.py [--dataset dataset] [--examples 8]

Sample answers (sample_output.csv) are read ONLY here, for scoring; discovery never sees them.
"""
import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

from audit import discovery, identity, ingest, intent  # noqa: E402


def read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def build(dataset):
    manifest, obs, diags = ingest.ingest(dataset)
    index = identity.load_identities(dataset)
    links = discovery.build_links(obs, index, discovery.load_catalog_links(dataset))
    resources = discovery.classify_resources(obs, links)
    return obs, index, links, resources


def show_link(l):
    who = l.user_id or "UNREGISTERED"
    return (f"{l.resource_id} <- {who} via {l.link_type} {l.identity_kind}={l.identity_value[:40]} "
            f"[{l.identity_field}] @ {l.evidence_id} {l.locator} key={l.key_status}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", default=ROOT / "dataset", type=Path)
    p.add_argument("--examples", default=8, type=int)
    a = p.parse_args(argv)
    obs, index, links, resources = build(a.dataset)

    ids = [i for s in index.subjects.values() for i in s.identifiers]
    print(f"Identity model: {len(index.subjects)} subjects, {len(ids)} identifiers "
          f"({sum(i.current for i in ids)} current, {sum(not i.current for i in ids)} historical); "
          f"by kind {dict(Counter(i.kind for i in ids))}; keys shared by >1 user: {len(index.ambiguous_keys())}")

    for name in ("deletion_requests.csv", "sample_deletion_requests.csv"):
        reqs = read(a.dataset / name)
        res = [identity.resolve_request(index, r["email"]) for r in reqs]
        dele = [intent.classify(r["request_text"]) != intent.NON_DELETION for r in reqs]
        print(f"\n{name}: {len(reqs)} requests ({sum(dele)} audited as deletion)")
        print(f"  resolution: {dict(Counter(x['status'] for x in res))}; case-normalized matches: {sum(x['case_normalized'] for x in res)}")
        un = [r["case_id"] for r, x in zip(reqs, res) if x["status"] in ("unresolved", "ambiguous")]
        print(f"  unresolved/ambiguous: {un or 'none'}")
        counts = []
        for r, x, d in zip(reqs, res, dele):
            if d and len(x["user_ids"]) == 1:
                found = discovery.discover(x["user_ids"][0], links, resources)
                counts.append(sum(f["strength"] == "direct" for f in found))
        print(f"  direct-linked resources per deletion request: min {min(counts)}, median {sorted(counts)[len(counts) // 2]}, "
              f"max {max(counts)}; requests with none: {counts.count(0)}")

    print(f"\nLinks: {len(links)} total")
    for (lt, reg), n in sorted(Counter((l.link_type, l.user_id is not None) for l in links).items()):
        print(f"  {lt:<16} {'known user' if reg else 'unregistered key':<17} {n:>5}")
    print(f"  key status at observation (known users): {dict(Counter(l.key_status for l in links if l.user_id))}")

    st = Counter((r["status"], r["conflict"]) for r in resources.values())
    print(f"\nResources seen: {len(resources)}; by ownership status: " + ", ".join(f"{s}{'/' + c if c else ''}={n}" for (s, c), n in sorted(st.items())))
    for status in ("conflicting", "candidate_only", "unregistered_only"):
        rows = [(rid, r) for rid, r in resources.items() if r["status"] == status]
        print(f"  {status}: {len(rows)}")
        for rid, r in rows[:a.examples]:
            first = next(l for l in links if l.resource_id == rid)
            print(f"    {rid} {r['conflict'] or ''} owners={list(r['owners'])[:4]} | e.g. {show_link(first)}")
    unl = [(rid, r) for rid, r in resources.items() if r["status"] == "unlinked"]
    print(f"  unlinked by format: {dict(Counter('+'.join(r['formats']) for _, r in unl))}")
    print(f"  unlinked by system: {dict(Counter('+'.join(r['systems']) or 'unknown' for _, r in unl))}")
    print(f"  unlinked by id prefix: {dict(Counter(rid.rsplit('_', 1)[0] for rid, _ in unl))}")

    # ---- sample scoring (evaluation only)
    reqs = read(a.dataset / "sample_deletion_requests.csv")
    gold = {r["case_id"]: {s["resource_id"] for s in json.loads(r["steps_json"])} for r in read(a.dataset / "sample_output.csv")}
    in_corpus = set(resources)
    in_activity = {r["resource_id"] for r in read(a.dataset / "system_activity.csv")}
    tp = fp = fn = fn_rec = tp_any = 0
    miss_kind, extras, misses = Counter(), [], []
    for r in reqs:
        x = identity.resolve_request(index, r["email"])
        found = discovery.discover(x["user_ids"][0], links, resources) if len(x["user_ids"]) == 1 else []
        direct = {f["resource_id"] for f in found if f["strength"] == "direct"}
        g = gold[r["case_id"]]
        tp += len(direct & g)
        tp_any += len({f["resource_id"] for f in found} & g)
        fp += len(direct - g)
        fn += len(g - direct)
        for rid in sorted(g - direct):
            if rid not in in_corpus:
                kind = ("only in system_activity (needs activity attribution, M3)" if rid in in_activity
                        else "in no input text file or activity row (screenshot-only or unrecoverable)")
            else:
                st = resources[rid]["status"]
                kind = {"unlinked": "in evidence, no identity link (needs activity attribution, M3)",
                        "owned": "linked only to another subject"}.get(st, f"resource status {st}")
                fn_rec += 1
            miss_kind[kind] += 1
            misses.append((r["case_id"], rid, kind))
        for rid in sorted(direct - g):
            extras.append((r["case_id"], next(l for l in found if l["resource_id"] == rid)["links"][0]))
    print("\nSample resource-link quality (direct links vs resources in the sample answers):")
    print(f"  precision {tp}/{tp + fp} = {tp / (tp + fp):.3f}; recall {tp}/{tp + fn} = {tp / (tp + fn):.3f}; "
          f"recall on resources present in parsed evidence {tp}/{tp + fn_rec} = {tp / (tp + fn_rec):.3f}")
    print(f"  recall counting candidate links too: {tp_any}/{tp + fn} = {tp_any / (tp + fn):.3f}")
    print(f"  misses by cause: {dict(miss_kind)}")
    for m in misses[:a.examples]:
        print(f"    MISS {m[0]} {m[1]}: {m[2]}")
    for case, l in extras[:a.examples]:
        print(f"    EXTRA {case}: {show_link(l)}")
    if len(extras) > a.examples:
        print(f"    ... {len(extras)} extras in total")


if __name__ == "__main__":
    main()
