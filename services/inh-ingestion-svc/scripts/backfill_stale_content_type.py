#!/usr/bin/env python3
"""Operator entry point: backfill the stale `text/markdown` `content_type`
left on documents stored before #208's MCP upload-fallback fix (#288).

Background
----------
#208 changed the MCP `upload_document` fallback for an unrecognized/absent
filename extension from `text/markdown` to `text/plain` -- see
`_default_upload_content_type` in
`services/inh-public-api-svc/src/mcp_server/server.py`. That fix only
affects NEW uploads. A document ingested before it keeps the stale label,
and `content_type` is not cosmetic: it is an indexed Postgres column,
returned to callers as `mime_type`, denormalized onto every Weaviate chunk,
and drives the chunking-hint dispatch (`FILE_TYPE_REGISTRY.chunking_hint`).
A caller filtering `content_type = 'text/markdown'` to find their
documentation still gets Dockerfiles and tarball names back for anything
ingested pre-fix.

No re-indexing required
------------------------
Established while fixing #208 and re-confirmed here: the `txt` and
`markdown` registry entries both declare `extractor="text_passthrough"` and
`chunking_hint="prose"`. A document chunks byte-for-byte identically whether
its `content_type` reads `text/plain` or `text/markdown`, so this script
only ever corrects the STORED LABEL -- it never touches `document_chunks`
content, never re-embeds, and never re-runs extraction.

Why this is a plain script, not a SQL migration
------------------------------------------------
This is a DATA correction (existing rows get a different value in an
existing column), not a schema change -- no table, column, index, or
constraint is added, altered, or dropped. A `CREATE OR REPLACE FUNCTION`
migration doing the derivation in SQL (the shape this backfill originally
shipped as) duplicates `FILE_TYPE_REGISTRY` into a hand-maintained SQL
`CASE` -- the day someone adds an extension to the registry, the SQL
silently keeps answering the old way, and a future backfill run (or anyone
copying that CASE statement as a reference) writes wrong data with nothing
to catch it. Doing the derivation here in Python instead lets it import and
call the SAME registry functions
(`inh_contracts.file_types.get_spec_for_extension` /
`.mime_type_for_extension`) that `_default_upload_content_type` itself
calls -- one source of truth, and a pure function plain `pytest` can cover
with zero database setup (see `tests/test_backfill_stale_content_type.py`).

Determinable vs. not -- read this before changing the derivation
-------------------------------------------------------------------
`derive_confident_content_type` below is a DELIBERATELY STRICTER cousin of
`_default_upload_content_type`. The upload-time function must always return
something (it is picking a default for a brand new document with nothing
else to go on), so it falls back to `text/plain` for an extensionless or
unregistered-extension filename. This backfill has a different job:
correcting an EXISTING label, which demands proof, not a best guess. For an
extensionless name (`Dockerfile`, `Makefile`, `README`) or an unregistered
extension (`archive.tar.gz`, whose derived extension is the unregistered
`.gz`) or a dotfile whose "extension" is unregistered (`.gitignore` --
takes the extension branch per #288's own note, but `.gitignore` is not in
`FILE_TYPE_REGISTRY`), there is no way to tell a genuine pre-#208 fallback
victim apart from a caller who explicitly, correctly declared
`content_type="text/markdown"` for that exact filename. Writing `text/plain`
over either case indiscriminately would repeat the precise mistake #208
fixed -- a confidently WRONG label -- just aimed the other way. These rows
are the #288 issue's own headline examples, and per this script's design
they are left untouched, on purpose, forever (unless a human resolves the
ambiguity out of band). What DOES get fixed: a stale `text/markdown` row
whose filename carries an extension `FILE_TYPE_REGISTRY` maps to a
DIFFERENT, specific MCP-eligible type (e.g. `notes.csv`, `script.py`) --
there is no plausible reading of "text/markdown" for those.

Idempotent, batched, re-runnable
----------------------------------
Rows are paged by primary key (`id > cursor`, `ORDER BY id`) rather than
`OFFSET`, so a partially-run backfill can be safely resumed, and pages
never skip or repeat rows as matching rows drop out of the `WHERE
content_type = 'text/markdown'` filter mid-run. Each document's Postgres
row and Weaviate chunks are updated together, right after each other, in
the same pass -- no separate script an operator has to remember to run
second in the right order. A repeat run is a no-op: an already-corrected
row no longer matches `content_type = 'text/markdown'`, and a
not-determinable row is skipped identically every time (see
`plan_content_type_update`, which encodes this "already handled or
un-decidable -> None" rule as a pure, tested function).

Usage (run from the repository root)
-------------------------------------

    uv --project services/inh-ingestion-svc run python \\
        services/inh-ingestion-svc/scripts/backfill_stale_content_type.py \\
        --dry-run

    uv --project services/inh-ingestion-svc run python \\
        services/inh-ingestion-svc/scripts/backfill_stale_content_type.py

    # Smaller batches against a live, low-headroom deployment:
    uv --project services/inh-ingestion-svc run python \\
        services/inh-ingestion-svc/scripts/backfill_stale_content_type.py \\
        --batch-size 50

Exit code is 1 if any Postgres row failed to sync into Weaviate (the two
stores would then disagree until re-run), 0 otherwise. `--dry-run` never
exits 1 -- it only reports.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Deliberately the ONLY import at module scope beyond the stdlib: this keeps
# `derive_confident_content_type` / `plan_content_type_update` importable by
# `tests/test_backfill_stale_content_type.py` with no database, no Weaviate
# client, and no settings/env-var setup -- a pure function needs no I/O to
# unit test, and importing DatabaseService/WeaviateService at module scope
# would force every test importing this file to pay for (and mock) both.
from inh_contracts.file_types import get_spec_for_extension, mime_type_for_extension

SCRIPT_DIR = Path(__file__).resolve().parent
SERVICE_ROOT = SCRIPT_DIR.parent  # .../services/inh-ingestion-svc
REPO_ROOT = SERVICE_ROOT.parent.parent

# The label #208 set out to correct. Matches processed_documents.content_type
# exactly -- see scripts/migrations/000_initial_schema.sql.
STALE_CONTENT_TYPE = "text/markdown"

DEFAULT_BATCH_SIZE = 200


def derive_confident_content_type(filename: str) -> str | None:
    """The content_type `filename` PROVABLY resolves to, or None when it
    cannot be determined with confidence (#288).

    Mirrors `_default_upload_content_type`'s extension-resolution steps
    (`inh_contracts.get_spec_for_extension` -> mcp-surface check ->
    `mime_type_for_extension`) so the answer, when there IS one, is
    identical to what today's (post-#208) upload code would have assigned
    -- but returns None instead of a fallback guess whenever that function
    would have fallen through to its `text/plain` default. See the module
    docstring's "Determinable vs. not" section for why: a fallback default
    picked for a BRAND NEW upload with nothing else to go on is not evidence
    that an EXISTING row's `text/markdown` label is wrong.

    Returns None for:
      - an extensionless filename (`Dockerfile`, `Makefile`, `README`)
      - a filename whose extension is not in `FILE_TYPE_REGISTRY` (including
        a dotfile like `.gitignore`, whose derived extension is itself
        `.gitignore` -- present because it "contains a dot and takes the
        extension branch" per #288, but not a registered one)
      - a registered extension that is not MCP-eligible (e.g. `.pdf`,
        `.json`, `.docx`) -- only #208's own MCP-transported bug could have
        produced this row's stale label, so a REST-only type is exactly as
        undecidable as an unregistered one here
    """
    if "." not in filename:
        return None
    extension = "." + filename.rsplit(".", 1)[-1]
    spec = get_spec_for_extension(extension)
    if spec is None or "mcp" not in spec.surfaces:
        return None
    return mime_type_for_extension(spec, extension)


def plan_content_type_update(current_content_type: str, filename: str) -> str | None:
    """The new content_type to write for a row currently labelled
    `current_content_type`, or None if no write is needed (#288).

    None covers three cases the caller does not need to distinguish to
    decide whether to write:
      1. `current_content_type` is not the stale marker at all -- nothing
         to backfill (defensive; the caller's own query already filters to
         `STALE_CONTENT_TYPE`, but this makes the function correct on its
         own rather than trusting that filter silently).
      2. `derive_confident_content_type` cannot determine a type -- see its
         docstring.
      3. The determinable type IS `current_content_type` already (a
         genuinely markdown-named file, e.g. `notes.md`, correctly labelled
         `text/markdown` from the start) -- a real answer, just not a
         change.

    This is also the idempotency guarantee in pure-function form: once a
    row has been updated to the value this returns, calling it again with
    that NEW value as `current_content_type` returns None (case 1) --
    exactly mirroring why a second script run leaves an already-fixed row
    alone (see `tests/test_backfill_stale_content_type.py`'s idempotency
    test, which exercises this without touching a database).
    """
    if current_content_type != STALE_CONTENT_TYPE:
        return None
    derived = derive_confident_content_type(filename)
    if derived is None or derived == current_content_type:
        return None
    return derived


@dataclass
class BackfillStats:
    """Counts for the operator summary printed at the end of a run."""

    scanned: int = 0
    updated: int = 0
    not_determinable: int = 0
    already_correct: int = 0
    weaviate_chunks_synced: int = 0
    weaviate_failures: list[str] = field(default_factory=list)


def _load_repo_env() -> None:
    """Load `REPO_ROOT/.env` into `os.environ` before any service import.

    Mirrors `scripts/reindex_orphaned_document.py` /
    `scripts/check_index_consistency.py` (same python-dotenv-first,
    minimal-parser-fallback approach) so every operator script behaves
    identically when python-dotenv isn't installed in a given service venv.
    """
    import os

    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import dotenv_values  # type: ignore[import-not-found]

        for key, value in dotenv_values(env_path).items():
            if value is not None and key not in os.environ:
                os.environ[key] = value
    except ImportError:
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


async def _run_backfill(
    database,
    weaviate,
    *,
    batch_size: int,
    dry_run: bool,
    logger,
) -> BackfillStats:
    """Page through stale rows and correct each one (Postgres + Weaviate).

    Keyset-paginated on `id` (never OFFSET): a batch is `WHERE content_type
    = 'text/markdown' AND id > cursor ORDER BY id LIMIT batch_size`. This is
    what makes the loop both SAFE under concurrent writes (OFFSET drifts
    when rows are updated out from under it mid-scan; a keyset cursor does
    not) and BATCHED without long locks (each page is a short, separate
    read; each row's UPDATE is a single-row, primary-key write, never a
    multi-row statement holding a wider lock).
    """
    from sqlalchemy import and_

    stats = BackfillStats()
    cursor_id = 0

    while True:
        with database.get_session() as session:
            query = (
                database.processed_documents.select()
                .where(
                    and_(
                        database.processed_documents.c.content_type == STALE_CONTENT_TYPE,
                        database.processed_documents.c.id > cursor_id,
                    )
                )
                .order_by(database.processed_documents.c.id)
                .limit(batch_size)
            )
            rows = [dict(r._mapping) for r in session.execute(query).fetchall()]

        if not rows:
            break

        for row in rows:
            cursor_id = row["id"]
            stats.scanned += 1
            filename = row["original_filename"]
            new_content_type = plan_content_type_update(row["content_type"], filename)

            if new_content_type is None:
                derived = derive_confident_content_type(filename)
                if derived is None:
                    stats.not_determinable += 1
                    logger.info(
                        "content_type not determinable from filename -- leaving row untouched",
                        document_id=row["document_id"],
                        filename=filename,
                        current_content_type=row["content_type"],
                    )
                else:
                    stats.already_correct += 1
                continue

            logger.info(
                (
                    "Backfilling stale content_type"
                    if not dry_run
                    else "Would backfill stale content_type"
                ),
                document_id=row["document_id"],
                filename=filename,
                old_content_type=row["content_type"],
                new_content_type=new_content_type,
                dry_run=dry_run,
            )

            if dry_run:
                stats.updated += 1
                weaviate_count = await weaviate.update_chunks_content_type(
                    document_id=row["document_id"],
                    workspace_id=row["workspace_id"],
                    user_id=row["user_id"],
                    content_type=new_content_type,
                    dry_run=True,
                )
                stats.weaviate_chunks_synced += weaviate_count
                continue

            # Postgres first: a Weaviate failure after this leaves the two
            # stores briefly disagreeing (recorded in weaviate_failures and
            # surfaced via a nonzero exit code), which a re-run resolves --
            # the WHERE clause above no longer matches this row's NEW
            # content_type, but plan_content_type_update recomputes to the
            # SAME value it already holds, so the retry is a genuine no-op
            # once Weaviate is the only thing still behind. The reverse
            # order (Weaviate first) would risk leaving Postgres -- the
            # source of truth `mime_type` reads from -- silently wrong on a
            # failure instead.
            with database.get_session() as session:
                session.execute(
                    database.processed_documents.update()
                    .where(database.processed_documents.c.id == row["id"])
                    .values(content_type=new_content_type, updated_at=datetime.now(UTC))
                )
            stats.updated += 1

            try:
                weaviate_count = await weaviate.update_chunks_content_type(
                    document_id=row["document_id"],
                    workspace_id=row["workspace_id"],
                    user_id=row["user_id"],
                    content_type=new_content_type,
                )
                stats.weaviate_chunks_synced += weaviate_count
            except Exception as e:  # noqa: BLE001 -- record and keep going
                stats.weaviate_failures.append(row["document_id"])
                logger.error(
                    "Postgres updated but Weaviate sync failed -- stores now disagree until re-run",
                    document_id=row["document_id"],
                    error=str(e),
                    exc_info=True,
                )

    return stats


async def _main(args: argparse.Namespace) -> int:
    _load_repo_env()
    sys.path.insert(0, str(SERVICE_ROOT))

    import structlog

    from src.config.settings import get_settings
    from src.services.database import DatabaseService
    from src.services.weaviate import WeaviateService

    logger = structlog.get_logger(__name__)
    settings = get_settings()

    database = DatabaseService(settings)
    database.connect()

    weaviate = WeaviateService(settings)
    weaviate.connect()

    if not weaviate.is_connected():
        print("ERROR: could not connect to Weaviate -- aborting before touching any document.")
        database.disconnect()
        return 1

    try:
        stats = await _run_backfill(
            database,
            weaviate,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            logger=logger,
        )
    finally:
        weaviate.disconnect()
        database.disconnect()

    prefix = "DRY RUN: would have" if args.dry_run else "Backfill complete:"
    print(
        f"{prefix} scanned={stats.scanned} updated={stats.updated} "
        f"weaviate_chunks_synced={stats.weaviate_chunks_synced} "
        f"not_determinable={stats.not_determinable} "
        f"already_correct={stats.already_correct} "
        f"weaviate_failures={len(stats.weaviate_failures)}"
    )
    if stats.weaviate_failures:
        print(f"  Postgres/Weaviate now disagree for: {', '.join(stats.weaviate_failures)}")
        print("  Re-run this script to retry -- Postgres is already correct for these.")

    return 1 if stats.weaviate_failures else 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Rows fetched per page (default: {DEFAULT_BATCH_SIZE}). Each row is still "
        "written as its own single-row statement regardless of this value.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change (Postgres rows and Weaviate chunk counts) "
        "without writing anything.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse_args())))
