#!/usr/bin/env python3
"""Router 路由模式：关键词优先、LLM 兜底的三路意图分发。

支持三种意图：

- ``github_search``：检索 GitHub 仓库，使用标准库 ``urllib`` 调用 Search API；
- ``knowledge_query``：检索本地知识库（优先 ``knowledge/articles/index.json``，
  缺失时回退扫描按日期分区的文章 JSON）；
- ``general_chat``：调用 LLM 直接回答。

分类分两层：先做零成本的关键词匹配，关键词无法唯一判定时才调用 LLM 分类，
避免每次请求都产生模型费用。

Example:
    python patterns/router.py "搜索 GitHub 上 star 最多的 RAG 框架"
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.model_client import (  # noqa: E402 - 需先调整 sys.path
    LLMError,
    chat,
    chat_json,
)

LOGGER = logging.getLogger(__name__)

INTENT_GITHUB = "github_search"
INTENT_KNOWLEDGE = "knowledge_query"
INTENT_CHAT = "general_chat"
INTENTS = (INTENT_GITHUB, INTENT_KNOWLEDGE, INTENT_CHAT)

DEFAULT_KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"
ARTICLES_DIRNAME = "articles"
INDEX_FILENAME = "index.json"

GITHUB_API_URL = "https://api.github.com/search/repositories"
GITHUB_TIMEOUT_SECONDS = 15.0
GITHUB_RESULT_LIMIT = 5
GITHUB_DESCRIPTION_CHARS = 180
GITHUB_KEYWORD_LIMIT = 5

KNOWLEDGE_RESULT_LIMIT = 3
KNOWLEDGE_SUMMARY_CHARS = 160

_GITHUB_KEYWORDS = (
    "github",
    "仓库",
    "开源项目",
    "repository",
    "repo",
    "star",
)
_KNOWLEDGE_KEYWORDS = (
    "知识库",
    "知识条目",
    "文章",
    "收录",
    "kb-",
    "资料",
    "检索",
    "条目",
)
_CHAT_KEYWORDS = (
    "你好",
    "您好",
    "谢谢",
    "你是谁",
    "介绍一下你",
    "讲个笑话",
)

# 提取 GitHub 搜索词时剔除的修饰语与停用词。
_GITHUB_NOISE = (
    "github 上",
    "github上",
    "github",
    "开源项目",
    "仓库",
    "项目",
    "repo",
    "帮我",
    "帮忙",
    "请",
    "搜索",
    "查找",
    "搜一下",
    "找一下",
    "找",
    "一下",
    "关于",
    "相关",
    "有哪些",
    "有没有",
    "的",
    "一些",
)
_GITHUB_STOPWORDS = frozenset(
    {
        "github",
        "repo",
        "repository",
        "project",
        "search",
        "star",
        "stars",
        "best",
        "top",
        "the",
        "and",
        "for",
        "with",
    }
)

# 中文查询里常见的功能性短语，匹配前先剔除。
_CHINESE_STOP_PHRASES = (
    "知识库里",
    "知识库中",
    "知识库",
    "知识条目",
    "文章",
    "条目",
    "资料",
    "内容",
    "有哪些",
    "哪些",
    "有没有",
    "关于",
    "相关",
    "介绍",
    "一下",
    "帮我",
    "帮忙",
    "查找",
    "搜索",
    "检索",
    "看看",
    "我想",
    "知道",
    "请",
    "里",
    "的",
    "吗",
    "呢",
)

_LATIN_TERM_RE = re.compile(r"[a-z0-9_+#.\-]{2,}")
_CHINESE_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


@dataclass(frozen=True)
class KnowledgeEntry:
    """One searchable knowledge article.

    Attributes:
        id: Knowledge-entry identifier.
        title: Article title.
        summary: Chinese summary.
        tags: Article tags.
        url: Original source URL.
    """

    id: str
    title: str
    summary: str
    tags: tuple[str, ...]
    url: str


def _truncate(text: str, limit: int) -> str:
    """Truncate text to ``limit`` characters with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def classify_by_keywords(query: str) -> str | None:
    """Classify a query with keyword rules only.

    Args:
        query: User query.

    Returns:
        The matching intent when one keyword group clearly wins, otherwise
        ``None`` so the caller can fall back to the LLM.
    """
    lowered = query.casefold()
    scores = {
        INTENT_GITHUB: sum(
            1 for word in _GITHUB_KEYWORDS if word in lowered
        ),
        INTENT_KNOWLEDGE: sum(
            1 for word in _KNOWLEDGE_KEYWORDS if word in lowered
        ),
        INTENT_CHAT: sum(
            1 for word in _CHAT_KEYWORDS if word in lowered
        ),
    }

    best = max(scores.values())
    if best == 0:
        return None

    winners = [intent for intent, score in scores.items() if score == best]
    if len(winners) != 1:
        LOGGER.debug("关键词打分持平: %s", scores)
        return None

    LOGGER.debug("关键词打分: %s", scores)
    return winners[0]


