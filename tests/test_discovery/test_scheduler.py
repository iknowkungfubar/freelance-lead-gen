"""Tests for the DiscoveryScheduler's reliability features."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from freelance_lead_gen.discovery.scheduler import DiscoveryScheduler


@pytest.mark.asyncio
async def test_auto_disables_after_consecutive_failures() -> None:
    """Verify scheduler auto-disables a platform after N consecutive failures.

    Each discovery-cycle failure is tracked per-platform.  When the count
    reaches *max_consecutive_failures* the platform is automatically disabled
    so no further cycles are attempted.
    """
    mock_fn = AsyncMock(side_effect=Exception("Platform failure"))
    scheduler = DiscoveryScheduler(discovery_fn=mock_fn)
    scheduler.add_platform("test_platform", interval_minutes=999)

    ps = scheduler._platforms["test_platform"]
    ps.max_consecutive_failures = 2  # Shorten threshold for this test.

    # First failure — tracked but not yet disabled.
    await scheduler._run_discovery_cycle("test_platform")
    assert ps.consecutive_failures == 1
    assert ps.enabled

    # Second failure — reaches threshold; platform is auto-disabled.
    await scheduler._run_discovery_cycle("test_platform")
    assert ps.consecutive_failures == 2
    assert ps.enabled is False

    # health_status should reflect the disabled platform.
    status = scheduler.health_status
    assert "test_platform" in status["auto_disabled"]


@pytest.mark.asyncio
async def test_consecutive_failures_reset_on_success() -> None:
    """Verify consecutive_failures resets after a successful discovery cycle.

    A single success should zero out the failure counter, preventing a
    transient platform outage from causing permanent auto-disable.
    """
    mock_fn = AsyncMock(side_effect=[
        Exception("Fail 1"),
        Exception("Fail 2"),
        {"test_platform": {"found": 5, "new": 3, "failed": 0}},
    ])
    scheduler = DiscoveryScheduler(discovery_fn=mock_fn)
    scheduler.add_platform("test_platform", interval_minutes=999)
    ps = scheduler._platforms["test_platform"]

    # Two consecutive failures.
    await scheduler._run_discovery_cycle("test_platform")
    await scheduler._run_discovery_cycle("test_platform")
    assert ps.consecutive_failures == 2

    # Then a success — counter should reset to 0.
    await scheduler._run_discovery_cycle("test_platform")
    assert ps.consecutive_failures == 0
    assert ps.total_found == 5


@pytest.mark.asyncio
async def test_health_status_property() -> None:
    """Verify health_status returns all expected keys for monitoring / heartbeats.

    The health_status dict is consumed by external monitors and the serve
    command's dashboard, so the schema must be stable.
    """
    scheduler = DiscoveryScheduler(discovery_fn=None)
    status = scheduler.health_status

    assert "running" in status
    assert "total_cycles" in status
    assert "total_leads" in status
    assert "total_errors" in status
    assert "per_platform" in status
    assert "consecutive_failures" in status
    assert "auto_disabled" in status
    assert "last_cycle_at" in status

    # Default state before any cycles.
    assert status["running"] is False
    assert status["total_cycles"] == 0
    assert status["total_leads"] == 0
    assert status["total_errors"] == 0
    assert status["per_platform"] == {}
    assert status["consecutive_failures"] == {}
    assert status["auto_disabled"] == []
    assert status["last_cycle_at"] is None
