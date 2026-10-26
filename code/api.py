"""TraceZero - minimal HTTP API over the audit library (P4). Local/development use only.

Usage: python code/api.py        (configuration from environment variables, see Config.from_env)

  GET  /health             -> 200 {"status": "ok"}  (the process is alive; says nothing about any dataset)
  POST /audits             {"dataset_id": "demo"} -> 202 {"audit_id": ..., "status": "queued"}
  GET  /audits/{audit_id}  -> 200 {"audit_id", "dataset_id", "status"[, "result" | "error"]}

A dataset_id names a directory directly beneath the server's dataset root; clients never send paths.
Every audit is audit.engine.audit(<root>/<dataset_id>) with the engine's default request file, policy and
settings, and out=None, so the API writes no files. Jobs run on a bounded thread pool and live in memory:
a restart loses them, and separate server processes don't share them. No authentication: results quote
evidence and may contain personal data. The full contract is in docs/API.md.
"""
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


class Jobs:
    """In-process job registry. The lock guards only the registry; audits run outside it, on the pool."""

    def __init__(self, root, workers=2, max_pending=8, run=None):
        self.root, self.max_pending = Path(root).resolve(), max_pending
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

    def _execute(self, audit_id, dataset_id, path):
        self._set(audit_id, status=RUNNING)
        outcome = {"status": FAILED, "error": INTERNAL}  # whatever happens below, the job leaves RUNNING
        try:
            result = self._run(path)
            # The only change to AuditResult: its two path fields name the dataset, not the server's directory.
            result = replace(result, dataset=dataset_id, requests=f"{dataset_id}/{Path(result.requests).name}")
            outcome = {"status": COMPLETED, "result": result.to_dict()}
        except InputError as e:
            outcome = {"status": FAILED, "error": {"code": "invalid_input", "message": self._redact(str(e), path, dataset_id)}}
        except Exception as e:  # noqa: BLE001 - reported as a sanitized internal_error
            log.error("audit %s failed: %s", audit_id, type(e).__name__)  # no message: it may quote evidence
        finally:
            self._set(audit_id, **outcome)
        log.info("audit %s %s", audit_id, outcome["status"])

    def _redact(self, message, path, dataset_id):
        for prefix, name in ((path, dataset_id), (self.root, "<datasets>")):
            for form in (str(prefix), prefix.as_posix()):
                message = message.replace(form, name)
        return message


def handle(jobs, method, target, body=b""):
    """-> (http status, JSON-compatible body). The whole HTTP contract, independent of the socket server."""
    path = urlsplit(target).path
    try:
        if path == "/health":
            allow(method, "GET")
            return 200, {"status": "ok"}
        if path == "/audits":
            allow(method, "POST")
            return 202, {"audit_id": jobs.submit(dataset_id_of(body)), "status": QUEUED}
        if path.startswith("/audits/"):
            allow(method, "GET")
            audit_id = path[len("/audits/"):]
            job = jobs.get(audit_id) if AUDIT_ID.fullmatch(audit_id) else None
            if job is None:
                raise ApiError(404, "audit_not_found", "no audit with this id (audits are kept in memory until the server restarts)")
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


class Handler(BaseHTTPRequestHandler):
    server_version = "TraceZero"
    sys_version = ""

    def _dispatch(self):
        raw = self.headers.get("Content-Length") or "0"
        length = int(raw) if raw.isdigit() else -1
        if length < 0 or length > MAX_BODY:  # the body is not read, so the connection can't be reused
            self.close_connection = True
            status, body = (400, error("invalid_request", "invalid Content-Length")) if length < 0 else \
                (413, error("payload_too_large", f"the body is limited to {MAX_BODY} bytes"))
        else:
            try:
                status, body = handle(self.server.jobs, self.command, self.path, self.rfile.read(length))
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


def make_server(config, run=None):
    """-> a ThreadingHTTPServer bound to config.host:config.port, with its Jobs as server.jobs."""
    server = ThreadingHTTPServer((config.host, config.port), Handler)
    server.jobs = Jobs(config.datasets, config.workers, config.max_pending, run)
    return server


def main(env=os.environ):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        config = Config.from_env(env)
    except ConfigError as e:
        raise SystemExit(f"invalid configuration: {e}")
    server = make_server(config)
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
