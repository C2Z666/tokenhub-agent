# 网关源码索引

可用代码工具: code\_project\_overview, code\_list\_files, code\_grep, code\_read\_file

## 路径规则

- 源码根目录统一配置为 gateway-api 项目根目录，例如：`xxx\apps\gateway-api`。
- 本文件是源码快速索引，供 `code_project_overview` 返回给模型，用于判断是否需要查代码、应该查哪个源码文件。
- 下方“核心模块”表格中的文件路径都相对于 gateway-api 项目根目录，可直接传给 `code_read_file` / `code_grep` 的 `file_path` / `file_glob`。
- Java 主代码位于 `src/main/java/...`，配置文件位于 `src/main/resources/...`。
- 不要传绝对路径，也不要传 agent 项目内的 `data/source-code/...` 路径；代码工具会把参数按 gateway-api 项目相对路径解析。

## 详细背景文档

- 项目相对位置：`debug/docs/gateway-api-introduction-en.md`
- 工具读取路径：`debug/docs/gateway-api-introduction-en.md`
- 使用时机：只有当本索引不足以判断模块职责、请求链路或架构背景时，再显式读取该详细文档；不要在每次排查中默认读取。

## 核心模块

| 文件                                                                 | 类名                      | 职责                                  |
| ------------------------------------------------------------------ | ----------------------- | ----------------------------------- |
| src/main/java/com/aigateway/filter/ApiKeyAuthFilter.java           | ApiKeyAuthFilter        | 认证过滤器、trace\_id 生成、会话上下文            |
| src/main/java/com/aigateway/service/AuthService.java               | AuthService             | API Key 校验、配额检查、限流判断                |
| src/main/java/com/aigateway/service/GatewayService.java            | GatewayService          | 供应商路由、模型映射、请求改写、转发编排                |
... 不便完整给出
| src/main/java/com/aigateway/exception/GatewayAuthException.java    | GatewayAuthException    | 认证异常（401/403）                       |
| src/main/resources/application.yml                                 | (config)                | 主配置（超时、日志、数据库等）                     |
| src/main/resources/application-prod.yml                            | (config)                | 生产环境配置覆盖                            |

## 请求处理链路

```
HTTP 请求
  → ApiKeyAuthFilter（认证 + trace_id 生成）
  → OpenAIController / AnthropicController（协议适配）
  → GatewayService（供应商选择 + 模型改写 + 转发编排）
  → WebClientService（上游 HTTP 调用）
  → 响应流处理 → UsageExtractUtil（用量提取）
  → SlsProducerService（日志上报）
```

