---
name: tech-summary
description: 当需要对采集的技术内容进行深度分析总结时使用此技能
allowed-tools: Read, Grep, Glob, WebFetch
---

# 技术内容深度分析

对 `knowledge/raw/` 中最新的采集结果做逐条深度分析与跨条目趋势归纳，产出可归档的结构化 JSON。

## 使用场景

- `collector` 完成采集后，需要快速判断每条内容的价值。
- 日报 / 周报需要「项目级结论 + 整体趋势」两层信息。
- 进入 `organizer` 归档前，需要先完成评分与标签校准。

不适用：抓取新条目、写文件归档、生成 Markdown 成稿。

## 执行步骤

### 第 1 步：读取最新采集文件

1. 用 `Glob` 列出 `knowledge/raw/**/*.json`。
2. 从路径或文件内 `collected_at` 解析日期，选最新一天；同一天有多个来源（`github_trending` / `hacker_news`）时全部读取。
3. 路径不含日期时，按文件名排序取最后一个；不要用 `Bash` 查询修改时间。
4. 合并后按 `url` 去重，最多保留 15 条；超出时按热度与来源均衡取舍。
5. 记录实际读取的文件路径，写入输出的 `source_files`。
6. 文件缺失或 JSON 非法时停止并报告，不要猜测内容。

### 第 2 步：逐条深度分析

对每条内容产出以下字段：

| 字段 | 要求 |
| --- | --- |
| `summary` | ≤ 50 字（含标点），说清「是什么 + 为什么值得关注」 |
| `highlights` | 2–3 条，每条 15–40 字，必须是可验证的事实、能力或数据 |
| `score` | 1–10 的整数 |
| `score_reason` | 20–50 字，给出具体加减分依据 |
| `tags` | 3–5 个，小写 kebab-case，优先复用既有标签 |

摘要示例：

> LangGraph 用图结构编排多 Agent 工作流，支持检查点恢复，适合长任务。

亮点要求「用事实说话」：

- ✅ 「内置检查点，失败后从最近节点恢复，无需重跑全流程」
- ✅ 「支持节点级人工审批，可插入生产审核环节」
- ❌ 「架构先进，社区活跃，非常值得关注」

评分标准见下一节。

### 第 3 步：趋势发现

逐条分析完成后，再做跨条目归纳：

1. **共同主题**：至少 2 条内容指向同一方向才可列为 `theme`；写出主题名、1–2 句判断、支撑条目。
2. **新概念**：识别近期出现、尚未进入既有标签体系的术语；用一句话定义并列出支撑条目。只有 1 条支撑时标注「早期信号」，不得包装成确定趋势。
3. 趋势必须由 `items` 中的证据支撑，禁止凭常识补充。
4. 主题数量控制在 2–5 个，新概念 1–3 个；没有可靠趋势时输出空数组，不要凑数。

### 第 4 步：输出分析结果 JSON

- 最终答复只输出一个 ```json 代码块，不附加解释性文字。
- 使用 UTF-8、2 空格缩进，保证可直接解析。
- 本技能 `allowed-tools` 不含 `Write`：默认把 JSON 作为答复返回；如需归档，由具备写权限的上层保存为 `knowledge/raw/tech-summary-YYYY-MM-DD.json`。
- 输出前检查 9–10 分数量是否 ≤ 2，并在 `stats` 中如实统计。

## 评分标准

| 分数 | 含义 | 判断要点 |
| --- | --- | --- |
| **9–10** | 改变格局 | 范式级变化、头部厂商关键发布、可能重塑工作流；有硬数据或广泛影响佐证 |
| **7–8** | 直接有帮助 | 能马上用于项目：新框架、新工具、可复用方案、重要模型更新 |
| **5–6** | 值得了解 | 有信息量但暂不落地：早期项目、局部优化、有价值观点 |
| **1–4** | 可略过 | 同质化、营销稿、信息量低、无法验证、弱相关 |

**高分配额（硬约束）**

- 15 条项目中，9–10 分**最多 2 条**。
- 若超过 2 条，保留影响最大的 2 条，其余降到 8 分，并在 `score_reason` 注明「受 9–10 配额限制」。
- 不足 15 条时同样最多 2 条；不得为了凑配额抬高其他条目。
- 评分只给整数；证据不足时取下限。

## 注意事项

- 不编造事实、数据、链接或引述；亮点必须能在原文或仓库页面找到依据。
- `WebFetch` 只用于核对 `knowledge/raw/` 中已有条目的原文，不用于发现新条目。
- 遵守上游速率限制，不绕过 `robots.txt`，不高频请求。
- 不读取、不输出任何密钥或 `.env` 内容。
- 摘要严格 ≤ 50 字；超长时删修饰词，保留「能力 + 价值」。
- 标签优先复用 `knowledge/articles/` 中的已有词表，避免同义词分裂。
- 趋势归纳只基于本批数据，不外推成行业定论。
- 不写文件、不执行命令；你的产出只有 JSON。

## 输出格式

```json
{
  "skill": "tech-summary",
  "analyzed_at": "2026-11-25T02:00:00Z",
  "source_files": [
    "knowledge/raw/github-trending-2026-11-25.json"
  ],
  "items": [
    {
      "title": "langchain-ai/langgraph",
      "url": "https://github.com/langchain-ai/langgraph",
      "source": "github_trending",
      "summary": "用图结构编排多 Agent 工作流，支持检查点恢复，适合长任务。",
      "highlights": [
        "以图结构表达条件分支与循环",
        "检查点支持失败节点恢复",
        "提供节点级人工审批"
      ],
      "score": 8,
      "score_reason": "可直接用于生产级多 Agent 编排，但非范式级突破。",
      "tags": ["langgraph", "agent-orchestration", "checkpointing"]
    },
    {
      "title": "example-org/example-agent",
      "url": "https://github.com/example-org/example-agent",
      "source": "github_trending",
      "summary": "轻量 Agent 运行时，提供工具调用与执行轨迹回放。",
      "highlights": [
        "内置工具调用与上下文管理",
        "支持本地复现执行轨迹"
      ],
      "score": 7,
      "score_reason": "适合快速搭建原型，生态与生产特性仍需观察。",
      "tags": ["agent-runtime", "observability"]
    }
  ],
  "trends": {
    "themes": [
      {
        "theme": "Agent 工作流强调可恢复与可观测",
        "summary": "多个项目同时把检查点、失败重试与执行轨迹作为核心能力。",
        "evidence": ["langchain-ai/langgraph", "example-org/example-agent"]
      }
    ],
    "new_concepts": [
      {
        "concept": "agent checkpointing",
        "description": "把 Agent 执行状态持久化，失败后从最近节点恢复。",
        "evidence": ["langchain-ai/langgraph"]
      }
    ]
  },
  "stats": {
    "total": 2,
    "score_distribution": {
      "9-10": 0,
      "7-8": 2,
      "5-6": 0,
      "1-4": 0
    }
  }
}
```

**结构约束**

- 顶层字段固定为 `skill`、`analyzed_at`、`source_files`、`items`、`trends`、`stats`。
- `items` 最多 15 条，每条固定包含 `title`、`url`、`source`、`summary`、`highlights`、`score`、`score_reason`、`tags`。
- `trends.themes[]` 固定包含 `theme`、`summary`、`evidence`；`trends.new_concepts[]` 固定包含 `concept`、`description`、`evidence`。
- `stats` 固定包含 `total` 与 `score_distribution`，且 `total` 必须等于 `items` 长度。
