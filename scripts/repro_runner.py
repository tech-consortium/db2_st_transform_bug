"""Entry point used by ``make test`` to reproduce the spatial failure."""

import argparse
import logging
import shutil
import sys
import time
from typing import Optional

from .db2_container import DB2ContainerConfig, DB2ContainerManager, CommandError
from .query_runner import ConnectionPool, QueryHammer

TARGET_SQL = """
SELECT CASE
           WHEN DB2GSE.ST_IsEmpty(DB2GSE.ST_Point(CAST(-98.71447796 AS DOUBLE),
                                                  CAST(29.48604692 AS DOUBLE), CAST(4269 AS INTEGER)))=1
                OR DB2GSE.ST_IsValid(DB2GSE.ST_Point(CAST(-98.71447796 AS DOUBLE),
                                                     CAST(29.48604692 AS DOUBLE), CAST(4269 AS INTEGER)))=0
           THEN NULL
           ELSE CAST(db2gse.ST_AsText(db2gse.ST_Transform(DB2GSE.ST_Point(CAST(-98.71447796 AS DOUBLE),
                                                                          CAST(29.48604692 AS DOUBLE), CAST(4269 AS INTEGER)), CAST(4326 AS INTEGER))) AS CLOB(2097152))
      END
FROM SYSIBM.SYSDUMMY1
""".strip()


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments for the reproduction harness."""
    parser = argparse.ArgumentParser(
        description="Reproduce DB2 ST_Transform concurrency bug using a dockerised DB2 instance."
    )
    parser.add_argument("--threads", type=int, default=8, help="Number of concurrent worker threads (default: 8)")
    parser.add_argument(
        "--pool-size",
        type=int,
        default=16,
        help="Number of connections to keep in the pool (default: 16)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=300,
        help="Maximum duration to run the hammer in seconds. Set to 0 to run until failure (default: 300)",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=0,
        help="Optional hard limit for total iterations. 0 disables the limit (default: 0)",
    )
    parser.add_argument(
        "--container-name",
        default="db2-st-transform",
        help="Docker container name to use (default: db2-st-transform)",
    )
    parser.add_argument(
        "--image",
        default="icr.io/db2_community/db2",
        help="DB2 docker image to run (default: icr.io/db2_community/db2)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=50000,
        help="Host port to bind for DB2 (default: 50000)",
    )
    parser.add_argument(
        "--password",
        default="Password123!",
        help="Password to configure for the DB2 instance user (default: Password123!)",
    )
    parser.add_argument(
        "--keep-container",
        action="store_true",
        help="Do not destroy the DB2 container when the run finishes.",
    )
    parser.add_argument(
        "--reuse-container",
        action="store_true",
        help="Reuse an already running DB2 container instead of starting a new one.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Set the logging verbosity (default: INFO)",
    )
    return parser.parse_args(argv)


def ensure_docker_available() -> None:
    """Raise ``CommandError`` unless the Docker CLI is discoverable."""
    if not shutil.which("docker"):
        raise CommandError("Docker CLI is required but was not found on PATH")


def main(argv: Optional[list[str]] = None) -> int:
    """Orchestrate the container lifecycle and run the threaded workload."""
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logger = logging.getLogger("repro")

    try:
        ensure_docker_available()
    except CommandError as exc:
        logger.error("%s", exc)
        return 2

    config = DB2ContainerConfig(
        name=args.container_name,
        image=args.image,
        port=args.port,
        password=args.password,
    )

    manager = DB2ContainerManager(config=config, logger=logger)
    pool: Optional[ConnectionPool] = None
    started_container = False
    max_iterations = args.max_iterations if args.max_iterations > 0 else None
    max_seconds = args.duration if args.duration > 0 else None

    try:
        if args.reuse_container:
            if not manager.exists():
                logger.error(
                    "Requested to reuse container '%s' but it does not exist. Rerun without --reuse-container.",
                    config.name,
                )
                return 1
            logger.info("Reusing existing DB2 container '%s'", config.name)
        else:
            manager.start()
            started_container = True
            manager.wait_for_setup()
            # db2sampl occasionally fails if the instance is not ready; retry a few times.
            _retry(manager.create_sample_database, attempts=3, delay=30, logger=logger)
            manager.spatial_enable()

        dsn = manager.connection_dsn()
        logger.info("Connecting to DB2 using DSN: %s", dsn.replace(config.password, "********"))
        pool = ConnectionPool(dsn, args.pool_size, logger=logger)

        hammer = QueryHammer(
            pool,
            TARGET_SQL,
            threads=args.threads,
            max_iterations=max_iterations,
            max_seconds=max_seconds,
            logger=logger,
        )

        logger.info(
            "Starting query hammer with %s threads, pool size %s, duration %s, max iterations %s",
            args.threads,
            args.pool_size,
            max_seconds if max_seconds else "∞",
            max_iterations if max_iterations else "∞",
        )
        result = hammer.run()
        logger.info(
            "Hammer finished after %.2fs and %s iterations",
            result.duration,
            result.iterations,
        )
        if result.failure:
            logger.error("Encountered failure during run: %s", result.failure)
            return 1
        if max_seconds:
            logger.warning(
                "Reached configured duration (%ss) without observing a failure. "
                "Consider increasing --duration or setting it to 0 to run until failure.",
                max_seconds,
            )
        if max_iterations:
            logger.warning(
                "Reached configured iteration limit (%s) without observing a failure.",
                max_iterations,
            )
        return 0
    except CommandError as exc:
        logger.error("Command error: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return 130
    finally:
        if pool:
            pool.close()
        if started_container and not args.keep_container:
            try:
                manager.stop()
            except CommandError as exc:
                logger.error("Failed to stop container cleanly: %s", exc)


def _retry(func, *, attempts: int, delay: int, logger: logging.Logger) -> None:
    """Retry helper used for db2sampl, which occasionally fails while DB2 boots."""
    errors = []
    for attempt in range(1, attempts + 1):
        try:
            func()
            return
        except CommandError as exc:
            errors.append(exc)
            if attempt == attempts:
                raise
            logger.warning("Attempt %s/%s failed (%s). Retrying in %ss...", attempt, attempts, exc, delay)
            time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
