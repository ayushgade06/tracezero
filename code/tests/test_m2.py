"""M2 self-tests: identity model, request resolution, evidence links, ownership status.

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

from audit import discovery, identity, ingest, parsers  # noqa: E402

from refdata import DATASET, requires_reference  # noqa: E402
H1, H2 = "a" * 64, "b" * 64


def write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def toy_index(extra_changes=()):
    """user_1 changed old@x.io -> new@x.io on 2026-03-01; user_2 is unrelated.
    extra_changes lets a test hand an old address to another person."""
    d = tempfile.mkdtemp()
    write_csv(Path(d) / "user_identities.csv", ["user_id", "name", "email", "phone", "email_hash", "updated_at"], [
        ["user_1", "Ana Silva", "new@x.io", "+1 555 0101", H2, "2026-03-01T00:00:00Z"],
        ["user_2", "Ana Silva", "other@x.io", "+1 555 0202", "c" * 64, "2026-01-01T00:00:00Z"],
    ])
    write_csv(Path(d) / "user_audit_log.csv", ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"], [
        ["audit_1", "user_1", "2026-03-01T00:00:00Z", "email", "old@x.io", "new@x.io"],
        ["audit_2", "user_1", "2026-03-01T00:00:00Z", "email_hash", H1, H2],
        *extra_changes,
    ])
    return identity.load_identities(d)


def meta(fmt, file_id="ev_t", captured="2026-05-01T00:00:00Z"):
    return {"file_id": file_id, "file_path": f"evidence/t.{fmt}", "origin": "test", "captured_at": captured, "format": fmt}


def records(file_id, recs, captured="2026-05-01T00:00:00Z"):
    obs, _ = parsers.parse_json(meta("json", file_id, captured), json.dumps({"records": recs}))
    return obs


def run(obs, index, catalog=()):
    links = discovery.build_links(obs, index, catalog)
    return links, discovery.classify_resources(obs, links)


class IdentityTests(unittest.TestCase):
    def test_current_and_historical_identifiers(self):
        idx = toy_index()
        old, new = identity.resolve_request(idx, "old@x.io"), identity.resolve_request(idx, "new@x.io")
        self.assertEqual((old["status"], old["user_ids"]), ("historical", ["user_1"]))
        self.assertEqual((new["status"], new["user_ids"]), ("current", ["user_1"]))
        i_old = old["identifiers"][0]
        self.assertEqual((i_old.source, i_old.valid_to), ("user_audit_log.csv:audit_1:old_value", "2026-03-01T00:00:00Z"))
        self.assertEqual(i_old.status_at("2026-02-01T00:00:00Z"), "valid")
        self.assertEqual(i_old.status_at("2026-04-01T00:00:00Z"), "superseded")
        self.assertEqual(new["identifiers"][0].status_at("2026-02-01T00:00:00Z"), "not_yet_valid")
        self.assertEqual(idx.users("email_hash", H1.upper()), ["user_1"])  # historical hash, case-insensitive hex

    def test_case_insensitive_email(self):
        r = identity.resolve_request(toy_index(), "  OLD@X.IO ")
        self.assertEqual((r["user_ids"], r["case_normalized"]), (["user_1"], True))

    def test_decoys_and_similar_identifiers_never_match(self):
        idx = toy_index()
        for decoy in ("old.jr@x.io", "ol@x.io", "old@x.io.evil", "old@x", "new@x.iox", "Ana Silva", ""):
            self.assertEqual(identity.resolve_request(idx, decoy)["status"], "unresolved", decoy)
        self.assertEqual(idx.users("phone", "+15550101"), [])  # phones are exact strings
        self.assertEqual(idx.users("user_id", "user_10"), [])

    def test_overlapping_historical_identifiers_stay_ambiguous(self):
        # user_2 later takes over the address user_1 gave up.
        idx = toy_index([["audit_3", "user_2", "2026-04-01T00:00:00Z", "email", "other@x.io", "old@x.io"]])
        r = identity.resolve_request(idx, "old@x.io")
        self.assertEqual((r["status"], r["user_ids"]), ("ambiguous", ["user_1", "user_2"]))
        self.assertIn(("email", "old@x.io"), idx.ambiguous_keys())
        obs = records("ev_a", [{"resource_id": "account_1", "email": "old@x.io", "observed_at": "2026-02-01T00:00:00Z"}])
        links, res = run(obs, idx)
        self.assertEqual({(l.user_id, l.key_status) for l in links}, {("user_1", "valid"), ("user_2", "not_yet_valid")})
        self.assertEqual(res["account_1"]["status"], "conflicting")  # kept, not resolved

    @requires_reference
    def test_real_identity_tables(self):
        idx = identity.load_identities(DATASET)
        self.assertEqual(len(idx.subjects), 850)
        self.assertEqual(idx.ambiguous_keys(), {})
        with open(DATASET / "deletion_requests.csv", encoding="utf-8") as f:
            res = [identity.resolve_request(idx, r["email"]) for r in csv.DictReader(f)]
        self.assertEqual(sum(len(r["user_ids"]) == 1 for r in res), 518)
        self.assertEqual(sum(r["status"] == "historical" for r in res), 100)


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.idx = toy_index()

    def test_explicit_record_and_catalog_links(self):
        obs = records("ev_a", [{"resource_id": "support_1", "requester_email": "NEW@x.io", "recorded_at": "2026-04-01T00:00:00Z"}])
        cat = [{"activity_id": "activity_9", "event_time": "2026-04-02T00:00:00Z", "event_type": "LINK_RESOURCE_IDENTITY",
                "target_system": "export_service", "resource_id": "export_1", "details": "Data catalog linked export_1 to new@x.io and user_1."}]
        links, res = run(obs, self.idx, cat)
        rec = next(l for l in links if l.resource_id == "support_1")
        self.assertEqual((rec.user_id, rec.link_type, rec.identity_field, rec.evidence_id, rec.obs_id),
                         ("user_1", "record", "requester_email", "ev_a", "ev_a:records[0]"))
        self.assertEqual({(l.link_type, l.identity_kind) for l in links if l.resource_id == "export_1"},
                         {("catalog", "email"), ("catalog", "user_id")})
        self.assertEqual({f["resource_id"] for f in discovery.discover("user_1", links, res)}, {"support_1", "export_1"})

    def test_trace_join_and_unrelated_resources_in_one_trace(self):
        log = ("2026-04-01T10:00:00Z level=DEBUG service=account_service trace_id=tr-1 event=profile_lookup_started lookup_key=old@x.io\n"
               "2026-04-01T10:00:01Z level=INFO service=account_service trace_id=tr-1 event=profile_lookup_completed resource_id=account_1 http_status=404\n"
               "2026-04-01T10:00:02Z level=DEBUG service=search_service trace_id=tr-2 event=index_query_started match_key=new@x.io\n"
               "2026-04-01T10:00:03Z level=INFO service=search_service trace_id=tr-2 event=index_query_completed resource_id=search_1 result=hit\n"
               "2026-04-01T10:00:04Z level=INFO service=analytics_platform trace_id=tr-2 event=analytics_profile_read resource_id=analytics_1\n")
        obs, _ = parsers.parse_log(meta("log"), log)
        links, res = run(obs, self.idx)
        one = next(l for l in links if l.resource_id == "account_1")
        self.assertEqual((one.link_type, one.identity_field, one.key_status, one.locator["trace_lines"]),
                         ("trace", "trace:L1.lookup_key", "superseded", [1, 2]))
        multi = {l.resource_id: l.link_type for l in links if l.locator.get("trace_id") == "tr-2"}
        self.assertEqual(multi, {"search_1": "trace_multi", "analytics_1": "trace_multi"})  # not direct
        found = {f["resource_id"]: f["strength"] for f in discovery.discover("user_1", links, res)}
        self.assertEqual(found, {"account_1": "direct", "search_1": "candidate", "analytics_1": "candidate"})

    def test_resource_reused_across_periods(self):
        obs = (records("ev_a", [{"resource_id": "account_7", "email": "old@x.io", "observed_at": "2026-02-01T00:00:00Z"}])
               + records("ev_b", [{"resource_id": "account_7", "email": "other@x.io", "observed_at": "2026-09-01T00:00:00Z"}]))
        links, res = run(obs, self.idx)
        r = res["account_7"]
        self.assertEqual((r["status"], r["conflict"]), ("conflicting", "disjoint_periods"))
        self.assertEqual(r["owners"]["user_1"]["span"], ("2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z"))
        for user, other in (("user_1", "user_2"), ("user_2", "user_1")):
            f = discovery.discover(user, links, res)[0]
            self.assertEqual((f["resource_id"], f["competing_owners"]), ("account_7", [other]))  # both kept

    def test_conflicting_ownership_same_period(self):
        obs = (records("ev_a", [{"resource_id": "search_9", "email": "new@x.io", "observed_at": "2026-05-01T00:00:00Z"}])
               + records("ev_b", [{"resource_id": "search_9", "email": "other@x.io", "observed_at": "2026-05-01T00:00:00Z"}]))
        _, res = run(obs, self.idx)
        self.assertEqual((res["search_9"]["status"], res["search_9"]["conflict"]), ("conflicting", "overlapping_periods"))

    def test_known_owner_plus_decoy_key_is_a_conflict(self):
        obs = records("ev_a", [{"resource_id": "account_3", "user_id": "user_1", "email": "new.jr@x.io"}])
        links, res = run(obs, self.idx)
        self.assertEqual(res["account_3"]["conflict"], "known_and_unregistered_keys")
        self.assertIn(None, {l.user_id for l in links})

    def test_cohort_file_does_not_spread_ownership(self):
        obs = records("ev_c", [
            {"resource_id": "account_1", "email": "new@x.io"},
            {"resource_id": "account_2", "email": "other@x.io"},
            {"resource_id": "analytics_3", "email": None, "customer_reference": None},
            {"resource_id": "search_4", "email": "new.jr@x.io"},
        ])
        links, res = run(obs, self.idx)
        self.assertEqual([f["resource_id"] for f in discovery.discover("user_1", links, res)], ["account_1"])
        self.assertEqual(res["analytics_3"]["status"], "unlinked")
        self.assertEqual(res["search_4"]["status"], "unregistered_only")
        self.assertFalse(any(l.resource_id == "analytics_3" for l in links))

    def test_pdf_page_and_mentions_are_candidates_only(self):
        obs, _ = parsers.parse_pdf_pages(meta("pdf"), ["Reference support_5\nCustomer new@x.io"])
        obs += records("ev_s", [{"resource_id": "search_1", "email": "new@x.io", "source_resource_id": "account_9"}])
        links, res = run(obs, self.idx)
        found = {f["resource_id"]: f["strength"] for f in discovery.discover("user_1", links, res)}
        self.assertEqual(found, {"support_5": "candidate", "account_9": "candidate", "search_1": "direct"})
        page = next(l for l in links if l.resource_id == "support_5")
        self.assertEqual((page.link_type, page.locator), ("page", {"page": 1}))


@requires_reference
class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest, cls.obs, _ = ingest.ingest(DATASET)
        cls.idx = identity.load_identities(DATASET)
        cls.cat = discovery.load_catalog_links(DATASET)
        cls.links = discovery.build_links(cls.obs, cls.idx, cls.cat)
        cls.res = discovery.classify_resources(cls.obs, cls.links)

    def test_every_link_has_provenance(self):
        files = {m["file_id"] for m in self.manifest}
        acts = {r["activity_id"] for r in self.cat}
        obs_ids = {o.obs_id for o in self.obs} | acts
        for l in self.links:
            self.assertTrue(l.evidence_id in files or l.evidence_id in acts, l)
            self.assertIn(l.obs_id, obs_ids)
            self.assertTrue(l.locator and l.identity_field and l.excerpt, l)
            if l.user_id:
                self.assertTrue(l.identity_source.startswith(("user_identities.csv:", "user_audit_log.csv:")), l)

    def test_decoys_create_no_known_user_links(self):
        for l in self.links:
            if ".jr@" in l.identity_value:
                self.assertIsNone(l.user_id, l)
        self.assertGreaterEqual(sum(l.user_id is None for l in self.links), 40)

    def test_deterministic(self):
        again = discovery.build_links(self.obs, self.idx, self.cat)
        dump = lambda ls: json.dumps([l.to_dict() for l in ls], sort_keys=True)  # noqa: E731
        self.assertEqual(dump(self.links), dump(again))

    def test_no_forced_assignment(self):
        # The same subject's two requests (user_18) both see the subject's full candidate set;
        # nothing is removed to make requests unique.
        found = discovery.discover("user_18", self.links, self.res)
        self.assertTrue(found)
        self.assertEqual(found, discovery.discover("user_18", self.links, self.res))


if __name__ == "__main__":
    unittest.main()
