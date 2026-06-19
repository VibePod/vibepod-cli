"""Thin Docker SDK wrapper used by CLI commands."""

from __future__ import annotations

import os
import select
import shutil
import signal
import sys
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from vibepod.constants import CONTAINER_LABEL_MANAGED

docker: Any | None
APIError: type[Exception]
DockerException: type[Exception]
NotFound: type[Exception]
termios: Any | None
tty: Any | None
msvcrt: Any | None

try:
    import termios as _termios
    import tty as _tty
except ImportError:  # pragma: no cover - exercised on Windows
    termios = None
    tty = None
else:
    termios = _termios
    tty = _tty

try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - exercised off Windows
    msvcrt = None
else:
    msvcrt = _msvcrt

try:
    import docker as _docker
    from docker.errors import APIError as _APIError
    from docker.errors import DockerException as _DockerException
    from docker.errors import NotFound as _NotFound
except ImportError:  # pragma: no cover - handled at runtime
    docker = None
    APIError = Exception
    DockerException = Exception
    NotFound = Exception
else:
    docker = _docker
    APIError = _APIError
    DockerException = _DockerException
    NotFound = _NotFound


class DockerClientError(RuntimeError):
    """Raised for Docker availability or lifecycle errors."""


def _encode_console_character(ch: str) -> bytes:
    encoding = getattr(sys.stdin, "encoding", None) or "utf-8"
    return ch.encode(encoding, errors="replace")


def _forward_windows_console_input(sock: Any, logger: Any, stop_event: threading.Event) -> None:
    if msvcrt is None:
        return
    while not stop_event.is_set():
        try:
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                ch += msvcrt.getwch()
        except (EOFError, KeyboardInterrupt, OSError):
            return

        data = _encode_console_character(ch)
        if logger is not None:
            logger.log_input(data)
        try:
            sock.sendall(data)
        except OSError:
            return


def _is_latest_tag(image: str) -> bool:
    """Return True when *image* uses the ``latest`` tag (explicitly or by omission)."""
    name = image.split("/")[-1]
    return ":" not in name or name.endswith(":latest")


