"""Evidence ingestion report: inventory, per-format counts, diagnostics, resource-id coverage.

Usage: python code/evaluation/ingest_report.py [--dataset dataset] [--jsonl observations.jsonl]
Exit code 1 if any error diagnostic or unresolved resource id is found.
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

from audit.ingest import coverage, ingest  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", default=ROOT / "dataset", type=Path)
    p.add_argument("--jsonl", type=Path, help="also write every observation as JSON lines")
    a = p.parse_args(argv)

    manifest, obs, diags = ingest(a.dataset)
    by_fmt = defaultdict(Counter)
    for m in manifest:
        by_fmt[m["format"]]["files"] += 1
    fmt_of = {m["file_id"]: m["format"] for m in manifest}
    for o in obs:
        c = by_fmt[o.file_format]
        c[f"obs_{o.kind}"] += 1
        c["with_resource_id"] += bool(o.resource_id)
        c["with_identity_keys"] += bool(o.identity_keys)
        c["with_observed_at"] += bool(o.observed_at)
    for d in diags:
        by_fmt[fmt_of[d["file_id"]]][f"diag_{d['severity']}"] += 1

    print(f"Manifest: {len(manifest)} entries")
    cols = ["files", "obs_record", "obs_scope", "obs_page", "with_resource_id", "with_identity_keys", "with_observed_at",
            "diag_error", "diag_warning", "diag_info"]
    print("format  " + " ".join(f"{c:>18}" for c in cols))
    for fmt in sorted(by_fmt):
        print(f"{fmt:<7} " + " ".join(f"{by_fmt[fmt][c]:>18}" for c in cols))
    print(f"observations: {len(obs)}  unique obs_id: {len({o.obs_id for o in obs})}")

    print("\nDiagnostics by reason:")
    for (sev, reason), n in sorted(Counter((d["severity"], d["reason"].split(":")[0]) for d in diags).items()):
        print(f"  {sev:<8} {n:>4}  {reason}")
    for d in diags:
        if d["severity"] != "info":
            print(f"  - {d['file_id']} {d['locator']} {d['reason']} | {d['excerpt'][:120]}")

    cov = coverage(a.dataset, manifest, obs)
    print(f"\nResource-id coverage over {cov['files']} files with readable text (screenshots via the OCR cache): "
          f"{cov['represented']}/{cov['tokens']} distinct (file, id) pairs represented, {len(cov['unresolved'])} unresolved")
    for u in cov["unresolved"][:40]:
        print(f"  UNRESOLVED {u['file_id']} L{u['line']} {u['token']}: {u['excerpt'][:120]}")
    print("Other id-like tokens (not resource ids, not user ids), by prefix:")
    for prefix, n in cov["other_id_tokens"].most_common():
        print(f"  {prefix:<24} {n:>5}  e.g. {cov['other_examples'][prefix]}")

    if a.jsonl:
        with open(a.jsonl, "w", encoding="utf-8", newline="\n") as f:
            for o in obs:
                f.write(json.dumps(o.to_dict(), sort_keys=True, ensure_ascii=False) + "\n")
        print(f"\nwrote {len(obs)} observations to {a.jsonl}")
    errors = sum(d["severity"] == "error" for d in diags)
    return 1 if errors or cov["unresolved"] else 0


if __name__ == "__main__":
    sys.exit(main())
