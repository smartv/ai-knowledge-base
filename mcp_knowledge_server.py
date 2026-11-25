#!/usr/bin/env python3
"""本地知识库 MCP Server。

通过 JSON-RPC 2.0 over stdio 暴露 ``knowledge/articles/`` 下的知识条目，
供 Claude Desktop、OpenCode 等支持 MCP 的客户端调用。仅使用 Python 标准库，
不需要安装任何第三方依赖。

协议约定：

* 每个 JSON-RPC 消息占一行，UTF-8 编码，消息内不允许出现裸换行；
* stdout 只输出协议消息，日志一律写 stderr；
* 支持 ``initialize``、``ping``、``tools/list``、``tools/call``；
* 支持 JSON-RPC 批量请求，通知消息不返回响应。

典型接入配置（以 stdio 客户端为例）::

    {
      "mcpServers": {
        "knowledge-base": {
          "command": "python3",
          "args": ["/path/to/mcp_knowledge_server.py"]
        }
      }
    }

直接运行::

    python3 mcp_knowledge_server.py --knowledge-dir knowledge --verbose
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

LOGGER = logging.getLogger("mcp-knowledge")

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"
ARTICLES_DIRNAME = "articles"

SERVER_NAME = "ai-knowledge-base"
SERVER_VERSION = "1.0.0"

# 按从新到旧排列。客户端请求的版本在列表内则原样返回，否则返回最新版本。
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

DEFAULT_LIMIT = 5
MAX_LIMIT = 50
MAX_KEYWORD_LENGTH = 200
TOP_TAGS = 10

# JSON-RPC 2.0 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

NOTIFICATION_METHODS = frozenset(
    {
        "notifications/initialized",
        "notifications/cancelled",
        "notifications/progress",
        "notifications/roots/list_changed",
    }
)

INSTRUCTIONS = (
    "本地 AI 技术知识库。先用 search_articles 按关键词检索候选条目，"
    "再用 get_article 读取完整内容；knowledge_stats 可查看总量、"
    "来源分布与热门标签。"
)

Response = dict[str, Any]

TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "search_articles",
        "description": (
            "按关键词检索本地知识库文章，同时匹配标题、摘要与标签。"
            "多个关键词用空格分隔时要求全部命中。"
            "返回按相关度排序的摘要级结果，可用 get_article 读取全文。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词，例如 \"agent\" 或 \"llm 推理\"",
                    "minLength": 1,
                    "maxLength": MAX_KEYWORD_LENGTH,
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回条数，默认 5",
                    "minimum": 1,
                    "maximum": MAX_LIMIT,
                    "default": DEFAULT_LIMIT,
                },
            },
            "required": ["keyword"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_article",
        "description": (
            "按文章 ID 获取完整知识条目，包含摘要、要点、标签、评分、"
            "来源链接等字段。可先调用 search_articles 获取 ID。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "article_id": {
                    "type": "string",
                    "description": "文章 ID，例如 kb-20261125-0001 或文件名主干",
                    "minLength": 1,
                }
            },
            "required": ["article_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "knowledge_stats",
        "description": (
            "返回知识库统计信息：文章总数、来源分布、状态分布、"
            "分类分布与热门标签。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
)

TOOL_NAMES = frozenset(tool["name"] for tool in TOOL_DEFINITIONS)


class ToolRequestError(ValueError):
    """Raised when a tool receives invalid arguments."""


@dataclass(frozen=True)
class Article:
    """One knowledge entry loaded from the articles directory.

    Attributes:
        id: Knowledge-entry identifier, taken from the JSON ``id`` field.
        title: Entry title.
        source: Collection source, e.g. ``github_trending``.
        summary: Chinese summary.
        tags: Tag tuple in the original order.
        score: Analysis score on a 1-10 scale, or ``None`` when absent.
        category: Entry category.
        status: Entry lifecycle status.
        source_url: Original link.
        published_at: Publication time in ISO 8601 UTC.
        collected_at: Collection time in ISO 8601 UTC.
        path: Absolute path of the backing JSON file.
        data: The complete parsed JSON object.
    """

    id: str
    title: str
    source: str
    summary: str
    tags: tuple[str, ...]
    score: float | None
    category: str
    status: str
    source_url: str
    published_at: str
    collected_at: str
    path: Path
    data: Mapping[str, Any]

    @property
    def title_key(self) -> str:
        """Return the case-folded title used for matching."""
        return self.title.casefold()

    @property
    def summary_key(self) -> str:
        """Return the case-folded summary used for matching."""
        return self.summary.casefold()

    @property
    def tag_keys(self) -> tuple[str, ...]:
        """Return case-folded tags used for matching."""
        return tuple(tag.casefold() for tag in self.tags)


def setup_logging(verbose: bool) -> None:
    """Configure stderr logging.

    Args:
        verbose: When true, emit DEBUG records.
    """
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )


def _as_text(record: Mapping[str, Any], key: str) -> str:
    """Return a stripped string field, or an empty string."""
    value = record.get(key)
    return value.strip() if isinstance(value, str) else ""


def _as_tags(record: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the string entries of the ``tags`` array."""
    value = record.get("tags")
    if not isinstance(value, list):
        return ()
    return tuple(entry.strip() for entry in value if isinstance(entry, str) and entry.strip())


