"""Main Typer application for VibePod."""

from __future__ import annotations

import typer

from vibepod.commands import config, list_cmd, logs, run, stop, update

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


@app.command("c", hidden=True)
def alias_claude() -> None:
    """Alias for `vp run claude`."""
    run.run(agent="claude")


@app.command("g", hidden=True)
def alias_gemini() -> None:
    """Alias for `vp run gemini`."""
    run.run(agent="gemini")


@app.command("o", hidden=True)
def alias_opencode() -> None:
    """Alias for `vp run opencode`."""
    run.run(agent="opencode")


@app.command("d", hidden=True)
def alias_devstral() -> None:
    """Alias for `vp run devstral`."""
    run.run(agent="devstral")


@app.command("a", hidden=True)
def alias_auggie() -> None:
    """Alias for `vp run auggie`."""
    run.run(agent="auggie")


@app.command("p", hidden=True)
def alias_copilot() -> None:
    """Alias for `vp run copilot`."""
    run.run(agent="copilot")


@app.command("x", hidden=True)
def alias_codex() -> None:
    """Alias for `vp run codex`."""
    run.run(agent="codex")


@app.command("ui", hidden=True)
def alias_ui() -> None:
    """Alias for `vp logs ui`."""
    logs.logs_ui()


def main() -> None:
    """CLI entrypoint."""
    app()


if __name__ == "__main__":
    main()
