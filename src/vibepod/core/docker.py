"""Thin Docker SDK wrapper used by CLI commands."""

from __future__ import annotations

import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
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


_PODMAN_HINT = (
    "If you use Podman, make sure the machine is running (`podman machine start`) "
    "or point DOCKER_HOST at the Podman socket."
)

#: Image namespace owned by vibepod; the only one auto_clean ever sweeps.
IMAGE_NAMESPACE = "vibepod"

# How much trailing container output attach_interactive keeps for post-exit
# inspection (resume hints appear in the last few lines of a session).
ATTACH_TAIL_LIMIT = 64 * 1024
PROXY_POLICY_SCHEMA_LABEL = "io.vibepod.proxy.policy-schema"
# The per-source policy schema this CLI speaks; a proxy image must carry the
# matching PROXY_POLICY_SCHEMA_LABEL value.
PROXY_POLICY_SCHEMA = "2"


def _run_podman(podman: str, args: list[str]) -> str | None:
    """Run a Podman subcommand, returning its trimmed stdout on success."""
    try:
        result = subprocess.run([podman, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _podman_machine_sockets(podman: str) -> list[str]:
    """Socket paths of every configured Podman machine, not just the default one."""
    listing = _run_podman(podman, ["machine", "list", "--format", "{{.Name}}"])
    if listing is None:
        return []
    # `machine list` suffixes the default machine's name with "*".
    names = [stripped.rstrip("*") for line in listing.splitlines() if (stripped := line.strip())]
    if not names:
        return []
    inspected = _run_podman(
        podman,
        ["machine", "inspect", *names, "--format", "{{.ConnectionInfo.PodmanSocket.Path}}"],
    )
    if inspected is None:
        return []
    return [stripped for line in inspected.splitlines() if (stripped := line.strip())]


def _discover_podman_socket() -> str | None:
    """Locate a Podman socket to use when the default Docker socket is unavailable.

    Podman machines on macOS expose the Docker-compatible API on a socket whose
    path depends on $TMPDIR at machine-start time, so it cannot be hardcoded.
    Ask Podman itself, then fall back to the rootless default on Linux.
    """
    if os.environ.get("DOCKER_HOST"):
        return None

    candidates: list[str] = []

    podman = shutil.which("podman")
    if podman is not None:
        machine_sockets = _podman_machine_sockets(podman)
        candidates.extend(machine_sockets)
        if not machine_sockets:
            # With a machine in play `podman info` reports the socket path as seen
            # from inside the VM, which does not exist on the host — only trust it
            # for native installs, where no machine is configured.
            remote_socket = _run_podman(podman, ["info", "--format", "{{.Host.RemoteSocket.Path}}"])
            if remote_socket is not None:
                candidates.append(remote_socket)

    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        candidates.append(os.path.join(runtime_dir, "podman", "podman.sock"))

    for candidate in candidates:
        path = candidate.removeprefix("unix://")
        if Path(path).is_socket():
            return f"unix://{path}"
    return None


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


def _reference_namespace(reference: str) -> str | None:
    """Return the namespace of an image *reference*, ignoring registry and digest.

    ``vibepod/claude@sha256:…`` and ``ghcr.io/vibepod/claude@sha256:…`` both
    yield ``vibepod``. A bare ``python@sha256:…`` has no namespace, and neither
    has a deeper path such as ``ghcr.io/acme/vibepod/tool`` — that repository
    belongs to ``acme``, not to us.
    """
    repository = reference.split("@", 1)[0]
    parts = repository.split("/")
    if len(parts) > 2 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        # Drop the registry host; what remains must be exactly <namespace>/<image>.
        parts = parts[1:]
    if len(parts) != 2:
        return None
    return parts[0]


def _parse_image_name(image: str) -> tuple[str, str | None]:
    """Parse a full image string into repository and tag/digest."""
    if "@" in image:
        repository, tag = image.split("@", 1)
        return repository, tag
    elif ":" in image:
        parts = image.rsplit(":", 1)
        if "/" not in parts[1]:
            return parts[0], parts[1]
    return image, None


class DockerManager:
    """Manager for all Docker operations."""

    def __init__(self) -> None:
        if docker is None:
            raise DockerClientError("Docker SDK not installed")
        try:
            self.client = docker.from_env()
            self.client.ping()
        except DockerException as exc:
            podman_socket = _discover_podman_socket()
            if podman_socket is None:
                raise DockerClientError(f"Docker is not available: {exc}. {_PODMAN_HINT}") from exc
            try:
                self.client = docker.DockerClient(base_url=podman_socket)
                self.client.ping()
            except DockerException as retry_exc:
                raise DockerClientError(
                    f"Docker is not available: {exc}. "
                    f"Found Podman socket {podman_socket} but could not connect: {retry_exc}",
                ) from retry_exc

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

    def _pull_image_with_progress(self, image: str) -> None:
        from rich.progress import (
            BarColumn,
            DownloadColumn,
            Progress,
            TextColumn,
            TimeRemainingColumn,
            TransferSpeedColumn,
        )

        from vibepod.utils.console import console

        repository, tag = _parse_image_name(image)
        try:
            response = self.client.api.pull(repository, tag=tag, stream=True, decode=True)
        except APIError as exc:
            raise DockerClientError(f"Failed to pull image {image}: {exc}") from exc

        tasks: dict[str, Any] = {}
        try:
            with Progress(
                TextColumn("{task.description}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
                disable=not console.is_terminal,
            ) as progress:
                for chunk in response:
                    if not isinstance(chunk, dict):
                        continue
                    if "error" in chunk:
                        raise DockerClientError(f"Failed to pull image {image}: {chunk['error']}")

                    status = chunk.get("status", "")
                    layer_id = chunk.get("id")
                    progress_detail = chunk.get("progressDetail") or {}

                    if not layer_id:
                        if status and status not in (
                            "Downloading",
                            "Extracting",
                            "Waiting",
                            "Download complete",
                            "Pull complete",
                            "Already exists",
                            "Pulling fs layer",
                        ):
                            progress.console.print(f"[dim]{status}[/dim]")
                        continue

                    status_color = "cyan"
                    if status in ("Download complete", "Pull complete", "Already exists"):
                        status_color = "green"
                    elif status == "Waiting":
                        status_color = "yellow"
                    elif "error" in status.lower() or "fail" in status.lower():
                        status_color = "red"

                    description = f"[{status_color}][{layer_id}][/{status_color}] {status}"

                    if layer_id not in tasks:
                        tasks[layer_id] = progress.add_task(description, total=None)

                    task_id = tasks[layer_id]
                    progress.update(task_id, description=description)

                    total = progress_detail.get("total", 0)
                    current = progress_detail.get("current", 0)

                    if total:
                        progress.update(task_id, total=total, completed=current)

                    if status in ("Download complete", "Pull complete", "Already exists"):
                        if total:
                            progress.update(task_id, completed=total, total=total)
                        else:
                            progress.update(task_id, completed=1, total=1)
        except Exception as exc:
            if isinstance(exc, DockerClientError):
                raise
            if isinstance(exc, APIError):
                raise DockerClientError(f"Failed to pull image {image}: {exc}") from exc
            raise DockerClientError(f"Failed to pull image {image}: {exc}") from exc

    def pull_image(self, image: str, auto_clean: bool = False) -> None:
        self._pull_image_with_progress(image)
        if auto_clean:
            self.clean_untagged_images()

    def pull_if_newer(self, image: str, auto_clean: bool = False) -> bool:
        """Pull *image* and return True if the local image was updated.

        Returns False when the image is already up to date, when the pull
        fails (e.g. no network / private registry), or when the image only
        exists locally and cannot be found on a registry.

        With *auto_clean*, untagged images left behind by this and earlier
        pulls are swept afterwards (see :meth:`clean_untagged_images`).
        """
        try:
            old_id = self.image_id(image)
            self.pull_image(image)
            new_id = self.image_id(image)
            if new_id is None:
                return False

            if auto_clean:
                self.clean_untagged_images()
            return bool(old_id != new_id)
        except (APIError, DockerClientError):
            return False

    def image_id(self, image: str) -> str | None:
        """Return the local id of *image*, or None when it cannot be determined."""
        try:
            return str(self.client.images.get(image).id)
        except DockerException:
            # Covers NotFound and any transient daemon error: callers treat a
            # missing id as "no confirmed image", never as a failure to report.
            return None

    def require_proxy_policy_schema(self, image: str | Any, required: str = "2") -> None:
        """Require an exact per-source policy schema label on a proxy image."""
        image_name = image if isinstance(image, str) else "the running proxy image"
        try:
            inspected = self.client.images.get(image) if isinstance(image, str) else image
            attrs = inspected.attrs
            config = attrs.get("Config", {}) if isinstance(attrs, dict) else {}
            labels = config.get("Labels", {}) if isinstance(config, dict) else {}
            actual = labels.get(PROXY_POLICY_SCHEMA_LABEL) if isinstance(labels, dict) else None
        except (AttributeError, DockerException) as exc:
            raise DockerClientError(
                f"Proxy image {image_name} could not be inspected for policy schema {required}",
            ) from exc
        if actual != required:
            shown = actual if isinstance(actual, str) and actual else "missing"
            raise DockerClientError(
                f"Proxy image {image_name} is incompatible: required policy schema {required}, "
                f"found {shown}. Use an updated VibePod proxy image or add "
                f"the label {PROXY_POLICY_SCHEMA_LABEL}={required} to a compatible custom image.",
            )

    def build_image(
        self,
        context_tar: Any,
        tag: str,
        labels: dict[str, str],
        *,
        nocache: bool = False,
    ) -> None:
        """Build *tag* from an in-memory custom-context tar, streaming output.

        The tar must contain a complete Dockerfile (see
        :func:`vibepod.core.overlay.build_context_tar`); nothing is written to
        the host filesystem. *nocache* disables docker's layer cache so a
        forced rebuild re-runs every instruction.
        """
        try:
            chunks = self.client.api.build(
                fileobj=context_tar,
                custom_context=True,
                tag=tag,
                labels=labels,
                rm=True,
                nocache=nocache,
                decode=True,
            )
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    continue
                if "error" in chunk:
                    raise DockerClientError(f"Failed to build image {tag}: {chunk['error']}")
                stream = chunk.get("stream")
                if stream and stream.strip():
                    # Through the shared console so ACP mode's stderr routing
                    # keeps build logs off the JSON-RPC stream on stdout.
                    from vibepod.utils.console import console

                    console.out(stream, end="" if stream.endswith("\n") else "\n", highlight=False)
        except DockerClientError:
            raise
        except (APIError, DockerException) as exc:
            raise DockerClientError(f"Failed to build image {tag}: {exc}") from exc

    def remove_stale_overlays(self, overlay_key: str, keep_tag: str) -> int:
        """Remove overlay images labeled with *overlay_key* except *keep_tag*.

        Old content-addressed tags accumulate as the overlay evolves; each
        successful build sweeps its predecessors. Images docker refuses to
        remove (still used by a container) stay and are retried next build.
        The label name is ``vibepod.core.overlay.OVERLAY_KEY_LABEL`` (kept as
        a literal here to avoid an import cycle with that module).
        """
        try:
            images = self.client.images.list(
                filters={"label": [f"vibepod.overlay.key={overlay_key}"]},
            )
        except DockerException:
            return 0

        removed = 0
        for image in images:
            if keep_tag in (image.tags or []):
                continue
            try:
                self.client.images.remove(str(image.id))
            except DockerException:
                continue
            removed += 1
        return removed

    def clean_untagged_images(self, namespace: str = IMAGE_NAMESPACE) -> int:
        """Remove untagged *namespace* images and return how many were removed.

        A pull that moves the ``latest`` tag leaves the previous image behind
        with no tag but with its repository digest intact, so it still shows up
        as ``vibepod/claude <none>``. Sweeping the whole namespace — rather
        than only the image just replaced — also clears leftovers from earlier
        pulls whose removal failed because a container still held them.

        Images that are still tagged, that belong to another namespace, or that
        cannot be attributed to one are never touched. Docker refuses to remove
        images used by a container (running or stopped); those stay and are
        retried by the next sweep.
        """
        try:
            images = self.client.images.list()
        except DockerException:
            return 0

        removed = 0
        for image in images:
            if image.tags:
                continue
            digests = image.attrs.get("RepoDigests") or []
            if not any(_reference_namespace(str(digest)) == namespace for digest in digests):
                continue
            try:
                self.client.images.remove(str(image.id))
            except DockerException:
                continue
            removed += 1
        return removed

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
                f"Image {image} not found locally. Pull the image first (for example with --pull).",
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
                "Specify a command in the image or in agent settings.",
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
        extra_labels: dict[str, str] | None = None,
        workspace_mount_path: str | None = None,
        start: bool = True,
        tty: bool = True,
    ) -> Any:
        container_name = name or f"vibepod-{agent}-{uuid4().hex[:8]}"

        labels = {
            CONTAINER_LABEL_MANAGED: "true",
            "vibepod.agent": agent,
            "vibepod.workspace": str(workspace),
            "vibepod.version": version,
            **(extra_labels or {}),
        }

        environment = {**env}

        volumes: list[str] = [
            f"{workspace}:/workspace:rw",
            f"{config_dir}:{config_mount_path}:rw",
        ]
        if workspace_mount_path:
            # ACP path parity: bind the workspace a second time onto its own
            # host path so host-side absolute paths (ACP session cwd, @-mentions,
            # diffs) resolve identically inside the container.
            volumes.insert(1, f"{workspace}:{workspace_mount_path}:rw")
        if extra_volumes:
            volumes.extend(f"{host}:{bind}:{mode}" for host, bind, mode in extra_volumes)

        try:
            if userns_mode is not None or not start:
                # Low-level create path: needed for Podman's `keep-id` (docker-py
                # rejects it) and for ACP's create-without-start ordering, and
                # because the high-level containers.create/run does not accept
                # `stdin_once` (the stdin-EOF lifecycle hook).
                host_config = self.client.api.create_host_config(
                    binds=volumes,
                    auto_remove=auto_remove,
                    network_mode=network,
                    port_bindings=ports,
                    extra_hosts={"host.docker.internal": "host-gateway"},
                )
                # docker-py validates userns_mode against Docker's enum and rejects
                # Podman's `keep-id`, so set the Docker-compatible HostConfig field
                # directly for Podman engines.
                if userns_mode is not None:
                    host_config["UsernsMode"] = userns_mode

                create_kwargs: dict[str, Any] = {
                    "image": image,
                    "name": container_name,
                    "command": command,
                    "tty": tty,
                    "stdin_open": True,
                    "labels": labels,
                    "environment": environment,
                    "working_dir": workspace_mount_path or "/workspace",
                    "host_config": host_config,
                }
                # Note: docker-py derives StdinOnce=true itself from
                # detach=False + stdin_open=True (ContainerConfig), which is
                # the ACP lifecycle hook — no explicit flag exists.
                if ports:
                    # (port, proto) tuples: raw "1456/tcp" keys would be
                    # re-suffixed by docker-py's exposed-port normalization
                    # into "1456/tcp/tcp".
                    create_kwargs["ports"] = [tuple(p.split("/", 1)) for p in ports]
                if platform:
                    create_kwargs["platform"] = platform
                if user:
                    create_kwargs["user"] = user
                if entrypoint:
                    create_kwargs["entrypoint"] = entrypoint

                created = self.client.api.create_container(**create_kwargs)
                container_id = created["Id"]
                if start:
                    self.client.api.start(container_id)
                return self.client.containers.get(container_id)

            run_kwargs: dict[str, Any] = {
                "image": image,
                "name": container_name,
                "command": command,
                "detach": True,
                "tty": tty,
                "stdin_open": True,
                "auto_remove": auto_remove,
                "labels": labels,
                "environment": environment,
                "volumes": volumes,
                "working_dir": workspace_mount_path or "/workspace",
                "network": network,
                # Docker Desktop resolves host.docker.internal natively, but Docker
                # Engine on Linux and Podman only do so with an explicit
                # host-gateway mapping. Local LLM servers (Ollama, LM Studio, ...)
                # are documented against this hostname, so map it unconditionally.
                "extra_hosts": {"host.docker.internal": "host-gateway"},
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
                    f"Failed to stop container '{container.name}': {exc}",
                ) from exc
            except DockerException as exc:
                raise DockerClientError(
                    f"Failed to stop container '{container.name}': {exc}",
                ) from exc
            stopped += 1
        return stopped

    def stop_container(self, name_or_id: str, force: bool = False) -> Any:
        container = self.get_container(name_or_id)
        labels = getattr(container, "labels", {}) or {}
        if labels.get(CONTAINER_LABEL_MANAGED) != "true":
            raise DockerClientError(
                f"Container '{name_or_id}' is not managed by VibePod; refusing to stop.",
            )
        try:
            container.stop(timeout=0 if force else 10)
        except APIError as exc:
            raise DockerClientError(
                f"Failed to stop container '{name_or_id}': {exc}",
            ) from exc
        except DockerException as exc:
            raise DockerClientError(
                f"Failed to stop container '{name_or_id}': {exc}",
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
                    f"Failed to stop container '{container.name}': {exc}",
                ) from exc
            except DockerException as exc:
                raise DockerClientError(
                    f"Failed to stop container '{container.name}': {exc}",
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
            all=True,
            filters={"label": ["vibepod.managed=true", "vibepod.role=datasette"]},
        )
        return containers[0] if containers else None

    def ensure_datasette(
        self,
        image: str,
        logs_db_path: Path,
        proxy_db_path: Path,
        port: int,
    ) -> Any:
        existing = self.find_datasette()
        if existing:
            existing.reload()
            env_list = existing.attrs.get("Config", {}).get("Env", []) or []
            has_proxy_env = any(env.startswith("PROXY_DB_PATH=") for env in env_list)
            if existing.status == "running" and has_proxy_env:
                return existing
            existing.remove(force=True)

        if hasattr(self.client, "images"):
            try:
                self.client.images.get(image)
            except NotFound:
                self.pull_image(image)

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
            all=True,
            filters={"label": ["vibepod.managed=true", "vibepod.role=proxy"]},
        )
        return containers[0] if containers else None

    def remove_proxy(self, existing: Any, timeout: float = 15.0) -> None:
        """Force-remove a proxy container and wait for it to disappear.

        Concurrent launches (e.g. an editor spawning `vp run` twice) can race
        on the removal; Docker then answers 409 "removal ... is already in
        progress", or 404 if the peer already finished. Treat both as success
        and wait until the container is gone so the caller can create its
        replacement.

        The wait tracks *this* container, not any proxy: a peer's replacement
        carries the same labels, so waiting for `find_proxy()` to go empty
        would never be satisfied once one exists.
        """
        target_id = getattr(existing, "id", None)
        try:
            existing.remove(force=True)
        except NotFound:
            return
        except APIError as exc:
            if "already in progress" not in str(exc):
                raise
        deadline = time.time() + timeout
        while time.time() < deadline:
            current = self.find_proxy()
            if current is None or getattr(current, "id", None) != target_id:
                return
            time.sleep(0.2)
        raise DockerClientError(
            "Timed out waiting for the previous vibepod-proxy container to be removed.",
        )

    def ensure_proxy(
        self,
        image: str,
        db_path: Path,
        ca_dir: Path,
        network: str,
        policy_schema: str | None = None,
    ) -> Any:
        existing = self.find_proxy()
        if existing:
            if existing.status == "running":
                if policy_schema is not None:
                    self.require_proxy_policy_schema(existing.image, policy_schema)
                return existing
            self.remove_proxy(existing)

        if hasattr(self.client, "images"):
            try:
                self.client.images.get(image)
            except NotFound:
                self.pull_image(image)

        if policy_schema is not None:
            self.require_proxy_policy_schema(image, policy_schema)

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

        if not self.is_rootless_podman():
            getuid = getattr(os, "getuid", None)
            getgid = getattr(os, "getgid", None)
            if callable(getuid) and callable(getgid):
                run_kwargs["user"] = f"{getuid()}:{getgid()}"

        return self.client.containers.run(**run_kwargs)

    def attach_interactive(self, container: Any, logger: Any = None) -> bytes:
        """Attach local stdin/stdout to a running container TTY.

        Returns the tail (last ``ATTACH_TAIL_LIMIT`` bytes) of the container
        output, so callers can inspect it after the session ends (e.g. for
        agent resume hints).
        """

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

        output_tail = bytearray()
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
                    output_tail.extend(data)
                    if len(output_tail) > ATTACH_TAIL_LIMIT:
                        del output_tail[:-ATTACH_TAIL_LIMIT]

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
        return bytes(output_tail)

    def attach_stdio(
        self,
        container: Any,
        logger: Any = None,
        on_attached: Any = None,
        auto_remove: bool = False,
    ) -> int:
        """Attach local stdin/stdout to a container without a TTY (ACP mode).

        Unlike ``attach_interactive`` this performs no raw-mode switching, no
        resize handling and no ``logs`` replay; instead it demultiplexes the
        Docker stream frame protocol so container stdout goes to our stdout
        and stderr to our stderr, keeping the JSON-RPC stream clean.
        Returns the container's exit code, also when the container was
        stopped by the SIGINT/SIGTERM handling. ``auto_remove`` must match the
        container's AutoRemove setting so the exit code survives its removal.
        """
        try:
            sock_wrapper = self.client.api.attach_socket(
                container.id,
                params={
                    "stdin": 1,
                    "stdout": 1,
                    "stderr": 1,
                    "stream": 1,
                },
            )
        except Exception as exc:  # pragma: no cover - runtime Docker behavior
            raise DockerClientError(f"Failed to attach to container: {exc}") from exc

        sock = getattr(sock_wrapper, "_sock", sock_wrapper)

        # Register the exit-code wait before the container starts. With
        # AutoRemove the container can be gone by the time the stream closes,
        # so inspecting it afterwards races the daemon; a wait opened up front
        # is answered with the status code even once the container is removed
        # (docker's own CLI waits for "removed" under --rm for the same reason).
        wait_result: dict[str, Any] = {}

        def _wait_for_exit() -> None:
            try:
                result = self.client.api.wait(
                    container.id,
                    timeout=None,
                    condition="removed" if auto_remove else "next-exit",
                )
            except Exception:
                return
            if isinstance(result, dict):
                wait_result.update(result)

        waiter = threading.Thread(target=_wait_for_exit, name="vibepod-acp-wait", daemon=True)
        waiter.start()

        if on_attached is not None:
            # Create-attach-start ordering: the ACP adapter emits its first
            # JSON-RPC frames immediately once the entrypoint runs, so the
            # caller starts the container only after the attach is in place.
            on_attached()
        stdin_fd = None
        if sys.stdin is not None and not sys.stdin.closed:
            try:
                stdin_fd = sys.stdin.fileno()
            except (OSError, ValueError):
                stdin_fd = None

        stop_requested = False
        old_sigterm = None
        old_sigint = None

        def _request_stop(signum: int, frame: Any) -> None:
            del signum, frame
            nonlocal stop_requested
            stop_requested = True
            try:
                container.stop(timeout=5)
            except Exception:  # pragma: no cover - best effort
                pass

        try:
            old_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, _request_stop)
            old_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, _request_stop)
        except ValueError:  # pragma: no cover - not in main thread (tests)
            old_sigterm = old_sigint = None

        buffer = b""
        try:
            while True:
                if stop_requested:
                    break
                readers: list[Any] = [sock]
                if stdin_fd is not None:
                    readers.append(sys.stdin)
                ready, _, _ = select.select(readers, [], [])

                if sock in ready:
                    data = sock.recv(65536)
                    if not data:
                        break
                    buffer += data
                    # Docker (no TTY) frames each stream chunk with an 8-byte
                    # header: stream byte, 3 padding bytes, 4-byte big-endian
                    # payload length. Frames can split across recv() bounds,
                    # so buffer until header and payload are complete.
                    while len(buffer) >= 8:
                        stream_type = buffer[0]
                        length = int.from_bytes(buffer[4:8], "big")
                        if len(buffer) < 8 + length:
                            break
                        payload = buffer[8 : 8 + length]
                        buffer = buffer[8 + length :]
                        if stream_type == 1:
                            sys.stdout.buffer.write(payload)
                            sys.stdout.buffer.flush()
                        elif stream_type == 2:
                            sys.stderr.buffer.write(payload)
                            sys.stderr.buffer.flush()
                        if logger is not None:
                            logger.log_output(payload)

                if stdin_fd is not None and sys.stdin in ready:
                    try:
                        user_data = os.read(stdin_fd, 65536)
                    except OSError:
                        user_data = b""
                    if not user_data:
                        # stdin EOF (ACP client went away): close our write
                        # side so the adapter sees EOF and exits, but keep
                        # draining the container stream until it ends.
                        try:
                            sock.shutdown(socket.SHUT_WR)
                        except OSError:  # pragma: no cover - already closed
                            pass
                        stdin_fd = None
                    else:
                        try:
                            sock.sendall(user_data)
                        except OSError:
                            stdin_fd = None
        finally:
            try:
                sock_wrapper.close()
            except Exception:  # pragma: no cover - best effort
                pass
            if old_sigterm is not None:
                signal.signal(signal.SIGTERM, old_sigterm)
            if old_sigint is not None:
                signal.signal(signal.SIGINT, old_sigint)

        waiter.join(timeout=10)
        status = wait_result.get("StatusCode")
        if status is None:
            # No wait answer (daemon without wait conditions, or the stream
            # ended without an exit): fall back to an inspect, which only
            # works while the container still exists.
            try:
                container.reload()
                status = container.attrs.get("State", {}).get("ExitCode", 0)
            except Exception:  # pragma: no cover - auto-removed containers
                status = 0
        return int(status or 0)
