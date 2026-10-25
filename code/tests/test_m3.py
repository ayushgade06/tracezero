"""M3 self-tests: request-to-activity matching, confidence, abstention, history and reuse.

Run: python -m unittest discover -s code/tests -v
"""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

from audit import discovery, identity, ingest, matching, parsers  # noqa: E402
from audit.matching import Event  # noqa: E402

from refdata import DATASET, requires_reference  # noqa: E402
DELETE = "Please permanently delete the account associated with this email address."


def write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def toy_index():
    """user_1 changed old@x.io -> new@x.io on 2026-02-01; user_2..user_4 are separate people."""
    d = tempfile.mkdtemp()
    write_csv(Path(d) / "user_identities.csv", ["user_id", "name", "email", "phone", "email_hash", "updated_at"], [
        ["user_1", "A", "new@x.io", "+1 1", "a" * 64, "2026-02-01T00:00:00Z"],
        ["user_2", "B", "two@x.io", "+1 2", "b" * 64, "2026-01-01T00:00:00Z"],
        ["user_3", "C", "three@x.io", "+1 3", "c" * 64, "2026-01-01T00:00:00Z"],
        ["user_4", "D", "four@x.io", "+1 4", "d" * 64, "2026-01-01T00:00:00Z"],
    ])
    write_csv(Path(d) / "user_audit_log.csv", ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"],
              [["audit_1", "user_1", "2026-02-01T00:00:00Z", "email", "old@x.io", "new@x.io"]])
    return identity.load_identities(d)


def req(case, email, start, report, status="COMPLETE", text=DELETE, audit="2026-12-31T00:00:00Z"):
    return {"case_id": case, "email": email, "requested_at": start, "reported_status": status, "reported_at": report,
            "audit_at": audit, "request_text": text}


def ev(aid, t, rid, result="SUCCESS", etype="DELETE_RECORD", target="account_service"):
    return Event(aid, t, etype, "deletion_worker", target, rid, result, f"Deletion worker submitted primary profile {rid}.")


def evidence(recs, file_id="ev_1", captured="2026-09-02T00:00:00Z"):
    m = {"file_id": file_id, "file_path": "evidence/t.json", "origin": "t", "captured_at": captured, "format": "json"}
    return parsers.parse_json(m, json.dumps({"records": recs}))[0]


def run(rows, events, obs=(), idx=None):
    idx = idx or toy_index()
    links = discovery.build_links(list(obs), idx)
    return matching.match(rows, events, links, idx)


SEP = ("2026-09-01T00:00:00Z", "2026-09-03T12:00:00Z")  # request window used by most tests


