"""Async repository for CRUD operations on opportunities and drafts.

Provides a :class:`OpportunityRepository` with methods for creating,
querying, updating, and de-duplicating lead records against the SQLite
database.

All methods accept or return Pydantic domain models (:class:`LeadOpportunity`,
:class:`OutboundDraft`) — the repository handles the mapping between ORM
rows and domain objects.
"""

from __future__ import annotations as _annotations

import contextlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import RowMapping, text

from freelance_lead_gen.models.opportunity import LeadOpportunity, LeadStatus, OutboundDraft
from freelance_lead_gen.storage.database import get_session_factory

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

# ── Valid FTS column names ───────────────────────────────────────────────────

_VALID_FTS_COLUMNS: frozenset[str] = frozenset({
    "id", "platform", "platform_job_id", "title", "company", "description",
    "budget_min", "budget_max", "currency", "skills", "posted_date", "url",
    "location", "status", "score", "notes", "raw_data", "created_at", "updated_at",
})
"""Column names allowed in FTS WHERE clause extra_params."""


# ── Custom exceptions ────────────────────────────────────────────────────────


class OpportunityNotFound(LookupError):
    """Raised when an opportunity ID does not exist in the database."""

    def __init__(self, opportunity_id: str) -> None:
        self.opportunity_id = opportunity_id
        super().__init__(f"Opportunity not found: {opportunity_id}")


class DraftNotFound(LookupError):
    """Raised when a draft ID does not exist in the database."""

    def __init__(self, draft_id: str) -> None:
        self.draft_id = draft_id
        super().__init__(f"Draft not found: {draft_id}")


class DatabaseError(RuntimeError):
    """Generic database error wrapper."""

    def __init__(self, message: str, original: Exception | None = None) -> None:
        self.original = original
        super().__init__(message)


# ── Row ↔ Domain mapping helpers ────────────────────────────────────────────


