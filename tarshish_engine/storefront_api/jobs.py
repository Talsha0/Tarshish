"""Background jobs for the web admin: run CLI commands without holding an HTTP request open.

Every admin action that can take longer than a request (harvest, publish, verify, seed, the
queue worker) becomes a *job*: a row in ``job_runs`` plus one task on a single worker thread.
The job runs the very same code as ``python main.py …`` (``main.dispatch``) against the API's
engine, and everything that code prints, and every engine log record it emits, is appended to
the job's log. The admin portal polls ``GET /api/admin/jobs/{id}`` to stream it.

Design notes
------------
* Jobs run one at a time, in submission order, like commands typed into one terminal. That
  keeps SQLite writers, provider "quota exhausted" memory and the AI budget guard simple.
* Output is captured per thread: ``sys.stdout`` / ``sys.stderr`` are wrapped once by a router
  that sends writes from a job's thread to that job and everything else to the real stream, so
  the API's own output is never swallowed.
* The log lives in memory while the job runs and is flushed to the database at most once a
  second (and when the job ends), so a poll served by another process is at most a second
  behind. Every line is redacted with the same filter as the engine's log files.
* A job left QUEUED/RUNNING by a process that died is marked FAILED on the next start.
* Serverless hosts (Vercel) freeze a function as soon as its response is sent, so background
  jobs need the long-running backend (uvicorn on a VPS / behind a tunnel); see DEPLOYMENT.md.
"""

from __future__ import annotations

import io
import json
import logging
import secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

from core.database import Database
from core.models import utcnow
from core.resilience import LOGGER_NAME, get_logger, redact

logger = get_logger("admin.jobs")

MAX_LOG_CHARS = 400_000          # keep the newest output when a job prints more than this
FLUSH_INTERVAL_SECONDS = 1.0
INTERRUPTED_AFTER = timedelta(hours=6)
EXIT_STATUS = {0: "SUCCEEDED"}   # any other exit code is FAILED (1 failures, 2 config, 3 budget)

_local = threading.local()


class _ThreadRouter(io.TextIOBase):
    """A stand-in for ``sys.stdout``/``sys.stderr`` that diverts writes made by job threads."""

    def __init__(self, fallback: Any) -> None:
        self.fallback = fallback

    def write(self, text: str) -> int:  # type: ignore[override]
        sink = getattr(_local, "sink", None)
        if sink is not None:
            sink(text)
            return len(text)
        return self.fallback.write(text)

    def flush(self) -> None:
        if getattr(_local, "sink", None) is None:
            self.fallback.flush()

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(self.fallback, "encoding", "utf-8") or "utf-8"

    def isatty(self) -> bool:
        return False

    def reconfigure(self, **_: Any) -> None:  # main._utf8_stdio() compatibility
        return None


def _install_router() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if not isinstance(stream, _ThreadRouter):
            setattr(sys, name, _ThreadRouter(stream))
    # A console log handler created after the router was installed would copy every engine log
    # line into the job a second time (``_JobLogHandler`` already records them): unwrap it.
    for log in (logging.getLogger(), logging.getLogger(LOGGER_NAME)):
        for handler in log.handlers:
            stream = getattr(handler, "stream", None)
            if isinstance(handler, logging.StreamHandler) and isinstance(stream, _ThreadRouter):
                handler.stream = stream.fallback


