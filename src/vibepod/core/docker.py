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
    from docker.errors import APIError, DockerException, NotFound
except Exception:  # pragma: no cover - handled at runtime
    docker = None  # type: ignore[assignment]
    APIError = Exception  # type: ignore[misc,assignment]
    DockerException = Exception  # type: ignore[misc,assignment]
    NotFound = Exception  # type: ignore[misc,assignment]


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

    def ensure_network(self, name: str) -> None:
        try:
            self.client.networks.get(name)
        except NotFound:
            self.client.networks.create(name, labels={CONTAINER_LABEL_MANAGED: "true"})

    def networks_with_running_containers(self) -> list[str]:
        networks: set[str] = set()
        for container in self.client.containers.list():
            try:
                attached = container.attrs.get("NetworkSettings", {}).get("Networks", {}) or {}
            except AttributeError:
                continue
            networks.update(attached.keys())
        return sorted(networks)

    def connect_network(self, container: Any, network_name: str) -> None:
        try:
            network = self.client.networks.get(network_name)
            network.connect(container)
        except APIError as exc:
            raise DockerClientError(f"Failed to connect to network {network_name}: {exc}") from exc

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
        network: str | None = None,
        extra_volumes: list[tuple[str, str, str]] | None = None,
        platform: str | None = None,
        user: str | None = None,
    ) -> Any:
        container_name = name or f"vibepod-{agent}-{uuid4().hex[:8]}"

        labels = {
            CONTAINER_LABEL_MANAGED: "true",
            "vibepod.agent": agent,
            "vibepod.workspace": str(workspace),
            "vibepod.version": version,
        }

        environment = {**env}

        volumes: list[str] = [
            f"{workspace}:/workspace:rw",
            f"{config_dir}:{config_mount_path}:rw",
        ]
        if extra_volumes:
            volumes.extend(f"{host}:{bind}:{mode}" for host, bind, mode in extra_volumes)

        try:
            run_kwargs: dict[str, Any] = {
                "image": image,
                "name": container_name,
                "command": command,
                "detach": True,
                "tty": True,
                "stdin_open": True,
                "auto_remove": auto_remove,
                "labels": labels,
                "environment": environment,
                "volumes": volumes,
                "working_dir": "/workspace",
                "network": network,
            }
            if platform:
                run_kwargs["platform"] = platform
            if user:
                run_kwargs["user"] = user

            return self.client.containers.run(**run_kwargs)
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

    def ensure_datasette(self, image: str, logs_db_path: Path, proxy_db_path: Path, port: int) -> Any:
        existing = self.find_datasette()
        if existing:
            existing.reload()
            env_list = existing.attrs.get("Config", {}).get("Env", []) or []
            has_proxy_env = any(env.startswith("PROXY_DB_PATH=") for env in env_list)
            if existing.status == "running" and has_proxy_env:
                return existing
            existing.remove(force=True)

        logs_db_path.parent.mkdir(parents=True, exist_ok=True)
        if not logs_db_path.exists():
            logs_db_path.touch()

        logs_parent = Path(os.path.abspath(str(logs_db_path.parent)))
        proxy_parent = Path(os.path.abspath(str(proxy_db_path.parent)))

        if logs_parent == proxy_parent:
            volumes = {str(logs_parent): {"bind": "/mount/data", "mode": "rw"}}
            logs_db_container_path = f"/mount/data/{logs_db_path.name}"
            proxy_db_container_path = f"/mount/data/{proxy_db_path.name}"
        else:
            volumes = {
                str(logs_parent): {"bind": "/mount/logs", "mode": "rw"},
                str(proxy_parent): {"bind": "/mount/proxy", "mode": "rw"},
            }
            logs_db_container_path = f"/mount/logs/{logs_db_path.name}"
            proxy_db_container_path = f"/mount/proxy/{proxy_db_path.name}"

        return self.client.containers.run(
            image=image,
            name="vibepod-datasette",
            detach=True,
            labels={"vibepod.managed": "true", "vibepod.role": "datasette"},
            environment={
                "LOGS_DB_PATH": logs_db_container_path,
                "PROXY_DB_PATH": proxy_db_container_path,
                "DATASETTE_PORT": "8001",
            },
            volumes=volumes,
            ports={"8001/tcp": port},
        )

    def find_proxy(self) -> Any | None:
        containers = self.client.containers.list(
            all=True, filters={"label": ["vibepod.managed=true", "vibepod.role=proxy"]}
        )
        return containers[0] if containers else None

    def ensure_proxy(self, image: str, db_path: Path, ca_dir: Path, port: int, network: str) -> Any:
        existing = self.find_proxy()
        if existing:
            if existing.status == "running":
                return existing
            existing.remove(force=True)

        db_path.parent.mkdir(parents=True, exist_ok=True)
        ca_dir.mkdir(parents=True, exist_ok=True)

        volumes = {
            str(db_path.parent): {"bind": "/data", "mode": "rw"},
            str(ca_dir): {"bind": "/data/mitmproxy", "mode": "rw"},
        }

        run_kwargs: dict[str, Any] = {
            "image": image,
            "name": "vibepod-proxy",
            "detach": True,
            "labels": {"vibepod.managed": "true", "vibepod.role": "proxy"},
            "environment": {
                "PROXY_DB_PATH": "/data/proxy.db",
                "PROXY_CONF_DIR": "/data/mitmproxy",
            },
            "volumes": volumes,
            "ports": {"8080/tcp": port},
            "network": network,
        }

        getuid = getattr(os, "getuid", None)
        getgid = getattr(os, "getgid", None)
        if callable(getuid) and callable(getgid):
            run_kwargs["user"] = f"{getuid()}:{getgid()}"

        return self.client.containers.run(**run_kwargs)

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
