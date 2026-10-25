"""Synthetic demo dataset (tests/fixtures/demo): the real CLI and pipeline, end to end, with no private data.

Every person, identifier and record in the fixture is fictional (example.org / example.net). The expected
rows below were designed into the fixture before it was run: residual data (case_901), proven deletion
via a historical email (case_902), insufficient evidence (case_903) and a non-deletion request (case_904).

Run: python -m unittest discover -s code/tests
"""
import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
sys.path.append(str(CODE / "evaluation"))

import main  # noqa: E402
import validate_output  # noqa: E402
from audit import identity, ingest, pipeline  # noqa: E402

DEMO = CODE / "tests" / "fixtures" / "demo"
REQUESTS = DEMO / "deletion_requests.csv"

EXPECTED = {
    "case_901": ("INCOMPLETE", [
        ("FINDING", "REPORT_REMAINING_RECORD", "search_9101", "evidence_902"),
        ("FINDING", "REPORT_ACTIVE_DATA_FLOW", "data_flow_9101", "evidence_903"),
        ("FINDING", "REPORT_RESTORE_RISK", "backup_9101", "evidence_901"),
        ("REMEDIATION", "DISABLE_DATA_FLOW", "data_flow_9101", ""),
        ("REMEDIATION", "BLOCK_RESTORE", "backup_9101", ""),
        ("REMEDIATION", "DELETE_RECORD", "search_9101", ""),
        ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_9101", ""),
        ("VERIFICATION", "VERIFY_RECORD_ABSENT", "search_9101", ""),
        ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "data_flow_9101", ""),
        ("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "backup_9101", "")]),
    "case_902": ("COMPLETE", [
        ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_9102", "evidence_904"),
        ("VERIFICATION", "VERIFY_RECORD_PRESENT", "audit_record_9102", "evidence_904")]),
    "case_903": ("INCOMPLETE", [("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_9103", "")]),
    "case_904": ("NOT_APPLICABLE", []),
}


def read_rows(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


class DemoFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "output.csv"
        cls.rows = main.run(DEMO, REQUESTS, cls.out)
        cls.result = pipeline.run(DEMO)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_ingests_every_supported_text_format_without_diagnostics(self):
        manifest, obs, diags = ingest.ingest(DEMO)
        self.assertEqual(diags, [])
        self.assertEqual({m["format"] for m in manifest}, {"json", "sql", "log", "txt", "pdf"})
        self.assertEqual({m["file_id"] for m in manifest}, {o.file_id for o in obs})  # every file contributes

    def test_rows_steps_and_citations(self):
        got = {r["case_id"]: (r["deletion_status"], [(s["phase"], s["operation"], s["resource_id"], s["evidence_id"])
                                                    for s in json.loads(r["steps_json"])])
               for r in read_rows(self.out)}
        self.assertEqual(got, EXPECTED)
        self.assertEqual([r["case_id"] for r in read_rows(self.out)], [r["case_id"] for r in read_rows(REQUESTS)])

    def test_output_passes_strict_validator(self):
        errors, warnings = validate_output.validate(self.out, REQUESTS, strict=True)
        self.assertEqual((errors, warnings), ([], []))

    def test_intent_and_matching_are_exercised(self):
        dec = self.result["dec"]
        self.assertEqual((dec["case_901"].family, dec["case_901"].status, dec["case_901"].activity_id),
                         ("account_deletion", "MATCHED", "activity_9002"))
        self.assertEqual((dec["case_904"].family, dec["case_904"].status, dec["case_904"].activity_id),
                         ("request_classification", "MATCHED", "activity_9003"))
        self.assertEqual({c: t["intent"] for c, _, _, t in self.rows},
                         {"case_901": "DELETION", "case_902": "DELETION", "case_903": "DELETION", "case_904": "NON_DELETION"})

    def test_historical_email_resolves_to_its_owner(self):
        r = identity.resolve_request(identity.load_identities(DEMO), "t.reyes@example.com")
        self.assertEqual((r["status"], r["user_ids"]), ("historical", ["user_9002"]))

    def test_insufficient_evidence_blocks_completion(self):
        trace = next(t for c, _, _, t in self.rows if c == "case_903")
        self.assertEqual(trace["blockers"], ["account_9103:STATE_UNKNOWN:HISTORICAL_READINGS_ONLY"])

    def test_cli_output_is_byte_identical_across_processes(self):
        outs = []
        for seed in ("1", "2"):
            out = Path(self.tmp.name) / f"cli_{seed}.csv"
            subprocess.run([sys.executable, str(CODE / "main.py"), "--dataset", str(DEMO), "--out", str(out)],
                           env=dict(os.environ, PYTHONHASHSEED=seed, PYTHONIOENCODING="utf-8"),
                           check=True, capture_output=True)
            outs.append(out.read_bytes())
        self.assertEqual(outs[0], outs[1])
        self.assertEqual(outs[0], self.out.read_bytes())


if __name__ == "__main__":
    unittest.main()
