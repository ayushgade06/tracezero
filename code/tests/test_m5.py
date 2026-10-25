"""M5 self-tests: policy evaluation, findings, remediations, ownership gate, citations.

Run: python -m unittest discover -s code/tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(CODE / "evaluation"))
sys.path.insert(0, str(CODE / "tests"))

from audit import policy, state  # noqa: E402

AUDIT = "2026-09-10T00:00:00Z"


def ref(eid, time="2026-09-05T00:00:00Z", **facts):
    return {"ref": f"{eid}:records[0]", "evidence_id": eid, "kind": "lookup", "copy": "primary", "time": time,
            "locator": {"json_path": "records[0]"}, "fields": ["result"], "facts": facts}


def aspect(value="UNKNOWN", basis="NONE", deciding=(), contradicting=(), reasons=(), per_copy=None):
    return {"value": value, "basis": basis, "per_copy": per_copy or ({"primary": value} if deciding else {}),
            "deciding": list(deciding), "supporting": [], "contradicting": list(contradicting), "reasons": list(reasons)}


def make(rid, categories=(), attribution="EVIDENCE_LINKED", existence=None, exposure=None, flow=None, restore=None,
         reasons=(), excluded=(), case="case_1"):
    att = {"attribution": attribution, "source": "M2", "link_evidence": ["ev_link"],
           "attributed_to_requester": attribution in ("EVIDENCE_LINKED", "ACTIVITY_MATCH_HIGH", "ACTIVITY_MATCH_MEDIUM")}
    if attribution.startswith("ACTIVITY_MATCH"):
        att["m3"] = {"confidence": attribution.rsplit("_", 1)[1], "reason": "x", "activity_id": "activity_1"}
    return state.Assessment(case, "2026-09-01T00:00:00Z", "2026-09-03T12:00:00Z", AUDIT, rid, "some_system", "explicit",
                            list(categories), att,
                            {"existence": existence or aspect(), "exposure": exposure or aspect(),
                             "flow": flow or aspect(), "restore": restore or aspect()},
                            "X", list(excluded), list(reasons), [])


def ctx(attrs=None):
    c = policy.PolicyContext([], [])
    c.prefix_category = {"account": "USER_PROFILE", "audit_record": "SECURITY_AUDIT_LOG", "data_flow": "DATA_FLOW",
                         "backup": "BACKUP_ARCHIVE", "analytics": "ANALYTICS_PROFILE"}
    c.attrs.update(attrs or {})
    return c


def present(eid="ev_1", time="2026-09-05T00:00:00Z"):
    return aspect("PRESENT", "OBSERVED", [ref(eid, time, existence="PRESENT")])


def run_one(a, c=None):
    e = policy.evaluate(a, c or ctx())
    return e, (policy.plan(e.finding) if e.finding else None)


class FindingTests(unittest.TestCase):
    def test_confirmed_remaining_record(self):
        e, r = run_one(make("account_1", existence=present()))
        f = e.finding
        self.assertEqual((e.outcome, f.finding_type, f.state, f.rule_id, f.conclusion), ("VIOLATION", "REPORT_REMAINING_RECORD", "PRESENT", "rule_01", "confirmed"))
        self.assertEqual((f.citation["evidence_id"], f.citation["ref"]), ("ev_1", "ev_1:records[0]"))
        self.assertEqual(f.identifying, [{"evidence_id": "ev_link", "role": "M2_LINK"}])
        self.assertEqual((r.operation, r.target_state, r.finding_id), ("DELETE_RECORD", "ABSENT", f.finding_id))

    def test_explicit_absence_is_compliant(self):
        ab = aspect("ABSENT", "OBSERVED", [ref("ev_2", existence="ABSENT")])
        e, r = run_one(make("account_1", existence=ab))
        self.assertEqual((e.outcome, e.reason, e.finding, r), ("COMPLIANT", "RECORD_ABSENT", None, None))
        self.assertEqual(e.citation["evidence_id"], "ev_2")  # kept for later verification steps

    def test_unknown_and_conflicting_and_stale_are_inconclusive(self):
        cases = {
            "STATE_UNKNOWN:POST_AUDIT_EVIDENCE_ONLY": aspect(reasons=["POST_AUDIT_EVIDENCE_ONLY"]),
            "STATE_UNKNOWN:ACTIVITY_ONLY": aspect(reasons=["ACTIVITY_ONLY"]),
            "STATE_CONFLICTING": aspect("CONFLICTING", "OBSERVED", [ref("ev_1", existence="PRESENT")],
                                        [ref("ev_2", existence="ABSENT")], ["CONFLICTING_READINGS"]),
            "STATE_STALE": aspect("PRESENT", "OBSERVED_STALE", [ref("ev_1", existence="PRESENT")], reasons=["LATER_CLAIM"]),
        }
        for reason, ex in cases.items():
            with self.subTest(reason):
                e, r = run_one(make("account_1", existence=ex))
                self.assertEqual((e.outcome, e.reason, e.finding, r), ("INCONCLUSIVE", reason, None, None))

    def test_exposed_pii_and_redacted_support_message(self):
        exposed = make("support_1", ["SUPPORT_MESSAGE"], existence=present(),
                       exposure=aspect("IDENTIFIABLE", "OBSERVED", [ref("ev_1", exposure="IDENTIFIABLE")]))
        e, r = run_one(exposed)
        self.assertEqual((e.finding.finding_type, r.operation, r.target_state), ("REPORT_EXPOSED_PII", "REDACT_RECORD", "REDACTED"))
        self.assertIn("CONDITION_ASSUMED_SHARED_CONTEXT", r.caveats)
        redacted = make("support_1", ["SUPPORT_MESSAGE"], existence=present(),
                        exposure=aspect("REDACTED", "OBSERVED", [ref("ev_1", exposure="REDACTED")]))
        self.assertEqual(run_one(redacted)[0].outcome, "COMPLIANT")
        unknown = make("support_1", ["SUPPORT_MESSAGE"], existence=present())
        self.assertEqual(run_one(unknown)[0].reason, "EXPOSURE_UNKNOWN:NO_EVIDENCE")

    def test_analytics_anonymize_not_redact(self):
        ident = make("analytics_1", existence=present(), exposure=aspect("IDENTIFIABLE", "OBSERVED", [ref("ev_1", exposure="IDENTIFIABLE")]))
        e, r = run_one(ident)
        self.assertEqual((e.category, e.category_basis, r.operation), ("ANALYTICS_PROFILE", "unanimous_id_prefix", "ANONYMIZE_RECORD"))
        anon = make("analytics_1", existence=present(), exposure=aspect("ANONYMIZED", "OBSERVED", [ref("ev_1", exposure="ANONYMIZED")]))
        self.assertEqual(run_one(anon)[0].reason, "RECORD_ANONYMIZED")
        redacted = make("analytics_1", existence=present(), exposure=aspect("REDACTED", "OBSERVED", [ref("ev_1", exposure="REDACTED")]))
        self.assertEqual(run_one(redacted)[0].reason, "EXPOSURE_REDACTED_NOT_ANONYMIZED")

    def test_tombstone_preserved(self):
        exposed = make("tombstone_1", ["deleted_customer_references"], existence=present(),
                       exposure=aspect("IDENTIFIABLE", "OBSERVED", [ref("ev_1", exposure="IDENTIFIABLE")]))
        e, r = run_one(exposed)
        self.assertEqual((e.category, r.operation), ("ACCOUNT_TOMBSTONE", "REDACT_RECORD"))  # never DELETE
        clean = make("tombstone_1", existence=present(), exposure=aspect("REDACTED", "OBSERVED", [ref("ev_1", exposure="REDACTED")]))
        self.assertEqual((run_one(clean)[0].category_basis, run_one(clean)[0].outcome), ("prefix_name", "COMPLIANT"))
        gone = make("tombstone_1", existence=aspect("ABSENT", "OBSERVED", [ref("ev_1", existence="ABSENT")]))
        self.assertEqual((run_one(gone)[0].outcome, run_one(gone)[0].reason), ("INCONCLUSIVE", "TOMBSTONE_MISSING"))

    def test_retained_security_audit_record(self):
        c = ctx({"ev_1:records[0]": {"retention_until": "2033-12-31T23:59:59Z"}, "ev_9:records[0]": {"retention_until": "2026-01-01T00:00:00Z"}})
        ok = make("audit_record_1", existence=present("ev_1"))
        self.assertEqual(run_one(ok, c)[0].outcome, "COMPLIANT")
        expired = make("audit_record_1", existence=present("ev_9"))
        self.assertEqual(run_one(expired, c)[0].reason, "RETENTION_EXPIRED_NO_RULE")
        missing = make("audit_record_1", existence=aspect("ABSENT", "OBSERVED", [ref("ev_1", existence="ABSENT")]))
        e, r = run_one(missing, c)
        self.assertEqual((e.outcome, e.reason, e.finding, r), ("INCONCLUSIVE", "RETAINED_RECORD_MISSING", None, None))
        no_period = make("audit_record_1", existence=present("ev_5"))  # D3 revised: no supplied period -> retained
        e = run_one(no_period, c)[0]
        self.assertEqual((e.outcome, e.reason, e.citation["evidence_id"]), ("COMPLIANT", "RETAINED_NO_PERIOD_SUPPLIED", "ev_5"))
        c.prefix_category["legal_hold"] = "LEGAL_HOLD_RECORD"  # a present hold with no supplied end is active (approved)
        e = run_one(make("legal_hold_1", existence=present("ev_5")), c)[0]
        self.assertEqual((e.outcome, e.reason, e.citation["evidence_id"]), ("COMPLIANT", "RETAINED_NO_PERIOD_SUPPLIED", "ev_5"))
        c.attrs["ev_9:records[0]"] = {"retained_until": "2026-01-01T00:00:00Z"}  # an ended hold still blocks
        e = run_one(make("legal_hold_1", existence=present("ev_9")), c)[0]
        self.assertEqual((e.outcome, e.reason), ("INCONCLUSIVE", "RETENTION_EXPIRED_NO_RULE"))

    def test_retention_boundaries(self):  # D3 and the active hold apply only to an established, unexpired present record
        c = ctx({"ev_1:records[0]": {"retention_until": "2033-12-31T23:59:59Z"}, "ev_9:records[0]": {"retention_until": "2026-01-01T00:00:00Z"},
                 "ev_7:records[0]": {"retained_until": "2033-12-31T23:59:59Z"}})
        c.prefix_category["legal_hold"] = "LEGAL_HOLD_RECORD"
        both = aspect("PRESENT", "OBSERVED", [ref("ev_1", existence="PRESENT"), ref("ev_9", existence="PRESENT")])
        e = run_one(make("audit_record_1", existence=both), c)[0]  # one end expired, one not: never retained
        self.assertEqual((e.outcome, e.reason), ("INCONCLUSIVE", "RETENTION_CONFLICTING"))
        for rid in ("audit_record_1", "legal_hold_1"):
            with self.subTest(rid):
                late = make(rid, existence=aspect(reasons=["POST_AUDIT_EVIDENCE_ONLY"]))  # seen only after audit_at
                self.assertEqual(run_one(late, c)[0].reason, "STATE_UNKNOWN:POST_AUDIT_EVIDENCE_ONLY")
                gone = make(rid, existence=aspect("ABSENT", "OBSERVED", [ref("ev_2", existence="ABSENT")]))  # deletion is not compliance
                self.assertEqual(run_one(gone, c)[0].reason, "RETAINED_RECORD_MISSING")
        e = run_one(make("legal_hold_1", existence=present("ev_7")), c)[0]  # a supplied end after audit_at: active
        self.assertEqual((e.outcome, e.reason), ("COMPLIANT", "RETAINED_WITHIN_PERIOD"))

    def test_active_connector(self):
        active = make("data_flow_1", flow=aspect("ACTIVE", "OBSERVED", [ref("ev_1", flow="ACTIVE")]))
        e, r = run_one(active)
        self.assertEqual((e.finding.finding_type, e.finding.state, r.operation), ("REPORT_ACTIVE_DATA_FLOW", "ACTIVE", "DISABLE_DATA_FLOW"))
        self.assertEqual(run_one(make("data_flow_1", flow=aspect("DISABLED", "OBSERVED", [ref("ev_1", flow="DISABLED")])))[0].outcome, "COMPLIANT")
        self.assertEqual(run_one(make("data_flow_1", existence=present()))[0].outcome, "INCONCLUSIVE")  # flow unknown

    def test_restore_risk(self):
        risk = make("backup_1", restore=aspect("ALLOWED", "OBSERVED", [ref("ev_1", restore="ALLOWED")]))  # existence unknown
        e, r = run_one(risk)
        self.assertEqual((e.finding.finding_type, r.operation, r.target_state), ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "BLOCKED"))
        blocked = make("backup_1", existence=present(), restore=aspect("BLOCKED", "OBSERVED", [ref("ev_1", restore="BLOCKED")]))
        self.assertEqual(run_one(blocked)[0].outcome, "COMPLIANT")  # retained archive is never deleted

    def test_category_specific_delete_vs_redact(self):
        attach = make("support_2", ["SUPPORT_ATTACHMENT"], existence=present(),
                      exposure=aspect("REDACTED", "OBSERVED", [ref("ev_1", exposure="REDACTED")]))
        self.assertEqual(run_one(attach)[1].operation, "DELETE_RECORD")  # redaction does not satisfy DELETE
        c = ctx()
        c.support_signals["support_3"] = {("SUPPORT_ATTACHMENT", "2026-09-05T00:00:00Z")}  # M6.1: signals carry capture time
        self.assertEqual(run_one(make("support_3", existence=present()), c)[0].category_basis, "support_content_signal")
        unknown = run_one(make("support_4", existence=present()))[0]
        self.assertEqual((unknown.outcome, unknown.reason), ("INCONCLUSIVE", "CATEGORY_UNKNOWN"))
        both = make("support_5", ["SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT"], existence=present())
        self.assertEqual(run_one(both)[0].reason, "CATEGORY_CONFLICTING")
        with self.assertRaises(KeyError):  # a non-permitted pairing cannot be planned
            bad = run_one(make("account_9", existence=present()))[0].finding
            bad.category = "SECURITY_AUDIT_LOG"
            policy.plan(bad)

    def test_ownership_gate(self):
        low = run_one(make("account_1", attribution="ACTIVITY_MATCH_LOW", existence=present()))
        self.assertEqual((low[0].outcome, low[0].reason, low[0].finding, low[1]), ("NOT_ATTRIBUTABLE", "OWNERSHIP_ACTIVITY_MATCH_LOW", None, None))
        self.assertEqual(low[0].potential_violation["would_be"], "REPORT_REMAINING_RECORD")
        alt = run_one(make("account_2", attribution="UNATTRIBUTED_ALTERNATIVE", existence=present()))[0]
        self.assertEqual((alt.outcome, alt.finding), ("NOT_ATTRIBUTABLE", None))
        medium = run_one(make("account_3", attribution="ACTIVITY_MATCH_MEDIUM", existence=present()))[0]
        self.assertEqual((medium.finding.conclusion, medium.finding.identifying[-1]["role"]), ("inferred", "M3_MATCH_MEDIUM"))

    def test_duplicates_and_contradictions(self):
        ex = aspect("PRESENT", "OBSERVED", [ref("ev_1", existence="PRESENT"), ref("ev_2", existence="PRESENT"),
                                            ref("ev_3", existence="PRESENT")], [ref("ev_0", "2026-09-02T00:00:00Z", existence="ABSENT")])
        E, F, R = policy.run([make("account_1", existence=ex)], ctx())
        self.assertEqual((len(F), len(R)), (1, 1))
        self.assertEqual(len(F[0].establishing), 3)
        self.assertEqual([c["evidence_id"] for c in F[0].contradicting], ["ev_0"])
        self.assertTrue(F[0].citation["tie"])  # three equally valid citations
        self.assertEqual(F[0].citation["evidence_id"], "ev_1")
        self.assertIn("CITATION_TIE", R[0].caveats)

    def test_replica_drift_note(self):
        ex = aspect("PRESENT", "OBSERVED", [ref("ev_1", existence="PRESENT")], per_copy={"primary": "ABSENT", "read_replica": "PRESENT"})
        e, r = run_one(make("account_1", existence=ex, reasons=["EXISTENCE_REPLICA_DRIFT"]))
        self.assertIn("REPLICA_DRIFT:primary=ABSENT,read_replica=PRESENT", r.caveats)


def worker_event(rid, event_type, source="deletion_worker"):
    from audit.matching import Event
    return Event("activity_1", "2026-09-04T00:00:00Z", event_type, source, "support_platform", rid, "FAILED", "")


class WorkerOperationCategoryTests(unittest.TestCase):
    def wctx(self, *events):
        c = ctx()
        c.worker_ops = policy.PolicyContext([], [], events=events).worker_ops
        return c

    def test_worker_operation_categorises_an_otherwise_unknown_support_record(self):
        for op, cat in (("DELETE_RECORD", "SUPPORT_ATTACHMENT"), ("REDACT_RECORD", "SUPPORT_MESSAGE")):
            with self.subTest(op):
                c, a = self.wctx(worker_event("support_1", op)), make("support_1")
                a.categories_admissible = []
                self.assertEqual(policy.category(a, c), (cat, "worker_operation"))
                self.assertEqual(policy.category(a, c, admissible=True), (None, "CATEGORY_UNKNOWN"))  # never M6.1 provenance

    def test_finding_is_planned_and_noted(self):
        e, r = run_one(make("support_1", existence=present()), self.wctx(worker_event("support_1", "DELETE_RECORD")))
        self.assertEqual((e.finding.finding_type, r.operation, r.rule_id), ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "rule_06"))
        self.assertIn("CATEGORY_FROM_WORKER_OPERATION", e.notes)
        self.assertNotIn("CATEGORY_POST_AUDIT_ONLY", e.notes)
        self.assertIn("CATEGORY_FROM_WORKER_OPERATION", r.caveats)

    def test_weaker_than_every_evidence_source_and_ignored_when_ambiguous(self):
        both = self.wctx(worker_event("support_1", "DELETE_RECORD"), worker_event("support_1", "REDACT_RECORD"))
        self.assertEqual(policy.category(make("support_1"), both), (None, "CATEGORY_CONFLICTING"))
        attach = self.wctx(worker_event("support_1", "DELETE_RECORD"))
        self.assertEqual(policy.category(make("support_1", ["support_conversations"]), attach), ("SUPPORT_MESSAGE", "collection_name"))
        others = self.wctx(worker_event("support_1", "DELETE_RECORD", source="support_platform"), worker_event("object_1", "DELETE_RECORD"))
        self.assertEqual(policy.category(make("support_1"), others), (None, "CATEGORY_UNKNOWN"))
        self.assertEqual(policy.category(make("object_1"), others), (None, "CATEGORY_UNKNOWN"))


class PipelineTests(unittest.TestCase):
    def test_post_audit_evidence_never_creates_a_finding(self):
        import test_m4 as m4
        only_post = m4.ledger("ev_2", "2026-09-12T00:00:00Z", [m4.lookup("account_1", "FOUND", "2026-09-08T00:00:00Z")])
        r, _ = m4.assess(only_post)
        a = r[("case_1", "account_1")]
        E, F, _ = policy.run([a], policy.PolicyContext(only_post, []))
        self.assertEqual((E[0].outcome, E[0].reason, F), ("INCONCLUSIVE", "STATE_UNKNOWN:POST_AUDIT_EVIDENCE_ONLY", []))


class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import policy_report
        cls.manifest, cls.A, cls.E, cls.F, cls.R = policy_report.run(CODE.parent / "dataset")
        cls.report = policy_report

    def test_invariants_hold(self):
        self.assertEqual(self.report.invariants(self.manifest, self.A, self.E, self.F, self.R), {})

    def test_links_between_findings_and_remediations(self):
        fids = {f.finding_id for f in self.F}
        self.assertEqual({r.finding_id for r in self.R}, fids)
        files = {m["file_id"] for m in self.manifest}
        for f in self.F:
            self.assertIn(f.citation["evidence_id"], files)
            self.assertTrue(all(r["evidence_id"] in files for r in f.establishing))

    def test_gate_and_no_sample_answers(self):
        for e in self.E:
            if e.attribution["attribution"] not in policy.ATTRIBUTABLE:
                self.assertIsNone(e.finding)
        self.assertNotIn("sample_output", (CODE / "audit" / "policy.py").read_text(encoding="utf-8"))

    def test_deterministic_across_processes(self):
        import hashlib
        import os
        import subprocess
        digests = set()
        for seed in ("3", "4"):
            with tempfile.TemporaryDirectory() as d:
                out = Path(d) / "e.json"
                subprocess.run([sys.executable, str(CODE / "evaluation" / "policy_report.py"), "--json", str(out)],
                               env=dict(os.environ, PYTHONHASHSEED=seed, PYTHONIOENCODING="utf-8"), check=True, capture_output=True)
                digests.add(hashlib.sha256(out.read_bytes()).hexdigest())
        self.assertEqual(len(digests), 1)


if __name__ == "__main__":
    unittest.main()
