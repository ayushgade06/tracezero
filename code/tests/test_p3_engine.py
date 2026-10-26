"""P3: the library entry point audit/engine.py (audit -> AuditResult), on the synthetic demo dataset.

Covers: the result agrees with the CLI and with the pipeline it is copied from; JSON serialisation;
sequential audits with different datasets, policies, settings and outputs; the error contract; the CLI
delegating to the library.

Run: python -m unittest discover -s code/tests -p "test_p3_*.py"
"""
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import main  # noqa: E402
from audit import engine, final, pipeline, policy, settings  # noqa: E402
from audit.engine import AuditResult, audit  # noqa: E402
from audit.inputs import InputError  # noqa: E402

DEMO = CODE / "tests" / "fixtures" / "demo"
STATUS = {"case_901": "INCOMPLETE", "case_902": "COMPLETE", "case_903": "INCOMPLETE", "case_904": "NOT_APPLICABLE"}
CUSTOM_POLICY = policy.DEFAULT_POLICY_FILE.read_text(encoding="utf-8").replace(
    "rule_14,BACKUP_ARCHIVE,Restore access can be blocked,RETAIN", "rule_14,BACKUP_ARCHIVE,Always,DELETE")


def csv_rows(path):
    with open(path, encoding="utf-8", newline="") as f:
        return [(r["case_id"], r["deletion_status"], json.loads(r["steps_json"])) for r in csv.DictReader(f)]


