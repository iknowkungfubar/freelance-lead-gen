"""Tests for the Click CLI commands."""

from __future__ import annotations

from click.testing import CliRunner

from freelance_lead_gen.cli import main


def test_cli_help() -> None:
    """Verify --help shows the usage line and all command names."""
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.output
    # All expected command names must appear in the help output.
    for cmd in ("init", "discover", "pipeline", "review", "list", "stats", "quickstart", "serve"):
        assert cmd in result.output, f"Command {cmd!r} missing from help"


def test_cli_init_help() -> None:
    """Verify 'init --help' shows the init command description."""
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--help"])
    assert result.exit_code == 0
    assert "Initialize" in result.output


def test_cli_list_help() -> None:
    """Verify 'list --help' shows the expected options."""
    runner = CliRunner()
    result = runner.invoke(main, ["list", "--help"])
    assert result.exit_code == 0
    assert "--status" in result.output
    assert "--platform" in result.output
    assert "--limit" in result.output


def test_cli_stats_no_db() -> None:
    """Verify 'stats' runs even without a pre-initialised database.

    The ``_ensure_db()`` helper is called by every command, so the
    database is lazily initialised on first use.  This test verifies
    that the stats command exits cleanly rather than crashing.
    """
    runner = CliRunner()
    result = runner.invoke(main, ["stats"])
    assert result.exit_code == 0
