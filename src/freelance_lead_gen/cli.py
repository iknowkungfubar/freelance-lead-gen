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
import signal
import sys

import click
import structlog
from rich.console import Console
from rich.table import Column, Table

from freelance_lead_gen.agents.orchestrator import LeadGenOrchestrator
from freelance_lead_gen.config.settings import get_settings
from freelance_lead_gen.discovery.discovery_agent import DiscoveryAgent
from freelance_lead_gen.models.opportunity import LeadStatus
from freelance_lead_gen.storage.database import init_db
from freelance_lead_gen.storage.repository import DatabaseError, OpportunityRepository

logger = structlog.get_logger(__name__)
console = Console()


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
@click.option("--headless/--no-headless", default=True, help="Run browser in headless mode")
def discover(headless: bool) -> None:
    """Run the discovery phase.

    Searches all enabled platforms for new freelance opportunities and persists
    them to the database.
    """
    try:
        asyncio.run(_do_discover(headless=headless))
    except (RuntimeError, DatabaseError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"Unexpected error: {exc}", err=True)
        sys.exit(1)


async def _do_discover(headless: bool) -> None:
    """Async implementation of the discover command."""
    try:
        await init_db()
    except Exception as exc:
        click.echo(
            f"Database not initialised. Run `freelance-lead-gen init` first: {exc}",
            err=True,
        )
        return

    settings = get_settings()
    settings.browser.headless = headless
    agent = DiscoveryAgent(settings=settings)
    await agent.initialize()

    click.echo("Starting discovery cycle...")
    report = await agent.run_discovery_cycle()
    await agent.shutdown()

    # Display results.
    console.print("\n[bold]Discovery Complete[/bold]")
    console.print(f"  Platforms attempted:  {report.platforms_attempted}")
    console.print(f"  Platforms succeeded:  {report.platforms_succeeded}")
    console.print(f"  Total leads found:    [bold]{report.total_found}[/bold]")
    console.print(f"  New leads persisted:  [bold green]{report.total_new}[/bold green]")
    console.print(f"  Errors:               {report.total_errors}")
    if report.elapsed_seconds is not None:
        console.print(f"  Elapsed time:         {report.elapsed_seconds:.1f}s")

    if report.per_platform:
        table = Table(
            Column("Platform"),
            Column("Found", justify="right"),
            Column("New", justify="right"),
            Column("Succeeded"),
            title="Per-Platform Breakdown",
            title_style="bold",
        )
        for pname, pdata in sorted(report.per_platform.items()):
            succeeded = "✓" if pdata.get("failed", 0) == 0 else "✗"
            table.add_row(pname, str(pdata.get("found", 0)), str(pdata.get("new", 0)), succeeded)
        console.print(table)


# ── pipeline ──────────────────────────────────────────────────────────────────


@main.command()
@click.option(
    "--discover/--no-discover",
    default=True,
    help="Run the discovery phase as part of the pipeline",
)
@click.option(
    "--headless/--no-headless",
    default=True,
    help="Run browser in headless mode",
)
def pipeline(discover: bool, headless: bool) -> None:
    """Run the full pipeline.

    Executes all phases in sequence: discovery (optional), qualification,
    personalisation, verification, and human-in-the-loop review.
    """
    try:
        asyncio.run(_do_pipeline(run_discovery=discover, headless=headless))
    except (RuntimeError, DatabaseError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"Unexpected error: {exc}", err=True)
        sys.exit(1)


async def _do_pipeline(run_discovery: bool, headless: bool) -> None:
    """Async implementation of the pipeline command."""
    try:
        await init_db()
    except Exception as exc:
        click.echo(
            f"Database not initialised. Run `freelance-lead-gen init` first: {exc}",
            err=True,
        )
        return

    settings = get_settings()
    settings.browser.headless = headless

    console.print("[bold]Initialising pipeline...[/bold]")

    orchestrator = LeadGenOrchestrator(settings=settings)
    await orchestrator.initialize()

    try:
        report = await orchestrator.run_full_pipeline(
            run_discovery=run_discovery,
        )
    finally:
        await orchestrator.shutdown()

    # Display report.
    summary = report.summary
    console.print("\n[bold]Pipeline Complete[/bold]")
    console.print(f"  Success:              {'[green]Yes[/green]' if summary['success'] else '[red]No[/red]'}")
    console.print(f"  Phases completed:     {', '.join(report.phases_completed) if report.phases_completed else '[dim]none[/dim]'}")
    if report.phases_failed:
        console.print(f"  Phases failed:        [red]{', '.join(report.phases_failed)}[/red]")
    console.print(f"  Leads discovered:     {summary['discovered']}")
    console.print(f"  Leads qualified:      {summary['qualified']}")
    console.print(f"  Drafts generated:     {summary['drafted']}")
    console.print(f"  Verified pass:        {summary['verified_pass']}")
    console.print(f"  Verified fail:        {summary['verified_fail']}")
    console.print(f"  Reviewed:             {summary['reviewed']}")
    console.print(f"  Errors:               {summary['errors']}")
    if summary.get("elapsed_seconds") is not None:
        console.print(f"  Elapsed time:         {summary['elapsed_seconds']:.1f}s")

    if report.errors:
        console.print("\n[bold]Error Details:[/bold]")
        for err in report.errors:
            console.print(f"  [{err['phase']}] {err.get('opportunity_id', '')}: {err['message']}")

    if not summary["success"]:
        sys.exit(1)