class MatchTests(unittest.TestCase):
    def test_explicit_link_current_identity(self):
        obs = evidence([{"resource_id": "account_1", "email": "new@x.io", "observed_at": "2026-09-02T00:00:00Z"}])
        _, cands, dec = run([req("case_1", "new@x.io", *SEP)], [ev("activity_1", "2026-09-03T11:00:00Z", "account_1")], obs)
        d = dec["case_1"]
        self.assertEqual((d.status, d.confidence, d.reason, d.method, d.resource_id, d.activity_id),
                         ("MATCHED", "HIGH", "EXPLICIT_LINK_IN_WINDOW", "explicit_evidence", "account_1", "activity_1"))
        link = next(s for s in d.chosen.supports if s.code == "EXPLICIT_LINK")
        self.assertEqual((link.evidence[0]["evidence_id"], link.evidence[0]["identity_field"]), ("ev_1", "email"))

    def test_historical_identity(self):
        # Request uses the old address; evidence uses the new one. Both identify user_1.
        obs = evidence([{"resource_id": "account_1", "email": "new@x.io", "observed_at": "2026-09-02T00:00:00Z"}])
        rq, _, dec = run([req("case_1", "OLD@x.io", *SEP)], [ev("activity_1", "2026-09-03T11:00:00Z", "account_1")], obs)
        self.assertEqual((rq[0].resolution, dec["case_1"].confidence), ("historical", "HIGH"))

    def test_activity_only_account(self):
        _, _, dec = run([req("case_1", "new@x.io", *SEP)], [ev("activity_1", "2026-09-03T11:00:00Z", "account_77")])
        d = dec["case_1"]
        self.assertEqual((d.confidence, d.reason, d.method, d.resource_id),
                         ("MEDIUM", "UNIQUE_FEASIBLE_CANDIDATE", "unique_candidate", "account_77"))
        self.assertFalse(d.chosen.explicit)

    def test_no_defensible_candidate(self):
        _, _, dec = run([req("case_1", "new@x.io", *SEP)], [ev("activity_1", "2026-08-01T00:00:00Z", "account_77")])
        self.assertEqual((dec["case_1"].status, dec["case_1"].reason), ("UNMATCHED", "NO_CANDIDATE_IN_WINDOW"))

    def test_two_requests_competing_for_one_event(self):
        rows = [req("case_1", "new@x.io", "2026-09-03T00:00:00Z", "2026-09-03T12:00:00Z"),
                req("case_2", "two@x.io", "2026-09-01T00:00:00Z", "2026-09-03T12:00:00Z")]
        events = [ev("activity_1", "2026-09-03T11:00:00Z", "account_1"), ev("activity_2", "2026-09-02T11:00:00Z", "account_2")]
        _, _, dec = run(rows, events)
        self.assertEqual((dec["case_1"].resource_id, dec["case_2"].resource_id), ("account_1", "account_2"))
        self.assertEqual(dec["case_2"].method, "global_assignment")

    def test_explicit_beats_temporal_proximity(self):
        obs = evidence([{"resource_id": "account_1", "email": "new@x.io", "observed_at": "2026-09-02T00:00:00Z"}])
        events = [ev("activity_1", "2026-09-03T10:50:00Z", "account_1"), ev("activity_2", "2026-09-03T11:10:00Z", "account_2")]
        _, _, dec = run([req("case_1", "new@x.io", *SEP)], events, obs)
        self.assertEqual((dec["case_1"].resource_id, dec["case_1"].confidence), ("account_1", "HIGH"))

    def test_meaningful_tie_abstains(self):
        rows = [req("case_1", "new@x.io", *SEP), req("case_2", "two@x.io", *SEP)]
        events = [ev("activity_1", "2026-09-03T11:00:00Z", "account_1"), ev("activity_2", "2026-09-03T10:50:00Z", "account_2")]
        _, _, dec = run(rows, events)
        for c in ("case_1", "case_2"):
            self.assertEqual((dec[c].status, dec[c].reason), ("UNMATCHED", "AMBIGUOUS_NEAR_TIE"))
            self.assertEqual(len(dec[c].considered), 2)  # competing candidates stay available

    def test_contradictory_evidence(self):
        # Only candidate contradicts the reported status -> LOW, not established.
        _, _, dec = run([req("case_1", "new@x.io", *SEP, status="NOT_STARTED")], [ev("activity_1", "2026-09-03T11:00:00Z", "account_1")])
        d = dec["case_1"]
        self.assertEqual((d.confidence, d.reason), ("LOW", "INFERRED_WITH_CONTRADICTION"))
        self.assertIn("RESULT_STATUS_MISMATCH", [s.code for s in d.chosen.contradictions])
        # Requester and someone else both linked in the same period: kept, flagged.
        obs = evidence([{"resource_id": "account_1", "email": "new@x.io", "observed_at": "2026-09-02T00:00:00Z"},
                        {"resource_id": "account_1", "email": "two@x.io", "observed_at": "2026-09-02T00:00:00Z"}])
        _, _, dec = run([req("case_1", "new@x.io", *SEP)], [ev("activity_1", "2026-09-03T11:00:00Z", "account_1")], obs)
        self.assertEqual((dec["case_1"].confidence, dec["case_1"].reason), ("MEDIUM", "EXPLICIT_LINK_WITH_CONTRADICTION"))
        self.assertIn("OTHER_SUBJECT_LINK", [s.code for s in dec["case_1"].chosen.contradictions])

    def test_reuse_across_periods_and_history_does_not_win(self):
        # account_9 belonged to user_1 in March; the ID is deleted again in September for user_2.
        obs = evidence([{"resource_id": "account_9", "email": "old@x.io", "observed_at": "2026-01-10T00:00:00Z"}],
                       captured="2026-01-11T00:00:00Z")
        rows = [req("case_1", "new@x.io", "2026-01-09T00:00:00Z", "2026-01-11T12:00:00Z"),
                req("case_2", "two@x.io", *SEP),
                req("case_3", "new@x.io", "2026-09-01T00:00:00Z", "2026-09-03T12:00:00Z")]
        events = [ev("activity_1", "2026-01-11T11:00:00Z", "account_9"), ev("activity_2", "2026-09-03T11:00:00Z", "account_9"),
                  ev("activity_3", "2026-09-03T10:20:00Z", "account_5")]
        _, cands, dec = run(rows, events, obs)
        self.assertEqual((dec["case_1"].activity_id, dec["case_1"].confidence), ("activity_1", "HIGH"))
        sept = {c.case_id: c for cs in cands.values() for c in cs if c.activity_id == "activity_2"}
        self.assertTrue(sept["case_2"].feasible)  # the March owner does not exclude user_2
        self.assertIn("HISTORICAL_OTHER_SUBJECT_LINK", [n.code for n in sept["case_2"].notes])
        self.assertFalse(sept["case_3"].explicit)  # user_1's March link is not current ownership in September
        self.assertIn("HISTORICAL_SELF_LINK", [n.code for n in sept["case_3"].notes])
        self.assertNotEqual(dec["case_3"].confidence, "HIGH")
        self.assertEqual(sum(d.activity_id == "activity_1" for d in dec.values()), 1)  # not reused later

    def test_trace_with_unrelated_resources_is_not_explicit(self):
        log = ("2026-09-02T10:00:00Z level=DEBUG service=account_service trace_id=tr-1 event=lookup_started lookup_key=new@x.io\n"
               "2026-09-02T10:00:01Z level=INFO service=account_service trace_id=tr-1 event=lookup_completed resource_id=account_1\n"
               "2026-09-02T10:00:02Z level=INFO service=search_service trace_id=tr-1 event=query_completed resource_id=search_1\n")
        m = {"file_id": "ev_l", "file_path": "evidence/t.log", "origin": "t", "captured_at": "2026-09-02T11:00:00Z", "format": "log"}
        obs, _ = parsers.parse_log(m, log)
        _, _, dec = run([req("case_1", "new@x.io", *SEP)], [ev("activity_1", "2026-09-03T11:00:00Z", "account_1")], obs)
        self.assertFalse(dec["case_1"].chosen.explicit)  # trace_multi is a candidate link only
        self.assertEqual(dec["case_1"].confidence, "MEDIUM")

    def test_cohort_file_does_not_establish_ownership(self):
        obs = evidence([{"resource_id": "account_1", "email": "new@x.io", "observed_at": "2026-09-02T00:00:00Z"},
                        {"resource_id": "account_2", "email": "two@x.io", "observed_at": "2026-09-02T00:00:00Z"},
                        {"resource_id": "account_3", "email": None, "observed_at": "2026-09-02T00:00:00Z"}])
        events = [ev("activity_2", "2026-09-03T11:00:00Z", "account_2"), ev("activity_3", "2026-09-03T11:05:00Z", "account_3")]
        _, cands, dec = run([req("case_1", "new@x.io", *SEP)], events, obs)
        by = {c.resource_id: c for c in cands["case_1"]}
        self.assertEqual((by["account_2"].feasible, by["account_2"].rejected_reason), (False, "OTHER_SUBJECT_CONTEMPORANEOUS_LINK"))
        self.assertFalse(by["account_3"].explicit)
        self.assertEqual((dec["case_1"].resource_id, dec["case_1"].confidence), ("account_3", "MEDIUM"))

    def test_window_boundaries(self):
        obs = evidence([{"resource_id": f"account_{i}", "email": "new@x.io", "observed_at": "2026-09-02T00:00:00Z"} for i in (1, 2, 3)])
        events = [ev("activity_1", "2026-08-31T23:59:00Z", "account_1"),   # before requested_at
                  ev("activity_2", "2026-09-03T12:20:00Z", "account_2"),   # > reported_at + 15 min
                  ev("activity_3", "2026-09-03T12:10:00Z", "account_3")]   # within the 15 min slack
        _, cands, dec = run([req("case_1", "new@x.io", *SEP)], events, obs)
        by = {c.resource_id: c for c in cands["case_1"]}
        self.assertEqual(by["account_1"].rejected_reason, "OUT_OF_WINDOW")
        self.assertEqual(by["account_2"].rejected_reason, "OUT_OF_WINDOW")
        self.assertEqual(dec["case_1"].resource_id, "account_3")

    def test_non_deletion_requests_use_their_own_family(self):
        rows = [req("case_1", "new@x.io", *SEP, status="NOT_STARTED", text="Please change the email address used for this account. I want to continue using the service.")]
        events = [ev("activity_1", "2026-09-03T11:00:00Z", "account_1", result="FAILED"),
                  ev("activity_2", "2026-09-03T11:00:00Z", "account_2", etype="CLASSIFY_REQUEST", target="privacy_case_management")]
        _, _, dec = run(rows, events)
        self.assertEqual((dec["case_1"].family, dec["case_1"].activity_id), ("request_classification", "activity_2"))


