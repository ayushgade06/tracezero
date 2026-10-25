"""M0 self-tests: intent classification, writer, independent validator, determinism.

Run: python -m unittest discover -s code/tests -v
"""
import csv
import json
import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
sys.path.append(str(CODE / "evaluation"))  # after CODE, so "main" is code/main.py

import main  # noqa: E402
import validate_output  # noqa: E402
from audit import intent  # noqa: E402

DATASET = CODE.parent / "dataset"
REQUESTS = DATASET / "deletion_requests.csv"
SAMPLE_REQUESTS = DATASET / "sample_deletion_requests.csv"
SAMPLE_OUTPUT = DATASET / "sample_output.csv"


def read_rows(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.reader(f))


def write_rows(rows, path):
    with open(path, "w", encoding="utf-8", newline="") as f:
        csv.writer(f, lineterminator="\n").writerows(rows)


class IntentTests(unittest.TestCase):
    def test_dataset_intents(self):
        with open(REQUESTS, encoding="utf-8") as f:
            labels = {r["case_id"]: intent.classify(r["request_text"]) for r in csv.DictReader(f)}
        non_deletion = {c for c, l in labels.items() if l == intent.NON_DELETION}
        self.assertEqual(non_deletion, {f"case_{n}" for n in range(533, 543)})
        self.assertNotIn(intent.AMBIGUOUS, labels.values())

    def test_no_sample_becomes_not_applicable(self):
        # All 32 samples are deletion cases. Follow-up wording (case_26 "confirm the profile is gone",
        # case_32 "keep only the reference ... remove the rest") is AMBIGUOUS and must still be audited.
        with open(SAMPLE_REQUESTS, encoding="utf-8") as f:
            reqs = list(csv.DictReader(f))
        # M7: the M0 placeholder status is gone; the property tested is that no sample is NOT_APPLICABLE
        self.assertNotIn(intent.NON_DELETION, {intent.classify(r["request_text"]) for r in reqs})
        ambiguous = {r["case_id"] for r in reqs if intent.classify(r["request_text"]) == intent.AMBIGUOUS}
        self.assertEqual(ambiguous, {"case_26", "case_32"})

    def test_edge_texts(self):
        cases = {
            "I no longer use this account and would like it permanently deleted.": intent.DELETION,
            "Please do not delete my account.": intent.NON_DELETION,
            "Please remove me from marketing lists only.": intent.NON_DELETION,
            "Please update my phone number and leave the account active.": intent.NON_DELETION,
            # Unknown language / mixed signals must never become NOT_APPLICABLE silently.
            "Bitte löschen Sie mein Konto.": intent.AMBIGUOUS,
            "Delete my account, but keep my order history.": intent.AMBIGUOUS,
            "": intent.AMBIGUOUS,
        }
        for text, want in cases.items():
            self.assertEqual(intent.classify(text), want, text)


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "out.csv"
        self.sample = read_rows(SAMPLE_OUTPUT)

    def tearDown(self):
        self.tmp.cleanup()

    def errors(self, rows, strict=True):
        write_rows(rows, self.path)
        return validate_output.validate(self.path, SAMPLE_REQUESTS, strict)[0]

    def test_published_sample_output_passes_strict(self):
        self.assertEqual(self.errors(self.sample), [])

    def mutate_steps(self, row_index, fn):
        rows = [r[:] for r in self.sample]
        steps = json.loads(rows[row_index][2])
        fn(steps)
        rows[row_index][2] = json.dumps(steps)
        return rows

    def test_rejects_corruptions(self):
        r = lambda: [x[:] for x in self.sample]  # noqa: E731
        complete = next(i for i, x in enumerate(self.sample) if x[1] == "COMPLETE")
        incomplete = next(i for i, x in enumerate(self.sample) if x[1] == "INCOMPLETE")
        corruptions = {
            "header": [["case_id", "status", "steps_json"]] + r()[1:],
            "missing row": r()[:-1],
            "duplicate row": r() + [r()[1]],
            "row order": [r()[0], r()[2], r()[1]] + r()[3:],
            "bad status": [x if i != 1 else [x[0], "DONE", x[2]] for i, x in enumerate(r())],
            "invalid json": [x if i != 1 else [x[0], x[1], x[2][:-1]] for i, x in enumerate(r())],
            "not an array": [x if i != 1 else [x[0], x[1], "{}"] for i, x in enumerate(r())],
            "extra column": [x if i != 1 else x + ["?"] for i, x in enumerate(r())],
            "NA with steps": [x if i != complete else [x[0], "NOT_APPLICABLE", x[2]] for i, x in enumerate(r())],
            "missing key": self.mutate_steps(1, lambda s: s[0].pop("state")),
            "extra key": self.mutate_steps(1, lambda s: s[0].update(note="x")),
            "step_no gap": self.mutate_steps(incomplete, lambda s: s[-1].update(step_no=99)),
            "step_no string": self.mutate_steps(1, lambda s: s[0].update(step_no="1")),
            "step_no bool": self.mutate_steps(1, lambda s: s[0].update(step_no=True)),
            "unknown operation": self.mutate_steps(1, lambda s: s[0].update(operation="PURGE")),
            "wrong state": self.mutate_steps(1, lambda s: s[0].update(state="PRESENT")),
            "wrong phase": self.mutate_steps(1, lambda s: s[0].update(phase="FINDING")),
            "reversed order": self.mutate_steps(incomplete, lambda s: [s.reverse(), [x.update(step_no=i) for i, x in enumerate(s, 1)]]),
            "uncited COMPLETE verify": self.mutate_steps(complete, lambda s: s[0].update(evidence_id="")),
            "cited remediation": self.mutate_steps(incomplete, lambda s: [x.update(evidence_id="evidence_01") for x in s if x["phase"] == "REMEDIATION"]),
            "unknown evidence": self.mutate_steps(complete, lambda s: s[0].update(evidence_id="evidence_999")),
            "post-audit evidence": self.mutate_steps(complete, lambda s: s[0].update(evidence_id="evidence_03")),
            "empty COMPLETE (strict)": [x if i != complete else [x[0], x[1], "[]"] for i, x in enumerate(r())],
        }
        for name, rows in corruptions.items():
            with self.subTest(name):
                self.assertNotEqual(self.errors(rows), [], name)

    # ---- M7.1 strict step-relationship rules
    def edit(self, row_index, fn):
        """Apply fn to the step list of one row, then renumber step_no (only the targeted rule may fail)."""
        def go(steps):
            fn(steps)
            for i, x in enumerate(steps, 1):
                x["step_no"] = i
        return self.mutate_steps(row_index, go)

    def row_where(self, pred):
        return next(i for i, x in enumerate(self.sample) if i and pred(x[1], json.loads(x[2])))  # row 0 is the header

    def test_strict_relationship_rules(self):
        multi = self.row_where(lambda st, s: st == "INCOMPLETE" and sum(x["phase"] == "FINDING" for x in s) >= 2)
        steps = json.loads(self.sample[multi][2])
        f0 = next(x for x in steps if x["phase"] == "FINDING")
        res = (f0["system_id"], f0["resource_id"])
        on = lambda phase: lambda x: x["phase"] == phase and (x["system_id"], x["resource_id"]) == res  # noqa: E731
        drop = lambda pred: lambda s: s.__setitem__(slice(None), [x for x in s if not pred(x)])  # noqa: E731
        complete = self.row_where(lambda st, s: st == "COMPLETE" and len(s) > 1)
        acct = lambda x: x["resource_id"].startswith(("account_", "tombstone_"))  # noqa: E731
        corruptions = {
            "finding without remediation": self.edit(multi, drop(on("REMEDIATION"))),
            "remediation without finding": self.edit(multi, drop(on("FINDING"))),
            "remediated resource not verified": self.edit(multi, drop(on("VERIFICATION"))),
            "verification state differs from remediation": self.edit(multi, lambda s: next(x for x in s if on("VERIFICATION")(x)).update(
                operation="VERIFY_RECORD_PRESENT", state="PRESENT")),
            "duplicate phase/resource": self.edit(multi, lambda s: s.append(dict(s[-1]))),
            "INCOMPLETE without findings but with remediation": self.edit(multi, drop(lambda x: x["phase"] == "FINDING")),
            "COMPLETE without account/tombstone check": self.edit(complete, drop(acct)),
            "malformed resource_id": self.edit(complete, lambda s: s[-1].update(resource_id="account-x")),
            "malformed system_id": self.edit(complete, lambda s: s[-1].update(system_id="zz-service")),
        }
        for name, rows in corruptions.items():
            with self.subTest(name):
                self.assertNotEqual(self.errors(rows), [], name)

    def test_empty_incomplete_is_a_reported_evidence_gap(self):
        # Approved M7 decision: INCOMPLETE [] is contract-valid (no statable check) but always reported
        incomplete = self.row_where(lambda st, s: st == "INCOMPLETE")
        rows = [x if i != incomplete else [x[0], x[1], "[]"] for i, x in enumerate(self.sample)]
        write_rows(rows, self.path)
        errors, warnings = validate_output.validate(self.path, SAMPLE_REQUESTS, strict=True)
        self.assertEqual(errors, [])
        self.assertTrue(any(rows[incomplete][0] in w for w in warnings), warnings)

    def test_verification_only_incomplete_is_valid(self):
        # D1: INCOMPLETE without findings carries only uncited after-the-fix checks
        multi = self.row_where(lambda st, s: st == "INCOMPLETE")
        rows = self.edit(multi, lambda s: s.__setitem__(slice(None), [x for x in s if x["phase"] == "VERIFICATION"]))
        self.assertEqual(self.errors(rows), [])


class PipelineTests(unittest.TestCase):
    def test_output_valid_and_deterministic(self):
        # M7: the CLI now writes the full audit; it must stay strictly valid and byte-stable
        with tempfile.TemporaryDirectory() as d:
            hashes = []
            for n in range(2):
                out = Path(d) / f"out{n}.csv"
                with io.StringIO() as buf:
                    stdout, sys.stdout = sys.stdout, buf
                    try:
                        main.main(["--out", str(out)])
                    finally:
                        sys.stdout = stdout
                hashes.append(hashlib.sha256(out.read_bytes()).hexdigest())
            errors, warnings = validate_output.validate(out, REQUESTS, strict=True)
            self.assertEqual(errors, [])
            self.assertEqual(hashes[0], hashes[1])
            rows = read_rows(out)
            self.assertEqual(len(rows), 519)
            na = [r for r in rows[1:] if r[1] == "NOT_APPLICABLE"]
            self.assertEqual(len(na), 10)
            self.assertTrue(all(r[2] == "[]" for r in na))


if __name__ == "__main__":
    unittest.main()
