"""Run command implementation."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer

from vibepod import __version__
from vibepod.constants import EXIT_DOCKER_NOT_RUNNING, SUPPORTED_AGENTS
from vibepod.core.agents import (
    agent_config_dir,
    effective_agent_image,
    get_agent_spec,
    is_supported_agent,
)
from vibepod.core.config import get_config
from vibepod.core.docker import DockerClientError, DockerManager
from vibepod.core.session_logger import SessionLogger
from vibepod.utils.console import error, info, success, warning


def _parse_env_pairs(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for entry in values:
        if "=" not in entry:
            raise typer.BadParameter(f"Invalid --env value '{entry}', expected KEY=VALUE")
        key, value = entry.split("=", 1)
        if not key:
            raise typer.BadParameter("Environment variable key cannot be empty")
        parsed[key] = value
    return parsed


def _get_container_ip(container: object, network: str) -> str | None:
    """Extract the container's IP address on the given Docker network."""
    try:
        networks = container.attrs["NetworkSettings"]["Networks"]  # type: ignore[index]
        return networks[network]["IPAddress"] or None  # type: ignore[index]
    except (KeyError, TypeError, AttributeError):
        return None


def _update_container_mapping(
    mapping_path: Path,
    ip: str,
    container_id: str,
    container_name: str,
    agent: str,
) -> bool:
    """Merge a new IP→container entry into containers.json atomically."""
    mapping: dict[str, dict[str, str]] = {}
    try:
        if mapping_path.exists():
            try:
                mapping = json.loads(mapping_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        mapping[ip] = {
            "container_id": container_id,
            "container_name": container_name,
            "agent": agent,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }

        tmp_path = mapping_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(mapping, indent=2))
        os.replace(tmp_path, mapping_path)
    except OSError:
        return False
    return True


