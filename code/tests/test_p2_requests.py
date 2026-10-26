"""P2.2: either request file may be absent; the files present are still matched jointly.

Each test audits a temporary copy of the synthetic demo dataset (its sample file is header-only).
Run: python -m unittest discover -s code/tests
"""
import csv
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import main  # noqa: E402
from audit import pipeline  # noqa: E402

DEMO = CODE / "tests" / "fixtures" / "demo"
SAMPLE, MAIN = pipeline.REQUEST_FILES


def rows_by_case(path):
    with open(path, encoding="utf-8", newline="") as f:
        return {r["case_id"]: r for r in csv.DictReader(f)}


class RequestFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base_out = Path(cls.tmp.name) / "base.csv"
        main.run(DEMO, DEMO / MAIN, cls.base_out)
        cls.base = cls.base_out.read_bytes()
        cls.base_dec = {c: d.to_dict() for c, d in pipeline.run(DEMO)["dec"].items()}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=self.tmp.name))
        self.ds = self.dir / "ds"
        shutil.copytree(DEMO, self.ds)
        self.out = self.dir / "out.csv"

    def split(self, move):
        """Move the request rows whose case_id is in `move` from deletion_requests.csv to the sample file."""
        with open(self.ds / MAIN, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            fields, rows = reader.fieldnames, list(reader)
        for name, keep in ((SAMPLE, lambda r: r["case_id"] in move), (MAIN, lambda r: r["case_id"] not in move)):
            with open(self.ds / name, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fields, lineterminator="\n")
                w.writeheader()
                w.writerows(r for r in rows if keep(r))

    def test_both_files(self):
        self.assertEqual([p.name for p in pipeline.request_files(self.ds)], [SAMPLE, MAIN])
        main.run(self.ds, self.ds / MAIN, self.out)
        self.assertEqual(self.out.read_bytes(), self.base)

    def test_absent_or_empty_sample_file_is_a_header_only_one(self):
        (self.ds / SAMPLE).unlink()
        self.assertEqual([p.name for p in pipeline.request_files(self.ds)], [MAIN])
        main.run(self.ds, self.ds / MAIN, self.out)
        self.assertEqual(self.out.read_bytes(), self.base)
        (self.ds / SAMPLE).write_bytes(b"")  # zero bytes, not even a header: no rows, as before P2
        main.run(self.ds, self.ds / MAIN, self.out)
        self.assertEqual(self.out.read_bytes(), self.base)

    def test_only_the_sample_file(self):
        self.split({"case_901", "case_902", "case_903", "case_904"})
        (self.ds / MAIN).unlink()
        self.assertEqual([p.name for p in pipeline.request_files(self.ds)], [SAMPLE])
        main.run(self.ds, self.ds / SAMPLE, self.out)
        self.assertEqual(self.out.read_bytes(), self.base)

    def test_requests_file_missing(self):
        (self.ds / MAIN).unlink()
        with self.assertRaises(SystemExit) as cm:
            main.main(["--dataset", str(self.ds), "--out", str(self.out)])
        self.assertIn("invalid input", cm.exception.code)
        self.assertIn(f"{MAIN}: request file not found (choose one with --requests)", cm.exception.code)
        self.assertFalse(self.out.exists())

    def test_both_files_missing(self):
        (self.ds / MAIN).unlink()
        (self.ds / SAMPLE).unlink()
        with self.assertRaises(pipeline.InputError) as cm:
            pipeline.run(self.ds)
        self.assertIn(f"no request file; expected at least one of ['{SAMPLE}', '{MAIN}']", str(cm.exception))
        with self.assertRaises(SystemExit) as cm:
            main.main(["--dataset", str(self.ds), "--requests", str(DEMO / MAIN), "--out", str(self.out)])
        self.assertIn("no request file", cm.exception.code)
        self.assertFalse(self.out.exists())

    def test_requests_split_across_files_are_matched_jointly(self):
        self.split({"case_902"})
        result = pipeline.run(self.ds)
        self.assertEqual(sorted(r["case_id"] for r in result["rows"]), ["case_901", "case_902", "case_903", "case_904"])
        self.assertEqual({c: d.to_dict() for c, d in result["dec"].items()}, self.base_dec)
        base = rows_by_case(self.base_out)
        main.run(self.ds, self.ds / MAIN, self.out)
        self.assertEqual(rows_by_case(self.out), {c: r for c, r in base.items() if c != "case_902"})
        main.run(self.ds, self.ds / SAMPLE, self.out)
        self.assertEqual(rows_by_case(self.out), {"case_902": base["case_902"]})

    def test_requested_rows_must_be_in_the_dataset_request_files(self):
        self.split({"case_902"})
        (self.ds / SAMPLE).unlink()  # case_902 is no longer in any of the dataset's request files
        with self.assertRaises(SystemExit) as cm:
            main.run(self.ds, DEMO / MAIN, self.out)
        self.assertIn("case_902", str(cm.exception.code))


if __name__ == "__main__":
    unittest.main()
