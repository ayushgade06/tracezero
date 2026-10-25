"""M6 report: OCR extraction per screenshot, the visual checklist, and the effect of OCR and of
the ownership-conflict gate on M2-M5.

Usage:
  python code/evaluation/ocr_report.py                 # uses the committed cache (no OCR engine needed)
  python code/evaluation/ocr_report.py --refresh-ocr   # re-run rapidocr (requirements-ocr.txt, Python <= 3.12)
                                                       # and rewrite code/cache/ocr
Three pipeline runs of the CURRENT code are compared: "pre" (OCR off, conflict gate off), OCR
without the gate, and OCR with the gate. "pre" reproduces pre-M6 behaviour, not pre-M6 bytes: every
run carries the additive M6/M6.1 fields (e.g. association `conflicting_owners`, assessment
`categories_admissible`), so "identical" below means byte-identical between these runs only.
Comparison with an older commit (e.g. b4ea79e) needs that commit's code and differs by those fields.
Sample answers are read only in the separate agreement section.
"""
import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

from audit import discovery, identity, ingest, matching, ocr, policy, state  # noqa: E402

# Facts read off the screenshots by eye before OCR was run (M6 design inspection).
CHECKLIST = {
    "evidence_04": {"resources": ["account_5762"], "identifiers": ["user_11", "haruka.s1838@zoho.com"]},
    "evidence_09": {"resources": ["search_428", "search_1874"], "identifiers": ["user_13", "g.silva76@gmail.com", "g.silva76.jr@gmail.com"]},
    "evidence_14": {"resources": ["data_flow_1121", "data_flow_9846"], "identifiers": ["jain.aditi@gmail.com", "lucia-torres@gmail.com"]},
    "evidence_19": {"resources": ["analytics_7438"], "identifiers": ["user_20", "darwish.omar@icloud.com"]},
    "evidence_23": {"resources": ["export_1258", "export_1259", "export_1260", "export_1261"], "identifiers": ["ingrid.dahl@outlook.com"]},
    "evidence_28": {"resources": ["support_1034"], "identifiers": ["kagyeman8947@fastmail.com"]},
    "evidence_32": {"resources": ["backup_9286", "backup_9287", "backup_9288", "backup_9291"], "identifiers": ["areed1433@gmail.com"]},
    "evidence_37": {"resources": [f"audit_record_00{i}" for i in range(1, 7)], "identifiers": ["user_03"]},
    "evidence_42": {"resources": [], "identifiers": [], "context": ["user_17"]},
    "evidence_46": {"resources": [], "identifiers": [], "context": ["delivery_confirmation_19.pdf"]},
    "evidence_60": {"resources": ["marketing_001"], "identifiers": ["sj.park@naver.com"]},
}


def read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def pipeline(dataset, use_ocr, conflict_gate):
    manifest, obs, diags = ingest.ingest(dataset, use_ocr=use_ocr)
    index = identity.load_identities(dataset)
    links = discovery.build_links(obs, index, discovery.load_catalog_links(dataset))
    res = discovery.classify_resources(obs, links)
    rows = read(dataset / "sample_deletion_requests.csv") + read(dataset / "deletion_requests.csv")
    events = matching.load_events(dataset)
    reqs, _, dec = matching.match(rows, events, links, index)
    A = state.reconstruct(reqs, dec, obs, links, res, events)
    E, F, R = policy.run(A, policy.PolicyContext(obs, manifest, conflict_gate=conflict_gate, events=events))
    return {"manifest": manifest, "obs": obs, "diags": diags, "links": links, "res": res, "dec": dec, "A": A, "E": E, "F": F, "R": R,
            "events": events}


def checklist(obs):
    """-> rows (file_id, fact, kind, status, where)."""
    rows = []
    for fid, want in CHECKLIST.items():
        mine = [o for o in obs if o.file_id == fid]
        for rid in want["resources"]:
            hit = [o for o in mine if o.resource_id == rid]
            rows.append((fid, rid, "resource", "VERIFIED record" if hit else "not extracted",
                         [o.obs_id for o in hit] or [o.obs_id for o in mine if rid in (o.raw or "")]))
        for ident in want["identifiers"]:
            hit = [o for o in mine if any(k["value"] == ident for k in o.identity_keys)]
            rows.append((fid, ident, "identifier", "identity key on a record" if hit else "not extracted", [o.obs_id for o in hit]))
        for text in want.get("context", []):
            hit = [o for o in mine if text in (o.raw or "")]
            rows.append((fid, text, "context", "context text" if hit else "not extracted", [o.obs_id for o in hit]))
    return rows


