# AI 知识库助手

自动化技术情报流水线：定时从 GitHub 与 RSS 采集 AI / LLM / Agent 领域的技术动态，
经大模型分析、去重、校验后，以 JSON 知识条目归档，并通过 MCP 供 AI 工具检索。

```
采集 (collect) → 分析 (analyze) → 整理 (organize) → 保存 (save)
```

## 核心能力

- **多源采集**：GitHub Search API + 可配置 RSS（Hacker News、Lobsters、arXiv、
  OpenAI、Hugging Face、中文社区），支持限流重试与跨源去重。
- **统一模型客户端**：DeepSeek / Qwen / OpenAI 兼容接口，超时、重试、
  token 用量与 USD 成本估算统一封装在 `pipeline/model_client.py`。
- **质量门槛**：JSON Schema 校验 + 五维质量评分（满分 100，A/B/C 分级）。
- **MCP Server**：标准库实现的 stdio 服务，向 AI 工具暴露搜索、读取与统计能力。

## 目录结构

```
.
├── AGENTS.md                     # Agent 协作契约与数据规范（先读这个）
├── mcp_knowledge_server.py       # 知识库 MCP Server（仅标准库）
├── pipeline/
│   ├── pipeline.py               # 四步流水线与 CLI
│   ├── model_client.py           # 多厂商 LLM 客户端
│   ├── distribute.py             # 飞书群机器人分发（仅标准库）
│   └── rss_sources.yaml          # RSS 数据源配置
├── hooks/
│   ├── validate_json.py          # 知识条目 schema 校验
│   └── check_quality.py          # 知识条目质量评分
├── knowledge/
│   ├── raw/YYYY-MM-DD/           # 采集原始快照（append-only）
│   └── articles/YYYY-MM-DD/      # 结构化知识条目
└── .opencode/                    # Agent 角色定义与可复用技能
```

## 快速开始

环境要求：Python 3.12+，第三方依赖仅 `httpx`。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install httpx
```

配置模型与采集凭证：

```bash
export LLM_PROVIDER=deepseek          # deepseek / qwen / openai，默认 deepseek
export DEEPSEEK_API_KEY=sk-...        # 对应 provider 的 API Key
export GITHUB_TOKEN=ghp_...           # 可选，但能显著提高 GitHub 速率上限
```

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `LLM_PROVIDER` | 否 | `deepseek`（默认）/ `qwen` / `openai` |
| `DEEPSEEK_API_KEY` | 视 provider | DeepSeek API Key |
| `QWEN_API_KEY` / `DASHSCOPE_API_KEY` | 视 provider | 通义千问 API Key，二者取其一 |
| `OPENAI_API_KEY` | 视 provider | OpenAI API Key |
| `LLM_MODEL` | 否 | 覆盖 provider 默认模型 |
| `LLM_BASE_URL` | 否 | 覆盖 OpenAI 兼容接口地址 |
| `GITHUB_TOKEN` | 否 | GitHub API Token，提高请求配额 |
| `FEISHU_WEBHOOK_URL` | 否 | 飞书群自定义机器人 Webhook，配置后才会分发 |
| `FEISHU_WEBHOOK_SECRET` | 否 | 机器人签名校验密钥，强烈建议配置 |

## 运行流水线

```bash
# 采集两个来源，每个来源最多 20 条
python3 pipeline/pipeline.py --sources github,rss --limit 20

# 只采 GitHub
python3 pipeline/pipeline.py --sources github --limit 5

# 只采 RSS
python3 pipeline/pipeline.py --sources rss --limit 10

# 干跑：只采集并打印候选，不调用 LLM、不写文件
python3 pipeline/pipeline.py --sources github --limit 5 --dry-run

# 调试日志
python3 pipeline/pipeline.py --verbose
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--sources` | `github,rss` | 逗号分隔的来源列表 |
| `--limit` | `20` | **每个来源**最多采集条数 |
| `--dry-run` | 关闭 | 仅采集与去重预检，不调用 LLM、不写文件 |
| `--verbose` | 关闭 | 输出 DEBUG 级日志 |
| `--knowledge-dir` | `knowledge/` | 知识库根目录 |
| `--rss-config` | `pipeline/rss_sources.yaml` | RSS 数据源配置 |

产物：

- 原始快照：`knowledge/raw/YYYY-MM-DD/{feed}-{timestamp}.json`，只增不改；
- 知识条目：`knowledge/articles/YYYY-MM-DD/kb-YYYYMMDD-NNNN.json`，
  评分 `>= 6` 记为 `analyzed`，否则记为 `rejected`。

## RSS 数据源

编辑 `pipeline/rss_sources.yaml` 即可增删数据源，无需改代码：

```yaml
sources:
  - name: huggingface-blog
    url: https://huggingface.co/blog/feed.xml
    category: 公司博客
    enabled: true