@requires_reference
class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, cls.obs, _ = ingest.ingest(DATASET)
        cls.idx = identity.load_identities(DATASET)
        cls.links = discovery.build_links(cls.obs, cls.idx, discovery.load_catalog_links(DATASET))
        rows = []
        for name in ("sample_deletion_requests.csv", "deletion_requests.csv"):
            with open(DATASET / name, encoding="utf-8") as f:
                rows += list(csv.DictReader(f))
        cls.rows, cls.events = rows, matching.load_events(DATASET)
        cls.requests, cls.cands, cls.dec = matching.match(rows, cls.events, cls.links, cls.idx)

    def test_account_6523_reuse(self):
        c = {cid: next(x for x in cs if x.resource_id == "account_6523") for cid, cs in self.cands.items()
             if any(x.resource_id == "account_6523" for x in cs)}
        # user_17's March association (evidence_67) does not reach the September event.
        for cid in ("case_17", "case_46"):
            self.assertEqual(c[cid].rejected_reason, "OUT_OF_WINDOW")
            self.assertIn("HISTORICAL_SELF_LINK", [n.code for n in c[cid].notes])
        self.assertNotEqual(self.dec["case_46"].resource_id, "account_6523")
        for cid in ("case_03", "case_44"):
            self.assertTrue(c[cid].feasible)
            self.assertIn("HISTORICAL_OTHER_SUBJECT_LINK", [n.code for n in c[cid].notes])
        holders = [d for d in self.dec.values() if d.resource_id == "account_6523"]
        self.assertTrue(all(d.confidence != "HIGH" for d in holders))

    def test_one_to_one_and_abstention_used(self):
        matched = [d for d in self.dec.values() if d.status == "MATCHED"]
        self.assertEqual(len({d.activity_id for d in matched}), len(matched))
        self.assertTrue(any(d.reason == "AMBIGUOUS_NEAR_TIE" for d in self.dec.values()))
        explicit_cases = {cid for cid, cs in self.cands.items() if any(c.explicit and c.feasible for c in cs)}
        self.assertEqual({d.case_id for d in matched if d.confidence == "HIGH"}, explicit_cases)

    def test_provenance_on_every_candidate_and_match(self):
        acts = {e.activity_id for e in self.events}
        for d in self.dec.values():
            for c in d.considered:
                self.assertIn(c.activity_id, acts)
                self.assertTrue(c.event_time and c.request_window and c.supports)
                for s in c.supports + c.contradictions + c.notes:
                    self.assertTrue(s.evidence and all(e.get("evidence_id") for e in s.evidence), s)
            if d.status == "MATCHED":
                self.assertIs(d.chosen, next(c for c in d.considered if c.activity_id == d.activity_id))
                if d.chosen.explicit:
                    link = next(s for s in d.chosen.supports if s.code == "EXPLICIT_LINK")
                    self.assertTrue(all(e["obs_id"] and e["locator"] for e in link.evidence))

    def test_deterministic(self):
        _, _, again = matching.match(list(reversed(self.rows)), list(reversed(self.events)), self.links, self.idx)
        dump = lambda d: json.dumps({k: v.to_dict() for k, v in sorted(d.items())}, sort_keys=True, default=str)  # noqa: E731
        self.assertEqual(dump(self.dec), dump(again))

    def test_production_code_never_reads_sample_answers(self):
        self.assertNotIn("sample_output", (CODE / "audit" / "matching.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
