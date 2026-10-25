"""M1 self-tests: observation schema, per-format parsers, diagnostics, full-corpus coverage.

Run: python -m unittest discover -s code/tests -v
"""
import json
import sys
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

from audit import ingest, parsers  # noqa: E402
from audit.observation import Observation, parse_ts  # noqa: E402

DATASET = CODE.parent / "dataset"


def meta(fmt, file_id="ev_t"):
    return {"file_id": file_id, "file_path": f"evidence/test.{fmt}", "origin": "test",
            "captured_at": "2026-05-01T00:00:00Z", "format": fmt}


def by_id(obs):
    return {o.obs_id: o for o in obs}


class SchemaTests(unittest.TestCase):
    def test_schema_fields_and_serialization(self):
        documented = ["obs_id", "file_id", "file_path", "file_format", "file_origin", "file_captured_at", "kind",
                      "locator", "context", "resource_id", "system", "system_source", "record_type", "state",
                      "state_field", "operation", "observed_at", "observed_at_field", "identity_keys",
                      "related_resource_ids", "attrs", "raw"]
        self.assertEqual([f.name for f in fields(Observation)], documented)
        o, _ = parsers.parse_txt(meta("txt"), "- 2026-05-01T10:00:00Z | account_service | account_1 | USER_PROFILE | FOUND | a@x.io\n")
        json.dumps(o[0].to_dict())  # JSON-serializable

    def test_timestamps(self):
        self.assertEqual(parse_ts("2026-09-02T23:00:00Z"), "2026-09-02T23:00:00Z")
        self.assertEqual(parse_ts("2026-09-02T23:00:00"), "2026-09-02T23:00:00Z")  # naive -> UTC
        self.assertEqual(parse_ts("2026-09-03T04:30:00+05:30"), "2026-09-02T23:00:00Z")
        self.assertEqual(parse_ts("2026-09-02 23:00:00"), "2026-09-02T23:00:00Z")
        self.assertEqual(parse_ts("18 Jun 2026 10\n24 UTC"), "2026-06-18T10:24:00Z")
        self.assertEqual(parse_ts("05 Sep 2026"), "2026-09-05T00:00:00Z")
        for bad in (None, "", "null", "yesterday", "32 Foo 2026", 42):
            self.assertIsNone(parse_ts(bad), bad)


class JsonTests(unittest.TestCase):
    def test_nested_records_context_and_missing_fields(self):
        doc = {"exported_at": "2026-05-01T00:00:00Z", "service_exports": {"search_service": {"index_queries": [
            {"observed_at": "2026-04-30T09:00:00Z", "query": "A@X.IO", "resource_id": "search_1",
             "hits": {"total": 1, "documents": [{"resource_id": "search_1", "email": "a@x.io", "phone": None}]}},
            {"resource_id": "search_2", "query": None},
        ]}}}
        obs, diags = parsers.parse_json(meta("json"), json.dumps(doc))
        self.assertEqual(diags, [])
        rec = by_id(obs)
        parent, child = rec["ev_t:service_exports.search_service.index_queries[0]"], rec["ev_t:service_exports.search_service.index_queries[0].hits.documents[0]"]
        self.assertEqual((parent.system, parent.system_source), ("search_service", "json_path"))
        self.assertEqual(parent.attrs["hits"], "<nested records>")
        self.assertEqual(child.context["parent_resource_id"], "search_1")
        self.assertEqual((child.observed_at, child.observed_at_field), ("2026-04-30T09:00:00Z", "context.observed_at"))
        self.assertEqual([k["value"] for k in parent.identity_keys], ["a@x.io"])
        bare = rec["ev_t:service_exports.search_service.index_queries[1]"]
        self.assertIsNone(bare.observed_at)  # exported_at is file-level, never used as a record time
        self.assertEqual(bare.identity_keys, [])
        listing = rec["ev_t:service_exports.search_service.index_queries"]
        self.assertEqual((listing.kind, listing.system, listing.attrs["record_count"]), ("scope", "search_service", 2))

    def test_empty_snapshot_is_a_scope_observation(self):
        doc = {"snapshots": [{"captured_at": "2026-04-01T00:00:00Z", "data_store": "account_service",
                              "collection": "customer_profiles", "records": []}]}
        obs, _ = parsers.parse_json(meta("json"), json.dumps(doc))
        self.assertEqual(len(obs), 1)
        s = obs[0]
        self.assertEqual((s.kind, s.system, s.observed_at, s.attrs["record_count"]),
                         ("scope", "account_service", "2026-04-01T00:00:00Z", 0))

    def test_malformed_json_is_reported(self):
        obs, diags = parsers.parse_json(meta("json"), '{"records": [')
        self.assertEqual(obs, [])
        self.assertEqual(diags[0]["severity"], "error")


