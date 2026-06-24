"""Integration tests for the full pipeline with mocked external dependencies."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from freelance_lead_gen.agents.filtering_agent import FilteringPipeline, FilteringReport
from freelance_lead_gen.agents.orchestrator import (
    LeadGenOrchestrator,
)
from freelance_lead_gen.agents.personalization_agent import PersonalizationAgent
from freelance_lead_gen.agents.verification_agent import (
    VerificationAgent,
    VerificationResult,
)
from freelance_lead_gen.discovery.discovery_agent import DiscoveryAgent, DiscoveryCycleReport
from freelance_lead_gen.models.opportunity import (
    LeadOpportunity,
    LeadStatus,
    OutboundDraft,
)
from freelance_lead_gen.storage.repository import OpportunityRepository
from tests.conftest import _make_opportunity

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def in_memory_db() -> AsyncSession:
    """Create an in-memory SQLite database with the project schema.

    Yields an :class:`AsyncSession` connected to a temporary in-memory
    database with the ``opportunities`` and ``drafts`` tables created.
    The engine is disposed after the test completes.
    """
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)

    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS opportunities (
                id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                platform_job_id TEXT NOT NULL,
                title TEXT NOT NULL,
                company TEXT,
                description TEXT NOT NULL,
                budget_min REAL,
                budget_max REAL,
                currency TEXT DEFAULT 'USD',
                skills TEXT DEFAULT '[]',
                posted_date TEXT,
                url TEXT,
                location TEXT,
                status TEXT DEFAULT 'discovered',
                score INTEGER,
                notes TEXT,
                raw_data TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(platform, platform_job_id)
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS drafts (
                id TEXT PRIMARY KEY,
                opportunity_id TEXT NOT NULL,
                versions TEXT DEFAULT '[]',
                current_version_index INTEGER DEFAULT 0,
                subject TEXT,
                approved INTEGER DEFAULT 0,
                human_edited INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (opportunity_id) REFERENCES opportunities(id)
            )
        """))

    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False,
    )
    async with factory() as session:
        yield session

    await engine.dispose()


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_discover_filter() -> None:
    """Verify the full pipeline processes discovered opportunities through
    filtering when agents are properly mocked.

    Creates a :class:`LeadGenOrchestrator` with mocked discovery, filtering,
    personalization, and verification agents.  Asserts that the returned
    report reflects the expected counts through each phase.
    """
    # ── Mock discovery agent ────────────────────────────────────────────────
    mock_discovery = AsyncMock(spec=DiscoveryAgent)
    mock_discovery.run_discovery_cycle.return_value = DiscoveryCycleReport(
        total_new=2,
        total_found=2,
        total_errors=0,
        platforms_attempted=1,
        platforms_succeeded=1,
        per_platform={"upwork": {"found": 2, "new": 2, "failed": 0, "searched": 1}},
    )

    discovered = [
        _make_opportunity("discover-1", title="Python Backend Lead"),
        _make_opportunity("discover-2", title="React Frontend Lead"),
    ]

    mock_repo = AsyncMock(spec=OpportunityRepository)
    mock_repo.search.return_value = discovered

    qualified = [
        _make_opportunity(
            "discover-1", title="Python Backend Lead", status=LeadStatus.QUALIFIED, score=85,
        ),
        _make_opportunity(
            "discover-2", title="React Frontend Lead", status=LeadStatus.QUALIFIED, score=72,
        ),
    ]

    mock_filtering = AsyncMock(spec=FilteringPipeline)
    mock_filtering.run.return_value = (
        qualified,
        FilteringReport(
            total_input=2,
            high_count=1,
            potential_count=1,
            low_count=0,
            disqualified_count=0,
            errors=0,
        ),
    )

    # ── Mock personalization agent ──────────────────────────────────────────
    mock_personalization = AsyncMock(spec=PersonalizationAgent)

    def _make_draft(opp: LeadOpportunity, *args: object, **kwargs: object) -> OutboundDraft:
        draft = OutboundDraft(opportunity_id=opp.id)
        draft.add_version(f"Personalised outreach for {opp.title}")
        return draft

    mock_personalization.generate_draft.side_effect = _make_draft

    # ── Mock verification agent ─────────────────────────────────────────────
    mock_verification = AsyncMock(spec=VerificationAgent)
    mock_verification.verify.return_value = VerificationResult(
        passed=True,
        score=88,
        word_count=42,
        paragraph_count=3,
        issues=[],
        banned_phrases_found=[],
        ai_markers_found=[],
        suggested_fixes=[],
    )

    # Mock LLM client so agents don't attempt real LLM calls.
    mock_llm = AsyncMock()

    # ── Run the pipeline ────────────────────────────────────────────────────
    orchestrator = LeadGenOrchestrator(
        discovery_agent=mock_discovery,
        filtering_pipeline=mock_filtering,
        personalization_agent=mock_personalization,
        verification_agent=mock_verification,
        repository=mock_repo,
        llm_client=mock_llm,
    )

    report = await orchestrator.run_full_pipeline()

    # ── Assertions ──────────────────────────────────────────────────────────
    assert report.success, f"Pipeline failed: {report.errors}"
    assert report.total_discovered == 2
    assert report.total_qualified == 2
    assert report.total_drafted == 2
    assert report.total_verified_pass >= 1
    assert report.total_errors == 0

    # Verify that the discovery agent was actually called.
    mock_discovery.run_discovery_cycle.assert_awaited_once()

    # Verify the filtering pipeline received the discovered opportunities.
    mock_filtering.run.assert_awaited_once()

    # Verify personalization was called for each qualified lead.
    assert mock_personalization.generate_draft.await_count == 2

    # Verify verification was called for each draft.
    assert mock_verification.verify.await_count == 2


