"""Independent output-contract validator for output.csv.

Reads the serialized CSV only; it does not import or trust the generator. Every rule
below comes from the audit output contract (see docs/ARCHITECTURE.md).

Usage: python code/evaluation/validate_output.py [--output output.csv]
           [--requests dataset/deletion_requests.csv] [--strict]

Errors are contract violations. --strict adds the step-relationship rules that make a row
internally consistent (M7, approved decisions D1/D2/D4/D5); the published sample answers pass them:
  - identifiers well formed: case_<n>, system ids snake_case, resource ids <prefix>_<digits>,
    evidence ids evidence_/activity_/audit_<digits>
  - no two steps share (phase, system_id, resource_id); with the contract sort key this makes the
    order strictly increasing
  - COMPLETE: at least one step, and a verification of the requester's account or tombstone
    (resource account_* / tombstone_*)
  - INCOMPLETE with findings: every FINDING has exactly one REMEDIATION on the same resource with
    the permitted pairing (REMAINING->DELETE, EXPOSED_PII->REDACT|ANONYMIZE, ACTIVE_DATA_FLOW->
    DISABLE, RESTORE_RISK->BLOCK); every REMEDIATION has its FINDING; every remediated resource has
    a VERIFICATION of the remediation's state
  - INCOMPLETE without findings: no REMEDIATION (nothing to remediate) and at least one VERIFICATION,
    except an empty INCOMPLETE row: allowed (approved evidence-gap rows: no finding and no attributed
    resource whose required treatment is known) but always reported as a warning
  - COMPLETE with no steps is an error
The validator cannot see the generator's evaluations, so "no unresolved blocking condition" is
checked by the generator's tests, not here.
"""
import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HEADER = ["case_id", "deletion_status", "steps_json"]
STATUSES = {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}
KEYS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}
PHASES = ["FINDING", "REMEDIATION", "VERIFICATION"]
# (operation, phase, state) in the order of the "Allowed Operations" table.
OPERATIONS = [
    ("REPORT_REMAINING_RECORD", "FINDING", "PRESENT"),
    ("REPORT_EXPOSED_PII", "FINDING", "PRESENT"),
    ("REPORT_ACTIVE_DATA_FLOW", "FINDING", "ACTIVE"),
    ("REPORT_RESTORE_RISK", "FINDING", "ACTIVE"),
    ("DISABLE_DATA_FLOW", "REMEDIATION", "DISABLED"),
    ("BLOCK_RESTORE", "REMEDIATION", "BLOCKED"),
    ("DELETE_RECORD", "REMEDIATION", "ABSENT"),
    ("REDACT_RECORD", "REMEDIATION", "REDACTED"),
    ("ANONYMIZE_RECORD", "REMEDIATION", "ANONYMIZED"),
    ("VERIFY_RECORD_ABSENT", "VERIFICATION", "ABSENT"),
    ("VERIFY_RECORD_PRESENT", "VERIFICATION", "PRESENT"),
    ("VERIFY_RECORD_REDACTED", "VERIFICATION", "REDACTED"),
    ("VERIFY_RECORD_ANONYMIZED", "VERIFICATION", "ANONYMIZED"),
    ("VERIFY_DATA_FLOW_DISABLED", "VERIFICATION", "DISABLED"),
    ("VERIFY_RESTORE_BLOCKED", "VERIFICATION", "BLOCKED"),
]
OP = {name: (i, phase, state) for i, (name, phase, state) in enumerate(OPERATIONS)}
PAIRS = {"REPORT_REMAINING_RECORD": ("DELETE_RECORD",), "REPORT_EXPOSED_PII": ("REDACT_RECORD", "ANONYMIZE_RECORD"),
         "REPORT_ACTIVE_DATA_FLOW": ("DISABLE_DATA_FLOW",), "REPORT_RESTORE_RISK": ("BLOCK_RESTORE",)}
CASE_ID = re.compile(r"case_\d+")
SYSTEM_ID = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
RESOURCE_ID = re.compile(r"[a-z]+(?:_[a-z]+)*_\d+")
EVIDENCE_ID = re.compile(r"(?:evidence|activity|audit)_\d+")
ACCOUNT_LIKE = re.compile(r"(?:account|tombstone)_\d+")


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _read_csv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def load_evidence_times(dataset):
    """Input-visible evidence ids -> capture/event time (file_id, activity_id, audit_id)."""
    times = {r["file_id"]: _ts(r["captured_at"]) for r in _read_csv(dataset / "evidence_files.csv")}
    times.update((r["activity_id"], _ts(r["event_time"])) for r in _read_csv(dataset / "system_activity.csv"))
    times.update((r["audit_id"], _ts(r["event_time"])) for r in _read_csv(dataset / "user_audit_log.csv"))
    return times


