"""Capture db2trc output while stressing DB2GSE.ST_Transform until it fails."""

from __future__ import annotations

import argparse
import logging
import os
import shlex
import subprocess
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Optional

import ibm_db

from .db2_container import DB2ContainerConfig, DB2ContainerManager
from .repro_runner import TARGET_SQL


def _connect(dsn: str, logger: logging.Logger) -> ibm_db.IBM_DBConnection:
    """Create a single autocommit connection."""
    try:
        conn = ibm_db.connect(dsn, "", "")
    except Exception as exc:  # pragma: no cover - depends on runtime env
        logger.error("Unable to connect to Db2: %s", exc)
        raise
    ibm_db.autocommit(conn, ibm_db.SQL_AUTOCOMMIT_ON)
    return conn


def _run_in_container(manager: DB2ContainerManager, command: str, *, logger: logging.Logger) -> subprocess.CompletedProcess:
    """Execute a shell command inside the container as the instance user."""
    logger.debug("Running in container: %s", command)
    return subprocess.run(
        [
            "docker",
            "exec",
            manager.config.name,
            "su",
            "-",
            manager.config.instance,
            "-c",
            command,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _copy_from_container(
    manager: DB2ContainerManager,
    source: str,
    destination: Path,
    *,
    logger: logging.Logger,
    required: bool = True,
) -> bool:
    """Copy a file or directory from the container."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "docker",
                "cp",
                f"{manager.config.name}:{source}",
                str(destination),
            ],
            check=True,
            text=True,
        )
        logger.debug("Copied %s -> %s", source, destination)
        return True
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() if exc.stderr else str(exc)
        if required:
            logger.error("Failed to copy %s: %s", source, message)
            raise
        logger.warning("Skipping copy of %s (%s)", source, message)
        return False


def _cleanup_container_path(manager: DB2ContainerManager, path: str, *, logger: logging.Logger) -> None:
    """Remove temporary files/directories inside the container."""
    try:
        _run_in_container(manager, f"rm -rf {shlex.quote(path)}", logger=logger)
    except subprocess.CalledProcessError as exc:  # pragma: no cover - best effort
        logger.warning("Failed to clean up %s inside container: %s", path, exc)


def _fodc_parent(manager: DB2ContainerManager) -> str:
    """Return the db2dump directory path."""
    return f"/database/config/{manager.config.instance}/sqllib/db2dump"


def _latest_fodc_directory(manager: DB2ContainerManager) -> str:
    """Return the newest FODC_* directory name or empty string."""
    parent = _fodc_parent(manager)
    result = subprocess.run(
        [
            "docker",
            "exec",
            manager.config.name,
            "su",
            "-",
            manager.config.instance,
            "-c",
            f"cd {shlex.quote(parent)} && ls -1dt FODC_* 2>/dev/null | head -n 1",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _fodc_directory_size(manager: DB2ContainerManager, fodc_dir: str) -> Optional[int]:
    """Return the total size in bytes of the specified FODC directory."""
    parent = _fodc_parent(manager)
    command = (
        f"cd {shlex.quote(parent)} && "
        f"du -sb {shlex.quote(fodc_dir)} 2>/dev/null | awk '{{print $1}}'"
    )
    result = subprocess.run(
        [
            "docker",
            "exec",
            manager.config.name,
            "su",
            "-",
            manager.config.instance,
            "-c",
            command,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    try:
        return int(output)
    except ValueError:
        return None


def _wait_for_fodc_stable(
    manager: DB2ContainerManager,
    fodc_dir: str,
    *,
    quiesce_seconds: int,
    timeout_seconds: int,
    logger: logging.Logger,
) -> None:
    """Wait until the FODC directory size stops changing for quiesce_seconds."""
    if not fodc_dir:
        return
    logger.info(
        "Waiting for FODC directory %s to stabilize (%ss quiet window, %ss timeout)...",
        fodc_dir,
        quiesce_seconds,
        timeout_seconds,
    )
    deadline = time.time() + max(timeout_seconds, quiesce_seconds)
    last_size = None
    stable_since = None
    while time.time() < deadline:
        size = _fodc_directory_size(manager, fodc_dir)
        if size is None:
            logger.debug("FODC directory %s not ready yet; retrying...", fodc_dir)
            time.sleep(2)
            continue
        logger.debug("FODC %s current size: %s bytes", fodc_dir, size)
        if size != last_size:
            last_size = size
            stable_since = time.time()
        elif stable_since and (time.time() - stable_since) >= quiesce_seconds:
            logger.info("FODC directory %s stable for %ss", fodc_dir, quiesce_seconds)
            return
        time.sleep(2)
    logger.warning("FODC directory %s did not stabilize before timeout; continuing anyway.", fodc_dir)


def _container_epoch(manager: DB2ContainerManager, logger: logging.Logger) -> int:
    """Return the container's current epoch time in seconds."""
    result = subprocess.run(
        [
            "docker",
            "exec",
            manager.config.name,
            "su",
            "-",
            manager.config.instance,
            "-c",
            "date +%s",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning("Failed to read container time; defaulting to host time.")
        return int(time.time())
    try:
        return int(result.stdout.strip())
    except ValueError:
        logger.warning("Unexpected container time output %r; defaulting to host time.", result.stdout.strip())
        return int(time.time())


def _copy_db2dump_artifacts(
    manager: DB2ContainerManager,
    *,
    since_epoch: int,
    output_dir: Path,
    logger: logging.Logger,
) -> None:
    """Copy recent db2dump root artifacts (core/bin/stack files) to the host."""
    parent = _fodc_parent(manager)
    find_cmd = (
        f"cd {shlex.quote(parent)} && "
        "find . -maxdepth 1 -type f "
        "\\( -name '*.dump.bin' -o -name '*.stack.txt' -o -name '*.app_stack.txt' "
        "-o -name '*.nonEDU.app_stack.txt' \\) "
        "-printf '%T@ %P\\n'"
    )
    result = subprocess.run(
        [
            "docker",
            "exec",
            manager.config.name,
            "su",
            "-",
            manager.config.instance,
            "-c",
            find_cmd,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.debug("No db2dump root artifacts discovered (find exit %s)", result.returncode)
        return
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    candidates: list[str] = []
    for line in lines:
        try:
            timestamp_str, relpath = line.split(" ", 1)
            timestamp = float(timestamp_str)
        except ValueError:
            continue
        if timestamp >= since_epoch:
            candidates.append(relpath)
    if not candidates:
        logger.debug("No db2dump root artifacts newer than %s", since_epoch)
        return
    root_output = output_dir / "db2dump_root"
    copied = False
    for relpath in candidates:
        source = f"{parent}/{relpath}"
        destination = root_output / relpath
        if _copy_from_container(manager, source, destination, logger=logger, required=False):
            copied = True
    if copied:
        logger.info("Copied db2dump root artifacts to %s", root_output)


def _worker_loop(
    conn,
    label: str,
    stop_event: threading.Event,
    failure: dict,
    failure_lock: threading.Lock,
    logger: logging.Logger,
) -> None:
    """Execute the target SQL in a loop until failure or stop."""
    iterations = 0
    while not stop_event.is_set():
        try:
            stmt = ibm_db.exec_immediate(conn, TARGET_SQL)
            try:
                ibm_db.fetch_tuple(stmt)
            finally:
                ibm_db.free_stmt(stmt)
            iterations += 1
        except Exception as exc:  # pragma: no cover - depends on failure mode
            logger.error("Worker %s observed failure after %s iterations: %s", label, iterations, exc)
            with failure_lock:
                if "exception" not in failure:
                    failure["exception"] = exc
                    failure["worker"] = label
            stop_event.set()
            break
    logger.debug("Worker %s exiting after %s iterations", label, iterations)


def capture_trace(args: argparse.Namespace) -> int:
    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="%(asctime)s [%(levelname)s] %(message)s")
    logger = logging.getLogger("trace")

    config = DB2ContainerConfig(
        name=args.container_name,
        port=args.port,
        password=args.password,
        sample_db=args.database,
        instance=args.instance,
    )
    manager = DB2ContainerManager(config=config, logger=logger)

    if not manager.is_running():
        logger.error("Container %s is not running; start it before capturing a trace.", config.name)
        return 2

    dsn = manager.connection_dsn()
    logger.info("Connecting to %s with DSN: %s", config.sample_db, dsn)

    container_trace_dir = args.container_trace_dir or f"/tmp/db2trc_{int(time.time())}"
    dump_path = f"{container_trace_dir}/db2trc.dmp"
    flow_path = f"{container_trace_dir}/db2trc.flw"
    fmt_path = f"{container_trace_dir}/db2trc.fmt"
    trace_started = False
    container_epoch_start = int(time.time())
    latest_known_fodc = _latest_fodc_directory(manager)

    connections = []
    stop_event = threading.Event()
    failure: dict = {}
    failure_lock = threading.Lock()
    detected_fodc: Optional[str] = None

    try:
        for idx in range(args.threads):
            try:
                conn = _connect(dsn, logger)
            except Exception:
                stop_event.set()
                raise
            connections.append(conn)

        container_epoch_start = _container_epoch(manager, logger=logger)
        latest_known_fodc = _latest_fodc_directory(manager)

        try:
            _run_in_container(manager, f"mkdir -p {shlex.quote(container_trace_dir)}", logger=logger)
            _run_in_container(
                manager,
                f"cd {shlex.quote(container_trace_dir)} && db2trc on -t -f {shlex.quote(dump_path)}",
                logger=logger,
            )
            trace_started = True
        except subprocess.CalledProcessError as exc:
            logger.error("Failed to start db2trc: %s", exc.stderr.strip() if exc.stderr else exc)
            return 3

        threads = []
        for idx, conn in enumerate(connections):
            label = f"worker-{idx}"
            thread = threading.Thread(
                target=_worker_loop,
                args=(conn, label, stop_event, failure, failure_lock, logger),
                daemon=True,
            )
            threads.append(thread)
            thread.start()

        logger.info("Executing workload with %s connections...", len(connections))

        start_time = time.time()
        timed_out = False
        last_poll = 0.0
        try:
            while not stop_event.is_set():
                if args.max_seconds and (time.time() - start_time) >= args.max_seconds:
                    timed_out = True
                    logger.warning("Reached max_seconds (%s); stopping workload.", args.max_seconds)
                    break
                if args.fodc_poll_seconds and (time.time() - last_poll) >= args.fodc_poll_seconds:
                    last_poll = time.time()
                    current_fodc = _latest_fodc_directory(manager)
                    if current_fodc and current_fodc != latest_known_fodc:
                        latest_known_fodc = current_fodc
                        detected_fodc = current_fodc
                        logger.info("Detected new FODC directory: %s", current_fodc)
                        with failure_lock:
                            if "exception" not in failure:
                                failure["exception"] = RuntimeError(f"Detected FODC directory {current_fodc}")
                                failure["worker"] = "monitor"
                                failure["fodc"] = current_fodc
                        stop_event.set()
                        break
                time.sleep(0.5)
        finally:
            stop_event.set()

        for thread in threads:
            thread.join()

        elapsed = time.time() - start_time
        if "exception" in failure:
            logger.info(
                "Captured failure from %s after %.2fs: %s",
                failure.get("worker", "unknown"),
                elapsed,
                failure["exception"],
            )
        elif detected_fodc:
            logger.info("Workload stopped after detecting FODC %s (%.2fs)", detected_fodc, elapsed)
        elif timed_out:
            logger.warning("No failure observed before timeout (%.2fs).", elapsed)
        else:
            logger.warning("No failure observed during trace window (%.2fs).", elapsed)

        fodc_dir = failure.get("fodc") or detected_fodc
        if fodc_dir:
            _wait_for_fodc_stable(
                manager,
                fodc_dir,
                quiesce_seconds=args.fodc_quiesce_seconds,
                timeout_seconds=args.fodc_wait_timeout,
                logger=logger,
            )

    finally:
        stop_event.set()
        for conn in connections:
            with suppress(Exception):
                ibm_db.close(conn)

        if trace_started:
            try:
                _run_in_container(manager, "db2trc off", logger=logger)
            except subprocess.CalledProcessError as exc:  # pragma: no cover - best effort
                logger.warning("db2trc off reported an error: %s", exc.stderr.strip() if exc.stderr else exc)
            try:
                _run_in_container(
                    manager,
                    f"cd {shlex.quote(container_trace_dir)} && db2trc flow -t -wc {shlex.quote(dump_path)} {shlex.quote(flow_path)}",
                    logger=logger,
                )
                _run_in_container(
                    manager,
                    f"cd {shlex.quote(container_trace_dir)} && db2trc fmt {shlex.quote(dump_path)} {shlex.quote(fmt_path)}",
                    logger=logger,
                )
            except subprocess.CalledProcessError as exc:
                logger.error("Failed to format trace data: %s", exc.stderr.strip() if exc.stderr else exc)

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    copied_any = False
    try:
        copied_any |= _copy_from_container(manager, flow_path, output_dir / "db2trc.flw", logger=logger, required=False)
        copied_any |= _copy_from_container(manager, fmt_path, output_dir / "db2trc.fmt", logger=logger, required=False)
        if copied_any:
            logger.info("Trace files copied to %s", output_dir)
        else:
            logger.warning("No trace artifacts were copied; check container logs for db2trc errors.")
        _copy_db2dump_artifacts(
            manager,
            since_epoch=container_epoch_start,
            output_dir=output_dir,
            logger=logger,
        )
    finally:
        if not args.keep_container_trace:
            _cleanup_container_path(manager, container_trace_dir, logger=logger)

    if "exception" in failure or detected_fodc:
        return 0
    return 4


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the ST_Transform workload under db2trc and extract trace files.")
    parser.add_argument("--container-name", default=os.environ.get("DB2_CONTAINER", "db2-st-transform"), help="Docker container name")
    parser.add_argument("--instance", default=os.environ.get("DB2_INSTANCE", "db2inst1"), help="Db2 instance user inside the container")
    parser.add_argument("--port", type=int, default=int(os.environ.get("DB2_PORT", "50000")), help="Host port mapped to Db2 (default: 50000)")
    parser.add_argument("--password", default=os.environ.get("DB2_PASSWORD", "Password123!"), help="Password for the Db2 instance user")
    parser.add_argument("--database", default=os.environ.get("DB2_DATABASE", "SAMPLE"), help="Database name to connect to")
    parser.add_argument(
        "--threads",
        type=int,
        default=int(os.environ.get("TRACE_THREADS", "4")),
        help="Number of workload connections to run (must be >=1)",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Host directory where db2trc.flw and db2trc.fmt will be stored",
    )
    parser.add_argument(
        "--container-trace-dir",
        help="Optional directory inside the container for db2trc artifacts (default: /tmp/db2trc_<timestamp>)",
    )
    parser.add_argument(
        "--keep-container-trace",
        action="store_true",
        help="Retain db2trc artifacts inside the container (useful for debugging).",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("TRACE_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    parser.add_argument(
        "--max-seconds",
        type=int,
        default=int(os.environ.get("TRACE_MAX_SECONDS", "120")),
        help="Maximum time to run the workload before stopping (default: 120)",
    )
    parser.add_argument(
        "--fodc-poll-seconds",
        type=int,
        default=int(os.environ.get("TRACE_FODC_POLL", "5")),
        help="Interval between checks for new FODC directories (default: 5)",
    )
    parser.add_argument(
        "--fodc-quiesce-seconds",
        type=int,
        default=int(os.environ.get("TRACE_FODC_QUIESCE", "10")),
        help="Seconds a detected FODC directory must remain unchanged before proceeding (default: 10)",
    )
    parser.add_argument(
        "--fodc-wait-timeout",
        type=int,
        default=int(os.environ.get("TRACE_FODC_TIMEOUT", "300")),
        help="Maximum time to wait for FODC stabilization after detection (default: 300)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    if args.threads < 1:
        print("threads must be >= 1", flush=True)
        return 1
    try:
        return capture_trace(args)
    except KeyboardInterrupt:  # pragma: no cover - interactive abort
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
