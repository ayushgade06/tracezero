"""M6 self-tests: OCR cache, layout units, observation safeguards, M4/M5 integration, conflict gate.

Run: python -m unittest discover -s code/tests -v
Most tests use synthetic OCR boxes and the committed cache; the live-engine test is skipped
when rapidocr-onnxruntime is not installed.
"""
import copy
import hashlib
import importlib.util
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(CODE / "evaluation"))
sys.path.insert(0, str(CODE / "tests"))

from audit import ingest, ocr, policy  # noqa: E402

from refdata import DATASET, requires_reference  # noqa: E402
META = {"file_id": "ev_img", "file_path": "evidence/images/x.png", "origin": "console", "captured_at": "2026-09-05T00:00:00Z", "format": "image"}


def box(x, y, text, score=0.99, w=None):
    return [x, y, x + (w or 8 * len(text)), y + 18, text, score]


def screen(header_sub="user_1 · account_77", rows=(("account_77", "one@x.io"), ("account_78", "two@x.io")), panel_title="account_77",
           panel=(("Email", "one@x.io"),), activity=("account_79 created",), search="email:one@x.io", score=0.99):
    """A synthetic screen on the shared admin-console template."""
    b = [box(20, 20, "Northstar Shop"), box(470, 20, search), box(244, 80, "Northstar Shop / Console / Customers"),
         box(246, 105, "Customer page", w=200), box(1600, 107, "Filter"), box(244, 135, header_sub, score),
         box(256, 232, "Email"), box(256, 252, "one@x.io"), box(642, 232, "Phone"), box(642, 252, "+15550101"),
         box(258, 330, "Overview"), box(1438, 330, panel_title), box(256, 371, "ID"), box(452, 371, "Account email")]
    for i, (rid, mail) in enumerate(rows):
        y = 407 + 41 * i
        b += [box(262, y + 3, str(i + 1), w=10), box(279, y, rid, score), box(452, y, mail)]
    for i, (lab, val) in enumerate(panel):
        b += [box(1438, 378 + 33 * i, lab), box(1555, 378 + 33 * i, val)]
    b.append(box(1440, 621, "Activity"))
    for i, t in enumerate(activity):
        b.append(box(1461, 659 + 45 * i, t))
    return b


def obs_of(boxes, aliases=None, meta=META):
    return ocr.image_observations(meta, boxes, aliases or {}, 1800, "test")


