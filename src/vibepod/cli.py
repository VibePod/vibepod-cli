"""Main Typer application for VibePod."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from vibepod.commands import config, doctor, list_cmd, logs, proxy, run, stop, update
from vibepod.constants import AGENT_SHORTCUTS, SUPPORTED_AGENTS

app = typer.Typer(
    name="vp",
    help="VibePod - One CLI for all AI coding agents",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

app.command(
    name="run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)(run.run)
app.command(name="stop")(stop.stop)
app.command(name="list")(list_cmd.list_agents)
app.command(name="version")(update.version)

app.add_typer(logs.app, name="logs")
app.add_typer(config.app, name="config")
app.add_typer(proxy.app, name="proxy")
app.add_typer(doctor.app, name="doctor")


def _register_run_alias(command_name: str, agent_name: str) -> None:
    def _alias(
        workspace: Annotated[
            Path, typer.Option("-w", "--workspace", help="Workspace directory")
        ] = Path("."),
        pull: Annotated[
            bool, typer.Option("--pull", help="Pull latest image before run")
        ] = False,
        detach: Annotated[
            bool, typer.Option("-d", "--detach", "--detached", help="Run container in background")
        ] = False,
        prompt: Annotated[
            str | None,
            typer.Option(
                "--prompt",
                help="Run a single prompt in the agent's non-interactive mode",
            ),
        ] = None,
        env: Annotated[
            list[str] | None,
            typer.Option("-e", "--env", help="Environment variable KEY=VALUE", show_default=False),
        ] = None,
        name: Annotated[
            str | None, typer.Option("--name", help="Custom container name")
        ] = None,
        network: Annotated[
            str | None,
            typer.Option(
                "--network",
                help="Additional Docker network to connect the container to",
            ),
        ] = None,
        paste_images: Annotated[
            bool,
            typer.Option(
                "--paste-images",
                help="Enable image pasting via X11 clipboard (requires DISPLAY to be set)",
            ),
        ] = False,
        ikwid: Annotated[
            bool,
            typer.Option(
                "--ikwid",
                help="I Know What I'm Doing: enable auto-approval / skip permission prompts",
            ),
        ] = False,
    ) -> None:
        run.run(
            agent=agent_name,
            workspace=workspace,
            pull=pull,
            detach=detach,
            prompt=prompt,
            env=env,
            name=name,
            network=network,
            paste_images=paste_images,
            ikwid=ikwid,
        )

    _alias.__name__ = f"alias_{command_name}"
    _alias.__doc__ = f"Alias for `vp run {agent_name}`."
    app.command(
        command_name,
        hidden=True,
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )(_alias)


@app.command("ui", hidden=True)
def alias_ui() -> None:
    """Alias for `vp logs start`."""
    logs.logs_start()


for shortcut, agent in AGENT_SHORTCUTS.items():
    _register_run_alias(shortcut, agent)

for agent in SUPPORTED_AGENTS:
    _register_run_alias(agent, agent)


def main() -> None:
    """CLI entrypoint."""
    app()


if __name__ == "__main__":
    main()