def _key(x):
    return json.dumps(x.to_dict() if hasattr(x, "to_dict") else x.__dict__, sort_keys=True, default=str)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", default=ROOT / "dataset", type=Path)
    p.add_argument("--refresh-ocr", action="store_true")
    a = p.parse_args(argv)
    if a.refresh_ocr:
        for m in ingest.load_manifest(a.dataset):
            if m["format"] == "image":
                boxes, source, d = ocr.load_boxes(m, a.dataset, refresh=True)
                print(f"refreshed {m['file_id']}: {source} {len(boxes or [])} boxes {d}")

    pre = pipeline(a.dataset, use_ocr=False, conflict_gate=False)
    mid = pipeline(a.dataset, use_ocr=True, conflict_gate=False)
    post = pipeline(a.dataset, use_ocr=True, conflict_gate=True)

    img = [o for o in post["obs"] if o.file_format == "image"]
    print(f"OCR engine {ocr.ENGINE} {ocr.ENGINE_VERSION}, models {[v[0] for v in ocr.MODELS.values()]}, config {ocr.CONFIG}, "
          f"preprocess {ocr.PREPROCESS}, min token score {ocr.MIN_SCORE}")
    print("\nImages processed:")
    for m in post["manifest"]:
        if m["format"] != "image":
            continue
        mine = [o for o in img if o.file_id == m["file_id"]]
        src = Counter(o.attrs["_ocr"]["source"] for o in mine)
        d = [x for x in post["diags"] if x["file_id"] == m["file_id"]]
        print(f"  {m['file_id']} {m['file_path'].split('/')[-1]:<44} captured {m['captured_at']} source={dict(src)} "
              f"units={len(mine)} records={sum(o.kind == 'record' for o in mine)} diagnostics={len(d)}")
        for o in mine:
            if o.kind == "record":
                print(f"      record {o.obs_id:<30} {o.resource_id:<17} bbox={o.locator['bbox']} keys={[k['value'] for k in o.identity_keys]} "
                      f"exposure_text={o.attrs['_ocr_direct_identifiers']}")
            elif o.attrs.get("_alias_candidates"):
                print(f"      context {o.obs_id:<29} alias candidates {o.attrs['_alias_candidates']} (candidate mapping only)")
        for x in d:
            print(f"      {x['severity']}: {x['reason']}")

    print("\nVisual checklist (facts read by eye before OCR):")
    rows = checklist(post["obs"])
    for fid, fact, kind, status, where in rows:
        print(f"  {fid} {kind:<10} {fact:<32} {status:<26} {where[:3]}")
    print(f"  extracted {sum(r[3] != 'not extracted' for r in rows)}/{len(rows)}")

    # ---- effects
    non_image = lambda r: [_key(o) for o in r["obs"] if o.file_format != "image"]  # noqa: E731
    print("\nNon-image observations identical before/after:", non_image(pre) == non_image(post))
    new_links = [l for l in post["links"] if l.evidence_id in {o.file_id for o in img}]
    print(f"New M2 links from screenshots: {len(new_links)} {dict(Counter((l.link_type, l.user_id is not None) for l in new_links))}")
    changed_status = {r: (pre["res"].get(r, {}).get("status"), v["status"], v["conflict"]) for r, v in post["res"].items()
                      if pre["res"].get(r, {}).get("status") != v["status"]}
    print(f"Resources whose M2 ownership status changed: {len(changed_status)}")
    for r, (b, s, c) in sorted(changed_status.items()):
        print(f"    {r}: {b} -> {s} {c or ''}")
    dchg = [(k, pre["dec"][k], post["dec"][k]) for k in post["dec"] if _key(pre["dec"][k]) != _key(post["dec"][k])]
    core = lambda d: (d.status, d.confidence, d.reason, d.resource_id, d.activity_id)  # noqa: E731
    real = [x for x in dchg if core(x[1]) != core(x[2])]
    print(f"M3 decisions changed: {len(real)}; decisions with provenance-only changes (extra link evidence listed): {len(dchg) - len(real)}")
    for k, b, c in dchg:
        print(f"    {k}: {'DECISION' if core(b) != core(c) else 'provenance only'} {core(b)} -> {core(c)}")
    pa = {(x.case_id, x.resource_id): x for x in pre["A"]}
    qa = {(x.case_id, x.resource_id): x for x in post["A"]}
    achg = [k for k in qa if k not in pa or (pa[k].summary, pa[k].aspects["exposure"]["value"]) != (qa[k].summary, qa[k].aspects["exposure"]["value"])]
    print(f"M4 assessments added: {len(set(qa) - set(pa))}, removed: {len(set(pa) - set(qa))}, state changed: {len([k for k in achg if k in pa])}")
    for k in sorted(achg, key=lambda k: (int(k[0].split('_')[1]), k[1])):
        b = pa.get(k)
        print(f"    {k[0]} {k[1]}: {(b.summary, b.aspects['exposure']['value']) if b else 'new'} -> "
              f"{(qa[k].summary, qa[k].aspects['exposure']['value'])} [{qa[k].association['attribution']}]")

    def fset(r):
        return {(f.case_id, f.resource_id, f.finding_type) for f in r["F"]}
    by_ocr, by_gate = fset(mid) ^ fset(pre), fset(post) ^ fset(mid)
    print(f"\nM5 findings: pre {len(pre['F'])}, OCR only {len(mid['F'])}, M6 (OCR + gate) {len(post['F'])}")
    print("  changes attributable to OCR:")
    for t in sorted(by_ocr):
        print(f"    {'+' if t in fset(mid) else '-'} {t}")
    print("  changes attributable to the ownership-conflict gate:")
    for t in sorted(by_gate):
        e = next((x for x in post["E"] if (x.case_id, x.resource_id) == t[:2]), None)
        print(f"    {'+' if t in fset(post) else '-'} {t} -> {e.outcome if e else ''} {e.reason if e else ''} {e.notes if e else ''}")
    print("  evaluation outcome changes (OCR, then gate):")
    for label, a_, b_ in (("OCR", pre, mid), ("gate", mid, post)):
        ea = {(e.case_id, e.resource_id): e for e in a_["E"]}
        for e in b_["E"]:
            k = (e.case_id, e.resource_id)
            if k not in ea or (ea[k].outcome, ea[k].reason) != (e.outcome, e.reason):
                old = (ea[k].outcome, ea[k].reason) if k in ea else "new"
                print(f"    [{label}] {k[0]} {k[1]}: {old} -> {(e.outcome, e.reason)} {e.notes}")
    fa = {(f.case_id, f.resource_id, f.finding_type): f for f in pre["F"]}
    for f in post["F"]:
        k = (f.case_id, f.resource_id, f.finding_type)
        if k in fa and _key(fa[k]) != _key(f):
            changed = [x for x in ("citation", "establishing", "contradicting", "identifying", "attribution") if
                       json.dumps(getattr(fa[k], x), sort_keys=True, default=str) != json.dumps(getattr(f, x), sort_keys=True, default=str)]
            print(f"  existing finding with changed content {k}: fields {changed}")
    gate_pre = policy.run(pre["A"], policy.PolicyContext(pre["obs"], pre["manifest"], conflict_gate=True, events=pre["events"]))[1]
    print(f"  conflict gate on pre-M6 inputs changes {len(fset({'F': gate_pre}) ^ fset(pre))} findings (expected 0: no conflicts existed)")
    img_ids = {o.file_id for o in img}
    touched = {k[:2] for k in by_ocr | by_gate} | {(f.case_id, f.resource_id) for f in post["F"]
                                                   if any(i["evidence_id"] in img_ids for i in f.identifying)}
    unrelated = lambda r: sorted(_key(f) for f in r["F"] if (f.case_id, f.resource_id) not in touched)  # noqa: E731
    print(f"  findings on all (request, resource) pairs without screenshot evidence byte-identical between the OCR-off and "
          f"OCR-on runs of this code: {unrelated(pre) == unrelated(post)} ({len(unrelated(post))} findings; not a comparison "
          f"with pre-M6 code, see docstring)")

    sample = {r["case_id"] for r in read(a.dataset / "sample_deletion_requests.csv")}
    gold = {(r["case_id"], s["resource_id"], s["operation"]) for r in read(a.dataset / "sample_output.csv")
            for s in json.loads(r["steps_json"]) if s["phase"] == "FINDING"}
    for label, r in (("pre-M6", pre), ("M6", post)):
        mine = {t for t in fset(r) if t[0] in sample}
        print(f"Sample agreement ({label}, agreement only): findings {len(mine & gold)}/{len(gold)}, extra {len(mine - gold)}")


if __name__ == "__main__":
    main()