def classify_by_llm(query: str) -> str:
    """Classify a query with the LLM.

    Args:
        query: User query.

    Returns:
        One of :data:`INTENTS`; falls back to ``general_chat`` when the model
        call fails or returns an invalid intent.
    """
    prompt = (
        "把下面的用户问题分类为三种意图之一：\n"
        "- github_search：想在 GitHub 上查找仓库、开源项目或代码\n"
        "- knowledge_query：想查询本地知识库中已收录的文章、条目或资料\n"
        "- general_chat：其他问题、闲聊或通用知识问答\n\n"
        f"用户问题：{query}\n\n"
        '只输出 JSON，例如 {"intent": "github_search", "confidence": 0.9}'
    )
    try:
        payload, _usage = chat_json(
            prompt,
            system="你是意图分类器，只输出 JSON 对象。",
            temperature=0.0,
            max_tokens=100,
        )
    except (LLMError, ValueError) as exc:
        LOGGER.warning("LLM 意图分类失败，回退 general_chat: %s", exc)
        return INTENT_CHAT

    intent = payload.get("intent")
    if not isinstance(intent, str) or intent not in INTENTS:
        LOGGER.warning("LLM 返回非法意图 %r，回退 general_chat", intent)
        return INTENT_CHAT
    return intent


def classify_intent(query: str) -> str:
    """Classify a query with keywords first, then the LLM.

    Args:
        query: User query.

    Returns:
        One of :data:`INTENTS`.

    Raises:
        ValueError: If ``query`` is empty.
    """
    if not query.strip():
        raise ValueError("query 不能为空")

    intent = classify_by_keywords(query)
    if intent is not None:
        LOGGER.info("关键词命中意图: %s", intent)
        return intent

    LOGGER.info("关键词无法判定，调用 LLM 分类")
    intent = classify_by_llm(query)
    LOGGER.info("LLM 判定意图: %s", intent)
    return intent


def _github_headers() -> dict[str, str]:
    """Build GitHub API headers, using ``GITHUB_TOKEN`` when available."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ai-knowledge-base-router/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _extract_github_keyword(query: str) -> str:
    """Extract a concise GitHub search keyword from a natural-language query."""
    latin = [
        term
        for term in _LATIN_TERM_RE.findall(query.casefold())
        if term not in _GITHUB_STOPWORDS
    ]
    if latin:
        return " ".join(latin[:GITHUB_KEYWORD_LIMIT])

    cleaned = query
    for token in _GITHUB_NOISE:
        cleaned = re.sub(
            re.escape(token),
            " ",
            cleaned,
            flags=re.IGNORECASE,
        )
    cleaned = " ".join(cleaned.split()).strip(" ，。！？,.!?")
    return cleaned or query.strip()


def _format_github_results(items: Sequence[Any], keyword: str) -> str:
    """Format GitHub search items as plain text."""
    lines = [f"GitHub 搜索结果（关键词：{keyword}）"]
    for index, item in enumerate(items[:GITHUB_RESULT_LIMIT], start=1):
        if not isinstance(item, dict):
            continue
        name = item.get("full_name") or item.get("name") or "unknown"
        stars = item.get("stargazers_count")
        stars_text = f"{stars:,}" if isinstance(stars, int) else "未知"
        language = item.get("language") or "未标注"
        description = item.get("description") or "（无描述）"
        html_url = item.get("html_url") or ""
        lines.append(
            f"{index}. {name}（⭐ {stars_text}，{language}）\n"
            f"   {_truncate(str(description), GITHUB_DESCRIPTION_CHARS)}\n"
            f"   {html_url}"
        )
    return "\n".join(lines)


def handle_github_search(query: str) -> str:
    """Search GitHub repositories and format the top results.

    Args:
        query: User query; trigger words are stripped before searching.

    Returns:
        A human-readable result summary. Network failures are reported in the
        returned text instead of raising, so :func:`route` always answers.
    """
    keyword = _extract_github_keyword(query)
    encoded = urllib.parse.quote(keyword, safe="")
    url = (
        f"{GITHUB_API_URL}?q={encoded}"
        f"&sort=stars&order=desc&per_page={GITHUB_RESULT_LIMIT}"
    )
    request = urllib.request.Request(url, headers=_github_headers())

    try:
        with urllib.request.urlopen(
            request,
            timeout=GITHUB_TIMEOUT_SECONDS,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        LOGGER.warning("GitHub 搜索 HTTP 错误: %s", exc)
        hint = "（可能是未配置 GITHUB_TOKEN 触发限流）" if exc.code == 403 else ""
        return f"GitHub 搜索失败：HTTP {exc.code}{hint}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        LOGGER.warning("GitHub 搜索失败: %s", exc)
        return f"GitHub 搜索失败：{exc}"

    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        return f"GitHub 上没有找到与「{keyword}」相关的仓库。"

    return _format_github_results(items, keyword)


def _read_index_file(path: Path) -> list[Mapping[str, Any]]:
    """Read ``index.json`` in either list or ``{articles: [...]}`` form."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("读取索引失败 %s: %s", path, exc)
        return []

    if isinstance(payload, dict):
        payload = payload.get("articles", [])
    if not isinstance(payload, list):
        LOGGER.warning("索引格式非法（应为数组或 {articles: []}）: %s", path)
        return []

    return [item for item in payload if isinstance(item, dict)]


