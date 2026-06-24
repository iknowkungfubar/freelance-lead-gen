"""Tests for the Textual TUI application."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from freelance_lead_gen.storage.repository import OpportunityRepository
from freelance_lead_gen.ui.app import LeadGenTUI


@pytest.mark.asyncio
async def test_tui_initial_screen() -> None:
    """Verify the TUI shows the correct title including 'Freelance Lead Gen'."""
    mock_repo = AsyncMock(spec=OpportunityRepository)
    mock_repo.get_stats.return_value = {"total": 0}
    mock_repo.get_platform_counts.return_value = {}

    app = LeadGenTUI(repository=mock_repo)
    async with app.run_test() as pilot:
        assert "Freelance Lead Gen" in app.TITLE
        await pilot.pause()


@pytest.mark.asyncio
async def test_tui_screen_navigation() -> None:
    """Verify the app mounts without errors and shows the dashboard initially."""
    mock_repo = AsyncMock(spec=OpportunityRepository)
    mock_repo.get_stats.return_value = {"total": 0}
    mock_repo.get_platform_counts.return_value = {}

    app = LeadGenTUI(repository=mock_repo)
    async with app.run_test() as pilot:
        assert app.screen is not None
        assert len(app.screen_stack) >= 1
        await pilot.pause()


@pytest.mark.asyncio
async def test_tui_quit_action() -> None:
    """Verify the quit action (q keybinding) stops the application."""
    mock_repo = AsyncMock(spec=OpportunityRepository)
    mock_repo.get_stats.return_value = {"total": 0}
    mock_repo.get_platform_counts.return_value = {}

    app = LeadGenTUI(repository=mock_repo)
    async with app.run_test() as pilot:
        await pilot.press("q")
        await pilot.pause()
        assert not app._running