SQL = """-- Captured at: 2026-04-02T00:00:00Z
-- Database topology: read_replica
BEGIN TRANSACTION;
CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);
CREATE TABLE customer_profiles (resource_id TEXT PRIMARY KEY, email TEXT, enabled INTEGER NOT NULL CHECK (enabled IN (0,1)), account_status TEXT, updated_at TEXT NOT NULL);
CREATE TABLE support_records (resource_id TEXT PRIMARY KEY, requester_name TEXT, updated_at TEXT);
CREATE TABLE data_replica (
    resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL,
    record_json TEXT NOT NULL CHECK (json_valid(record_json)), updated_at TEXT NOT NULL
);
INSERT INTO snapshot_scope VALUES ('account_service', '2026-04-02T00:00:00Z');
INSERT INTO "customer_profiles" VALUES('account_1','a@x.io',1,'ACTIVE','2026-04-01T10:00:00Z');
INSERT INTO "data_replica" VALUES('account_1','account_service','{"user_id":"user_9","email":"A@X.IO","account_status":"DELETED","note":"it''s gone"}','2026-04-01T11:00:00Z');
INSERT INTO "customer_profiles" VALUES('account_2', NULL, 0, 'ACTIVE', -1.5e2);
ATTACH DATABASE 'should_not_exist.db' AS evil;
INSERT INTO "customer_profiles" VALUES('account_3', some_function());
COMMIT;
-- snapshot_completed_at=2026-04-02T00:00:00Z
"""


class SqlTests(unittest.TestCase):
    def setUp(self):
        self.obs, self.diags = parsers.parse_sql(meta("sql"), SQL)
        self.rec = by_id(self.obs)

    def test_tables_with_different_schemas(self):
        p, r = self.rec["ev_t:customer_profiles[1]"], self.rec["ev_t:data_replica[1]"]
        self.assertEqual((p.resource_id, p.state, p.observed_at, p.system), ("account_1", "ACTIVE", "2026-04-01T10:00:00Z", None))
        self.assertEqual((r.system, r.system_source, r.state, r.state_field), ("account_service", "origin_system", "DELETED", "record_json.account_status"))
        self.assertEqual(r.attrs["record_json"]["note"], "it's gone")  # '' escape
        self.assertEqual({(k["kind"], k["value"]) for k in r.identity_keys}, {("user_id", "user_9"), ("email", "a@x.io")})
        self.assertEqual(p.context["database_topology"], "read_replica")
        self.assertEqual(p.context["snapshot_completed_at"], "2026-04-02T00:00:00Z")
        self.assertEqual(self.rec["ev_t:customer_profiles[2]"].attrs["updated_at"], -150.0)

    def test_same_resource_in_two_tables_stays_distinct(self):
        same = [o for o in self.obs if o.resource_id == "account_1"]
        self.assertEqual(sorted((o.locator["table"], o.state) for o in same),
                         [("customer_profiles", "ACTIVE"), ("data_replica", "DELETED")])

    def test_scopes_including_empty_table(self):
        scope = self.rec["ev_t:snapshot_scope[1]"]
        self.assertEqual((scope.kind, scope.system, scope.observed_at), ("scope", "account_service", "2026-04-02T00:00:00Z"))
        self.assertEqual(self.rec["ev_t:support_records"].attrs["row_count"], 0)

    def test_unsupported_and_malformed_statements_are_reported_not_executed(self):
        reasons = [d["reason"] for d in self.diags]
        self.assertIn("unsupported SQL statement (not executed)", reasons)
        self.assertTrue(any(r.startswith("unparsed INSERT values") for r in reasons))
        self.assertFalse(Path("should_not_exist.db").exists())
        self.assertNotIn("ev_t:customer_profiles[3]", self.rec)

    def test_literal_parser(self):
        self.assertEqual(parsers.sql_values("('a''b', NULL, 3, -2.5), ('x', 1, 0, 0)"),
                         [["a'b", None, 3, -2.5], ["x", 1, 0, 0]])
        with self.assertRaises(ValueError):
            parsers.sql_values("(select 1)")


