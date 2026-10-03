"""`vp sandbox` — inspect the opt-in agent process sandbox."""

from __future__ import annotations

from typing import Annotated

import typer

from vibepod.core.agents import get_agent_spec, resolve_agent_name
from vibepod.core.config import get_config
from vibepod.core.sandbox import (
    SandboxError,
    check_syd_prerequisites,
    render_syd_policy,
    resolve_sandbox_type,
    syd_settings,
    writable_targets,
)
from vibepod.utils.console import error, info, success

app = typer.Typer(help="Inspect the agent process sandbox (Sydbox)", no_args_is_help=True)


@app.command("policy")
def policy_cmd(
    agent: Annotated[
        str | None,
        typer.Argument(help="Agent to render the policy for (default: default_agent)"),
    ] = None,
) -> None:
    """Print the syd policy `vp run --sandbox syd` would apply.

    The launch policy also covers the agent's extra volume mounts and, with
    --acp, the workspace's host-path binds.
    """
    config = get_config()
    selected_input = agent or str(config.get("default_agent", "claude"))
    selected = resolve_agent_name(selected_input)
    if selected is None:
        error(f"Unknown agent '{selected_input}'.")
        raise typer.Exit(1)
    spec = get_agent_spec(selected)
    agent_env = config.get("agents", {}).get(selected, {}).get("env", {})
    env = {**spec.extra_env, **{str(k): str(v) for k, v in agent_env.items()}}
    try:
        settings = syd_settings(config)
    except SandboxError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(
        render_syd_policy(
            agent=selected,
            workspace_paths=["/workspace"],
            writable_paths=writable_targets(
                config_mount_path=spec.config_mount_path,
                env=env,
                volumes=[],
            ),
            log_level=settings["log_level"],
            rules=settings["rules"],
        ),
        nl=False,
    )


@app.command("check")
def check_cmd() -> None:
    """Check that the syd sandbox prerequisites are in place."""
    config = get_config()
    try:
        configured = resolve_sandbox_type(config)
        prerequisites = check_syd_prerequisites(config)
    except SandboxError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    success(f"syd binary: {prerequisites.binary} (static, {prerequisites.arch})")
    info(f"Configured sandbox.type: {configured}")
    if configured == "none":
        info("Enable it per run with `vp run --sandbox syd`, or set sandbox.type: syd.")