class LayoutTests(unittest.TestCase):
    def test_units_records_and_context(self):
        obs, diags = obs_of(screen())
        by = {o.obs_id: o for o in obs}
        head = by["ev_img:header"]
        self.assertEqual((head.kind, head.resource_id), ("record", "account_77"))
        self.assertEqual({k["value"] for k in head.identity_keys}, {"user_1", "one@x.io"})  # KPI joins a record view
        self.assertIn("+15550101", head.attrs["_ocr_direct_identifiers"])  # phone: exposure text only
        self.assertNotIn("phone", {k["kind"] for k in head.identity_keys})
        row2 = by["ev_img:table_row.row2"]
        self.assertEqual((row2.resource_id, [k["value"] for k in row2.identity_keys]), ("account_78", ["two@x.io"]))
        self.assertEqual(row2.attrs["Account email"], "two@x.io")  # cell mapped to its column header
        self.assertEqual((row2.locator["region"], row2.locator["row"]), ("table_row", 2))
        self.assertEqual(len(row2.locator["bbox"]), 4)
        self.assertEqual(row2.attrs["_ocr"]["tokens"][0]["text"], "account_78")
        panel = by["ev_img:detail_panel"]
        self.assertEqual((panel.resource_id, panel.attrs["Email"]), ("account_77", "one@x.io"))
        for o in obs:  # page context never carries identity
            if o.locator["region"] in ("search_query", "activity"):
                self.assertEqual((o.kind, o.identity_keys), ("context", []))
        self.assertEqual(by["ev_img:activity.row1"].related_resource_ids, ["account_79"])
        self.assertEqual(diags, [])
        self.assertTrue(all(o.observed_at is None and o.file_captured_at == META["captured_at"] for o in obs))

    def test_list_page_header_does_not_feed_rows(self):
        obs, _ = obs_of(screen(header_sub="Data exports · 4 jobs", panel_title="Details"))
        by = {o.obs_id: o for o in obs}
        self.assertEqual(by["ev_img:header"].kind, "context")
        self.assertEqual(by["ev_img:kpi"].kind, "context")  # aggregates of a list page stay page context
        self.assertEqual([k["value"] for k in by["ev_img:table_row.row1"].identity_keys], ["one@x.io"])
        self.assertEqual(by["ev_img:detail_panel"].kind, "context")  # panel title is not an id

    def test_low_confidence_tokens(self):
        obs, diags = obs_of(screen(score=0.80))
        by = {o.obs_id: o for o in obs}
        self.assertEqual(by["ev_img:table_row.row1"].kind, "context")
        self.assertIn("LOW_CONFIDENCE_RESOURCE_ID", by["ev_img:table_row.row1"].attrs["_ocr_reasons"])
        self.assertEqual(by["ev_img:table_row.row1"].attrs["_ocr_trust"], "TEXT_ONLY")
        b = screen()
        b[-5][5] = 0.5  # row 2's email (from the end: activity item, heading, panel value, label, row-2 email)
        obs, diags = obs_of(b)
        row2 = {o.obs_id: o for o in obs}["ev_img:table_row.row2"]
        self.assertEqual((row2.kind, row2.identity_keys), ("record", []))
        self.assertTrue(any("LOW_CONFIDENCE_TOKEN" in d[1] for d in diags))

    def test_partial_ambiguous_and_multiple_ids(self):
        obs, diags = obs_of(screen(rows=(("3export_1259", "a@x.io"), ("audit_record_oo3", "b@x.io"), ("account_1 account_2", "c@x.io"))))
        by = {o.obs_id: o for o in obs}
        self.assertTrue(all(by[f"ev_img:table_row.row{i}"].kind == "context" for i in (1, 2, 3)))
        codes = [d[1] for d in diags]
        self.assertIn("PARTIAL_IDENTIFIER", codes)
        self.assertIn("AMBIGUOUS_IDENTIFIER", codes)
        self.assertIn("MULTIPLE_RESOURCE_IDS", by["ev_img:table_row.row3"].attrs["_ocr_reasons"])
        _, d2 = obs_of(screen(rows=(("account_5", "customer_analytics_warehouse"),)))
        self.assertEqual(d2, [])  # ordinary words are not "ambiguous identifiers"

    def test_alias_is_candidate_only(self):
        aliases = {"delivery_confirmation_19.pdf": [{"resource_id": "object_19", "activity_id": "activity_1"}]}
        obs, _ = obs_of(screen(rows=(("delivery_confirmation_19.pdf", "x"),), panel_title="delivery_confirmation_19.pdf"), aliases)
        row = {o.obs_id: o for o in obs}["ev_img:table_row.row1"]
        self.assertEqual((row.kind, row.resource_id, row.identity_keys), ("context", None, []))
        self.assertEqual(row.attrs["_alias_candidates"], ["object_19"])

    def test_unrecognised_layout(self):
        b = [x for x in screen() if x[4] != "Northstar Shop"]
        obs, diags = obs_of(b)
        self.assertEqual([d[1] for d in diags], ["UNRECOGNISED_LAYOUT"])
        self.assertTrue(all(o.kind == "context" for o in obs))

    def test_order_independent(self):
        a = json.dumps([o.to_dict() for o in obs_of(screen())[0]], sort_keys=True)
        shuffled = screen()
        random.Random(7).shuffle(shuffled)
        self.assertEqual(a, json.dumps([o.to_dict() for o in obs_of(shuffled)[0]], sort_keys=True))


class StateGateTests(unittest.TestCase):
    def test_text_only_never_a_reading_and_no_identifier_is_not_redaction(self):
        from audit import state
        obs, _ = obs_of(screen(panel=(("Status", "Active"),)))
        panel = {o.obs_id: o for o in obs}["ev_img:detail_panel"]
        self.assertEqual(state.interpret(panel), ({"existence": "PRESENT"}, ["ocr_visible_unit"]))  # no exposure claim
        weak = copy.deepcopy(panel)
        weak.attrs["_ocr_trust"] = "TEXT_ONLY"
        self.assertEqual(state.interpret(weak), (None, "OCR_TEXT_ONLY"))
        head = {o.obs_id: o for o in obs}["ev_img:header"]
        self.assertEqual(state.interpret(head)[0], {"existence": "PRESENT", "exposure": "IDENTIFIABLE"})


