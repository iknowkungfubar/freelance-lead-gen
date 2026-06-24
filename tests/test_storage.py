"""Tests for the storage layer — database, migrations, and repository CRUD."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from freelance_lead_gen.config.settings import get_settings
from freelance_lead_gen.models.opportunity import LeadOpportunity, LeadStatus, OutboundDraft
from freelance_lead_gen.storage.database import close_db, get_engine, get_session, init_db
from freelance_lead_gen.storage.migrations import MIGRATIONS, apply_migrations, get_migration_status
from freelance_lead_gen.storage.repository import (
    DraftNotFound,
    OpportunityNotFound,
    OpportunityRepository,
)

if TYPE_CHECKING:
    from pathlib import Path

# ── Helper factories ────────────────────────────────────────────────────────────


def _make_opp(
    platform_job_id: str,
    title: str = "Test Lead",
    status: LeadStatus = LeadStatus.DISCOVERED,
    score: int | None = None,
) -> LeadOpportunity:
    return LeadOpportunity(
        platform="upwork",
        platform_job_id=platform_job_id,
        title=title,
        description="A test opportunity for storage tests.",
        status=status,
        score=score,
    )


# ── Database initialisation ─────────────────────────────────────────────────────


class TestDatabaseInit:
    """Tests for database engine initialisation and lifecycle."""

    @pytest.mark.asyncio
    async def test_init_and_close(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify init_db creates an engine and close_db disposes it."""
        db_path = tmp_path / "test_init.db"
        monkeypatch.setenv("DATABASE_PATH", str(db_path))
        get_settings.cache_clear()

        engine = await init_db()
        assert engine is not None

        # Verify we can connect.
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            assert result.scalar_one() == 1

        await close_db()

        # Engine should be disposed now.
        with pytest.raises(RuntimeError, match="not initialised"):
            get_engine()

    @pytest.mark.asyncio
    async def test_get_session_requires_init(self) -> None:
        """Verify get_session raises if init_db was not called first."""
        await close_db()  # Ensure clean state.
        with pytest.raises(RuntimeError, match="not initialised"):
            async with get_session():
                pass  # pragma: no cover


# ── Migrations ──────────────────────────────────────────────────────────────────


class TestMigrations:
    """Tests for the inline migration system."""

    @pytest.mark.asyncio
    async def test_apply_migrations(self, in_memory_db) -> None:
        """Verify migrations are applied and the registry table is created."""
        status = await get_migration_status()
        assert len(status) == len(MIGRATIONS)
        for entry in status:
            assert entry["applied"] is True
            assert entry["applied_at"] is not None

    @pytest.mark.asyncio
    async def test_migrations_idempotent(self, in_memory_db) -> None:
        """Verify running migrations twice does not fail.

        The in_memory_db fixture applies migrations during setup, so
        apply_migrations() returns 0 on the first test call (all already
        applied). Idempotency is confirmed by the second call also returning
        0 without errors.
        """
        applied_first = await apply_migrations()
        applied_second = await apply_migrations()
        # First call may return 0 if fixture already applied them.
        assert isinstance(applied_first, list)
        assert len(applied_second) == 0  # Nothing new to apply

    @pytest.mark.asyncio
    async def test_migration_tables_exist(self, in_memory_db) -> None:
        """Verify the expected tables exist after migrations."""
        engine = get_engine()
        async with engine.connect() as conn:
            # Check opportunities table.
            result = await conn.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='opportunities'"
                )
            )
            assert result.scalar_one_or_none() == "opportunities"

            # Check drafts table.
            result = await conn.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='drafts'"
                )
            )
            assert result.scalar_one_or_none() == "drafts"

            # Check migrations registry.
            result = await conn.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='_migrations'"
                )
            )
            assert result.scalar_one_or_none() == "_migrations"


# ── Repository CRUD ─────────────────────────────────────────────────────────────


