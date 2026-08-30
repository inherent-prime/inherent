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
    Console().print(rich_table)
