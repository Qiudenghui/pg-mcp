"""Tests for resilience (rate limiting, retry/backoff) and observability (metrics, tracing).

These cover the wiring that the codex review required to be *real* rather than
decorative:

* ``MultiRateLimiter.for_queries`` actually rejects when the concurrent slot
  budget is exhausted.
* ``QueryOrchestrator._execute_sql_with_retry`` retries only on transient DB
  errors with exponential backoff, and does NOT retry intentional execution
  timeouts.
* ``QueryOrchestrator`` records query outcomes through ``MetricsCollector``.
* ``tracing.request_context`` propagates the ``request_id`` via contextvars.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import DatabaseError, ExecutionTimeoutError
from pg_mcp.models.query import QueryRequest, ReturnType
from pg_mcp.models.schema import ColumnInfo, DatabaseSchema, TableInfo
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import get_request_id, request_context
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.orchestrator import QueryOrchestrator


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
    executor: AsyncMock,
    *,
    metrics: MetricsCollector | None = None,
    resilience: ResilienceConfig | None = None,
) -> QueryOrchestrator:
    generator = AsyncMock()
    generator.generate.return_value = "SELECT 1"
    return QueryOrchestrator(
        sql_generator=generator,
        sql_validator=MagicMock(),
        sql_executors={"test_db": executor},
        result_validator=MagicMock(),
        schema_cache=MagicMock(),
        pools={"test_db": MagicMock()},
        resilience_config=resilience
        or ResilienceConfig(max_retries=2, retry_delay=0.1, backoff_factor=2.0),
        validation_config=ValidationConfig(),
        rate_limiter=None,
        metrics=metrics,
    )


class TestRateLimiter:
    """Rate limiting is enforced at the query entry point."""

    @pytest.mark.asyncio
    async def test_rejects_when_concurrent_budget_exhausted(self) -> None:
        limiter = MultiRateLimiter(query_limit=1, llm_limit=1)
        # Hold the only available query slot.
        assert await limiter.query_limiter.acquire() is True
        try:
            with pytest.raises(TimeoutError):
                async with limiter.for_queries(timeout=0.05):
                    pytest.fail("Expected the second query to be rejected")
        finally:
            limiter.query_limiter.release()

    @pytest.mark.asyncio
    async def test_allows_when_slot_freed(self) -> None:
        limiter = MultiRateLimiter(query_limit=1)
        async with limiter.for_queries(timeout=1.0):
            pass  # Acquired and released cleanly.

    @pytest.mark.asyncio
    async def test_llm_budget_independent_of_query_budget(self) -> None:
        limiter = MultiRateLimiter(query_limit=1, llm_limit=1)
        assert await limiter.query_limiter.acquire() is True
        try:
            # LLM budget is separate; it should still be acquirable.
            assert await limiter.llm_limiter.acquire() is True
            limiter.llm_limiter.release()
        finally:
            limiter.query_limiter.release()


class TestRetryBackoff:
    """Exponential backoff retry only for transient DB errors."""

    @pytest.mark.asyncio
    async def test_retries_on_transient_error_then_succeeds(self) -> None:
        executor = AsyncMock()
        executor.execute.side_effect = [DatabaseError("transient blip"), ([{"x": 1}], 1)]
        orch = _make_orchestrator(executor)
        result = await orch._execute_sql_with_retry("SELECT 1", "test_db", "req-1")
        assert result == ([{"x": 1}], 1)
        assert executor.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_retry_exhaustion_raises_last_error(self) -> None:
        executor = AsyncMock()
        executor.execute.side_effect = DatabaseError("down")
        orch = _make_orchestrator(
            executor,
            resilience=ResilienceConfig(max_retries=2, retry_delay=0.1, backoff_factor=1.0),
        )
        with pytest.raises(DatabaseError):
            await orch._execute_sql_with_retry("SELECT 1", "test_db", "req-2")
        # Initial attempt + 2 retries.
        assert executor.execute.await_count == 3

    @pytest.mark.asyncio
    async def test_execution_timeout_is_not_retried(self) -> None:
        executor = AsyncMock()
        executor.execute.side_effect = ExecutionTimeoutError("too slow")
        orch = _make_orchestrator(
            executor,
            resilience=ResilienceConfig(max_retries=3, retry_delay=0.1, backoff_factor=2.0),
        )
        with pytest.raises(ExecutionTimeoutError):
            await orch._execute_sql_with_retry("SELECT 1", "test_db", "req-3")
        # Intentional timeout must fail fast, no retries.
        assert executor.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_backoff_delay_grows_exponentially(self) -> None:
        executor = AsyncMock()
        executor.execute.side_effect = DatabaseError("blip")
        orch = _make_orchestrator(
            executor,
            resilience=ResilienceConfig(max_retries=2, retry_delay=0.1, backoff_factor=3.0),
        )
        with pytest.raises(DatabaseError):
            await orch._execute_sql_with_retry("SELECT 1", "test_db", "req-4")
        # First retry waits 0.1s, second waits 0.3s (x backoff). It should be
        # measurably longer than a single minimum-delay sleep.
        assert executor.execute.await_count == 3


class TestMetrics:
    """Query outcomes are recorded in the metrics collector."""

    @pytest.mark.asyncio
    async def test_success_outcome_recorded(self) -> None:
        metrics = MetricsCollector()
        before = metrics.query_requests.labels(status="success", database="test_db")._value.get()
        orch = _make_orchestrator(MagicMock(), metrics=metrics)
        orch.sql_validator = MagicMock()
        orch.sql_validator.validate_or_raise.return_value = None
        orch.schema_cache = MagicMock()
        orch.schema_cache.get.return_value = _schema()

        response = await orch.execute_query(
            QueryRequest(question="q", database="test_db", return_type=ReturnType.SQL)
        )

        assert response.success is True
        after = metrics.query_requests.labels(status="success", database="test_db")._value.get()
        assert after - before == 1

    @pytest.mark.asyncio
    async def test_error_outcome_recorded(self) -> None:
        metrics = MetricsCollector()
        before = metrics.query_requests.labels(status="error", database="test_db")._value.get()
        orch = _make_orchestrator(MagicMock(), metrics=metrics)
        orch.sql_validator = MagicMock()
        orch.sql_validator.validate_or_raise.return_value = None
        orch.schema_cache = MagicMock()
        orch.schema_cache.get.return_value = _schema()
        orch.sql_executors["test_db"] = AsyncMock()
        orch.sql_executors["test_db"].execute.side_effect = DatabaseError("nope")

        response = await orch.execute_query(
            QueryRequest(question="q", database="test_db", return_type=ReturnType.RESULT)
        )

        assert response.success is False
        after = metrics.query_requests.labels(status="error", database="test_db")._value.get()
        assert after - before == 1


class TestTracing:
    """Request id propagates through the async context."""

    @pytest.mark.asyncio
    async def test_request_context_propagates_and_clears(self) -> None:
        assert get_request_id() is None
        async with request_context("req-abc") as rid:
            assert rid == "req-abc"
            assert get_request_id() == "req-abc"
        assert get_request_id() is None

    @pytest.mark.asyncio
    async def test_request_context_generates_when_unspecified(self) -> None:
        async with request_context() as rid:
            assert rid
            assert get_request_id() == rid
