"""TraceZero - minimal HTTP API over the audit library (P4). Local/development use only.

Usage: python code/api.py        (configuration from environment variables, see Config.from_env)

  GET  /health             -> 200 {"status": "ok"}  (the process is alive; says nothing about any dataset)
  GET  /datasets           -> 200 {"datasets": [{"dataset_id": "demo"}, ...]}  (ids only, never paths)
  POST /audits             {"dataset_id": "demo"} -> 202 {"audit_id": ..., "status": "queued"}
  GET  /audits/{audit_id}  -> 200 {"audit_id", "dataset_id", "status"[, "result" | "error"]}

A dataset_id names a directory directly beneath the server's dataset root; clients never send paths, and a
dataset whose evidence manifest names a file outside its own directory is refused. Every audit is
audit.engine.audit(<root>/<dataset_id>) with the engine's default request file, policy and settings, and
out=None, so the API writes no files. Jobs run on a bounded thread pool and live in memory: the oldest
finished ones are dropped beyond KEEP_FINISHED, a restart loses them all, and separate server processes don't
share them. Requests must name a local or IP-literal Host (no DNS rebinding). No authentication: results
quote evidence and may contain personal data. The full contract is in docs/API.md.
"""
import csv
import ipaddress
import json
import logging
import os
import re
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from audit import engine
from audit.inputs import InputError

CODE = Path(__file__).resolve().parent
log = logging.getLogger("tracezero.api")

QUEUED, RUNNING, COMPLETED, FAILED = "queued", "running", "completed", "failed"
DATASET_ID = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?")  # a single name: no separators, no "..", no drive
AUDIT_ID = re.compile(r"[0-9a-f]{32}")
MAX_BODY = 64 * 1024
KEEP_FINISHED = 32  # completed/failed jobs kept for GET; a reference-sized result is ~4.4 MiB of JSON
INTERNAL = {"code": "internal_error", "message": "the audit failed with an internal error"}


class ConfigError(ValueError):
    pass


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class Config:
    datasets: Path           # dataset root; each direct subdirectory is a dataset_id
    host: str = "127.0.0.1"
    port: int = 8000
    workers: int = 2         # audits running at once
    max_pending: int = 8     # queued + running audits accepted before POST /audits answers 503

    @classmethod
    def from_env(cls, env=os.environ):
        """TRACEZERO_API_DATASETS (default code/tests/fixtures, the synthetic demo), TRACEZERO_API_HOST,
        TRACEZERO_API_PORT, TRACEZERO_API_WORKERS, TRACEZERO_API_MAX_PENDING. Raises ConfigError."""
        def number(name, default, low, high):
            raw = env.get(name, str(default))
            try:
                value = int(raw)
            except ValueError:
                raise ConfigError(f"{name}={raw!r} is not an integer") from None
            if not low <= value <= high:
                raise ConfigError(f"{name}={value} is outside {low}..{high}")
            return value
        root = Path(env.get("TRACEZERO_API_DATASETS") or CODE / "tests" / "fixtures").expanduser()
        if not root.is_dir():
            raise ConfigError(f"TRACEZERO_API_DATASETS={str(root)!r} is not a directory")
        workers = number("TRACEZERO_API_WORKERS", 2, 1, 32)
        return cls(root.resolve(), env.get("TRACEZERO_API_HOST") or "127.0.0.1", number("TRACEZERO_API_PORT", 8000, 1, 65535),
                   workers, number("TRACEZERO_API_MAX_PENDING", 4 * workers, workers, 1024))


def resolve_dataset(root, dataset_id):
    """-> the dataset directory for a client-supplied id, or raise ApiError. root must be resolved."""
    if not isinstance(dataset_id, str):
        raise ApiError(400, "invalid_dataset_id", "dataset_id must be a string")
    if not DATASET_ID.fullmatch(dataset_id):
        raise ApiError(400, "invalid_dataset_id",
                       "dataset_id must be 1-64 letters, digits, '.', '_' or '-', starting and ending with a letter or digit")
    path = (root / dataset_id).resolve()  # follows symlinks and junctions, so the containment check sees the target
    if path.parent != root or not path.is_dir():
        raise ApiError(404, "dataset_not_found", f"no dataset {dataset_id!r} on this server")
    return path


def list_datasets(root):
    """-> sorted ids of the root's direct subdirectories that POST /audits would accept and that hold the
    default request file. Same checks as resolve_dataset, so a symlink or junction leaving the root is skipped."""
    try:
        names = sorted(p.name for p in root.iterdir())
    except OSError:
        raise ApiError(503, "datasets_unavailable", "the dataset root can't be read") from None
    found = []
    for name in names:
        try:
            if (resolve_dataset(root, name) / "deletion_requests.csv").is_file():
                found.append(name)
        except (ApiError, OSError):
            continue
    return found


