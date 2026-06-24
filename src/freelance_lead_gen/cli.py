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
from freelance_lead_gen.storage.database import close_db, init_db
from freelance_lead_gen.storage.migrations import apply_migrations
from freelance_lead_gen.storage.repository import DatabaseError, OpportunityRepository

logger = structlog.get_logger(__name__)
console = Console()


# ── Safe error helper ──────────────────────────────────────────────────────────


def _safe_error(msg: str, exc: Exception) -> None:
    """Log the full exception details and print a sanitised message to the user.

    The full exception (which may contain API keys, file paths, or other
    sensitive information) is written to the structured log with traceback.
    The user only sees a generic message that won't leak internals.
    """
    logger.error(msg, exc_info=exc)
    click.echo("An unexpected error occurred.  Please check the logs for details.", err=True)


def _validate_settings() -> list[str]:
    """Run pre-flight checks on the current settings and return a list of
    validation errors (empty if everything is OK).

    Checks:
    - ``LLM_API_KEY`` is set and non-empty.
    - ``DATABASE_PATH`` points to a writable location.
    - At least one search query is configured.
    """
    from pathlib import Path

    from freelance_lead_gen.config.settings import get_settings

    errors: list[str] = []

    try:
        settings = get_settings()
    except Exception as exc:
        errors.append(f"Failed to load settings: {exc}")
        return errors

    # LLM_API_KEY
    key = settings.llm.api_key
    if not key or key == "***":
        errors.append(
            "LLM_API_KEY is not set.  Set it in your .env file or environment "
            "and ensure it is a valid API key."
        )

    # DATABASE_PATH writable
    db_path = settings.database.database_url
    if db_path and db_path != "sqlite+aiosqlite:///:memory:":
        # Extract the path from the SQLAlchemy URL.
        url_str = str(db_path)
        if url_str.startswith("sqlite+aiosqlite:///"):
            file_path_str = url_str[len("sqlite+aiosqlite:///"):]
            if file_path_str:
                p = Path(file_path_str)
                parent = p.parent
                try:
                    parent.mkdir(parents=True, exist_ok=True)
                    # Try to touch the file to verify write access.
                    test_file = parent / ".write_test"
                    test_file.touch()
                    test_file.unlink()
                except (OSError, PermissionError) as exc:
                    errors.append(
                        f"Database path is not writable: {file_path_str} "
                        f"(parent directory: {parent}).  Error: {exc}"
                    )

    # Search queries.
    queries = settings.discovery.queries
    if not queries or all(not q.strip() for q in queries):
        errors.append(
            "No search queries configured.  Set DISCOVERY_QUERIES in your "
            "settings or environment (a comma-separated list of search terms)."
        )

    return errors


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
    # Pre-flight config validation.
    validation_errors = _validate_settings()
    if validation_errors:
        for err in validation_errors:
            click.echo(f"  [ERROR] {err}", err=True)
        click.echo("\nFix the above configuration issues and try again.", err=True)
        sys.exit(1)

    try:
        asyncio.run(_do_init())
        click.echo("Database initialised successfully.")
    except Exception as exc:
        _safe_error("Failed to initialise database", exc)
        sys.exit(1)


async def _ensure_db() -> None:
    """Ensure the database is initialised and migrations are applied.

    Call this from every CLI command that needs a working database,
    instead of calling :func:`init_db` directly.
    """
    await init_db()
    await apply_migrations()


async def _do_init() -> None:
    """Async implementation of the init command."""
    await _ensure_db()


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
        _safe_error("Command failed", exc)
        sys.exit(1)
    except Exception as exc:
        _safe_error("Command failed with unexpected error", exc)
        sys.exit(1)


async def _do_discover(headless: bool) -> None:
    """Async implementation of the discover command."""
    try:
        await _ensure_db()
    except Exception as exc:
        _safe_error("Database not initialised", exc)
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
        _safe_error("Command failed", exc)
        sys.exit(1)
    except Exception as exc:
        _safe_error("Command failed with unexpected error", exc)
        sys.exit(1)


async def _do_pipeline(run_discovery: bool, headless: bool) -> None:
    """Async implementation of the pipeline command."""
    try:
        await _ensure_db()
    except Exception as exc:
        _safe_error("Database not initialised", exc)
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
        _safe_error("Command failed", exc)
        sys.exit(1)
    except Exception as exc:
        _safe_error("Command failed with unexpected error", exc)
        sys.exit(1)


