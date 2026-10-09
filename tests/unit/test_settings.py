"""Tests for multi-database configuration and security enforcement fields.

The codex review noted that several configuration fields were declared but never
wired into the running system. These tests lock in the new behaviour:

* ``Settings.databases`` always contains the primary database.
* ``SecurityConfig.blocked_tables`` / ``blocked_columns`` / ``allow_explain`` are
  configurable (including from comma-separated env strings).
* ``ResilienceConfig.query_rate_limit`` / ``llm_rate_limit`` are configurable.
"""
from pg_mcp.config.settings import (
    DatabaseConfig,
    OpenAIConfig,
    ResilienceConfig,
    SecurityConfig,
    Settings,
)


def _make_settings(**overrides) -> Settings:
    kwargs: dict = {
        "_env_file": None,
        "database": DatabaseConfig(name="primary", host="localhost"),
        "openai": OpenAIConfig(api_key="sk-test"),
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def test_primary_database_always_in_databases() -> None:
    settings = _make_settings()
    assert "primary" in settings.databases
    assert settings.databases["primary"].name == "primary"


def test_secondary_databases_preserved() -> None:
    secondary = DatabaseConfig(name="analytics", host="analytics.db")
    settings = _make_settings(databases={"analytics": secondary})
    assert "primary" in settings.databases
    assert "analytics" in settings.databases
    assert settings.databases["analytics"] is secondary


def test_security_blocked_tables_default_empty() -> None:
    sec = SecurityConfig()
    assert sec.blocked_tables == []
    assert sec.blocked_columns == []
    assert sec.allow_explain is False


def test_security_blocked_tables_csv_parsing() -> None:
    sec = SecurityConfig(blocked_tables="users, secrets , tokens")
    assert sec.blocked_tables == ["users", "secrets", "tokens"]


def test_security_blocked_columns_csv_parsing() -> None:
    sec = SecurityConfig(blocked_columns="password, ssn")
    assert sec.blocked_columns == ["password", "ssn"]


def test_security_allow_explain_true() -> None:
    assert SecurityConfig(allow_explain=True).allow_explain is True


def test_resilience_rate_limit_defaults() -> None:
    rc = ResilienceConfig()
    assert rc.query_rate_limit == 10
    assert rc.llm_rate_limit == 5