def _scan_article_files(root: Path) -> list[Mapping[str, Any]]:
    """Read every dated article JSON under the articles directory."""
    if not root.is_dir():
        return []

    records: list[Mapping[str, Any]] = []
    for path in sorted(root.rglob("*.json")):
        if path.name == INDEX_FILENAME:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("跳过无法解析的文章 %s: %s", path, exc)
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _to_entry(
    record: Mapping[str, Any],
    fallback_id: str,
) -> KnowledgeEntry | None:
    """Normalize one raw record into a :class:`KnowledgeEntry`."""
    article_id = record.get("id")
    article_id = article_id.strip() if isinstance(article_id, str) else ""
    article_id = article_id or fallback_id
    if not article_id:
        return None

    title = record.get("title")
    title = title.strip() if isinstance(title, str) else ""
    if not title:
        return None

    summary = record.get("summary")
    summary = summary.strip() if isinstance(summary, str) else ""

    tags_value = record.get("tags")
    tags = (
        tuple(
            tag.strip()
            for tag in tags_value
            if isinstance(tag, str) and tag.strip()
        )
        if isinstance(tags_value, list)
        else ()
    )

    url = record.get("source_url")
    url = url.strip() if isinstance(url, str) else ""

    return KnowledgeEntry(
        id=article_id,
        title=title,
        summary=summary,
        tags=tags,
        url=url,
    )


def load_knowledge_index(
    knowledge_dir: Path | None = None,
) -> list[KnowledgeEntry]:
    """Load searchable knowledge entries.

    ``knowledge/articles/index.json`` is preferred when it exists. Because the
    collection pipeline writes dated JSON files without maintaining an index,
    the function falls back to scanning ``knowledge/articles/**/*.json`` so the
    router still works out of the box.

    Args:
        knowledge_dir: Knowledge root; defaults to the project ``knowledge/``.

    Returns:
        Deduplicated entries ordered by file name.
    """
    root = (knowledge_dir or DEFAULT_KNOWLEDGE_DIR) / ARTICLES_DIRNAME
    index_path = root / INDEX_FILENAME

    if index_path.is_file():
        raw_records = _read_index_file(index_path)
        LOGGER.debug("从 %s 载入 %d 条索引", index_path, len(raw_records))
    else:
        LOGGER.info("未找到 %s，回退扫描文章目录", index_path)
        raw_records = _scan_article_files(root)

    entries: list[KnowledgeEntry] = []
    seen: set[str] = set()
    for record in raw_records:
        fallback_id = str(record.get("source_id") or "")
        entry = _to_entry(record, fallback_id)
        if entry is None or entry.id in seen:
            continue
        seen.add(entry.id)
        entries.append(entry)
    return entries


