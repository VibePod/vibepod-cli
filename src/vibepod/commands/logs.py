"""Logs subcommands."""

from __future__ import annotations

import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Annotated, Any

import typer

from vibepod.constants import EXIT_DOCKER_NOT_RUNNING
from vibepod.core.config import get_config
from vibepod.core.docker import DockerClientError, DockerManager, _is_latest_tag
from vibepod.core.session_logger import SessionLogger
from vibepod.utils.console import error, info, success, warning

app = typer.Typer(help="View logs and traffic UI")


_HEALTH_TIMEOUT = 30
_HEALTH_INTERVAL = 0.5


def _wait_for_datasette(port: int) -> bool:
    url = f"http://localhost:{port}/"
    deadline = time.monotonic() + _HEALTH_TIMEOUT
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)  # noqa: S310
            return True
        except urllib.error.HTTPError:
            return True  # server responded — healthy enough
        except (urllib.error.URLError, OSError):
            time.sleep(_HEALTH_INTERVAL)
    return False


def _log_db_path(config: dict[str, Any]) -> Path:
    log_cfg = config.get("logging", {})
    return Path(str(log_cfg.get("db_path", "~/.config/vibepod/logs.db"))).expanduser().resolve()


@app.command("start")
def logs_start(
    port: Annotated[int | None, typer.Option("--port", help="Datasette host port")] = None,
    no_open: Annotated[bool, typer.Option("--no-open", help="Do not open browser")] = False,
) -> None:
    """Start or reuse Datasette for session and proxy logs."""
    config = get_config()
    log_cfg = config.get("logging", {})
    proxy_cfg = config.get("proxy", {})

    datasette_image = str(log_cfg.get("image", "vibepod/datasette:latest"))
    datasette_port = port if port is not None else int(log_cfg.get("ui_port", 8001))
    logs_db_path = Path(str(log_cfg.get("db_path", "~/.config/vibepod/logs.db"))).expanduser()
    proxy_db_path = Path(
        str(proxy_cfg.get("db_path", "~/.config/vibepod/proxy/proxy.db"))
    ).expanduser()

    try:
        manager = DockerManager()
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc

    if _is_latest_tag(datasette_image):
        info("Checking for datasette image updates…")
        updated = manager.pull_if_newer(datasette_image)
        if updated:
            info("New image available — restarting datasette")
            existing = manager.find_datasette()
            if existing:
                existing.remove(force=True)

    info(f"Starting Datasette on http://localhost:{datasette_port}")
    manager.ensure_datasette(
        image=datasette_image,
        logs_db_path=logs_db_path,
        proxy_db_path=proxy_db_path,
        port=datasette_port,
    )

    if _wait_for_datasette(datasette_port):
        success("Datasette is ready")
        if not no_open:
            webbrowser.open(f"http://localhost:{datasette_port}/-/dashboards")
    else:
        warning("Datasette did not become healthy in time — opening browser anyway")
        if not no_open:
            webbrowser.open(f"http://localhost:{datasette_port}/-/dashboards")


@app.command("stop")
def logs_stop(
    force: Annotated[bool, typer.Option("-f", "--force", help="Force stop")] = False,
) -> None:
    """Stop the Datasette container."""
    try:
        manager = DockerManager()
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc

    existing = manager.find_datasette()
    if not existing:
        warning("Datasette is not running")
        return

    existing.stop(timeout=0 if force else 10)
    success("Datasette stopped")


@app.command("status")
def logs_status() -> None:
    """Show Datasette container status."""
    try:
        manager = DockerManager()
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc

    existing = manager.find_datasette()
    if not existing:
        info("Datasette is not running")
        return

    existing.reload()
    info(f"Datasette container: {existing.name} ({existing.status})")


@app.command("show")
def logs_show(
    task_id: Annotated[str, typer.Argument(help="Task/run ID returned by `vp run --detach`")],
) -> None:
    """Print persisted logs for a detached task."""
    db_path = _log_db_path(get_config())
    session = SessionLogger.get_session(db_path, task_id)
    if session is None:
        error(f"Unknown task ID: {task_id}")
        raise typer.Exit(1)

    outputs = SessionLogger.get_outputs(db_path, task_id)
    if outputs:
        for row in outputs:
            print(row["content"], end="")
        return

    try:
        docker_logs = DockerManager().container_logs(str(session["container_id"]))
    except DockerClientError as exc:
        warning(f"No persisted output has been collected for this task: {exc}")
        return

    if docker_logs:
        print(docker_logs, end="")
        return

    info("No output has been collected for this task yet.")


@app.command("attach")
def logs_attach(
    task_id: Annotated[str, typer.Argument(help="Task/run ID returned by `vp run --detach`")],
) -> None:
    """Attach to a running detached task by task ID."""
    db_path = _log_db_path(get_config())
    session = SessionLogger.get_session(db_path, task_id)
    if session is None:
        error(f"Unknown task ID: {task_id}")
        raise typer.Exit(1)

    try:
        manager = DockerManager()
        container = manager.get_container(str(session["container_id"]))
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc

    info(f"Attached to {session['container_name']}. Use Ctrl+C to detach/stop.")
    try:
        manager.attach_interactive(container)
    except KeyboardInterrupt:
        info("Detached")


@app.command("collect", hidden=True)
def logs_collect(
    task_id: Annotated[str, typer.Argument(help="Task/run ID")],
    db_path: Annotated[Path | None, typer.Option("--db-path", help="Logs DB path")] = None,
) -> None:
    """Persist Docker logs for a detached task until the container exits."""
    config = get_config()
    resolved_db_path = (
        db_path.expanduser().resolve() if db_path is not None else _log_db_path(config)
    )
    session = SessionLogger.get_session(resolved_db_path, task_id)
    if session is None:
        raise typer.Exit(1)

    try:
        manager = DockerManager()
        container = manager.get_container(str(session["container_id"]))
        for chunk in container.logs(stream=True, follow=True, stdout=True, stderr=True):
            if isinstance(chunk, bytes):
                content = chunk.decode("utf-8", errors="replace")
            else:
                content = str(chunk)
            SessionLogger.append_output(
                resolved_db_path,
                session_id=task_id,
                content=content,
            )
        try:
            result = container.wait()
            status_code = result.get("StatusCode") if isinstance(result, dict) else None
            exit_reason = f"exit_{status_code}" if status_code is not None else "normal"
        except Exception:
            exit_reason = "normal"
        SessionLogger.close_session_by_id(
            resolved_db_path,
            session_id=task_id,
            exit_reason=exit_reason,
        )
    except Exception:
        SessionLogger.close_session_by_id(
            resolved_db_path,
            session_id=task_id,
            exit_reason="collector_error",
        )
        raise typer.Exit(1)


@app.command("ui", hidden=True)
def logs_ui(
    port: Annotated[int | None, typer.Option("--port", help="Datasette host port")] = None,
    no_open: Annotated[bool, typer.Option("--no-open", help="Do not open browser")] = False,
) -> None:
    """Alias for `vp logs start`."""
    logs_start(port=port, no_open=no_open)
