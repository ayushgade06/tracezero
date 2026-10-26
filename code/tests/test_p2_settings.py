"""P2.4: dataset conventions (R4e dump header, screenshot template marker) are named settings whose
defaults are the values previously written into state.py and ocr.py.

Run: python -m unittest discover -s code/tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(CODE / "tests"))

import main  # noqa: E402
from audit import discovery, matching, ocr, parsers, settings, state  # noqa: E402
from test_m4 import CLAIM, MY_AUDIT, TEACH, ex, meta, req, toy_index  # noqa: E402
from test_m6 import META, obs_of, screen  # noqa: E402

DEMO = CODE / "tests" / "fixtures" / "demo"
CUSTOM = settings.Settings(sql_point_in_time_header=("Snapshot Kind", "full export"), screenshot_template_marker="Acme")


def pit_dump(head):
    """Two copies of one table, listing only someone else's record (MY_AUDIT), after a comment header."""
    cols = "(resource_id TEXT PRIMARY KEY, origin_system TEXT, collection_name TEXT, record_json TEXT, updated_at TEXT)"
    body = "".join(f"INSERT INTO \"customer_data_{t}\" VALUES('{rid}','{s}','{c}','{json.dumps(r)}','2026-09-05T00:00:00Z');\n"
                   for t, rid, s, c, r in MY_AUDIT)
    text = (f"{head}-- Captured at: 2026-09-05T00:00:00Z\nBEGIN TRANSACTION;\nCREATE TABLE customer_data_primary {cols};\n"
            f"CREATE TABLE customer_data_read_replica {cols};\n{body}COMMIT;\n")
    return parsers.parse_sql(meta("ev_pit", "sql", "2026-09-05T00:00:00Z"), text)[0]


def account_existence(obs, conventions=settings.DEFAULT):
    obs, idx = TEACH + obs, toy_index()
    links = discovery.build_links(obs, idx)
    requests, _, dec = matching.match([req()], list(CLAIM), links, idx)
    out = state.reconstruct(requests, dec, obs, links, discovery.classify_resources(obs, links), list(CLAIM), conventions)
    return ex({(a.case_id, a.resource_id): a for a in out}[("case_1", "account_1")])["value"]


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def write(self, data):
        path = self.tmp / "settings.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    def bad(self, data, *fragments):
        path = self.write(data)
        with self.assertRaises(settings.SettingsError) as cm:
            settings.load_settings(path)
        for f in (str(path),) + fragments:
            self.assertIn(f, str(cm.exception))

    def test_defaults_are_the_previous_literals(self):
        self.assertEqual(settings.DEFAULT.point_in_time_context, ("ghost_data_still_exists", "point-in-time database snapshot"))
        self.assertEqual(settings.DEFAULT.screenshot_template_marker, "Northstar")
        self.assertEqual(settings.load_settings(self.write({})), settings.DEFAULT)

    def test_load(self):
        got = settings.load_settings(self.write({"sql_point_in_time_header": {"label": " Snapshot Kind ", "value": "full export"},
                                                 "screenshot_template_marker": "Acme"}))
        self.assertEqual(got, CUSTOM)
        self.assertEqual(got.point_in_time_context, ("snapshot_kind", "full export"))
        off = settings.load_settings(self.write({"sql_point_in_time_header": None, "screenshot_template_marker": None}))
        self.assertEqual((off.point_in_time_context, off.screenshot_template_marker), (None, None))

    def test_invalid_files(self):
        self.bad({"sql_point_in_time_heder": None}, "unknown setting 'sql_point_in_time_heder'")
        self.bad({"sql_point_in_time_header": "Ghost"}, "sql_point_in_time_header must be null or")
        self.bad({"sql_point_in_time_header": {"label": "1st kind", "value": "x"}}, "sql_point_in_time_header must be")
        self.bad({"sql_point_in_time_header": {"label": "Kind", "value": " "}}, "sql_point_in_time_header must be")
        self.bad({"screenshot_template_marker": ""}, "screenshot_template_marker must be null or non-empty text")
        self.bad({"screenshot_template_marker": 3}, "screenshot_template_marker must be")
        self.bad("[]", "must be a JSON object")
        self.bad("{,}", "not valid JSON")
        with self.assertRaises(settings.SettingsError) as cm:
            settings.load_settings(self.tmp / "nope.json")
        self.assertIn("nope.json: cannot read settings file", str(cm.exception))

    def test_point_in_time_header_setting_drives_r4e(self):
        reference = pit_dump("-- Ghost Data Still Exists: point-in-time database snapshot\n")
        custom = pit_dump("-- Snapshot Kind: full export\n")
        self.assertEqual(account_existence(reference), "ABSENT")  # unchanged default (test_m4 R4e)
        self.assertEqual(account_existence(custom), "UNKNOWN")
        self.assertEqual(account_existence(custom, CUSTOM), "ABSENT")
        self.assertEqual(account_existence(reference, CUSTOM), "UNKNOWN")
        self.assertEqual(account_existence(reference, settings.Settings(sql_point_in_time_header=None)), "UNKNOWN")

    def test_screenshot_marker_setting_drives_template_recognition(self):
        def recognised(boxes, marker=settings.DEFAULT.screenshot_template_marker):
            obs, diags = ocr.image_observations(META, boxes, {}, 1800, "test", marker)
            return not any(code == "UNRECOGNISED_LAYOUT" for _, code, _ in diags) and any(o.kind == "record" for o in obs)
        northstar = screen()
        self.assertEqual(ocr.image_observations(META, northstar, {}, 1800, "test"), obs_of(northstar))  # default = Northstar
        acme = [[*b[:4], b[4].replace("Northstar", "Acme"), b[5]] for b in northstar]
        self.assertTrue(recognised(northstar))
        self.assertFalse(recognised(acme))
        self.assertTrue(recognised(acme, "Acme"))
        self.assertFalse(recognised(northstar, "Acme"))
        self.assertFalse(recognised(northstar, None))

    def test_cli(self):
        a, b = self.tmp / "a.csv", self.tmp / "b.csv"
        main.main(["--dataset", str(DEMO), "--out", str(a)])
        main.main(["--dataset", str(DEMO), "--out", str(b), "--settings", str(self.write({}))])
        self.assertEqual(a.read_bytes(), b.read_bytes())
        out = self.tmp / "c.csv"
        with self.assertRaises(SystemExit) as cm:
            main.main(["--dataset", str(DEMO), "--out", str(out), "--settings", str(self.write({"x": 1}))])
        self.assertIn("invalid settings:", cm.exception.code)
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
