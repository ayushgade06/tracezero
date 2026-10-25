"""M4 self-tests: temporal state at audit_at, admissibility, reconciliation, reuse, attribution.

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

from audit import discovery, identity, ingest, matching, parsers, state  # noqa: E402
from audit.matching import Event  # noqa: E402

from refdata import DATASET, requires_reference  # noqa: E402
DELETE = "Please permanently delete the account associated with this email address."
REQ = ("2026-09-01T00:00:00Z", "2026-09-03T12:00:00Z", "2026-09-10T00:00:00Z")  # requested, reported, audit


def write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def toy_index():
    d = tempfile.mkdtemp()
    write_csv(Path(d) / "user_identities.csv", ["user_id", "name", "email", "phone", "email_hash", "updated_at"], [
        ["user_1", "A", "one@x.io", "+1 1", "a" * 64, "2026-01-01T00:00:00Z"],
        ["user_2", "B", "two@x.io", "+1 2", "b" * 64, "2026-01-01T00:00:00Z"],
    ])
    write_csv(Path(d) / "user_audit_log.csv", ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"], [])
    return identity.load_identities(d)


def req(case="case_1", email="one@x.io", times=REQ, status="COMPLETE"):
    return {"case_id": case, "email": email, "requested_at": times[0], "reported_status": status, "reported_at": times[1],
            "audit_at": times[2], "request_text": DELETE}


def meta(fid, fmt, captured):
    return {"file_id": fid, "file_path": f"evidence/{fid}.{fmt}", "origin": "t", "captured_at": captured, "format": fmt}


def snapshot(fid, captured, store, coll, records, *more):
    """One bundle file; `more` adds further (store, collection, records) snapshots to the same file."""
    parts = [(store, coll, records)] + [tuple(m) for m in more]
    doc = {"snapshots": [{"captured_at": captured, "data_store": s, "collection": c, "records": r} for s, c, r in parts]}
    return parsers.parse_json(meta(fid, "json", captured), json.dumps(doc))[0]


def ledger(fid, captured, records):
    return parsers.parse_json(meta(fid, "json", captured), json.dumps({"records": records}))[0]


def lookup(rid, result, when, who="one@x.io", system="account_service", rtype="USER_PROFILE"):
    return {"recorded_at": when, "system": system, "resource_id": rid, "record_type": rtype, "result": result, "subject_reference": who}


def ev(aid, t, rid, etype="DELETE_RECORD", result="SUCCESS", target="account_service", details=None):
    return Event(aid, t, etype, "worker", target, rid, result, details or f"Worker processed {rid}.")


def assess(obs, events=(), rows=None):
    idx = toy_index()
    links = discovery.build_links(obs, idx)
    res = discovery.classify_resources(obs, links)
    requests, _, dec = matching.match(rows or [req()], list(events), links, idx)
    out = state.reconstruct(requests, dec, obs, links, res, list(events))
    return {(a.case_id, a.resource_id): a for a in out}, dec


def ex(a):
    return a.aspects["existence"]


class AdmissibilityTests(unittest.TestCase):
    def test_present_before_and_at_cutoff(self):
        obs = ledger("ev_1", "2026-09-05T00:00:00Z", [lookup("account_1", "FOUND", "2026-09-05T00:00:00Z")])
        a = assess(obs)[0][("case_1", "account_1")]
        self.assertEqual((ex(a)["value"], ex(a)["basis"], a.summary), ("PRESENT", "OBSERVED", "PRESENT"))
        self.assertEqual(ex(a)["deciding"][0]["ref"], "ev_1:records[0]")

    def test_post_audit_file_never_establishes_state(self):
        obs = (ledger("ev_1", "2026-09-05T00:00:00Z", [lookup("account_1", "FOUND", "2026-09-05T00:00:00Z")])
               + ledger("ev_2", "2026-09-12T00:00:00Z", [lookup("account_1", "NOT_FOUND", "2026-09-08T00:00:00Z")]))
        a = assess(obs)[0][("case_1", "account_1")]
        self.assertEqual(ex(a)["value"], "PRESENT")  # the later NOT_FOUND is in a post-audit file
        self.assertIn({"ref": "ev_2:records[0]", "evidence_id": "ev_2", "reason": "FILE_CAPTURED_AFTER_AUDIT"}, a.excluded)
        only_post = ledger("ev_2", "2026-09-12T00:00:00Z", [lookup("account_1", "NOT_FOUND", "2026-09-08T00:00:00Z")])
        b = assess(only_post)[0][("case_1", "account_1")]
        self.assertEqual((ex(b)["value"], ex(b)["reasons"]), ("UNKNOWN", ["POST_AUDIT_EVIDENCE_ONLY"]))

    def test_record_time_after_audit(self):
        obs = ledger("ev_1", "2026-09-09T00:00:00Z", [lookup("account_1", "NOT_FOUND", "2026-09-11T00:00:00Z"),
                                                      lookup("account_1", "FOUND", "2026-09-04T00:00:00Z")])
        a = assess(obs)[0][("case_1", "account_1")]
        self.assertEqual(ex(a)["value"], "PRESENT")
        self.assertIn("RECORD_AFTER_AUDIT", [e["reason"] for e in a.excluded])

    def test_record_time_after_its_own_capture(self):
        inconsistent = ledger("ev_1", "2026-09-05T00:00:00Z", [lookup("account_1", "NOT_FOUND", "2026-09-07T00:00:00Z")])
        a = assess(inconsistent)[0][("case_1", "account_1")]
        self.assertEqual((ex(a)["value"], ex(a)["reasons"]), ("UNKNOWN", ["ONLY_INCONSISTENT_TIMESTAMPS"]))
        both = inconsistent + ledger("ev_2", "2026-09-06T00:00:00Z", [lookup("account_1", "FOUND", "2026-09-06T00:00:00Z")])
        b = assess(both)[0][("case_1", "account_1")]
        self.assertEqual(ex(b)["value"], "PRESENT")  # the inconsistent reading cannot override
        self.assertIn("TIMESTAMP_INCONSISTENT", [d.get("note") for d in ex(b)["contradicting"]])

    def test_lookup_attempts_carry_no_state(self):
        log = ("2026-09-05T10:00:00Z level=DEBUG service=account_service trace_id=tr-1 event=profile_lookup_started resource_id=account_1 lookup_key=one@x.io\n"
               "2026-09-05T10:00:01Z level=INFO service=account_service trace_id=tr-1 event=profile_lookup_completed resource_id=account_1 rows=0 http_status=404\n")
        obs, _ = parsers.parse_log(meta("ev_l", "log", "2026-09-05T11:00:00Z"), log)
        a = assess(obs)[0][("case_1", "account_1")]
        self.assertEqual((ex(a)["value"], ex(a)["basis"]), ("ABSENT", "OBSERVED"))  # explicit negative
        self.assertIn({"ref": "ev_l:L1", "evidence_id": "ev_l", "reason": "LOOKUP_ATTEMPT_NO_RESULT"}, a.excluded)


class ClaimTests(unittest.TestCase):
    def test_deletion_event_confirmed_by_state(self):
        obs = ledger("ev_1", "2026-09-05T00:00:00Z", [lookup("account_1", "NOT_FOUND", "2026-09-05T00:00:00Z")])
        a = assess(obs, [ev("activity_1", "2026-09-03T11:00:00Z", "account_1")])[0][("case_1", "account_1")]
        self.assertEqual((ex(a)["value"], ex(a)["basis"]), ("ABSENT", "OBSERVED"))
        self.assertIn("activity_1", [d["ref"] for d in ex(a)["supporting"]])

    def test_deletion_event_without_state_evidence(self):
        a = assess([], [ev("activity_1", "2026-09-03T11:00:00Z", "account_1")])[0][("case_1", "account_1")]
        self.assertEqual((ex(a)["value"], ex(a)["reasons"]), ("UNKNOWN", ["ACTIVITY_ONLY"]))
        self.assertEqual(a.association["attribution"], "ACTIVITY_MATCH_MEDIUM")
        # A success claim after the last reading makes the reading stale, never ABSENT.
        obs = ledger("ev_1", "2026-09-02T00:00:00Z", [lookup("account_1", "FOUND", "2026-09-02T00:00:00Z")])
        b = assess(obs, [ev("activity_1", "2026-09-03T11:00:00Z", "account_1")])[0][("case_1", "account_1")]
        self.assertEqual((ex(b)["value"], ex(b)["basis"]), ("PRESENT", "OBSERVED_STALE"))
        self.assertIn("activity_1", [d["ref"] for d in ex(b)["contradicting"]])

    def test_failed_then_successful_deletion(self):
        obs = ledger("ev_1", "2026-09-02T00:00:00Z", [lookup("account_1", "FOUND", "2026-09-01T06:00:00Z")])
        events = [ev("activity_1", "2026-09-02T12:00:00Z", "account_1", result="FAILED"),
                  ev("activity_2", "2026-09-03T11:00:00Z", "account_1")]
        a = assess(obs, events)[0][("case_1", "account_1")]
        self.assertEqual(ex(a)["basis"], "OBSERVED_STALE")
        notes = {d["ref"]: d["note"] for d in ex(a)["supporting"] + ex(a)["contradicting"] if d["kind"] == "activity_claim"}
        self.assertIn("failed", notes["activity_1"])
        self.assertIn("state may have changed", notes["activity_2"])
        confirmed = obs + ledger("ev_2", "2026-09-06T00:00:00Z", [lookup("account_1", "NOT_FOUND", "2026-09-06T00:00:00Z")])
        b = assess(confirmed, events)[0][("case_1", "account_1")]
        self.assertEqual((ex(b)["value"], ex(b)["basis"]), ("ABSENT", "OBSERVED"))
        self.assertIn("ev_1:records[0]", [d["ref"] for d in ex(b)["contradicting"]])  # earlier reading kept

    def test_restored_after_deletion(self):
        obs = ledger("ev_1", "2026-09-04T00:00:00Z", [lookup("search_1", "NOT_FOUND", "2026-09-04T00:00:00Z", system="search_service", rtype="SEARCH_INDEX_ENTRY")])
        restore = ev("activity_9", "2026-09-06T00:00:00Z", "backup_7", etype="RESTORE_BACKUP", target="search_service",
                     details="Restore job repopulated downstream resource search_1.")
        a = assess(obs, [restore])[0][("case_1", "search_1")]
        self.assertEqual((ex(a)["value"], ex(a)["basis"]), ("ABSENT", "OBSERVED_STALE"))
        self.assertIn("activity_9", [d["ref"] for d in ex(a)["contradicting"]])
        later = obs + ledger("ev_2", "2026-09-08T00:00:00Z", [lookup("search_1", "FOUND", "2026-09-08T00:00:00Z", system="search_service", rtype="SEARCH_INDEX_ENTRY")])
        self.assertEqual(ex(assess(later, [restore])[0][("case_1", "search_1")])["value"], "PRESENT")


class ResidualTests(unittest.TestCase):
    def test_tombstone_present_redacted_vs_exposed(self):
        clean = snapshot("ev_1", "2026-09-05T00:00:00Z", "identity_service", "deleted_customer_references",
                         [{"resource_id": "tombstone_1", "subject_reference": "user_1", "account_status": "DELETED",
                           "name": None, "email": None, "phone": None, "email_hash": None}])
        a = assess(clean)[0][("case_1", "tombstone_1")]
        self.assertEqual((ex(a)["value"], a.aspects["exposure"]["value"], a.summary), ("PRESENT", "REDACTED", "RESIDUAL"))
        exposed = snapshot("ev_1", "2026-09-05T00:00:00Z", "identity_service", "deleted_customer_references",
                           [{"resource_id": "tombstone_1", "subject_reference": "user_1", "account_status": "DELETED",
                             "name": "A", "email": "one@x.io", "phone": None, "email_hash": None}])
        b = assess(exposed)[0][("case_1", "tombstone_1")]
        self.assertEqual((ex(b)["value"], b.aspects["exposure"]["value"], b.summary), ("PRESENT", "IDENTIFIABLE", "PRESENT"))

    def test_redacted_record_versus_deleted_record(self):
        red = snapshot("ev_1", "2026-09-05T00:00:00Z", "support_platform", "support_conversations",
                       [{"resource_id": "support_1", "subject_reference": "user_1", "message_text": "[REDACTED]"}])
        a = assess(red)[0][("case_1", "support_1")]
        self.assertEqual((ex(a)["value"], a.aspects["exposure"]["value"]), ("PRESENT", "REDACTED"))  # not absent
        gone = ledger("ev_2", "2026-09-05T00:00:00Z", [lookup("support_1", "NOT_FOUND", "2026-09-05T00:00:00Z", system="support_platform", rtype="SUPPORT_MESSAGE")])
        b = assess(gone)[0][("case_1", "support_1")]
        self.assertEqual((ex(b)["value"], b.aspects["exposure"]["value"], b.summary), ("ABSENT", "UNKNOWN", "ABSENT"))

    def test_anonymized_analytics(self):
        obs = (ledger("ev_1", "2026-09-05T00:00:00Z", [lookup("analytics_1", "ANONYMIZED", "2026-09-05T00:00:00Z", system="analytics_platform", rtype="ANALYTICS_PROFILE")])
               + snapshot("ev_2", "2026-09-06T00:00:00Z", "analytics_platform", "customer_analytics_reports",
                          [{"resource_id": "analytics_1", "user_id": None, "email": None, "phone": None, "email_hash": None}]))
        a = assess(obs)[0][("case_1", "analytics_1")]
        self.assertEqual((ex(a)["value"], a.aspects["exposure"]["value"], a.summary), ("PRESENT", "ANONYMIZED", "RESIDUAL"))

    def test_flow_and_restore_flags_independent(self):
        obs = snapshot("ev_1", "2026-09-05T00:00:00Z", "integration_service", "connector_configurations",
                       [{"resource_id": "data_flow_1", "customer_email": "one@x.io", "enabled": "1"}])
        obs += snapshot("ev_2", "2026-09-05T00:00:00Z", "backup_service", "backup_catalog",
                        [{"resource_id": "backup_1", "subject_reference": "user_1", "restore_allowed": "false"}])
        r = assess(obs)[0]
        self.assertEqual(r[("case_1", "data_flow_1")].aspects["flow"]["value"], "ACTIVE")
        self.assertEqual(r[("case_1", "backup_1")].aspects["restore"]["value"], "BLOCKED")
        self.assertEqual(r[("case_1", "backup_1")].aspects["flow"]["value"], "UNKNOWN")


SQL = """-- Captured at: 2026-09-05T00:00:00Z
-- Database topology: read_replica
BEGIN TRANSACTION;
CREATE TABLE customer_data_primary (resource_id TEXT PRIMARY KEY, origin_system TEXT, collection_name TEXT, record_json TEXT, updated_at TEXT);
CREATE TABLE customer_data_read_replica (resource_id TEXT PRIMARY KEY, origin_system TEXT, collection_name TEXT, record_json TEXT, updated_at TEXT);
INSERT INTO "customer_data_primary" VALUES('search_9','search_service','customer_search_documents','{"resource_id":"search_9","email":"two@x.io"}','2026-09-05T00:00:00Z');
INSERT INTO "customer_data_read_replica" VALUES('account_1','account_service','customer_profiles','{"resource_id":"account_1","user_id":"user_1","email":"one@x.io"}','2026-09-05T00:00:00Z');
INSERT INTO "customer_data_read_replica" VALUES('search_9','search_service','customer_search_documents','{"resource_id":"search_9","email":"two@x.io"}','2026-09-05T00:00:00Z');
COMMIT;
"""


class ReconciliationTests(unittest.TestCase):
    def test_replica_drift_and_same_copy_conflict(self):
        obs, _ = parsers.parse_sql(meta("ev_s", "sql", "2026-09-05T00:00:00Z"), SQL)
        a = assess(obs)[0][("case_1", "account_1")]
        self.assertEqual(ex(a)["value"], "PRESENT")
        self.assertEqual(ex(a)["per_copy"], {"primary": "ABSENT", "read_replica": "PRESENT"})
        self.assertIn("EXISTENCE_REPLICA_DRIFT", a.reasons)
        clash = ledger("ev_1", "2026-09-05T00:00:00Z", [lookup("account_1", "FOUND", "2026-09-05T00:00:00Z"),
                                                        lookup("account_1", "NOT_FOUND", "2026-09-05T00:00:00Z")])
        b = assess(clash)[0][("case_1", "account_1")]
        self.assertEqual((ex(b)["value"], b.summary), ("CONFLICTING", "CONFLICTING"))
        self.assertEqual(len(ex(b)["deciding"]) + len(ex(b)["contradicting"]), 2)  # both sides kept

    def test_support_and_contradiction_listed(self):
        obs = (ledger("ev_1", "2026-09-02T00:00:00Z", [lookup("account_1", "FOUND", "2026-09-02T00:00:00Z")])
               + ledger("ev_2", "2026-09-06T00:00:00Z", [lookup("account_1", "NOT_FOUND", "2026-09-06T00:00:00Z")]))
        a = assess(obs)[0][("case_1", "account_1")]
        self.assertEqual(ex(a)["value"], "ABSENT")
        self.assertEqual([d["ref"] for d in ex(a)["deciding"]], ["ev_2:records[0]"])
        self.assertEqual([d["ref"] for d in ex(a)["contradicting"]], ["ev_1:records[0]"])

    def test_unscoped_or_partial_listing_is_not_absence(self):
        obs = (ledger("ev_1", "2026-09-02T00:00:00Z", [lookup("account_1", "FOUND", "2026-09-02T00:00:00Z")])
               + ledger("ev_2", "2026-09-06T00:00:00Z", [lookup("account_2", "FOUND", "2026-09-06T00:00:00Z", who="two@x.io")])
               + snapshot("ev_3", "2026-09-06T00:00:00Z", "account_service", "customer_profiles",
                          [{"resource_id": "account_5", "email": "one@x.io"}]))
        a = assess(obs)[0][("case_1", "account_1")]
        self.assertEqual((ex(a)["value"], ex(a)["basis"]), ("PRESENT", "OBSERVED"))

    def test_empty_scoped_snapshot_is_an_explicit_negative(self):
        base = ledger("ev_1", "2026-09-02T00:00:00Z", [lookup("account_1", "FOUND", "2026-09-02T00:00:00Z")])
        about_me = snapshot("ev_2", "2026-09-06T00:00:00Z", "account_service", "customer_profiles", [],
                            ("security_service", "security_audit_events", [{"resource_id": "audit_record_1", "subject_reference": "user_1"}]))
        a = assess(base + about_me)[0][("case_1", "account_1")]
        self.assertEqual((ex(a)["value"], ex(a)["basis"]), ("ABSENT", "INFERRED"))
        nameless = snapshot("ev_4", "2026-09-06T00:00:00Z", "account_service", "customer_profiles", [])
        # account_1 is known only from activity; its system comes from the id prefix (another
        # person's explicit account_service record teaches account_ -> account_service).
        b = assess(ledger("ev_1", "2026-09-02T00:00:00Z", [lookup("account_9", "FOUND", "2026-09-02T00:00:00Z", who="two@x.io")]) + nameless,
                   [ev("activity_1", "2026-09-03T11:00:00Z", "account_1")])[0][("case_1", "account_1")]
        self.assertEqual((ex(b)["value"], ex(b)["reasons"]), ("UNKNOWN", ["WEAK_ABSENCE_ONLY"]))


def pit_sql(rows, captured="2026-09-05T00:00:00Z", header=True):
    """A two-copy customer-data dump; rows: (table suffix, resource_id, system, collection, record dict)."""
    head = f"-- Ghost Data Still Exists: point-in-time database snapshot\n-- Captured at: {captured}\n-- Database topology: read_replica\n" if header else ""
    cols = "(resource_id TEXT PRIMARY KEY, origin_system TEXT, collection_name TEXT, record_json TEXT, updated_at TEXT)"
    body = "".join(f"INSERT INTO \"customer_data_{t}\" VALUES('{rid}','{sysn}','{coll}','{json.dumps(rec)}','{captured}');\n"
                   for t, rid, sysn, coll, rec in rows)
    text = f"{head}BEGIN TRANSACTION;\nCREATE TABLE customer_data_primary {cols};\nCREATE TABLE customer_data_read_replica {cols};\n{body}COMMIT;\n"
    return parsers.parse_sql(meta("ev_pit", "sql", captured), text)[0]


MY_AUDIT = [(t, "audit_record_1", "security_service", "security_audit_events", {"resource_id": "audit_record_1", "subject_reference": "user_1"})
            for t in ("primary", "read_replica")]
CLAIM = [ev("activity_1", "2026-09-03T11:00:00Z", "account_1")]
TEACH = ledger("ev_0", "2026-09-02T00:00:00Z", [lookup("account_9", "FOUND", "2026-09-02T00:00:00Z", who="two@x.io")])  # account_ -> account_service


class PointInTimeSnapshotTests(unittest.TestCase):  # R4e
    def account(self, obs, events=CLAIM):
        return assess(TEACH + obs, events)[0][("case_1", "account_1")]

    def test_missing_from_every_copy_of_a_snapshot_about_the_requester(self):
        a = self.account(pit_sql(MY_AUDIT))
        self.assertEqual((ex(a)["value"], ex(a)["basis"]), ("ABSENT", "INFERRED"))
        self.assertEqual(ex(a)["per_copy"], {"primary": "ABSENT", "read_replica": "ABSENT"})
        self.assertEqual({d["evidence_id"] for d in ex(a)["deciding"]}, {"ev_pit"})

    def test_not_absence_without_the_snapshot_header_or_about_someone_else(self):
        inventory_like = self.account(pit_sql(MY_AUDIT, header=False))
        self.assertEqual((ex(inventory_like)["value"], ex(inventory_like)["reasons"]), ("UNKNOWN", ["ACTIVITY_ONLY"]))
        theirs = [(t, "audit_record_2", "security_service", "security_audit_events", {"resource_id": "audit_record_2", "subject_reference": "user_2"})
                  for t in ("primary", "read_replica")]
        other = self.account(pit_sql(theirs))
        self.assertEqual(ex(other)["value"], "UNKNOWN")

    def test_cutoff_and_later_readings_still_apply(self):
        late = self.account(pit_sql(MY_AUDIT, captured="2026-09-12T00:00:00Z"))  # captured after audit_at
        self.assertEqual(ex(late)["value"], "UNKNOWN")
        found = ledger("ev_9", "2026-09-07T00:00:00Z", [lookup("account_1", "FOUND", "2026-09-07T00:00:00Z")])
        back = self.account(pit_sql(MY_AUDIT) + found)  # a later direct reading decides (R1)
        self.assertEqual(ex(back)["value"], "PRESENT")

    def test_replica_drift_is_unchanged(self):
        mine = ("read_replica", "account_1", "account_service", "customer_profiles", {"resource_id": "account_1", "user_id": "user_1", "email": "one@x.io"})
        a = self.account(pit_sql(MY_AUDIT + [mine]))
        self.assertEqual((ex(a)["value"], ex(a)["per_copy"]), ("PRESENT", {"primary": "ABSENT", "read_replica": "PRESENT"}))

    def test_only_an_exact_identifier_is_listed(self):
        near = [(t, "account_11", "account_service", "customer_profiles", {"resource_id": "account_11", "user_id": "user_2"})
                for t in ("primary", "read_replica")]
        self.assertEqual(ex(self.account(pit_sql(MY_AUDIT + near)))["value"], "ABSENT")  # account_11 is not account_1

    def test_presence_read_at_the_snapshot_time_conflicts(self):
        found = ledger("ev_9", "2026-09-05T00:00:00Z", [lookup("account_1", "FOUND", "2026-09-05T00:00:00Z")])
        self.assertEqual(ex(self.account(pit_sql(MY_AUDIT) + found))["value"], "CONFLICTING")

    def test_snapshot_before_the_request_is_history(self):
        a = self.account(pit_sql(MY_AUDIT, captured="2026-08-30T00:00:00Z"))
        self.assertEqual((ex(a)["value"], ex(a)["reasons"]), ("UNKNOWN", ["HISTORICAL_READINGS_ONLY"]))

    def test_later_recreation_claim_makes_the_absence_stale(self):
        sync = ev("activity_2", "2026-09-07T00:00:00Z", "sync_1", "SYNC_RECORD", details="Sync job repopulated downstream resource account_1.")
        a = self.account(pit_sql(MY_AUDIT), CLAIM + [sync])
        self.assertEqual((ex(a)["value"], ex(a)["basis"], ex(a)["reasons"]), ("ABSENT", "OBSERVED_STALE", ["LATER_CLAIM"]))


class OwnershipTests(unittest.TestCase):
    def test_reuse_and_other_owner_period(self):
        march = ledger("ev_1", "2026-03-05T00:00:00Z", [lookup("account_7", "FOUND", "2026-03-05T00:00:00Z", who="one@x.io")])
        sept = ledger("ev_2", "2026-09-05T00:00:00Z", [lookup("account_7", "FOUND", "2026-09-05T00:00:00Z", who="two@x.io")])
        rows = [req("case_1", "one@x.io"), req("case_2", "two@x.io")]
        r = assess(march + sept, rows=rows)[0]
        mine = r[("case_1", "account_7")]
        self.assertEqual(mine.association["attribution"], "HISTORICAL_LINK")  # March link, September request
        self.assertFalse(mine.association["attributed_to_requester"])
        self.assertIn({"ref": "ev_2:records[0]", "evidence_id": "ev_2", "reason": "OTHER_SUBJECT_INSTANCE"}, mine.excluded)
        theirs = r[("case_2", "account_7")]
        self.assertEqual(theirs.association["attribution"], "EVIDENCE_LINKED")
        self.assertIn({"ref": "ev_1:records[0]", "evidence_id": "ev_1", "reason": "OTHER_SUBJECT_INSTANCE"}, theirs.excluded)
        self.assertEqual(ex(theirs)["deciding"][0]["ref"], "ev_2:records[0]")

    def test_low_confidence_match_stays_inferred(self):
        r, dec = assess([], [ev("activity_1", "2026-09-03T11:00:00Z", "account_1", result="FAILED")])
        a = r[("case_1", "account_1")]
        self.assertEqual(dec["case_1"].confidence, "LOW")
        self.assertEqual((a.association["attribution"], a.association["attributed_to_requester"]), ("ACTIVITY_MATCH_LOW", False))
        self.assertIn("ATTRIBUTION_ACTIVITY_MATCH_LOW", a.ambiguity)

    def test_unmatched_request_gets_unattributed_alternatives(self):
        events = [ev("activity_1", "2026-09-03T11:00:00Z", "account_1"), ev("activity_2", "2026-09-03T10:50:00Z", "account_2")]
        rows = [req("case_1", "one@x.io"), req("case_2", "two@x.io")]
        r, dec = assess([], events, rows)
        self.assertEqual(dec["case_1"].status, "UNMATCHED")
        alts = [a for (c, _), a in r.items() if c == "case_1"]
        self.assertEqual({a.resource_id for a in alts}, {"account_1", "account_2"})
        self.assertTrue(all(a.association["attribution"] == "UNATTRIBUTED_ALTERNATIVE" and not a.association["attributed_to_requester"] for a in alts))


@requires_reference
class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest, cls.obs, _ = ingest.ingest(DATASET)
        cls.idx = identity.load_identities(DATASET)
        cls.links = discovery.build_links(cls.obs, cls.idx, discovery.load_catalog_links(DATASET))
        cls.res = discovery.classify_resources(cls.obs, cls.links)
        rows = []
        for name in ("sample_deletion_requests.csv", "deletion_requests.csv"):
            with open(DATASET / name, encoding="utf-8") as f:
                rows += list(csv.DictReader(f))
        cls.events = matching.load_events(DATASET)
        cls.requests, _, cls.dec = matching.match(rows, cls.events, cls.links, cls.idx)
        cls.A = state.reconstruct(cls.requests, cls.dec, cls.obs, cls.links, cls.res, cls.events)

    def test_post_cutoff_evidence_never_decides(self):
        captured = {m["file_id"]: m["captured_at"] for m in self.manifest}
        for a in self.A:
            for asp in a.aspects.values():
                for d in asp["deciding"]:
                    self.assertLessEqual(captured[d["evidence_id"]], a.audit_at.replace("+00:00", "Z"), (a.case_id, d))
                    self.assertLessEqual(d["time"], a.audit_at, (a.case_id, d))

    def test_traceable_and_qualified(self):
        obs_ids = {o.obs_id for o in self.obs}
        for a in self.A:
            for asp in a.aspects.values():
                for d in asp["deciding"] + asp["supporting"] + asp["contradicting"]:
                    self.assertTrue(d["ref"] in obs_ids or d["ref"].startswith("activity_"), d)
            if a.association["attribution"] in ("ACTIVITY_MATCH_LOW", "UNATTRIBUTED_ALTERNATIVE"):
                self.assertFalse(a.association["attributed_to_requester"])
        self.assertTrue(any(a.summary == "UNKNOWN" for a in self.A))

    def test_case_6523_low_match_ignores_other_instance(self):
        a = next(x for x in self.A if x.resource_id == "account_6523" and x.association["attribution"] == "ACTIVITY_MATCH_LOW")
        self.assertIn("OTHER_SUBJECT_INSTANCE", [e["reason"] for e in a.excluded])

    def test_deterministic(self):
        again = state.reconstruct(self.requests, self.dec, self.obs, self.links, self.res, self.events)
        dump = lambda xs: json.dumps([x.to_dict() for x in xs], sort_keys=True, default=str)  # noqa: E731
        self.assertEqual(dump(self.A), dump(again))

    def test_deterministic_across_processes(self):
        # Set iteration order changes with the hash seed; output must not.
        import hashlib
        import os
        import subprocess
        digests = set()
        for seed in ("1", "2"):
            with tempfile.TemporaryDirectory() as d:
                out = Path(d) / "a.json"
                subprocess.run([sys.executable, str(CODE / "evaluation" / "state_report.py"), "--dataset", str(DATASET), "--json", str(out)],
                               env=dict(os.environ, PYTHONHASHSEED=seed, PYTHONIOENCODING="utf-8"),
                               check=True, capture_output=True)
                digests.add(hashlib.sha256(out.read_bytes()).hexdigest())
        self.assertEqual(len(digests), 1)

    def test_inputs_not_modified_and_no_sample_answers(self):
        before = json.dumps([o.to_dict() for o in self.obs], sort_keys=True, default=str)
        state.reconstruct(self.requests, self.dec, self.obs, self.links, self.res, self.events)
        self.assertEqual(before, json.dumps([o.to_dict() for o in self.obs], sort_keys=True, default=str))
        self.assertNotIn("sample_output", (CODE / "audit" / "state.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
