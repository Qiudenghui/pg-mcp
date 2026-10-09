# FEATURE_W5 — PostgreSQL MCP Server：codex review 三类缺失功能落地

基于训练营第 5 章第 4 节 `codex review`，本分支补全了评审中指出的三类缺失功能，
使系统行为回归实施方案。所有改动均复用既有模式，不引入新依赖、不重构无关逻辑，
并保持单库场景向后兼容（默认库即主库）。

## 一、功能对照（codex review 三点 → 落地情况）

### 1. 多数据库与安全控制（落地而非仅设计）

| 能力 | 实施前 | 实施后 |
| --- | --- | --- |
| 多库路由 | `orchestrator` 持单个 `SQLExecutor`，无法按库选择 | `Settings.databases: dict[str, DatabaseConfig]`（主键自动并入）；`server.lifespan` 为每张库建连接池 + `SQLExecutor`；`orchestrator` 持 `sql_executors` 字典，按 `request.database`（缺省取主库）选执行器；库名不在 `databases` 中 → 抛 `DatabaseError` 拒绝 |
| 表/列/EXPLAIN 限制 | `SQLValidator` 方法齐全，但 `server` 写死 `None/False`，校验从不触发 | `SecurityConfig` 新增 `blocked_tables` / `blocked_columns` / `allow_explain`；`server.lifespan` 显式传入（**注意**：`SQLValidator` 只认显式参数、不读 `config`，因此必须显式传）；`validate_or_raise` 真实拦截 |
| 只读角色/搜索路径/超时 | 设计承诺但未落地 | `SQLExecutor._set_session_params` 执行前按 `readonly_role` 发 `SET ROLE`、按 `safe_search_path` 发 `SET search_path`，并以 `max_execution_time` 设 `statement_timeout`（asyncpg 毫秒） |

### 2. 弹性与可观测性（接入真实请求流程）

- **速率限制**：`MultiRateLimiter` 在 `orchestrator.execute_query` 入口调用 `for_queries(timeout=30)`；未配置时为 no-op CM（`_null_async_cm`）。
- **重试/退避**：`orchestrator._execute_sql_with_retry` 用 `ResilienceConfig.max_retries / retry_delay / backoff_factor` 实现指数退避，仅对可重试异常（`DatabaseError`、`DatabaseConnectionError`、`OSError`、`ConnectionError`、`TimeoutError`）重试；**故意的 `ExecutionTimeoutError` 不重试**。
- **指标/追踪**：`MetricsCollector.increment_query_request(status, db)` 在成功/失败分支记录；`tracing.request_context(request_id)` 串起请求级 `request_id`。

### 3. 响应/模型缺陷修复与测试补足

- `QueryResponse` 重复的 `to_dict` 已删除，仅保留单一实现：统一 `model_dump(exclude_none=False)`，并将缺失的 `tokens_used` 补 `0`（原第二份定义覆盖第一份，使该逻辑成为死代码）。
- 未使用配置字段落实：`readonly_role / safe_search_path / max_execution_time` 已并入执行器；`max_retries / retry_delay / backoff_factor` 已并入重试；`min_confidence_score` 与 `confidence_threshold` 重复，保留后者（代码中统一使用 `confidence_threshold`）。
- 补充单元测试（全部用 `unittest.mock` 注入 `Pool` / `OpenAI` / `SQLValidator` 等，无需真实 PostgreSQL 或 API Key）：
  - `tests/unit/test_settings.py` — 多库配置、安全字段、未用字段处理
  - `tests/unit/test_query_model.py` — `to_dict` 单一实现与 `tokens_used` 语义
  - `tests/unit/test_resilience_observability.py` — 限流拒绝、重试成功/耗尽/超时不重试、指标记录、tracing
  - `tests/integration/test_query_flow.py` — 多库路由、错误库拒绝、表/列/EXPLAIN 端到端拦截、瞬时失败重试 + 指标

## 二、改动文件

- `src/pg_mcp/config/settings.py` — `SecurityConfig`（新增 `blocked_tables/columns/allow_explain` + CSV 解析）、`ResilienceConfig`（新增 `query_rate_limit/llm_rate_limit`）、`Settings.databases` + 主键自动并入校验器。
- `src/pg_mcp/models/query.py` — 删除重复 `to_dict`，统一 `tokens_used` 默认。
- `src/pg_mcp/services/orchestrator.py` — 持 `sql_executors` 字典并路由；接入 `rate_limiter / metrics`；`_execute_sql_with_retry` 指数退避；`_record_query_outcome` 指标记录；`request_context` 包裹。
- `src/pg_mcp/server.py` — `lifespan` 为每张库建池+执行器；`SQLValidator` 传真实安全配置；注入 `MultiRateLimiter`/`MetricsCollector`。
- 新增/改写测试（见上）。

## 三、测试

```bash
# 仅本作业相关模块（不触发 server 导入，server 依赖 mcp 1.x API）
pytest tests/unit/test_query_model.py tests/unit/test_resilience_observability.py \
       tests/integration/test_query_flow.py tests/unit/test_settings.py \
       tests/unit/test_orchestrator.py tests/unit/test_sql_validator.py -q
```

> 说明：本沙箱环境安装的是 `mcp` 2.x（已将 `FastMCP` 重命名为 `MCPServer`），而 `server.py`
> 使用 mcp 1.x 的 `from mcp.server.fastmcp import FastMCP` 接口；`tests/e2e/test_mcp.py` 与
> `tests/integration/test_full_flow.py` 因导入 `server` 在该环境无法收集。这是**环境依赖版本**
> 问题，与本次功能改动无关（本次改动均未触碰 `FastMCP` 接口）。在 mcp 1.x 环境下整套测试可正常运行。
> 此外 `tests/unit/test_config.py::test_default_values` 断言 `log_format=="json"` 与代码默认值
> `"text"` 不符，属仓库既有缺陷，未纳入本次范围。

## 四、向后兼容

- 单库配置（`database`）经 `databases` 自动包含默认库，旧 `.env`（仅 `DATABASE_*`）仍可运行。
- 未配置速率限制/指标时，`orchestrator` 自动退化为 no-op，行为与原先一致。
