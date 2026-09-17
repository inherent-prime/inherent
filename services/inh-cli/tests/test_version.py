import click
from click.testing import CliRunner as ClickRunner
from typer.testing import CliRunner

from inh_cli import __version__
from inh_cli.client import ClientError
from inh_cli.config import StackNotConfigured
from inh_cli.main import ExitCodeGroup, app


def test_version() -> None:
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_help_smoke() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "--json" in result.stdout
    assert "--version" in result.stdout


def test_global_json_switch_does_not_write_output() -> None:
    result = CliRunner().invoke(app, ["--json"])

    assert result.exit_code == 0
    assert result.stdout == ""


def test_bad_arguments_use_general_error_exit_code() -> None:
    result = CliRunner().invoke(app, ["--unknown"])

    assert result.exit_code == 1


def test_explicit_stack_exit_code_is_preserved() -> None:
    def stack_exit() -> None:
        raise click.exceptions.Exit(2)

    group = ExitCodeGroup(name="test")
    group.add_command(click.Command("stack", callback=stack_exit))

    assert ClickRunner().invoke(group, ["stack"]).exit_code == 2


def test_click_errors_use_the_general_error_exit_code() -> None:
    def fail() -> None:
        raise click.ClickException("bad")

    group = ExitCodeGroup(name="test")
    group.add_command(click.Command("fail", callback=fail))

    result = ClickRunner().invoke(group, ["fail"])

    assert result.exit_code == 1
    assert "bad" in result.stderr


def test_shared_domain_errors_render_without_tracebacks() -> None:
    def api_error() -> None:
        raise ClientError("bad key")

    def stack_error() -> None:
        raise StackNotConfigured("run inherent up")

    group = ExitCodeGroup(name="test")
    group.add_command(click.Command("api", callback=api_error))
    group.add_command(click.Command("stack", callback=stack_error))

    api = ClickRunner().invoke(group, ["api"])
    stack = ClickRunner().invoke(group, ["stack"])

    assert (api.exit_code, api.stderr) == (1, "Error: bad key\n")
    assert (stack.exit_code, stack.stderr) == (2, "Error: run inherent up\n")
