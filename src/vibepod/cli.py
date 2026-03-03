"""Main Typer application for VibePod."""

from __future__ import annotations

import typer

from vibepod.commands import config, list_cmd, logs, proxy, run, stop, update
from vibepod.constants import AGENT_SHORTCUTS, SUPPORTED_AGENTS

app = typer.Typer(
    name="vp",
    help="VibePod - One CLI for all AI coding agents",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

app.command(name="run")(run.run)
app.command(name="stop")(stop.stop)
app.command(name="list")(list_cmd.list_agents)
app.command(name="version")(update.version)

app.add_typer(logs.app, name="logs")
app.add_typer(config.app, name="config")
app.add_typer(proxy.app, name="proxy")


def _register_run_alias(command_name: str, agent_name: str) -> None:
    def _alias(bound_agent: str = agent_name) -> None:
        run.run(agent=bound_agent)

    _alias.__name__ = f"alias_{command_name}"
    _alias.__doc__ = f"Alias for `vp run {agent_name}`."
    app.command(command_name, hidden=True)(_alias)


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
