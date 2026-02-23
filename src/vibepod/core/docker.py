"""Thin Docker SDK wrapper used by CLI commands."""

from __future__ import annotations

import os
import select
import shutil
import signal
import sys
import termios
import tty
from pathlib import Path
from typing import Any
from uuid import uuid4

from vibepod.constants import CONTAINER_LABEL_MANAGED

try:
    import docker
    from docker.errors import APIError, DockerException
except Exception:  # pragma: no cover - handled at runtime
    docker = None  # type: ignore[assignment]
    APIError = Exception  # type: ignore[misc,assignment]
    DockerException = Exception  # type: ignore[misc,assignment]


class DockerClientError(RuntimeError):
    """Raised for Docker availability or lifecycle errors."""


class DockerManager:
    """Manager for all Docker operations."""

    def __init__(self) -> None:
        if docker is None:
            raise DockerClientError("Docker SDK not installed")
        try:
            self.client = docker.from_env()
            self.client.ping()
        except DockerException as exc:
            raise DockerClientError(f"Docker is not available: {exc}") from exc

    def pull_image(self, image: str) -> None:
        try:
            self.client.images.pull(image)
        except APIError as exc:
            raise DockerClientError(f"Failed to pull image {image}: {exc}") from exc

    def run_agent(
        self,
        *,
        agent: str,
        image: str,
        workspace: Path,
        config_dir: Path,
        config_mount_path: str,
        env: dict[str, str],
        command: list[str] | None,
        auto_remove: bool,
        name: str | None,
        version: str,
    ) -> Any:
        container_name = name or f"vibepod-{agent}-{uuid4().hex[:8]}"

        labels = {
            CONTAINER_LABEL_MANAGED: "true",
            "vibepod.agent": agent,
            "vibepod.workspace": str(workspace),
            "vibepod.version": version,
        }

        environment = {**env}

        volumes = {
            str(workspace): {"bind": "/workspace", "mode": "rw"},
            str(config_dir): {"bind": config_mount_path, "mode": "rw"},
        }

        try:
            return self.client.containers.run(
                image=image,
                name=container_name,
                command=command,
                detach=True,
                tty=True,
                stdin_open=True,
                auto_remove=auto_remove,
                labels=labels,
                environment=environment,
                volumes=volumes,
                working_dir="/workspace",
            )
        except APIError as exc:
            raise DockerClientError(f"Failed to start container: {exc}") from exc

    def stop_agent(self, agent: str, force: bool = False) -> int:
        stopped = 0
        for container in self.list_managed(all_containers=True):
            if container.labels.get("vibepod.agent") != agent:
                continue
            container.stop(timeout=0 if force else 10)
            stopped += 1
        return stopped

    def stop_all(self, force: bool = False) -> int:
        stopped = 0
        for container in self.list_managed(all_containers=True):
            container.stop(timeout=0 if force else 10)
            stopped += 1
        return stopped

    def list_managed(self, all_containers: bool = False) -> list[Any]:
        filters = {"label": f"{CONTAINER_LABEL_MANAGED}=true"}
        return list(self.client.containers.list(all=all_containers, filters=filters))

    def find_datasette(self) -> Any | None:
        containers = self.client.containers.list(
            all=True, filters={"label": ["vibepod.managed=true", "vibepod.role=datasette"]}
        )
        return containers[0] if containers else None

    def ensure_datasette(self, image: str, db_path: Path, port: int) -> Any:
        existing = self.find_datasette()
        if existing:
            if existing.status != "running":
                existing.start()
            return existing

        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.touch(exist_ok=True)

        return self.client.containers.run(
            image=image,
            name="vibepod-datasette",
            command=[
                "datasette",
                "/data/logs.db",
                "--host",
                "0.0.0.0",
                "--port",
                "8001",
                "--setting",
                "sql_time_limit_ms",
                "10000",
            ],
            detach=True,
            labels={"vibepod.managed": "true", "vibepod.role": "datasette"},
            volumes={str(db_path.parent): {"bind": "/data", "mode": "rw"}},
            ports={"8001/tcp": port},
        )

    def attach_interactive(self, container: Any, logger: Any = None) -> None:
        """Attach local stdin/stdout to a running container TTY."""
        def resize_tty() -> None:
            size = shutil.get_terminal_size(fallback=(120, 40))
            try:
                self.client.api.resize(container.id, height=size.lines, width=size.columns)
            except Exception:
                pass

        try:
            sock_wrapper = self.client.api.attach_socket(
                container.id,
                params={
                    "stdin": 1,
                    "stdout": 1,
                    "stderr": 1,
                    "stream": 1,
                    "logs": 1,
                },
            )
        except Exception as exc:  # pragma: no cover - runtime Docker behavior
            raise DockerClientError(f"Failed to attach to container: {exc}") from exc

        sock = getattr(sock_wrapper, "_sock", sock_wrapper)
        resize_tty()

        stdin_fd = None
        old_tty = None
        old_winch_handler = None
        try:
            if sys.stdin.isatty():
                stdin_fd = sys.stdin.fileno()
                old_tty = termios.tcgetattr(stdin_fd)
                tty.setraw(stdin_fd)
                old_winch_handler = signal.getsignal(signal.SIGWINCH)

                def _on_winch(signum: int, frame: Any) -> None:
                    del signum, frame
                    resize_tty()

                signal.signal(signal.SIGWINCH, _on_winch)

            while True:
                readers = [sock]
                if stdin_fd is not None:
                    readers.append(sys.stdin)

                ready, _, _ = select.select(readers, [], [])

                if sock in ready:
                    data = sock.recv(8192)
                    if not data:
                        break
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()

                if stdin_fd is not None and sys.stdin in ready:
                    user_data = os.read(stdin_fd, 1024)
                    if not user_data:
                        continue
                    if logger is not None:
                        logger.log_input(user_data)
                    sock.sendall(user_data)
        finally:
            try:
                sock_wrapper.close()
            except Exception:
                pass
            if old_winch_handler is not None:
                signal.signal(signal.SIGWINCH, old_winch_handler)
            if stdin_fd is not None and old_tty is not None:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_tty)
