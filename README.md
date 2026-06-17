# TokenHub Agent

TokenHub Agent 是一个面向 AI 网关运维排障场景的只读诊断型 Agent。它将 LangGraph 排查工作流、本地 MCP 工具、Skill 排障手册、RAG 记忆和源码只读探索结合起来，帮助 SRE / 平台工程团队把自然语言问题转化为有证据支撑的诊断报告。

本项目适用于所有请求通过 trace_id 串联所有日志包括请求/响应记录、用量元等信息的系统，项目基于 Aliyun SLS 记录日志，Mysql 记录用户信息开发，支持源码分析。

Agent 不会修改生产环境，也不会执行重启、改配置、修复资源等操作；所有能力都限定在只读诊断和修复建议生成。



## 演示视频

https://github.com/user-attachments/assets/f60e3e8c-7a42-45a8-b6ea-bd2a0041808b

[完整视频](https://github.com/C2Z666/tokenhub-agent/releases/download/v0.1.0/demo.mp4)



## 项目能做什么

TokenHub Agent 可以辅助回答这类问题：

- 某个请求为什么失败？
- 某个 trace 是超时、协议不兼容、路由错误，还是上游供应商异常？
- 某个时间窗口内是否有网关异常？
- 相关问题可能对应到哪个网关模块或代码路径？

典型排查流程如下：

```text
用户问题
  -> 意图识别与实体抽取
  -> Skill 路由
  -> 工具调用规划
  -> 只读证据收集
  -> 证据校验 / 重新规划
  -> 结构化排查报告
```

## 核心特性

- **证据驱动排查**：先收集日志、请求元信息、用量数据和源码上下文，再输出结论，避免无证据臆断。
- **只读 MCP 工具**：通过本地 MCP Server 暴露受控的数据库查询和日志查询能力。
- **LangGraph 工作流**：使用 intake、skill_router、planner、executor、verifier、reporter 等阶段化节点组织排查过程。
- **Skill 排障手册**：将常见事故类型沉淀为可复用的排查方法，例如超时断流、协议不兼容、端点路由错误、上游异常等。
- **多轮对话**：支持跨轮上下文累积、追问、切换 trace、恢复历史 session。
- **源码只读探索**：可在配置的网关源码目录内进行受控代码检索和阅读，辅助定位代码层原因。
- **敏感信息脱敏**：对 API Key、Authorization、Token、Secret 等内容进行掩码处理。
- **评测集支持**：建立 golden 测试集，用于回归测试行为、安全约束和多轮排查能力。

## 架构概览

```text
CLI / Chat
  -> Session Manager
  -> LangGraph 排查工作流
      -> intake
      -> skill_router
      -> planner
      -> executor
      -> verifier
      -> reporter
  -> 能力层
      -> Skill Library
      -> MCP Tools
      -> RAG Store
      -> Code Explorer
  -> 存储层
      -> SQLite 会话与报告持久化
      -> SQLite RAG 存储
```

本地 MCP Server 主要提供两类工具：

- `db_*`：面向明确业务问题的窄口径只读数据库查询。
- `sls_*`：面向网关日志和 trace 链路的只读排查工具。

Agent 本身负责任务理解、排查规划、证据校验和最终报告生成。

## 仓库结构

```text
agent/                  LangGraph Agent、CLI、Prompt、Memory、RAG、持久化
mcp/                    本地 MCP Server 与只读诊断工具
skills/                 事故类型 Skill / Runbook 文档
data/source-code/       网关源码索引和可选的只读源码快照
tests/eval/             Golden 评测样本
doc/                    演示视频和项目文档
pyproject.toml          Python 包元数据和依赖配置
.env.example            环境变量模板
```

## 安装

### 环境要求

- Python 3.10+
- 可用的 LLM Provider Apikey
- 可选：只读日志 / 数据库凭据，用于真实排查

### 从源码安装

```bash
git clone https://github.com/<your-org>/tokenhub-agent.git
cd tokenhub-agent
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Windows PowerShell：

```powershell
git clone https://github.com/<your-org>/tokenhub-agent.git
cd tokenhub-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## 配置

复制环境变量模板并按需填写：

```bash
cp .env.example .env
```

主要配置项：

| 变量 | 说明 |
|---|---|
| `SLS_ENDPOINT`, `SLS_PROJECT`, `SLS_LOGSTORE`, `SLS_TOPIC` | 日志服务配置 |
| `ALIBABA_CLOUD_ACCESS_KEY_ID`, `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | 只读日志访问凭据 |
| `PROD_DB_HOST`, `PROD_DB_PORT`, `PROD_DB_NAME`, `PROD_DB_USERNAME`, `PROD_DB_PASSWORD` | 可选的只读数据库配置 |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | LLM Provider 凭据 |
| `RELAY_API_KEY`, `RELAY_BASE_URL` | 可选的 OpenAI 兼容中转地址 |
| `AGENT_DEFAULT_MODEL` | Agent 默认模型 |
| `AGENT_DB_PATH` | 会话和报告持久化 SQLite 路径 |
| `RAG_DB_PATH`, `RAG_STORE_BACKEND` | RAG 存储配置 |

请不要提交填充了真实值的 `.env` 文件。

## 使用方式

将 Skill 文档索引到 RAG 存储：

```bash
tokenhub-agent index
```

执行一次单轮排查：

```bash
tokenhub-agent ask "2026-01-01 10:00 trace_id xxxxx 这个请求为什么失败？"
```

进入推荐的多轮对话模式：

```bash
tokenhub-agent chat
```

恢复历史会话：

```bash
tokenhub-agent resume <session_id>
```

CLI 会展示工具调用、证据收集进度、RAG 命中、重新规划事件，并最终输出 Markdown 格式的排查报告。

## Skill 排障手册

`skills/` 目录保存了常见网关事故类型的排障手册。每个 Skill 都包含元数据、触发信号和排查建议，可供路由器和规划器使用。

当前示例包括：

- `S02_timeout`：超时、取消、流式中断等问题。
- `S04_protocol`：协议或请求参数兼容性问题。
- `S05_endpoint`：端点不存在、路由错误、客户端路径错误等问题。
- `S07_upstream`：上游供应商错误、限流、服务不稳定等问题。

## 安全边界

TokenHub Agent 的设计目标是安全、可控地辅助排障：

- 只做诊断，不重启服务、不修改配置、不自动修复生产资源。
- 数据库能力通过窄口径只读工具暴露，不提供任意 SQL 查询入口。
- 日志查询受时间窗口、返回条数和字段处理约束。
- API Key、Authorization、Token、Secret 等敏感内容会在返回前脱敏。
- 源码探索限定在配置的只读目录内。
- 最终报告要求基于证据输出，避免没有日志或代码依据的结论。

## 评测

Golden 评测样本位于 `tests/eval/`。这些样本覆盖诊断行为、安全约束和多轮交互场景，可用于回归测试 Prompt、Skill 和工具规划逻辑的改动。**仅包含部分脱敏示例**。
