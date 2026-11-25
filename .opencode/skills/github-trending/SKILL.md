---
name: github-trending
description: 当需要采集 GitHub 热门开源项目时使用此技能
allowed-tools: Read, Grep, Glob, WebFetch
---

# GitHub Trending 采集

从 GitHub 搜索并筛选近期热门的 AI / LLM / Agent 开源项目，生成结构化的中文候选列表。

## 使用场景

- 每日 / 每周知识库更新，需要补充 GitHub 侧的技术动态。
- 上游 `collector` Agent 需要一份可直接进入分析环节的候选仓库列表。
- 需要按 star 热度快速了解某个方向（Agent、RAG、MCP、推理框架等）的代表项目。

不适用：需要仓库历史趋势曲线、代码质量评估或安全审计的场景。

## 执行步骤

### 第 1 步：搜索热门仓库（GitHub API）

GitHub 没有官方 Trending API，使用 **Search API** 近似：

```
https://api.github.com/search/repositories?q={query}&sort=stars&order=desc&per_page=30
```

- 默认时间窗：最近 7 天；用 `pushed:>={date}` 或 `created:>={date}` 限定。
- `>` 在 URL 中必须编码为 `%3E`，例如 `pushed:%3E%3D2026-11-18`。
- 建议发起 3–5 个查询，覆盖不同方向，避免单一 topic 偏差：

| 方向 | 查询示例 |
| --- | --- |
| LLM 基础 | `topic:llm+pushed:%3E%3D{date}` |
| Agent | `topic:ai-agents+pushed:%3E%3D{date}` |
| RAG | `topic:rag+pushed:%3E%3D{date}` |
| MCP | `topic:mcp+pushed:%3E%3D{date}` |
| 新项目 | `agent+in:name,description+created:%3E%3D{date}` |

- 使用 `WebFetch` 发起请求，不拼接任何 token；未认证 Search API 限制约为 **10 次/分钟**，查询数控制在 5 次以内。

### 第 2 步：提取信息

从每个 `item` 中提取以下字段，并保留 `archived`、`fork`、`created_at`、`pushed_at` 供过滤使用：

| 字段 | API 来源 | 说明 |
| --- | --- | --- |
| `name` | `full_name` | 形如 `owner/repo` |
| `url` | `html_url` | 仓库主页直链 |
| `description` | `description` | 原文描述，用于撰写摘要 |
| `stars` | `stargazers_count` | 整数 |
| `language` | `language` | 可能为 `null`，输出时用 `"Unknown"` |
| `topics` | `topics` | 字符串数组，可能为空 |

字段缺失时保留 `null` / 空数组，不要猜测或补造。

### 第 3 步：过滤

**纳入**满足任一条件的仓库：

- `topics` 或名称/描述命中：`ai`、`llm`、`gpt`、`agent`、`rag`、`mcp`、`embedding`、`vector`、`transformer`、`diffusion`、`multimodal`、`fine-tuning`、`inference`、`prompt`、`langchain`、`llama` 等。
- 属于 AI 应用的基础设施：推理服务、向量数据库、评测框架、Agent 编排、模型训练/微调工具。

**排除**：

- `archived: true` 或 `fork: true` 的仓库。
- **Awesome 列表**及同类资料汇总：名称或描述含 `awesome-`、`awesome list`、`curated list`、`resources`、`collection of ... links` 等。
- 纯教程、课程、电子书、面试题、招聘信息，以及没有可运行代码的仓库。
- 与 AI / LLM / Agent 仅弱相关（如通用 Web 框架、普通 CLI 工具）。

判断依据以 `description` 与 `topics` 为主；描述模糊时可用 `WebFetch` 打开仓库主页确认，不要凭仓库名臆测。

### 第 4 步：去重

- 以 `name`（转小写）和 `url`（去掉末尾 `/`）为唯一键。
- 同一仓库在多个查询中重复出现时只保留一条，优先保留字段更完整、star 更高的记录。
- 注意改名 / 迁移后的仓库：以 API 返回的当前 `full_name` 为准。

