"""Simple inline migration system using SQLAlchemy DDL.

Instead of requiring Alembic to be wired up at this stage, this module
provides a lightweight migration runner that tracks applied migrations in
a ``_migrations`` table and applies pending ones in order.

Usage::

    from freelance_lead_gen.storage.database import init_db
    from freelance_lead_gen.storage.migrations import apply_migrations

    await init_db()
    await apply_migrations()
"""

from __future__ import annotations as _annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import text

from freelance_lead_gen.storage.database import get_engine

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncConnection

logger = structlog.get_logger(__name__)

# ── Migration record type ────────────────────────────────────────────────────


class Migration:
    """A single database migration.

    Each migration has a unique *id*, a human-readable *description*, and
    the DDL statements to execute in *up*.  Migrations are applied in order
    of their *id*.
    """

    def __init__(
        self,
        migration_id: str,
        description: str,
        up: list[str],
    ) -> None:
        self.id = migration_id
        self.description = description
        self.up = up


# ── Migration registry table DDL ─────────────────────────────────────────────

_REGISTRY_TABLE = "_migrations"

_CREATE_REGISTRY_TABLE = """
CREATE TABLE IF NOT EXISTS _migrations (
    id          TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    checksum    TEXT NOT NULL DEFAULT ''
)
"""

# ── Migration definitions ────────────────────────────────────────────────────

MIGRATIONS: list[Migration] = [
    Migration(
        migration_id="001_initial",
        description="Create opportunities, drafts, and migrations tracking tables",
        up=[
            # ── opportunities ────────────────────────────────────────────────
            """
            CREATE TABLE IF NOT EXISTS opportunities (
                id                  TEXT PRIMARY KEY,
                platform            TEXT NOT NULL,
                platform_job_id     TEXT NOT NULL,
                title               TEXT NOT NULL,
                company             TEXT,
                description         TEXT NOT NULL,
                budget_min          REAL,
                budget_max          REAL,
                currency            TEXT NOT NULL DEFAULT 'USD',
                skills              TEXT NOT NULL DEFAULT '[]',
                posted_date         TEXT,
                url                 TEXT,
                location            TEXT,
                status              TEXT NOT NULL DEFAULT 'discovered',
                score               INTEGER,
                notes               TEXT,
                raw_data            TEXT NOT NULL DEFAULT '{}',
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL
            )
            """,
            # Unique constraint: one record per platform listing.
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_opportunities_platform_job
                ON opportunities(platform, platform_job_id)
            """,
            # Indexes for common query patterns.
            """
            CREATE INDEX IF NOT EXISTS idx_opportunities_status
                ON opportunities(status)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_opportunities_created_at
                ON opportunities(created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_opportunities_platform
                ON opportunities(platform)
            """,
            # Full-text search via FTS5 (optional, for text search).
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS opportunities_fts USING fts5(
                title, description, company,
                content='opportunities',
                content_rowid='rowid'
            )
            """,
            # FTS5 content sync triggers — keep the FTS index in sync with
            # INSERTs, UPDATEs, and DELETEs on the opportunities table.
            """
            CREATE TRIGGER IF NOT EXISTS opportunities_fts_ai AFTER INSERT ON opportunities BEGIN
                INSERT INTO opportunities_fts(rowid, title, description, company)
                VALUES (new.rowid, new.title, new.description, new.company);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS opportunities_fts_ad AFTER DELETE ON opportunities BEGIN
                INSERT INTO opportunities_fts(opportunities_fts, rowid, title, description, company)
                VALUES ('delete', old.rowid, old.title, old.description, old.company);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS opportunities_fts_au AFTER UPDATE ON opportunities BEGIN
                INSERT INTO opportunities_fts(opportunities_fts, rowid, title, description, company)
                VALUES ('delete', old.rowid, old.title, old.description, old.company);
                INSERT INTO opportunities_fts(rowid, title, description, company)
                VALUES (new.rowid, new.title, new.description, new.company);
            END
            """,
            # ── drafts ──────────────────────────────────────────────────────
            """
            CREATE TABLE IF NOT EXISTS drafts (
                id                      TEXT PRIMARY KEY,
                opportunity_id          TEXT NOT NULL,
                versions                TEXT NOT NULL DEFAULT '[]',
                current_version_index   INTEGER NOT NULL DEFAULT 0,
                subject                 TEXT,
                approved                INTEGER NOT NULL DEFAULT 0,
                human_edited            INTEGER NOT NULL DEFAULT 0,
                created_at              TEXT NOT NULL,
                updated_at              TEXT NOT NULL,
                FOREIGN KEY (opportunity_id) REFERENCES opportunities(id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_drafts_opportunity_id
                ON drafts(opportunity_id)
            """,
        ],
    ),
    Migration(
        migration_id="002_index_score",
        description="Add indexes for score-based queries and filtered searches",
        up=[
            # Composite index for filtered searches (status + score).
            """
            CREATE INDEX IF NOT EXISTS idx_opportunities_status_score
                ON opportunities(status, score)
            """,
            # Index for score range queries alone.
            """
            CREATE INDEX IF NOT EXISTS idx_opportunities_score
                ON opportunities(score)
            """,
        ],
    ),
    Migration(
        migration_id="002_fts_triggers",
        description="Add FTS5 content-synchronisation triggers",
        up=[
            """
            CREATE TRIGGER IF NOT EXISTS opportunities_fts_ai AFTER INSERT ON opportunities BEGIN
                INSERT INTO opportunities_fts(rowid, title, description, company)
                VALUES (new.rowid, new.title, new.description, new.company);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS opportunities_fts_ad AFTER DELETE ON opportunities BEGIN
                INSERT INTO opportunities_fts(opportunities_fts, rowid, title, description, company)
                VALUES ('delete', old.rowid, old.title, old.description, old.company);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS opportunities_fts_au AFTER UPDATE ON opportunities BEGIN
                INSERT INTO opportunities_fts(opportunities_fts, rowid, title, description, company)
                VALUES ('delete', old.rowid, old.title, old.description, old.company);
                INSERT INTO opportunities_fts(rowid, title, description, company)
                VALUES (new.rowid, new.title, new.description, new.company);
            END
            """,
            # Rebuild the FTS index for any existing data.
            "INSERT INTO opportunities_fts(opportunities_fts) VALUES('rebuild')",
        ],
    ),
]


# ── Migration runner ─────────────────────────────────────────────────────────


async def _ensure_registry_table(conn: AsyncConnection) -> None:
    """Create the migration registry table if it does not exist."""
    await conn.execute(text(_CREATE_REGISTRY_TABLE))


async def _get_applied_ids(conn: AsyncConnection) -> set[str]:
    """Return the set of already-applied migration IDs."""
    result = await conn.execute(text(f"SELECT id FROM {_REGISTRY_TABLE}"))
    return {row[0] for row in result.fetchall()}


async def _record_migration(
    conn: AsyncConnection,
    migration: Migration,
) -> None:
    """Insert a record of the applied migration into the registry."""
    migration_id = migration.id
    description = migration.description
    applied_at = (
        datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(UTC).microsecond:06d}Z"
    )
    # Simple checksum: first 8 hex chars of a UUID based on the migration content.
    checksum = uuid.uuid5(uuid.NAMESPACE_DNS, "".join(migration.up)).hex[:8]

    await conn.execute(
        text(
            f"INSERT INTO {_REGISTRY_TABLE} (id, description, applied_at, checksum) "
            "VALUES (:id, :description, :applied_at, :checksum)"
        ),
        {
            "id": migration_id,
            "description": description,
            "applied_at": applied_at,
            "checksum": checksum,
        },
    )


