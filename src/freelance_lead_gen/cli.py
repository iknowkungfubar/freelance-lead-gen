"""CLI entry points for the freelance lead generation system.

Provides Click commands for all pipeline operations:

- ``init``: Database initialisation and schema creation
- ``discover``: Run the discovery phase
- ``pipeline``: Run the full pipeline
- ``review``: Review pending drafts in the TUI
- ``list``: List opportunities with optional filters
- ``stats``: Show pipeline statistics
- ``serve``: Start the terminal UI
"""

from __future__ import annotations as _annotations

import asyncio
import sys

import click
import structlog

from freelance_lead_gen.storage.database import close_db, init_db
from freelance_lead_gen.storage.repository import DatabaseError, OpportunityRepository

logger = structlog.get_logger(__name__)


# ── Main CLI group ────────────────────────────────────────────────────────────


@click.group()
@click.version_option()
def main() -> None:
    """Freelance Lead Gen — automated opportunity discovery & outreach preparation."""


# ── init ──────────────────────────────────────────────────────────────────────


@main.command()
def init() -> None:
    """Initialize the database and create the schema.

    Runs the database migration to create all required tables.  Safe to run
    multiple times — migrations are idempotent.
    """
    try:
        asyncio.run(_do_init())
        click.echo("Database initialised successfully.")
    except Exception as exc:
        click.echo(f"Failed to initialise database: {exc}", err=True)
        sys.exit(1)


async def _do_init() -> None:
    """Async implementation of the init command."""
    await init_db()


# ── discover ──────────────────────────────────────────────────────────────────


@main.command()
def discover() -> None:
    """Run the discovery phase.

    Searches all enabled platforms for new freelance opportunities and persists
    them to the database.
    """
    click.echo("Running discovery... (not yet implemented in CLI)")


# ── pipeline ──────────────────────────────────────────────────────────────────


@main.command()
@click.option(
    "--discover/--no-discover",
    default=True,
    help="Run the discovery phase as part of the pipeline",
)
def pipeline(discover: bool) -> None:
    """Run the full pipeline.

    Executes all phases in sequence: discovery (optional), qualification,
    personalisation, verification, and human-in-the-loop review.
    """
    click.echo("Running pipeline... (not yet implemented in CLI)")


# ── review ────────────────────────────────────────────────────────────────────


@main.command()
def review() -> None:
    """Review pending drafts.

    Opens the terminal UI focused on the review queue, showing drafts that
    need human approval.
    """
    click.echo("Opening review queue... (not yet implemented in CLI)")


# ── list ──────────────────────────────────────────────────────────────────────


@main.command(name="list")
@click.option("--status", "-s", default=None, help="Filter by pipeline status")
@click.option("--platform", "-p", default=None, help="Filter by source platform")
@click.option("--limit", "-l", type=int, default=50, help="Maximum results to show")
def list_opportunities(
    status: str | None,
    platform: str | None,
    limit: int,
) -> None:
    """List opportunities with optional filters.

    Displays opportunities in a table with their status, platform, title,
    and score.  Results can be filtered by status, platform, and limited in
    count.
    """
    click.echo("Listing opportunities... (not yet implemented in CLI)")


# ── stats ─────────────────────────────────────────────────────────────────────


@main.command()
def stats() -> None:
    """Show pipeline statistics.

    Displays aggregate counts of opportunities by pipeline status, platform
    breakdown, and other summary metrics.  Requires a working database.
    """
    try:
        asyncio.run(_do_stats())
    except (RuntimeError, DatabaseError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"Unexpected error: {exc}", err=True)
        sys.exit(1)


async def _do_stats() -> None:
    """Async implementation of the stats command."""
    engine = await init_db()
    repo = OpportunityRepository()

    stats_data = await repo.get_stats()
    platform_counts = await repo.get_platform_counts()

    click.echo(f"\n{'='*40}")
    click.echo("  Pipeline Statistics")
    click.echo(f"{'='*40}")
    click.echo(f"  Total leads:         {stats_data.get('total', 0)}")
    click.echo(f"  Discovered:          {stats_data.get('discovered', 0)}")
    click.echo(f"  Qualified:           {stats_data.get('qualified', 0)}")
    click.echo(f"  Drafted:             {stats_data.get('drafted', 0)}")
    click.echo(f"  Reviewed:            {stats_data.get('reviewed', 0)}")
    click.echo(f"  Submitted:           {stats_data.get('submitted', 0)}")
    click.echo(f"  Archived:            {stats_data.get('archived', 0)}")
    click.echo(f"  Rejected:            {stats_data.get('rejected', 0)}")
    click.echo(f"{'='*40}")
    click.echo(f"  Platforms:           {len(platform_counts)}")
    for pname, pcount in sorted(platform_counts.items()):
        click.echo(f"    {pname}: {pcount}")
    click.echo(f"{'='*40}\n")


# ── serve ─────────────────────────────────────────────────────────────────────


@main.command()
def serve() -> None:
    """Start the terminal UI.

    Launches the Textual-based terminal interface for interactive pipeline
    management, lead review, and dashboard monitoring.
    """
    click.echo("Starting TUI... (not yet implemented in CLI)")