@requires_reference
class CacheTests(unittest.TestCase):
    def setUp(self):
        self.meta = next(m for m in ingest.load_manifest(DATASET) if m["file_id"] == "evidence_60")
        self.cached = json.loads((ocr.CACHE_DIR / "evidence_60.json").read_text(encoding="utf-8"))

    def _with_cache(self, entry):
        d = tempfile.mkdtemp()
        (Path(d) / "evidence_60.json").write_text(json.dumps(entry), encoding="utf-8")
        return d

    def test_cache_key_and_mismatch(self):
        boxes, source, diags = ocr.load_boxes(self.meta, DATASET)
        self.assertEqual((source, diags), ("cache", []))
        self.assertEqual(self.cached["image_sha256"], hashlib.sha256((DATASET / self.meta["file_path"]).read_bytes()).hexdigest())
        orig = ocr.run_engine
        ocr.run_engine = lambda path: (_ for _ in ()).throw(ImportError("no engine"))
        try:
            for field, value in (("image_sha256", "0" * 64), ("config", {"intra_op_num_threads": 4}), ("engine_version", "9.9")):
                bad = dict(self.cached, **{field: value})
                boxes, source, diags = ocr.load_boxes(self.meta, DATASET, cache_dir=self._with_cache(bad))
                self.assertIsNone(boxes, field)
                self.assertEqual([d[1] for d in diags], ["OCR_CACHE_MISMATCH", "OCR_UNAVAILABLE"], field)
            obs, diags = ocr.parse_image(self.meta, DATASET, {}, cache_dir=tempfile.mkdtemp())  # no cache, no engine
            self.assertEqual((obs, diags[0]["severity"]), ([], "error"))
            self.assertIn("OCR_UNAVAILABLE", diags[0]["reason"])
        finally:
            ocr.run_engine = orig

    def test_cache_records_engine_models_preprocessing(self):
        for f in sorted(ocr.CACHE_DIR.glob("*.json")):
            c = json.loads(f.read_text(encoding="utf-8"))
            self.assertEqual({k: c[k] for k in ("engine", "engine_version", "models", "config", "preprocess")},
                             {k: v for k, v in ocr.cache_identity("").items() if k != "image_sha256"})

    @unittest.skipUnless(importlib.util.find_spec("rapidocr_onnxruntime"), "rapidocr-onnxruntime not installed")
    def test_live_engine_matches_cache(self):
        self.assertEqual(ocr.run_engine(DATASET / self.meta["file_path"]), self.cached["boxes"])


@requires_reference
class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import ocr_report
        cls.R = ocr_report
        cls.images = sorted(DATASET.glob("evidence/images/*.png"))
        cls.before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in cls.images}
        cls.post = ocr_report.pipeline(DATASET, use_ocr=True, conflict_gate=True)
        cls.pre = ocr_report.pipeline(DATASET, use_ocr=False, conflict_gate=False)
        cls.img = [o for o in cls.post["obs"] if o.file_format == "image"]

    def test_all_eleven_processed_from_cache_and_images_untouched(self):
        ids = [m["file_id"] for m in self.post["manifest"] if m["format"] == "image"]
        self.assertEqual(len(ids), 11)
        self.assertEqual(sorted({o.file_id for o in self.img}), sorted(ids))
        self.assertTrue(all(o.attrs["_ocr"]["source"] == "cache" for o in self.img))
        self.assertEqual({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.images}, self.before)

    def test_checklist_and_reported_misses(self):
        rows = self.R.checklist(self.post["obs"])
        missed = [(r[0], r[1]) for r in rows if r[3] == "not extracted"]
        self.assertEqual(missed, [("evidence_23", "export_1259")])
        self.assertTrue(any(d["file_id"] == "evidence_23" and "PARTIAL_IDENTIFIER" in d["reason"] for d in self.post["diags"]))
        self.assertTrue(any(d["file_id"] == "evidence_37" and "audit_record_oo3" in d["reason"] for d in self.post["diags"]))

    def test_cache_introduces_no_facts(self):
        for o in self.img:
            cached = {b[4].strip() for b in json.loads((ocr.CACHE_DIR / f"{o.file_id}.json").read_text(encoding="utf-8"))["boxes"]}
            for t in o.attrs["_ocr"]["tokens"]:
                self.assertIn(t["text"], cached)
            for k in o.identity_keys:
                self.assertTrue(any(k["value"] in c.lower() for c in cached), k)

    def test_timestamps_and_cutoff(self):
        cap = {m["file_id"]: m["captured_at"] for m in self.post["manifest"]}
        for o in self.img:
            self.assertIsNone(o.observed_at)
            self.assertEqual(o.file_captured_at, cap[o.file_id])
        for a in self.post["A"]:
            for asp in a.aspects.values():
                for d in asp["deciding"]:
                    if d["evidence_id"] in cap and d["evidence_id"] in {o.file_id for o in self.img}:
                        self.assertEqual(d["time"], cap[d["evidence_id"]])
                        self.assertLessEqual(d["time"], a.audit_at)

    def test_identity_safeguards(self):
        decoy = [l for l in self.post["links"] if l.resource_id == "search_1874"]
        self.assertTrue(decoy and all(l.user_id is None for l in decoy))
        self.assertEqual(self.post["res"]["marketing_001"]["status"], "conflicting")
        ev = next(e for e in self.post["E"] if (e.case_id, e.resource_id) == ("case_63", "marketing_001"))
        # M6.1 (A10): the screenshot's unregistered email is an unresolved identifier, not a rival owner;
        # its reading is still excluded for the requester (OTHER_SUBJECT_INSTANCE), so state stays unknown.
        self.assertEqual((ev.outcome, ev.reason), ("INCONCLUSIVE", "STATE_UNKNOWN:NO_ADMISSIBLE_STATE_OBSERVATION"))
        self.assertIn("UNRESOLVED_IDENTIFIER:email:sj.park@naver.com", ev.notes)
        self.assertFalse(any(l.evidence_id == "evidence_46" or l.evidence_id == "evidence_42" for l in self.post["links"]))

    def test_flow_and_restore_never_from_screenshots(self):
        for a in self.post["A"]:
            for asp in ("flow", "restore"):
                self.assertFalse(any(d["evidence_id"] in {o.file_id for o in self.img} for d in a.aspects[asp]["deciding"]))

    def test_integration_effects(self):
        key = lambda r: [json.dumps(o.to_dict(), sort_keys=True, default=str) for o in r["obs"] if o.file_format != "image"]  # noqa: E731
        self.assertEqual(key(self.pre), key(self.post))
        f = lambda r: {(x.case_id, x.resource_id, x.finding_type) for x in r["F"]}  # noqa: E731
        # M6.1 (A9): case_33 / export_1260 is no longer added: its only screenshot reading predates the request.
        self.assertEqual(f(self.post) - f(self.pre), {
            ("case_34", "account_5762", "REPORT_REMAINING_RECORD"),
            ("case_35", "search_428", "REPORT_REMAINING_RECORD"), ("case_50", "analytics_7438", "REPORT_EXPOSED_PII"),
            ("case_52", "export_1260", "REPORT_REMAINING_RECORD"), ("case_53", "support_1034", "REPORT_EXPOSED_PII")})
        self.assertEqual(f(self.pre) - f(self.post), set())
        dup = [x for x in self.post["F"] if x.resource_id == "search_428"]  # row + panel: one finding
        self.assertEqual(len(dup), 1)
        self.assertEqual(sorted(r["ref"] for r in dup[0].establishing), ["evidence_09:detail_panel", "evidence_09:table_row.row1"])

    def test_deterministic_across_processes(self):
        script = ("import sys,json,hashlib;sys.path.insert(0,'code');from audit import ingest;"
                  "o=[x.to_dict() for x in ingest.ingest(%r)[1] if x.file_format=='image'];"
                  "print(hashlib.sha256(json.dumps(o,sort_keys=True,default=str).encode()).hexdigest())") % str(DATASET)
        out = {subprocess.run([sys.executable, "-c", script], cwd=CODE.parent, capture_output=True, text=True, check=True,
                              env=dict(os.environ, PYTHONHASHSEED=s)).stdout for s in ("5", "6")}
        self.assertEqual(len(out), 1)


