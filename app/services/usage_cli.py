"""Usage-ledger maintenance commands.

Usage::

    flask usage backfill [--batch-size N] [--limit N] [--dry-run] [--json]

The backfill copies historical ``Message`` token columns into the append-only
usage ledger. It never updates message rows, is safe to re-run, and processes
messages in bounded keyset pages.
"""

from __future__ import annotations

import json

import click
from flask import Flask

from app.services.usage_ledger import BACKFILL_DEFAULT_BATCH_SIZE, BACKFILL_MAX_BATCH_SIZE

EXIT_OK = 0
EXIT_ERROR = 2


def _write_json(data) -> None:
    click.echo(json.dumps(data, indent=2, sort_keys=True))


def _fail(message: str, as_json: bool) -> None:
    if as_json:
        _write_json({"error": message})
    else:
        click.echo(message, err=True)
    raise click.exceptions.Exit(EXIT_ERROR)


def register_usage_cli(app: Flask) -> None:
    """Register the ``usage`` CLI command group on ``app``."""

    @app.cli.group("usage")
    def usage_group():
        """Usage-ledger maintenance commands."""

    @usage_group.command("backfill")
    @click.option(
        "--batch-size",
        type=click.IntRange(1, BACKFILL_MAX_BATCH_SIZE),
        default=BACKFILL_DEFAULT_BATCH_SIZE,
        show_default=True,
        help="Messages processed per database batch.",
    )
    @click.option(
        "--limit",
        type=click.IntRange(min=0),
        default=None,
        help="Maximum messages to scan in this run (default: all).",
    )
    @click.option(
        "--dry-run",
        is_flag=True,
        help="Report what would be written without committing ledger rows.",
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def backfill(batch_size: int, limit: int | None, dry_run: bool, as_json: bool) -> int:
        """Backfill ledger rows from historical chat-message token counts."""
        from app.services.usage_ledger import backfill_from_messages

        try:
            result = backfill_from_messages(
                batch_size=batch_size,
                limit=limit,
                dry_run=dry_run,
            )
        except ValueError as exc:
            _fail(str(exc), as_json=as_json)

        if as_json:
            _write_json(result)
            return EXIT_OK

        action = "Dry run" if dry_run else "Backfill"
        click.echo(
            f"{action} complete: scanned={result['scanned']} "
            f"created={result['created']} "
            f"skipped_existing={result['skipped_existing']} "
            f"skipped_incomplete={result['skipped_incomplete']} "
            f"skipped_orphan={result['skipped_orphan']} "
            f"batches={result['batches']}"
        )
        return EXIT_OK


__all__ = ["EXIT_ERROR", "EXIT_OK", "register_usage_cli"]
