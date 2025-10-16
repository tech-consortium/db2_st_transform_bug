"""Container orchestration helpers for the DB2 ST_Transform reproduction suite."""

import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Iterable, Optional


class CommandError(RuntimeError):
    """Raised when a subprocess returns a non-zero exit code."""


def _run(
    command: Iterable[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    text: bool = True,
    timeout: Optional[int] = None,
) -> subprocess.CompletedProcess:
    """Execute a shell command and return the completed process."""
    return subprocess.run(
        list(command),
        check=check,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
    )


@dataclass
class DB2ContainerConfig:
    """Parameter bundle used to start and interact with the DB2 container."""

    name: str = "db2-st-transform"
    image: str = "icr.io/db2_community/db2"
    port: int = 50000
    instance: str = "db2inst1"
    password: str = "Password123!"
    sample_db: str = "SAMPLE"
    license_env: str = "accept"
    startup_timeout: int = 900


class DB2ContainerManager:
    """Utility to manage the lifecycle of the DB2 docker container."""

    def __init__(self, config: Optional[DB2ContainerConfig] = None, logger: Optional[logging.Logger] = None) -> None:
        self.config = config if config else DB2ContainerConfig()
        self.logger = logger or logging.getLogger(__name__)
        self._started = False

    def start(self) -> None:
        """Start a fresh DB2 container, replacing any previous run."""
        self.logger.info("Starting DB2 container %s", self.config.name)
        self.stop()
        command = ["docker", "run", "-d"]
        if sys.platform == "darwin":
            command.extend(["--platform", "linux/amd64"])
            self.logger.info("Detected macOS host; forcing Docker platform linux/amd64")
        command.extend(
            [
                "--name",
                self.config.name,
                "--privileged",
                "-p",
                f"{self.config.port}:50000",
                "-e",
                f"LICENSE={self.config.license_env}",
                "-e",
                f"DB2INSTANCE={self.config.instance}",
                "-e",
                f"DB2INST1_PASSWORD={self.config.password}",
                "-e",
                f"DBNAME={self.config.sample_db}",
                self.config.image,
            ]
        )
        self.logger.info("Launching Db2 image %s", self.config.image)
        try:
            result = _run(command, capture_output=True)
            self.logger.debug("docker run output: %s", result.stdout.strip())
            self._started = True
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else "no stderr"
            lower = stderr.lower()
            if "not found" in lower or "unauthorized" in lower:
                self.logger.error("Docker could not pull %s. Verify the image name or try a manual `docker pull`.", self.config.image)
            self.logger.error("Failed to launch DB2 container: %s", stderr)
            raise CommandError(f"docker run failed: {stderr}") from exc

    def wait_for_setup(self) -> None:
        """Block until the DB2 entrypoint reports `Setup has completed`."""
        deadline = time.time() + self.config.startup_timeout
        marker = "Setup has completed"
        self.logger.info("Waiting for DB2 setup to finish (timeout %ss)", self.config.startup_timeout)
        while time.time() < deadline:
            if not self.is_running():
                raise CommandError("DB2 container stopped unexpectedly during startup")
            try:
                logs = _run(
                    ["docker", "logs", "--tail", "200", self.config.name],
                    capture_output=True,
                ).stdout
            except subprocess.CalledProcessError:
                logs = ""
            if marker in logs:
                self.logger.info("DB2 container reported setup complete")
                return
            time.sleep(10)
        raise TimeoutError("DB2 container did not finish setup within the allotted time")

    def is_running(self) -> bool:
        """Return True when the container process is alive."""
        try:
            result = _run(
                ["docker", "inspect", "-f", "{{.State.Running}}", self.config.name],
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            return False
        return result.stdout.strip() == "true"

    def create_sample_database(self) -> None:
        """Create the SAMPLE database using db2sampl."""
        self.logger.info("Creating SAMPLE database via db2sampl")
        command = [
            "docker",
            "exec",
            self.config.name,
            "su",
            "-",
            self.config.instance,
            "-c",
            "db2sampl -force -name SAMPLE",
        ]
        try:
            _run(command, capture_output=True, timeout=600)
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else ""
            # db2sampl returns code 2 when SAMPLE already exists; treat as success.
            if "SQL1005N" in stderr or "already exists" in stderr:
                self.logger.debug("db2sampl reported existing database, continuing")
            else:
                self.logger.error("db2sampl failed: %s", stderr)
                raise CommandError(f"db2sampl failed: {stderr}") from exc

    def spatial_enable(self) -> None:
        """Enable DB2 spatial features on the SAMPLE database using db2se."""
        self.logger.info("Enabling spatial services on SAMPLE database")
        commands = [
            "db2se enable_db SAMPLE",
        ]
        for cmd in commands:
            self.logger.debug("Running spatial command: %s", cmd)
            try:
                _run(
                    [
                        "docker",
                        "exec",
                        self.config.name,
                        "su",
                        "-",
                        self.config.instance,
                        "-c",
                        cmd,
                    ],
                    capture_output=True,
                    timeout=600,
                )
            except subprocess.CalledProcessError as exc:
                stderr = exc.stderr.strip() if exc.stderr else ""
                stdout = exc.stdout.strip() if exc.stdout else ""
                message = "; ".join(part for part in [stdout, stderr] if part)
                self.logger.error("Failed spatial command [%s]: %s", cmd, message)
                raise CommandError(f"Failed to enable spatial services: {message}") from exc

    def stop(self) -> None:
        """Stop and remove the container if it exists."""
        if not self.exists():
            return
        self.logger.info("Stopping DB2 container %s", self.config.name)
        _run(["docker", "rm", "-f", self.config.name], check=False, capture_output=True)
        self._started = False

    def exists(self) -> bool:
        """Return True if Docker reports the container in any state."""
        result = _run(
            ["docker", "ps", "-a", "--filter", f"name={self.config.name}", "--format", "{{.ID}}"],
            capture_output=True,
        )
        return bool(result.stdout.strip())

    def connection_dsn(self) -> str:
        """Build an IBM DB API DSN for the container."""
        return (
            f"DATABASE={self.config.sample_db};"
            f"HOSTNAME=127.0.0.1;"
            f"PORT={self.config.port};"
            "PROTOCOL=TCPIP;"
            f"UID={self.config.instance};"
            f"PWD={self.config.password};"
        )