def outside_dataset(path):
    """-> the manifest file_paths that resolve outside the dataset directory `path` (resolved), following symlinks
    and junctions. The engine reads whatever the manifest names; over HTTP that is limited to the dataset itself."""
    try:
        with open(path / "evidence_files.csv", encoding="utf-8", newline="") as f:
            names = [r.get("file_path") for r in csv.DictReader(f)]
    except (OSError, UnicodeDecodeError, csv.Error):
        return []  # missing or unreadable: the engine's input validation reports it
    bad = []
    for name in names:
        try:
            inside = not name or (path / name).resolve().is_relative_to(path)
        except (OSError, ValueError):
            inside = False
        if not inside:
            bad.append(name)
    return bad


class Jobs:
    """In-process job registry. The lock guards only the registry; audits run outside it, on the pool."""

    def __init__(self, root, workers=2, max_pending=8, run=None, keep=KEEP_FINISHED):
        self.root, self.max_pending, self.keep = Path(root).resolve(), max_pending, keep
        self._run = run or (lambda path: engine.audit(path))  # looked up per call: one audit path for CLI and API
        self._lock = threading.Lock()
        self._jobs, self._futures = {}, {}
        # ponytail: threads share one GIL, so concurrent audits interleave rather than run in parallel;
        # a process pool (or an external queue) is the upgrade when throughput matters.
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tracezero-audit")

    def submit(self, dataset_id):
        path = resolve_dataset(self.root, dataset_id)
        audit_id = uuid.uuid4().hex
        with self._lock:
            if sum(j["status"] in (QUEUED, RUNNING) for j in self._jobs.values()) >= self.max_pending:
                raise ApiError(503, "busy", "too many audits queued or running; retry later")
            try:
                self._futures[audit_id] = self._pool.submit(self._execute, audit_id, dataset_id, path)
            except RuntimeError:  # the pool is shutting down
                raise ApiError(503, "busy", "the server is shutting down") from None
            self._jobs[audit_id] = {"audit_id": audit_id, "dataset_id": dataset_id, "status": QUEUED}
        return audit_id

    def get(self, audit_id):
        """-> a copy of the job's public state, or None."""
        with self._lock:
            job = self._jobs.get(audit_id)
            return None if job is None else dict(job)

    def wait(self, audit_id, timeout=None):
        """Block until the job has finished (completed or failed)."""
        self._futures[audit_id].result(timeout)

    def shutdown(self):
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _set(self, audit_id, **state):
        with self._lock:
            self._jobs[audit_id].update(state)

    def _finish(self, audit_id, outcome):
        """Record the final state, then drop the earliest-finished jobs beyond `keep`. Re-inserting the job keeps
        finished jobs in finish order, so a long audit isn't dropped as it completes. Queued and running jobs are
        never dropped; a dropped id answers 404 audit_not_found, like an unknown one."""
        with self._lock:
            self._jobs[audit_id] = {**self._jobs.pop(audit_id), **outcome}
            finished = [i for i, j in self._jobs.items() if j["status"] in (COMPLETED, FAILED)]
            for old in finished[:max(0, len(finished) - self.keep)]:
                del self._jobs[old]
                self._futures.pop(old, None)

    def _execute(self, audit_id, dataset_id, path):
        self._set(audit_id, status=RUNNING)
        outcome = {"status": FAILED, "error": INTERNAL}  # whatever happens below, the job leaves RUNNING
        try:
            escaped = outside_dataset(path)
            if escaped:
                raise InputError(f"evidence_files.csv: file_path outside the dataset directory, refused by the API: {escaped[:5]}")
            result = self._run(path)
            # The only change to AuditResult: its two path fields name the dataset, not the server's directory.
            result = replace(result, dataset=dataset_id, requests=f"{dataset_id}/{Path(result.requests).name}")
            outcome = {"status": COMPLETED, "result": self._scrub(result.to_dict(), path, dataset_id)}
        except InputError as e:
            outcome = {"status": FAILED, "error": {"code": "invalid_input", "message": self._redact(str(e), path, dataset_id)}}
        except Exception as e:  # noqa: BLE001 - reported as a sanitized internal_error
            log.error("audit %s failed: %s", audit_id, type(e).__name__)  # no message: it may quote evidence
        finally:
            self._finish(audit_id, outcome)
        log.info("audit %s %s", audit_id, outcome["status"])

    def _redact(self, message, path, dataset_id):
        for prefix, name in ((path, dataset_id), (self.root, "<datasets>")):
            for form in (str(prefix), prefix.as_posix(), repr(str(prefix))[1:-1]):  # repr: OSError messages quote paths
                message = message.replace(form, name)
        return message

    def _scrub(self, value, path, dataset_id):
        """The result with server paths redacted in every string (e.g. an OSError quoted in a parser diagnostic)."""
        if isinstance(value, str):
            return self._redact(value, path, dataset_id)
        if isinstance(value, list):
            return [self._scrub(v, path, dataset_id) for v in value]
        if isinstance(value, dict):
            return {k: self._scrub(v, path, dataset_id) for k, v in value.items()}
        return value


