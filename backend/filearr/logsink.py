"""Fail-open DB log sink backing the console Logs panel.

App and worker are separate containers, so no single process's stdout shows
the whole system — both install this sink, which persists selected log
records to the shared ``app_logs`` table (Postgres source of truth; the Jobs
page tails it). Policy: ``filearr.*`` loggers at ``log_db_level`` (INFO by
default — that's the "activity" stream), every other logger at WARNING+,
``uvicorn.access`` never (request lines would swamp the table), DEBUG never.

Strictly best-effort, mirroring :mod:`filearr.joberrors`: records flow
through a bounded in-memory queue to a daemon thread that batch-inserts on
its OWN psycopg connection. A full queue or a broken DB drops records —
logging must never block or raise into application code. Retention is the
``purge_app_logs`` maintenance task, not the sink.
"""

from __future__ import annotations

import logging
import queue
import threading
import traceback as tb_mod
from datetime import UTC, datetime

_MESSAGE_CAP = 2_000
_EXC_CAP = 8_000
_QUEUE_CAP = 5_000
_BATCH_CAP = 200
_FAIL_BACKOFF_S = 15.0

# Loggers whose records must never enter the sink: our own (a sink failure
# logging into the sink would loop) and per-request access noise.
_EXCLUDED = ("filearr.logsink", "uvicorn.access")

_INSERT = (
    "INSERT INTO app_logs (ts, source, level, levelno, logger, message, exc) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s)"
)

log = logging.getLogger(__name__)

_install_lock = threading.Lock()
_installed: DbLogSink | None = None


def _conninfo() -> str:
    """The raw-psycopg conninfo for the sink's private connection (the
    SQLAlchemy URL minus its ``+psycopg`` driver marker)."""
    from filearr.config import get_settings

    return get_settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)


class DbLogHandler(logging.Handler):
    """Queue-producer side: policy-filter the record, shape a row, drop on a
    full queue. Never raises (handler contract + fail-open design)."""

    def __init__(self, sink: DbLogSink) -> None:
        super().__init__(level=logging.INFO)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            if not self._sink.should_store(record):
                return
            self._sink.offer(self._sink.row_for(record))
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


class DbLogSink:
    """The queue + flusher-thread pair. One per process (module singleton via
    :func:`install`); tests construct their own with a private conninfo."""

    def __init__(
        self,
        source: str,
        conninfo: str,
        *,
        filearr_level: int = logging.INFO,
        flush_interval: float = 2.0,
        fail_backoff: float = _FAIL_BACKOFF_S,
    ) -> None:
        self.source = source
        self._conninfo = conninfo
        self.filearr_level = filearr_level
        self.flush_interval = flush_interval
        self.fail_backoff = fail_backoff
        self._q: queue.Queue[tuple] = queue.Queue(maxsize=_QUEUE_CAP)
        self._conn = None
        self._stop = threading.Event()
        self.handler = DbLogHandler(self)
        self._thread = threading.Thread(
            target=self._run, name="filearr-logsink", daemon=True
        )

    # -- producer side ------------------------------------------------------

    def should_store(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.INFO:
            return False
        name = record.name
        if name in _EXCLUDED or name.startswith("filearr.logsink"):
            return False
        if name == "filearr" or name.startswith("filearr."):
            return record.levelno >= self.filearr_level
        return record.levelno >= logging.WARNING

    def row_for(self, record: logging.LogRecord) -> tuple:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - malformed %-args must not lose the record
            message = str(record.msg)
        exc = None
        if record.exc_info and record.exc_info[0] is not None:
            exc = "".join(tb_mod.format_exception(*record.exc_info))[:_EXC_CAP]
        return (
            datetime.fromtimestamp(record.created, tz=UTC),
            self.source,
            record.levelname,
            record.levelno,
            record.name,
            message[:_MESSAGE_CAP],
            exc,
        )

    def offer(self, row: tuple) -> None:
        try:
            self._q.put_nowait(row)
        except queue.Full:
            pass  # fail-open: a storm drops records, never blocks callers

    # -- flusher side -------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Test hook: stop the thread (daemon threads need no stop in prod)."""
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                first = self._q.get(timeout=self.flush_interval)
            except queue.Empty:
                continue
            batch = [first]
            while len(batch) < _BATCH_CAP:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            if not self._write(batch):
                # Dropped batch; give a broken DB room to recover instead of
                # spinning. Producers keep filling (then dropping at) the queue.
                self._stop.wait(self.fail_backoff)

    def _write(self, batch: list[tuple]) -> bool:
        import psycopg

        try:
            if self._conn is None or self._conn.closed:
                self._conn = psycopg.connect(self._conninfo, autocommit=True)
            with self._conn.cursor() as cur:
                cur.executemany(_INSERT, batch)
            return True
        except Exception:  # noqa: BLE001 - fail-open: drop, backoff, retry later
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None
            # Own logger is excluded from the sink — this reaches stdout only.
            log.warning("app_logs flush failed; dropped %d records", len(batch))
            return False


def install(source: str) -> DbLogSink | None:
    """Attach the process-wide sink to the root logger (first caller wins —
    the app process imports :mod:`filearr.worker` for deferring, so its later
    ``install('worker')`` must not relabel). No-op when disabled."""
    global _installed
    from filearr.config import get_settings

    settings = get_settings()
    if not settings.log_db_enabled:
        return None
    with _install_lock:
        if _installed is not None:
            return _installed
        level = getattr(logging, settings.log_db_level.upper(), logging.INFO)
        sink = DbLogSink(source, _conninfo(), filearr_level=level)
        # filearr.* INFO must survive logger-level filtering to reach root
        # handlers; only ever widen — never silence an operator's override.
        filearr_logger = logging.getLogger("filearr")
        if filearr_logger.getEffectiveLevel() > level:
            filearr_logger.setLevel(level)
        logging.getLogger().addHandler(sink.handler)
        sink.start()
        _installed = sink
        return sink