LOG = """2026-05-01T10:00:00Z level=DEBUG service=account_service trace_id=tr-1 event=profile_lookup_started lookup_key="Old@X.io" attempt=1
2026-05-01T10:00:01Z level=DEBUG service=account_service trace_id=tr-2 event=profile_lookup_started lookup_key="b@x.io"
2026-05-01T10:00:02Z level=INFO service=account_service trace_id=tr-1 event=profile_lookup_completed resource_id=account_1 rows=1 http_status=200
2026-05-01T10:00:03Z level=INFO service=account_service trace_id=tr-2 event=profile_lookup_completed rows=0 http_status=404
not a log line at all
2026-05-02T10:00:00Z level=INFO service=account_service trace_id=tr-3 event=profile_lookup_completed resource_id=account_1 rows=0 http_status=404
"""


class LogTests(unittest.TestCase):
    def test_trace_join_conflicts_and_diagnostics(self):
        obs, diags = parsers.parse_log(meta("log"), LOG)
        rec = by_id(obs)
        hit = rec["ev_t:L3"]
        self.assertEqual((hit.resource_id, hit.state, hit.operation, hit.observed_at),
                         ("account_1", "200", "profile_lookup_completed", "2026-05-01T10:00:02Z"))
        self.assertEqual(hit.locator, {"line": 3, "trace_id": "tr-1", "trace_lines": [1, 3]})
        self.assertEqual(hit.identity_keys, [{"kind": "email", "value": "old@x.io", "field": "trace:L1.lookup_key"}])
        miss = rec["ev_t:L4"]  # a lookup with no resource returned is kept, resource unknown
        self.assertIsNone(miss.resource_id)
        self.assertEqual((miss.state, miss.identity_keys[0]["value"]), ("404", "b@x.io"))
        later = rec["ev_t:L6"]  # same resource, later and conflicting: a separate observation
        self.assertEqual((later.resource_id, later.state), ("account_1", "404"))
        self.assertEqual([d["locator"] for d in diags], [{"line": 5}])

    def test_dialects(self):
        lines = {
            "logfmt": 'level=INFO service=search_service trace_id=tr-9 event=index_query_completed resource_id=search_5 result=hit',
            "json": '{"severity":"INFO","service":"search_service","trace_id":"tr-9","event":"index_query_completed","resource_id":"search_5","result":"hit"}',
            "bracket": '[INFO] [search_service] [tr-9] index_query_completed: resource_id="search_5" result="hit"',
            "pipe": '| INFO | search_service | tr-9 | index_query_completed | resource_id="search_5" result="hit"',
            "syslog": '<15>1 node-1 search_service tr-9 - - level=INFO event="index_query_completed" resource_id="search_5" result="hit"',
            "cloud": 'requestId=tr-9 level=INFO function=search_service message="resource_id=\\"search_5\\" event=\\"index_query_completed\\" result=\\"hit\\""',
        }
        for name, body in lines.items():
            with self.subTest(name):
                ts, f = parsers.parse_log_line("2026-05-01T10:00:00Z " + body)
                self.assertEqual((f["service"], f["trace_id"], f["event"], f["resource_id"], f["result"]),
                                 ("search_service", "tr-9", "index_query_completed", "search_5", "hit"))


