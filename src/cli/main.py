"""CLI interface for Quclaw using Typer."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from cli.chat import chat_command
from cli.server import server_command
from utils.config import Config


app = typer.Typer(
    name="Quclaw",
    help="Quclaw: Personal AI Assistant",
    no_args_is_help=True,
    add_completion=True,
)

console = Console()


def workspace_callback(ctx: typer.Context, workspace: str) -> Path:
    """Store workspace path in context for later use."""
    workspace_path = Path(workspace).expanduser().resolve()
    ctx.ensure_object(dict)
    ctx.obj["workspace"] = workspace_path
    return workspace_path


@app.callback()
def main(
    ctx: typer.Context,
    workspace: str = typer.Option(
        "default_workspace",
        "--workspace",
        "-w",
        help="Path to workspace directory",
        callback=workspace_callback,
    ),
) -> None:
    """Configuration is loaded from workspace/config.user.json by default."""
    workspace_path = ctx.obj["workspace"]
    config_file = Config.find_user_config_path(workspace_path)

    if config_file is None:
        console.print(
            "[yellow]No configuration found. Expected "
            "config.user.json (or legacy config.user.yaml).[/yellow]"
        )
        raise typer.Exit(1)

    try:
        cfg = Config.load(workspace_path)
        ctx.obj["config"] = cfg
    except Exception as e:
        console.print(f"[red]Error loading config: {e}[/red]")
        raise typer.Exit(1)



@app.command("chat")
def chat(
    ctx: typer.Context,
    agent: Annotated[
        str | None,
        typer.Option(
            "--agent",
            "-a",
            help="Agent ID to use (overrides default_agent from config)",
        ),
    ] = None,
) -> None:
    """Start interactive chat session."""
    config = ctx.obj.get("config")
    if config is None:
        console.print("[red]Configuration not loaded[/red]")
        raise typer.Exit(1)

    chat_command(ctx, agent_id=agent)


@app.command("server")
def serve(ctx: typer.Context) -> None:
    """Start channel workers and serve configured platforms."""
    server_command(ctx)


if __name__ == "__main__":
    app()
