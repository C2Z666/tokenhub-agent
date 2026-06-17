"""SLS query builders with a narrow whitelist."""

from __future__ import annotations

from security import validate_safe_identifier


SUPPORTED_LOGSTORES = {
    "gateway": {
        "description": "网关程序日志",
        "primary_fields": ["message"],
    },
    "gateway_usage_log": {
        "description": "鉴权、用量、成功失败信息",
        "primary_fields": ["trace_id", "log_type", "chunk", "timestamp"],
    },
    "request_response": {
        "description": "请求和响应内容",
        "primary_fields": ["trace_id", "log_type", "chunk", "timestamp"],
    },
}

OVERVIEW_DEFAULT_LIMIT = 2000
OVERVIEW_MAX_LIMIT = 10000
OVERVIEW_FIELD_SQL = {
    "api_key": "t.api_key",
    "provider": "COALESCE(t.provider, 'unknown')",
    "model": "COALESCE(t.model, 'unknown')",
    "trace_id": "at.trace_id",
    "user_agent": "r.user_agent",
    "client_ip": "r.client_ip",
    "host": "r.host",
    "base_path": "r.base_path",
    "fc_request_id": "r.fc_request_id",
    "error_detail": "COALESCE(t.error_detail, 'unknown')",
}
OVERVIEW_ALLOWED_OPS = {"eq", "ne", "contains", "prefix"}
AGGREGATE_DEFAULT_LIMIT = 1000
AGGREGATE_MAX_LIMIT = 10000
AGGREGATE_ALLOWED_INTERVALS = {"minute", "hour", "day"}
AGGREGATE_GROUP_FIELDS = {
    "api_key": "api_key_masked",
    "provider": "provider",
    "model": "model",
    "status": "status",
    "user_agent": "user_agent",
    "client_ip": "client_ip",
    "host": "host",
    "base_path": "base_path",
    "fc_request_id": "fc_request_id",
}


def validate_logstore(logstore: str) -> str:
    if logstore not in SUPPORTED_LOGSTORES:
        raise ValueError(f"Unsupported logstore: {logstore}")
    return logstore


