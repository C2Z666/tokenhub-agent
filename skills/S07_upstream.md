---
name: S07-上游供应商错误
description: 网关已成功转发，上游返回 401/402/403/429/503 或空响应。问题在上游侧（供应商账号、限流、权限、余额等）
keywords:
  - Upstream error response
  - Payment Required
  - Provider error
  - Provider rate limit
  - 401 UNAUTHORIZED
  - 403 FORBIDDEN
  - 429
  - 402
  - 503 SERVICE_UNAVAILABLE
  - empty response
  - 无权访问
  - 无效的令牌
  - 限流
strong_signals:
  - "429"
  - "402"
  - "401"
  - "403"
  - "503"
  - "Payment Required"
  - "Provider rate limit"
priority_logstores: [gateway, gateway_usage_log, request_response]
tools_hint:
  required:
    - sls_get_trace
    - sls_query_gateway_usage_overview
  optional:
    - sls_get_request_response
---

## 适用场景

**S07 专门处理上游侧错误（问题在供应商），覆盖 401/402/403/429/503 等**。

- 网关已成功转发，上游返回错误状态码或空响应
- 问题集中在某个供应商、某个供应商账号、某个模型或某种功能

> 区分：S04 处理 400 类格式问题（发送方），本 Skill 不处理 400。

## 典型用户问题

- "为什么上游报 503？"
- "为什么供应商限流？"
- "为什么显示无权访问供应商？"
- "为什么同一个请求有时成功有时失败？"
- "为什么返回空？"

## 排查步骤

```python
# 1. 有 trace_id：直接串联
sls_get_trace(trace_id="<trace_id>", start_time="...", end_time="...")
# 重点：gateway events 中 "Upstream error response" / status / body 摘要
# gateway_usage_log 中 provider / ActualModel

# 2. 无 trace_id：按供应商/错误类型批量查
sls_query_gateway_usage_overview(
    start_time="...", end_time="...",
    filters=[
        {"field": "provider", "op": "eq", "value": "<供应商名>"},
        {"field": "status", "op": "eq", "value": "failed"}
    ],
    limit=200
)
# 观察 error_detail 字段是否集中于 401/403/429/402/503

# 3. 需要确认上游返回 body 细节
sls_get_request_response(trace_id="<trace_id>", start_time="...", end_time="...")
```

排查要点：
1. 确认已经通过网关鉴权并进入上游转发
2. 提取上游 URL、status、body 中的错误 message、provider、ActualModel
3. 判断错误是否集中在某个供应商/账号/模型
4. 401/403：查供应商 API key 是否无效、账号组是否无权限
5. 429：查供应商限流、用户并发、同 key 请求量
6. 503/空返回：查是否可重试、是否有其他供应商可故障转移
7. 同请求不同结果：考虑供应商负载到不同后端或渠道稳定性差

## 判断逻辑

- `无效的令牌`：通常是上游供应商 key 或错误 header 透传
- `无权访问 ... 分组`：供应商账号权限不足
- `Provider rate limit`：供应商限流
- `Payment Required` (402)：供应商账号余额不足
- `No available channel`：供应商无可用渠道或模型不可用
- 网关 success 但用户无感知：可能是上游返回格式或客户端解析问题，联动 S04

## 已知规律

- **Aiberm** 是 403 权限问题的高发供应商（渠道分组权限细粒度）
- **DeepSeek** 是 400（字段兼容，走 S04）和 402（余额）的高发供应商
- 401/403/402/429 类错误几乎都属于 S07

## 修复建议

- 添加上游错误信息投递并进入监控
- 对可重试错误做指数退避和故障转移
- 按 session 进行供应商负载，避免 KV cache 失效和成本升高
- 对供应商账号做健康状态和限流状态记录
- 对经常出错渠道做降权或临时下线

## 输出证据

- 上游 URL、status、body 摘要、provider、ActualModel、是否同供应商集中

## 安全注意

- 上游错误 body 可能包含 request id 可保留，但不要输出上游完整密钥