class ConflictGateTests(unittest.TestCase):
    def _assessment(self, conflicting):
        import test_m5 as m5
        a = m5.make("account_1", existence=m5.present())
        a.association = dict(a.association, conflicting_owners=conflicting)
        return a, m5.ctx()

    def test_conflicting_direct_owners_withhold_finding(self):
        a, c = self._assessment(["user_2"])
        e = policy.evaluate(a, c)
        self.assertEqual((e.outcome, e.reason, e.finding), ("NOT_ATTRIBUTABLE", "OWNERSHIP_CONFLICT", None))
        self.assertEqual(e.potential_violation["conflicting_owners"], ["user_2"])  # kept, not discarded

    def test_uncontested_owner_regression_and_gate_off(self):
        a, c = self._assessment([])
        self.assertEqual(policy.evaluate(a, c).outcome, "VIOLATION")
        a, c = self._assessment(["user_2"])
        c.conflict_gate = False
        self.assertEqual(policy.evaluate(a, c).outcome, "VIOLATION")

    def test_other_period_owner_is_reuse_not_conflict(self):
        import test_m4 as m4
        march = m4.ledger("ev_1", "2026-03-05T00:00:00Z", [m4.lookup("account_7", "FOUND", "2026-03-05T00:00:00Z", who="two@x.io")])
        sept = m4.ledger("ev_2", "2026-09-05T00:00:00Z", [m4.lookup("account_7", "FOUND", "2026-09-05T00:00:00Z", who="one@x.io")])
        a = m4.assess(march + sept)[0][("case_1", "account_7")]
        self.assertEqual(a.association["competing_owners"], ["user_2"])
        self.assertEqual(a.association["conflicting_owners"], [])  # March owner, September request
        same = m4.ledger("ev_3", "2026-09-04T00:00:00Z", [m4.lookup("account_7", "FOUND", "2026-09-04T00:00:00Z", who="two@x.io")])
        b = m4.assess(sept + same)[0][("case_1", "account_7")]
        self.assertEqual(b.association["conflicting_owners"], ["user_2"])


if __name__ == "__main__":
    unittest.main()
