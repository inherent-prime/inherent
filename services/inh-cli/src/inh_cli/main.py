"""Typer entrypoint for the Inherent CLI."""

from __future__ import annotations

from typing import Annotated, Any

import click
import typer
from typer.core import TyperGroup

from inh_cli import __version__
from inh_cli.commands.connect import connect
from inh_cli.commands.documents import chunks, docs_app
from inh_cli.commands.identity import keys_app, whoami, workspaces_app
from inh_cli.commands.search import search
from inh_cli.stack import register as register_stack


class ExitCodeGroup(TyperGroup):
    """Reserve exit code 2 for an unavailable or unconfigured stack."""

    def main(self, *args: Any, **kwargs: Any) -> Any:
        # Force Click to raise instead of sys.exit, then map codes ourselves.
        # When standalone_mode is False, Click *returns* Exit's code rather
        # than raising, so integer results must become SystemExit too.
        kwargs["standalone_mode"] = False
        try:
            result = super().main(*args, **kwargs)
        except click.UsageError as error:
            error.show()
            raise SystemExit(1) from error
        except click.ClickException as error:
            error.show()
            raise SystemExit(error.exit_code) from error
        except click.exceptions.Exit as error:
            raise SystemExit(error.exit_code) from error
        if isinstance(result, int):
            raise SystemExit(result)
        return result


app = typer.Typer(
    cls=ExitCodeGroup,
    help="Manage and query an Inherent agent memory stack.",
    invoke_without_command=True,
    no_args_is_help=False,
)


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    json_mode: Annotated[
        bool, typer.Option("--json", help="Write machine-readable JSON to stdout.")
    ] = False,
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", help="Send X-Workspace-Id on API requests."),
    ] = None,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show the CLI version."),
    ] = False,
) -> None:
    """Store global output settings for subcommands."""

    del version
    ctx.ensure_object(dict)
    ctx.obj["json"] = json_mode
    ctx.obj["workspace"] = workspace


register_stack(app)
app.add_typer(docs_app, name="docs")
app.command("chunks")(chunks)
app.command("search")(search)
app.command("whoami")(whoami)
app.add_typer(workspaces_app, name="workspaces")
app.add_typer(keys_app, name="keys")
app.command("connect")(connect)