def _as_score(record: Mapping[str, Any]) -> float | None:
    """Return the 1-10 score, falling back to ``relevance_score``."""
    value = record.get("score")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)

    relevance = record.get("relevance_score")
    if isinstance(relevance, (int, float)) and not isinstance(relevance, bool):
        return round(float(relevance) * 10, 2)
    return None


def _format_score(score: float | None) -> float | int | None:
    """Render an integral score without a trailing ``.0``."""
    if score is None:
        return None
    if float(score).is_integer():
        return int(score)
    return round(float(score), 2)


def load_article(path: Path) -> Article | None:
    """Load one knowledge entry from disk.

    Malformed files are reported on stderr and skipped so a single bad file
    cannot take down the whole server.

    Args:
        path: JSON file under ``knowledge/articles/``.

    Returns:
        A parsed article, or ``None`` when the file is unreadable or invalid.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        LOGGER.warning("跳过无法读取的文件 %s: %s", path, exc)
        return None

    try:
        record = json.loads(text)
    except json.JSONDecodeError as exc:
        LOGGER.warning("跳过 JSON 解析失败的文件 %s: %s", path, exc)
        return None

    if not isinstance(record, dict):
        LOGGER.warning("跳过非对象 JSON 文件 %s", path)
        return None

    article_id = _as_text(record, "id") or path.stem
    return Article(
        id=article_id,
        title=_as_text(record, "title") or article_id,
        source=_as_text(record, "source") or "unknown",
        summary=_as_text(record, "summary"),
        tags=_as_tags(record),
        score=_as_score(record),
        category=_as_text(record, "category"),
        status=_as_text(record, "status") or "unknown",
        source_url=_as_text(record, "source_url"),
        published_at=_as_text(record, "published_at"),
        collected_at=_as_text(record, "collected_at"),
        path=path,
        data=record,
    )


def _tokenize(keyword: str) -> list[str]:
    """Split a keyword string into unique case-folded tokens."""
    return list(
        dict.fromkeys(
            token for token in re.split(r"\s+", keyword.casefold()) if token
        )
    )


def _match_fields(
    article: Article,
    tokens: Sequence[str],
) -> tuple[list[str], int] | None:
    """Match every token against an article.

    Args:
        article: Article to inspect.
        tokens: Case-folded tokens; all of them must match.

    Returns:
        ``(matched_fields, relevance)``, or ``None`` when any token misses.
        Title hits weigh most, tag hits second, summary hits least.
    """
    fields: list[str] = []
    relevance = 0

    for token in tokens:
        title_hits = article.title_key.count(token)
        summary_hits = article.summary_key.count(token)
        tag_hits = sum(1 for tag in article.tag_keys if token in tag)
        if not (title_hits or summary_hits or tag_hits):
            return None

        if title_hits:
            fields.append("title")
        if summary_hits:
            fields.append("summary")
        if tag_hits:
            fields.append("tags")
        relevance += title_hits * 3 + tag_hits * 2 + summary_hits

    return sorted(set(fields)), relevance


class KnowledgeBase:
    """In-memory view of the articles directory with mtime-based caching."""

    def __init__(self, knowledge_dir: Path) -> None:
        """Initialize the knowledge base.

        Args:
            knowledge_dir: Root directory that contains ``articles/``.
        """
        self.knowledge_dir = knowledge_dir
        self.articles_dir = knowledge_dir / ARTICLES_DIRNAME
        self._cache: dict[Path, tuple[int, int, Article | None]] = {}
        self._missing_warned = False

    def refresh(self) -> list[Article]:
        """Reload changed files and return all articles.

        Only files whose mtime or size changed are parsed again, so repeated
        tool calls stay cheap even when the knowledge base grows.

        Returns:
            Articles sorted by publication time descending, then by ID.
        """
        files = self._collect_files()
        if not files and not self._missing_warned:
            LOGGER.warning("未找到文章目录或目录为空: %s", self.articles_dir)
            self._missing_warned = True

        for stale in set(self._cache) - set(files):
            del self._cache[stale]

        articles: list[Article] = []
        by_id: dict[str, Article] = {}
        for path in files:
            article = self._load_cached(path)
            if article is None:
                continue
            if article.id in by_id:
                LOGGER.warning(
                    "重复的文章 ID %s，已忽略 %s", article.id, path
                )
                continue
            by_id[article.id] = article
            articles.append(article)

        articles.sort(key=lambda item: (item.published_at, item.id), reverse=True)
        LOGGER.debug("知识库刷新完成: %d 篇", len(articles))
        return articles

    def _collect_files(self) -> list[Path]:
        """Return every JSON file under the articles directory."""
        if not self.articles_dir.is_dir():
            return []
        return sorted(
            path
            for path in self.articles_dir.rglob("*.json")
            if path.is_file()
        )

    def _load_cached(self, path: Path) -> Article | None:
        """Load an article, reusing the cached copy when unchanged."""
        try:
            stat = path.stat()
        except OSError as exc:
            LOGGER.warning("无法读取文件状态 %s: %s", path, exc)
            return None

        cached = self._cache.get(path)
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]

        article = load_article(path)
        self._cache[path] = (stat.st_mtime_ns, stat.st_size, article)
        return article

    def relative_path(self, article: Article) -> str:
        """Return the article path relative to the knowledge directory."""
        try:
            return article.path.relative_to(self.knowledge_dir).as_posix()
        except ValueError:
            return article.path.as_posix()

    def search(self, keyword: str, limit: int) -> tuple[int, list[tuple[Article, list[str]]]]:
        """Search titles, summaries and tags for every keyword token.

        Args:
            keyword: Whitespace-separated keywords; all tokens must match.
            limit: Maximum number of results.

        Returns:
            A tuple of ``(total_matches, hits)`` where each hit pairs an
            article with the fields it matched (``title`` / ``summary`` /
            ``tags``).
        """
        tokens = _tokenize(keyword)
        if not tokens:
            return 0, []

        matched: list[tuple[Article, list[str], int]] = []
        for article in self.refresh():
            outcome = _match_fields(article, tokens)
            if outcome is None:
                continue
            fields, relevance = outcome
            matched.append((article, fields, relevance))

        matched.sort(
            key=lambda entry: (
                -entry[2],
                -(entry[0].score or 0.0),
                entry[0].published_at,
                entry[0].id,
            )
        )
        hits = [(article, fields) for article, fields, _ in matched[:limit]]
        return len(matched), hits

    def get(self, article_id: str) -> Article | None:
        """Look up an article by ID, file stem, or upstream source ID."""
        wanted = article_id.strip()
        if wanted.casefold().endswith(".json"):
            wanted = wanted[:-5]
        if not wanted:
            return None

        articles = self.refresh()
        lowered = wanted.casefold()

        for article in articles:
            if article.id == wanted:
                return article
        for article in articles:
            if article.id.casefold() == lowered:
                return article
        for article in articles:
            if article.path.stem.casefold() == lowered:
                return article

        for article in articles:
            source_id = article.data.get("source_id")
            if isinstance(source_id, str) and source_id.casefold() == lowered:
                return article
        return None

    def stats(self) -> dict[str, Any]:
        """Build a statistics summary of the knowledge base.

        Returns:
            Counts by source, status and category, plus top tags, score
            distribution and the covered date range.
        """
        articles = self.refresh()
        sources = Counter(article.source for article in articles)
        statuses = Counter(article.status for article in articles)
        categories = Counter(
            article.category for article in articles if article.category
        )
        tags = Counter(tag for article in articles for tag in article.tags)
        scores = [
            article.score for article in articles if article.score is not None
        ]
        timestamps = sorted(
            article.published_at or article.collected_at
            for article in articles
            if article.published_at or article.collected_at
        )

        return {
            "total_articles": len(articles),
            "by_source": dict(sources.most_common()),
            "by_status": dict(statuses.most_common()),
            "by_category": dict(categories.most_common()),
            "top_tags": [
                {"tag": tag, "count": count}
                for tag, count in tags.most_common(TOP_TAGS)
            ],
            "unique_tags": len(tags),
            "score": {
                "count": len(scores),
                "average": (
                    round(sum(scores) / len(scores), 2) if scores else None
                ),
                "min": min(scores) if scores else None,
                "max": max(scores) if scores else None,
            },
            "date_range": {
                "earliest": timestamps[0] if timestamps else None,
                "latest": timestamps[-1] if timestamps else None,
            },
            "articles_dir": str(self.articles_dir),
        }


def _error(
    request_id: Any,
    code: int,
    message: str,
    data: Any = None,
) -> Response:
    """Build a JSON-RPC error response."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _result(request_id: Any, result: Mapping[str, Any]) -> Response:
    """Build a JSON-RPC success response."""
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def _tool_text(payload: Any, is_error: bool = False) -> Response:
    """Wrap a tool payload in an MCP ``tools/call`` result."""
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def _parse_limit(arguments: Mapping[str, Any]) -> int:
    """Validate and clamp the ``limit`` argument."""
    value = arguments.get("limit", DEFAULT_LIMIT)
    if value is None:
        return DEFAULT_LIMIT
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolRequestError("limit 必须是整数")
    if float(value) != int(value):
        raise ToolRequestError("limit 必须是整数")
    return max(1, min(MAX_LIMIT, int(value)))


