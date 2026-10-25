"""Custom evaluation cases (synthetic, fictional people and identifiers).

Each case is a tiny dataset run end to end through the real code: identity model (M2), evidence
parsing (M1 JSON parser), links, matching (M3), temporal state (M4), policy (M5) and final
assembly (M7). The produced row is serialized like output.csv and checked by the independent
strict validator, then compared with the expected result written down before running it.

Usage: python code/evaluation/custom_cases.py
"""
import csv
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(1, str(ROOT / "code"))  # after this directory: "main" stays code/evaluation/main.py

from audit import discovery, final, identity, intent, matching, parsers, policy, state  # noqa: E402
from audit.output import steps_json  # noqa: E402
import validate_output  # noqa: E402

REQUESTED, REPORTED, AUDIT = "2026-09-01T00:00:00Z", "2026-09-03T12:00:00Z", "2026-09-10T00:00:00Z"
DELETE_TEXT = "Please permanently delete my account and all personal data you hold about me."
SYSTEM = {"account": "account_service", "audit_record": "security_service", "data_flow": "integration_service",
          "backup": "backup_service"}
RTYPE = {"account": "USER_PROFILE", "audit_record": "SECURITY_AUDIT_LOG", "data_flow": "DATA_FLOW", "backup": "BACKUP_ARCHIVE"}


def lookup(rid, result, when, **extra):
    prefix = rid.rsplit("_", 1)[0]
    return dict({"recorded_at": when, "system": SYSTEM[prefix], "resource_id": rid, "record_type": RTYPE[prefix],
                 "result": result, "subject_reference": "mira.holt@example.org"}, **extra)


def ledger(file_id, captured, records):
    meta = {"file_id": file_id, "file_path": f"evidence/records/{file_id}.json", "origin": "custom", "captured_at": captured,
            "format": "json"}
    return meta, parsers.parse_json(meta, json.dumps({"records": records}))[0]