def _query_terms(query: str) -> list[str]:
    """Extract searchable Latin words and Chinese chunks from a query."""
    lowered = query.casefold()
    terms = set(_LATIN_TERM_RE.findall(lowered))

    chinese = " ".join(_CHINESE_RUN_RE.findall(lowered))
    for phrase in _CHINESE_STOP_PHRASES:
        chinese = chinese.replace(phrase, " ")

    for chunk in chinese.split():
        if len(chunk) < 2:
            continue
        terms.add(chunk)
        if len(chunk) >= 4:
            terms.update(
                chunk[index : index + 2]
                for index in range(len(chunk) - 1)
            )

    return sorted(terms)


def _score_entry(entry: KnowledgeEntry, terms: Sequence[str]) -> int:
    """Score one entry against the query terms."""
    title = entry.title.casefold()
    summary = entry.summary.casefold()
    tags = [tag.casefold() for tag in entry.tags]

    score = 0
    for term in terms:
        score += title.count(term) * 3
        score += summary.count(term)
        score += sum(2 for tag in tags if term in tag)
    return score


def handle_knowledge_query(query: str) -> str:
    """Search the local knowledge base and format the top matches.

    Args:
        query: User query.

    Returns:
        A human-readable result summary; an empty knowledge base or a query
        without matches produces an explanatory message.
    """
    entries = load_knowledge_index()
    if not entries:
        return "知识库暂无文章，请先运行采集流水线。"

    terms = _query_terms(query)
    if not terms:
        terms = [query.strip().casefold()]

    scored = [
        (entry, _score_entry(entry, terms))
        for entry in entries
    ]
    hits = sorted(
        (item for item in scored if item[1] > 0),
        key=lambda item: (-item[1], item[0].id),
    )[:KNOWLEDGE_RESULT_LIMIT]

    if not hits:
        return f"知识库中没有找到与「{query.strip()}」匹配的文章。"

    lines = [f"知识库检索结果（关键词：{' '.join(terms)}）"]
    for index, (entry, _score) in enumerate(hits, start=1):
        lines.append(
            f"{index}. [{entry.id}] {entry.title}\n"
            f"   {_truncate(entry.summary, KNOWLEDGE_SUMMARY_CHARS)}\n"
            f"   {entry.url}"
        )
    return "\n".join(lines)


def handle_general_chat(query: str) -> str:
    """Answer a general question with the LLM.

    Args:
        query: User question.

    Returns:
        The model answer, or an error message when the call fails.
    """
    try:
        text, usage = chat(
            query,
            system="你是简洁、准确的 AI 技术助手，回答控制在 300 字以内。",
            max_tokens=800,
        )
    except (LLMError, ValueError) as exc:
        LOGGER.error("LLM 回答失败: %s", exc)
        return f"抱歉，回答失败：{exc}"

    LOGGER.info("general_chat 使用 tokens=%d", usage.total_tokens)
    return text


HANDLERS: Mapping[str, Callable[[str], str]] = {
    INTENT_GITHUB: handle_github_search,
    INTENT_KNOWLEDGE: handle_knowledge_query,
    INTENT_CHAT: handle_general_chat,
}


def route(query: str) -> str:
    """Classify a query and dispatch it to the matching handler.

    Args:
        query: User query.

    Returns:
        The handler result as plain text.

    Raises:
        ValueError: If ``query`` is empty.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 不能为空")

    intent = classify_intent(query)
    handler = HANDLERS[intent]
    LOGGER.info("路由 intent=%s handler=%s", intent, handler.__name__)
    return handler(query)


def _run_demo(queries: Sequence[str]) -> int:
    """Run the demo queries and write results to stdout."""
    for query in queries:
        sys.stdout.write(f"\n=== 查询: {query}\n")
        try:
            answer = route(query)
        except (LLMError, ValueError, OSError) as exc:
            LOGGER.error("查询失败: %s", exc)
            sys.stdout.write(f"失败：{exc}\n")
            continue
        sys.stdout.write(f"{answer}\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line demo.

    Args:
        argv: Query list; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    queries = list(sys.argv[1:] if argv is None else argv)
    if not queries:
        queries = [
            "搜索 GitHub 上 star 最多的 RAG 框架",
            "知识库里有关于 MCP 的文章吗？",
            "用一句话解释什么是 RAG",
        ]
    return _run_demo(queries)


if __name__ == "__main__":
    raise SystemExit(main())