@pytest.mark.asyncio
async def test_db_persistence(in_memory_db: AsyncSession) -> None:
    """Verify that opportunities can be created and read back via the
    :class:`OpportunityRepository` using an in-memory database.

    Creates a :class:`LeadOpportunity`, persists it, retrieves it by ID,
    and asserts field-level equivalence.
    """
    repo = OpportunityRepository(session=in_memory_db)

    opp = _make_opportunity(
        platform_job_id="persist-001",
        title="Full Stack Developer",
    )

    # ── Create ──────────────────────────────────────────────────────────────
    saved = await repo.create(opp)
    assert saved.id == opp.id
    assert saved.platform == "upwork"
    assert saved.title == "Full Stack Developer"
    assert saved.status == LeadStatus.DISCOVERED

    # ── Read back ───────────────────────────────────────────────────────────
    fetched = await repo.get_by_id(opp.id)
    assert fetched is not None
    assert fetched.id == opp.id
    assert fetched.title == "Full Stack Developer"
    assert fetched.platform == "upwork"
    assert fetched.platform_job_id == "persist-001"
    assert fetched.description == "A test freelance opportunity for integration testing."
    assert fetched.status == LeadStatus.DISCOVERED
    assert fetched.score is None

    # ── Update ──────────────────────────────────────────────────────────────
    opp.title = "Senior Full Stack Developer"
    opp.score = 92
    opp.status = LeadStatus.QUALIFIED
    updated = await repo.update(opp)
    assert updated.title == "Senior Full Stack Developer"
    assert updated.score == 92
    assert updated.status == LeadStatus.QUALIFIED

    # ── Verify the update persisted ─────────────────────────────────────────
    refetched = await repo.get_by_id(opp.id)
    assert refetched.title == "Senior Full Stack Developer"
    assert refetched.score == 92
    assert refetched.status == LeadStatus.QUALIFIED

    # ── Search by status ────────────────────────────────────────────────────
    results = await repo.search(status=LeadStatus.QUALIFIED)
    assert len(results) >= 1
    assert results[0].id == opp.id


@pytest.mark.asyncio
async def test_pipeline_empty_discovery() -> None:
    """Verify the pipeline handles empty discovery results gracefully.

    When the discovery phase produces no opportunities, the pipeline should
    complete successfully with zero qualified leads, zero errors, and a
    non-fatal report.
    """
    mock_discovery = AsyncMock(spec=DiscoveryAgent)
    mock_discovery.run_discovery_cycle.return_value = DiscoveryCycleReport(
        total_new=0,
        total_found=0,
        total_errors=0,
        platforms_attempted=1,
        platforms_succeeded=1,
        per_platform={"upwork": {"found": 0, "new": 0, "failed": 0, "searched": 1}},
    )

    mock_repo = AsyncMock(spec=OpportunityRepository)
    mock_repo.search.return_value = []

    mock_llm = AsyncMock()

    orchestrator = LeadGenOrchestrator(
        discovery_agent=mock_discovery,
        repository=mock_repo,
        llm_client=mock_llm,
        # Filtering, personalization, and verification are created with
        # defaults internally, but since no opportunities flow through,
        # they won't be exercised.
    )

    report = await orchestrator.run_full_pipeline()

    # ── Assertions ──────────────────────────────────────────────────────────
    assert report.success, "Pipeline should report success even with empty discovery"
    assert report.total_discovered == 0
    assert report.total_qualified == 0
    assert report.total_drafted == 0
    assert report.total_errors == 0
    assert report.total_verified_pass == 0
    assert report.total_verified_fail == 0
    assert len(report.phases_completed) >= 1
    assert "discovery" in report.phases_completed