class TestRepositoryCRUD:
    """Tests for OpportunityRepository CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_and_get_by_id(self, repository: OpportunityRepository) -> None:
        """Verify create inserts and get_by_id retrieves."""
        opp = _make_opp("crud-1", title="Create Test")
        saved = await repository.create(opp)
        assert saved.id == opp.id

        fetched = await repository.get_by_id(opp.id)
        assert fetched.title == "Create Test"
        assert fetched.platform == "upwork"

    @pytest.mark.asyncio
    async def test_get_by_id_not_found(self, repository: OpportunityRepository) -> None:
        """Verify get_by_id raises for non-existent IDs."""
        with pytest.raises(OpportunityNotFound):
            await repository.get_by_id("nonexistent-id")

    @pytest.mark.asyncio
    async def test_get_by_platform_job_id(self, repository: OpportunityRepository) -> None:
        """Verify lookup by (platform, platform_job_id)."""
        opp = _make_opp("platform-ref-1")
        await repository.create(opp)

        found = await repository.get_by_platform_job_id("upwork", "platform-ref-1")
        assert found is not None
        assert found.id == opp.id

        not_found = await repository.get_by_platform_job_id("upwork", "no-such-id")
        assert not_found is None

    @pytest.mark.asyncio
    async def test_update(self, repository: OpportunityRepository) -> None:
        """Verify update modifies fields correctly."""
        opp = _make_opp("update-1")
        await repository.create(opp)

        opp.title = "Updated Title"
        opp.score = 95
        updated = await repository.update(opp)
        assert updated.title == "Updated Title"
        assert updated.score == 95

    @pytest.mark.asyncio
    async def test_update_not_found(self, repository: OpportunityRepository) -> None:
        """Verify update raises for non-existent IDs."""
        opp = _make_opp("ghost")
        with pytest.raises(OpportunityNotFound):
            await repository.update(opp)

    @pytest.mark.asyncio
    async def test_update_status(self, repository: OpportunityRepository) -> None:
        """Verify update_status transitions state correctly."""
        opp = _make_opp("status-1")
        await repository.create(opp)

        updated = await repository.update_status(
            opp.id,
            LeadStatus.QUALIFIED,
            notes="Qualified via test",
            score=85,
        )
        assert updated.status == LeadStatus.QUALIFIED
        assert updated.notes == "Qualified via test"
        assert updated.score == 85

    @pytest.mark.asyncio
    async def test_update_status_not_found(self, repository: OpportunityRepository) -> None:
        """Verify update_status raises for non-existent IDs."""
        with pytest.raises(OpportunityNotFound):
            await repository.update_status("ghost", LeadStatus.QUALIFIED)

    @pytest.mark.asyncio
    async def test_delete(self, repository: OpportunityRepository) -> None:
        """Verify delete removes an opportunity."""
        opp = _make_opp("delete-1")
        await repository.create(opp)
        assert await repository.delete(opp.id) is True
        assert await repository.delete(opp.id) is False  # Already gone.

    @pytest.mark.asyncio
    async def test_upsert_new(self, repository: OpportunityRepository) -> None:
        """Verify upsert creates a new record when none exists."""
        opp = _make_opp("upsert-new")
        persisted = await repository.upsert(opp)
        assert persisted.id == opp.id
        assert persisted.status == LeadStatus.DISCOVERED

    @pytest.mark.asyncio
    async def test_upsert_existing(self, repository: OpportunityRepository) -> None:
        """Verify upsert updates scraped fields but preserves pipeline state."""
        opp = _make_opp("upsert-existing", title="Original Title")
        await repository.create(opp)

        # Modify pipeline state (should be preserved).
        await repository.update_status(opp.id, LeadStatus.QUALIFIED, score=80)

        # Now upsert with updated scraped fields and default pipeline state.
        updated_opp = LeadOpportunity(
            platform="upwork",
            platform_job_id="upsert-existing",
            title="Updated Title",
            description="Updated description.",
        )
        persisted = await repository.upsert(updated_opp)
        assert persisted.title == "Updated Title"  # Scraped field updated.
        assert persisted.status == LeadStatus.QUALIFIED  # Pipeline state preserved.
        assert persisted.score == 80


# ── Repository Search ───────────────────────────────────────────────────────────


class TestRepositorySearch:
    """Tests for repository search and pagination."""

    @pytest.mark.asyncio
    async def test_list_paginated_empty(self, repository: OpportunityRepository) -> None:
        """Verify list_paginated returns empty when no records exist."""
        rows, total = await repository.list_paginated()
        assert rows == []
        assert total == 0

    @pytest.mark.asyncio
    async def test_list_paginated(self, repository: OpportunityRepository) -> None:
        """Verify list_paginated returns records with total count."""
        for i in range(5):
            await repository.create(_make_opp(f"page-{i}", title=f"Lead {i}"))

        rows, total = await repository.list_paginated(limit=3, offset=0)
        assert len(rows) == 3
        assert total == 5

    @pytest.mark.asyncio
    async def test_search_by_status(self, repository: OpportunityRepository) -> None:
        """Verify search filters by status correctly."""
        opp1 = _make_opp("search-s-1", status=LeadStatus.DISCOVERED)
        opp2 = _make_opp("search-s-2", status=LeadStatus.QUALIFIED)
        opp3 = _make_opp("search-s-3", status=LeadStatus.DISCOVERED)
        for opp in (opp1, opp2, opp3):
            await repository.create(opp)

        discovered = await repository.search(status=LeadStatus.DISCOVERED)
        assert len(discovered) == 2

        qualified = await repository.search(status=LeadStatus.QUALIFIED)
        assert len(qualified) == 1

    @pytest.mark.asyncio
    async def test_search_by_platform(self, repository: OpportunityRepository) -> None:
        """Verify search filters by platform."""
        upwork = _make_opp("plat-1", title="Upwork Job")
        upwork.platform = "upwork"
        linkedin = _make_opp("plat-2", title="LinkedIn Job")
        linkedin.platform = "linkedin"
        await repository.create(upwork)
        await repository.create(linkedin)

        results = await repository.search(platform="linkedin")
        assert len(results) == 1
        assert results[0].platform == "linkedin"

    @pytest.mark.asyncio
    async def test_search_by_min_score(self, repository: OpportunityRepository) -> None:
        """Verify search filters by minimum score."""
        opp1 = _make_opp("score-1", score=90)
        opp2 = _make_opp("score-2", score=50)
        opp3 = _make_opp("score-3", score=30)
        for opp in (opp1, opp2, opp3):
            await repository.create(opp)

        results = await repository.search(min_score=60)
        assert len(results) == 1
        assert results[0].score == 90

    @pytest.mark.asyncio
    async def test_search_text(self, repository: OpportunityRepository) -> None:
        """Verify full-text search works."""
        opp1 = _make_opp("fts-1", title="Python Backend Developer")
        opp2 = _make_opp("fts-2", title="React Frontend Engineer")
        for opp in (opp1, opp2):
            await repository.create(opp)

        results = await repository.search(text_query="Python")
        assert len(results) >= 1
        assert "Python" in results[0].title


# ── Repository Draft Operations ─────────────────────────────────────────────────


class TestRepositoryDrafts:
    """Tests for draft CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_and_get_draft(
        self, repository: OpportunityRepository
    ) -> None:
        """Verify creating and retrieving a draft."""
        opp = _make_opp("draft-opp-1")
        await repository.create(opp)

        draft = OutboundDraft(opportunity_id=opp.id)
        draft.add_version("Test draft body")
        saved = await repository.create_draft(draft)
        assert saved.id == draft.id

        fetched = await repository.get_draft_by_id(draft.id)
        assert fetched.opportunity_id == opp.id
        assert fetched.current_body == "Test draft body"

    @pytest.mark.asyncio
    async def test_get_draft_not_found(self, repository: OpportunityRepository) -> None:
        """Verify get_draft_by_id raises for non-existent drafts."""
        with pytest.raises(DraftNotFound):
            await repository.get_draft_by_id("no-such-draft")

    @pytest.mark.asyncio
    async def test_get_drafts_for_opportunity(
        self, repository: OpportunityRepository
    ) -> None:
        """Verify retrieving all drafts for an opportunity."""
        opp = _make_opp("draft-opp-2")
        await repository.create(opp)

        draft1 = OutboundDraft(opportunity_id=opp.id)
        draft1.add_version("Draft one")
        draft2 = OutboundDraft(opportunity_id=opp.id)
        draft2.add_version("Draft two")
        await repository.create_draft(draft1)
        await repository.create_draft(draft2)

        drafts = await repository.get_drafts_for_opportunity(opp.id)
        assert len(drafts) == 2

    @pytest.mark.asyncio
    async def test_update_draft(self, repository: OpportunityRepository) -> None:
        """Verify updating a draft."""
        opp = _make_opp("draft-opp-3")
        await repository.create(opp)

        draft = OutboundDraft(opportunity_id=opp.id)
        draft.add_version("Original")
        await repository.create_draft(draft)

        draft.approve()
        await repository.update_draft(draft)

        fetched = await repository.get_draft_by_id(draft.id)
        assert fetched.approved is True

    @pytest.mark.asyncio
    async def test_update_draft_not_found(self, repository: OpportunityRepository) -> None:
        """Verify update_draft raises for non-existent drafts."""
        draft = OutboundDraft(opportunity_id="no-opp")
        with pytest.raises(DraftNotFound):
            await repository.update_draft(draft)


