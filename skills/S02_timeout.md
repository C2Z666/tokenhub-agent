---
name: S02-超时断流
description: 长任务超时/断流排查（latency 集中于固定阈值如 60s/180s/300s，或客户端一直转无 finish reason）
keywords:
  - ReadTimeoutException
  - request cancelled
  - PrematureCloseException
  - responseTimeout
  - hasResponse
  - Connection prematurely closed
  - Upstream stream cancelled
  - Model stream ended without a finish reason
  - 超时
  - 断开
  - 断流
strong_signals:
  - "ReadTimeoutException"
  - "PrematureCloseException"
  - "request cancelled"
  - "Connection prematurely closed"
priority_logstores: [gateway, gateway_usage_log, request_response]
tools_hint:
  required:
    - sls_query_gateway_usage_overview
    - sls_get_trace
---

## 适用场景

- 请求执行时间长，输出一半断开
- 客户端一直转、没有结束符、`success` 日志与客户端体验不一致
- 请求在固定时间附近中断（30s、60s、180s、300s）
- 多条请求 latency 集中于同一数值，强烈提示存在固定超时参数

## 典型用户问题

- "为什么模型回复到一半停了？"
- "为什么日志显示 success 但客户端还在思考？"
- "为什么长任务总是在某个时间点断开？"

## 排查步骤

### 阶段一：统计确认（无 trace_id 时）

```python
sls_query_gateway_usage_overview(
    start_time="...", end_time="...",
    filters=[{"field": "status", "op": "eq", "value": "failed"}],
    limit=200
)
```

观察返回的 `latency_ms` 字段：
- 多条记录集中于 **60000 / 180000 / 300000 / 600000 ms** 附近 → 强提示固定超时阈值
- latency 分散无规律 → 非典型 S02，考虑 S07 上游问题

### 阶段二：单 trace 定位（已知 trace_id 或从 overview 取一条）

```python
sls_get_trace(trace_id="<trace_id>", start_time="...", end_time="...")
```

在返回的 `events` 中重点查：
- `gateway`：`request cancelled` / `hasResponse` / `ReadTimeoutException` / `PrematureCloseException`
- `gateway_usage_log`：`latency=XXXXms` / `success` 或 `error` 最终状态
- `request_response`：response 是否有内容、是否缺 `finish_reason`

## 判断矩阵

| 现象 | 推断 |
|---|---|
| latency ≈ 60000ms，`hasResponse=false` | FC 超时（原 1min）或连接层 `maxLifeTime` |
| latency ≈ 180000ms，`ReadTimeoutException` | `responseTimeout` 首字节超时（原 3min） |
| latency ≈ 300000ms，`hasResponse=true` | 连接层 `maxLifeTime`（原 5min）触发，已有部分响应 |
| latency ≈ 600000ms 正常结束 | FC 超时 3600s 内正常长任务 |
| 上游 `PrematureCloseException BEFORE response` | 上游主动断开（非网关参数问题） |

## 当前生产参数基线

| 参数 | 修复前 | 修复后 |
|---|---|---|
| FC 超时 | 1min / 3min | 3600s |
| `responseTimeout` | 30s | 5min |
| 连接 `maxLifeTime` | 5min | 30min |
| 连接 `maxIdleTime` | - | 60s |

## 修复建议

- 长任务接口提高 `responseTimeout`
- FC 超时时间要与任务时长匹配
- 保持 SSE 结束格式规范，确保最终 success/error 日志只在真实完成时写入
- 对长任务增加首字节超时、总超时、客户端断开三类日志字段

## 输出证据

- 中断发生时间、latency、是否有首字节、是否有部分 response
- success/error/info 日志是否完整
- 判断为：FC 超时 / 上游超时 / 客户端取消 / SSE 不完整 / 连接层断开

## 安全注意

- 不要直接展开完整 response，优先输出是否存在结尾、usage、finish reason 等结构化摘要

