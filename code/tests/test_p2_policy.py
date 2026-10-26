"""P2.1: the deletion policy is loaded from a CSV file; the default file is exactly the pre-P2 matrix.

Run: python -m unittest discover -s code/tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import main  # noqa: E402
from audit import final, pipeline, policy  # noqa: E402

DEMO = CODE / "tests" / "fixtures" / "demo"

# The hard-coded policy.POLICY before P2, copied verbatim (order included). Do not edit to make a test pass.
CANONICAL = [
    ("USER_PROFILE", ("rule_01", "DELETE", "RULES", None)),
    ("SECONDARY_IDENTIFIER", ("rule_02", "DELETE", "RULES", None)),
    ("SEARCH_INDEX_ENTRY", ("rule_03", "DELETE", "RULES", None)),
    ("CACHE_ENTRY", ("rule_04", "DELETE", "RULES", None)),
    ("SUPPORT_MESSAGE", ("rule_05", "REDACT", "RULES", "CONDITION_ASSUMED_SHARED_CONTEXT")),
    ("SUPPORT_ATTACHMENT", ("rule_06", "DELETE", "RULES", "CONDITION_ASSUMED_SOLE_SUBJECT")),
    ("ANALYTICS_PROFILE", ("rule_07", "ANONYMIZE", "RULES", None)),
    ("PRODUCT_TELEMETRY", ("rule_08", "ANONYMIZE", "RULES", None)),
    ("USER_EXPORT", ("rule_09", "DELETE", "RULES", None)),
    ("MARKETING_PROFILE", ("rule_10", "DELETE", "RULES", None)),
    ("EMAIL_DELIVERY_LOG", ("rule_11", "REDACT", "RULES", None)),
    ("SECURITY_AUDIT_LOG", ("rule_12", "RETAIN", "RULES", None)),
    ("LEGAL_HOLD_RECORD", ("rule_13", "RETAIN", "RULES", None)),
    ("BACKUP_ARCHIVE", ("rule_14", "RETAIN", "RULES", "CONDITION_ASSUMED_RESTORE_BLOCKABLE")),
    ("ACCOUNT_TOMBSTONE", ("rule_15", "REDACT", "RULES", None)),
    ("DATA_FLOW", ("spec_6.5_no_recreating_sync", "FLOW", "SPEC", None)),
]
DEFAULT_TEXT = policy.DEFAULT_POLICY_FILE.read_text(encoding="utf-8")


class PolicyFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, text, name="rules.csv"):
        path = Path(self.tmp.name) / name
        path.write_text(text, encoding="utf-8")
        return path

    def bad(self, text, *fragments):
        path = self.write(text)
        with self.assertRaises(policy.PolicyError) as cm:
            policy.load_policy(path)
        for f in (str(path),) + fragments:
            self.assertIn(f, str(cm.exception))
        return str(cm.exception)

    def test_default_policy_is_the_pre_p2_matrix(self):
        self.assertEqual(list(policy.POLICY.items()), CANONICAL)
        self.assertEqual(list(policy.load_policy(policy.DEFAULT_POLICY_FILE).items()), CANONICAL)
        self.assertIs(policy.PolicyContext([], []).policy, policy.POLICY)

    def test_required_categories_are_those_the_engine_assigns(self):
        self.assertEqual(policy.REQUIRED_CATEGORIES, (
            "ACCOUNT_TOMBSTONE", "ANALYTICS_PROFILE", "BACKUP_ARCHIVE", "DATA_FLOW", "LEGAL_HOLD_RECORD", "MARKETING_PROFILE",
            "SEARCH_INDEX_ENTRY", "SECURITY_AUDIT_LOG", "SUPPORT_ATTACHMENT", "SUPPORT_MESSAGE", "USER_EXPORT", "USER_PROFILE"))

    def test_valid_custom_policy(self):
        text = DEFAULT_TEXT.replace("rule_03,SEARCH_INDEX_ENTRY,Always,DELETE,RULES,", "org_7,SEARCH_INDEX_ENTRY,Always,ANONYMIZE,ORG,NOTE_X")
        m = policy.load_policy(self.write(text))
        self.assertEqual(m["SEARCH_INDEX_ENTRY"], ("org_7", "ANONYMIZE", "ORG", "NOTE_X"))
        self.assertEqual(list(m), [c for c, _ in CANONICAL])

    def test_optional_columns_default_and_extra_categories_are_allowed(self):
        rows = ["rule_id,data_category,required_action"] + [f"{r[0]},{c},{r[1]}" for c, r in CANONICAL] + ["org_1,CHAT_LOG,DELETE"]
        m = policy.load_policy(self.write("\n".join(rows) + "\n"))
        self.assertEqual(m["DATA_FLOW"], ("spec_6.5_no_recreating_sync", "FLOW", "RULES", None))
        self.assertEqual(m["CHAT_LOG"], ("org_1", "DELETE", "RULES", None))

    def test_dataset_style_rules_file_without_data_flow_is_rejected(self):
        rows = ["rule_id,data_category,condition,required_action"] + [f"{r[0]},{c},Always,{r[1]}" for c, r in CANONICAL[:-1]]
        self.bad("\n".join(rows) + "\n", "no rule for required data_category ['DATA_FLOW']")

    def test_changed_policy_reaches_findings_and_remediations(self):
        text = (DEFAULT_TEXT.replace("rule_03,SEARCH_INDEX_ENTRY", "org_search,SEARCH_INDEX_ENTRY")
                .replace("rule_14,BACKUP_ARCHIVE,Restore access can be blocked,RETAIN", "rule_14,BACKUP_ARCHIVE,Always,DELETE"))
        default, custom = pipeline.run(DEMO), pipeline.run(DEMO, policy_matrix=policy.load_policy(self.write(text)))
        f = lambda r: sorted((x.resource_id, x.rule_id, x.finding_type) for x in r["F"])  # noqa: E731
        self.assertEqual(f(default), [("backup_9101", "rule_14", "REPORT_RESTORE_RISK"),
                                      ("data_flow_9101", "spec_6.5_no_recreating_sync", "REPORT_ACTIVE_DATA_FLOW"),
                                      ("search_9101", "rule_03", "REPORT_REMAINING_RECORD")])
        self.assertEqual(f(custom), [("backup_9101", "rule_14", "REPORT_REMAINING_RECORD"),
                                     ("data_flow_9101", "spec_6.5_no_recreating_sync", "REPORT_ACTIVE_DATA_FLOW"),
                                     ("search_9101", "org_search", "REPORT_REMAINING_RECORD")])
        self.assertIn(("backup_9101", "DELETE_RECORD"), {(x.resource_id, x.operation) for x in custom["R"]})

    def test_required_state_follows_the_action(self):
        self.assertEqual(final.required_state("BACKUP_ARCHIVE", "RETAIN"), "BLOCKED")
        self.assertEqual(final.required_state("BACKUP_ARCHIVE", "DELETE"), "ABSENT")

    def test_missing_required_columns(self):
        self.bad("rule_id,data_category\nrule_01,USER_PROFILE\n", "missing required column(s) ['required_action']")

    def test_unknown_column(self):
        self.bad(DEFAULT_TEXT.replace("assumed_condition", "assumed_conditon"), "unknown column(s) ['assumed_conditon']")

    def test_invalid_action(self):
        msg = self.bad(DEFAULT_TEXT.replace("rule_04,CACHE_ENTRY,Always,DELETE", "rule_04,CACHE_ENTRY,Always,PURGE"),
                       ":5:", "required_action 'PURGE'")
        self.assertIn("'DELETE'", msg)

    def test_empty_fields(self):
        self.bad(DEFAULT_TEXT.replace("rule_04,CACHE_ENTRY,", ",,"), ":5:", "empty rule_id", "empty data_category")

    def test_duplicate_rule_id(self):
        self.bad(DEFAULT_TEXT.replace("rule_04,CACHE_ENTRY", "rule_03,CACHE_ENTRY"), ":5:", "duplicate rule_id 'rule_03' (first on line 4)")

    def test_duplicate_category(self):
        self.bad(DEFAULT_TEXT + "rule_99,USER_PROFILE,Always,DELETE,RULES,\n", ":18:", "duplicate data_category 'USER_PROFILE'")

    def test_missing_required_rule(self):
        self.bad(DEFAULT_TEXT.replace("rule_01,USER_PROFILE,Always,DELETE,RULES,\n", ""), "no rule for required data_category ['USER_PROFILE']")

    def test_row_with_extra_fields(self):
        self.bad(DEFAULT_TEXT.replace("rule_02,SECONDARY_IDENTIFIER,Always,DELETE,RULES,", "rule_02,SECONDARY_IDENTIFIER,Always,DELETE,RULES,,x"),
                 ":3:", "more fields than the header")

    def test_every_problem_is_reported(self):
        msg = self.bad(DEFAULT_TEXT.replace("rule_04,CACHE_ENTRY,Always,DELETE", "rule_04,CACHE_ENTRY,Always,PURGE")
                       .replace("rule_09,USER_EXPORT,Always,DELETE", "rule_09,USER_EXPORT,Always,"))
        self.assertIn(":5:", msg)
        self.assertIn(":10:", msg)

    def test_missing_file_and_not_utf8(self):
        with self.assertRaises(policy.PolicyError) as cm:
            policy.load_policy(Path(self.tmp.name) / "nope.csv")
        self.assertIn("nope.csv: cannot read policy file", str(cm.exception))
        path = Path(self.tmp.name) / "latin1.csv"
        path.write_bytes(DEFAULT_TEXT.replace("Always", "Toujours \xe9").encode("latin-1"))
        with self.assertRaises(policy.PolicyError) as cm:
            policy.load_policy(path)
        self.assertIn("not UTF-8", str(cm.exception))


class PolicyCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_invalid_policy_fails_before_the_audit_and_never_falls_back(self):
        bad, out = self.tmp / "bad.csv", self.tmp / "out.csv"
        bad.write_text("rule_id,data_category\n", encoding="utf-8")
        for path in (bad, self.tmp / "missing.csv"):
            with self.assertRaises(SystemExit) as cm:
                main.main(["--dataset", str(DEMO), "--out", str(out), "--policy", str(path)])
            self.assertIn("invalid policy", str(cm.exception.code))
            self.assertIn(str(path), str(cm.exception.code))
            self.assertFalse(out.exists())

    def test_default_policy_file_given_explicitly_gives_the_default_output(self):
        a, b = self.tmp / "a.csv", self.tmp / "b.csv"
        main.main(["--dataset", str(DEMO), "--out", str(a)])
        main.main(["--dataset", str(DEMO), "--out", str(b), "--policy", str(policy.DEFAULT_POLICY_FILE)])
        self.assertEqual(a.read_bytes(), b.read_bytes())


if __name__ == "__main__":
    unittest.main()
