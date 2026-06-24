# Security & Credential Management

> How freelance-lead-gen handles secrets, browser sessions, and safe configuration.

---

## Principles

1. **Never hardcode secrets** — all credentials come from environment variables or `.env`.
2. **Never log secrets** — Pydantic serialisers redact sensitive fields automatically.
3. **Never commit secrets** — `.gitignore` excludes `.env`, `browser_data/`, `*.key`, `secrets/`.
4. **Least privilege** — each credential is scoped to a single platform.
5. **Auditable** — `PlatformCredentials.redacted()` produces safe-to-log representations.

---

## Credential Storage

### Platform Credentials

Platform authentication data (passwords, API keys, tokens) is stored in
`PlatformCredentials` models under `models/platform.py`:

```python
class PlatformCredentials(BaseModel):
    platform: Platform
    username: str | None
    password: str | None        # ← auto-redacted
    api_key: str | None         # ← auto-redacted
    token: str | None           # ← auto-redacted
    cookies: dict | None        # ← auto-redacted
```

**Secret redaction** is automatic via `@field_serializer`:

```python
@field_serializer("password", "api_key", "token", "cookies")
def _redact_secrets(self, value, _info):
    if value is None:
        return None
    return "********"
```

This means:
- `print(creds.model_dump())` → `{"password": "********", ...}`
- `str(creds)` → `PlatformCredentials(platform='upwork', password='********', ...)`
- Logging the model never exposes secrets

### API Keys

LLM API keys are set via environment variables:

```bash
LLM_API_KEY=sk-...
```

The `Settings` model loads these via pydantic-settings with `env_prefix="LLM_"`.
If no key is provided, a placeholder `"sk-placeholder"` is used to satisfy the
OpenAI SDK (for local/Ollama endpoints that accept any token).

### Browser Sessions

Persistent browser sessions (cookies, local storage) are stored in
`browser_data/` which is excluded from version control via `.gitignore`.
The `ManagedBrowser` can save/load cookies from JSON files for session
restoration, but these files should be treated as secrets.

---

## Environment Configuration

### `.env` file

Copy the template and fill in your values:

```bash
cp .env.example .env
```

The `.env` file is **never committed** — `.gitignore` contains `.env` and `.env.*`
(except `.env.example` and `.env.template`).

### Required environment variables

| Variable | Purpose | Sensitive? |
|----------|---------|------------|
| `LLM_API_KEY` | OpenAI-compatible API key | Yes |
| `LLM_BASE_URL` | API endpoint URL | No |
| `LLM_MODEL` | Model identifier | No |
| `BROWSER_HEADLESS` | Run headless or visible | No |
| `DATABASE_PATH` | SQLite file location | No |

### Platform credentials (optional)

```bash
UPWORK_USERNAME=...
UPWORK_PASSWORD=...        # Sensitive
LINKEDIN_USERNAME=...
LINKEDIN_PASSWORD=...      # Sensitive
FREELANCER_USERNAME=...
FREELANCER_PASSWORD=...    # Sensitive
```

---

## Runtime Security

### Database

- SQLite with **WAL mode** for safe concurrent access
- Foreign key constraints enforced
- Busy timeout prevents deadlocks
- Database files (`*.sqlite`, `*.db`) excluded from version control

### Browser Automation

- Fingerprint rotation per session to avoid tracking
- Configurable user-agent and viewport
- Proxy support for traffic routing
- No hardcoded credentials in extraction scripts

### Logging

- `structlog` for structured, JSON-friendly logging
- Credential models use custom serialisers that always redact secrets
- Raw HTML content extracted from platforms is never logged at INFO level

### Terminal UI

- The Textual TUI runs locally and does not expose network services
- No authentication tokens are displayed in the UI
- Draft content (which may contain personal information) is only shown for approved reviewing

---

## Attack Surface

| Surface | Risk | Mitigation |
|---------|------|------------|
| `.env` file | Credential leak | `.gitignore`, `build` exclusion in `pyproject.toml` |
| Browser user data | Session hijacking | Excluded from VCS, stored locally |
| LLM API key | Unauthorised LLM usage | Env var only, never logged |
| Platform credentials | Account compromise | Redacted in all output |
| SQLite DB | Data exposure | Excluded from VCS |
| Browser automation | Detection/blocking | Stealth config, jitter, fingerprint rotation |

---

## Secure Development Guidelines

1. **Never** add secrets to Python source files
2. **Never** log the return value of `PlatformCredentials.model_dump(secrets=True)` — use `.redacted()` instead
3. **Always** use `.gitignore` to exclude sensitive directories
4. **Rotate** browser user-data directories periodically to avoid accumulated fingerprinting
5. **Review** extracted HTML content for embedded credentials before committing any debug output
6. **Use environment-specific `.env` files** for different deployment targets
7. **Prefer read-only API tokens** where platforms offer them

---

## CI/CD Security

The CI pipeline (`.github/workflows/ci.yml`) runs tests with a mock API key
and does **not** have access to production credentials. Secrets are only
available in:

- Local development `.env` files
- Runtime environment variables (not checked into CI)

---

## Dependency Supply Chain

| Dependency | Risk | Mitigation |
|------------|------|------------|
| `playwright` | Binary downloads | Use pinned version, checksum verification |
| `openai` | API key exposure | Never pass to subprocess |
| `langchain` | Broad dependency tree | Dependabot configured for weekly updates |
| `textual` | Terminal rendering | Pinned minor version |

Dependabot is configured for **weekly** dependency updates to balance
security patches against update fatigue.
