"""P6: release-readiness regressions in the HTTP API, on temporary copies of the demo dataset.

Covers: a manifest naming evidence outside its dataset (by `..` or through a symlink/junction) is refused,
and no outside content is returned; server paths quoted by an OSError in a parser diagnostic are redacted;
finished jobs beyond the retention bound are dropped oldest first, never a queued or running one; the Host
header check (DNS rebinding); a non-ASCII-digit Content-Length; a stalled client is disconnected.
Concurrency uses events, never sleeps.

Run: python -m unittest discover -s code/tests -p "test_p6_*.py"
"""
import csv
import json
import os
import shutil
import socket
import sys
import threading
import unittest
from unittest import mock

from test_p4_api import DEMO, TIMEOUT, ApiCase  # first: puts code/ on sys.path

import api  # noqa: E402
from audit.inputs import InputError  # noqa: E402

SECRET = "OUTSIDE-THE-DATASET search_1234"


def add_evidence(dataset, file_id, file_path):
    with open(dataset / "evidence_files.csv", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    rows.append({"file_id": file_id, "file_path": file_path, "origin": "test", "captured_at": "2026-09-01T00:00:00Z"})
    with open(dataset / "evidence_files.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def link_dir(target, link):
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        if sys.platform != "win32":
            raise unittest.SkipTest("symlinks unavailable")
        import _winapi
        _winapi.CreateJunction(str(target), str(link))  # no privilege needed; resolve() follows it too


class ManifestContainmentTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        (self.outside / "notes.txt").write_text(f"line one\n{SECRET}\n", encoding="utf-8")

    def refused(self, file_path):
        add_evidence(self.root / "demo", "EV-X", file_path)
        jobs = self.jobs()
        data = self.finished(jobs, self.submit(jobs))
        self.assertEqual((data["status"], data["error"]["code"]), ("failed", "invalid_input"), data)
        self.assertIn("outside the dataset directory", data["error"]["message"])
        self.assertIn(repr(file_path), data["error"]["message"])  # the manifest's own value, as for any CSV error
        self.assertNotIn("OUTSIDE-THE-DATASET", json.dumps(data))
        return data

    def test_parent_reference_is_refused(self):
        self.assertNoServerPath(self.refused("../../outside/notes.txt"))

    def test_absolute_path_is_refused(self):
        self.refused((self.outside / "notes.txt").as_posix())

    def test_link_out_of_the_dataset_is_refused(self):
        link_dir(self.outside, self.root / "demo" / "evidence" / "linked")
        self.assertTrue((self.root / "demo" / "evidence" / "linked" / "notes.txt").is_file())  # only the check stops it
        self.assertNoServerPath(self.refused("evidence/linked/notes.txt"))

    def test_the_library_itself_is_unchanged(self):
        # Containment is an API rule: the CLI and audit() still read what the operator's manifest names.
        add_evidence(self.root / "demo", "EV-X", "../../outside/notes.txt")
        result = api.engine.audit(self.root / "demo")
        self.assertIn("OUTSIDE-THE-DATASET", json.dumps(result.to_dict()))

    def test_helper(self):
        dataset = (self.root / "demo").resolve()
        self.assertEqual(api.outside_dataset(dataset), [])  # the demo's own manifest
        self.assertEqual(api.outside_dataset(self.root.resolve()), [])  # no manifest: left to the engine


class PathRedactionTests(ApiCase):
    def test_os_error_in_a_parser_diagnostic(self):
        (self.root / "demo" / "evidence" / "adir.log").mkdir()  # reading a directory raises an OSError naming it
        add_evidence(self.root / "demo", "EV-D", "evidence/adir.log")
        jobs = self.jobs()
        data = self.finished(jobs, self.submit(jobs))
        self.assertEqual(data["status"], "completed")
        messages = [d.get("reason", "") for d in data["result"]["diagnostics"]["parser"] if d["file_id"] == "EV-D"]
        self.assertTrue(any("parser error" in m for m in messages), messages)
        self.assertNoServerPath(data)


class RetentionTests(ApiCase):
    def test_oldest_finished_jobs_are_dropped(self):
        def run(path):
            raise InputError("bad")  # finishes at once, as failed
        jobs = self.jobs(run, workers=1, keep=2)
        ids = []
        for _ in range(4):
            ids.append(self.submit(jobs))
            jobs.wait(ids[-1], TIMEOUT)
        for old in ids[:2]:
            self.assertError(self.call(jobs, "GET", f"/audits/{old}"), 404, "audit_not_found")
        for kept in ids[2:]:
            self.assertEqual(self.call(jobs, "GET", f"/audits/{kept}")[1]["status"], "failed")

    def test_running_jobs_are_never_dropped(self):
        started, release = threading.Event(), threading.Event()

        def run(path):
            if not started.is_set():
                started.set()
                self.assertTrue(release.wait(TIMEOUT))
            raise InputError("bad")
        jobs = self.jobs(run, workers=2, keep=1)
        slow = self.submit(jobs)
        self.assertTrue(started.wait(TIMEOUT))
        first = self.submit(jobs)
        jobs.wait(first, TIMEOUT)
        second = self.submit(jobs)
        jobs.wait(second, TIMEOUT)
        self.assertError(self.call(jobs, "GET", f"/audits/{first}"), 404, "audit_not_found")
        self.assertEqual(self.call(jobs, "GET", f"/audits/{slow}")[1]["status"], "running")  # older, but running
        release.set()
        jobs.wait(slow, TIMEOUT)
        self.assertEqual(self.call(jobs, "GET", f"/audits/{slow}")[1]["status"], "failed")
        self.assertError(self.call(jobs, "GET", f"/audits/{second}"), 404, "audit_not_found")

    def jobs(self, run=None, workers=2, max_pending=8, keep=api.KEEP_FINISHED):
        jobs = api.Jobs(self.root, workers, max_pending, run, keep)
        self.addCleanup(jobs.shutdown)
        return jobs


class HttpLayerTests(unittest.TestCase):
    """A real server on 127.0.0.1 with an OS-chosen port; raw sockets for what urllib won't send."""

    @classmethod
    def setUpClass(cls):
        cls.server = api.make_server(api.Config(DEMO.parent.resolve(), "127.0.0.1", 0))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.jobs.shutdown()

    def raw(self, request):
        with socket.create_connection(("127.0.0.1", self.port), timeout=TIMEOUT) as s:
            s.sendall(request)
            data = b""
            while chunk := s.recv(65536):
                data += chunk
        if not data:
            return None, None
        head, _, body = data.partition(b"\r\n\r\n")
        return int(head.split()[1]), json.loads(body)

    def get(self, host):
        return self.raw(f"GET /health HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())

    def test_host_header(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", "LOCALHOST", "[::1]:8000", "192.168.1.20"):
            self.assertEqual(self.get(host), (200, {"status": "ok"}), host)
        for host in ("attacker.example", f"attacker.example:{self.port}", "localhost.attacker.example", "127.0.0.1.nip.io"):
            status, body = self.get(host)
            self.assertEqual((status, body["error"]["code"]), (403, "host_not_allowed"), host)
        self.assertEqual(self.raw(b"GET /health HTTP/1.0\r\n\r\n"), (200, {"status": "ok"}))  # no Host at all

    def test_non_ascii_content_length(self):
        status, body = self.raw(b"POST /audits HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: \xb2\r\n\r\n")
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_request"))

    def test_port_in_use_fails_to_start(self):
        # On Windows the stdlib default (SO_REUSEADDR) let a second server "start" on this port and receive nothing.
        with self.assertRaises(OSError):
            api.make_server(api.Config(DEMO.parent.resolve(), "127.0.0.1", self.port)).server_close()

    def test_stalled_client_is_disconnected(self):
        with mock.patch.object(api.Handler, "timeout", 0.2):
            # Headers promise a body that never comes; the server drops the connection instead of waiting forever.
            self.assertEqual(self.raw(b"POST /audits HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 10\r\n\r\n"), (None, None))


if __name__ == "__main__":
    unittest.main()
