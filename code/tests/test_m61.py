"""M6.1 self-tests: historical readings (A9), category provenance (T2), conflict window and
unresolved identifiers (A10), screenshot capture-time disputes (T4), corpus effects.

Run: python -m unittest discover -s code/tests -v
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(CODE / "evaluation"))
sys.path.insert(0, str(CODE / "tests"))

from audit import ocr, policy, state  # noqa: E402
import test_m4 as m4  # noqa: E402
import test_m5 as m5  # noqa: E402

DATASET = CODE.parent / "dataset"
REQUESTED = "2026-09-01T00:00:00Z"  # test_m4.REQ: requested 09-01, audit 09-10 -> conflict window from 07-03


def rd(copy, time, value, aspect="existence"):
    return state.Reading(f"ev_{copy}:{time}", f"ev_{copy}", "account_1", "snapshot", copy, time, None, time,
                         {aspect: value}, ["x"], (), False, {})


def resolve(readings, aspect="existence"):
    return state._resolve(aspect, readings, [], [], REQUESTED)


class HistoricalReadingTests(unittest.TestCase):
    def test_historical_present_and_absent_never_decide(self):
        for value in ("PRESENT", "ABSENT"):
            st = resolve([rd("primary", "2026-08-01T00:00:00Z", value)])
            self.assertEqual((st.value, st.basis, st.deciding, st.reasons), ("UNKNOWN", "NONE", [], ["HISTORICAL_READINGS_ONLY"]))
            self.assertEqual([s["note"][:10] for s in st.supporting], ["HISTORICAL"])  # kept as context, with provenance
        # end to end: a pre-request NOT_FOUND cannot make the resource ABSENT (no stale route to COMPLETE)
        for result in ("FOUND", "NOT_FOUND"):
            obs = m4.ledger("ev_1", "2026-08-20T00:00:00Z", [m4.lookup("account_1", result, "2026-08-20T00:00:00Z")])
            a = m4.assess(obs)[0][("case_1", "account_1")]
            self.assertEqual((m4.ex(a)["value"], m4.ex(a)["reasons"], a.summary), ("UNKNOWN", ["HISTORICAL_READINGS_ONLY"], "UNKNOWN"))

    def test_reading_exactly_at_requested_at_is_current(self):
        st = resolve([rd("primary", REQUESTED, "PRESENT")])
        self.assertEqual((st.value, st.basis), ("PRESENT", "OBSERVED"))
        self.assertEqual(resolve([rd("primary", "2026-08-31T23:59:59Z", "PRESENT")]).value, "UNKNOWN")

    def test_current_reading_supersedes_history(self):
        st = resolve([rd("primary", "2026-08-01T00:00:00Z", "PRESENT"), rd("primary", "2026-09-05T00:00:00Z", "ABSENT")])
        self.assertEqual((st.value, [d["time"] for d in st.deciding]), ("ABSENT", ["2026-09-05T00:00:00Z"]))
        self.assertEqual([c["note"] for c in st.contradicting], ["earlier reading of this copy"])

    def test_mixed_copies(self):
        # a current PRESENT copy establishes presence (R2); the historical copy neither overrides it nor counts as drift
        st = resolve([rd("primary", "2026-08-01T00:00:00Z", "ABSENT"), rd("read_replica", "2026-09-05T00:00:00Z", "PRESENT")])
        self.assertEqual((st.value, st.per_copy, st.reasons), ("PRESENT", {"primary": "UNKNOWN", "read_replica": "PRESENT"}, []))
        # a current ABSENT copy cannot vouch for a copy last seen before the request: unresolved, not ABSENT
        st = resolve([rd("primary", "2026-08-01T00:00:00Z", "PRESENT"), rd("read_replica", "2026-09-05T00:00:00Z", "ABSENT")])
        self.assertEqual((st.value, st.reasons), ("UNKNOWN", ["HISTORICAL_COPY_UNRESOLVED"]))
        self.assertEqual(st.per_copy, {"primary": "UNKNOWN", "read_replica": "ABSENT"})  # per-copy state preserved
        st = resolve([rd("primary", "2026-08-01T00:00:00Z", "IDENTIFIABLE", "exposure"),
                      rd("read_replica", "2026-09-05T00:00:00Z", "IDENTIFIABLE", "exposure")], "exposure")
        self.assertEqual(st.value, "IDENTIFIABLE")


class CategoryProvenanceTests(unittest.TestCase):
    def _exposed(self, rid, categories, admissible):
        a = m5.make(rid, categories, existence=m5.present(),
                    exposure=m5.aspect("IDENTIFIABLE", "OBSERVED", [m5.ref("ev_1", exposure="IDENTIFIABLE")]))
        a.categories_admissible = admissible
        return a

    def test_post_audit_only_category_is_flagged_not_changed(self):
        e, r = m5.run_one(self._exposed("support_7", ["SUPPORT_MESSAGE"], []))
        self.assertEqual((e.outcome, e.category, e.finding.finding_type), ("VIOLATION", "SUPPORT_MESSAGE", "REPORT_EXPOSED_PII"))
        self.assertIn("CATEGORY_POST_AUDIT_ONLY", e.notes)
        self.assertIn("CATEGORY_POST_AUDIT_ONLY", e.finding.notes)
        self.assertIn("CATEGORY_POST_AUDIT_ONLY", r.caveats)
        c = m5.ctx()  # a support signal captured after audit_at establishes nothing at the cutoff
        c.support_signals["support_8"] = {("SUPPORT_MESSAGE", "2026-09-12T00:00:00Z")}
        e, _ = m5.run_one(self._exposed("support_8", [], []), c)
        self.assertEqual((e.category, e.category_basis), ("SUPPORT_MESSAGE", "support_content_signal"))
        self.assertIn("CATEGORY_POST_AUDIT_ONLY", e.notes)

    def test_sources_disagree_is_flagged(self):
        c = m5.ctx()
        c.support_signals["support_9"] = {("SUPPORT_MESSAGE", "2026-09-05T00:00:00Z")}
        e, _ = m5.run_one(self._exposed("support_9", ["ticket_attachments"], []), c)
        self.assertEqual(e.category, "SUPPORT_ATTACHMENT")  # outcome logic unchanged (option B)
        self.assertIn("CATEGORY_SOURCES_DISAGREE:admissible=SUPPORT_MESSAGE", e.notes)

    def test_admissible_category_and_uncomputed_provenance_are_not_flagged(self):
        for admissible in (["SUPPORT_MESSAGE"], None):
            e, _ = m5.run_one(self._exposed("support_6", ["SUPPORT_MESSAGE"], admissible))
            self.assertFalse([n for n in e.notes if n.startswith("CATEGORY_")], admissible)


class ConflictWindowTests(unittest.TestCase):
    SUBJECT = m4.ledger("ev_s", "2026-09-05T00:00:00Z", [m4.lookup("account_7", "FOUND", "2026-09-05T00:00:00Z")])

    def rival(self, when, captured=None, who="two@x.io", fid="ev_r"):
        return m4.ledger(fid, captured or when, [m4.lookup("account_7", "FOUND", when, who=who)])

    def assess(self, obs):
        return m4.assess(self.SUBJECT + obs)[0][("case_1", "account_7")]

    def test_post_audit_owner_is_not_a_conflict(self):
        self.assertEqual(self.assess(self.rival("2026-09-12T00:00:00Z")).association["conflicting_owners"], [])
        late_capture = self.rival("2026-09-05T00:00:00Z", captured="2026-09-12T00:00:00Z")  # dated in window, captured later
        self.assertEqual(self.assess(late_capture).association["conflicting_owners"], [])

    def test_window_boundaries(self):
        for when, expected in (("2026-07-03T00:00:00Z", ["user_2"]), ("2026-07-02T23:59:59Z", []),  # requested_at - 60d
                               ("2026-09-10T00:00:00Z", ["user_2"]), ("2026-09-10T00:00:01Z", [])):  # audit_at
            self.assertEqual(self.assess(self.rival(when)).association["conflicting_owners"], expected, when)

    def test_unregistered_identifier_is_ambiguity_not_a_rival(self):
        a = self.assess(self.rival("2026-09-06T00:00:00Z", who="agent@northstar.shop"))
        self.assertEqual(a.association["conflicting_owners"], [])
        self.assertIn("UNRESOLVED_IDENTIFIER:email:agent@northstar.shop", a.ambiguity)
        e = policy.evaluate(a, m5.ctx())
        self.assertEqual((e.outcome, e.finding.finding_type), ("VIOLATION", "REPORT_REMAINING_RECORD"))  # not suppressed
        self.assertIn("UNRESOLVED_IDENTIFIER:email:agent@northstar.shop", e.notes)

    def test_genuine_registered_rival_withholds_the_finding(self):
        a = self.assess(self.rival("2026-09-06T00:00:00Z"))
        e = policy.evaluate(a, m5.ctx())
        self.assertEqual((e.outcome, e.reason, e.finding), ("NOT_ATTRIBUTABLE", "OWNERSHIP_CONFLICT", None))
        self.assertEqual(e.potential_violation["conflicting_owners"], ["user_2"])

    def test_co_subject_is_not_a_rival(self):
        shared = m4.lookup("account_7", "FOUND", "2026-09-05T00:00:00Z")
        shared["customer_email"] = "two@x.io"  # one record names both people
        a = m4.assess(m4.ledger("ev_s", "2026-09-05T00:00:00Z", [shared]))[0][("case_1", "account_7")]
        self.assertEqual((a.association["competing_owners"], a.association["conflicting_owners"]), (["user_2"], []))
        self.assertEqual(policy.evaluate(a, m5.ctx()).outcome, "VIOLATION")

    def test_missing_evidence_reuse_and_uncontested_owner(self):
        for extra in ([], self.rival("2026-03-05T00:00:00Z")):  # no rival at all; a rival only in March (ID reuse)
            a = self.assess(extra)
            self.assertEqual(a.association["conflicting_owners"], [])
            self.assertFalse([x for x in a.ambiguity if x.startswith("UNRESOLVED_IDENTIFIER")])
            self.assertEqual(policy.evaluate(a, m5.ctx()).outcome, "VIOLATION")


class CaptureTimeTests(unittest.TestCase):
    def test_only_backward_looking_labels_after_capture_are_flagged(self):
        unit = lambda attrs: SimpleNamespace(obs_id="ev:unit", attrs=attrs)  # noqa: E731
        cap = "2026-06-18T12:00:00Z"
        self.assertEqual(ocr._capture_disputes([unit({"Created": "20 Jun 14:18 UTC"})], cap),
                         [{"ref": "ev:unit", "label": "Created", "value": "20 Jun 14:18 UTC"}])
        self.assertEqual(len(ocr._capture_disputes([unit({"indexed_at": "2026-06-19T00:00:00Z"})], cap)), 1)
        for attrs in ({"Expires": "25 Jun 2026 06:42 UTC"}, {"Expected": "26 Jun 2026 06:42 UTC"},  # forward-looking
                      {"Retention end": "26 Jul 2026"}, {"Created": "18 Jun 06:42 UTC"},  # earlier than capture
                      {"Last contact": "24Sep202609:14JST"}, {"Executed": "06:00:14"}):  # other zone / no date
            self.assertEqual(ocr._capture_disputes([unit(attrs)], cap), [], attrs)


class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import ocr_report
        cls.p = ocr_report.pipeline(DATASET, use_ocr=True, conflict_gate=True)
        cls.E = {(e.case_id, e.resource_id): e for e in cls.p["E"]}
        cls.A = {(a.case_id, a.resource_id): a for a in cls.p["A"]}

    def test_case_outcomes(self):
        # 748 at M6.1; +6 since the worker-operation support category (case_10, case_18, case_47, case_198, case_201, case_287)
        self.assertEqual((len(self.p["F"]), len(self.p["R"])), (754, 754))
        e = self.E[("case_33", "export_1260")]
        self.assertEqual((e.outcome, e.reason, e.finding), ("INCONCLUSIVE", "STATE_UNKNOWN:HISTORICAL_READINGS_ONLY", None))
        self.assertEqual({s["evidence_id"] for s in self.A[("case_33", "export_1260")].aspects["existence"]["supporting"]}, {"evidence_23"})
        e = self.E[("case_52", "export_1260")]  # same screenshot, read after case_52's request: still a finding, flagged
        self.assertEqual((e.outcome, e.notes), ("VIOLATION", ["CAPTURE_TIME_DISPUTED:evidence_23"]))
        e = self.E[("case_53", "support_1034")]
        self.assertEqual((e.outcome, e.category, e.category_basis), ("VIOLATION", "SUPPORT_MESSAGE", "collection_name"))
        self.assertIn("CATEGORY_POST_AUDIT_ONLY", e.finding.notes)
        e = self.E[("case_63", "marketing_001")]
        self.assertEqual((e.outcome, e.notes), ("INCONCLUSIVE", ["UNRESOLVED_IDENTIFIER:email:sj.park@naver.com"]))
        self.assertTrue(self.E[("case_15", "data_flow_9846")].finding)  # flow state is current; only existence is historical
        self.assertEqual(self.A[("case_15", "data_flow_9846")].aspects["existence"]["value"], "UNKNOWN")
        self.assertEqual(self.A[("case_17", "account_6523")].aspects["existence"]["value"], "UNKNOWN")

    def test_conflicts_and_capture_dispute(self):
        self.assertFalse([k for k, a in self.A.items() if a.association.get("conflicting_owners")])
        disputes = [d for d in self.p["diags"] if d["reason"].startswith("OCR_ONSCREEN_TIME_AFTER_CAPTURE")]
        self.assertEqual([(d["file_id"], d["severity"]) for d in disputes], [("evidence_23", "warning")])
        self.assertIn("table_row.row1: Created '20 Jun 14:18 UTC'", disputes[0]["reason"])
        cap = {m["file_id"]: m["captured_at"] for m in self.p["manifest"]}
        img = [o for o in self.p["obs"] if o.file_format == "image"]
        flagged = [o for o in img if o.attrs.get("_ocr_capture_disputed")]
        self.assertEqual({o.file_id for o in flagged}, {"evidence_23"})
        self.assertEqual(len(flagged), sum(o.file_id == "evidence_23" for o in img))  # every unit kept and flagged
        self.assertTrue(all(o.file_captured_at == cap["evidence_23"] for o in flagged))  # capture time not moved


if __name__ == "__main__":
    unittest.main()
