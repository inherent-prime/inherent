import json

from inh_cli.output import TableSpec, render, render_fields


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


def test_document_content_is_never_parsed_as_rich_markup(capsys) -> None:
    """Snippets carry arbitrary file content; Rich must not eat bracketed spans."""
    render(
        [{"snippet": "config uses [bold] and [/] markers"}],
        json_mode=False,
        table=TableSpec(("snippet",)),
    )

    output = capsys.readouterr().out
    assert "[bold]" in output
    assert "[/]" in output


def test_malformed_markup_still_renders(capsys) -> None:
    render(
        [{"snippet": "unclosed [tag and arr[0]"}],
        json_mode=False,
        table=TableSpec(("snippet",)),
    )

    assert "[tag" in capsys.readouterr().out


def test_render_fields_shows_every_value_on_its_own_row(capsys) -> None:
    """A wide record laid out horizontally elides every value at normal widths."""
    record = {
        "id": "e01b3b18-bcc1-4933-8bda-d4b0e37625c1",
        "name": "review-doc.md",
        "workspace_id": "ws_review",
        "mime_type": "application/octet-stream",
        "status": "processed",
        "metadata": None,
    }

    render_fields(record, json_mode=False)

    output = capsys.readouterr().out
    for value in ("e01b3b18-bcc1-4933-8bda-d4b0e37625c1", "review-doc.md", "processed"):
        assert value in output, f"{value!r} was truncated away"


def test_render_fields_json_mode_emits_only_json(capsys) -> None:
    record = {"id": "doc-1", "status": "processed"}

    render_fields(record, json_mode=True)

    captured = capsys.readouterr()
    assert json.loads(captured.out) == record
    assert captured.err == ""
