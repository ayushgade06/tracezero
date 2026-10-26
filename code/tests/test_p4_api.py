"""P4: the HTTP API in code/api.py, on synthetic datasets in temporary directories.

Covers: the endpoints and their JSON contract; dataset_id validation, traversal, absolute paths and symlink
escapes; errors from the library and internal failures (sanitized); job state transitions; concurrent
audits on different datasets; no files written; the result equals the CLI's; one real socket round trip
on 127.0.0.1. Concurrency uses barriers and events, never sleeps.

Run: python -m unittest discover -s code/tests -p "test_p4_*.py"
"""
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import api  # noqa: E402
import refdata  # noqa: E402
from audit import engine, output  # noqa: E402
from audit.engine import audit  # noqa: E402
from audit.policy import PolicyError  # noqa: E402

DEMO = CODE / "tests" / "fixtures" / "demo"
TIMEOUT = 60
GUARDRAIL = "3e8aa75b6a771a8a8e76936fdce1f5b2b5239fcf12c431769dd94b943a969db1"


def snapshot(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(root).rglob("*")) if p.is_file()}


def second_dataset(path):
    """A copy of the demo whose reported request file holds only case_902 and case_904."""
    shutil.copytree(DEMO, path)
    with open(path / "deletion_requests.csv", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    moved = [r for r in rows[1:] if r[0] in ("case_901", "case_903")]
    with open(path / "deletion_requests.csv", "w", encoding="utf-8", newline="") as f:
        csv.writer(f, lineterminator="\n").writerows([rows[0]] + [r for r in rows[1:] if r not in moved])
    with open(path / "sample_deletion_requests.csv", "a", encoding="utf-8", newline="") as f:
        csv.writer(f, lineterminator="\n").writerows(moved)  # still matched jointly, just not reported


def csv_bytes(cases, path):
    output.write_output([(c["case_id"], c["deletion_status"], c["steps"]) for c in cases], path)
    return Path(path).read_bytes()


class ApiCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "datasets"
        self.root.mkdir()
        shutil.copytree(DEMO, self.root / "demo")

    def jobs(self, run=None, workers=2, max_pending=8):
        jobs = api.Jobs(self.root, workers, max_pending, run)
        self.addCleanup(jobs.shutdown)
        return jobs

    def call(self, jobs, method, target, body=b""):
        status, data = api.handle(jobs, method, target, body if isinstance(body, bytes) else json.dumps(body).encode())
        json.dumps(data)  # every response body is JSON
        return status, data

    def submit(self, jobs, dataset_id="demo"):
        status, data = self.call(jobs, "POST", "/audits", {"dataset_id": dataset_id})
        self.assertEqual((status, data["status"]), (202, "queued"), data)
        self.assertEqual(set(data), {"audit_id", "status"})
        return data["audit_id"]

    def finished(self, jobs, audit_id):
        jobs.wait(audit_id, TIMEOUT)
        status, data = self.call(jobs, "GET", f"/audits/{audit_id}")
        self.assertEqual(status, 200)
        return data

    def assertError(self, response, status, code):
        self.assertEqual(response[0], status, response[1])
        self.assertEqual(set(response[1]), {"error"})
        self.assertEqual(response[1]["error"]["code"], code)
        self.assertIsInstance(response[1]["error"]["message"], str)

    def assertNoServerPath(self, data):
        text = json.dumps(data)
        for p in (self.tmp, self.tmp.resolve(), CODE):
            for form in (str(p), p.as_posix(), json.dumps(str(p))[1:-1]):
                self.assertNotIn(form, text)


class EndpointTests(ApiCase):
    def test_health(self):
        self.assertEqual(self.call(self.jobs(), "GET", "/health"), (200, {"status": "ok"}))

    def test_submit_and_completed_result(self):
        jobs = self.jobs()
        audit_id = self.submit(jobs)
        self.assertRegex(audit_id, r"^[0-9a-f]{32}$")
        data = self.finished(jobs, audit_id)
        self.assertEqual(set(data), {"audit_id", "dataset_id", "status", "result"})
        self.assertEqual((data["audit_id"], data["dataset_id"], data["status"]), (audit_id, "demo", "completed"))
        expected = audit(self.root / "demo").to_dict()
        expected.update(dataset="demo", requests="demo/deletion_requests.csv")
        self.assertEqual(data["result"], expected)  # AuditResult.to_dict(), only the two path fields renamed
        self.assertEqual(json.loads(json.dumps(data["result"])), data["result"])
        self.assertEqual(data["result"]["summary"]["status"], {"INCOMPLETE": 2, "COMPLETE": 1, "NOT_APPLICABLE": 1})
        self.assertIsNone(data["result"]["output"])
        self.assertNoServerPath(data)

    def test_result_rows_equal_cli_output(self):
        jobs = self.jobs()
        result = self.finished(jobs, self.submit(jobs))["result"]
        cli = self.tmp / "cli.csv"
        subprocess.run([sys.executable, str(CODE / "main.py"), "--dataset", str(self.root / "demo"), "--out", str(cli)],
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"), check=True, capture_output=True)
        self.assertEqual(csv_bytes(result["cases"], self.tmp / "api.csv"), cli.read_bytes())

    def test_unknown_audit_ids(self):
        jobs = self.jobs()
        for target in ("/audits/" + "0" * 32, "/audits/nope", "/audits/", "/audits/../health"):
            self.assertError(self.call(jobs, "GET", target), 404, "audit_not_found")

    def test_unknown_routes_and_methods(self):
        jobs = self.jobs()
        self.assertError(self.call(jobs, "GET", "/"), 404, "not_found")
        self.assertError(self.call(jobs, "GET", "/audits"), 405, "method_not_allowed")
        self.assertError(self.call(jobs, "POST", "/health"), 405, "method_not_allowed")
        self.assertError(self.call(jobs, "DELETE", "/audits/" + "0" * 32), 405, "method_not_allowed")


class ValidationTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.run = mock.Mock(side_effect=AssertionError("no audit may start"))
        self.j = self.jobs(self.run)

    def tearDown(self):
        self.run.assert_not_called()
        self.assertEqual(self.j._jobs, {})  # rejected submissions create no job

    def post(self, body):
        return self.call(self.j, "POST", "/audits", body)

    def test_invalid_json(self):
        for body in (b"", b"{", b"nope", b"\xff\xfe", b"[]", b'"demo"', b"null", b"42"):
            self.assertError(self.post(body), 400, "invalid_json")

    def test_missing_dataset_id(self):
        self.assertError(self.post({}), 400, "invalid_request")
        self.assertError(self.post({"dataset": "demo"}), 400, "invalid_request")

    def test_unexpected_fields(self):
        for extra in ({"policy": "x.csv"}, {"settings": "s.json"}, {"out": "/tmp/x.csv"}, {"requests": "r.csv"}):
            self.assertError(self.post({"dataset_id": "demo", **extra}), 400, "invalid_request")

    def test_invalid_types_and_empty(self):
        for value in (None, 1, 1.5, True, [], ["demo"], {"id": "demo"}, "", " ", "demo "):
            self.assertError(self.post({"dataset_id": value}), 400, "invalid_dataset_id")

    def test_traversal(self):
        for value in ("..", ".", "../demo", "demo/..", "../../etc/passwd", "..\\demo", "demo/evidence",
                      "demo\\evidence", "%2e%2e", ".demo", "demo.", "a" * 65, "demo\x00", "demo:stream"):
            self.assertError(self.post({"dataset_id": value}), 400, "invalid_dataset_id")

    def test_absolute_paths(self):
        for value in (str(self.root / "demo"), (self.root / "demo").as_posix(), "/etc", "C:\\Windows", "C:demo",
                      "\\\\server\\share", str(DEMO)):
            self.assertError(self.post({"dataset_id": value}), 400, "invalid_dataset_id")

    def test_unknown_dataset(self):
        (self.root / "file.csv").write_text("x", encoding="utf-8")
        for value in ("nosuch", "file.csv", "fixtures", "tests"):
            response = self.post({"dataset_id": value})
            self.assertError(response, 404, "dataset_not_found")
            self.assertNoServerPath(response[1])

    def test_symlink_escape(self):
        outside = self.tmp / "outside"
        shutil.copytree(DEMO, outside)
        link = self.root / "escape"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            if sys.platform != "win32":
                self.skipTest("symlinks unavailable")
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))  # no privilege needed; resolve() follows it too
        self.assertTrue((link / "deletion_requests.csv").is_file())  # the link works: only the check stops it
        self.assertError(self.post({"dataset_id": "escape"}), 404, "dataset_not_found")