def _normalize_command(value: Any) -> list[str]:
    """Normalize Docker command/entrypoint values to a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(part) for part in value]
    return [str(value)]


def _version_is_podman(version: Any) -> bool:
    """Return True when Docker-compatible API version metadata belongs to Podman."""
    if not isinstance(version, dict):
        return False

    components = version.get("Components", [])
    if isinstance(components, list):
        for component in components:
            if not isinstance(component, dict):
                continue
            name = str(component.get("Name", "")).lower()
            if "podman" in name:
                return True

    platform = version.get("Platform")
    if isinstance(platform, dict) and "podman" in str(platform.get("Name", "")).lower():
        return True

    return "podman" in str(version.get("Name", "")).lower()


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

        self._rootless_podman: bool | None = None

    def is_rootless_podman(self) -> bool:
        """Return True for a rootless Podman engine exposed through the Docker API."""
        cached = getattr(self, "_rootless_podman", None)
        if isinstance(cached, bool):
            return cached

        try:
            info = self.client.info()
            version = self.client.version()
        except (APIError, DockerException, AttributeError):
            self._rootless_podman = False
            return False

        if not isinstance(info, dict):
            self._rootless_podman = False
            return False

        security_options = info.get("SecurityOptions", [])
        rootless = bool(info.get("Rootless")) or (
            isinstance(security_options, list)
            and any(str(option).lower() == "name=rootless" for option in security_options)
        )
        self._rootless_podman = rootless and _version_is_podman(version)
        return self._rootless_podman

    def pull_image(self, image: str) -> None:
        try:
            self.client.images.pull(image)
        except APIError as exc:
            raise DockerClientError(f"Failed to pull image {image}: {exc}") from exc

    def pull_if_newer(self, image: str) -> bool:
        """Pull *image* and return True if the local image was updated.

        Returns False when the image is already up to date, when the pull
        fails (e.g. no network / private registry), or when the image only
        exists locally and cannot be found on a registry.
        """
        try:
            old_id: str | None
            try:
                old_id = self.client.images.get(image).id
            except NotFound:
                old_id = None

            self.client.images.pull(image)

            try:
                new_id = self.client.images.get(image).id
            except NotFound:
                return False

            return bool(old_id != new_id)
        except APIError:
            return False

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

    def get_container(self, name_or_id: str) -> Any:
        try:
            return self.client.containers.get(name_or_id)
        except NotFound as exc:
            raise DockerClientError(f"Container '{name_or_id}' not found") from exc
        except APIError as exc:
            raise DockerClientError(f"Failed to look up container '{name_or_id}': {exc}") from exc
        except DockerException as exc:
            raise DockerClientError(f"Failed to look up container '{name_or_id}': {exc}") from exc

    def resolve_launch_command(self, image: str, command: list[str] | None) -> list[str]:
        """Resolve the full executable argv for a container start."""
        try:
            image_obj = self.client.images.get(image)
        except NotFound as exc:
            raise DockerClientError(
                f"Image {image} not found locally. Pull the image first (for example with --pull)."
            ) from exc
        except APIError as exc:
            raise DockerClientError(f"Failed to inspect image {image}: {exc}") from exc
        except DockerException as exc:
            raise DockerClientError(f"Failed to inspect image {image}: {exc}") from exc

        image_config = image_obj.attrs.get("Config", {}) if hasattr(image_obj, "attrs") else {}
        if not isinstance(image_config, dict):
            image_config = {}

        image_entrypoint = _normalize_command(image_config.get("Entrypoint"))
        image_cmd = _normalize_command(image_config.get("Cmd"))
        effective_cmd = command if command is not None else image_cmd
        launch = [*image_entrypoint, *effective_cmd]

        if not launch:
            raise DockerClientError(
                f"Could not resolve a startup command for image {image}. "
                "Specify a command in the image or in agent settings."
            )
        return launch

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
        ports: dict[str, Any] | None = None,
        extra_volumes: list[tuple[str, str, str]] | None = None,
        platform: str | None = None,
        user: str | None = None,
        entrypoint: list[str] | None = None,
        userns_mode: str | None = None,
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
            if userns_mode is not None:
                host_config = self.client.api.create_host_config(
                    binds=volumes,
                    auto_remove=auto_remove,
                    network_mode=network,
                    port_bindings=ports,
                )
                # docker-py validates userns_mode against Docker's enum and rejects
                # Podman's `keep-id`, so set the Docker-compatible HostConfig field
                # directly for Podman engines.
                host_config["UsernsMode"] = userns_mode

                create_kwargs: dict[str, Any] = {
                    "image": image,
                    "name": container_name,
                    "command": command,
                    "tty": True,
                    "stdin_open": True,
                    "labels": labels,
                    "environment": environment,
                    "working_dir": "/workspace",
                    "host_config": host_config,
                }
                if ports:
                    create_kwargs["ports"] = list(ports.keys())
                if platform:
                    create_kwargs["platform"] = platform
                if user:
                    create_kwargs["user"] = user
                if entrypoint:
                    create_kwargs["entrypoint"] = entrypoint

                created = self.client.api.create_container(**create_kwargs)
                container_id = created["Id"]
                self.client.api.start(container_id)
                return self.client.containers.get(container_id)

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
            if ports:
                # Maps "<container_port>/tcp" -> host_port. Used to publish the
                # Codex OAuth callback forwarder during `codex login`.
                run_kwargs["ports"] = ports
            if platform:
                run_kwargs["platform"] = platform
            if user:
                run_kwargs["user"] = user
            if entrypoint:
                run_kwargs["entrypoint"] = entrypoint

            return self.client.containers.run(**run_kwargs)
        except APIError as exc:
            raise DockerClientError(f"Failed to start container: {exc}") from exc

    def stop_agent(self, agent: str, force: bool = False) -> int:
        stopped = 0
        timeout = 0 if force else 10
        for container in self.list_managed(all_containers=True):
            if container.labels.get("vibepod.agent") != agent:
                continue
            try:
                container.stop(timeout=timeout)
            except APIError as exc:
                raise DockerClientError(
                    f"Failed to stop container '{container.name}': {exc}"
                ) from exc
            except DockerException as exc:
                raise DockerClientError(
                    f"Failed to stop container '{container.name}': {exc}"
                ) from exc
            stopped += 1
        return stopped

    def stop_container(self, name_or_id: str, force: bool = False) -> Any:
        container = self.get_container(name_or_id)
        labels = getattr(container, "labels", {}) or {}
        if labels.get(CONTAINER_LABEL_MANAGED) != "true":
            raise DockerClientError(
                f"Container '{name_or_id}' is not managed by VibePod; refusing to stop."
            )
        try:
            container.stop(timeout=0 if force else 10)
        except APIError as exc:
            raise DockerClientError(
                f"Failed to stop container '{name_or_id}': {exc}"
            ) from exc
        except DockerException as exc:
            raise DockerClientError(
                f"Failed to stop container '{name_or_id}': {exc}"
            ) from exc
        return container

    def stop_all(self, force: bool = False) -> int:
        stopped = 0
        timeout = 0 if force else 10
        for container in self.list_managed(all_containers=True):
            try:
                container.stop(timeout=timeout)
            except APIError as exc:
                raise DockerClientError(
                    f"Failed to stop container '{container.name}': {exc}"
                ) from exc
            except DockerException as exc:
                raise DockerClientError(
                    f"Failed to stop container '{container.name}': {exc}"
                ) from exc
            stopped += 1
        return stopped

    def list_managed(self, all_containers: bool = False) -> list[Any]:
        filters = {"label": f"{CONTAINER_LABEL_MANAGED}=true"}
        try:
            return list(self.client.containers.list(all=all_containers, filters=filters))
        except APIError as exc:
            raise DockerClientError(f"Failed to list containers: {exc}") from exc
        except DockerException as exc:
            raise DockerClientError(f"Failed to list containers: {exc}") from exc

    def find_datasette(self) -> Any | None:
        containers = self.client.containers.list(
            all=True, filters={"label": ["vibepod.managed=true", "vibepod.role=datasette"]}
        )
        return containers[0] if containers else None

    def ensure_datasette(
        self, image: str, logs_db_path: Path, proxy_db_path: Path, port: int
    ) -> Any:
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

    def ensure_proxy(self, image: str, db_path: Path, ca_dir: Path, network: str) -> Any:
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
            "network": network,
            "extra_hosts": {"host.docker.internal": "host-gateway"},
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
        input_stop_event: threading.Event | None = None
        input_thread: threading.Thread | None = None
        sigwinch = getattr(signal, "SIGWINCH", None)
        try:
            if sys.stdin.isatty() and termios is not None and tty is not None:
                stdin_fd = sys.stdin.fileno()
                old_tty = termios.tcgetattr(stdin_fd)
                tty.setraw(stdin_fd)
                if sigwinch is not None:
                    old_winch_handler = signal.getsignal(sigwinch)

                    def _on_winch(signum: int, frame: Any) -> None:
                        del signum, frame
                        resize_tty()

                    signal.signal(sigwinch, _on_winch)
            elif sys.stdin.isatty() and msvcrt is not None:
                input_stop_event = threading.Event()
                input_thread = threading.Thread(
                    target=_forward_windows_console_input,
                    args=(sock, logger, input_stop_event),
                    daemon=True,
                )
                input_thread.start()

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
            if input_stop_event is not None:
                input_stop_event.set()
            if input_thread is not None:
                input_thread.join(timeout=0.2)
            if sigwinch is not None and old_winch_handler is not None:
                signal.signal(sigwinch, old_winch_handler)
            if stdin_fd is not None and old_tty is not None and termios is not None:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_tty)