def handle(jobs, method, target, body=b""):
    """-> (http status, JSON-compatible body). The whole HTTP contract, independent of the socket server."""
    path = urlsplit(target).path
    try:
        if path == "/health":
            allow(method, "GET")
            return 200, {"status": "ok"}
        if path == "/datasets":
            allow(method, "GET")
            return 200, {"datasets": [{"dataset_id": d} for d in list_datasets(jobs.root)]}
        if path == "/audits":
            allow(method, "POST")
            return 202, {"audit_id": jobs.submit(dataset_id_of(body)), "status": QUEUED}
        if path.startswith("/audits/"):
            allow(method, "GET")
            audit_id = path[len("/audits/"):]
            job = jobs.get(audit_id) if AUDIT_ID.fullmatch(audit_id) else None
            if job is None:
                raise ApiError(404, "audit_not_found",
                               f"no audit with this id (audits are kept in memory: the {KEEP_FINISHED} most recent finished ones, until the server restarts)")
            return 200, job
        raise ApiError(404, "not_found", "no such endpoint")
    except ApiError as e:
        return e.status, error(e.code, e.message)


def error(code, message):
    return {"error": {"code": code, "message": message}}


def allow(method, expected):
    if method != expected:
        raise ApiError(405, "method_not_allowed", f"use {expected}")


def dataset_id_of(body):
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApiError(400, "invalid_json", "the request body must be a UTF-8 JSON object") from None
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "the request body must be a UTF-8 JSON object")
    if "dataset_id" not in data:
        raise ApiError(400, "invalid_request", "dataset_id is required")
    extra = sorted(set(data) - {"dataset_id"})
    if extra:  # policy, settings, paths and output are server-side choices
        raise ApiError(400, "invalid_request", f"unexpected field(s): {extra}")
    return data["dataset_id"]


def local_host(header):
    """Is a Host header one a DNS-rebinding page can't send? localhost or an IP literal, any port; absent is fine."""
    if not header:
        return True
    try:
        host = urlsplit("//" + header).hostname or ""
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "TraceZero"
    sys_version = ""
    timeout = 30  # seconds a client may stall while sending a request; then the connection is dropped

    def _dispatch(self):
        raw = self.headers.get("Content-Length") or "0"
        length = int(raw) if raw.isascii() and raw.isdigit() else -1
        if not local_host(self.headers.get("Host")):
            self.close_connection = True
            status, body = 403, error("host_not_allowed", "the Host header must be localhost or an IP address")
        elif length < 0 or length > MAX_BODY:  # the body is not read, so the connection can't be reused
            self.close_connection = True
            if MAX_BODY < length <= 16 * MAX_BODY:
                self.rfile.read(length)  # closing with unread bytes resets the connection, and the client can lose the 413
            status, body = (400, error("invalid_request", "invalid Content-Length")) if length < 0 else \
                (413, error("payload_too_large", f"the body is limited to {MAX_BODY} bytes"))
        else:
            data = self.rfile.read(length)  # a client stalling past `timeout` raises here: the connection is dropped
            try:
                status, body = handle(self.server.jobs, self.command, self.path, data)
            except Exception as e:  # noqa: BLE001
                log.error("request failed: %s", type(e).__name__)
                status, body = 500, {"error": INTERNAL}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if status == 202:
            self.send_header("Location", f"/audits/{body['audit_id']}")
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _dispatch


class Server(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR (http.server's default) lets a second server bind a port that is already in use:
    # it starts without error and never receives a request. Elsewhere it only skips TIME_WAIT, which is harmless.
    allow_reuse_address = sys.platform != "win32"


def make_server(config, run=None):
    """-> a ThreadingHTTPServer bound to config.host:config.port, with its Jobs as server.jobs. Raises OSError
    if the port is in use."""
    server = Server((config.host, config.port), Handler)
    server.jobs = Jobs(config.datasets, config.workers, config.max_pending, run)
    return server


def main(env=os.environ):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        config = Config.from_env(env)
    except ConfigError as e:
        raise SystemExit(f"invalid configuration: {e}")
    try:
        server = make_server(config)
    except OSError as e:  # typically another server (perhaps another TraceZero API) already has the port
        raise SystemExit(f"cannot listen on {config.host}:{config.port}: {e.strerror or e}")
    host, port = server.server_address[:2]
    if config.host not in ("127.0.0.1", "localhost"):
        log.warning("listening on %s: reachable from other machines, with no authentication", config.host)
    log.info("TraceZero API on http://%s:%s, datasets in %s", host, port, config.datasets)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.jobs.shutdown()


if __name__ == "__main__":
    sys.exit(main())
