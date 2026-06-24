# Development Guide

> Setup instructions, testing guidelines, and how to add new platforms.

---

## Prerequisites

- **Python 3.11+**
- **uv** (recommended package manager) or pip
- **Playwright browsers** (for discovery/extraction)
- **An LLM API key** (OpenAI-compatible endpoint)

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/iknowkungfubar/freelance-lead-gen.git
cd freelance-lead-gen

# Create virtual environment and install dependencies
uv venv
source .venv/bin/activate
uv sync

# Install dev dependencies
uv sync --group dev
```

### 2. Install Playwright browsers

```bash
playwright install chromium
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env with your LLM API key and preferences
```

### 4. Verify setup

```bash
freelance-lead-gen init
freelance-lead-gen --help
```

---

## Project Structure

```
freelance-lead-gen/
├── src/
│   └── freelance_lead_gen/
│       ├── __init__.py          # Package metadata
│       ├── __main__.py          # `python -m freelance_lead_gen`
│       ├── cli.py               # Click CLI entrypoint
│       ├── llm.py               # LLM client (OpenAI-compatible)
│       ├── agents/              # Pipeline agents
│       │   ├── orchestrator.py  # LeadGenOrchestrator
│       │   ├── filtering_agent.py
│       │   ├── personalization_agent.py
│       │   ├── verification_agent.py
│       │   └── profile_matcher.py
│       ├── config/              # Settings and prompts
│       │   ├── settings.py
│       │   └── prompts.py
│       ├── discovery/           # Browser automation
│       │   ├── browser.py       # ManagedBrowser
│       │   ├── extractor.py     # GenericPlaywrightExtractor
│       │   ├── discovery_agent.py
│       │   ├── scheduler.py
│       │   └── platforms/       # Per-platform extractors
│       ├── models/              # Pydantic domain models
│       │   ├── opportunity.py
│       │   ├── pipeline.py
│       │   └── platform.py
│       ├── storage/             # Database layer
│       │   ├── database.py
│       │   ├── migrations.py
│       │   └── repository.py
│       ├── ui/                  # Textual TUI
│       │   ├── app.py
│       │   ├── dashboard.py
│       │   ├── lead_list.py
│       │   ├── lead_detail.py
│       │   ├── content_editor.py
│       │   ├── review_queue.py
│       │   └── widgets.py
│       └── utils/               # Utilities
│           ├── fingerprint.py
│           └── logging.py
├── tests/
│   ├── conftest.py              # Shared fixtures
│   ├── test_cli.py
│   ├── test_integration.py
│   ├── test_models.py
│   ├── test_storage.py
│   ├── test_discovery/
│   │   ├── __init__.py
│   │   ├── test_browser.py
│   │   └── test_extractor.py
│   ├── test_agents/
│   │   ├── __init__.py
│   │   ├── test_orchestrator.py
│   │   ├── test_filtering.py
│   │   ├── test_personalization.py
│   │   └── test_verification.py
│   └── test_ui/
│       └── test_app.py
├── docs/
│   ├── architecture.md
│   ├── security.md
│   └── development.md
├── .github/
│   ├── workflows/ci.yml
│   └── dependabot.yml
├── pyproject.toml
├── README.md
└── .env.example
```

---

## Running Tests

### All tests

```bash
pytest
```

### With coverage

```bash
pytest --cov=freelance_lead_gen --cov-report=term-missing
```

### Test markers

| Marker | Usage | Skip with |
|--------|-------|-----------|
| `smoke` | Fast smoke tests | `-m "not smoke"` |
| `integration` | Full pipeline integration | `-m "not integration"` |
| `slow` | Slow tests (LLM calls) | `-m "not slow"` |
| `network` | Network-dependent tests | `-m "not network"` |
| `hitl` | Human-in-the-loop interface | `-m "not hitl"` |

### Example

```bash
# Run only smoke tests
pytest -m smoke

# Run everything except slow and network tests
pytest -m "not slow and not network"
```

---

## Code Quality

The project uses **ruff** for linting and formatting:

```bash
# Lint
ruff check src/

# Format
ruff format src/

