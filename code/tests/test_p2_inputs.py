"""P2.3: CSV inputs are validated before the audit, and every problem is reported with file and line.

Each test edits a temporary copy of the synthetic demo dataset.
Run: python -m unittest discover -s code/tests
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import main  # noqa: E402
from audit import inputs, pipeline  # noqa: E402

DEMO = CODE / "tests" / "fixtures" / "demo"
SAMPLE, MAIN = pipeline.REQUEST_FILES


class InputValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ds = Path(self.tmp.name) / "ds"
        shutil.copytree(DEMO, self.ds)
        self.out = Path(self.tmp.name) / "out.csv"

    def edit(self, name, old, new, count=1):
        path = self.ds / name
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, count), encoding="utf-8")

    def errors(self, *fragments):
        with self.assertRaises(inputs.InputError) as cm:
            pipeline.run(self.ds)
        for f in fragments:
            self.assertIn(f, str(cm.exception))
        return str(cm.exception)

    def test_demo_and_reference_style_inputs_pass(self):
        inputs.validate(self.ds, pipeline.request_files(self.ds))

    def test_dataset_directory_missing(self):
        with self.assertRaises(SystemExit) as cm:
            main.main(["--dataset", str(self.ds / "nope"), "--out", str(self.out)])
        self.assertIn("nope: dataset directory not found", cm.exception.code)
        with self.assertRaises(inputs.InputError) as cm:
            pipeline.run(self.ds / "nope")
        self.assertIn("dataset directory not found", str(cm.exception))

    def test_missing_file(self):
        (self.ds / "user_identities.csv").unlink()
        self.errors(f"{self.ds / 'user_identities.csv'}: file not found")

    def test_missing_required_column(self):
        self.edit("evidence_files.csv", "file_id,file_path,origin,captured_at", "file_id,path,origin,captured_at")
        self.errors("evidence_files.csv: header ['file_id', 'path', 'origin', 'captured_at']: missing required column(s) ['file_path']")

    def test_extra_columns(self):
        self.edit("user_identities.csv", "updated_at\n", "updated_at,team\n")  # allowed: the engine ignores it
        self.edit("user_identities.csv", "Z\n", "Z,x\n", count=-1)
        self.edit("system_activity.csv", "details\n", "details,team\n")  # not allowed: rows become Event(**row)
        msg = self.errors("system_activity.csv: header", "unexpected column(s) ['team']")
        self.assertNotIn("user_identities", msg)

    def test_row_problems_name_the_line(self):
        self.edit(MAIN, "case_902,", ",")
        self.edit(MAIN, "2026-09-10T00:00:00Z,I want", "10/09/2026,I want")
        self.edit("system_activity.csv", "activity_9002,", "activity_9001,")
        self.errors(f"{MAIN}:3: empty case_id; audit_at '10/09/2026' is not an ISO-8601 timestamp",
                    "system_activity.csv:3: duplicate activity_id 'activity_9001' (first at ")

    def test_row_with_wrong_number_of_fields(self):
        self.edit("evidence_files.csv", ",demo_lookup_ledger,", ",demo_lookup_ledger,x,")
        self.errors("evidence_files.csv:2: number of fields differs from the header")

    def test_duplicate_case_id_across_request_files(self):
        row = (self.ds / MAIN).read_text(encoding="utf-8").splitlines()[1]
        with open(self.ds / SAMPLE, "a", encoding="utf-8", newline="") as f:
            f.write(row + "\n")
        self.errors(f"{MAIN}:2: duplicate case_id 'case_901' (first at {self.ds / SAMPLE}:2)")

    def test_zero_byte_file_has_no_rows(self):
        (self.ds / "user_audit_log.csv").write_bytes(b"")
        inputs.validate(self.ds, pipeline.request_files(self.ds))

    def test_not_utf8(self):
        (self.ds / "user_identities.csv").write_bytes("user_id,name\nuser_1,J\xf6rg\n".encode("latin-1"))
        self.errors("user_identities.csv: not UTF-8 text")

    def test_every_problem_is_reported_and_long_lists_are_capped(self):
        (self.ds / "user_audit_log.csv").unlink()
        rows = "".join(f",x@example.org,bad,COMPLETE,bad,bad,text {i}\n" for i in range(25))
        with open(self.ds / MAIN, "a", encoding="utf-8", newline="") as f:
            f.write(rows)
        msg = self.errors("user_audit_log.csv: file not found", f"{MAIN}: ... and 5 more row(s) with problems")
        self.assertEqual(msg.count(f"{MAIN}:"), 21)

    def test_cli_reports_and_writes_nothing(self):
        self.edit(MAIN, "case_903,", ",")
        with self.assertRaises(SystemExit) as cm:
            main.main(["--dataset", str(self.ds), "--out", str(self.out)])
        self.assertTrue(cm.exception.code.startswith("invalid input:\n"))
        self.assertIn(f"{MAIN}:4: empty case_id", cm.exception.code)
        self.assertFalse(self.out.exists())

    def test_requests_file_outside_the_dataset_is_checked(self):
        other = Path(self.tmp.name) / "mine.csv"
        other.write_text("case_id,email\ncase_901,mira.holt@example.org\n", encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            main.main(["--dataset", str(self.ds), "--requests", str(other), "--out", str(self.out)])
        self.assertIn("mine.csv: header ['case_id', 'email']: missing required column(s)", cm.exception.code)


if __name__ == "__main__":
    unittest.main()
