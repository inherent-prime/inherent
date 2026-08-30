import json

from inh_cli.output import TableSpec, render


def test_json_output_contains_only_parseable_json(capsys) -> None:
    data = [{"name": "local", "status": "ready"}]

    render(data, json_mode=True, table=TableSpec(("name", "status")))

    captured = capsys.readouterr()
    assert json.loads(captured.out) == data
    assert captured.err == ""


def test_table_output_uses_declared_columns(capsys) -> None:
    render(
        [{"name": "local", "ignored": "secret"}],
        json_mode=False,
        table=TableSpec(("name",), title="Stacks"),
    )

    output = capsys.readouterr().out
    assert "Stacks" in output
    assert "local" in output
    assert "secret" not in output