def _require_text(arguments: Mapping[str, Any], key: str) -> str:
    """Return a required non-empty string argument."""
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolRequestError(f"缺少必填参数 {key}（非空字符串）")
    return value.strip()


class KnowledgeServer:
    """MCP request handler backed by a :class:`KnowledgeBase`."""

    def __init__(self, knowledge_dir: Path) -> None:
        """Initialize the server.

        Args:
            knowledge_dir: Root directory that contains ``articles/``.
        """
        self.kb = KnowledgeBase(knowledge_dir)

    def handle_request(self, payload: Any) -> Response | None:
        """Handle one JSON-RPC message.

        Args:
            payload: Decoded JSON value from stdin.

        Returns:
            A response object, or ``None`` for notifications.
        """
        if not isinstance(payload, dict):
            return _error(None, INVALID_REQUEST, "请求必须是 JSON 对象")

        request_id = payload.get("id")
        is_notification = "id" not in payload
        method = payload.get("method")

        if payload.get("jsonrpc") != "2.0" or not isinstance(method, str):
            if is_notification:
                LOGGER.warning("忽略无效通知: %s", payload)
                return None
            return _error(request_id, INVALID_REQUEST, "无效的 JSON-RPC 2.0 请求")

        LOGGER.debug("收到请求: method=%s id=%r", method, request_id)

        if is_notification:
            if method not in NOTIFICATION_METHODS:
                LOGGER.debug("忽略未处理的通知: %s", method)
            return None

        if method == "initialize":
            return _result(request_id, self._initialize(payload.get("params")))
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _result(
                request_id,
                {"tools": [dict(tool) for tool in TOOL_DEFINITIONS]},
            )
        if method == "tools/call":
            return self._call_tool(request_id, payload.get("params"))

        return _error(request_id, METHOD_NOT_FOUND, f"未知方法: {method}")

    def _initialize(self, params: Any) -> dict[str, Any]:
        """Negotiate the protocol version and advertise capabilities."""
        requested: Any = None
        client_name = "unknown"
        if isinstance(params, dict):
            requested = params.get("protocolVersion")
            client_info = params.get("clientInfo")
            if isinstance(client_info, dict):
                name = client_info.get("name")
                if isinstance(name, str) and name:
                    client_name = name

        version = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION
        )
        LOGGER.info(
            "initialize: client=%s requested=%s negotiated=%s",
            client_name,
            requested,
            version,
        )
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        }

    def _call_tool(self, request_id: Any, params: Any) -> Response:
        """Validate ``tools/call`` params and dispatch to a tool."""
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "params 必须是对象")

        name = params.get("name")
        if not isinstance(name, str) or not name:
            return _error(request_id, INVALID_PARAMS, "缺少工具名 name")

        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _error(request_id, INVALID_PARAMS, "arguments 必须是对象")

        if name not in TOOL_NAMES:
            return _error(
                request_id,
                INVALID_PARAMS,
                f"未知工具: {name}",
                {"available_tools": sorted(TOOL_NAMES)},
            )

        try:
            payload = self.dispatch_tool(name, arguments)
        except ToolRequestError as exc:
            LOGGER.info("工具参数错误: %s: %s", name, exc)
            return _result(request_id, _tool_text({"error": str(exc)}, True))

        LOGGER.info("工具调用: %s", name)
        return _result(request_id, _tool_text(payload))

    def dispatch_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        """Run a tool and return its structured payload.

        Args:
            name: Tool name, already validated.
            arguments: Raw arguments from the client.

        Returns:
            A JSON-serializable payload.

        Raises:
            ToolRequestError: If the arguments are invalid.
        """
        if name == "search_articles":
            return self._search(arguments)
        if name == "get_article":
            return self._get_article(arguments)
        return self._stats(arguments)

    def _search(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Implement the ``search_articles`` tool."""
        keyword = _require_text(arguments, "keyword")
        if len(keyword) > MAX_KEYWORD_LENGTH:
            raise ToolRequestError(
                f"keyword 过长（最多 {MAX_KEYWORD_LENGTH} 字符）"
            )
        limit = _parse_limit(arguments)

        total, hits = self.kb.search(keyword, limit)
        return {
            "keyword": keyword,
            "limit": limit,
            "total_matches": total,
            "returned": len(hits),
            "results": [
                self._search_hit(article, fields) for article, fields in hits
            ],
        }

    def _search_hit(
        self,
        article: Article,
        fields: Sequence[str],
    ) -> dict[str, Any]:
        """Render one search result."""
        return {
            "id": article.id,
            "title": article.title,
            "source": article.source,
            "category": article.category,
            "status": article.status,
            "score": _format_score(article.score),
            "tags": list(article.tags),
            "published_at": article.published_at or None,
            "source_url": article.source_url or None,
            "summary": article.summary,
            "matched_in": list(fields),
            "path": self.kb.relative_path(article),
        }

    def _get_article(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Implement the ``get_article`` tool."""
        article_id = _require_text(arguments, "article_id")
        article = self.kb.get(article_id)
        if article is None:
            total = len(self.kb.refresh())
            raise ToolRequestError(
                f"未找到文章 {article_id!r}（当前共 {total} 篇，"
                "可先用 search_articles 搜索）"
            )
        return {
            "path": self.kb.relative_path(article),
            "article": article.data,
        }

    def _stats(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Implement the ``knowledge_stats`` tool."""
        del arguments  # 统计工具不接受参数
        return self.kb.stats()


def _send(message: Any) -> None:
    """Write one JSON-RPC message to stdout."""
    sys.stdout.write(
        json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    sys.stdout.flush()


def _dispatch_payload(server: KnowledgeServer, payload: Any) -> None:
    """Handle a single message or a JSON-RPC batch."""
    if isinstance(payload, list):
        if not payload:
            _send(_error(None, INVALID_REQUEST, "空的批量请求"))
            return
        responses = [
            response
            for item in payload
            if (response := server.handle_request(item)) is not None
        ]
        if responses:
            _send(responses)
        return

    response = server.handle_request(payload)
    if response is not None:
        _send(response)


def _handle_exception(request_id: Any, exc: Exception) -> None:
    """Log an unexpected failure and report it as an internal error."""
    LOGGER.exception("处理请求时发生未预期异常")
    _send(_error(request_id, INTERNAL_ERROR, f"服务器内部错误: {exc}"))


def _serve_loop(server: KnowledgeServer) -> int:
    """Read messages from stdin and dispatch them until EOF.

    Args:
        server: Initialized request handler.

    Returns:
        Process exit code.
    """
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                LOGGER.warning("JSON 解析失败: %s", exc)
                _send(_error(None, PARSE_ERROR, f"JSON 解析失败: {exc.msg}"))
                continue

            try:
                _dispatch_payload(server, payload)
            except BrokenPipeError:
                raise
            except Exception as exc:  # noqa: BLE001 - 顶层兜底必须记录并回复
                request_id = payload.get("id") if isinstance(payload, dict) else None
                _handle_exception(request_id, exc)
    except KeyboardInterrupt:
        LOGGER.info("收到中断信号，退出")
        return 0
    except BrokenPipeError:
        LOGGER.info("客户端已关闭连接，退出")
        return 0

    LOGGER.info("stdin 已关闭，退出")
    return 0


def serve(knowledge_dir: Path) -> int:
    """Run the stdio server until stdin closes.

    Args:
        knowledge_dir: Root directory that contains ``articles/``.

    Returns:
        Process exit code.
    """
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        LOGGER.debug("当前标准流不支持 reconfigure，沿用默认编码")

    server = KnowledgeServer(knowledge_dir)
    total = len(server.kb.refresh())
    LOGGER.info(
        "MCP 服务就绪: %s（%d 篇文章），协议版本 %s",
        server.kb.articles_dir,
        total,
        LATEST_PROTOCOL_VERSION,
    )
    return _serve_loop(server)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        Parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="mcp_knowledge_server",
        description="本地知识库 MCP Server（JSON-RPC 2.0 over stdio）。",
        epilog=(
            "示例: python3 mcp_knowledge_server.py "
            "--knowledge-dir knowledge --verbose"
        ),
    )
    parser.add_argument(
        "--knowledge-dir",
        type=Path,
        default=DEFAULT_KNOWLEDGE_DIR,
        metavar="DIR",
        help="知识库根目录，需包含 articles/（默认项目下的 knowledge/）",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="向 stderr 输出 DEBUG 日志",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    args = parse_args(argv)
    setup_logging(args.verbose)

    knowledge_dir = args.knowledge_dir.expanduser()
    if not knowledge_dir.is_dir():
        LOGGER.error("知识库目录不存在: %s", knowledge_dir)
        return 1

    return serve(knowledge_dir)


if __name__ == "__main__":
    raise SystemExit(main())
