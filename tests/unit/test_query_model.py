"""Tests for QueryResponse model, specifically the to_dict behaviour.

The codex review flagged a duplicate ``to_dict`` definition on ``QueryResponse``:
the second definition silently shadowed the first, turning the ``tokens_used``
normalisation logic into dead code. These tests lock in the *single* correct
implementation.
"""

from pg_mcp.models.errors import ErrorCode
from pg_mcp.models.query import ErrorDetail, QueryResponse


def test_to_dict_has_single_definition() -> None:
    """There must be exactly one ``to_dict`` method on QueryResponse."""
    count = sum(1 for name in vars(QueryResponse) if name == "to_dict")
    assert count == 1


def test_to_dict_defaults_missing_tokens_used_to_zero() -> None:
    """When tokens_used is not provided it is normalised to 0 in the output."""
    response = QueryResponse(success=True, generated_sql="SELECT 1")
    data = response.to_dict()
    assert data["tokens_used"] == 0


def test_to_dict_preserves_explicit_tokens_used() -> None:
    """An explicitly provided token count is preserved verbatim."""
    response = QueryResponse(success=True, generated_sql="SELECT 1", tokens_used=123)
    assert response.to_dict()["tokens_used"] == 123


def test_to_dict_includes_all_top_level_fields() -> None:
    """Every declared response field is present in the serialised dict."""
    response = QueryResponse(success=True, generated_sql="SELECT 1")
    data = response.to_dict()
    for field in (
        "success",
        "generated_sql",
        "validation",
        "data",
        "error",
        "confidence",
        "tokens_used",
    ):
        assert field in data


def test_to_dict_serialises_error_payload() -> None:
    """When the request failed, the error detail is serialised fully."""
    response = QueryResponse(
        success=False,
        generated_sql=None,
        error=ErrorDetail(
            code=ErrorCode.DATABASE_ERROR.value,
            message="boom",
            details={"db": "test_db"},
        ),
    )
    data = response.to_dict()
    assert data["success"] is False
    assert data["tokens_used"] == 0
    assert data["error"]["code"] == ErrorCode.DATABASE_ERROR.value
    assert data["error"]["message"] == "boom"
    assert data["error"]["details"] == {"db": "test_db"}
