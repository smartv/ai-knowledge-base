# AGENTS.md

本文件是本仓库的 Agent 协作契约。所有参与本项目的 AI Agent 与人类贡献者在修改代码、数据或配置前，必须先阅读并遵守本文件。

---

## 1. 项目概述

**AI 知识库助手**是一个自动化技术情报流水线：定时从 GitHub Trending 与 Hacker News 采集 AI / LLM / Agent 领域的技术动态，经大模型分析、去重、摘要与打标后，结构化为 JSON 知识条目归档到 `knowledge/`，再通过 OpenClaw 网关分发到 Telegram、飞书等多渠道。目标是让团队每天用最少的时间获得经过筛选、可检索、可追溯的高信号技术信息。

核心链路：

```
采集 (collector) → 分析 (analyst) → 整理归档 (curator) → 多渠道分发 (OpenClaw)
```

---

## 2. 技术栈

| 层次 | 选型 | 说明 |
| --- | --- | --- |
| 语言 | **Python 3.12** | 统一使用 `python3.12`，不使用 3.12 以下语法特性之外的兼容代码 |
| Agent 运行时 | **OpenCode** | Agent 定义放在 `.opencode/agents/`，技能放在 `.opencode/skills/` |
| 模型 | **国产大模型** | 通过 OpenAI 兼容接口调用（如 Qwen、GLM、DeepSeek、Kimi）；模型 ID 一律走配置，禁止硬编码 |
| 编排 | **LangGraph** | 采集 → 分析 → 整理的多节点状态机，节点需可重试、可断点续跑 |
| 分发 | **OpenClaw** | 统一消息网关，负责 Telegram / 飞书渠道投递与格式化 |
| 数据格式 | **JSON（UTF-8, 2 空格缩进）** | 知识条目唯一事实来源，禁止使用 YAML/数据库作为主存储 |
| 依赖管理 | `pyproject.toml` + `uv` | 锁定版本，提交 `uv.lock` |

**模型调用约定**

- 所有模型调用必须封装在 `src/llm/` 客户端中，业务代码不直接发 HTTP 请求。
- 必须设置超时、最大重试次数与 token 上限；失败时抛出明确异常，不得静默返回空字符串。
- Prompt 模板统一放 `src/prompts/`，禁止在业务逻辑里拼接长 Prompt 字符串。

---

## 3. 编码规范

### 3.1 基础规范

- 严格遵循 **PEP 8**：4 空格缩进，行宽 ≤ 100，模块级常量全大写。
- 命名一律 **snake_case**：变量、函数、模块、文件名；类名用 `PascalCase`；私有成员前置单下划线。
- 所有公开函数、类、模块必须有 **Google 风格 docstring**（含 `Args` / `Returns` / `Raises`）。
- 必须提供类型注解，禁止裸 `Any` 兜底；复杂结构用 `TypedDict` 或 `dataclass` 建模。
- 单个函数不超过 50 行，超过则拆分；嵌套层级不超过 3 层。
- 异常必须捕获具体类型，禁止 `except Exception: pass`。

### 3.2 日志

- **禁止裸 `print()`**。统一使用 `src/logging_config.py` 暴露的 `get_logger(__name__)`。
- 日志使用结构化字段，敏感信息（URL 中的 token、API Key）必须脱敏。

```python
from src.logging_config import get_logger

logger = get_logger(__name__)


def fetch_trending(language: str = "python", since: str = "daily") -> list[dict]:
    """Fetch GitHub Trending repositories.

    Args:
        language: Repository language filter, e.g. ``"python"``.
        since: Trending window, one of ``"daily"``, ``"weekly"``, ``"monthly"``.

    Returns:
        A list of raw repository records.

    Raises:
        FetchError: If the upstream request fails after retries.
    """
    logger.info("fetch_trending_start", extra={"language": language, "since": since})
    ...
```

### 3.3 测试与提交

- 新增逻辑必须配 `pytest` 用例，覆盖率不低于 80%；网络请求一律 mock。
- Commit 使用 Conventional Commits：`feat:`、`fix:`、`docs:`、`refactor:`、`chore:`、`data:`。
- 提交前必须通过 `ruff check .`、`ruff format --check .`、`mypy src/`、`pytest`。
- 数据类变更（schema、采集源）使用 `data:` 前缀，并在 PR 描述中说明影响范围。

---

## 4. 项目结构