def check_steps(steps, status, audit_at, evidence_times, strict=False):
    errs = []
    if not isinstance(steps, list):
        return ["steps_json is not a JSON array"]
    if status == "NOT_APPLICABLE" and steps:
        errs.append("NOT_APPLICABLE must have an empty steps array")
    if strict and status == "COMPLETE" and not steps:
        errs.append("COMPLETE has no steps")
    prev = None
    for n, s in enumerate(steps, 1):
        where = f"step {n}"
        if not isinstance(s, dict):
            errs.append(f"{where}: not a JSON object")
            continue
        if set(s) != KEYS:
            errs.append(f"{where}: keys {sorted(s)} != {sorted(KEYS)}")
            continue
        if type(s["step_no"]) is not int or s["step_no"] != n:
            errs.append(f"{where}: step_no {s['step_no']!r} should be integer {n}")
        bad = [k for k in KEYS - {"step_no"} if not isinstance(s[k], str)]
        if bad:
            errs.append(f"{where}: non-string fields {sorted(bad)}")
            continue
        if s["operation"] not in OP:
            errs.append(f"{where}: unknown operation {s['operation']}")
            continue
        idx, phase, state = OP[s["operation"]]
        if s["phase"] != phase:
            errs.append(f"{where}: {s['operation']} belongs to phase {phase}, got {s['phase']}")
        if s["state"] != state:
            errs.append(f"{where}: {s['operation']} requires state {state}, got {s['state']}")
        if not s["system_id"] or not s["resource_id"]:
            errs.append(f"{where}: empty system_id/resource_id")
        key = (idx, s["system_id"], s["resource_id"])
        if prev is not None and key < prev:
            errs.append(f"{where}: out of order (phase, operation, system_id, resource_id)")
        prev = key
        ev = s["evidence_id"]
        if status == "COMPLETE" and phase != "VERIFICATION":
            errs.append(f"{where}: COMPLETE may contain only VERIFICATION steps")
        cited = (status == "COMPLETE") or phase == "FINDING"
        if cited and not ev:
            errs.append(f"{where}: {status} {phase} step must cite evidence")
        if not cited and ev:
            errs.append(f"{where}: INCOMPLETE {phase} step must have empty evidence_id")
        if ev:
            if ev not in evidence_times:
                errs.append(f"{where}: unknown evidence_id {ev}")
            elif audit_at is not None and evidence_times[ev] > audit_at:
                errs.append(f"{where}: {ev} is dated after audit_at")
    if strict and not errs and steps:  # an empty INCOMPLETE row is an evidence-gap row (warning, see validate)
        errs += _strict_relations(steps, status)
    return errs


def _strict_relations(steps, status):
    """Step-relationship rules (see module docstring); runs only on rows that pass the contract checks."""
    errs = []
    for n, s in enumerate(steps, 1):
        for field, pat in (("system_id", SYSTEM_ID), ("resource_id", RESOURCE_ID)):
            if not pat.fullmatch(s[field]):
                errs.append(f"step {n}: malformed {field} {s[field]!r}")
        if s["evidence_id"] and not EVIDENCE_ID.fullmatch(s["evidence_id"]):
            errs.append(f"step {n}: malformed evidence_id {s['evidence_id']!r}")
    keys = [(s["phase"], s["system_id"], s["resource_id"]) for s in steps]
    for k in sorted({k for k in keys if keys.count(k) > 1}):
        errs.append(f"duplicate {k[0]} step for {k[1]}/{k[2]}")
    by = {p: {(s["system_id"], s["resource_id"]): s["operation"] for s in steps if s["phase"] == p} for p in PHASES}
    findings, remediations, verifications = by["FINDING"], by["REMEDIATION"], by["VERIFICATION"]
    if status == "COMPLETE" and not any(ACCOUNT_LIKE.fullmatch(r) for _, r in verifications):
        errs.append("COMPLETE has no verification of an account_/tombstone_ resource")
    if status == "INCOMPLETE":
        if not findings:
            if remediations:
                errs.append("INCOMPLETE without findings must not contain REMEDIATION steps")
            if not verifications:
                errs.append("INCOMPLETE without findings needs at least one VERIFICATION step")
        for res, op in sorted(findings.items()):
            if res not in remediations:
                errs.append(f"finding {op} on {res[0]}/{res[1]} has no remediation")
            elif remediations[res] not in PAIRS[op]:
                errs.append(f"finding {op} on {res[0]}/{res[1]} has non-permitted remediation {remediations[res]}")
        for res, op in sorted(remediations.items()):
            if res not in findings:
                errs.append(f"remediation {op} on {res[0]}/{res[1]} has no finding")
            want = "VERIFY_" + {"DELETE_RECORD": "RECORD_ABSENT", "REDACT_RECORD": "RECORD_REDACTED", "ANONYMIZE_RECORD": "RECORD_ANONYMIZED",
                                "DISABLE_DATA_FLOW": "DATA_FLOW_DISABLED", "BLOCK_RESTORE": "RESTORE_BLOCKED"}[op]
            if verifications.get(res) != want:
                errs.append(f"remediation {op} on {res[0]}/{res[1]} needs verification {want}, found {verifications.get(res)}")
    return errs


