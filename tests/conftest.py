"""Pytest fixtures for the freelance lead gen test suite."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from pytest import MonkeyPatch

from freelance_lead_gen.config.settings import Settings, get_settings
from freelance_lead_gen.discovery.browser import ManagedBrowser
from freelance_lead_gen.llm import LLMClient
from freelance_lead_gen.models.opportunity import LeadOpportunity, LeadStatus, OutboundDraft
from freelance_lead_gen.storage.database import close_db, init_db
from freelance_lead_gen.storage.migrations import apply_migrations
from freelance_lead_gen.storage.repository import OpportunityRepository


# ── Settings ──────────────────────────────────────────────────────────────────


@pytest.fixture
def test_settings(monkeypatch: MonkeyPatch) -> Settings:
    """Create a Settings instance with in-memory database and test-friendly defaults.

    Overrides key environment variables to isolate tests from production config.
    Clears the :func:`get_settings` cache so the new values are picked up.
    """
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("BROWSER_HEADLESS", "true")
    monkeypatch.setenv("DISCOVERY_MAX_DAILY", "10")
    get_settings.cache_clear()
    return get_settings()


# ── Database fixtures ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def in_memory_db(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> AsyncGenerator[None, None]:
    """Set up a fully-migrated in-memory (file-backed) SQLite database.

    Creates a temporary directory and places ``test.db`` inside it.  After the
    test the database engine is disposed and the temporary directory is cleaned
    up by pytest's ``tmp_path`` fixture.

    The fixture calls :func:`init_db`, runs all migrations via
    :func:`apply_migrations`, yields control, then tears down via
    :func:`close_db`.
    """
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    get_settings.cache_clear()

    await init_db()
    await apply_migrations()

    yield

    await close_db()
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def repository(in_memory_db: None) -> AsyncGenerator[OpportunityRepository, None]:
    """Provide an :class:`OpportunityRepository` backed by a test database.

    Depends on :func:`in_memory_db` so the database is already initialised and
    migrated.  The repository uses auto-scoped sessions by default.
    """
    repo = OpportunityRepository()
    yield repo


# ── Model fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def sample_opportunity() -> LeadOpportunity:
    """Return a realistic :class:`LeadOpportunity` with known test data."""
    return LeadOpportunity(
        platform="upwork",
        platform_job_id="test-123",
        title="AI Automation Engineer needed for RAG pipeline",
        company="Acme AI Corp",
        description=(
            "We are looking for an experienced AI Automation Engineer to build a "
            "RAG pipeline for our customer support system.\n\n"
            "Requirements:\n"
            "- 3+ years of Python experience\n"
            "- Experience with LangChain or LlamaIndex\n"
            "- Familiarity with vector databases (Pinecone, Weaviate)\n"
            "- Knowledge of LLM APIs (OpenAI, Anthropic)\n\n"
            "Budget: $5,000-$8,000\n"
            "Duration: 2-3 months"
        ),
        budget_min=5000.0,
        budget_max=8000.0,
        currency="USD",
        skills=["Python", "LangChain", "RAG", "LLM", "Vector Database", "OpenAI"],
        url="https://www.upwork.com/jobs/test-123",
        location="Remote",
        status=LeadStatus.DISCOVERED,
    )


@pytest.fixture
def sample_draft(sample_opportunity: LeadOpportunity) -> OutboundDraft:
    """Return a realistic :class:`OutboundDraft` with multiple versions."""
    return OutboundDraft(
        opportunity_id=sample_opportunity.id,
        versions=[
            (
                "Hi, I'm an experienced AI engineer with deep expertise in building "
                "RAG pipelines. I've worked extensively with LangChain and vector "
                "databases like Pinecone and Weaviate. I'd love to discuss how I can "
                "help build your customer support RAG system."
            ),
            (
                "Hello, I have extensive experience designing and implementing RAG "
                "pipelines for customer support. My background includes LangChain, "
                "LlamaIndex, and vector database optimisation at scale. I'm confident "
                "I can deliver a robust solution for your needs."
            ),
        ],
        current_version_index=0,
        subject="AI Automation proposal for RAG pipeline project",
    )


# ── Mock fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def mock_browser() -> AsyncMock:
    """Return an :class:`AsyncMock` configured as a :class:`ManagedBrowser`.

    The mock supports async context manager usage (``__aenter__`` / ``__aexit__``)
    and provides pre-configured async mocks for common methods such as
    :meth:`navigate`, :meth:`extract_text`, :meth:`click`, and :meth:`screenshot`.
    """
    mock = AsyncMock(spec=ManagedBrowser)

    mock.navigate = AsyncMock(return_value=mock)
    mock.extract_text = AsyncMock(return_value="Mocked page text from the test browser.")
    mock.text_content = AsyncMock(return_value="Mocked text content from the test browser.")
    mock.click = AsyncMock()
    mock.type_text = AsyncMock()
    mock.scroll = AsyncMock()
    mock.scroll_into_view = AsyncMock()
    mock.screenshot = AsyncMock(return_value=b"mock-screenshot-png-data")
    mock.start = AsyncMock()
    mock.stop = AsyncMock()
    mock.abort = AsyncMock()
    mock.wait = AsyncMock()
    mock.wait_for_selector = AsyncMock(return_value=True)
    mock.wait_for_navigation = AsyncMock()
    mock.is_element_visible = AsyncMock(return_value=True)
    mock.get_url = AsyncMock(return_value="https://www.upwork.com/")
    mock.get_title = AsyncMock(return_value="Upwork")
    mock.evaluate = AsyncMock(return_value=None)
    mock.get_cookies = AsyncMock(return_value=[])
    mock.set_cookies = AsyncMock()
    mock.save_cookies = AsyncMock()
    mock.load_cookies = AsyncMock(return_value=0)
    mock.fetch_via_page = AsyncMock(return_value={})
    mock.retry_navigation = AsyncMock(return_value=mock)
    mock.human_mouse_move = AsyncMock()
    mock.is_running = True

    # Async context manager support
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)

    return mock


@pytest.fixture
def mock_llm() -> AsyncMock:
    """Return an :class:`AsyncMock` configured as a :class:`LLMClient`.

    Pre-configures:
    - :meth:`chat_completion` returns a realistic qualification-style dict.
    - :meth:`structured_classify` returns a dict with ``qualified=True``, ``score=85``.
    - :meth:`classify` returns a plain text response.
    """
    mock = AsyncMock(spec=LLMClient)

    mock.chat_completion = AsyncMock(
        return_value={
            "qualified": True,
            "score": 85,
            "skill_match_score": 90,
            "budget_fit_score": 75,
            "clarity_score": 80,
            "reasoning": "Strong match: candidate has deep RAG pipeline experience matching the job description.",
            "risks": ["Budget range may be below market rate for senior AI engineers."],
        }
    )
    mock.structured_classify = AsyncMock(
        return_value={
            "qualified": True,
            "score": 85,
            "reasoning": "Good alignment with profile skills and experience level.",
            "risks": [],
        }
    )
    mock.classify = AsyncMock(return_value="positive")
    mock.close = AsyncMock()

    return mock


# ── Working directory fixture ─────────────────────────────────────────────────


@pytest.fixture
def tmp_working_dir(monkeypatch: MonkeyPatch, tmp_path: Path) -> Path:
    """Create a temporary directory and change the working directory to it.

    Useful for tests that write config files or interact with the filesystem
    relative to ``Path.cwd()``.  The original working directory is restored
    after the test.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── Async event loop ──────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def event_loop() -> asyncio.AbstractEventLoop:
    """Provide a session-scoped event loop for ``pytest-asyncio``.

    This avoids creating and tearing down a new event loop for every async test,
    which is especially important when async fixtures share a module-level
    resource such as the database engine.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()