```
.
├── AGENTS.md                  # 本文件：Agent 协作契约
├── pyproject.toml
├── uv.lock
├── .env.example               # 仅示例，真实密钥放 .env（已 gitignore）
├── .opencode/
│   ├── agents/                # Agent 角色定义（collector / analyst / curator）
│   └── skills/                # 可复用技能：采集、摘要、打标、分发
├── src/
│   ├── graph/                 # LangGraph 状态机与节点
│   ├── collectors/            # GitHub Trending / Hacker News 采集器
│   ├── llm/                   # 国产模型统一客户端
│   ├── prompts/               # Prompt 模板
│   ├── distribute/            # OpenClaw 渠道适配（Telegram / 飞书）
│   ├── models/                # 知识条目等数据模型
│   └── logging_config.py
├── knowledge/
│   ├── raw/                   # 采集原始快照，按日期分区，只增不改
│   │   └── YYYY-MM-DD/
│   └── articles/              # 结构化知识条目 + 成稿，按日期分区
│       └── YYYY-MM-DD/
├── tests/
└── scripts/                   # 一次性运维脚本
```

**目录职责边界**

- `knowledge/raw/`：上游原始数据快照，**append-only**，用于回溯与重跑。
- `knowledge/articles/`：分析后的知识条目（JSON）与可读成稿（Markdown），是所有下游消费的唯一来源。
- `.opencode/agents/`：只放角色定义与工具权限，不放业务实现。
- `.opencode/skills/`：可被多个 Agent 复用的原子能力，必须自包含、可单独测试。

---

## 5. 知识条目 JSON 格式

每条知识一个 `knowledge/articles/YYYY-MM-DD/<id>.json` 文件，UTF-8 编码、2 空格缩进、字段顺序稳定。

```json
{
  "schema_version": 1,
  "id": "kb-20261125-0001",
  "title": "LangGraph 1.0 发布：更稳定的多 Agent 编排原语",
  "source": "github_trending",
  "source_url": "https://github.com/langchain-ai/langgraph",
  "source_id": "langchain-ai/langgraph",
  "author": "langchain-ai",
  "language": "en",
  "published_at": "2026-11-24T16:30:00Z",
  "collected_at": "2026-11-25T01:05:12Z",
  "summary": "LangGraph 1.0 正式发布，重点改进状态持久化、断点续跑与人工介入节点，官方称长任务成功率提升约 30%。本文梳理其核心 API 变化及对现有 Agent 项目的影响。",
  "key_points": [
    "新增 checkpointer 默认持久化策略",
    "支持节点级人工审批中断",
    "Python 3.12 下性能优化明显"
  ],
  "tags": ["langgraph", "agent-orchestration", "framework"],
  "category": "framework",
  "relevance_score": 0.92,
  "status": "analyzed",
  "raw_path": "knowledge/raw/2026-11-25/github_trending.json",
  "article_path": "knowledge/articles/2026-11-25/kb-20261125-0001.md",
  "distribution": {
    "telegram": { "status": "pending", "message_id": null, "sent_at": null },
    "feishu": { "status": "pending", "message_id": null, "sent_at": null }
  }
}
```

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `schema_version` | int | 是 | 当前为 `1`，结构变更必须递增 |
| `id` | string | 是 | `kb-YYYYMMDD-NNNN`，全局唯一，生成后不可变 |
| `title` | string | 是 | 原文标题或规范化标题，≤ 200 字符 |
| `source` | string | 是 | 枚举：`github_trending` / `hacker_news` |
| `source_url` | string | 是 | 原始链接，必须真实可访问，禁止编造 |
| `source_id` | string | 否 | 上游唯一标识（repo full_name / HN item id） |
| `author` | string | 否 | 仓库 owner 或文章作者 |
| `language` | string | 是 | 原文语言，ISO 639-1 |
| `published_at` | string | 否 | 原文发布时间，ISO 8601 UTC |
| `collected_at` | string | 是 | 采集时间，ISO 8601 UTC |
| `summary` | string | 是 | 中文摘要，150–300 字，禁止照抄原文 |
| `key_points` | string[] | 是 | 2–5 条要点 |
| `tags` | string[] | 是 | 3–8 个，小写 kebab-case，复用已有标签 |
| `category` | string | 是 | 枚举：`model` / `framework` / `tool` / `paper` / `opinion` / `dataset` |
| `relevance_score` | float | 是 | `0.0–1.0`，低于 `0.6` 标记 `rejected` |
| `status` | string | 是 | 枚举：`raw` / `analyzed` / `published` / `archived` / `rejected` |
| `raw_path` | string | 是 | 对应原始快照的相对路径 |
| `article_path` | string | 否 | Markdown 成稿路径，`published` 时必填 |
| `distribution` | object | 是 | 各渠道投递状态，见下 |