def operations(result, case_id):
    return [(s["phase"], s["operation"], s["resource_id"]) for s in result.case(case_id).steps]


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.out = cls.dir / "lib.csv"
        cls.result = audit(DEMO, out=cls.out)
        cls.cli_out = cls.dir / "cli.csv"
        cls.cli = subprocess.run([sys.executable, str(CODE / "main.py"), "--dataset", str(DEMO), "--out", str(cls.cli_out)],
                                 env=dict(os.environ, PYTHONIOENCODING="utf-8"), capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.case_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.case_tmp.cleanup)
        self.work = Path(self.case_tmp.name)

    def copy_demo(self):
        ds = self.work / "ds"
        shutil.copytree(DEMO, ds)
        return ds

    def write(self, name, text):
        path = self.work / name
        path.write_text(text, encoding="utf-8")
        return path

    # basic operation and agreement with the existing path

    def test_returns_an_audit_result_with_every_requested_row(self):
        r = self.result
        self.assertIsInstance(r, AuditResult)
        self.assertEqual({c.case_id: c.deletion_status for c in r.cases}, STATUS)
        self.assertEqual(r.summary, {"rows": 4, "status": {"INCOMPLETE": 2, "COMPLETE": 1, "NOT_APPLICABLE": 1},
                                     "basis": {"FINDINGS": 1, "COMPLIANCE_PROVEN": 1, "BLOCKED": 1, "NON_DELETION_REQUEST": 1},
                                     "steps": 13})
        self.assertEqual((r.dataset, r.requests, r.output),
                         (DEMO.as_posix(), (DEMO / "deletion_requests.csv").as_posix(), self.out.as_posix()))

    def test_csv_is_byte_identical_to_the_cli_and_matches_the_result(self):
        self.assertEqual(self.cli.returncode, 0, self.cli.stderr)
        self.assertEqual(self.out.read_bytes(), self.cli_out.read_bytes())
        self.assertEqual(csv_rows(self.out), [(c.case_id, c.deletion_status, c.steps) for c in self.result.cases])

    def test_cli_stdout_and_stderr_are_unchanged(self):
        self.assertEqual(self.cli.stdout.strip(), f"wrote 4 rows to {self.cli_out}; "
                         "status={'INCOMPLETE': 2, 'COMPLETE': 1, 'NOT_APPLICABLE': 1}; "
                         "basis={'FINDINGS': 1, 'COMPLIANCE_PROVEN': 1, 'BLOCKED': 1, 'NON_DELETION_REQUEST': 1}; steps=13")
        self.assertEqual(self.cli.stderr.splitlines(), [f"warning: {w}" for w in self.result.warnings])

    def test_no_output_written_unless_asked(self):
        r = audit(DEMO)
        self.assertIsNone(r.output)
        self.assertEqual(r.to_dict()["cases"], self.result.to_dict()["cases"])

    def test_trace_comes_from_the_pipeline_and_final_assembly(self):
        piped = pipeline.run(DEMO)
        rows = engine.audit_cases(pipeline.read_requests(DEMO / "deletion_requests.csv"), piped)
        self.assertEqual([(c, s, steps, t) for c, s, steps, t in rows],
                         [(c.case_id, c.deletion_status, c.steps, c.trace) for c in self.result.cases])
        for c in self.result.cases:
            mine = sorted((e for e in piped["E"] if e.case_id == c.case_id), key=lambda e: e.resource_id)
            self.assertEqual(c.evaluations, [e.to_dict() for e in mine])
            self.assertEqual([r["finding_id"] for r in c.remediations], [e.finding.finding_id for e in mine if e.finding])
            self.assertEqual(c.trace["blockers"], final.blockers(mine))

    def test_findings_carry_the_cited_evidence_and_locator(self):
        c = self.result.case("case_901")
        cited = {(s["resource_id"], s["evidence_id"]) for s in c.steps if s["phase"] == "FINDING"}
        findings = [e["finding"] for e in c.evaluations if e["finding"]]
        self.assertEqual({(f["resource_id"], f["citation"]["evidence_id"]) for f in findings}, cited)
        self.assertTrue(all(f["citation"]["locator"] for f in findings))
        self.assertTrue(all({"establishing", "contradicting", "inadmissible"} <= set(f) for f in findings))
        self.assertEqual([r["operation"] for r in c.remediations], ["BLOCK_RESTORE", "DISABLE_DATA_FLOW", "DELETE_RECORD"])
        self.assertEqual(c.trace["basis"], "FINDINGS")

    def test_blocked_and_non_deletion_cases_keep_their_trace(self):
        blocked = self.result.case("case_903")
        self.assertEqual(blocked.trace["basis"], "BLOCKED")
        self.assertTrue(blocked.trace["blockers"])
        self.assertTrue(any(e["outcome"] == "INCONCLUSIVE" for e in blocked.evaluations))
        self.assertFalse(any(s["evidence_id"] for s in blocked.steps))  # missing proof is never cited as a deletion
        na = self.result.case("case_904")
        self.assertEqual((na.deletion_status, na.steps, na.trace["intent"]), ("NOT_APPLICABLE", [], "NON_DELETION"))
        self.assertEqual(set(self.result.case("case_902").trace), {"basis", "blockers", "intent", "intent_rule", "intent_llm"})

    def test_diagnostics(self):
        d = self.result.diagnostics
        with open(DEMO / "evidence_files.csv", encoding="utf-8", newline="") as f:
            manifest = [r["file_id"] for r in csv.DictReader(f)]
        self.assertEqual([x["file_id"] for x in d["files"]], manifest)
        self.assertEqual(sum(x["observations"] for x in d["files"]), len(pipeline.run(DEMO)["obs"]))
        self.assertEqual(d["parser"], [])
        self.assertEqual({k: d["coverage"][k] for k in ("tokens", "represented", "unresolved", "unreadable")},
                         {"tokens": 8, "represented": 8, "unresolved": [], "unreadable": []})
        self.assertEqual(len(self.result.warnings), 1)
        self.assertTrue(self.result.warnings[0].startswith("sql_point_in_time_header: none of the 1 SQL dump(s)"))

    def test_missing_evidence_file_is_a_diagnostic_not_a_failure(self):
        ds = self.copy_demo()
        with open(ds / "evidence_files.csv", encoding="utf-8", newline="") as f:
            first = next(csv.DictReader(f))
        (ds / first["file_path"]).unlink()
        r = audit(ds)
        self.assertIn(first["file_id"], r.diagnostics["coverage"]["unreadable"])
        self.assertIn(("error", "manifest entry has no file"),
                      {(x["severity"], x["reason"]) for x in r.diagnostics["parser"] if x["file_id"] == first["file_id"]})
        self.assertEqual([x for x in r.diagnostics["files"] if x["file_id"] == first["file_id"]][0]["observations"], 0)

    # serialisation

    def test_to_dict_is_json_and_deterministic(self):
        d = self.result.to_dict()
        text = json.dumps(d, sort_keys=True, allow_nan=False)
        self.assertEqual(json.loads(text), d)  # nothing is lost or retyped (no tuples, paths, sets)
        again = audit(DEMO, out=self.work / "again.csv").to_dict()
        self.assertEqual(json.dumps(dict(again, output=None), sort_keys=True), json.dumps(dict(d, output=None), sort_keys=True))
        self.assertEqual(set(d), {"dataset", "requests", "output", "cases", "summary", "warnings", "diagnostics"})
        self.assertEqual(set(d["cases"][0]), {"case_id", "deletion_status", "steps", "trace", "evaluations", "remediations"})
        self.assertIn("locator", text)
        self.assertIn("coverage", text)

    # isolation of sequential audits

    def test_sequential_audits_with_different_datasets_and_outputs(self):
        ds = self.copy_demo()
        rows = (ds / "deletion_requests.csv").read_text(encoding="utf-8").splitlines(keepends=True)
        (ds / "deletion_requests.csv").write_text("".join(r for r in rows if not r.startswith("case_904")), encoding="utf-8")
        a, b = self.work / "a.csv", self.work / "b.csv"
        small = audit(ds, out=a)
        full = audit(DEMO, out=b)
        self.assertEqual([c.case_id for c in small.cases], ["case_901", "case_902", "case_903"])
        self.assertEqual(full.to_dict()["cases"], self.result.to_dict()["cases"])
        self.assertEqual(b.read_bytes(), self.out.read_bytes())
        self.assertEqual(csv_rows(a), csv_rows(b)[:3])

    def test_custom_policy_does_not_leak_into_the_next_audit(self):
        path = self.write("rules.csv", CUSTOM_POLICY)
        for custom in (path, policy.load_policy(path)):  # a path or a loaded matrix
            r = audit(DEMO, policy=custom)
            self.assertIn(("FINDING", "REPORT_REMAINING_RECORD", "backup_9101"), operations(r, "case_901"))
            self.assertEqual(audit(DEMO).to_dict(), dict(self.result.to_dict(), output=None))
        self.assertEqual(policy.POLICY, policy.load_policy(policy.DEFAULT_POLICY_FILE))

    def test_custom_settings_do_not_leak_into_the_next_audit(self):
        path = self.write("settings.json", json.dumps({"sql_point_in_time_header": None}))
        for custom in (path, settings.Settings(sql_point_in_time_header=None)):  # a path or a Settings object
            self.assertEqual(audit(DEMO, settings=custom).warnings, [])
            self.assertEqual(audit(DEMO).to_dict(), dict(self.result.to_dict(), output=None))
        self.assertEqual(settings.DEFAULT, settings.Settings())

    def test_paths_do_not_depend_on_the_working_directory(self):
        cwd = os.getcwd()
        os.chdir(self.work)
        try:
            r = audit(DEMO, out=self.work / "elsewhere.csv")
        finally:
            os.chdir(cwd)
        self.assertEqual((self.work / "elsewhere.csv").read_bytes(), self.out.read_bytes())
        self.assertEqual(r.to_dict()["cases"], self.result.to_dict()["cases"])

    # error contract

    def assert_invalid(self, error, fragment, **kwargs):
        out = self.work / "never.csv"
        with self.assertRaises(error) as cm:
            audit(kwargs.pop("dataset", DEMO), out=out, **kwargs)
        self.assertIsInstance(cm.exception, InputError)
        self.assertIn(fragment, str(cm.exception))
        self.assertFalse(out.exists())

    def test_invalid_dataset_path(self):
        self.assert_invalid(InputError, "dataset directory not found", dataset=self.work / "nope")

    def test_missing_required_input(self):
        ds = self.copy_demo()
        (ds / "user_identities.csv").unlink()
        self.assert_invalid(InputError, "user_identities.csv: file not found", dataset=ds)

    def test_invalid_request_files(self):
        self.assert_invalid(InputError, "request file not found", requests=self.work / "missing.csv")
        bad = self.write("bad.csv", (DEMO / "deletion_requests.csv").read_text(encoding="utf-8").replace("2026-", "yesterday-", 1))
        self.assert_invalid(InputError, "is not an ISO-8601 timestamp", requests=bad)
        other = self.write("other.csv", "case_id,email,requested_at,reported_status,reported_at,audit_at,request_text\n"
                                        "case_999,x@example.org,2026-01-01T00:00:00Z,COMPLETED,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z,delete\n")
        self.assert_invalid(InputError, "case_999", requests=other)

    def test_invalid_policy_file(self):
        self.assert_invalid(policy.PolicyError, "is not one of",
                            policy=self.write("rules.csv", CUSTOM_POLICY.replace(",DELETE", ",PURGE", 1)))

    def test_invalid_settings_file(self):
        self.assert_invalid(settings.SettingsError, "unknown setting", settings=self.write("s.json", '{"x": 1}'))

    def test_internal_failure_propagates_and_writes_nothing(self):
        out = self.work / "never.csv"
        with mock.patch.object(final, "assemble", side_effect=final.AssemblyError("case_901: no citation")):
            with self.assertRaises(final.AssemblyError) as cm:
                audit(DEMO, out=out)
        self.assertNotIsInstance(cm.exception, InputError)  # distinguishable from invalid input
        self.assertFalse(out.exists())

    # CLI delegation

    def test_cli_delegates_to_the_library(self):
        out = self.work / "cli.csv"
        with mock.patch.object(engine, "audit", wraps=engine.audit) as spy:
            main.main(["--dataset", str(DEMO), "--out", str(out), "--policy", str(policy.DEFAULT_POLICY_FILE)])
        spy.assert_called_once()
        self.assertEqual(out.read_bytes(), self.out.read_bytes())


if __name__ == "__main__":
    unittest.main()
