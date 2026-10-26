"""P5: GET /datasets, the dataset list the web interface offers, on temporary dataset roots.

Covers: only direct subdirectories with the default request file are listed, by id; names POST /audits
would reject, plain files and symlink or junction escapes are skipped; no server path appears; an
unreadable root is a JSON error; the method is checked.

Run: python -m unittest discover -s code/tests -p "test_p5_*.py"
"""
import os
import shutil
import sys
import unittest

from test_p4_api import DEMO, ApiCase  # first: puts code/ on sys.path


class DatasetListTests(ApiCase):
    def listed(self, jobs=None):
        status, data = self.call(jobs or self.jobs(), "GET", "/datasets")
        self.assertEqual(status, 200)
        self.assertNotIn(str(self.root), str(data))
        self.assertNotIn(self.root.as_posix(), str(data))
        return [d["dataset_id"] for d in data["datasets"]]

    def test_lists_datasets_by_id(self):
        shutil.copytree(DEMO, self.root / "b-copy")
        self.assertEqual(self.listed(), ["b-copy", "demo"])

    def test_listed_ids_are_accepted(self):
        jobs = self.jobs()
        for dataset_id in self.listed(jobs):
            self.assertEqual(self.finished(jobs, self.submit(jobs, dataset_id))["status"], "completed")

    def test_skips_non_datasets(self):
        (self.root / "notes.txt").write_text("x", encoding="utf-8")
        (self.root / "empty").mkdir()                       # no deletion_requests.csv
        (self.root / "-bad").mkdir()                        # an id POST /audits rejects
        (self.root / "-bad" / "deletion_requests.csv").write_text("x", encoding="utf-8")
        (self.root / "file_named").mkdir()
        (self.root / "file_named" / "deletion_requests.csv").mkdir()  # not a file
        self.assertEqual(self.listed(), ["demo"])

    def test_skips_symlink_escape(self):
        outside = self.tmp / "outside"
        shutil.copytree(DEMO, outside)
        link = self.root / "escape"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            if sys.platform != "win32":
                self.skipTest("symlinks unavailable")
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
        self.assertTrue((link / "deletion_requests.csv").is_file())
        self.assertEqual(self.listed(), ["demo"])

    def test_empty_root(self):
        shutil.rmtree(self.root / "demo")
        self.assertEqual(self.listed(), [])

    def test_unreadable_root(self):
        jobs = self.jobs()
        shutil.rmtree(self.root)
        status, data = self.call(jobs, "GET", "/datasets")
        self.assertEqual((status, data["error"]["code"]), (503, "datasets_unavailable"))
        self.assertNotIn(str(self.tmp), str(data))

    def test_method(self):
        status, data = self.call(self.jobs(), "POST", "/datasets", {})
        self.assertEqual((status, data["error"]["code"]), (405, "method_not_allowed"))


if __name__ == "__main__":
    unittest.main()