class TextTests(unittest.TestCase):
    def test_txt_records_and_unparsed_lines(self):
        text = ("Snapshot: 2026-10-01T12:00:00Z\n"
                "- 2026-10-01T12:00:00Z | account_service | account_1 | USER_PROFILE | NOT_FOUND | A@X.IO\n"
                "- broken | account_2 | line\n")
        obs, diags = parsers.parse_txt(meta("txt"), text)
        self.assertEqual((obs[0].resource_id, obs[0].record_type, obs[0].state, obs[0].identity_keys[0]["value"]),
                         ("account_1", "USER_PROFILE", "NOT_FOUND", "a@x.io"))
        self.assertEqual(obs[0].context["snapshot"], "2026-10-01T12:00:00Z")
        self.assertEqual(diags[0]["locator"], {"line": 3})

    def test_pdf_page_provenance(self):
        m = next(x for x in ingest.load_manifest(DATASET) if x["file_id"] == "evidence_58")
        obs, diags = ingest.parse_file(m, DATASET)
        self.assertEqual(diags, [])
        p1, p2 = obs
        self.assertEqual((p1.locator, p2.locator), ({"page": 1}, {"page": 2}))
        self.assertIn("support_001", p1.related_resource_ids)
        self.assertIn(("email", "priya.raman@gmail.com"), {(k["kind"], k["value"]) for k in p1.identity_keys})
        self.assertIn("2026-09-12T07:30:00Z", [d["value"] for d in p2.attrs["dates"]])
        self.assertIsNone(p1.resource_id)  # page mentions are not asserted as the page's resource

    def test_empty_pdf_page_reported(self):
        obs, diags = parsers.parse_pdf_pages(meta("pdf"), ["text user_1", "  "])
        self.assertEqual(len(obs), 1)
        self.assertEqual(diags[0]["locator"], {"page": 2})


class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest, cls.obs, cls.diags = ingest.ingest(DATASET)

    def test_inventory(self):
        fmts = [m["format"] for m in self.manifest]
        self.assertEqual(len(fmts), 94)
        self.assertEqual({f: fmts.count(f) for f in set(fmts)},
                         {"sql": 24, "json": 28, "log": 26, "txt": 1, "pdf": 4, "image": 11})
        self.assertTrue(all((DATASET / m["file_path"]).exists() for m in self.manifest))
        self.assertTrue(all(m["captured_at"] for m in self.manifest))

    def test_no_errors_and_images_reported(self):
        # Since M6 the 11 screenshots are OCR-parsed: non-image files still have no diagnostics,
        # and image diagnostics are only the known, explicit OCR warnings (never errors).
        images = {m["file_id"] for m in self.manifest if m["format"] == "image"}
        self.assertEqual([d for d in self.diags if d["file_id"] not in images], [])
        image_diags = [d for d in self.diags if d["file_id"] in images]
        self.assertTrue(all(d["severity"] != "error" for d in image_diags), image_diags)
        self.assertTrue(all(d["reason"].split(":")[0] in ("PARTIAL_IDENTIFIER", "AMBIGUOUS_IDENTIFIER", "LOW_CONFIDENCE_TOKEN",
                                                          "OCR_ONSCREEN_TIME_AFTER_CAPTURE")  # M6.1: explicit warning
                            for d in image_diags), image_diags)
        self.assertEqual(len({o.file_id for o in self.obs if o.file_format == "image"}), 11)

    def test_coverage_and_unique_ids(self):
        cov = ingest.coverage(DATASET, self.manifest, self.obs)
        self.assertEqual(cov["unresolved"], [])
        self.assertGreater(cov["tokens"], 2000)
        self.assertEqual(len({o.obs_id for o in self.obs}), len(self.obs))
        self.assertTrue(all(o.file_id.startswith("evidence_") for o in self.obs))  # citation ids, not paths

    def test_deterministic(self):
        _, again, _ = ingest.ingest(DATASET)
        dump = lambda xs: "\n".join(json.dumps(o.to_dict(), sort_keys=True) for o in xs)  # noqa: E731
        self.assertEqual(dump(self.obs), dump(again))

    def test_missing_file_reported(self):
        with tempfile.TemporaryDirectory() as d:
            m = dict(self.manifest[0], file_path="evidence/missing.log")
            obs, diags = ingest.parse_file(m, d)
        self.assertEqual((obs, diags[0]["severity"]), ([], "error"))

    def test_generation_code_never_reads_sample_answers(self):
        import re
        literal = re.compile(r"""["'][^"'\n]*sample_output""")  # a string literal that could open the file
        for path in list((CODE / "audit").glob("*.py")) + [CODE / "main.py"]:
            self.assertIsNone(literal.search(path.read_text(encoding="utf-8")), path)


if __name__ == "__main__":
    unittest.main()
