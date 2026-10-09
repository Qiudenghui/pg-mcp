"""Integration tests for the end-to-end orchestrator wiring.

These exercises confirm the codex-review fixes behave correctly together:

* Multi-database routing selects the executor for the *requested* database and
  rejects databases that were not configured (security: no access to unknown DBs).
* The configured ``SQLValidator`` (blocked tables / columns / EXPLAIN policy)
  actually blocks queries end-to-end.
* Transient DB failures are retried with backoff, and a successful run is
  recorded in the metrics collector.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from pg_mcp.config.settings import ResilienceConfig, SecurityConfig, ValidationConfig
from pg_mcp.models.errors import DatabaseError
from pg_mcp.models.query import QueryRequest, ReturnType
from pg_mcp.models.schema import ColumnInfo, DatabaseSchema, TableInfo
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.sql_validator import SQLValidator


def _schema() -> DatabaseSchema:
    return DatabaseSchema(
        database_name="test_db",
        tables=[
            TableInfo(
                schema_name="public",
                table_name="users",
                columns=[
                    ColumnInfo(
                        name="id",
                        data_type="integer",
                        is_nullable=False,
                        is_primary_key=True,
                    ),
                ],
            )
        ],
        version="15.0",
    )


def _make_orchestrator(
    executors: dict[str, AsyncMock],
    *,
    security: SecurityConfig | None = None,
    metrics: MetricsCollector | None = None,
    generator_sql: str = "SELECT 1",
) -> QueryOrchestrator:
    generator = AsyncMock()
    generator.generate.return_value = generator_sql
    sec = security or SecurityConfig()
    # Mirror server.lifespan: the validator only honours table/column/EXPLAIN
    # restrictions when they are passed explicitly (it does not read them from
    # ``config``), so we must forward them here.
    validator = SQLValidator(
        config=sec,
        blocked_tables=sec.blocked_tables,
        blocked_columns=sec.blocked_columns,
        allow_explain=sec.allow_explain,
    )
    return QueryOrchestrator(
        sql_generator=generator,
        sql_validator=validator,
        sql_executors=executors,
        result_validator=MagicMock(),
        schema_cache=MagicMock(get=MagicMock(return_value=_schema())),
        pools={name: MagicMock() for name in executors},
        resilience_config=ResilienceConfig(
            max_retries=2, retry_delay=0.1, backoff_factor=2.0
        ),
        validation_config=ValidationConfig(),
        metrics=metrics,
    )


class TestMultiDatabaseRouting:
    """Routing to the correct database executor and rejecting unknown DBs."""

    @pytest.mark.asyncio
    async def test_routes_to_requested_database(self) -> None:
        exec_a = AsyncMock(execute=AsyncMock(return_value=([{"id": 1}], 1)))
        exec_b = AsyncMock(execute=AsyncMock(return_value=([{"id": 2}], 1)))
        orch = _make_orchestrator({"db_a": exec_a, "db_b": exec_b})

        response = await orch.execute_query(
            QueryRequest(question="q", database="db_a", return_type=ReturnType.RESULT)
        )

        assert response.success is True
        exec_a.execute.assert_awaited_once()
        exec_b.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_database_rejected(self) -> None:
        exec_a = AsyncMock()
        orch = _make_orchestrator({"db_a": exec_a})

        response = await orch.execute_query(
            QueryRequest(question="q", database="ghost", return_type=ReturnType.SQL)
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "database_error"
        assert "ghost" in response.error.message.lower()
        exec_a.execute.assert_not_awaited()


class TestSecurityEnforcement:
    """Configured access restrictions are enforced through the orchestrator."""

    @pytest.mark.asyncio
    async def test_blocked_table_rejected(self) -> None:
        exec_a = AsyncMock()
        orch = _make_orchestrator(
            {"db_a": exec_a},
            security=SecurityConfig(blocked_tables=["secrets"]),
            generator_sql="SELECT * FROM secrets",
        )

        response = await orch.execute_query(
            QueryRequest(question="q", database="db_a", return_type=ReturnType.SQL)
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "security_violation"
        exec_a.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blocked_column_rejected(self) -> None:
        exec_a = AsyncMock()
        orch = _make_orchestrator(
            {"db_a": exec_a},
            security=SecurityConfig(blocked_columns=["password"]),
            generator_sql="SELECT id, password FROM users",
        )

        response = await orch.execute_query(
            QueryRequest(question="q", database="db_a", return_type=ReturnType.SQL)
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "security_violation"

    @pytest.mark.asyncio
    async def test_explain_rejected_when_disabled(self) -> None:
        exec_a = AsyncMock()
        orch = _make_orchestrator(
            {"db_a": exec_a},
            security=SecurityConfig(allow_explain=False),
            generator_sql="EXPLAIN SELECT * FROM users",
        )

        response = await orch.execute_query(
            QueryRequest(question="q", database="db_a", return_type=ReturnType.SQL)
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "security_violation"

    @pytest.mark.asyncio
    async def test_explain_allowed_when_enabled(self) -> None:
        exec_a = AsyncMock()
        orch = _make_orchestrator(
            {"db_a": exec_a},
            security=SecurityConfig(allow_explain=True),
            generator_sql="EXPLAIN SELECT * FROM users",
        )

        response = await orch.execute_query(
            QueryRequest(question="q", database="db_a", return_type=ReturnType.SQL)
        )

        assert response.success is True


class TestResilienceAndMetrics:
    """Transient failures are retried and the outcome is recorded."""

    @pytest.mark.asyncio
    async def test_transient_failure_retried_then_success_recorded(self) -> None:
        exec_a = AsyncMock()
        exec_a.execute.side_effect = [DatabaseError("blip"), ([{"id": 1}], 1)]
        metrics = MetricsCollector()
        before = metrics.query_requests.labels(status="success", database="db_a")._value.get()

        orch = _make_orchestrator({"db_a": exec_a}, metrics=metrics)

        response = await orch.execute_query(
            QueryRequest(question="q", database="db_a", return_type=ReturnType.RESULT)
        )

        assert response.success is True
        assert exec_a.execute.await_count == 2
        after = metrics.query_requests.labels(status="success", database="db_a")._value.get()
        assert after - before == 1