class FailureTests(ApiCase):
    def test_invalid_dataset_inputs(self):
        (self.root / "empty").mkdir()
        broken = self.root / "broken"
        shutil.copytree(DEMO, broken)
        (broken / "user_identities.csv").write_text("user_id,email\nu1,a@example.com\n", encoding="utf-8")
        jobs = self.jobs()
        for name, expected in (("empty", "empty: no request file"), ("broken", "broken/user_identities.csv")):
            data = self.finished(jobs, self.submit(jobs, name))
            self.assertEqual(set(data), {"audit_id", "dataset_id", "status", "error"})
            self.assertEqual((data["status"], data["error"]["code"]), ("failed", "invalid_input"))
            self.assertIn(expected, data["error"]["message"].replace("\\", "/"))
            self.assertNoServerPath(data)

    def test_policy_error_from_library(self):
        def run(path):
            raise PolicyError(f"{path / 'rules.csv'}:3: unknown required_action 'KEEP'")
        jobs = self.jobs(run)
        data = self.finished(jobs, self.submit(jobs))
        self.assertEqual(data["error"]["code"], "invalid_input")
        self.assertEqual(data["error"]["message"].replace("\\", "/"), "demo/rules.csv:3: unknown required_action 'KEEP'")
        self.assertNoServerPath(data)

    def test_internal_failure_is_sanitized(self):
        def run(path):
            raise RuntimeError(f"secret alice@example.com in {path} Traceback")
        jobs = self.jobs(run)
        with self.assertLogs("tracezero.api", "ERROR") as logs:
            data = self.finished(jobs, self.submit(jobs))
        self.assertEqual((data["status"], data["error"]), ("failed", api.INTERNAL))
        self.assertNotIn("alice", " ".join(logs.output))  # the log names the exception type only
        self.assertIn("RuntimeError", " ".join(logs.output))
        self.assertNoServerPath(data)

    def test_failure_after_audit_still_finishes(self):
        jobs = self.jobs(lambda path: object())  # not an AuditResult: serialisation fails
        with self.assertLogs("tracezero.api", "ERROR"):
            data = self.finished(jobs, self.submit(jobs))
        self.assertEqual((data["status"], data["error"]["code"]), ("failed", "internal_error"))