def escape_sls_string(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace("'", "\\'")


def escape_sql_literal(value: str) -> str:
    return (value or "").replace("'", "''")


def _limit_clause(limit: int, sql_offset: int = 0) -> str:
    if sql_offset > 0:
        return f"LIMIT {sql_offset}, {limit}"
    return f"LIMIT {limit}"


def trace_query(logstore: str, trace_id: str, limit: int, sql_offset: int = 0) -> str:
    validate_logstore(logstore)
    validate_safe_identifier(trace_id, "trace_id")
    escaped = escape_sls_string(trace_id)
    limit_clause = _limit_clause(limit, sql_offset)
    if logstore == "gateway":
        return (
            "* | where message like '%trace_id="
            + escaped
            + "%' or message like '%traceId="
            + escaped
            + "%'"
        )
    return f'* | SELECT * FROM log WHERE "message.trace_id" = \'{escaped}\' {limit_clause}'


def errors_query(limit: int) -> str:
    return f'message:"ERROR c.a.c.GlobalExceptionHandler" | SELECT * FROM log ORDER BY __time__ DESC LIMIT {limit}'


def keyword_query(logstore: str, keyword: str, limit: int) -> str:
    validate_logstore(logstore)
    escaped = escape_sls_string(keyword.strip())
    if not escaped:
        return f"* | SELECT * FROM log LIMIT {limit}"
    if logstore == "gateway":
        return f"* | where message like '%{escaped}%' limit {limit}"
    return f"* | SELECT * FROM log WHERE message like '%{escaped}%' LIMIT {limit}"


def build_overview_filter_conditions(filters: list[dict] | None) -> list[str]:
    conditions: list[str] = []

    for item in filters or []:
        if not isinstance(item, dict):
            raise ValueError("filter item must be an object")

        field = item.get("field")
        op = item.get("op")
        raw_value = item.get("value", "")
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        values = [str(value).strip() for value in values if str(value).strip()]
        if not values:
            raise ValueError("filter value cannot be empty")
        value = values[0]

        if field == "status":
            if op != "eq":
                raise ValueError("status only supports eq")
            if value == "success":
                conditions.append("t.is_success = 1")
            elif value == "failed":
                conditions.append("(t.is_success <> 1 OR t.is_success IS NULL)")
            else:
                raise ValueError("status only supports success or failed")
            continue

        if field == "trace_id":
            if op == "eq":
                escaped_values = [escape_sql_literal(trace_id) for trace_id in values]
                if len(escaped_values) == 1:
                    conditions.append(f"at.trace_id = '{escaped_values[0]}'")
                else:
                    quoted_values = ", ".join(f"'{trace_id}'" for trace_id in escaped_values)
                    conditions.append(f"at.trace_id IN ({quoted_values})")
            elif op == "contains":
                escaped_values = [escape_sql_literal(trace_id) for trace_id in values]
                like_conditions = [
                    f"at.trace_id LIKE '%{trace_id}%'" for trace_id in escaped_values
                ]
                if len(like_conditions) == 1:
                    conditions.append(like_conditions[0])
                else:
                    conditions.append("(" + " OR ".join(like_conditions) + ")")
            else:
                raise ValueError("trace_id only supports eq or contains")
            continue

        if field not in OVERVIEW_FIELD_SQL:
            raise ValueError(f"unsupported filter field: {field}")
        if op not in OVERVIEW_ALLOWED_OPS:
            raise ValueError(f"unsupported filter op: {op}")

        sql_field = OVERVIEW_FIELD_SQL[field]
        escaped_value = escape_sql_literal(value)

        if op == "eq":
            conditions.append(f"{sql_field} = '{escaped_value}'")
        elif op == "ne":
            conditions.append(f"{sql_field} <> '{escaped_value}'")
        elif op == "contains":
            conditions.append(f"{sql_field} LIKE '%{escaped_value}%'")
        elif op == "prefix":
            conditions.append(f"{sql_field} LIKE '{escaped_value}%'")

    return conditions


def gateway_usage_aggregate_query(
    filters: list[dict] | None,
    interval: str,
    group_by: list[str] | None,
    limit: int,
) -> str:
    if interval not in AGGREGATE_ALLOWED_INTERVALS:
        raise ValueError(f"interval only supports: {', '.join(sorted(AGGREGATE_ALLOWED_INTERVALS))}")
    if limit <= 0 or limit > AGGREGATE_MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {AGGREGATE_MAX_LIMIT}")

    normalized_group_by = []
    for field in group_by or []:
        field = str(field).strip()
        if not field:
            continue
        if field not in AGGREGATE_GROUP_FIELDS:
            raise ValueError(f"unsupported group_by field: {field}")
        if field not in normalized_group_by:
            normalized_group_by.append(field)

    where_conditions = build_overview_filter_conditions(filters)
    where_sql = "\n  AND ".join(where_conditions) or "1 = 1"
    time_format = {
        "minute": "%Y-%m-%d %H:%i:00",
        "hour": "%Y-%m-%d %H:00:00",
        "day": "%Y-%m-%d 00:00:00",
    }[interval]
    group_selects = [f"  {AGGREGATE_GROUP_FIELDS[field]}" for field in normalized_group_by]
    group_select_sql = "\n,".join(group_selects)
    select_prefix = f"  time_bucket"
    if group_select_sql:
        select_prefix += ",\n" + group_select_sql
    group_columns = ["time_bucket"] + [AGGREGATE_GROUP_FIELDS[field] for field in normalized_group_by]
    group_by_sql = ", ".join(group_columns)
    order_by_sql = ", ".join(group_columns)

    return f"""
WITH trace_agg AS (
  SELECT
    "message.trace_id" AS trace_id,
    MAX(TRIM(regexp_extract(CAST("message.chunk" AS VARCHAR), 'Provider:\\s*([^,\\s]+)', 1))) AS provider,
    MAX(TRIM(regexp_extract(CAST("message.chunk" AS VARCHAR), 'Model:\\s*([^,"]+)', 1)))     AS model,
    MAX(CASE WHEN "message.log_type" = 'auth'
             THEN TRIM(regexp_extract(CAST("message.chunk" AS VARCHAR), 'API Key validated:\\s*([^,\\s]+)', 1))
        END) AS api_key,
    MAX(CASE WHEN "message.log_type" = 'success' THEN 1 ELSE 0 END) AS is_success,
    MIN("message.timestamp") AS req_time_ms,
    COALESCE(SUM(CASE WHEN "message.log_type" = 'info'
                      THEN TRY_CAST(regexp_extract(CAST("message.chunk" AS VARCHAR), '"input_tokens"\\s*:\\s*(\\d+)', 1) AS BIGINT)
                 END), 0) AS input_tokens,
    COALESCE(SUM(CASE WHEN "message.log_type" = 'info'
                      THEN TRY_CAST(regexp_extract(CAST("message.chunk" AS VARCHAR), '"output_tokens"\\s*:\\s*(\\d+)', 1) AS BIGINT)
                 END), 0) AS output_tokens,
    COALESCE(SUM(CASE WHEN "message.log_type" = 'info'
                      THEN TRY_CAST(regexp_extract(CAST("message.chunk" AS VARCHAR), '"cache_read_input_tokens"\\s*:\\s*(\\d+)', 1) AS BIGINT)
                 END), 0) AS cache_read_tokens,
    COALESCE(SUM(CASE WHEN "message.log_type" = 'info'
                      THEN TRY_CAST(regexp_extract(CAST("message.chunk" AS VARCHAR), '"cache_creation_input_tokens"\\s*:\\s*(\\d+)', 1) AS BIGINT)
                 END), 0) AS cache_output_tokens,
    MAX(CASE WHEN "message.log_type" = 'success'
             THEN TRY_CAST(regexp_extract(CAST("message.chunk" AS VARCHAR), 'latency=(\\d+)ms', 1) AS INTEGER)
        END) AS latency_ms,
    MAX(CASE WHEN "message.log_type" = 'error'
             AND (CAST("message.chunk" AS VARCHAR) LIKE 'WebClient:%'
               OR CAST("message.chunk" AS VARCHAR) LIKE 'Gateway: Request error:%')
             THEN CAST("message.chunk" AS VARCHAR)
        END) AS error_detail
  FROM gateway_usage_log
  WHERE "message.log_type" IN ('auth', 'success', 'info', 'error')
  GROUP BY "message.trace_id"
),

request_headers AS (
  SELECT
    "message.trace_id" AS trace_id,
    MAX(TRIM(regexp_extract(CAST("message.chunk" AS VARCHAR), '"User-Agent"\\s*:\\s*\\["([^"]+)"', 1))) AS user_agent,
    MAX(TRIM(regexp_extract(CAST("message.chunk" AS VARCHAR), '"X-Forwarded-For"\\s*:\\s*\\["([^"]+)"', 1))) AS client_ip,
    MAX(TRIM(regexp_extract(CAST("message.chunk" AS VARCHAR), '"Host"\\s*:\\s*\\["([^"]+)"', 1))) AS host,
    MAX(TRIM(regexp_extract(CAST("message.chunk" AS VARCHAR), '"X-Fc-Base-Path"\\s*:\\s*\\["([^"]+)"', 1))) AS base_path,
    MAX(TRIM(regexp_extract(CAST("message.chunk" AS VARCHAR), '"X-Fc-Request-Id"\\s*:\\s*\\["([^"]+)"', 1))) AS fc_request_id,
    MIN("message.timestamp") AS req_time_ms
  FROM request_response
  WHERE "message.log_type" = 'request'
    AND "message.trace_id" IS NOT NULL
  GROUP BY "message.trace_id"
),

all_traces AS (
  SELECT trace_id FROM trace_agg
  UNION
  SELECT trace_id FROM request_headers
),

base AS (
  SELECT
    at.trace_id,
    DATE_FORMAT(FROM_UNIXTIME(COALESCE(t.req_time_ms, r.req_time_ms) / 1000.0), '{time_format}') AS time_bucket,
    CASE WHEN t.api_key IS NOT NULL THEN CONCAT(SUBSTR(t.api_key, 1, 9), '***') ELSE NULL END AS api_key_masked,
    COALESCE(t.provider, 'unknown') AS provider,
    COALESCE(t.model, 'unknown') AS model,
    r.user_agent AS user_agent,
    r.client_ip,
    r.host,
    r.base_path,
    r.fc_request_id,
    COALESCE(t.input_tokens, 0) AS input_tokens,
    COALESCE(t.output_tokens, 0) AS output_tokens,
    COALESCE(t.cache_read_tokens, 0) AS cache_read_tokens,
    COALESCE(t.cache_output_tokens, 0) AS cache_output_tokens,
    t.latency_ms,
    CASE
      WHEN t.is_success = 1 THEN '成功'
      WHEN t.trace_id IS NULL THEN '失败(无网关日志)'
      ELSE '失败/进行中'
    END AS status,
    COALESCE(t.error_detail, 'unknown') AS error_detail
  FROM all_traces at
  LEFT JOIN trace_agg t ON at.trace_id = t.trace_id
  LEFT JOIN request_headers r ON at.trace_id = r.trace_id
  WHERE {where_sql}
)

SELECT
{select_prefix},
  COUNT(trace_id) AS request_count,
  SUM(CASE WHEN status = '成功' THEN 1 ELSE 0 END) AS success_count,
  SUM(CASE WHEN status <> '成功' THEN 1 ELSE 0 END) AS failed_count,
  SUM(input_tokens) AS input_tokens,
  SUM(output_tokens) AS output_tokens,
  SUM(cache_read_tokens) AS cache_read_tokens,
  SUM(cache_output_tokens) AS cache_output_tokens,
  AVG(latency_ms) AS avg_latency_ms,
  MAX(latency_ms) AS max_latency_ms
FROM base
GROUP BY {group_by_sql}
ORDER BY {order_by_sql}
LIMIT {limit}
""".strip()


def gateway_usage_overview_query(filters: list[dict] | None, limit: int) -> str:
    if limit <= 0 or limit > OVERVIEW_MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {OVERVIEW_MAX_LIMIT}")

    where_conditions = build_overview_filter_conditions(filters)
    where_sql = "\n  AND ".join(where_conditions) or "1 = 1"

    return f"""
WITH trace_agg AS (
  SELECT
    "message.trace_id" AS trace_id,
    MAX(TRIM(regexp_extract(CAST("message.chunk" AS VARCHAR), 'Provider:\s*([^,\s]+)', 1))) AS provider,
    MAX(TRIM(regexp_extract(CAST("message.chunk" AS VARCHAR), 'Model:\s*([^,"]+)', 1)))     AS model,
    MAX(CASE WHEN "message.log_type" = 'auth'
             THEN TRIM(regexp_extract(CAST("message.chunk" AS VARCHAR), 'API Key validated:\s*([^,\s]+)', 1))
        END) AS api_key,
    MAX(CASE WHEN "message.log_type" = 'success' THEN 1 ELSE 0 END) AS is_success,
    MIN("message.timestamp") AS req_time_ms,
    COALESCE(SUM(CASE WHEN "message.log_type" = 'info'
                      THEN TRY_CAST(regexp_extract(CAST("message.chunk" AS VARCHAR), '"input_tokens"\s*:\s*(\d+)', 1) AS BIGINT)
                 END), 0) AS input_tokens,
    COALESCE(SUM(CASE WHEN "message.log_type" = 'info'
                      THEN TRY_CAST(regexp_extract(CAST("message.chunk" AS VARCHAR), '"output_tokens"\s*:\s*(\d+)', 1) AS BIGINT)
                 END), 0) AS output_tokens,
    COALESCE(SUM(CASE WHEN "message.log_type" = 'info'
                      THEN TRY_CAST(regexp_extract(CAST("message.chunk" AS VARCHAR), '"cache_read_input_tokens"\s*:\s*(\d+)', 1) AS BIGINT)
                 END), 0) AS cache_read_tokens,
    COALESCE(SUM(CASE WHEN "message.log_type" = 'info'
                      THEN TRY_CAST(regexp_extract(CAST("message.chunk" AS VARCHAR), '"cache_creation_input_tokens"\s*:\s*(\d+)', 1) AS BIGINT)
                 END), 0) AS cache_output_tokens,
    MAX(CASE WHEN "message.log_type" = 'success'
             THEN TRY_CAST(regexp_extract(CAST("message.chunk" AS VARCHAR), 'latency=(\d+)ms', 1) AS INTEGER)
        END) AS latency_ms,
    MAX(CASE WHEN "message.log_type" = 'error'
             AND (CAST("message.chunk" AS VARCHAR) LIKE 'WebClient:%'
               OR CAST("message.chunk" AS VARCHAR) LIKE 'Gateway: Request error:%')
             THEN CAST("message.chunk" AS VARCHAR)
        END) AS error_detail
  FROM gateway_usage_log
  WHERE "message.log_type" IN ('auth', 'success', 'info', 'error')
  GROUP BY "message.trace_id"
),

request_headers AS (
  SELECT
    "message.trace_id" AS trace_id,
    -- User-Agent
    MAX(TRIM(regexp_extract(
      CAST("message.chunk" AS VARCHAR),
      '"User-Agent"\s*:\s*\["([^"]+)"', 1
    ))) AS user_agent,
    MAX(TRIM(regexp_extract(
      CAST("message.chunk" AS VARCHAR),
      '"X-Forwarded-For"\s*:\s*\["([^"]+)"', 1
    ))) AS client_ip,
    MAX(TRIM(regexp_extract(
      CAST("message.chunk" AS VARCHAR),
      '"Host"\s*:\s*\["([^"]+)"', 1
    ))) AS host,
    MAX(TRIM(regexp_extract(
      CAST("message.chunk" AS VARCHAR),
      '"X-Fc-Base-Path"\s*:\s*\["([^"]+)"', 1
    ))) AS base_path,
    MAX(TRIM(regexp_extract(
      CAST("message.chunk" AS VARCHAR),
      '"X-Fc-Request-Id"\s*:\s*\["([^"]+)"', 1
    ))) AS fc_request_id,
    MIN("message.timestamp") AS req_time_ms
  FROM request_response
  WHERE "message.log_type" = 'request'
    AND "message.trace_id" IS NOT NULL
  GROUP BY "message.trace_id"
),

all_traces AS (
  SELECT trace_id FROM trace_agg
  UNION                       -- UNION 自动去重
  SELECT trace_id FROM request_headers
)

SELECT
  at.trace_id,
  CASE
    WHEN t.api_key IS NOT NULL THEN CONCAT(SUBSTR(t.api_key, 1, 9), '***')
    ELSE NULL
  END AS api_key_masked,
  COALESCE(t.provider, 'unknown')  AS provider,
  COALESCE(t.model,    'unknown')  AS model,
  FROM_UNIXTIME(COALESCE(t.req_time_ms, r.req_time_ms) / 1000.0) AS request_time,
  r.user_agent     AS request_user_agent,
  r.client_ip,
  r.host,
  r.base_path,
  r.fc_request_id,
  COALESCE(t.input_tokens,      0) AS input_tokens,
  COALESCE(t.output_tokens,     0) AS output_tokens,
  COALESCE(t.cache_read_tokens, 0) AS cache_read_tokens,
  COALESCE(t.cache_output_tokens,0) AS cache_output_tokens,
  t.latency_ms,
  CASE
    WHEN t.is_success = 1    THEN '成功'
    WHEN t.trace_id IS NULL  THEN '失败(无网关日志)'
    ELSE '失败/进行中'
  END AS status,
  COALESCE(t.error_detail, 'unknown') AS error_detail

FROM all_traces at
LEFT JOIN trace_agg       t ON at.trace_id = t.trace_id
LEFT JOIN request_headers r ON at.trace_id = r.trace_id
WHERE {where_sql}
ORDER BY COALESCE(t.req_time_ms, r.req_time_ms) DESC
LIMIT {limit}
""".strip()
