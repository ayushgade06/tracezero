"""Serialization of the audit output file (case_id, deletion_status, steps_json)."""
import csv
import json

HEADER = ["case_id", "deletion_status", "steps_json"]


def steps_json(steps):
    # Compact, key-sorted JSON: same shape as dataset/sample_output.csv, byte-stable across runs.
    return json.dumps(steps, sort_keys=True, separators=(",", ":"))


def write_output(rows, path):
    """rows: iterable of (case_id, deletion_status, steps list), already in output order."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(HEADER)
        for case_id, status, steps in rows:
            w.writerow([case_id, status, steps_json(steps)])