def _row_to_opportunity(row: RowMapping) -> LeadOpportunity:
    """Convert a raw database row to a :class:`LeadOpportunity`."""
    return LeadOpportunity(
        id=row["id"],
        platform=row["platform"],
        platform_job_id=row["platform_job_id"],
        title=row["title"],
        company=row.get("company"),
        description=row["description"],
        budget_min=row.get("budget_min"),
        budget_max=row.get("budget_max"),
        currency=row.get("currency", "USD"),
        skills=_parse_json_array(row.get("skills", "[]")),
        posted_date=_parse_optional_datetime(row.get("posted_date")),
        url=row.get("url"),
        location=row.get("location"),
        status=LeadStatus(row["status"]),
        score=row.get("score"),
        notes=row.get("notes"),
        raw_data=_parse_json_dict(row.get("raw_data", "{}")),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _opportunity_to_row(opp: LeadOpportunity) -> dict[str, Any]:
    """Convert a :class:`LeadOpportunity` to a database row dict."""
    return {
        "id": opp.id,
        "platform": opp.platform,
        "platform_job_id": opp.platform_job_id,
        "title": opp.title,
        "company": opp.company,
        "description": opp.description,
        "budget_min": opp.budget_min,
        "budget_max": opp.budget_max,
        "currency": opp.currency,
        "skills": json.dumps(opp.skills),
        "posted_date": opp.posted_date.isoformat() if opp.posted_date else None,
        "url": opp.url,
        "location": opp.location,
        "status": opp.status.value,
        "score": opp.score,
        "notes": opp.notes,
        "raw_data": json.dumps(opp.raw_data, default=str),
        "created_at": opp.created_at.isoformat(),
        "updated_at": opp.updated_at.isoformat(),
    }


def _row_to_draft(row: RowMapping) -> OutboundDraft:
    """Convert a raw database row to an :class:`OutboundDraft`."""
    return OutboundDraft(
        id=row["id"],
        opportunity_id=row["opportunity_id"],
        versions=_parse_json_array(row.get("versions", "[]")),
        current_version_index=row.get("current_version_index", 0),
        subject=row.get("subject"),
        approved=bool(row.get("approved", 0)),
        human_edited=bool(row.get("human_edited", 0)),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _draft_to_row(draft: OutboundDraft) -> dict[str, Any]:
    """Convert an :class:`OutboundDraft` to a database row dict."""
    return {
        "id": draft.id,
        "opportunity_id": draft.opportunity_id,
        "versions": json.dumps(draft.versions),
        "current_version_index": draft.current_version_index,
        "subject": draft.subject,
        "approved": 1 if draft.approved else 0,
        "human_edited": 1 if draft.human_edited else 0,
        "created_at": draft.created_at.isoformat(),
        "updated_at": draft.updated_at.isoformat(),
    }


# ── Parsing helpers ──────────────────────────────────────────────────────────


def _parse_json_array(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def _parse_json_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _parse_datetime(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(UTC)
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return datetime.now(UTC)


def _parse_optional_datetime(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


# ── Repository ───────────────────────────────────────────────────────────────


class OpportunityRepository:
    """Async CRUD repository for :class:`LeadOpportunity` records.

    Accepts an optional *session* argument.  When provided, all operations
    share that session (and the caller manages commit/rollback).  When
    omitted, each method opens and commits its own transaction scope.

    Usage with shared session::

        async with get_session() as session:
            repo = OpportunityRepository(session)
            await repo.create(opp)
            await repo.update_status(opp.id, LeadStatus.QUALIFIED)
            # session is committed on exit (or rolled back on error)

    Usage with auto-scoped sessions::

        repo = OpportunityRepository()
        opp = await repo.get_by_id("abc123")
    """

    def __init__(self, session: AsyncSession | None = None) -> None:
        self._session: AsyncSession | None = session

    # ── session scope ───────────────────────────────────────────────────

    @contextlib.asynccontextmanager
    async def _session_scope(self) -> AsyncGenerator[AsyncSession, None]:
        """Provide a session, creating a transactional scope if needed.

        If a session was injected via the constructor, it is yielded
        directly (no commit/rollback on exit — the caller manages that).
        Otherwise, a new session is acquired from the factory and committed
        when the context exits.
        """
        if self._session is not None:
            yield self._session
        else:
            factory = get_session_factory()
            session: AsyncSession = factory()
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    # ── CREATE ─────────────────────────────────────────────────────────

    async def create(self, opportunity: LeadOpportunity) -> LeadOpportunity:
        """Insert a new opportunity record.

        Parameters
        ----------
        opportunity : LeadOpportunity
            The opportunity to persist.

        Returns
        -------
        LeadOpportunity
            The same opportunity object.

        Raises
        ------
        DatabaseError
            If the insert fails (e.g. duplicate platform_job_id).

        """
        row = _opportunity_to_row(opportunity)
        columns = ", ".join(row)
        placeholders = ", ".join(f":{k}" for k in row)

        async with self._session_scope() as session:
            try:
                await session.execute(
                    text(f"INSERT INTO opportunities ({columns}) VALUES ({placeholders})"),
                    row,
                )
                logger.info(
                    "opportunity.created",
                    id=opportunity.id,
                    platform=opportunity.platform,
                    title=opportunity.title[:60],
                )
            except Exception as exc:
                logger.exception("opportunity.create_failed", id=opportunity.id, error=str(exc))
                raise DatabaseError(f"Failed to create opportunity: {exc}", original=exc) from exc

        return opportunity

    # ── READ ───────────────────────────────────────────────────────────

    async def get_by_id(self, opportunity_id: str) -> LeadOpportunity:
        """Retrieve a single opportunity by its ID.

        Parameters
        ----------
        opportunity_id : str
            The opportunity's unique ID.

        Returns
        -------
        LeadOpportunity

        Raises
        ------
        OpportunityNotFound
            If no opportunity matches the given ID.

        """
        async with self._session_scope() as session:
            result = await session.execute(
                text("SELECT * FROM opportunities WHERE id = :id"),
                {"id": opportunity_id},
            )
            row = result.mappings().one_or_none()

        if row is None:
            raise OpportunityNotFound(opportunity_id)

        return _row_to_opportunity(row)

    async def get_by_platform_job_id(
        self, platform: str, platform_job_id: str
    ) -> LeadOpportunity | None:
        """Look up an opportunity by its platform-native job ID.

        Parameters
        ----------
        platform : str
            The source platform name.
        platform_job_id : str
            The platform's job listing identifier.

        Returns
        -------
        LeadOpportunity or None
            The matching opportunity, or *None* if not found.

        """
        async with self._session_scope() as session:
            result = await session.execute(
                text(
                    "SELECT * FROM opportunities "
                    "WHERE platform = :platform AND platform_job_id = :job_id"
                ),
                {"platform": platform, "job_id": platform_job_id},
            )
            row = result.mappings().one_or_none()

        return _row_to_opportunity(row) if row is not None else None

    # ── SEARCH / LIST ──────────────────────────────────────────────────

    async def search(
        self,
        *,
        status: LeadStatus | None = None,
        platform: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        text_query: str | None = None,
        min_score: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[LeadOpportunity]:
        """Search opportunities with optional filters and full-text search.

        Parameters
        ----------
        status : LeadStatus or None
            Filter by pipeline status.
        platform : str or None
            Filter by source platform name.
        date_from : datetime or None
            Only opportunities created at or after this time.
        date_to : datetime or None
            Only opportunities created at or before this time.
        text_query : str or None
            Full-text search term (searches title, description, company).
            Uses SQLite FTS5 when available, falls back to LIKE.
        min_score : int or None
            Minimum qualification score threshold (0-100).
        limit : int
            Maximum results to return (default 50, max 500).
        offset : int
            Number of results to skip (for pagination).

        Returns
        -------
        list of LeadOpportunity

        """
        limit = min(limit, 500)
        conditions: list[str] = []
        params: dict[str, Any] = {}

        if status is not None:
            conditions.append("o.status = :status")
            params["status"] = status.value

        if platform is not None:
            conditions.append("o.platform = :platform")
            params["platform"] = platform

        if date_from is not None:
            conditions.append("o.created_at >= :date_from")
            params["date_from"] = date_from.isoformat()

        if date_to is not None:
            conditions.append("o.created_at <= :date_to")
            params["date_to"] = date_to.isoformat()

        if min_score is not None:
            conditions.append("o.score >= :min_score")
            params["min_score"] = min_score

        if text_query:
            # Try FTS5 first; if it returns nothing, fall back to LIKE
            # (this handles searches that match on non-FTS columns like skills
            # or cases where the FTS index hasn't been populated yet).
            fts_results = []
            try:
                fts_results = await self._search_fts(text_query, limit=limit, offset=offset, **params)
            except Exception:
                logger.debug("fts_search_failed, falling back to LIKE", query=text_query)

            if fts_results:
                return fts_results

            conditions.extend([
                "(o.title LIKE :text_query OR "
                "o.description LIKE :text_query OR "
                "o.company LIKE :text_query OR "
                "o.skills LIKE :text_query)"
            ])
            params["text_query"] = f"%{text_query}%"

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        stmt = (
            f"SELECT o.* FROM opportunities o "
            f"WHERE {where_clause} "
            f"ORDER BY o.created_at DESC "
            f"LIMIT :limit OFFSET :offset"
        )
        params["limit"] = limit
        params["offset"] = offset

        async with self._session_scope() as session:
            try:
                result = await session.execute(text(stmt), params)
                return [_row_to_opportunity(row) for row in result.mappings().fetchall()]
            except Exception as exc:
                raise DatabaseError(f"Search failed: {exc}", original=exc) from exc

    async def _search_fts(
        self,
        query: str,
        *,
        limit: int = 50,
        offset: int = 0,
        **extra_params: Any,
    ) -> list[LeadOpportunity]:
        """Full-text search via the FTS5 virtual table."""
        fts_query = " OR ".join(
            f'"{word}"*' for word in query.split() if word.strip()
        )
        if not fts_query:
            return []

        # Validate extra_params keys against known column names.
        invalid_keys = set(extra_params) - _VALID_FTS_COLUMNS
        if invalid_keys:
            msg = f"Invalid FTS column name(s): {', '.join(sorted(invalid_keys))}"
            raise ValueError(msg)

        conditions = ["opportunities_fts MATCH :fts_query"]
        params: dict[str, Any] = {
            "fts_query": fts_query,
            "limit": limit,
            "offset": offset,
        }
        params.update(extra_params)

        for key in extra_params:
            conditions.append(f"o.{key} = :{key}")

        where_extra = " AND ".join(conditions[1:])
        where_clause = conditions[0]
        if where_extra:
            where_clause += f" AND {where_extra}"

        stmt = (
            f"SELECT o.* FROM opportunities o "
            f"JOIN opportunities_fts ON o.rowid = opportunities_fts.rowid "
            f"WHERE {where_clause} "
            f"ORDER BY rank "
            f"LIMIT :limit OFFSET :offset"
        )

        async with self._session_scope() as session:
            result = await session.execute(text(stmt), params)
            return [_row_to_opportunity(row) for row in result.mappings().fetchall()]

    async def list_paginated(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[LeadOpportunity], int]:
        """List opportunities with pagination.

        Parameters
        ----------
        limit : int
            Page size (default 50, max 500).
        offset : int
            Number of records to skip.

        Returns
        -------
        tuple of (list of LeadOpportunity, total_count)

        """
        limit = min(limit, 500)

        async with self._session_scope() as session:
            count_result = await session.execute(text("SELECT COUNT(*) FROM opportunities"))
            total = count_result.scalar_one()

            result = await session.execute(
                text(
                    "SELECT * FROM opportunities "
                    "ORDER BY created_at DESC "
                    "LIMIT :limit OFFSET :offset"
                ),
                {"limit": limit, "offset": offset},
            )
            rows = [_row_to_opportunity(row) for row in result.mappings().fetchall()]

        return rows, total

    # ── UPDATE ─────────────────────────────────────────────────────────

    async def update(
        self, opportunity: LeadOpportunity, *, update_timestamp: bool = True
    ) -> LeadOpportunity:
        """Update an existing opportunity record.

        Parameters
        ----------
        opportunity : LeadOpportunity
            The opportunity with updated fields.  Must have an existing ID.
        update_timestamp : bool
            Whether to refresh *updated_at* (default *True*).

        Returns
        -------
        LeadOpportunity
            The updated opportunity.

        Raises
        ------
        OpportunityNotFound
            If the opportunity does not exist.

        """
        if update_timestamp:
            opportunity.touch()

        row = _opportunity_to_row(opportunity)
        set_clause = ", ".join(f"{k} = :{k}" for k in row if k != "id")

        async with self._session_scope() as session:
            result = await session.execute(
                text(f"UPDATE opportunities SET {set_clause} WHERE id = :id"),
                row,
            )

        if result.rowcount == 0:
            raise OpportunityNotFound(opportunity.id)

        logger.info("opportunity.updated", id=opportunity.id, status=opportunity.status.value)
        return opportunity

    async def update_status(
        self,
        opportunity_id: str,
        status: LeadStatus,
        *,
        notes: str | None = None,
        score: int | None = None,
    ) -> LeadOpportunity:
        """Update the status (and optionally notes / score) of an opportunity.

        A convenience method that avoids a full read-modify-write when only
        the status changes.

        Parameters
        ----------
        opportunity_id : str
            The opportunity ID.
        status : LeadStatus
            The new pipeline status.
        notes : str, optional
            Optional note to append or set.
        score : int, optional
            Optional qualification score to set (0-100).

        Returns
        -------
        LeadOpportunity
            The fully loaded, updated opportunity.

        Raises
        ------
        OpportunityNotFound
            If the ID does not exist.

        """
        now = datetime.now(UTC).isoformat()
        set_items = ["status = :status", "updated_at = :now"]
        params: dict[str, Any] = {"id": opportunity_id, "status": status.value, "now": now}

        if notes is not None:
            set_items.append("notes = :notes")
            params["notes"] = notes

        if score is not None:
            set_items.append("score = :score")
            params["score"] = score

        set_clause = ", ".join(set_items)

        async with self._session_scope() as session:
            result = await session.execute(
                text(f"UPDATE opportunities SET {set_clause} WHERE id = :id"),
                params,
            )

        if result.rowcount == 0:
            raise OpportunityNotFound(opportunity_id)

        logger.info("opportunity.status_updated", id=opportunity_id, status=status.value)
        return await self.get_by_id(opportunity_id)

    # ── UPSERT ─────────────────────────────────────────────────────────

    async def upsert(self, opportunity: LeadOpportunity) -> LeadOpportunity:
        """Insert or update an opportunity based on (platform, platform_job_id).

        This is the primary deduplication mechanism.  Uses a SQLite
        ``INSERT … ON CONFLICT … DO UPDATE`` statement to perform the
        operation atomically.

        .. important::
            Fields that represent pipeline state (``status``, ``score``,
            ``notes``) are **never** overwritten by an upsert — they are
            only set on initial insert.  This prevents a re-scrape from
            resetting pipeline progress.

        Parameters
        ----------
        opportunity : LeadOpportunity
            The opportunity to upsert.  If a record with the same
            *(platform, platform_job_id)* exists, its scraped fields
            (title, description, budget, skills, etc.) are updated;
            pipeline state fields are preserved.

        Returns
        -------
        LeadOpportunity
            The persisted opportunity as it exists in the database after
            the upsert.

        """
        row = _opportunity_to_row(opportunity)
        columns = ", ".join(row)
        placeholders = ", ".join(f":{k}" for k in row)

        # Fields that come from scraping — these can be safely overwritten.
        # Pipeline state fields (status, score, notes) are preserved.
        upsertable_fields = {
            "title", "description", "budget_min", "budget_max",
            "currency", "skills", "url", "location", "raw_data",
            "updated_at", "company",
        }

        conflict_updates = [
            f"{k} = excluded.{k}"
            for k in row
            if k in upsertable_fields
        ]
        conflict_set = ", ".join(conflict_updates)

        stmt = (
            f"INSERT INTO opportunities ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(platform, platform_job_id) DO UPDATE SET {conflict_set}"
        )

        async with self._session_scope() as session:
            try:
                await session.execute(text(stmt), row)
                # Fetch the persisted record back to get the real database state
                # (preserves existing status/score/notes, returns real id).
                result = await session.execute(
                    text(
                        "SELECT * FROM opportunities "
                        "WHERE platform = :platform AND platform_job_id = :job_id"
                    ),
                    {"platform": opportunity.platform, "job_id": opportunity.platform_job_id},
                )
                persisted = result.mappings().one()
                logger.info(
                    "opportunity.upserted",
                    id=persisted["id"],
                    platform=persisted["platform"],
                    title=persisted["title"][:60],
                )
                return _row_to_opportunity(persisted)
            except Exception as exc:
                logger.exception(
                    "opportunity.upsert_failed",
                    platform=opportunity.platform,
                    platform_job_id=opportunity.platform_job_id,
                    error=str(exc),
                )
                raise DatabaseError(f"Upsert failed: {exc}", original=exc) from exc

    # ── DELETE ─────────────────────────────────────────────────────────

    async def delete(self, opportunity_id: str) -> bool:
        """Delete an opportunity by ID.

        Parameters
        ----------
        opportunity_id : str
            The opportunity ID to delete.

        Returns
        -------
        bool
            *True* if a row was deleted, *False* if it did not exist.

        """
        async with self._session_scope() as session:
            result = await session.execute(
                text("DELETE FROM opportunities WHERE id = :id"),
                {"id": opportunity_id},
            )

        if result.rowcount:
            logger.info("opportunity.deleted", id=opportunity_id)
            return True
        return False

    # ── STATS ──────────────────────────────────────────────────────────

    async def get_stats(self) -> dict[str, int]:
        """Return aggregate counts keyed by status value.

        Example return value::

            {
                "total": 142,
                "discovered": 89,
                "qualified": 31,
                "drafted": 12,
                "reviewed": 6,
                "submitted": 2,
                "archived": 1,
                "rejected": 1,
            }

        Returns
        -------
        dict of str -> int

        """
        async with self._session_scope() as session:
            result = await session.execute(
                text(
                    "SELECT status, COUNT(*) as cnt FROM opportunities "
                    "GROUP BY status ORDER BY status"
                )
            )
            rows = result.fetchall()

        stats: dict[str, int] = {"total": 0}
        for row in rows:
            stats[row[0]] = row[1]
            stats["total"] += row[1]

        return stats

    async def get_platform_counts(self) -> dict[str, int]:
        """Return counts of opportunities grouped by platform.

        Returns
        -------
        dict of str -> int

        """
        async with self._session_scope() as session:
            result = await session.execute(
                text(
                    "SELECT platform, COUNT(*) as cnt FROM opportunities "
                    "GROUP BY platform ORDER BY platform"
                )
            )
            return {row[0]: row[1] for row in result.fetchall()}

    # ── DRAFT OPERATIONS ───────────────────────────────────────────────

    async def create_draft(self, draft: OutboundDraft) -> OutboundDraft:
        """Persist a new outreach draft.

        Parameters
        ----------
        draft : OutboundDraft
            The draft to persist.

        Returns
        -------
        OutboundDraft

        Raises
        ------
        DatabaseError
            If the insert fails.

        """
        row = _draft_to_row(draft)
        columns = ", ".join(row)
        placeholders = ", ".join(f":{k}" for k in row)

        async with self._session_scope() as session:
            try:
                await session.execute(
                    text(f"INSERT INTO drafts ({columns}) VALUES ({placeholders})"),
                    row,
                )
                logger.info("draft.created", id=draft.id, opportunity_id=draft.opportunity_id)
            except Exception as exc:
                raise DatabaseError(f"Failed to create draft: {exc}", original=exc) from exc

        return draft

    async def get_draft_by_id(self, draft_id: str) -> OutboundDraft:
        """Retrieve a draft by its ID.

        Parameters
        ----------
        draft_id : str
            The draft ID.

        Returns
        -------
        OutboundDraft

        Raises
        ------
        DraftNotFound
            If the ID does not exist.

        """
        async with self._session_scope() as session:
            result = await session.execute(
                text("SELECT * FROM drafts WHERE id = :id"),
                {"id": draft_id},
            )
            row = result.mappings().one_or_none()

        if row is None:
            raise DraftNotFound(draft_id)

        return _row_to_draft(row)

    async def get_drafts_for_opportunity(
        self, opportunity_id: str
    ) -> list[OutboundDraft]:
        """Return all drafts associated with an opportunity.

        Parameters
        ----------
        opportunity_id : str
            The opportunity ID.

        Returns
        -------
        list of OutboundDraft

        """
        async with self._session_scope() as session:
            result = await session.execute(
                text(
                    "SELECT * FROM drafts WHERE opportunity_id = :oid ORDER BY created_at DESC"
                ),
                {"oid": opportunity_id},
            )
            return [_row_to_draft(row) for row in result.mappings().fetchall()]

    async def update_draft(self, draft: OutboundDraft) -> OutboundDraft:
        """Update an existing draft record.

        Parameters
        ----------
        draft : OutboundDraft
            The draft with updated fields.

        Returns
        -------
        OutboundDraft

        Raises
        ------
        DraftNotFound
            If the draft ID does not exist.

        """
        draft.updated_at = datetime.now(UTC)
        row = _draft_to_row(draft)
        set_clause = ", ".join(f"{k} = :{k}" for k in row if k != "id")

        async with self._session_scope() as session:
            result = await session.execute(
                text(f"UPDATE drafts SET {set_clause} WHERE id = :id"),
                row,
            )

        if result.rowcount == 0:
            raise DraftNotFound(draft.id)

        logger.info("draft.updated", id=draft.id)
        return draft