async def _do_review() -> None:
    """Async implementation of the review command — launches the Textual TUI."""
    try:
        await _ensure_db()
    except Exception as exc:
        _safe_error("Database not initialised", exc)
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
        _safe_error("Command failed", exc)
        sys.exit(1)
    except Exception as exc:
        _safe_error("Command failed with unexpected error", exc)
        sys.exit(1)


async def _do_list(
    status: str | None,
    platform: str | None,
    limit: int,
) -> None:
    """Async implementation of the list command."""
    try:
        await _ensure_db()
    except Exception as exc:
        _safe_error("Database not initialised", exc)
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
        _safe_error("Command failed", exc)
        sys.exit(1)
    except Exception as exc:
        _safe_error("Command failed with unexpected error", exc)
        sys.exit(1)


async def _do_stats() -> None:
    """Async implementation of the stats command."""
    try:
        await _ensure_db()
    except Exception as exc:
        _safe_error("Database not initialised", exc)
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


# ── health ────────────────────────────────────────────────────


@main.command()
def health() -> None:
    """Show system health status.

    Reports on database connectivity, LLM configuration validity, and
    per-platform scheduler status (if the scheduler is running).  Uses
    Rich for formatted terminal output.
    """
    try:
        asyncio.run(_do_health())
    except Exception as exc:
        _safe_error("Health check failed", exc)
        sys.exit(1)


async def _do_health() -> None:
    """Async implementation of the health command.

    Checks performed in order:

    1. **LLM configuration** — API key presence and base URL format.
    2. **LLM endpoint** — TCP reachability of the configured API host.
    3. **Database** — engine initialisation and connection verification.
    4. **Scheduler** — advisory note (the scheduler is only active in
       ``serve`` mode).

    Each check is independent so partial failure in one does not prevent
    the remaining checks from running.
    """
    from urllib.parse import urlparse

    console.print("[bold]System Health Check[/bold]\n")

    # ── 1. LLM configuration ────────────────────────────────────────────
    errors = _validate_settings()
    if errors:
        console.print("[red]✗[/red] LLM Configuration: [red]INVALID[/red]")
        for err in errors:
            console.print(f"    {err}")
    else:
        console.print("[green]✓[/green] LLM Configuration: [green]OK[/green]")

    # ── 2. LLM endpoint reachability (lightweight TCP check) ────────────
    settings = get_settings()
    base_url = settings.llm.base_url
    try:
        parsed = urlparse(base_url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if host:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=5.0,
            )
            writer.close()
            await writer.wait_closed()
            console.print(
                f"[green]✓[/green] LLM Endpoint: [green]reachable[/green] "
                f"({host}:{port})"
            )
        else:
            console.print(
                "[yellow]~[/yellow] LLM Endpoint: [yellow]skipped[/yellow] "
                "(could not parse host from base_url)"
            )
    except OSError as exc:
        console.print(
            f"[red]✗[/red] LLM Endpoint: [red]unreachable[/red] "
            f"({base_url}): {exc}"
        )
    except TimeoutError:
        console.print(
            f"[red]✗[/red] LLM Endpoint: [red]timeout[/red] "
            f"({base_url}) — host did not respond within 5 s"
        )

    # ── 3. Database connectivity ────────────────────────────────────────
    try:
        await init_db()
        await close_db()
        console.print("[green]✓[/green] Database: [green]OK[/green]")
    except Exception as exc:
        console.print(
            f"[red]✗[/red] Database: [red]FAILED[/red] — {exc}"
        )

    # ── 4. Scheduler status ─────────────────────────────────────────────
    console.print(
        "\n[bold]Scheduler:[/bold] Not running "
        "(use [italic]serve[/italic] to start the scheduler)"
    )

    console.print("\n[bold green]Health check complete.[/bold green]")


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
        _safe_error("Command failed", exc)
        sys.exit(1)
    except Exception as exc:
        _safe_error("Command failed with unexpected error", exc)
        sys.exit(1)


async def _do_serve() -> None:
    """Async implementation of the serve command.

    Initialises the database, creates a discovery agent, starts the
    discovery scheduler, and runs until the user presses Ctrl+C.
    """
    try:
        await _ensure_db()
    except Exception as exc:
        _safe_error("Database not initialised", exc)
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
