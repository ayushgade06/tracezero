"""M7 self-tests: request status and steps_json assembly (audit/final.py), CLI output, regressions.

Run: python -m unittest discover -s code/tests -v
Synthetic tests build real M5 Evaluations with the test_m5 helpers; every produced row is also
checked by the independent strict validator. Corpus tests use one full pipeline run.
"""
import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(CODE / "tests"))
sys.path.append(str(CODE / "evaluation"))  # after CODE, so "main" is code/main.py

from audit import final, intent, pipeline, policy  # noqa: E402
import main  # noqa: E402
import test_m5 as m5  # noqa: E402
import validate_output  # noqa: E402

from refdata import DATASET, requires_reference  # noqa: E402
AT = m5.aspect


def build(*assessments, c=None):
    ev = [policy.evaluate(a, c or m5.ctx()) for a in assessments]
    return ev, {e.finding.finding_id: policy.plan(e.finding) for e in ev if e.finding}


def assemble(*assessments, c=None, label=intent.DELETION, **kw):
    ev, rem = build(*assessments, c=c)
    return final.assemble("case_1", label, ev, rem, **kw)


def strict_errors(status, steps):
    cited = {s["evidence_id"]: None for s in steps if s["evidence_id"]}
    return validate_output.check_steps(steps, status, None, cited, strict=True)


def absent(eid="evidence_2"):
    return AT("ABSENT", "OBSERVED", [m5.ref(eid, existence="ABSENT")])


RETAINED = m5.ctx({"evidence_3:records[0]": {"retention_until": "2033-12-31T23:59:59Z"}})