```

`enabled: false` 的源不会被请求。默认关闭量过大的 arXiv cs.AI、
RSS 不可用的机器之心，以及需要人工确认的 Anthropic 与量子位。

## 知识条目格式

每条知识一个独立 JSON 文件，字段、枚举与状态流转的权威定义见
[`AGENTS.md`](AGENTS.md) 第 5 节。状态单向流转：

```
raw → analyzed → published → archived
              ↘ rejected
```

## 质量校验

```bash
# schema 校验，全部通过时退出码为 0
python3 hooks/validate_json.py "knowledge/articles/2026-11-25/*.json"

# 五维质量评分，出现 C 级时退出码为 1
python3 hooks/check_quality.py "knowledge/articles/2026-11-25/*.json"
```

## 分发到飞书

`pipeline/distribute.py` 把当日条目推送为一张飞书消息卡片，仅依赖标准库，
可以在 `pip install` 之前运行。

### 配置群机器人

1. 目标群 → **设置 → 群机器人 → 添加机器人 → 自定义机器人**
2. 安全设置选 **签名校验**，复制密钥
3. 复制 Webhook URL

> 不要选「IP 白名单」：GitHub Actions 的出口 IP 不固定，选了会一直 403。

### 本地运行

```bash
# 只打印卡片 JSON，不发送、不写文件
python3 pipeline/distribute.py --date 2026-11-25 --dry-run

export FEISHU_WEBHOOK_URL='https://open.feishu.cn/open-apis/bot/v2/hook/xxx'
export FEISHU_WEBHOOK_SECRET='xxx'

# 真发，并回写 distribution.feishu 状态
python3 pipeline/distribute.py --date 2026-11-25 --mark-distribution
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--date` | 今天 UTC | 要推送的日期目录 |
| `--channel` | `feishu` | 分发渠道（当前仅飞书） |
| `--top` | `5` | 卡片内最多条目数 |
| `--min-score` | `6` | 推送所需最低评分 |
| `--dry-run` | 关闭 | 只打印卡片 JSON |
| `--force` | 关闭 | 已有 `sent` 记录时仍重新发送 |
| `--mark-distribution` | 关闭 | 发送成功后回写条目状态 |
| `--repo-url` | `GITHUB_REPOSITORY` 推断 | 「查看全部」按钮的仓库地址 |

### 推送规则

- 只推送 `status` 为 `analyzed` 或 `published` 且 `score >= 6` 的条目，
  未经分析的内容不会进入群聊；
- 按分数降序取前 `--top` 条，摘要截断 180 字；
- 卡片底部按钮跳转到当日目录，可查看全部条目；
- 回写只改动 `distribution` 字段，且自定义机器人不返回 `message_id`，
  该字段保持 `null`；
- 当日已有条目被标记为 `sent` 时默认跳过，避免手动重跑刷屏。

### 在 GitHub Actions 中

在 **Settings → Secrets and variables → Actions** 配置 `FEISHU_WEBHOOK_URL` 与
`FEISHU_WEBHOOK_SECRET`。工作流会在提交前推送并回写状态；未配置时自动跳过。
推送失败会让本次运行标红，但数据仍会正常提交。

## MCP Server

`mcp_knowledge_server.py` 通过 JSON-RPC 2.0 over stdio 暴露三个只读工具：

| 工具 | 说明 |
| --- | --- |
| `search_articles(keyword, limit=5)` | 检索标题、摘要与标签，多关键词需全部命中 |
| `get_article(article_id)` | 按 ID / 文件名主干 / 上游 `source_id` 获取完整条目 |
| `knowledge_stats()` | 文章总数、来源与状态分布、热门标签、评分与时间范围 |

手动调试：

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | python3 mcp_knowledge_server.py --verbose
```

OpenCode 接入（`opencode.json`）：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "knowledge": {
      "type": "local",
      "command": ["python3", "/absolute/path/to/mcp_knowledge_server.py"],
      "enabled": true
    }
  }
}
```

Claude Desktop 等使用 `mcpServers` 的客户端：

```json
{
  "mcpServers": {
    "knowledge": {
      "command": "python3",
      "args": ["/absolute/path/to/mcp_knowledge_server.py"]
    }
  }
}
```

## 开发约定

编码规范、日志、测试与提交要求以 [`AGENTS.md`](AGENTS.md) 为准，要点：

- Python 3.12，PEP 8，行宽 ≤ 100，公开接口必须有 Google 风格 docstring；
- 禁止裸 `print()`，统一使用 `logging`；
- 提交信息使用 Conventional Commits：`feat:`、`fix:`、`docs:`、`refactor:`、
  `chore:`、`data:`；
- 禁止提交密钥、禁止改写 `knowledge/raw/` 与已发布条目。