**状态流转（单向，禁止跳级）**

```
raw → analyzed → published → archived
              ↘ rejected
```

**约束**

- 已写入磁盘的条目只允许修改 `status`、`distribution`、`article_path` 与追加型字段（如 `updated_at`）；修改 `title`/`summary` 等需新建版本并在 `changelog` 中记录。
- 同一 `source_url` 只允许一条活跃条目；重复采集需合并到已有条目。
- `distribution.*.status` 枚举：`pending` / `sent` / `failed` / `skipped`。

---

## 6. Agent 角色概览

| 角色 | 名称 | 职责 | 输入 | 输出 | 工具/技能 |
| --- | --- | --- | --- | --- | --- |
| 采集 | `collector` | 抓取 GitHub Trending 与 Hacker News，做基础过滤与原始快照落盘 | 采集配置（语言、时间窗、关键词） | `knowledge/raw/YYYY-MM-DD/*.json` | HTTP 抓取、去重、限流；`skills/fetch-*` |
| 分析 | `analyst` | 阅读原始数据，判断相关性，生成中文摘要、要点、标签与分类 | 原始快照 | 草稿条目（`status=analyzed`） | LLM 客户端、摘要/打标 Prompt；`skills/summarize` |
| 整理 | `curator` | 审核草稿、统一标签词表、生成 Markdown 成稿、决定发布或归档 | 草稿条目 | `knowledge/articles/YYYY-MM-DD/*.json` + `.md` | 词表校验、模板渲染；`skills/distribute` |

**协作规则**

- 三个角色通过 `knowledge/` 中的文件交接，不共享进程内状态。
- `analyst` 不得改写 `collector` 产出的原始快照；`curator` 不得跳过 `analyzed` 直接发布 `raw` 条目。
- 分发动作只由 `curator` 触发，且必须在条目 `status=published` 之后执行。
- 任一角色失败时必须留下可诊断日志与中间产物，禁止回滚已成功的上游产物。

---

## 7. 红线（绝对禁止）

以下行为视为严重违规，任何 Agent 不得以「临时」「调试」「效率」为由触碰：

1. **禁止编造内容与链接。** `source_url` 必须来自真实采集结果；摘要不得虚构原文不存在的事实、数据或引述。
2. **禁止泄露或提交密钥。** 不得在代码、日志、Prompt、知识条目、提交历史中出现 API Key、Bot Token、Webhook、`.env` 内容；发现泄露必须立即上报。
3. **禁止绕过采集约束。** 不得忽略 `robots.txt`、不得高频轰炸上游、不得伪造 UA 或使用未授权代理池；遵守各源速率限制。
4. **禁止未经审核的群发。** 不得跳过 `curator` 审核直接向 Telegram / 飞书生产渠道推送；不得向非配置内的会话发送。
5. **禁止破坏历史数据。** 不得删除、覆盖 `knowledge/raw/` 与已 `published` 的条目；不得执行 `git push --force` 或改写已推送历史。
6. **禁止裸 `print()` 与调试残留。** 不得提交注释掉的代码块、`breakpoint()`、测试用硬编码数据或临时开关。
7. **禁止任意扩大依赖与权限。** 新增第三方依赖须说明理由；不得让 Agent 获取超出角色的文件/网络/密钥权限。
8. **禁止把用户/上游数据发往未授权模型。** 只允许调用项目配置中登记过的国产模型端点。
9. **禁止静默失败。** 不得用 `except: pass` 吞异常，不得在采集或分发失败时返回成功。
10. **禁止修改本文件的红线条款。** 如需调整，必须由人类维护者评审并显式批准。

---

## 8. 常见任务检查清单

开始任何改动前：

- [ ] 确认改动属于哪个角色/目录，是否越界
- [ ] 确认不会触碰红线
- [ ] 新逻辑是否配了测试与类型注解
- [ ] 是否使用了 `get_logger()` 而非 `print()`
- [ ] 数据变更是否更新了 `schema_version` 或标签词表
- [ ] 提交前是否通过 `ruff` / `mypy` / `pytest`