def validate(output_path, requests_path, strict=False):
    """Return (errors, warnings) for a serialized output file."""
    errors, warnings = [], []
    dataset = Path(requests_path).parent
    reqs = _read_csv(requests_path)
    expected = [r["case_id"] for r in reqs]
    audit_at = {r["case_id"]: _ts(r["audit_at"]) for r in reqs}
    evidence_times = load_evidence_times(dataset)

    raw = Path(output_path).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        errors.append("file starts with a UTF-8 BOM")
    with open(output_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows or rows[0] != HEADER:
        errors.append(f"header must be exactly {','.join(HEADER)}")
        return errors, warnings
    body = rows[1:]
    if len(body) != len(expected):
        errors.append(f"expected {len(expected)} data rows, found {len(body)}")
    ids = [r[0] if r else "" for r in body]
    dupes = sorted({c for c in ids if ids.count(c) > 1})
    if dupes:
        errors.append(f"duplicate case_ids: {dupes[:10]}")
    missing, extra = sorted(set(expected) - set(ids)), sorted(set(ids) - set(expected))
    if strict:
        malformed = [c for c in ids if not CASE_ID.fullmatch(c)]
        if malformed:
            errors.append(f"malformed case_ids: {malformed[:10]}")
    if missing:
        errors.append(f"missing case_ids: {missing[:10]}")
    if extra:
        errors.append(f"unexpected case_ids: {extra[:10]}")
    if not dupes and not missing and not extra and ids != expected:
        errors.append("rows are not in input (ascending case number) order")

    empty = []
    for line, r in enumerate(body, 2):
        where = f"line {line}"
        if len(r) != 3:
            errors.append(f"{where}: expected 3 columns, found {len(r)}")
            continue
        case_id, status, payload = r
        if status not in STATUSES:
            errors.append(f"{where} {case_id}: invalid deletion_status {status!r}")
            continue
        try:
            steps = json.loads(payload)
        except ValueError as e:
            errors.append(f"{where} {case_id}: steps_json is not valid JSON ({e})")
            continue
        errors += [f"{where} {case_id}: {e}" for e in check_steps(steps, status, audit_at.get(case_id), evidence_times, strict)]
        if status != "NOT_APPLICABLE" and steps == []:
            empty.append(case_id)
    if empty:
        warnings.append(f"{len(empty)} COMPLETE/INCOMPLETE rows have no steps (INCOMPLETE: evidence-gap rows; "
                        f"COMPLETE: error under --strict): {empty[:10]}")
    return errors, warnings


def main(argv=None):
    p = argparse.ArgumentParser(description="Validate output.csv against the audit output contract.")
    p.add_argument("--output", default=ROOT / "output.csv", type=Path)
    p.add_argument("--requests", default=ROOT / "dataset" / "deletion_requests.csv", type=Path)
    p.add_argument("--strict", action="store_true")
    a = p.parse_args(argv)
    errors, warnings = validate(a.output, a.requests, a.strict)
    for w in warnings:
        print("WARNING:", w)
    for e in errors[:50]:
        print("ERROR:", e)
    print(f"{'FAIL' if errors else 'PASS'}: {len(errors)} error(s), {len(warnings)} warning(s) in {a.output}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
