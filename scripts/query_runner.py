"""Concurrent SQL execution helpers for the DB2 spatial bug reproduction."""

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

import ibm_db


class ConnectionError(RuntimeError):
    """Raised when the connection pool fails to create a new connection."""


@dataclass
class HammerResult:
    """Summary information returned at the end of a hammer run."""

    iterations: int
    duration: float
    failure: Optional[BaseException]


class ConnectionPool:
    """Lightweight connection pool backed by ibm_db connections."""

    def __init__(self, dsn: str, size: int, logger: Optional[logging.Logger] = None) -> None:
        """Initialise a pool with ``size`` eager connections to ``dsn``."""
        if size <= 0:
            raise ValueError("Pool size must be positive")
        self.dsn = dsn
        self.size = size
        self.logger = logger or logging.getLogger(__name__)
        self._pool: "queue.LifoQueue[ibm_db.IBM_DBConnection]" = queue.LifoQueue(maxsize=size)
        self._lock = threading.Lock()
        self._populate()

    def _populate(self) -> None:
        """Fill the pool by creating ``size`` connections upfront."""
        for _ in range(self.size):
            conn = self._create_connection()
            self._pool.put(conn)

    def _create_connection(self):
        """Create a single autocommit connection for pooling."""
        try:
            conn = ibm_db.connect(self.dsn, "", "")
        except Exception as exc:  # pragma: no cover - ibm_db raises extension-specific errors
            self.logger.error("Failed to create DB2 connection: %s", exc)
            raise ConnectionError("Unable to establish DB2 connection") from exc
        ibm_db.autocommit(conn, ibm_db.SQL_AUTOCOMMIT_ON)
        return conn

    def acquire(self, timeout: Optional[float] = None):
        """Retrieve a connection, blocking up to ``timeout`` seconds if necessary."""
        return self._pool.get(timeout=timeout)

    def release(self, conn) -> None:
        """Return a healthy connection to the pool."""
        self._pool.put(conn)

    def invalidate(self, conn) -> None:
        """Close a broken connection and immediately replace it."""
        try:
            ibm_db.close(conn)
        except Exception:
            pass
        replacement = self._create_connection()
        self._pool.put(replacement)

    def close(self) -> None:
        """Drain the pool and close every connection (used during shutdown)."""
        while True:
            try:
                conn = self._pool.get_nowait()
            except queue.Empty:
                break
            try:
                ibm_db.close(conn)
            except Exception:
                pass


class QueryHammer:
    """Execute the target SQL concurrently until failure or the limits are reached."""

    def __init__(
        self,
        pool: ConnectionPool,
        sql: str,
        *,
        threads: int,
        max_iterations: Optional[int] = None,
        max_seconds: Optional[int] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """Configure worker threads that hammer the supplied SQL statement."""
        if threads <= 0:
            raise ValueError("Thread count must be positive")
        self.pool = pool
        self.sql = sql
        self.threads = threads
        self.max_iterations = max_iterations
        self.max_seconds = max_seconds
        self.logger = logger or logging.getLogger(__name__)
        self._total_iterations = 0
        self._failure: Optional[BaseException] = None
        self._lock = threading.Lock()

    def run(self) -> HammerResult:
        """Execute the workload and return aggregate statistics plus the first failure."""
        start = time.time()
        stop_event = threading.Event()
        threads = []

        def worker(worker_id: int) -> None:
            local_iterations = 0
            while not stop_event.is_set():
                if self._time_exceeded(start):
                    stop_event.set()
                    break
                if self._iterations_exceeded():
                    stop_event.set()
                    break
                try:
                    conn = self.pool.acquire(timeout=1)
                except queue.Empty:
                    continue
                try:
                    stmt = ibm_db.exec_immediate(conn, self.sql)
                    try:
                        ibm_db.fetch_tuple(stmt)
                    finally:
                        ibm_db.free_stmt(stmt)
                    local_iterations += 1
                    total = self._increment_iterations()
                    if local_iterations % 100 == 0:
                        self.logger.debug("Worker %s completed %s iterations (total %s)", worker_id, local_iterations, total)
                except Exception as exc:  # pragma: no cover - failure path depends on DB2 bug
                    self.logger.error("Worker %s encountered failure after %s iterations: %s", worker_id, local_iterations, exc)
                    self._record_failure(exc)
                    stop_event.set()
                    try:
                        self.pool.invalidate(conn)
                        conn = None
                    except Exception:
                        conn = None
                finally:
                    if conn:
                        self.pool.release(conn)

        for idx in range(self.threads):
            thread = threading.Thread(target=worker, args=(idx,), name=f"query-hammer-{idx}", daemon=True)
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        duration = time.time() - start
        return HammerResult(iterations=self._total_iterations, duration=duration, failure=self._failure)

    def _increment_iterations(self) -> int:
        """Atomically increment the global iteration counter."""
        with self._lock:
            self._total_iterations += 1
            if self.max_iterations and self._total_iterations >= self.max_iterations:
                # Signal the stop condition by returning the limit
                pass
            return self._total_iterations

    def _record_failure(self, exc: BaseException) -> None:
        """Persist the first failure observed by any worker thread."""
        with self._lock:
            if not self._failure:
                self._failure = exc

    def _iterations_exceeded(self) -> bool:
        """Return True once the configured iteration limit has been met."""
        if not self.max_iterations:
            return False
        with self._lock:
            return self._total_iterations >= self.max_iterations

    def _time_exceeded(self, start: float) -> bool:
        """Return True once the configured time budget has been used."""
        if not self.max_seconds:
            return False
        return time.time() - start >= self.max_seconds