class StatusTests(unittest.TestCase):
    def test_non_deletion_is_not_applicable(self):
        self.assertEqual(assemble(m5.make("account_1", existence=m5.present("evidence_1")), label=intent.NON_DELETION)[:2],
                         ("NOT_APPLICABLE", []))

    def test_complete_cites_proof_including_retained_audit_record(self):
        status, steps, trace = assemble(m5.make("account_1", existence=absent()),
                                        m5.make("audit_record_1", existence=m5.present("evidence_3")), c=RETAINED)
        self.assertEqual((status, trace["basis"]), ("COMPLETE", "COMPLIANCE_PROVEN"))
        self.assertEqual([(s["operation"], s["resource_id"], s["evidence_id"]) for s in steps],
                         [("VERIFY_RECORD_ABSENT", "account_1", "evidence_2"), ("VERIFY_RECORD_PRESENT", "audit_record_1", "evidence_3")])
        self.assertEqual(strict_errors(status, steps), [])

    def test_complete_requires_account_proof(self):  # D2
        status, steps, trace = assemble(m5.make("audit_record_1", existence=m5.present("evidence_3")), c=RETAINED)
        self.assertEqual((status, trace["basis"]), ("INCOMPLETE", "NO_ACCOUNT_PROOF"))
        self.assertEqual([(s["phase"], s["operation"], s["evidence_id"]) for s in steps], [("VERIFICATION", "VERIFY_RECORD_PRESENT", "")])
        self.assertEqual(strict_errors(status, steps), [])

    def test_retention_period_rules(self):  # D3 revised: no supplied period -> retained; an expired one blocks
        status, steps, trace = assemble(m5.make("account_1", existence=absent()),
                                        m5.make("audit_record_1", existence=m5.present("evidence_5")), c=RETAINED)
        self.assertEqual((status, trace["blockers"]), ("COMPLETE", []))
        self.assertEqual({(s["operation"], s["evidence_id"]) for s in steps},
                         {("VERIFY_RECORD_ABSENT", "evidence_2"), ("VERIFY_RECORD_PRESENT", "evidence_5")})
        expired = m5.ctx({"evidence_3:records[0]": {"retention_until": "2026-01-01T00:00:00Z"}})
        status, steps, trace = assemble(m5.make("account_1", existence=absent()),
                                        m5.make("audit_record_1", existence=m5.present("evidence_3")), c=expired)
        self.assertEqual((status, trace["blockers"]), ("INCOMPLETE", ["audit_record_1:RETENTION_EXPIRED_NO_RULE"]))
        self.assertEqual({(s["operation"], s["evidence_id"]) for s in steps}, {("VERIFY_RECORD_ABSENT", ""), ("VERIFY_RECORD_PRESENT", "")})
        self.assertEqual(strict_errors(status, steps), [])

    def test_legal_hold_is_preserved_never_deleted(self):  # rule_13: a present hold with no supplied end is active
        c = m5.ctx()
        c.prefix_category["legal_hold"] = "LEGAL_HOLD_RECORD"
        status, steps, trace = assemble(m5.make("account_1", existence=absent()), m5.make("legal_hold_1", existence=m5.present("evidence_6")), c=c)
        self.assertEqual((status, trace["blockers"]), ("COMPLETE", []))
        self.assertIn(("legal_hold_1", "VERIFY_RECORD_PRESENT", "evidence_6"), [(s["resource_id"], s["operation"], s["evidence_id"]) for s in steps])
        gone = m5.make("legal_hold_1", existence=absent("evidence_6"))  # a deleted hold is a preservation failure
        status, steps, trace = assemble(m5.make("account_1", existence=absent()), gone, c=c)
        self.assertEqual((status, trace["blockers"]), ("INCOMPLETE", ["legal_hold_1:RETAINED_RECORD_MISSING"]))
        self.assertIn(("legal_hold_1", "VERIFY_RECORD_PRESENT", ""), [(s["resource_id"], s["operation"], s["evidence_id"]) for s in steps])
        self.assertEqual(strict_errors(status, steps), [])
        self.assertEqual(assemble(m5.make("legal_hold_1", existence=m5.present("evidence_6")), c=c, label=intent.NON_DELETION)[:2],
                         ("NOT_APPLICABLE", []))  # a hold does not turn another kind of request into a deletion audit

    def test_historical_or_unknown_state_never_complete(self):
        for reason in ("HISTORICAL_READINGS_ONLY", "POST_AUDIT_EVIDENCE_ONLY", "ACTIVITY_ONLY", "WEAK_ABSENCE_ONLY"):
            with self.subTest(reason):
                status, steps, trace = assemble(m5.make("account_1", existence=AT(reasons=[reason])),
                                                m5.make("search_1", ["SEARCH_INDEX_ENTRY"], existence=absent()))
                self.assertEqual((status, trace["blockers"]), ("INCOMPLETE", [f"account_1:STATE_UNKNOWN:{reason}"]))
                # the account still gets its required after-the-fix check, uncited; nothing claims it happened
                self.assertIn(("VERIFICATION", "account_1", "VERIFY_RECORD_ABSENT", ""),
                              [(s["phase"], s["resource_id"], s["operation"], s["evidence_id"]) for s in steps])
                self.assertFalse([s for s in steps if s["phase"] != "VERIFICATION"])
                self.assertEqual(strict_errors(status, steps), [])

    def test_category_flag_blocks_complete(self):  # D6
        support = m5.make("support_1", ["SUPPORT_MESSAGE"], existence=m5.present("evidence_4"),
                          exposure=AT("REDACTED", "OBSERVED", [m5.ref("evidence_4", exposure="REDACTED")]))
        support.categories_admissible = []  # only a post-audit capture names the category
        status, _, trace = assemble(m5.make("account_1", existence=absent()), support)
        self.assertEqual((status, trace["blockers"]), ("INCOMPLETE", ["support_1:CATEGORY_POST_AUDIT_ONLY"]))

    def test_worker_operation_category_never_proves_complete(self):
        c = m5.ctx()
        c.worker_ops = policy.PolicyContext([], [], events=[m5.worker_event("support_1", "REDACT_RECORD")]).worker_ops
        redacted = m5.make("support_1", existence=m5.present("evidence_4"),
                           exposure=AT("REDACTED", "OBSERVED", [m5.ref("evidence_4", exposure="REDACTED")]))
        status, steps, trace = assemble(m5.make("account_1", existence=absent()), redacted, c=c)
        self.assertEqual((status, trace["blockers"]), ("INCOMPLETE", ["support_1:CATEGORY_FROM_WORKER_OPERATION"]))
        self.assertIn(("support_1", "VERIFY_RECORD_REDACTED", ""), [(s["resource_id"], s["operation"], s["evidence_id"]) for s in steps])
        self.assertEqual(strict_errors(status, steps), [])

    def test_unattributable_neither_blocks_nor_appears(self):
        status, steps, _ = assemble(m5.make("account_1", existence=absent()),
                                    m5.make("search_9", ["SEARCH_INDEX_ENTRY"], attribution="HISTORICAL_LINK", existence=m5.present("evidence_1")))
        self.assertEqual((status, [s["resource_id"] for s in steps]), ("COMPLETE", ["account_1"]))

    def test_ownership_conflict_blocks_and_is_not_attributed(self):
        rival = m5.make("search_9", ["SEARCH_INDEX_ENTRY"], existence=m5.present("evidence_1"))
        rival.association = dict(rival.association, conflicting_owners=["user_2"])
        status, steps, trace = assemble(m5.make("account_1", existence=absent()), rival)
        self.assertEqual((status, trace["blockers"]), ("INCOMPLETE", ["search_9:OWNERSHIP_CONFLICT"]))
        self.assertNotIn("search_9", [s["resource_id"] for s in steps])  # no unsupported finding or check

    def test_unknown_category_gives_no_check_and_may_leave_an_empty_row(self):
        status, steps, trace = assemble(m5.make("object_1", existence=m5.present("evidence_1")))
        self.assertEqual((status, steps, trace["basis"]), ("INCOMPLETE", [], "BLOCKED"))