class StateAndConcurrencyTests(ApiCase):
    def test_state_transitions(self):
        started, release = threading.Event(), threading.Event()
        seen = []

        def run(path):
            started.set()
            self.assertTrue(release.wait(TIMEOUT))
            if seen:
                raise RuntimeError("second run fails")
            seen.append(path)
            return audit(path)
        jobs = self.jobs(run, workers=1, max_pending=2)
        first = self.submit(jobs)
        self.assertTrue(started.wait(TIMEOUT))
        second = self.submit(jobs)
        self.assertEqual(self.call(jobs, "GET", f"/audits/{first}")[1],
                         {"audit_id": first, "dataset_id": "demo", "status": "running"})
        self.assertEqual(self.call(jobs, "GET", f"/audits/{second}")[1],
                         {"audit_id": second, "dataset_id": "demo", "status": "queued"})
        self.assertError(self.call(jobs, "POST", "/audits", {"dataset_id": "demo"}), 503, "busy")
        with self.assertLogs("tracezero.api", "ERROR"):
            release.set()
            self.assertEqual(self.finished(jobs, first)["status"], "completed")
            self.assertEqual(self.finished(jobs, second)["status"], "failed")  # never left running
        self.assertEqual(seen, [(self.root / "demo").resolve()])
        self.submit(jobs)  # capacity is back

    def test_unique_ids(self):
        jobs = self.jobs(lambda path: audit(path))
        ids = {self.submit(jobs) for _ in range(8)}
        self.assertEqual(len(ids), 8)
        for audit_id in ids:
            jobs.wait(audit_id, TIMEOUT)

    def test_concurrent_audits_on_different_datasets(self):
        second_dataset(self.root / "other")
        before = snapshot(self.root)
        barrier = threading.Barrier(2, timeout=TIMEOUT)

        def run(path):  # both audits are inside the engine at the same time, or the barrier breaks
            barrier.wait()
            return engine.audit(path)
        jobs = self.jobs(run, workers=2)
        a, b = self.submit(jobs, "demo"), self.submit(jobs, "other")
        ra, rb = self.finished(jobs, a), self.finished(jobs, b)
        self.assertEqual((ra["status"], rb["status"]), ("completed", "completed"))
        self.assertEqual([c["case_id"] for c in ra["result"]["cases"]], ["case_901", "case_902", "case_903", "case_904"])
        self.assertEqual([c["case_id"] for c in rb["result"]["cases"]], ["case_902", "case_904"])
        for data, name in ((ra, "demo"), (rb, "other")):  # each equals a sequential audit of its own dataset
            expected = audit(self.root / name).to_dict()
            expected.update(dataset=name, requests=f"{name}/deletion_requests.csv")
            self.assertEqual(data["result"], expected)
        self.assertEqual(snapshot(self.root), before)  # no output written, no dataset modified
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["demo", "other"])

    def test_same_dataset_concurrently(self):
        barrier = threading.Barrier(2, timeout=TIMEOUT)

        def run(path):
            barrier.wait()
            return engine.audit(path)
        jobs = self.jobs(run, workers=2)
        a, b = self.submit(jobs), self.submit(jobs)
        self.assertEqual(self.finished(jobs, a)["result"], self.finished(jobs, b)["result"])

    def test_registry_lock_not_held_during_audit(self):
        started, release = threading.Event(), threading.Event()

        def run(path):
            started.set()
            self.assertTrue(release.wait(TIMEOUT))
            return audit(path)
        jobs = self.jobs(run, workers=1)
        audit_id = self.submit(jobs)
        self.assertTrue(started.wait(TIMEOUT))
        self.assertEqual(self.call(jobs, "GET", "/health")[0], 200)  # answered while the audit runs
        self.assertEqual(self.call(jobs, "GET", f"/audits/{audit_id}")[1]["status"], "running")
        release.set()
        self.assertEqual(self.finished(jobs, audit_id)["status"], "completed")

    def test_default_run_is_the_engine(self):
        jobs = self.jobs()
        with mock.patch.object(engine, "audit", wraps=engine.audit) as spy:
            self.finished(jobs, self.submit(jobs))
        spy.assert_called_once_with((self.root / "demo").resolve())  # default request file, policy, settings; no out


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = api.Config.from_env({})
        self.assertEqual((config.host, config.port, config.workers, config.max_pending), ("127.0.0.1", 8000, 2, 8))
        self.assertEqual(config.datasets, (CODE / "tests" / "fixtures").resolve())

    def test_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            config = api.Config.from_env({"TRACEZERO_API_DATASETS": d, "TRACEZERO_API_PORT": "9000",
                                          "TRACEZERO_API_WORKERS": "3", "TRACEZERO_API_MAX_PENDING": "5"})
            self.assertEqual((config.datasets, config.port, config.workers, config.max_pending), (Path(d).resolve(), 9000, 3, 5))

    def test_invalid(self):
        for env in ({"TRACEZERO_API_DATASETS": str(DEMO / "nosuch")}, {"TRACEZERO_API_DATASETS": str(DEMO / "README.md")},
                    {"TRACEZERO_API_PORT": "http"}, {"TRACEZERO_API_PORT": "0"}, {"TRACEZERO_API_PORT": "70000"},
                    {"TRACEZERO_API_WORKERS": "0"}, {"TRACEZERO_API_WORKERS": "2", "TRACEZERO_API_MAX_PENDING": "1"}):
            with self.subTest(env=env), self.assertRaises(api.ConfigError):
                api.Config.from_env(env)

    def test_startup_fails_clearly(self):
        cp = subprocess.run([sys.executable, str(CODE / "api.py")], capture_output=True, text=True,
                            env=dict(os.environ, TRACEZERO_API_DATASETS=str(DEMO / "nosuch"), PYTHONIOENCODING="utf-8"))
        self.assertEqual(cp.returncode, 1)
        self.assertIn("invalid configuration: TRACEZERO_API_DATASETS=", cp.stderr)


