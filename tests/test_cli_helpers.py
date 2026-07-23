"""Tests for the CLI helper utilities."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from freelance_lead_gen.cli_helpers import validate_settings, write_dotenv

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

# ── write_dotenv tests ─────────────────────────────────────────────────────────


def test_write_dotenv_creates_new_file(tmp_path: Path) -> None:
    """write_dotenv should create a .env file when none exists."""
    env_path = tmp_path / ".env"
    assert not env_path.exists()

    write_dotenv("LLM_API_KEY", "sk-test-123", str(env_path))

    assert env_path.read_text() == "LLM_API_KEY=sk-test-123\n"


def test_write_dotenv_updates_existing_key(tmp_path: Path) -> None:
    """write_dotenv should replace an existing key rather than duplicate it."""
    env_path = tmp_path / ".env"
    env_path.write_text("LLM_API_KEY=sk-old\nDATABASE_URL=sqlite:///test.db\n")

    write_dotenv("LLM_API_KEY", "sk-new", str(env_path))

    lines = env_path.read_text().splitlines()
    assert "LLM_API_KEY=sk-new" in lines
    assert "DATABASE_URL=sqlite:///test.db" in lines
    assert len(lines) == 2  # No extra lines


def test_write_dotenv_appends_new_key(tmp_path: Path) -> None:
    """write_dotenv should add a new key without disturbing existing ones."""
    env_path = tmp_path / ".env"
    env_path.write_text("DATABASE_URL=sqlite:///test.db\n")

    write_dotenv("LLM_API_KEY", "sk-appended", str(env_path))

    content = env_path.read_text()
    assert "DATABASE_URL=sqlite:///test.db" in content
    assert "LLM_API_KEY=sk-appended" in content


def test_write_dotenv_preserves_other_lines(tmp_path: Path) -> None:
    """write_dotenv should preserve comments and blank lines in the file."""
    env_path = tmp_path / ".env"
    env_path.write_text("# This is a comment\n\nDATABASE_URL=sqlite:///test.db\n")

    write_dotenv("LLM_API_KEY", "sk-preserve", str(env_path))

    content = env_path.read_text()
    assert "# This is a comment" in content
    assert "DATABASE_URL=sqlite:///test.db" in content
    assert "LLM_API_KEY=sk-preserve" in content


# ── validate_settings tests ────────────────────────────────────────────────────


def test_validate_settings_missing_config_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Returns missing config.yaml and LLM_API_KEY when no config file exists."""
    monkeypatch.chdir(tmp_path)
    missing = validate_settings(require_llm_key=True)
    assert "config.yaml" in missing


def test_validate_settings_config_yaml_missing_llm_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Returns LLM_API_KEY missing when config.yaml has no api_key."""
    monkeypatch.chdir(tmp_path)
    config = {"llm": {"provider": "openai"}}
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    missing = validate_settings(require_llm_key=True)
    assert "LLM_API_KEY" in missing


def test_validate_settings_config_yaml_has_llm_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Returns empty list when config.yaml has the LLM key."""
    monkeypatch.chdir(tmp_path)
    config = {"llm": {"api_key": "sk-real-key"}}
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    missing = validate_settings(require_llm_key=True)
    assert missing == []


def test_validate_settings_does_not_require_llm_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Returns empty list when require_llm_key=False even without an LLM key."""
    monkeypatch.chdir(tmp_path)
    config = {"llm": {"provider": "openai"}}
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    missing = validate_settings(require_llm_key=False)
    assert missing == []