# ── review ────────────────────────────────────────────────────────────────────


@main.command()
def review() -> None:
    """Review pending drafts.

    Opens the terminal UI focused on the review queue, showing drafts that
    need human approval.
    """
    try:
        asyncio.run(_do_review())
    except (RuntimeError, DatabaseError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"Unexpected error: {exc}", err=True)
        sys.exit(1)


async def _do_review() -> None:
    """Async implementation of the review command — launches the Textual TUI."""
    try:
        await init_db()
    except Exception as exc:
        click.echo(
            f"Database not initialised. Run `freelance-lead-gen init` first: {exc}",
            err=True,
        )
        return

    from freelance_lead_gen.ui.app import LeadGenTUI

    app = LeadGenTUI()
    await app.run_async()
    click.echo("Review session ended.")


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
    try:
        asyncio.run(_do_list(status=status, platform=platform, limit=limit))
    except (RuntimeError, DatabaseError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"Unexpected error: {exc}", err=True)
        sys.exit(1)


async def _do_list(
    status: str | None,
    platform: str | None,
    limit: int,
) -> None:
    """Async implementation of the list command."""
    try:
        await init_db()
    except Exception as exc:
        click.echo(f"Database not initialised. Run `freelance-lead-gen init` first: {exc}", err=True)
        return

    repo = OpportunityRepository()

    # Parse status filter if provided.
    status_filter: LeadStatus | None = None
    if status:
        try:
            status_filter = LeadStatus(status.lower())
        except ValueError:
            click.echo(f"Invalid status: {status!r}. Valid values: {', '.join(s.value for s in LeadStatus)}", err=True)
            return

    opportunities = await repo.search(
        status=status_filter,
        platform=platform,
        limit=limit,
    )

    if not opportunities:
        click.echo("No opportunities found matching the current filters.")
        return

    # Build a rich table.
    table = Table(
        Column("ID", style="dim", no_wrap=True),
        Column("Status"),
        Column("Platform"),
        Column("Score", justify="right"),
        Column("Title"),
        Column("Company"),
        Column("Location"),
        title="Opportunities",
        title_style="bold",
    )

    for opp in opportunities:
        score_str = str(opp.score) if opp.score is not None else "-"
        table.add_row(
            opp.id,
            opp.status.value,
            opp.platform,
            score_str,
            opp.title[:60],
            opp.company or "-",
            opp.location or "-",
        )

    console.print(table)
    console.print(f"\n[dim]{len(opportunities)} result(s)[/dim]")


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
    try:
        await init_db()
    except Exception as exc:
        click.echo(f"Database not initialised. Run `freelance-lead-gen init` first: {exc}", err=True)
        return

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
    try:
        asyncio.run(_do_serve())
    except (RuntimeError, DatabaseError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"Unexpected error: {exc}", err=True)
        sys.exit(1)


async def _do_serve() -> None:
    """Async implementation of the serve command.

    Initialises the database, creates a discovery agent, starts the
    discovery scheduler, and runs until the user presses Ctrl+C.
    """
    try:
        await init_db()
    except Exception as exc:
        click.echo(
            f"Database not initialised. Run `freelance-lead-gen init` first: {exc}",
            err=True,
        )
        return

    settings = get_settings()
    agent = DiscoveryAgent(settings=settings)
    await agent.initialize()

    scheduler = agent.create_scheduler()

    click.echo("Starting discovery scheduler...")
    click.echo("Press Ctrl+C to stop.")
    click.echo("")

    try:
        await scheduler.start()

        # Wait for shutdown signal.
        stop_event = asyncio.Event()

        def _signal_handler() -> None:
            click.echo("\nShutdown requested...")
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except (NotImplementedError, ValueError):
                pass  # Windows or restricted environment.

        await stop_event.wait()

    except asyncio.CancelledError:
        pass
    finally:
        await scheduler.stop()
        await agent.shutdown()
        click.echo("Scheduler stopped.")