class SocketTests(unittest.TestCase):
    """One real server on 127.0.0.1 with an OS-chosen port, for the HTTP layer itself."""

    @classmethod
    def setUpClass(cls):
        cls.server = api.make_server(api.Config(DEMO.parent.resolve(), "127.0.0.1", 0))
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.jobs.shutdown()

    def request(self, method, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.status, dict(r.headers), json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), json.loads(e.read())

    def test_round_trip(self):
        self.assertEqual(self.request("GET", "/health")[::2], (200, {"status": "ok"}))
        status, headers, data = self.request("POST", "/audits", b'{"dataset_id": "demo"}', {"Content-Type": "application/json"})
        self.assertEqual((status, data["status"]), (202, "queued"))
        self.assertEqual(headers["Location"], f"/audits/{data['audit_id']}")
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.server.jobs.wait(data["audit_id"], TIMEOUT)
        status, _, job = self.request("GET", headers["Location"])
        self.assertEqual((status, job["status"], job["result"]["summary"]["rows"]), (200, "completed", 4))

    def test_errors_are_json(self):
        self.assertEqual(self.request("POST", "/audits", b"{")[2]["error"]["code"], "invalid_json")
        self.assertEqual(self.request("POST", "/audits", b'{"dataset_id": "../demo"}')[2]["error"]["code"], "invalid_dataset_id")
        self.assertEqual(self.request("GET", "/audits/x")[::2], (404, {"error": {
            "code": "audit_not_found", "message": "no audit with this id (audits are kept in memory until the server restarts)"}}))
        status, _, data = self.request("POST", "/audits", b"x" * (api.MAX_BODY + 1))
        self.assertEqual((status, data["error"]["code"]), (413, "payload_too_large"))


@refdata.requires_reference
class ReferenceTests(unittest.TestCase):
    def test_api_reproduces_guardrail(self):
        root = refdata.DATASET.parent
        jobs = api.Jobs(root, 1, 1)
        self.addCleanup(jobs.shutdown)
        status, data = api.handle(jobs, "POST", "/audits", json.dumps({"dataset_id": refdata.DATASET.name}).encode())
        self.assertEqual(status, 202, data)
        jobs.wait(data["audit_id"], 600)
        job = jobs.get(data["audit_id"])
        self.assertEqual(job["status"], "completed", job.get("error"))
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(hashlib.sha256(csv_bytes(job["result"]["cases"], Path(d) / "out.csv")).hexdigest(), GUARDRAIL)


if __name__ == "__main__":
    unittest.main()