class FindingTests(unittest.TestCase):
    def test_findings_remediation_and_checks(self):
        status, steps, trace = assemble(
            m5.make("account_1", existence=m5.present("evidence_1")),
            m5.make("data_flow_1", flow=AT("ACTIVE", "OBSERVED", [m5.ref("evidence_6", flow="ACTIVE")])),
            m5.make("backup_1", restore=AT("ALLOWED", "OBSERVED", [m5.ref("evidence_7", restore="ALLOWED")])),
            m5.make("search_1", ["SEARCH_INDEX_ENTRY"], existence=absent()),
            m5.make("audit_record_1", existence=m5.present("evidence_5")),  # retained, no period supplied: checked PRESENT
            m5.make("object_1", existence=m5.present("evidence_8")))  # unknown category: no stated treatment
        self.assertEqual((status, trace["basis"]), ("INCOMPLETE", "FINDINGS"))
        self.assertEqual([(s["step_no"], s["phase"], s["operation"], s["resource_id"], s["evidence_id"]) for s in steps], [
            (1, "FINDING", "REPORT_REMAINING_RECORD", "account_1", "evidence_1"),
            (2, "FINDING", "REPORT_ACTIVE_DATA_FLOW", "data_flow_1", "evidence_6"),
            (3, "FINDING", "REPORT_RESTORE_RISK", "backup_1", "evidence_7"),
            (4, "REMEDIATION", "DISABLE_DATA_FLOW", "data_flow_1", ""),  # flows and restores before records
            (5, "REMEDIATION", "BLOCK_RESTORE", "backup_1", ""),
            (6, "REMEDIATION", "DELETE_RECORD", "account_1", ""),
            (7, "VERIFICATION", "VERIFY_RECORD_ABSENT", "account_1", ""),
            (8, "VERIFICATION", "VERIFY_RECORD_ABSENT", "search_1", ""),  # already compliant: still a check
            (9, "VERIFICATION", "VERIFY_RECORD_PRESENT", "audit_record_1", ""),
            (10, "VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "data_flow_1", ""),
            (11, "VERIFICATION", "VERIFY_RESTORE_BLOCKED", "backup_1", "")])
        self.assertEqual(strict_errors(status, steps), [])

    def test_missing_system_fails_loudly_or_uses_the_single_m2_link_system(self):
        a = m5.make("search_1", ["SEARCH_INDEX_ENTRY"], existence=m5.present("evidence_1"))
        a.system = None
        with self.assertRaises(final.AssemblyError):
            assemble(a)
        ok = {"search_1": [("2026-09-05T00:00:00Z", "search_service")]}
        status, steps, trace = assemble(a, audit_at=m5.AUDIT, fallback_systems=ok)
        self.assertEqual(({s["system_id"] for s in steps}, trace["system_from_m2_link"]), ({"search_service"}, ["search_1:search_service"]))
        for bad in ({"search_1": [("2026-09-05T00:00:00Z", "a_service"), ("2026-09-06T00:00:00Z", "b_service")]},
                    {"search_1": [("2026-09-11T00:00:00Z", "search_service")]}):  # two systems; only a post-audit link
            with self.subTest(bad), self.assertRaises(final.AssemblyError):
                assemble(a, audit_at=m5.AUDIT, fallback_systems=bad)


def _evaluation_main():
    import importlib.util  # code/evaluation/main.py: loaded by path ("main" is code/main.py here)
    spec = importlib.util.spec_from_file_location("evaluation_main", CODE / "evaluation" / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class EvaluationRunnerTests(unittest.TestCase):
    def step(self, n, phase, op, state, rid, ev):
        return {"step_no": n, "phase": phase, "operation": op, "state": state, "system_id": "s", "resource_id": rid, "evidence_id": ev}

    def test_scorer_on_hand_made_predictions(self):
        ev = _evaluation_main()
        gold = {"case_1": ("COMPLETE", [self.step(1, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT", "account_1", "evidence_1")]),
                "case_2": ("INCOMPLETE", [self.step(1, "FINDING", "REPORT_REMAINING_RECORD", "PRESENT", "search_1", "evidence_2"),
                                          self.step(2, "REMEDIATION", "DELETE_RECORD", "ABSENT", "search_1", ""),
                                          self.step(3, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT", "search_1", "")])}
        same = ev.score(gold, gold)
        self.assertEqual((same["status_accuracy"], same["steps"]["f1"], same["exact_sequence_accuracy"], same["mean_normalized_edit_distance"]),
                         (1.0, 1.0, 1.0, 0.0))
        pred = {"case_1": ("INCOMPLETE", [self.step(1, "VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT", "account_1", "")]),
                "case_2": ("INCOMPLETE", [dict(s, evidence_id="evidence_9") if s["phase"] == "FINDING" else s for s in gold["case_2"][1]])}
        m = ev.score(pred, gold)
        self.assertEqual(m["status_accuracy"], 0.5)
        self.assertEqual((m["steps"]["tp"], m["steps"]["fp"], m["steps"]["fn"]), (2, 2, 2))  # two evidence_id differences
        self.assertEqual(m["steps_without_evidence_id"]["f1"], 1.0)
        self.assertEqual(m["field_accuracy"]["evidence_id"], 0.5)
        self.assertEqual((m["exact_sequence_accuracy"], m["mean_normalized_edit_distance"]), (0.0, round((1 / 1 + 1 / 3) / 2, 4)))
        self.assertEqual(ev._levenshtein("kitten", "sitting"), 3)

    def test_custom_cases_pass(self):
        import custom_cases
        results = custom_cases.run_all()
        self.assertGreaterEqual(len(results), 6)
        self.assertEqual([r["name"] for r in results if not r["passed"]], [])


@requires_reference
class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = pipeline.run(DATASET)
        cls.requests = pipeline.read_requests(DATASET / "deletion_requests.csv")
        cls.rows = {c: (s, steps, t) for c, s, steps, t in main.audit_cases(cls.requests, cls.result)}
        cls.E = {(e.case_id, e.resource_id): e for e in cls.result["E"]}
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "output.csv"
        from audit.output import write_output
        write_output([(r["case_id"], *cls.rows[r["case_id"]][:2]) for r in cls.requests], cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def steps(self, case):
        return [(s["phase"], s["operation"], s["system_id"], s["resource_id"], s["evidence_id"]) for s in self.rows[case][1]]

    def test_serialized_output_is_strictly_valid(self):
        errors, warnings = validate_output.validate(self.out, DATASET / "deletion_requests.csv", strict=True)
        self.assertEqual(errors, [])
        self.assertEqual([c for c, (s, steps, _) in self.rows.items() if s != "NOT_APPLICABLE" and not steps], ["case_61"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("case_61", warnings[0])  # the single evidence-gap row (no attributed resource of known category)
        # 453/55 at M7; D3 revised moved 17 rows to COMPLETE, then R4e 20 and the active legal hold 1 (case_390)
        self.assertEqual(Counter(s for s, _, _ in self.rows.values()), Counter(INCOMPLETE=415, COMPLETE=93, NOT_APPLICABLE=10))

    def test_tracked_cases(self):
        self.assertEqual(self.rows["case_33"][0], "INCOMPLETE")
        self.assertIn(("VERIFICATION", "VERIFY_RECORD_ABSENT", "export_service", "export_1260", ""), self.steps("case_33"))
        self.assertFalse([s for s in self.steps("case_33") if s[0] != "VERIFICATION"])
        self.assertEqual([s for s in self.steps("case_52") if s[3] == "export_1260"], [
            ("FINDING", "REPORT_REMAINING_RECORD", "export_service", "export_1260", "evidence_23"),
            ("REMEDIATION", "DELETE_RECORD", "export_service", "export_1260", ""),
            ("VERIFICATION", "VERIFY_RECORD_ABSENT", "export_service", "export_1260", "")])
        self.assertIn("CAPTURE_TIME_DISPUTED:evidence_23", self.E[("case_52", "export_1260")].notes)
        self.assertEqual([s for s in self.steps("case_53") if s[3] == "support_1034"], [
            ("FINDING", "REPORT_EXPOSED_PII", "support_platform", "support_1034", "evidence_28"),
            ("REMEDIATION", "REDACT_RECORD", "support_platform", "support_1034", ""),
            ("VERIFICATION", "VERIFY_RECORD_REDACTED", "support_platform", "support_1034", "")])
        self.assertIn("CATEGORY_POST_AUDIT_ONLY", self.E[("case_53", "support_1034")].notes)
        self.assertEqual([s for s in self.steps("case_47") if s[3] == "support_5742"], [  # category from the worker operation
            ("FINDING", "REPORT_REMAINING_RECORD", "support_platform", "support_5742", "evidence_94"),
            ("REMEDIATION", "DELETE_RECORD", "support_platform", "support_5742", ""),
            ("VERIFICATION", "VERIFY_RECORD_ABSENT", "support_platform", "support_5742", "")])
        self.assertIn("CATEGORY_FROM_WORKER_OPERATION", self.E[("case_47", "support_5742")].notes)
        status, _, trace = self.rows["case_63"]
        self.assertEqual((status, self.steps("case_63")), ("INCOMPLETE", [
            ("VERIFICATION", "VERIFY_RECORD_ABSENT", "marketing_platform", "marketing_001", "")]))
        self.assertEqual(trace["system_from_m2_link"], ["marketing_001:marketing_platform"])

    def test_worker_operation_matches_every_evidence_categorised_support_record(self):  # the [DATA] claim behind source (6)
        ops = policy.PolicyContext([], [], events=pipeline.matching.load_events(DATASET)).worker_ops
        checked = {(e.resource_id, e.category) for e in self.result["E"] if e.resource_id in ops
                   and e.category_basis in ("explicit_record_type", "collection_name")}
        self.assertEqual(len(checked), 111)
        self.assertEqual([x for x in checked if ops[x[0]] != {x[1]}], [])

    def test_status_rules_hold_on_every_row(self):
        by_case = {}
        for e in self.result["E"]:
            by_case.setdefault(e.case_id, []).append(e)
        for r in self.requests:
            c, (status, steps, trace) = r["case_id"], self.rows[r["case_id"]]
            ev = by_case.get(c, [])
            with self.subTest(c):
                if intent.classify(r["request_text"]) == intent.NON_DELETION:
                    self.assertEqual((status, steps), ("NOT_APPLICABLE", []))
                    continue
                self.assertNotEqual(status, "NOT_APPLICABLE")
                if status == "COMPLETE":  # no finding, no blocker, proof of the account, all cited
                    self.assertFalse([e for e in ev if e.finding or e.outcome == "INCONCLUSIVE" or e.reason == "OWNERSHIP_CONFLICT"])
                    self.assertTrue(any(e.outcome == "COMPLIANT" and e.category in final.ACCOUNT_CATEGORIES for e in ev))
                    self.assertTrue(all(s["evidence_id"] for s in steps))
                for s in steps:  # findings cite M5's citation; nothing else in INCOMPLETE cites
                    if status == "INCOMPLETE":
                        want = self.E[(c, s["resource_id"])].finding.citation["evidence_id"] if s["phase"] == "FINDING" else ""
                        self.assertEqual(s["evidence_id"], want)

    def test_deterministic_across_runs_and_hash_seeds(self):
        digests = set()
        with tempfile.TemporaryDirectory() as d:
            for seed in ("1", "2"):
                out = Path(d) / f"out_{seed}.csv"
                subprocess.run([sys.executable, str(CODE / "main.py"), "--dataset", str(DATASET), "--out", str(out)], check=True, capture_output=True,
                               env=dict(os.environ, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE="1"))
                digests.add(hashlib.sha256(out.read_bytes()).hexdigest())
        digests.add(hashlib.sha256(self.out.read_bytes()).hexdigest())  # and the in-process run above
        self.assertEqual(len(digests), 1)


if __name__ == "__main__":
    unittest.main()