CASES = [
    {"name": "complete_with_retained_audit_record",
     "about": "Account lookup NOT_FOUND after the request; the security audit record is present with a retention "
              "period beyond the audit, so it must be retained (rule_12).",
     "files": [("evidence_901", "2026-09-05T00:00:00Z", [
         lookup("account_7001", "NOT_FOUND", "2026-09-05T00:00:00Z"),
         lookup("audit_record_7001", "FOUND", "2026-09-05T00:00:00Z", retention_until="2033-12-31T23:59:59Z")])],
     "expect": ("COMPLETE", [("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_7001", "evidence_901"),
                             ("VERIFICATION", "VERIFY_RECORD_PRESENT", "audit_record_7001", "evidence_901")])},
    {"name": "incomplete_data_flow_and_restore_risk",
     "about": "The account is gone, but an active connector and a restorable backup can bring the data back.",
     "files": [("evidence_902", "2026-09-05T00:00:00Z", [
         lookup("account_7002", "NOT_FOUND", "2026-09-05T00:00:00Z"),
         lookup("data_flow_7002", "ACTIVE", "2026-09-05T00:00:00Z"),
         lookup("backup_7002", "ACTIVE", "2026-09-05T00:00:00Z")])],
     "expect": ("INCOMPLETE", [("FINDING", "REPORT_ACTIVE_DATA_FLOW", "data_flow_7002", "evidence_902"),
                               ("FINDING", "REPORT_RESTORE_RISK", "backup_7002", "evidence_902"),
                               ("REMEDIATION", "DISABLE_DATA_FLOW", "data_flow_7002", ""),
                               ("REMEDIATION", "BLOCK_RESTORE", "backup_7002", ""),
                               ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_7002", ""),
                               ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "data_flow_7002", ""),
                               ("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "backup_7002", "")])},
    {"name": "historical_only_evidence",
     "about": "The only reading predates the request: it proves an earlier state, not the state at audit_at.",
     "files": [("evidence_903", "2026-08-20T00:00:00Z", [lookup("account_7003", "NOT_FOUND", "2026-08-20T00:00:00Z")])],
     "expect": ("INCOMPLETE", [("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_7003", "")])},
    {"name": "non_deletion_request",
     "about": "The person asks to update a phone number; nothing authorizes deletion.",
     "text": "Please update my phone number and leave the account active.",
     "files": [("evidence_904", "2026-09-05T00:00:00Z", [lookup("account_7004", "FOUND", "2026-09-05T00:00:00Z")])],
     "expect": ("NOT_APPLICABLE", [])},
    {"name": "post_audit_only_evidence",
     "about": "The only absence reading is in a file captured after audit_at; it cannot establish the state.",
     "files": [("evidence_905", "2026-09-12T00:00:00Z", [lookup("account_7005", "NOT_FOUND", "2026-09-08T00:00:00Z")])],
     "expect": ("INCOMPLETE", [("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_7005", "")])},
    {"name": "retained_record_with_expired_retention_period",
     "about": "The audit record is present but its supplied retention period ended before the audit: rule_12 "
              "no longer applies and no other rule covers it, so completion is not proven (D3 revised).",
     "files": [("evidence_906", "2026-09-05T00:00:00Z", [
         lookup("account_7006", "NOT_FOUND", "2026-09-05T00:00:00Z"),
         lookup("audit_record_7006", "FOUND", "2026-09-05T00:00:00Z", retention_until="2026-06-30T23:59:59Z")])],
     "expect": ("INCOMPLETE", [("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_7006", ""),
                               ("VERIFICATION", "VERIFY_RECORD_PRESENT", "audit_record_7006", "")])},
    {"name": "retained_record_without_retention_period",
     "about": "The audit record is present and no retention period is supplied anywhere: it is retained "
              "(D3 revised), so the deletion is proven complete with both checks cited.",
     "files": [("evidence_907", "2026-09-05T00:00:00Z", [
         lookup("account_7007", "NOT_FOUND", "2026-09-05T00:00:00Z"),
         lookup("audit_record_7007", "FOUND", "2026-09-05T00:00:00Z")])],
     "expect": ("COMPLETE", [("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_7007", "evidence_907"),
                             ("VERIFICATION", "VERIFY_RECORD_PRESENT", "audit_record_7007", "evidence_907")])},
]


def toy_index(d):
    with open(Path(d) / "user_identities.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["user_id", "name", "email", "phone", "email_hash", "updated_at"])
        w.writerow(["user_7000", "Mira Holt", "mira.holt@example.org", "+1 555 0100", "c" * 64, "2026-01-01T00:00:00Z"])
    with open(Path(d) / "user_audit_log.csv", "w", encoding="utf-8", newline="") as f:
        csv.writer(f, lineterminator="\n").writerow(["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"])
    return identity.load_identities(d)


def run_case(case):
    with tempfile.TemporaryDirectory() as d:
        index = toy_index(d)
    manifest, obs = [], []
    for fid, captured, records in case["files"]:
        meta, o = ledger(fid, captured, records)
        manifest.append(meta)
        obs += o
    text = case.get("text", DELETE_TEXT)
    row = {"case_id": "case_9001", "email": "mira.holt@example.org", "requested_at": REQUESTED, "reported_status": "COMPLETE",
           "reported_at": REPORTED, "audit_at": AUDIT, "request_text": text}
    links = discovery.build_links(obs, index)
    reqs, _, dec = matching.match([row], [], links, index)
    A = state.reconstruct(reqs, dec, obs, links, discovery.classify_resources(obs, links), [])
    E, F, R = policy.run(A, policy.PolicyContext(obs, manifest))
    status, steps, trace = final.assemble("case_9001", intent.classify(text), E, {r.finding_id: r for r in R}, AUDIT)
    steps = json.loads(steps_json(steps))  # exactly what output.csv would contain
    times = {m["file_id"]: datetime.fromisoformat(m["captured_at"].replace("Z", "+00:00")) for m in manifest}
    errors = validate_output.check_steps(steps, status, datetime.fromisoformat(AUDIT.replace("Z", "+00:00")), times, strict=True)
    actual = (status, [(s["phase"], s["operation"], s["resource_id"], s["evidence_id"]) for s in steps])
    return {"name": case["name"], "about": case["about"], "expected": case["expect"], "actual": actual,
            "basis": trace["basis"], "blockers": trace["blockers"], "validator_errors": errors,
            "passed": actual == case["expect"] and not errors}


def run_all():
    return [run_case(c) for c in CASES]


def main():
    results = run_all()
    for r in results:
        print(f"{'PASS' if r['passed'] else 'FAIL'} {r['name']}: {r['actual'][0]} ({r['basis']}) steps={len(r['actual'][1])} "
              f"blockers={r['blockers']} validator_errors={r['validator_errors']}")
        if not r["passed"]:
            print(f"     expected {r['expected']}\n     actual   {r['actual']}")
    print(f"{sum(r['passed'] for r in results)}/{len(results)} custom cases passed")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
