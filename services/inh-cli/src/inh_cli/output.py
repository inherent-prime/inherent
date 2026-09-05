"""Shared human and machine-readable output rendering."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from rich.console import Console
from rich.table import Table


@dataclass(frozen=True)
class TableSpec:
    """Columns and optional title for Rich table output."""

    columns: tuple[str, ...]
    title: str | None = None


def render(data: Any, *, json_mode: bool, table: TableSpec) -> None:
    """Write either pure JSON or a table to stdout."""

    if json_mode:
        print(json.dumps(data, separators=(",", ":")))
        return

    rows: Sequence[dict[str, Any]] = data if isinstance(data, list) else [data]
    rich_table = Table(title=table.title)
    for column in table.columns:
        rich_table.add_column(column)
    for row in rows:
        rich_table.add_row(*(str(row.get(column, "")) for column in table.columns))
    # markup=False: cells carry document content. With Rich's default markup on,
    # a snippet containing "[bold]" or "[/]" had those spans silently deleted,
    # and a malformed tag raised MarkupError instead of printing the row.
    Console(markup=False).print(rich_table)


def render_fields(data: dict[str, Any], *, json_mode: bool, title: str | None = None) -> None:
    """Render one record as vertical field/value rows.

    A wide record rendered as one horizontal row elides every value at normal
    terminal widths, which defeats the point of a "show this thing" command.
    """

    if json_mode:
        print(json.dumps(data, separators=(",", ":")))
        return

    rich_table = Table(title=title)
    rich_table.add_column("field")
    rich_table.add_column("value")
    for key, value in data.items():
        rich_table.add_row(str(key), "" if value is None else str(value))
    Console(markup=False).print(rich_table)