async def apply_migrations(*, to_id: str | None = None) -> list[str]:
    """Apply all pending migrations.

    Parameters
    ----------
    to_id : str, optional
        If provided, only apply migrations up to (and including) this
        migration ID.  All later migrations are skipped.

    Returns
    -------
    list of str
        The IDs of the migrations that were applied in this run.

    Raises
    ------
    RuntimeError
        If the database engine has not been initialised.

    """
    engine = get_engine()
    applied: list[str] = []

    async with engine.begin() as conn:
        await _ensure_registry_table(conn)
        already_applied = await _get_applied_ids(conn)

        for migration in MIGRATIONS:
            if migration.id in already_applied:
                logger.debug("migration.skipped", id=migration.id)
                continue

            if to_id is not None and migration.id > to_id:
                break

            logger.info("migration.applying", id=migration.id, description=migration.description)

            for stmt in migration.up:
                cleaned = stmt.strip()
                if cleaned:
                    try:
                        await conn.execute(text(cleaned))
                    except Exception as exc:
                        logger.exception(
                            "migration.statement_failed",
                            id=migration.id,
                            statement=cleaned[:120],
                            error=str(exc),
                        )
                        raise

            await _record_migration(conn, migration)
            applied.append(migration.id)
            logger.info("migration.applied", id=migration.id)

    if applied:
        logger.info("migration.complete", count=len(applied), ids=applied)
    else:
        logger.info("migration.no_pending")

    return applied


async def get_migration_status() -> Sequence[dict[str, Any]]:
    """Return the status of all migrations (applied vs pending).

    Returns
    -------
    list of dict
        Each dict has keys: ``id``, ``description``, ``applied`` (bool),
        ``applied_at`` (str or None).

    """
    engine = get_engine()
    async with engine.connect() as conn:
        await _ensure_registry_table(conn)
        applied = await _get_applied_ids(conn)

        rows: list[dict[str, Any]] = []
        for migration in MIGRATIONS:
            row: dict[str, Any] = {
                "id": migration.id,
                "description": migration.description,
                "applied": migration.id in applied,
                "applied_at": None,
            }
            if migration.id in applied:
                result = await conn.execute(
                    text(f"SELECT applied_at FROM {_REGISTRY_TABLE} WHERE id = :id"),
                    {"id": migration.id},
                )
                row_result = result.fetchone()
                if row_result is not None:
                    row["applied_at"] = row_result[0]
            rows.append(row)

        return rows
