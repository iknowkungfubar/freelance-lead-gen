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
    mock_fn = AsyncMock(
        side_effect=[
            Exception("Fail 1"),
            Exception("Fail 2"),
            {"test_platform": {"found": 5, "new": 3, "failed": 0}},
        ]
    )
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
    assert "pipeline_runs" in status
    assert "pipeline_failures" in status

    # Default state before any cycles.
    assert status["running"] is False
    assert status["total_cycles"] == 0
    assert status["total_leads"] == 0
    assert status["total_errors"] == 0
    assert status["per_platform"] == {}
    assert status["consecutive_failures"] == {}
    assert status["auto_disabled"] == []
    assert status["last_cycle_at"] is None
    assert status["pipeline_runs"] == 0
    assert status["pipeline_failures"] == 0


@pytest.mark.asyncio
async def test_pipeline_runs_after_new_leads() -> None:
    """Auto-screening runs after a discovery cycle that found new leads."""
    pipeline_fn = AsyncMock(return_value=None)
    scheduler = DiscoveryScheduler(
        discovery_fn=AsyncMock(
            return_value={"remote_ok": {"found": 5, "new": 3, "failed": 0}}
        ),
        pipeline_fn=pipeline_fn,
    )
    scheduler.add_platform("remote_ok", interval_minutes=999)

    await scheduler._run_discovery_cycle("remote_ok")

    pipeline_fn.assert_awaited_once_with("remote_ok")
    assert scheduler._stats.pipeline_runs == 1
    assert scheduler._stats.pipeline_failures == 0
    # The cycle itself must still be marked successful.
    assert scheduler._platforms["remote_ok"].consecutive_failures == 0


@pytest.mark.asyncio
async def test_pipeline_skipped_when_no_new_leads() -> None:
    """Auto-screening is skipped when discovery found no new leads."""
    pipeline_fn = AsyncMock(return_value=None)
    scheduler = DiscoveryScheduler(
        discovery_fn=AsyncMock(
            return_value={"remote_ok": {"found": 5, "new": 0, "failed": 0}}
        ),
        pipeline_fn=pipeline_fn,
    )
    scheduler.add_platform("remote_ok", interval_minutes=999)

    await scheduler._run_discovery_cycle("remote_ok")

    pipeline_fn.assert_not_awaited()
    assert scheduler._stats.pipeline_runs == 0
    assert scheduler._stats.total_new == 0


@pytest.mark.asyncio
async def test_pipeline_skipped_when_not_configured() -> None:
    """Without a pipeline_fn the cycle behaves as before (discovery only)."""
    scheduler = DiscoveryScheduler(
        discovery_fn=AsyncMock(
            return_value={"remote_ok": {"found": 5, "new": 5, "failed": 0}}
        ),
    )
    scheduler.add_platform("remote_ok", interval_minutes=999)

    await scheduler._run_discovery_cycle("remote_ok")

    assert scheduler._stats.pipeline_runs == 0
    assert scheduler._platforms["remote_ok"].consecutive_failures == 0


@pytest.mark.asyncio
async def test_pipeline_failure_does_not_fail_cycle() -> None:
    """A screening error must not fail the discovery cycle or disable the
    platform — it is tracked separately in pipeline_failures."""
    scheduler = DiscoveryScheduler(
        discovery_fn=AsyncMock(
            return_value={"remote_ok": {"found": 2, "new": 2, "failed": 0}}
        ),
        pipeline_fn=AsyncMock(side_effect=RuntimeError("LLM quota exhausted")),
    )
    scheduler.add_platform("remote_ok", interval_minutes=999)
    ps = scheduler._platforms["remote_ok"]
    ps.max_consecutive_failures = 2

    await scheduler._run_discovery_cycle("remote_ok")

    assert ps.consecutive_failures == 0
    assert ps.enabled
    assert scheduler._stats.total_failures == 0
    assert scheduler._stats.pipeline_runs == 0
    assert scheduler._stats.pipeline_failures == 1


@pytest.mark.asyncio
async def test_accepts_report_object_result() -> None:
    """Discovery cycles in production return a DiscoveryCycleReport object
    exposing ``per_platform`` — the scheduler must parse it correctly."""
    from types import SimpleNamespace

    report = SimpleNamespace(
        per_platform={
            "remote_ok": {"found": 7, "new": 4, "failed": 0, "searched": 1},
        }
    )
    pipeline_fn = AsyncMock(return_value=None)
    scheduler = DiscoveryScheduler(
        discovery_fn=AsyncMock(return_value=report),
        pipeline_fn=pipeline_fn,
    )
    scheduler.add_platform("remote_ok", interval_minutes=999)

    await scheduler._run_discovery_cycle("remote_ok")

    assert scheduler._stats.total_runs == 1
    assert scheduler._stats.total_leads == 7
    assert scheduler._stats.total_new == 4
    assert scheduler._platforms["remote_ok"].consecutive_failures == 0
    pipeline_fn.assert_awaited_once_with("remote_ok")