class _JobLogHandler(logging.Handler):
    """Forwards engine log records emitted on a job's thread into that job's log."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        sink = getattr(_local, "sink", None)
        if sink is None:
            return
        try:
            sink(self.format(record) + "\n")
        except Exception:  # a log line must never break the job
            pass


@dataclass
class Job:
    id: str
    kind: str
    params: dict[str, Any]
    argv: list[str]
    status: str = "QUEUED"
    exit_code: int | None = None
    error: str | None = None
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    _chunks: list[str] = field(default_factory=list)
    _size: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _last_flush: float = 0.0
    _partial: str = ""

    def write(self, text: str) -> None:
        with self._lock:
            text = self._partial + text
            # Redact whole lines only, so a secret split across two writes is still caught.
            complete, _, self._partial = text.rpartition("\n")
            if complete:
                cleaned = redact(complete) + "\n"
                self._chunks.append(cleaned)
                self._size += len(cleaned)
                while self._size > MAX_LOG_CHARS and len(self._chunks) > 1:
                    self._size -= len(self._chunks.pop(0))

    def text(self) -> str:
        with self._lock:
            tail = redact(self._partial) if self._partial else ""
            return "".join(self._chunks) + tail


Action = Callable[[Job], int]


class JobRunner:
    """Queues admin actions on one worker thread and records their output in ``job_runs``."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="admin-job")
        self._live: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._handler = _JobLogHandler()
        engine_logger = logging.getLogger(LOGGER_NAME)
        if not any(isinstance(h, _JobLogHandler) for h in engine_logger.handlers):
            engine_logger.addHandler(self._handler)
        try:
            stale = db.fail_interrupted_jobs(utcnow() - INTERRUPTED_AFTER)
            if stale:
                logger.warning("%d admin job(s) from a stopped process were marked FAILED.", stale)
        except Exception as exc:  # pragma: no cover - never block the API on housekeeping
            logger.warning("Could not tidy up interrupted admin jobs: %s", exc)

    # -- submission ------------------------------------------------------------- #
    def submit(self, kind: str, params: dict[str, Any], argv: list[str], action: Action) -> Job:
        job = Job(id=secrets.token_hex(8), kind=kind, params=params, argv=argv, created_at=utcnow().isoformat())
        self.db.create_job_run(job.id, kind, params, argv)
        with self._lock:
            self._live[job.id] = job
        command = " ".join(["python main.py", *argv]) if argv else kind
        job.write(f"$ {command}\n")
        self._executor.submit(self._run, job, action)
        return job

    def _flush(self, job: Job, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - job._last_flush >= FLUSH_INTERVAL_SECONDS:
            job._last_flush = now
            try:
                self.db.write_job_log(job.id, job.text())
            except Exception as exc:  # pragma: no cover
                logger.warning("Could not save the log of job %s: %s", job.id, exc)

    def _run(self, job: Job, action: Action) -> None:
        _install_router()

        def sink(text: str) -> None:
            job.write(text)
            self._flush(job)

        job.status, job.started_at = "RUNNING", utcnow().isoformat()
        self.db.start_job_run(job.id)
        _local.sink = sink
        exit_code: int | None = None
        try:
            exit_code = int(action(job) or 0)
        except SystemExit as exc:  # argparse-style exits inside a command
            exit_code = exc.code if isinstance(exc.code, int) else 1
        except BaseException as exc:  # noqa: BLE001 - every failure is reported, never raised
            job.error = redact(f"{type(exc).__name__}: {exc}")
            job.write(f"\n[ERROR] {job.error}\n")
            logger.exception("Admin job %s (%s) crashed.", job.id, job.kind)
        finally:
            _local.sink = None
        job.exit_code = exit_code
        job.status = EXIT_STATUS.get(exit_code, "FAILED") if job.error is None else "FAILED"
        job.finished_at = utcnow().isoformat()
        job.write(f"\n[{job.status}] exit code {exit_code if exit_code is not None else '-'}\n")
        self.db.finish_job_run(job.id, status=job.status, exit_code=exit_code, log_text=job.text(), error=job.error)
        with self._lock:
            self._live.pop(job.id, None)

    # -- reading ------------------------------------------------------------------ #
    def get(self, job_id: str, since: int = 0) -> dict[str, Any] | None:
        with self._lock:
            job = self._live.get(job_id)
        row = self.db.get_job_run(job_id)
        if row is None:
            return None
        if job is not None:  # running in this process: fresher than the database
            row = {**row, "status": job.status, "log_text": job.text(), "started_at": row.get("started_at")}
        return present_job(row, since=since)

    def wait(self, job_id: str, timeout: float = 30.0) -> dict[str, Any] | None:
        """Block until a job has finished (tests, synchronous callers)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get(job_id)
            if job is None or job["status"] in ("SUCCEEDED", "FAILED"):
                return job
            time.sleep(0.05)
        return self.get(job_id)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def present_job(row: dict[str, Any], *, since: int = 0) -> dict[str, Any]:
    """A ``job_runs`` row as the API returns it; ``log`` holds the lines from index ``since`` on."""
    lines = (row.get("log_text") or "").splitlines()
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "exit_code": row.get("exit_code"),
        "error": row.get("error"),
        "params": json.loads(row.get("params_json") or "{}"),
        "command": " ".join(["python main.py", *json.loads(row.get("argv_json") or "[]")]),
        "created_at": row.get("created_at"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "log": lines[since:] if "log_text" in row else [],
        "log_total": len(lines),
        "done": row["status"] in ("SUCCEEDED", "FAILED"),
    }
