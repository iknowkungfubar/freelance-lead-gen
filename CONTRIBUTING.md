# Contributing

Thanks for your interest in contributing to **Freelance Lead Gen**! This document provides guidelines for reporting issues, adding features, and submitting changes.

---

## Table of Contents

- [Reporting Issues](#reporting-issues)
- [Feature Requests](#feature-requests)
- [Adding a New Platform Extractor](#adding-a-new-platform-extractor)
- [Pull Request Process](#pull-request-process)
- [Code Style](#code-style)
- [Development Setup](#development-setup)

---

## Reporting Issues

Before opening an issue, please search the existing issues to see if it has already been reported.

When filing a bug report, include:

1. **A clear description** of the bug and how to reproduce it
2. **Steps to reproduce** — minimal, reproducible example
3. **Expected behavior** — what you expected to happen
4. **Actual behavior** — what actually happened (include error output, logs, screenshots)
5. **Environment** — OS, Python version, package version, browser version
6. **Configuration** — relevant settings from your `.env` (redact secrets!)

Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md) when filing.

---

## Feature Requests

Feature requests are welcome. Please provide:

1. **The problem** — what are you trying to solve?
2. **The proposed solution** — how should the feature work?
3. **Alternatives considered** — what else have you explored?
4. **Additional context** — links, references, examples

Use the [feature request template](.github/ISSUE_TEMPLATE/feature_request.md).

---

## Adding a New Platform Extractor

Platform extractors allow the system to discover opportunities from new sources. See the [development guide](docs/development.md#adding-a-new-platform) for the full walkthrough.

In brief:

1. Add the platform to the `Platform` enum in `models/platform.py`
2. Add an anti-bot profile entry in `models/platform.py`
3. Create a new extractor class in `discovery/platforms/` extending `GenericPlaywrightExtractor`
4. Register it in `discovery/platforms/__init__.py`
5. Add platform credentials entries to `.env.example`
6. Write tests in `tests/test_discovery/`
7. Enable the platform via `PLATFORMS_ENABLED` in your `.env`

---

## Pull Request Process

1. **Create a feature branch** from `main`:
   ```bash
   git checkout -b feat/my-feature
   ```

2. **Make your changes** — keep them focused on one concern per PR.

3. **Write or update tests** — ensure coverage for new functionality.

4. **Run the full test suite**:
   ```bash
   pytest
   ```

5. **Run linters and type checks**:
   ```bash
   ruff check src/
   ruff format --check src/
   mypy src/
   ```

6. **Commit your changes** with a descriptive message:
   ```bash
   git commit -m "feat: add MyNewPlatform extractor"
   ```

7. **Push and open a pull request** against `main`:
   ```bash
   git push origin feat/my-feature
   ```

8. **Fill out the PR template** — describe what changed and why.

9. **Wait for CI** — all checks must pass before merging.

10. **Address review feedback** — maintainers may request changes.

---

## Code Style

The project enforces consistent code style through automated tooling:

- **Python 3.11+** — all code must be compatible with Python 3.11 and above
- **Ruff** — linting and formatting (config in `pyproject.toml`)
  - Line length: 100 characters
  - Quote style: double quotes
  - Run `ruff check src/` and `ruff format src/` before committing
- **Mypy** — strict type checking required
  - All public functions must have type annotations
  - Run `mypy src/` before committing
- **Pydantic v2** — domain models use Pydantic for validation and serialisation
- **Async-first** — I/O operations should be async (asyncio) where feasible
- **Structured logging** — use `structlog` instead of `print()` or `logging`
- **Docstrings** — Google-style or descriptive docstrings for public APIs

### Ruff rules

The project selects **ALL** lint rules and explicitly ignores categories that are too noisy for this codebase:

| Ignored rule | Reason |
|-------------|--------|
| `D` (pydocstyle) | Docstring rules relaxed for speed |
| `ANN` (annotations) | Relaxed for `self`/`cls` and private functions |
| `S311` | Pseudorandom generators OK for jitter/stealth |
| `T201` | `print()` allowed in CLI code |
| `EM101/102` | String literals in exceptions accepted |

See `pyproject.toml` for the full ignore list.

---

## Development Setup

```bash
# Clone and install
git clone https://github.com/iknowkungfubar/freelance-lead-gen.git
cd freelance-lead-gen
uv venv
source .venv/bin/activate
uv sync

# Install dev dependencies
uv sync --group dev

# Install Playwright browsers
playwright install chromium

# Verify setup
freelance-lead-gen init
freelance-lead-gen --help
```

See the [development guide](docs/development.md) for details.

---

## Licensing

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