def run(
    agent: Annotated[str | None, typer.Argument(help="Agent to run")] = None,
    workspace: Annotated[Path, typer.Option("-w", "--workspace", help="Workspace directory")] = Path(
        "."
    ),
    pull: Annotated[bool, typer.Option("--pull", help="Pull latest image before run")] = False,
    detach: Annotated[bool, typer.Option("-d", "--detach", help="Run container in background")] = False,
    env: Annotated[
        list[str] | None,
        typer.Option("-e", "--env", help="Environment variable KEY=VALUE", show_default=False),
    ] = None,
    name: Annotated[str | None, typer.Option("--name", help="Custom container name")] = None,
) -> None:
    """Start an agent container."""
    config = get_config()
    selected_agent = agent or str(config.get("default_agent", "claude"))

    if not is_supported_agent(selected_agent):
        error(f"Unknown agent '{selected_agent}'. Supported: {', '.join(SUPPORTED_AGENTS)}")
        raise typer.Exit(1)

    workspace_path = workspace.expanduser().resolve()
    if not workspace_path.exists() or not workspace_path.is_dir():
        raise typer.BadParameter(f"Workspace not found: {workspace_path}")

    agent_cfg = config.get("agents", {}).get(selected_agent, {})
    spec = get_agent_spec(selected_agent)
    merged_env = {
        "USER_UID": str(os.getuid()),
        "USER_GID": str(os.getgid()),
        **spec.extra_env,
        **{str(k): str(v) for k, v in agent_cfg.get("env", {}).items()},
        **_parse_env_pairs(env or []),
    }

    image = effective_agent_image(selected_agent, config)

    try:
        manager = DockerManager()
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc

    network_name = str(config.get("network", "vibepod-network"))
    manager.ensure_network(network_name)

    if pull or bool(config.get("auto_pull", False)):
        info(f"Pulling image: {image}")
        manager.pull_image(image)

    config_dir = agent_config_dir(selected_agent)
    config_dir.mkdir(parents=True, exist_ok=True)

    proxy_cfg = config.get("proxy", {})
    proxy_enabled = bool(proxy_cfg.get("enabled", True))
    proxy_ca_dir_value = str(proxy_cfg.get("ca_dir", "")).strip()
    proxy_ca_path_value = str(proxy_cfg.get("ca_path", "")).strip()
    proxy_ca_dir = Path(proxy_ca_dir_value).expanduser().resolve() if proxy_ca_dir_value else None
    proxy_ca_path = Path(proxy_ca_path_value).expanduser().resolve() if proxy_ca_path_value else None
    proxy_port = int(proxy_cfg.get("port", 8080))
    proxy_db_path: Path | None = None

    extra_volumes: dict[str, dict[str, str]] = {}
    if proxy_enabled:
        proxy_image = str(proxy_cfg.get("image", "vibepod/proxy:latest"))
        proxy_db_path = Path(str(proxy_cfg.get("db_path", "~/.config/vibepod/proxy/proxy.db"))).expanduser().resolve()

        manager.ensure_proxy(
            image=proxy_image,
            db_path=proxy_db_path,
            ca_dir=proxy_ca_dir or proxy_db_path.parent / "mitmproxy",
            port=proxy_port,
            network=network_name,
        )

        if proxy_ca_path:
            ca_ready = False
            deadline = time.time() + 10
            while time.time() < deadline:
                if proxy_ca_path.exists():
                    ca_ready = True
                    break
                time.sleep(0.25)
            if not ca_ready:
                warning(f"Proxy CA not found yet at {proxy_ca_path}")

        proxy_url = f"http://vibepod-proxy:{proxy_port}"
        merged_env.setdefault("HTTP_PROXY", proxy_url)
        merged_env.setdefault("HTTPS_PROXY", proxy_url)
        merged_env.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
        _ca = "/etc/vibepod-proxy-ca/mitmproxy-ca-cert.pem"
        merged_env.setdefault("NODE_EXTRA_CA_CERTS", _ca)
        merged_env.setdefault("REQUESTS_CA_BUNDLE", _ca)
        merged_env.setdefault("SSL_CERT_FILE", _ca)
        merged_env.setdefault("CURL_CA_BUNDLE", _ca)

        if proxy_ca_dir:
            extra_volumes[str(proxy_ca_dir)] = {"bind": "/etc/vibepod-proxy-ca", "mode": "ro"}

    info(f"Starting {selected_agent} with image {image}")
    container = manager.run_agent(
        agent=selected_agent,
        image=image,
        workspace=workspace_path,
        config_dir=config_dir,
        config_mount_path=spec.config_mount_path,
        env=merged_env,
        command=spec.command,
        auto_remove=bool(config.get("auto_remove", True)),
        name=name,
        version=__version__,
        network=network_name,
        extra_volumes=extra_volumes,
    )

    container.reload()
    if container.status != "running":
        recent = container.logs(tail=50).decode("utf-8", errors="replace")
        error("Container exited immediately after start.")
        if recent.strip():
            print(recent)
        raise typer.Exit(1)

    if proxy_db_path is not None:
        container_ip = _get_container_ip(container, network_name)
        if container_ip:
            mapping_path = proxy_db_path.parent / "containers.json"
            mapping_updated = _update_container_mapping(
                mapping_path, container_ip, container.id, container.name, selected_agent
            )
            if not mapping_updated:
                warning(
                    f"Could not write proxy container mapping at {mapping_path}. "
                    "Fix proxy directory permissions to restore container attribution."
                )

    if detach:
        success(f"Started {container.name}")
        return

    log_cfg = config.get("logging", {})
    log_enabled = bool(log_cfg.get("enabled", True))
    log_db_path = Path(str(log_cfg.get("db_path", "~/.config/vibepod/logs.db"))).expanduser().resolve()

    logger = SessionLogger(log_db_path, enabled=log_enabled)
    logger.open_session(
        agent=selected_agent,
        image=image,
        workspace=str(workspace_path),
        container_id=container.id,
        container_name=container.name,
        vibepod_version=__version__,
    )

    exit_reason = "normal"
    warning("Attached to container. Use Ctrl+C to stop.")
    try:
        manager.attach_interactive(container, logger=logger)
    except KeyboardInterrupt:
        exit_reason = "keyboard_interrupt"
        info("Stopping container...")
        container.stop(timeout=10)
        success("Stopped")
    except Exception:
        exit_reason = "error"
        raise
    finally:
        logger.close_session(exit_reason)
