---
name: S04-协议格式不兼容
description: 400/406 类协议/格式不兼容问题（问题在发送方或网关侧），如字段不支持、Accept 不匹配、SSE/JSON 混用
keywords:
  - 400
  - BAD_REQUEST
  - 406   
  - NOT_ACCEPTABLE
  - Could not find acceptable representation
  - tool_use
  - tool_result
  - redacted_thinking
  - Invalid 'user_id'
  - anthropic-sse
  - finish reason
  - output_config.format
  - 协议
  - 格式
  - Accept
strong_signals:
  - "BAD_REQUEST"
  - "NOT_ACCEPTABLE"
priority_logstores: [gateway, request_response]
tools_hint:
  required:
    - sls_get_trace
    - sls_get_request_response
  optional:
    - sls_search_errors
---

## 适用场景

**S04 专门处理 400 类格式/协议问题，问题在发送方（客户端或网关字段透传）**。

- OpenAI 格式、Anthropic 格式、Responses 接口、SSE/JSON 返回混用导致错误
- 某些客户端或模型组合报 400/406
- 切换模型后携带了另一个模型/供应商不支持的字段

> 区分：S07 处理上游侧错误（401/402/403/429/500/503），本 Skill 不处理。

## 典型用户问题

- "为什么 Claude 模型 400？"
- "为什么 Qwen 插件返回 406？"
- "为什么 DeepSeek 不支持某个字段？"
- "为什么 Claude Code 切换模型失败？"

## 排查步骤

```python
# 1. 有 trace_id：直接串联三个 logstore
sls_get_trace(trace_id="<trace_id>", start_time="...", end_time="...")
# 重点：gateway events 中 GlobalExceptionHandler 报错 + request_response 中请求路径/字段/Accept header

# 2. 无 trace_id：先按错误类型批量定位
sls_search_errors(start_time="...", end_time="...", limit=1000)
# 过滤关键词：400 / 406 / tool_use / redacted_thinking / finish reason

# 3. 需要看完整请求体（确认具体字段）
sls_get_request_response(trace_id="<trace_id>", start_time="...", end_time="...")
```

排查要点：
1. 确认请求路径：`/openai/v1/chat/completions` / `/openai/v1/responses` / `/anthropic/v1/messages`
2. 确认客户端实际发送格式（不是只看用户以为的协议）
3. 查请求体是否包含目标供应商不支持字段（`redacted_thinking`、`user_id`、`output_config.format`）
4. 查 response 期望类型：客户端是否需要 SSE / JSON / Anthropic-SSE
5. 对比成功样例和失败样例，重点比对 `role=tool`、tool_call、headers、Accept、stream 参数

## 判断逻辑

- `406 NOT_ACCEPTABLE`：Controller 返回类型与客户端 Accept 不匹配
- `400 BAD_REQUEST` 且 body 明确字段不支持：供应商协议兼容问题
- 某客户端切换模型失败：优先查客户端是否把 OpenAI 格式发到 Anthropic 路径
- `anthropic-sse` 修改后 usage 解析失败：协议兼容修复影响了统计模块

## 模型协议支持矩阵

| 模型 | OpenAI Chat | OpenAI Responses | Anthropic Messages |
|---|---|---|---|
| gemini | ✓ | ✗ | ✗ |
| qwen / gpt | ✓ | ✓ | ✓ |
| kimi / deepseek | ✗ | ✗ | ✓ |
| claude / glm | ✓ | ✗ | ✓ |

## 修复建议

- 不在通用逻辑里硬编码模型名，按协议、供应商能力和配置处理
- 对特定供应商不支持字段做过滤
- 对需要 JSON 返回的客户端补兼容路径或明确提示
- OpenAI 格式转 Claude 模型时，确认是否应走 OpenAI 兼容路径而非 Anthropic 路径
- 修复返回格式后，同步验证 usage 解析、success/error 日志和监控

## 输出证据

- 请求路径、请求协议、模型、供应商、Accept/stream、错误 body 中的字段名
- 判断为：客户端协议错误 / 供应商字段不兼容 / 网关返回格式不兼容 / 统计解析被影响

## 安全注意

- 请求体可能含用户内容，报告中只摘取字段名和结构，不展开长文本