### 第 5 步：撰写中文摘要

按固定公式组织，60–120 字：

```
项目名 + 做什么 + 为什么值得关注
```

要求：

- 基于 `description`、`topics` 与 README 事实，不照抄英文、不堆砌营销词。
- 说清能力与适用场景，避免“很强大”“非常好用”这类空话。
- 不编造 star 数、发布时间、性能数据或作者背景。

示例：

> LangGraph 是面向有状态多 Agent 应用的编排框架，用图结构描述工作流并支持检查点恢复。它适合需要多角色协作与失败重试的长任务，因此在 Agent 工程中关注度较高。

### 第 6 步：排序取 Top 15

- 按 `stars` 降序排序；star 相同时优先 `pushed_at` 更近的仓库。
- 取前 **15** 条。
- 过滤后不足 15 条时，返回实际合格数量，**不得用无关仓库凑数**。

### 第 7 步：输出 JSON

将结果整理为下方结构，交付到：

```
knowledge/raw/github-trending-YYYY-MM-DD.json
```

- `YYYY-MM-DD` 取采集时间（UTC）的日期。
- 具备写权限时直接写入该路径；若当前 Agent 无写权限，则在最终答复中输出完整 JSON 并注明目标路径，由上层落盘。
- 文件使用 UTF-8、2 空格缩进、`ensure_ascii=false`，保证可直接解析。

## 注意事项

- **不伪造数据**：`name`、`url`、`stars`、`language`、`topics` 必须来自 API 响应；摘要必须有原文依据。
- **遵守速率限制**：未认证 Search API 约 10 次/分钟；收到 `403` 或 `429` 时停止重试，减少查询或等待，不得高频轰炸。
- **只用官方 API**：不抓取需要登录的页面，不绕过 `robots.txt`，不伪造 UA。
- **不携带密钥**：URL 与输出中不得出现 token、Cookie 或 `.env` 内容。
- **Awesome 列表一律排除**：这类仓库 star 很高但对知识库是噪声。
- **摘要用中文**：项目名、专有名词可保留英文，其余用中文表达。
- **不足 15 条不凑数**：质量优先于数量，低相关仓库宁可不报。
- **字段类型稳定**：`stars` 为整数，`topics` 为数组，`language` 缺失时用 `"Unknown"`。
- **时间统一 UTC**：`collected_at` 使用 ISO 8601，例如 `2026-11-25T01:05:12Z`。
- **路径以本技能为准**：输出为 `knowledge/raw/github-trending-YYYY-MM-DD.json`。

## 输出格式

```json
{
  "source": "github_trending",
  "skill": "github-trending",
  "collected_at": "2026-11-25T01:05:12Z",
  "items": [
    {
      "name": "langchain-ai/langgraph",
      "url": "https://github.com/langchain-ai/langgraph",
      "summary": "LangGraph 是面向有状态多 Agent 应用的编排框架，用图结构描述工作流并支持检查点恢复。它适合需要多角色协作与失败重试的长任务，因此在 Agent 工程中关注度较高。",
      "stars": 12345,
      "language": "Python",
      "topics": ["agents", "llm", "orchestration"]
    },
    {
      "name": "example-org/example-agent",
      "url": "https://github.com/example-org/example-agent",
      "summary": "Example Agent 是一个轻量级 Agent 运行时，提供工具调用、上下文管理与可观测性。它适合快速搭建原型，并能在本地复现执行轨迹。",
      "stars": 6789,
      "language": "TypeScript",
      "topics": ["agent", "llm", "observability"]
    }
  ]
}
```

**结构约束**

- 顶层字段固定为 `source`、`skill`、`collected_at`、`items`。
- `source` 固定 `"github_trending"`，`skill` 固定 `"github-trending"`。
- `items` 按 `stars` 降序，最多 15 条。
- 每个 item 固定包含 `name`、`url`、`summary`、`stars`、`language`、`topics` 六个字段。