# Type check
mypy src/
```

Configuration is in `pyproject.toml`:
- Line length: 100
- Target: Python 3.11
- Quoting: double quotes
- Docstrings: conventionally accepted (some rules relaxed)

---

## Linting Rules

The Ruff config in `pyproject.toml` selects **ALL** rules by default and
explicitly ignores certain categories:

| Rule | Reason |
|------|--------|
| `D` (pydocstyle) | Mostly ignored for speed; docstrings written manually |
| `ANN` (annotations) | Relaxed for self/cls and private functions |
| `S311` | Pseudorandom generators acceptable for jitter/stealth |
| `T201` | `print()` allowed in CLI code |
| `EM101/102` | String literals in exceptions accepted |

Per-file overrides:
- `tests/**` — most rules relaxed (assertions allowed, no annotation requirements)
- `alembic/**` — docstring and line-length rules relaxed

---

## Adding a New Platform

### 1. Define platform enum

Add the platform to `Platform` enum in `models/platform.py`:

```python
class Platform(str, Enum):
    UPWORK = "upwork"
    LINKEDIN = "linkedin"
    # ...
    MY_NEW_PLATFORM = "my_new_platform"
```

Add a display name in `_DISPLAY_NAMES`.

### 2. Configure anti-bot profile

Add an entry in `_ANTI_BOT_PROFILES` in `models/platform.py`:

```python
_ANTI_BOT_PROFILES: dict[str, dict[str, Any]] = {
    "my_new_platform": {
        "stealth": True,
        "humanize_mouse": True,
        "random_delay_range": (1.0, 4.0),
        "avoid_webdriver_detect": True,
    },
    # ...
}
```

### 3. Create a platform extractor

Create `discovery/platforms/my_new_platform.py`:

```python
"""Extractor for MyNewPlatform.com."""

from freelance_lead_gen.discovery.browser import ManagedBrowser
from freelance_lead_gen.discovery.extractor import GenericPlaywrightExtractor, RawLead


class MyNewPlatformExtractor(GenericPlaywrightExtractor):
    """Extractor for MyNewPlatform job listings."""

    def __init__(self, browser: ManagedBrowser) -> None:
        super().__init__(
            browser=browser,
            search_url_template="https://mynewplatform.com/search?q={query}",
            card_selector=".job-card",
            title_selector="h2.job-title a",
            url_selector="h2.job-title a",
            description_selector=".job-description",
            company_selector=".company-name",
            budget_selector=".budget",
            posted_date_selector=".posted-date",
            next_page_selector="a.next-page",
            platform_name="my_new_platform",
        )
```

### 4. Register the extractor

Add it to the `PLATFORM_EXTRACTORS` dict in `discovery/platforms/__init__.py`.

### 5. Add platform credentials model

If the platform requires auth, add a credentials entry in the appropriate
section and ensure `UPWORK_*` env-var patterns are documented in `.env.example`.

### 6. Enable the platform

Add the platform name to the `PLATFORMS_ENABLED` env var:

```bash
PLATFORMS_ENABLED=upwork,linkedin,freelancer,my_new_platform
```

Or set the display name in `discovery/platforms/__init__.py`.

### 7. Write tests

Create `tests/test_discovery/test_my_new_platform.py` with:
- Extractor configuration tests
- Budget parsing tests (if the platform uses a different format)
- Mock browser tests

---

## CLI Reference

```bash
freelance-lead-gen init               # Initialize database
freelance-lead-gen discover           # Run discovery cycle
freelance-lead-gen pipeline           # Run full pipeline
freelance-lead-gen review             # Launch review TUI
freelance-lead-gen list               # List opportunities
freelance-lead-gen stats              # Show pipeline stats
freelance-lead-gen serve              # Start scheduler daemon
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `opencode` | Provider name for logging |
| `LLM_MODEL` | `deepseek-v4-flash` | Model identifier |
| `LLM_BASE_URL` | `https://opencode.ai/zen/go/v1` | API endpoint |
| `LLM_API_KEY` | `""` | API key |
| `LLM_MAX_RETRIES` | `3` | API call retries |
| `LLM_TIMEOUT_SECONDS` | `120` | Request timeout |
| `BROWSER_HEADLESS` | `false` | Headless mode |
| `BROWSER_USER_DATA_DIR` | `./browser_data` | Profile directory |
| `DATABASE_PATH` | `./data/leads.db` | SQLite file |
| `DISCOVERY_MAX_DAILY` | `50` | Max ops/day |
| `DISCOVERY_SCHEDULE_INTERVAL_MINUTES` | `60` | Schedule interval |
| `HITL_ENABLED` | `true` | Human review gate |
| `HITL_AUTO_APPROVE` | `false` | Auto-approve drafts |

---

## Adding Tests

### Test structure

Tests follow the project conventions:

1. **Unit tests** test one class or function in isolation
2. **Integration tests** test the pipeline with mocked external dependencies
3. **Characterisation tests** pin down behaviour before refactoring

### Fixtures

Shared fixtures are in `conftest.py`:

| Fixture | Purpose |
|---------|---------|
| `test_settings` | Isolated settings with in-memory DB |
| `in_memory_db` | Migrated SQLite database in tmpdir |
| `repository` | OpportunityRepository backed by in-memory DB |
| `sample_opportunity` | Pre-built LeadOpportunity |
| `sample_draft` | Pre-built OutboundDraft with versions |
| `mock_browser` | Mock ManagedBrowser with all methods |
| `mock_llm` | Mock LLMClient with structured responses |

### Writing a new test file

```python
"""Tests for the WidgetProcessor."""

from unittest.mock import AsyncMock

import pytest

from freelance_lead_gen.module import WidgetProcessor


@pytest.mark.asyncio
async def test_process_valid_input() -> None:
    """Verify that a valid widget is processed correctly."""
    processor = WidgetProcessor()
    result = await processor.process("valid")
    assert result.success is True
    assert result.value == "processed: valid"


@pytest.mark.asyncio
async def test_process_empty_input() -> None:
    """Verify that empty input raises an appropriate error."""
    processor = WidgetProcessor()
    with pytest.raises(ValueError, match="Input cannot be empty"):
        await processor.process("")
```
