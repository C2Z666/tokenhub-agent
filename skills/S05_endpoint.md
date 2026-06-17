---
name: S05-缺失端点/探测请求
description: 客户端自动访问不存在的接口返回 404，多为健康检查/余额查询/token 计数/event logging 等辅助请求
keywords:
  - 404 NOT_FOUND
  - No static resource
  - anthropic/api/event_logging/batch
  - anthropic/v1/messages/count_tokens
  - anthropic/user/balance
  - anthropic/chat/completions
  - 探测
  - 健康检查
strong_signals:
  - "No static resource"
  - "404 NOT_FOUND"
priority_logstores: [gateway, gateway_usage_log]
tools_hint:
  required:
    - sls_query_gateway_usage_overview
    - sls_get_trace
  optional:
    - sls_search_errors
---

## 适用场景

- 客户端自动访问不存在的接口，网关返回 404
- 多为健康检查、余额查询、token 计数、event logging、自动探测等辅助请求
- 通常不影响主请求

## 典型用户问题

- "为什么出现 404？"
- "这个 trace_id 是不是网关接口坏了？"
- "为什么有些错误没有影响实际使用？"

## 排查步骤

```python
# 1. 有 trace_id：直接查
sls_get_trace(trace_id="<trace_id>", start_time="...", end_time="...")
# 重点：gateway events 中 404 NOT_FOUND + "No static resource" + 请求 path

# 2. 无 trace_id：按关键词搜错误
sls_search_errors(start_time="...", end_time="...", limit=1000)
# 提取 path，判断是核心接口还是探测接口

# 3. 判断是否影响主请求（同 API Key 同时段主请求是否成功）
sls_query_gateway_usage_overview(
    start_time="...", end_time="...",
    filters=[
        {"field": "api_key", "op": "prefix", "value": "th-xxxx"},
        {"field": "status", "op": "eq", "value": "failed"}
    ],
    limit=1000
)
```

排查要点：
1. 从错误 message 提取 path
2. 判断 path 是核心模型调用接口还是客户端探测/辅助接口
3. 查同一时间用户主请求是否成功
4. 静默探测请求且不影响使用 → 可降级处理或从告警排除
5. 影响客户端功能 → 补空端点、简易实现或明确错误提示

## 判断逻辑

- `event_logging/batch` / `count_tokens` / `user/balance` / `anthropic/chat/completions` 等多为客户端辅助请求
- 主请求成功 + 辅助请求 404 → 通常不影响核心转发，低优先级
- 主请求也失败 → 联动 S04 继续排查

## 修复建议

- 对常见探测接口补空实现或简易实现
- 告警规则过滤已知无影响的探测 404，避免噪声
- 对错误路径如 `/anthropic/v1/responses` 给出用户友好提示

## 输出证据

- 缺失 path、客户端来源、是否影响主请求、建议补接口还是忽略

## 安全注意

- request headers 中可能有 apikey 和 session id，报告中只保留必要前缀或客户端类型