# ── Repository Stats ────────────────────────────────────────────────────────────


class TestRepositoryStats:
    """Tests for repository statistics methods."""

    @pytest.mark.asyncio
    async def test_get_stats_empty(self, repository: OpportunityRepository) -> None:
        """Verify stats return zero counts for empty DB."""
        stats = await repository.get_stats()
        assert stats["total"] == 0

    @pytest.mark.asyncio
    async def test_get_stats(self, repository: OpportunityRepository) -> None:
        """Verify stats aggregate correctly."""
        opp1 = _make_opp("stat-1", status=LeadStatus.DISCOVERED)
        opp2 = _make_opp("stat-2", status=LeadStatus.QUALIFIED)
        opp3 = _make_opp("stat-3", status=LeadStatus.DISCOVERED)
        for opp in (opp1, opp2, opp3):
            await repository.create(opp)

        stats = await repository.get_stats()
        assert stats["total"] == 3
        assert stats["discovered"] == 2
        assert stats["qualified"] == 1

    @pytest.mark.asyncio
    async def test_get_platform_counts(self, repository: OpportunityRepository) -> None:
        """Verify platform counts are accurate."""
        opp1 = _make_opp("pc-1")
        opp1.platform = "upwork"
        opp2 = _make_opp("pc-2")
        opp2.platform = "linkedin"
        opp3 = _make_opp("pc-3")
        opp3.platform = "upwork"
        for opp in (opp1, opp2, opp3):
            await repository.create(opp)

        counts = await repository.get_platform_counts()
        assert counts["upwork"] == 2
        assert counts["linkedin"] == 1
