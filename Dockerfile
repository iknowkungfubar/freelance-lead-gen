FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# System deps for Playwright (browser engine)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk-bridge2.0-0 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libatspi2.0-0 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv (Python package manager)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install dependencies (cached layer — only rebuilds when pyproject.toml / uv.lock change)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application code and supporting files
COPY src/ src/
COPY .env.example .env.example
COPY data/ data/
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Install Playwright browsers
RUN uv run playwright install chromium --with-deps

EXPOSE 8080

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["uv", "run", "python", "-m", "freelance_lead_gen", "serve"]
