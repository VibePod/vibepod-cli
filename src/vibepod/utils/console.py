"""Console output utilities."""

from __future__ import annotations

from rich.console import Console

console = Console()


def info(message: str) -> None:
    console.print(f"[cyan]{message}[/cyan]")


def success(message: str) -> None:
    console.print(f"[green]{message}[/green]")


def warning(message: str) -> None:
    console.print(f"[yellow]{message}[/yellow]")


def error(message: str) -> None:
    console.print(f"[red]{message}[/red]")
