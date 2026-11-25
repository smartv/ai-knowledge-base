#!/usr/bin/env python3
"""Run the four-stage AI knowledge-base pipeline.

Stages:
    1. Collect  - GitHub Search API plus curated AI RSS feeds.
    2. Analyze  - one LLM call per item for summary, score, tags, category.
    3. Organize - deduplicate, normalize and validate the analyses.
    4. Save     - write append-only raw snapshots and article JSON files.

Usage:
    python pipeline/pipeline.py --sources github,rss --limit 20
    python pipeline/pipeline.py --sources github --limit 5
    python pipeline/pipeline.py --sources rss --limit 10
    python pipeline/pipeline.py --sources github --limit 5 --dry-run
    python pipeline/pipeline.py --verbose

Environment:
    LLM_PROVIDER / DEEPSEEK_API_KEY / QWEN_API_KEY / OPENAI_API_KEY:
        Consumed by ``model_client.create_provider``.
    GITHUB_TOKEN:
        Optional but recommended; raises the GitHub API rate limit.

Layout:
    knowledge/raw/YYYY-MM-DD/{feed}-{timestamp}.json   append-only snapshots
    knowledge/articles/YYYY-MM-DD/{id}.json            validated entries
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

try:
    import httpx
except ModuleNotFoundError as exc:  # pragma: no cover - import guard
    raise ModuleNotFoundError(
        "pipeline/pipeline.py 需要 httpx，请先运行：pip install httpx"
    ) from exc

try:
    from model_client import (
        LLMError,
        LLMProvider,
        chat_with_retry,
        create_provider,
    )
except ModuleNotFoundError:  # 以 python -m pipeline.pipeline 方式运行时走包内导入
    from pipeline.model_client import (  # type: ignore[no-redef]
        LLMError,
        LLMProvider,
        chat_with_retry,
        create_provider,
    )

LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"

USER_AGENT = "ai-knowledge-base/0.1"
HTTP_TIMEOUT_SECONDS = 30.0
HTTP_MAX_ATTEMPTS = 3
RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

GITHUB_API_URL = "https://api.github.com/search/repositories"
GITHUB_TOPICS = ("llm", "ai-agents", "rag", "llmops")
GITHUB_MIN_STARS = 50
GITHUB_ACTIVE_DAYS = 180

SUMMARY_MIN_LENGTH = 50
SUMMARY_FULL_LENGTH = 150
SCORE_MIN = 1
SCORE_MAX = 10
SCORE_ACCEPT_THRESHOLD = 6
MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_PROMPT = 1200
MAX_TAGS = 6

KB_ID_RE = re.compile(r"^kb-(?P<date>\d{8})-(?P<seq>\d{4})$")
RSS_ITEM_RE = re.compile(r"<item\b[^>]*>(.*?)</item>", re.DOTALL | re.IGNORECASE)
ATOM_ENTRY_RE = re.compile(r"<entry\b[^>]*>(.*?)</entry>", re.DOTALL | re.IGNORECASE)
CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

CATEGORIES = frozenset(
    {"model", "framework", "tool", "paper", "opinion", "dataset"}
)

# ``rss`` extends the AGENTS.md v1 source enum so non-Hacker-News feeds can be
# attributed honestly instead of being mislabeled as ``hacker_news``.
ALLOWED_SOURCES = frozenset({"github_trending", "hacker_news", "rss"})

ALLOWED_TAGS = frozenset(
    {
        "agent",
        "agent-orchestration",
        "agent-runtime",
        "agents",
        "ai",
        "ai-safety",
        "alignment",
        "anthropic",
        "api",
        "automation",
        "benchmark",
        "chatbot",
        "checkpointing",
        "cli",
        "coding-assistant",
        "context-engineering",
        "copilot",
        "data",
        "dataset",
        "deep-learning",
        "deepseek",
        "deployment",
        "diffusion",
        "embedding",
        "embeddings",
        "evaluation",
        "fine-tuning",
        "framework",
        "function-calling",
        "gemini",
        "gpu",
        "huggingface",
        "inference",
        "infrastructure",
        "knowledge-base",
        "langchain",
        "langgraph",
        "llama",
        "llm",
        "llms",
        "mcp",
        "memory",
        "mistral",
        "model",
        "models",
        "multimodal",
        "nlp",
        "observability",
        "open-source",
        "openai",
        "opinion",
        "orchestration",
        "paper",
        "product",
        "prompt-engineering",
        "qwen",
        "quantization",
        "rag",
        "reasoning",
        "research",
        "retrieval",
        "sdk",
        "serving",
        "speech",
        "text-to-image",
        "tool",
        "tool-use",
        "training",
        "transformer",
        "vector-database",
        "vision",
        "workflow",
    }
)

SYSTEM_PROMPT = (
    "你是严谨的 AI 技术情报分析员。你只能根据用户提供的标题与描述撰写摘要，"
    "禁止补充原文中不存在的事实、数据、引述或发布时间。"
    "输出必须是单个 JSON 对象，不要 Markdown 代码块，不要任何额外解释。"
)


@dataclass(frozen=True)
class RssFeed:
    """Configuration for one RSS feed.

    Attributes:
        name: Short feed identifier used in logs and raw file names.
        url: Feed URL.
        source: Knowledge-entry source value for items from this feed.
    """

    name: str
    url: str
    source: str


RSS_FEEDS: tuple[RssFeed, ...] = (
    RssFeed(
        name="hacker-news",
        url="https://hnrss.org/newest?q=AI+OR+LLM+OR+agent",
        source="hacker_news",
    ),
    RssFeed(
        name="hugging-face",
        url="https://huggingface.co/blog/feed.xml",
        source="rss",
    ),
    RssFeed(
        name="arxiv-cs-ai",
        url="https://rss.arxiv.org/rss/cs.AI",
        source="rss",
    ),
)

SOURCE_CHOICES = ("github", "rss")


@dataclass(frozen=True)
class RawItem:
    """One collected item before LLM analysis.

    Attributes:
        feed: Feed identifier, e.g. ``github`` or ``hacker-news``.
        source: Knowledge-entry source value.
        source_id: Upstream identifier (repo full name, guid, or link).
        title: Original title.
        url: Original link.
        author: Repository owner, feed creator, or feed name.
        description: Plain-text description used as LLM input.
        published_at: ISO 8601 UTC publish/update time, if known.
        collected_at: ISO 8601 UTC collection time.
        popularity: GitHub stars or Hacker News points; zero when unknown.
        language: Detected original language, ``zh`` or ``en``.
        extra: Source-specific structured metadata.
    """

    feed: str
    source: str
    source_id: str
    title: str
    url: str
    author: str
    description: str
    published_at: str | None
    collected_at: str
    popularity: int = 0
    language: str = "en"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Analysis:
    """LLM analysis for one raw item.

    Attributes:
        summary: Chinese summary.
        key_points: Two to five concrete highlights.
        score: Integer relevance score from 1 to 10.
        category: One of the supported category values.
        tags: Normalized kebab-case tags.
    """

    summary: str
    key_points: tuple[str, ...]
    score: int
    category: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class AnalyzedItem:
    """A raw item paired with its analysis."""

    raw: RawItem
    analysis: Analysis


@dataclass(frozen=True)
class ExistingIndex:
    """Keys used to detect duplicates against already-saved entries.

    Attributes:
        urls: Canonical source URLs.
        source_ids: Upstream identifiers.
        titles: Normalized titles.
    """

    urls: frozenset[str]
    source_ids: frozenset[str]
    titles: frozenset[str]


@dataclass
class PipelineStats:
    """Counters collected while the pipeline runs."""

    collected: int = 0
    analyzed: int = 0
    analysis_failed: int = 0
    duplicates: int = 0
    invalid: int = 0
    saved: int = 0
    rejected: int = 0


@dataclass(frozen=True)
class PipelineConfig:
    """Runtime configuration for the pipeline.

    Attributes:
        sources: Collection sources, any combination of ``github`` and ``rss``.
        limit: Maximum items per source.
        dry_run: Collect and report only; no LLM calls and no writes.
        knowledge_dir: Root directory containing ``raw/`` and ``articles/``.
    """

    sources: tuple[str, ...] = SOURCE_CHOICES
    limit: int = 20
    dry_run: bool = False
    knowledge_dir: Path = DEFAULT_KNOWLEDGE_DIR

    def __post_init__(self) -> None:
        """Validate configuration values."""
        if not self.sources:
            raise ValueError("sources 不能为空")
        unknown = [name for name in self.sources if name not in SOURCE_CHOICES]
        if unknown:
            raise ValueError(f"未知 sources: {', '.join(unknown)}")
        if self.limit <= 0:
            raise ValueError("limit 必须大于 0")

    @property
    def project_root(self) -> Path:
        """Return the project root that relative paths are based on."""
        return self.knowledge_dir.resolve().parent


class CollectionError(RuntimeError):
    """Raised when an HTTP collection request fails."""


class AnalysisError(RuntimeError):
    """Raised when an LLM response cannot be turned into a valid analysis."""


class PipelineError(RuntimeError):
    """Raised when the pipeline cannot continue."""


def setup_logging(verbose: bool) -> None:
    """Configure root logging for CLI usage.

    Args:
        verbose: Enable DEBUG level when true.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def iso_utc(moment: datetime) -> str:
    """Format an aware datetime as ``YYYY-MM-DDTHH:MM:SSZ``."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_iso_timestamp(value: Any) -> str | None:
    """Normalize an ISO 8601 timestamp to UTC, returning ``None`` if invalid."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return iso_utc(moment)


def parse_feed_timestamp(value: str) -> str | None:
    """Parse an RSS/Atom timestamp into ISO 8601 UTC.

    Args:
        value: RFC 822 or ISO 8601 timestamp text.

    Returns:
        ISO 8601 UTC string, or ``None`` when the value cannot be parsed.
    """
    text = value.strip()
    if not text:
        return None

    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return normalize_iso_timestamp(text)

    if moment is None:
        return None
    return iso_utc(moment)


def detect_language(text: str) -> str:
    """Detect whether text is mainly Chinese or English.

    Args:
        text: Title plus description.

    Returns:
        ``"zh"`` when CJK characters are a meaningful share of the text,
        otherwise ``"en"``.
    """
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    latin = sum(1 for char in text if char.isascii() and char.isalpha())
    if cjk == 0:
        return "en"
    return "zh" if cjk * 5 >= latin else "en"


def canonical_url(value: str) -> str:
    """Strip tracking parameters and normalize a URL for de-duplication.

    Args:
        value: Raw URL.

    Returns:
        Canonical URL, or the stripped original when it is not a valid
        absolute HTTP(S) URL.
    """
    text = value.strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return text

    tracking = {"ref", "source", "fbclid", "gclid", "mc_cid", "mc_eid"}
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in tracking
    ]
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parsed.path.rstrip("/") or "/"

    return urlunparse(
        (parsed.scheme.lower(), netloc, path, "", urlencode(query), "")
    )


def normalize_title(value: str) -> str:
    """Normalize a title for duplicate detection."""
    lowered = value.strip().lower()
    cleaned = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def is_http_url(value: str) -> bool:
    """Return whether a string is an absolute HTTP(S) URL without spaces."""
    if any(char.isspace() for char in value):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Return the delay before the next retry."""
    header = response.headers.get("Retry-After", "").strip()
    if header.isdigit():
        return float(min(10, max(1, int(header))))
    return float(min(10, 2 ** (attempt - 1)))


def _get_with_retry(
    client: httpx.Client,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """Send a GET request with retries for transient failures.

    Args:
        client: Shared HTTP client.
        url: Request URL.
        params: Optional query parameters.
        headers: Optional extra request headers.

    Returns:
        A successful HTTP response.

    Raises:
        CollectionError: If the request still fails after all attempts.
    """
    last_error = ""
    for attempt in range(1, HTTP_MAX_ATTEMPTS + 1):
        try:
            response = client.get(url, params=params, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = f"网络错误: {exc}"
            if attempt >= HTTP_MAX_ATTEMPTS:
                break
            delay = float(2 ** (attempt - 1))
            LOGGER.warning("%s；%.0f 秒后重试", last_error, delay)
            time.sleep(delay)
            continue

        if response.status_code in RETRYABLE_STATUS_CODES:
            if attempt >= HTTP_MAX_ATTEMPTS:
                last_error = f"HTTP {response.status_code}"
                break
            delay = _retry_delay(response, attempt)
            LOGGER.warning(
                "HTTP %d，%.0f 秒后重试（%d/%d）",
                response.status_code,
                delay,
                attempt,
                HTTP_MAX_ATTEMPTS,
            )
            time.sleep(delay)
            continue

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise CollectionError(
                f"HTTP {response.status_code}: {response.text[:200]}"
            ) from exc

        return response

    raise CollectionError(f"请求失败（{url}）：{last_error}")


def _get_json(
    client: httpx.Client,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> Mapping[str, Any]:
    """Fetch and decode a JSON object."""
    response = _get_with_retry(client, url, params=params, headers=headers)
    try:
        data = response.json()
    except ValueError as exc:
        raise CollectionError(f"响应不是合法 JSON（{url}）") from exc
    if not isinstance(data, dict):
        raise CollectionError(f"响应顶层不是 JSON 对象（{url}）")
    return data


def _get_text(client: httpx.Client, url: str) -> str:
    """Fetch a text response."""
    response = _get_with_retry(client, url)
    return response.text


def _github_headers() -> dict[str, str]:
    """Build GitHub API headers, including an optional token."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _github_topics(repo: Mapping[str, Any]) -> list[str]:
    """Return the string topics attached to a GitHub repository."""
    value = repo.get("topics")
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, str)]


def _github_author(repo: Mapping[str, Any]) -> str:
    """Return the repository owner login, or an empty string."""
    owner = repo.get("owner")
    if isinstance(owner, dict) and isinstance(owner.get("login"), str):
        return owner["login"]
    return ""


def _github_description(
    repo: Mapping[str, Any],
    topic: str,
    stars: int,
    language: str,
    topics: Sequence[str],
) -> str:
    """Build the plain-text GitHub description used as LLM input."""
    base = repo.get("description")
    base = base.strip() if isinstance(base, str) else ""
    meta = (
        f"Topics: {', '.join(topics) or 'none'}; "
        f"Stars: {stars}; Language: {language}; Topic query: {topic}"
    )
    return f"{base}\n{meta}".strip()


def _github_item(repo: Any, topic: str, collected_at: str) -> RawItem | None:
    """Convert one GitHub search result into a raw item.

    Args:
        repo: One element from the search response ``items`` array.
        topic: Topic that produced this result.
        collected_at: Collection timestamp.

    Returns:
        A ``RawItem``, or ``None`` when required fields are missing.
    """
    if not isinstance(repo, dict):
        return None

    full_name = repo.get("full_name")
    html_url = repo.get("html_url")
    if not isinstance(full_name, str) or not full_name.strip():
        return None
    if not isinstance(html_url, str) or not is_http_url(html_url):
        return None

    stars_value = repo.get("stargazers_count")
    stars = (
        stars_value
        if isinstance(stars_value, int) and not isinstance(stars_value, bool)
        else 0
    )
    language = repo.get("language")
    language = language if isinstance(language, str) else "unknown"
    topics = _github_topics(repo)
    description = _github_description(repo, topic, stars, language, topics)

    published = normalize_iso_timestamp(
        repo.get("pushed_at") or repo.get("updated_at") or repo.get("created_at")
    )

    return RawItem(
        feed="github",
        source="github_trending",
        source_id=full_name,
        title=full_name,
        url=html_url,
        author=_github_author(repo),
        description=description,
        published_at=published,
        collected_at=collected_at,
        popularity=stars,
        language=detect_language(description),
        extra={
            "stars": stars,
            "forks": repo.get("forks_count", 0),
            "language": language,
            "topics": topics,
            "homepage": repo.get("homepage"),
            "query_topic": topic,
        },
    )


def collect_github(limit: int, client: httpx.Client) -> list[RawItem]:
    """Collect repositories from the GitHub Search API.

    Args:
        limit: Maximum number of repositories to return.
        client: Shared HTTP client.

    Returns:
        Raw items ordered by topic, highest-starred first.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=GITHUB_ACTIVE_DAYS)
    ).date().isoformat()
    per_topic = max(1, math.ceil(limit / len(GITHUB_TOPICS)))
    collected_at = iso_utc(datetime.now(timezone.utc))
    headers = _github_headers()

    items: list[RawItem] = []
    seen: set[str] = set()

    for topic in GITHUB_TOPICS:
        if len(items) >= limit:
            break

        params = {
            "q": f"topic:{topic} stars:>={GITHUB_MIN_STARS} pushed:>{cutoff}",
            "sort": "stars",
            "order": "desc",
            "per_page": min(100, per_topic),
        }
        try:
            payload = _get_json(
                client, GITHUB_API_URL, params=params, headers=headers
            )
        except CollectionError as exc:
            LOGGER.warning("GitHub 主题 %s 采集失败: %s", topic, exc)
            continue

        repos = payload.get("items")
        if not isinstance(repos, list):
            LOGGER.warning("GitHub 主题 %s 响应缺少 items 数组", topic)
            continue

        for repo in repos:
            if len(items) >= limit:
                break
            item = _github_item(repo, topic, collected_at)
            if item is None or item.source_id in seen:
                continue
            seen.add(item.source_id)
            items.append(item)

    LOGGER.info("GitHub 采集完成: %d 条", len(items))
    return items


def _raw_field(block: str, *names: str) -> str:
    """Return the first matching XML field body without cleaning."""
    for name in names:
        pattern = re.compile(
            rf"<{re.escape(name)}\b[^>]*>(.*?)</{re.escape(name)}>",
            re.DOTALL | re.IGNORECASE,
        )
        match = pattern.search(block)
        if match is not None:
            return match.group(1)
    return ""


def _clean_markup(value: str) -> str:
    """Strip CDATA wrappers, HTML tags and entities, then collapse whitespace."""
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", value, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _text_field(block: str, *names: str) -> str:
    """Return the first matching XML field as cleaned plain text."""
    return _clean_markup(_raw_field(block, *names))


def _extract_link(block: str) -> str:
    """Extract a link from RSS ``<link>`` or Atom ``<link href>`` markup."""
    href = re.search(
        r"<link\b[^>]*?\bhref\s*=\s*[\"']([^\"']+)[\"']",
        block,
        re.IGNORECASE,
    )
    if href is not None:
        return html.unescape(href.group(1)).strip()
    return _text_field(block, "link")


def _hn_points(description: str) -> int:
    """Read the Hacker News points value embedded in the RSS description."""
    match = re.search(r"Points:\s*(\d+)", description)
    return int(match.group(1)) if match is not None else 0


def _rss_raw_item(
    block: str,
    feed: RssFeed,
    collected_at: str,
) -> RawItem | None:
    """Convert one RSS item or Atom entry into a raw item."""
    title = _text_field(block, "title")
    link = _extract_link(block)
    if not title or not is_http_url(link):
        return None

    guid = _text_field(block, "guid", "id") or link
    raw_description = _text_field(
        block,
        "description",
        "summary",
        "content:encoded",
        "content",
    )
    if feed.name == "hacker-news":
        popularity = _hn_points(raw_description)
        description = raw_description.split("Comments URL:", 1)[0].strip()
    else:
        popularity = 0
        description = raw_description

    author = _text_field(block, "dc:creator", "author") or feed.name
    published = parse_feed_timestamp(
        _text_field(block, "pubDate", "published", "updated")
    )

    return RawItem(
        feed=feed.name,
        source=feed.source,
        source_id=guid.strip(),
        title=title,
        url=link,
        author=author,
        description=description,
        published_at=published,
        collected_at=collected_at,
        popularity=popularity,
        language=detect_language(f"{title}\n{description}"),
        extra={"feed_url": feed.url},
    )


def parse_feed(xml_text: str, feed: RssFeed, collected_at: str) -> list[RawItem]:
    """Parse RSS 2.0 items or Atom entries with simple regular expressions.

    Args:
        xml_text: Raw feed document.
        feed: Feed configuration.
        collected_at: Collection timestamp.

    Returns:
        Parsed raw items; malformed entries are skipped.
    """
    blocks = [match.group(1) for match in RSS_ITEM_RE.finditer(xml_text)]
    if not blocks:
        blocks = [match.group(1) for match in ATOM_ENTRY_RE.finditer(xml_text)]

    items: list[RawItem] = []
    for block in blocks:
        item = _rss_raw_item(block, feed, collected_at)
        if item is not None:
            items.append(item)
    return items


def collect_rss(limit: int, client: httpx.Client) -> list[RawItem]:
    """Collect items from the configured RSS feeds.

    Args:
        limit: Maximum number of items to return across all feeds.
        client: Shared HTTP client.

    Returns:
        Raw items, at most ``limit`` entries.
    """
    per_feed = max(1, math.ceil(limit / len(RSS_FEEDS)))
    collected_at = iso_utc(datetime.now(timezone.utc))

    items: list[RawItem] = []
    seen: set[str] = set()

    for feed in RSS_FEEDS:
        if len(items) >= limit:
            break

        try:
            xml_text = _get_text(client, feed.url)
        except CollectionError as exc:
            LOGGER.warning("RSS %s 采集失败: %s", feed.name, exc)
            continue

        added = 0
        for item in parse_feed(xml_text, feed, collected_at):
            if len(items) >= limit or added >= per_feed:
                break
            key = item.source_id or item.url
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
            added += 1

        LOGGER.info("RSS %s 采集完成: %d 条", feed.name, added)

    LOGGER.info("RSS 合计采集: %d 条", len(items))
    return items


def build_messages(item: RawItem) -> list[dict[str, str]]:
    """Build the chat messages used to analyze one item.

    Args:
        item: Raw item to analyze.

    Returns:
        A system message plus a user message requesting strict JSON.
    """
    description = item.description.strip()
    if len(description) > MAX_DESCRIPTION_PROMPT:
        description = description[:MAX_DESCRIPTION_PROMPT].rstrip() + "…"
    if not description:
        description = "（无描述，只能根据标题判断，禁止编造细节）"

    tags = " ".join(sorted(ALLOWED_TAGS))
    prompt = (
        "请分析下面的条目并输出一个 JSON 对象。\n\n"
        "字段要求：\n"
        "- summary：中文，150-300 字，说明是什么、解决什么问题、为什么值得关注\n"
        "- key_points：2-5 条中文要点，每条 15-50 字\n"
        "- score：1-10 的整数，宁低勿高；同一批中 9-10 分最多 2 条\n"
        "- category：model / framework / tool / paper / opinion / dataset 之一\n"
        "- tags：3 个英文小写 kebab-case 标签，只能从下方词表选择\n\n"
        f"可用标签：{tags}\n\n"
        "条目：\n"
        f"标题：{item.title}\n"
        f"来源：{item.source}\n"
        f"链接：{item.url}\n"
        f"发布时间：{item.published_at or 'unknown'}\n"
        f"作者：{item.author or 'unknown'}\n"
        f"热度：{item.popularity}\n"
        f"描述：{description}\n\n"
        '只输出 JSON，例如：{"summary": "...", "key_points": ["...", "..."], '
        '"score": 7, "category": "framework", '
        '"tags": ["llm", "agent", "open-source"]}'
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]


def _decode_json_object(text: str) -> Mapping[str, Any] | None:
    """Extract the first JSON object from model output."""
    candidates = [match.group(1) for match in CODE_FENCE_RE.finditer(text)]
    candidates.append(text)
    decoder = json.JSONDecoder()

    for candidate in candidates:
        stripped = candidate.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            start = stripped.find("{")
            if start < 0:
                continue
            try:
                data, _ = decoder.raw_decode(stripped[start:])
            except json.JSONDecodeError:
                continue
        if isinstance(data, dict):
            return data
    return None


def _normalize_tag(value: str) -> str:
    """Normalize one tag to lowercase kebab-case."""
    lowered = value.strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")


def _normalize_tags(
    raw_tags: Any,
    category: str,
    source: str,
) -> tuple[str, ...]:
    """Keep known tags and top up to three using deterministic fallbacks."""
    tags: list[str] = []
    if isinstance(raw_tags, list):
        for value in raw_tags:
            if not isinstance(value, str):
                continue
            tag = _normalize_tag(value)
            if tag in ALLOWED_TAGS and tag not in tags:
                tags.append(tag)

    fallback = [category, "open-source" if source == "github_trending" else "ai", "ai"]
    for tag in fallback:
        if len(tags) >= 3:
            break
        if tag in ALLOWED_TAGS and tag not in tags:
            tags.append(tag)

    return tuple(tags[:MAX_TAGS])


def _normalize_key_points(value: Any, summary: str) -> tuple[str, ...]:
    """Normalize key points, deriving simple sentences if the model omitted them."""
    points: list[str] = []
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, str) and entry.strip():
                points.append(entry.strip()[:120])

    points = list(dict.fromkeys(points))[:5]
    if len(points) >= 2:
        return tuple(points)

    for sentence in re.split(r"[。！？\n]+", summary):
        text = sentence.strip()
        if 10 <= len(text) <= 60 and text not in points:
            points.append(text)
        if len(points) >= 3:
            break

    return tuple(points[:5])


def _normalize_score(value: Any) -> int:
    """Validate and clamp a 1-10 score."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalysisError("score 不是数字")
    score = int(round(float(value)))
    if score != value:
        LOGGER.debug("score=%s 已四舍五入为 %d", value, score)
    if not SCORE_MIN <= score <= SCORE_MAX:
        LOGGER.warning("score=%s 超出 1-10，已裁剪", value)
    return max(SCORE_MIN, min(SCORE_MAX, score))


def parse_analysis(content: str, source: str) -> Analysis:
    """Parse and normalize an LLM analysis response.

    Args:
        content: Raw assistant text expected to contain one JSON object.
        source: Knowledge-entry source value, used for tag fallbacks.

    Returns:
        A normalized ``Analysis``.

    Raises:
        AnalysisError: If the response is missing required valid fields.
    """
    data = _decode_json_object(content)
    if data is None:
        raise AnalysisError("模型未返回合法 JSON 对象")

    summary = data.get("summary")
    if not isinstance(summary, str):
        raise AnalysisError("summary 缺失或不是字符串")
    summary = summary.strip()
    if len(summary) < SUMMARY_MIN_LENGTH:
        raise AnalysisError(
            f"summary 仅 {len(summary)} 字，至少需要 {SUMMARY_MIN_LENGTH} 字"
        )
    if len(summary) < SUMMARY_FULL_LENGTH:
        LOGGER.debug("summary 仅 %d 字，低于建议的 150 字", len(summary))

    category = data.get("category")
    category = category.strip().lower() if isinstance(category, str) else ""
    if category not in CATEGORIES:
        LOGGER.warning("模型返回未知 category=%r，回退为 tool", category)
        category = "tool"

    return Analysis(
        summary=summary,
        key_points=_normalize_key_points(data.get("key_points"), summary),
        score=_normalize_score(data.get("score")),
        category=category,
        tags=_normalize_tags(data.get("tags"), category, source),
    )


def analyze_items(
    items: Sequence[RawItem],
    provider: LLMProvider,
) -> tuple[list[AnalyzedItem], int]:
    """Analyze every raw item with the LLM.

    Args:
        items: Raw items to analyze.
        provider: Configured LLM provider.

    Returns:
        A tuple of ``(analyzed_items, failure_count)``. Items whose LLM call
        or response fails are skipped rather than replaced with invented text.
    """
    analyzed: list[AnalyzedItem] = []
    failures = 0
    total = len(items)

    for index, item in enumerate(items, start=1):
        LOGGER.info("分析 %d/%d: %s", index, total, item.title)
        try:
            response = chat_with_retry(
                provider,
                build_messages(item),
                temperature=0.2,
                max_tokens=900,
            )
            analysis = parse_analysis(response.content, item.source)
        except (LLMError, AnalysisError) as exc:
            failures += 1
            LOGGER.warning("跳过条目 %r: %s", item.title, exc)
            continue

        LOGGER.debug(
            "分析成功 score=%d category=%s tags=%s",
            analysis.score,
            analysis.category,
            ",".join(analysis.tags),
        )
        analyzed.append(AnalyzedItem(raw=item, analysis=analysis))

    LOGGER.info(
        "分析完成: 成功 %d 条，失败 %d 条",
        len(analyzed),
        failures,
    )
    return analyzed, failures


def load_existing_index(articles_root: Path) -> ExistingIndex:
    """Read saved entries to build duplicate-detection keys.

    Args:
        articles_root: ``knowledge/articles`` directory.

    Returns:
        An ``ExistingIndex``; unreadable or malformed files are ignored.
    """
    urls: set[str] = set()
    source_ids: set[str] = set()
    titles: set[str] = set()

    if not articles_root.exists():
        return ExistingIndex(frozenset(), frozenset(), frozenset())

    for path in articles_root.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            LOGGER.debug("忽略无法读取的既有条目: %s", path)
            continue
        if not isinstance(data, dict):
            continue

        url = data.get("source_url")
        if isinstance(url, str) and url.strip():
            urls.add(canonical_url(url))
        source_id = data.get("source_id")
        if isinstance(source_id, str) and source_id.strip():
            source_ids.add(source_id.strip())
        title = data.get("title")
        if isinstance(title, str) and title.strip():
            titles.add(normalize_title(title))

    LOGGER.debug(
        "既有条目索引: url=%d source_id=%d title=%d",
        len(urls),
        len(source_ids),
        len(titles),
    )
    return ExistingIndex(frozenset(urls), frozenset(source_ids), frozenset(titles))


def _invalid_reason(item: AnalyzedItem) -> str | None:
    """Return why an analyzed item is invalid, or ``None`` when it is valid."""
    raw = item.raw
    analysis = item.analysis

    if not raw.title.strip():
        return "title 为空"
    if raw.source not in ALLOWED_SOURCES:
        return f"source 非法: {raw.source!r}"
    if not is_http_url(raw.url):
        return "source_url 不是合法 HTTP(S) 地址"
    if len(analysis.summary.strip()) < SUMMARY_MIN_LENGTH:
        return "summary 过短"
    if len(analysis.key_points) < 2:
        return "key_points 少于 2 条"
    if not analysis.tags:
        return "tags 为空"
    if not SCORE_MIN <= analysis.score <= SCORE_MAX:
        return "score 超出 1-10"
    if analysis.category not in CATEGORIES:
        return "category 非法"
    return None


def organize_items(
    analyzed: Sequence[AnalyzedItem],
    existing: ExistingIndex,
) -> tuple[list[AnalyzedItem], int, int]:
    """Deduplicate and validate analyzed items.

    Items are ranked by score so the highest-value entry survives when two
    items describe the same resource.

    Args:
        analyzed: Analyzed items from step 2.
        existing: Duplicate keys from already-saved entries.

    Returns:
        A tuple of ``(unique_items, duplicate_count, invalid_count)``.
    """
    urls = set(existing.urls)
    source_ids = set(existing.source_ids)
    titles = set(existing.titles)

    unique: list[AnalyzedItem] = []
    duplicates = 0
    invalid = 0

    ranked = sorted(
        analyzed,
        key=lambda item: (-item.analysis.score, item.raw.title.lower()),
    )
    for item in ranked:
        reason = _invalid_reason(item)
        if reason is not None:
            invalid += 1
            LOGGER.warning("丢弃无效条目 %r: %s", item.raw.title, reason)
            continue

        url_key = canonical_url(item.raw.url)
        source_id = item.raw.source_id.strip()
        title_key = normalize_title(item.raw.title)

        if (
            url_key in urls
            or (bool(source_id) and source_id in source_ids)
            or title_key in titles
        ):
            duplicates += 1
            LOGGER.info("跳过重复条目: %s", item.raw.title)
            continue

        urls.add(url_key)
        if source_id:
            source_ids.add(source_id)
        titles.add(title_key)
        unique.append(item)

    LOGGER.info(
        "整理完成: 保留 %d 条，重复 %d 条，无效 %d 条",
        len(unique),
        duplicates,
        invalid,
    )
    return unique, duplicates, invalid


def date_dir_name(date_str: str) -> str:
    """Convert ``YYYYMMDD`` into ``YYYY-MM-DD``."""
    return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"


def next_sequence(articles_root: Path, date_str: str) -> int:
    """Return the next available four-digit sequence for a date."""
    date_dir = articles_root / date_dir_name(date_str)
    max_sequence = 0
    if date_dir.exists():
        for path in date_dir.glob("kb-*.json"):
            match = KB_ID_RE.fullmatch(path.stem)
            if match is not None and match.group("date") == date_str:
                max_sequence = max(max_sequence, int(match.group("seq")))
    return max_sequence + 1


def _truncate_title(title: str) -> str:
    """Trim a title to the schema limit."""
    text = title.strip()
    if len(text) <= MAX_TITLE_LENGTH:
        return text
    return text[: MAX_TITLE_LENGTH - 1].rstrip() + "…"


def build_article(
    item: AnalyzedItem,
    entry_id: str,
    raw_path: str,
) -> dict[str, Any]:
    """Build a schema-compliant knowledge entry.

    Args:
        item: Analyzed item.
        entry_id: Assigned ``kb-YYYYMMDD-NNNN`` identifier.
        raw_path: Relative path to the raw snapshot.

    Returns:
        A dictionary ready to be serialized as JSON.
    """
    raw = item.raw
    analysis = item.analysis
    status = (
        "analyzed"
        if analysis.score >= SCORE_ACCEPT_THRESHOLD
        else "rejected"
    )

    return {
        "schema_version": 1,
        "id": entry_id,
        "title": _truncate_title(raw.title),
        "source": raw.source,
        "source_url": raw.url,
        "source_id": raw.source_id,
        "author": raw.author or None,
        "language": raw.language,
        "published_at": raw.published_at,
        "collected_at": raw.collected_at,
        "summary": analysis.summary,
        "key_points": list(analysis.key_points),
        "tags": list(analysis.tags),
        "category": analysis.category,
        "relevance_score": round(analysis.score / 10, 2),
        "score": analysis.score,
        "status": status,
        "raw_path": raw_path,
        "article_path": None,
        "distribution": {
            "telegram": {
                "status": "pending",
                "message_id": None,
                "sent_at": None,
            },
            "feishu": {
                "status": "pending",
                "message_id": None,
                "sent_at": None,
            },
        },
    }


def build_articles(
    items: Sequence[AnalyzedItem],
    raw_paths: Mapping[str, str],
    date_str: str,
    first_sequence: int,
) -> list[dict[str, Any]]:
    """Assign IDs and build entries for all selected items.

    Args:
        items: Deduplicated, validated items.
        raw_paths: Feed name to raw snapshot path.
        date_str: Collection date in ``YYYYMMDD`` form.
        first_sequence: First four-digit sequence number.

    Returns:
        Schema-ready article dictionaries.

    Raises:
        PipelineError: If an item has no matching raw snapshot path.
    """
    articles: list[dict[str, Any]] = []
    for offset, item in enumerate(items):
        entry_id = f"kb-{date_str}-{first_sequence + offset:04d}"
        raw_path = raw_paths.get(item.raw.feed)
        if not raw_path:
            raise PipelineError(f"条目 {item.raw.title!r} 缺少原始快照路径")
        articles.append(build_article(item, entry_id, raw_path))
    return articles


def _relative_path(path: Path, root: Path) -> str:
    """Return a POSIX path relative to root when possible."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def save_raw_snapshot(
    raw_root: Path,
    feed: str,
    items: Sequence[RawItem],
    project_root: Path,
) -> str:
    """Write one append-only raw snapshot.

    Args:
        raw_root: ``knowledge/raw`` directory.
        feed: Feed identifier used in the file name.
        items: Collected items from this feed.
        project_root: Root used for the returned relative path.

    Returns:
        The snapshot path relative to ``project_root``.

    Raises:
        PipelineError: If the snapshot cannot be written.
    """
    if not items:
        raise PipelineError("不能写入空的原始快照")

    collected_at = items[0].collected_at
    date_dir = raw_root / collected_at[:10]
    try:
        date_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PipelineError(f"无法创建原始数据目录 {date_dir}: {exc}") from exc

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = date_dir / f"{feed}-{stamp}.json"
    suffix = 2
    while candidate.exists():
        candidate = date_dir / f"{feed}-{stamp}-{suffix}.json"
        suffix += 1

    payload = {
        "schema_version": 1,
        "feed": feed,
        "source": items[0].source,
        "collected_at": collected_at,
        "count": len(items),
        "items": [asdict(item) for item in items],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    temp_path = candidate.with_name(candidate.name + ".tmp")
    try:
        temp_path.write_text(text, encoding="utf-8")
        temp_path.replace(candidate)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise PipelineError(f"无法写入原始快照 {candidate}: {exc}") from exc

    return _relative_path(candidate, project_root)


def save_articles(
    articles: Sequence[Mapping[str, Any]],
    articles_root: Path,
    date_str: str,
) -> list[Path]:
    """Write article JSON files atomically.

    Args:
        articles: Schema-ready entries.
        articles_root: ``knowledge/articles`` directory.
        date_str: Collection date in ``YYYYMMDD`` form.

    Returns:
        Paths of newly written files.
    """
    date_dir = articles_root / date_dir_name(date_str)
    try:
        date_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PipelineError(f"无法创建文章目录 {date_dir}: {exc}") from exc

    saved: list[Path] = []
    for article in articles:
        entry_id = str(article["id"])
        path = date_dir / f"{entry_id}.json"
        if path.exists():
            LOGGER.warning("条目已存在，跳过写入: %s", path)
            continue

        text = json.dumps(article, ensure_ascii=False, indent=2) + "\n"
        temp_path = path.with_name(path.name + ".tmp")
        try:
            temp_path.write_text(text, encoding="utf-8")
            temp_path.replace(path)
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            raise PipelineError(f"无法写入条目 {path}: {exc}") from exc

        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            path.unlink(missing_ok=True)
            LOGGER.error("写入后校验失败，已删除 %s: %s", path, exc)
            continue

        saved.append(path)

    return saved


def _dry_run_report(
    items: Sequence[RawItem],
    existing: ExistingIndex,
) -> int:
    """Log collected candidates and return the duplicate count."""
    urls = set(existing.urls)
    source_ids = set(existing.source_ids)
    titles = set(existing.titles)
    duplicates = 0

    LOGGER.info("干跑模式：仅采集与去重预检，不调用 LLM、不写文件")
    for index, item in enumerate(items, start=1):
        url_key = canonical_url(item.url)
        source_id = item.source_id.strip()
        title_key = normalize_title(item.title)
        is_duplicate = (
            url_key in urls
            or (bool(source_id) and source_id in source_ids)
            or title_key in titles
        )
        if is_duplicate:
            duplicates += 1
        marker = "[重复]" if is_duplicate else "[新条目]"
        LOGGER.info(
            "%s %d/%d %s | %s | 热度=%d",
            marker,
            index,
            len(items),
            item.title,
            item.url,
            item.popularity,
        )
        urls.add(url_key)
        if source_id:
            source_ids.add(source_id)
        titles.add(title_key)

    LOGGER.info(
        "干跑完成: 候选 %d 条，其中重复 %d 条",
        len(items),
        duplicates,
    )
    return duplicates


def _group_by_feed(items: Sequence[RawItem]) -> dict[str, list[RawItem]]:
    """Group raw items by feed while preserving insertion order."""
    grouped: dict[str, list[RawItem]] = {}
    for item in items:
        grouped.setdefault(item.feed, []).append(item)
    return grouped


def _collect_raw_items(config: PipelineConfig) -> list[RawItem]:
    """Run the collection stage for all configured sources."""
    items: list[RawItem] = []
    with httpx.Client(
        timeout=HTTP_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        if "github" in config.sources:
            items.extend(collect_github(config.limit, client))
        if "rss" in config.sources:
            items.extend(collect_rss(config.limit, client))
    return items


def _persist_raw_snapshots(
    raw_items: Sequence[RawItem],
    raw_root: Path,
    project_root: Path,
) -> dict[str, str]:
    """Write one raw snapshot per feed and return a feed-to-path mapping."""
    raw_paths: dict[str, str] = {}
    for feed, feed_items in _group_by_feed(raw_items).items():
        raw_path = save_raw_snapshot(raw_root, feed, feed_items, project_root)
        raw_paths[feed] = raw_path
        LOGGER.info("原始快照: %s（%d 条）", raw_path, len(feed_items))
    return raw_paths


def _resolve_provider(provider: LLMProvider | None) -> LLMProvider:
    """Return the injected provider or create one from the environment."""
    if provider is not None:
        return provider
    try:
        return create_provider()
    except ValueError as exc:
        raise PipelineError(f"LLM 客户端初始化失败: {exc}") from exc


def _analyze_stage(
    raw_items: Sequence[RawItem],
    provider: LLMProvider,
    existing: ExistingIndex,
    stats: PipelineStats,
) -> list[AnalyzedItem]:
    """Run the analysis stage and return deduplicated valid items."""
    LOGGER.info("Step 2/4 分析: %d 条", len(raw_items))
    analyzed, failures = analyze_items(raw_items, provider)
    stats.analyzed = len(analyzed)
    stats.analysis_failed = failures
    if not analyzed:
        LOGGER.error("所有条目分析失败，流水线终止")
        return []

    LOGGER.info("Step 3/4 整理")
    selected, duplicates, invalid = organize_items(analyzed, existing)
    stats.duplicates = duplicates
    stats.invalid = invalid
    if not selected:
        LOGGER.warning("去重与校验后没有可保存条目")
    return selected


def _save_stage(
    selected: Sequence[AnalyzedItem],
    raw_paths: Mapping[str, str],
    articles_root: Path,
    date_str: str,
    stats: PipelineStats,
) -> None:
    """Build and persist article files for the selected items."""
    LOGGER.info("Step 4/4 保存")
    first_sequence = next_sequence(articles_root, date_str)
    articles = build_articles(selected, raw_paths, date_str, first_sequence)
    saved = save_articles(articles, articles_root, date_str)
    stats.saved = len(saved)
    stats.rejected = sum(
        1 for article in articles if article["status"] == "rejected"
    )
    for path in saved:
        LOGGER.info("已保存: %s", path)


def run_pipeline(
    config: PipelineConfig,
    provider: LLMProvider | None = None,
) -> PipelineStats:
    """Execute all four pipeline stages.

    Args:
        config: Runtime configuration.
        provider: Optional provider override, mainly for tests and embedding.

    Returns:
        Pipeline statistics.

    Raises:
        PipelineError: If a stage cannot continue.
    """
    stats = PipelineStats()
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    raw_root = config.knowledge_dir / "raw"
    articles_root = config.knowledge_dir / "articles"
    existing = load_existing_index(articles_root)

    LOGGER.info(
        "Step 1/4 采集: sources=%s limit=%d",
        ",".join(config.sources),
        config.limit,
    )
    raw_items = _collect_raw_items(config)
    stats.collected = len(raw_items)
    if not raw_items:
        LOGGER.error("没有采集到任何条目，流水线终止")
        return stats

    if config.dry_run:
        stats.duplicates = _dry_run_report(raw_items, existing)
        return stats

    raw_paths = _persist_raw_snapshots(
        raw_items,
        raw_root,
        config.project_root,
    )
    provider = _resolve_provider(provider)

    selected = _analyze_stage(raw_items, provider, existing, stats)
    if not selected:
        return stats

    _save_stage(selected, raw_paths, articles_root, date_str, stats)

    LOGGER.info(
        "流水线完成: 采集 %d / 分析 %d / 分析失败 %d / 重复 %d / "
        "无效 %d / 保存 %d（其中 rejected %d）",
        stats.collected,
        stats.analyzed,
        stats.analysis_failed,
        stats.duplicates,
        stats.invalid,
        stats.saved,
        stats.rejected,
    )
    return stats


def _positive_int(value: str) -> int:
    """Argparse type for a positive integer."""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"需要正整数，实际为 {value!r}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("需要大于 0 的整数")
    return number


def _parse_sources(value: str) -> tuple[str, ...]:
    """Argparse type for a comma-separated source list."""
    names = [part.strip().lower() for part in value.split(",") if part.strip()]
    if not names:
        raise argparse.ArgumentTypeError("--sources 不能为空")
    unknown = [name for name in names if name not in SOURCE_CHOICES]
    if unknown:
        allowed = ",".join(SOURCE_CHOICES)
        raise argparse.ArgumentTypeError(
            f"未知来源 {','.join(unknown)}；可选 {allowed}"
        )
    return tuple(dict.fromkeys(names))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        Parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description="四步 AI 知识库流水线：采集 → 分析 → 整理 → 保存。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python pipeline/pipeline.py --sources github,rss --limit 20\n"
            "  python pipeline/pipeline.py --sources github --limit 5\n"
            "  python pipeline/pipeline.py --sources rss --limit 10\n"
            "  python pipeline/pipeline.py --sources github --limit 5 --dry-run\n"
            "  python pipeline/pipeline.py --verbose"
        ),
    )
    parser.add_argument(
        "--sources",
        type=_parse_sources,
        default=SOURCE_CHOICES,
        metavar="LIST",
        help="逗号分隔的来源列表：github,rss（默认两者）",
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=20,
        metavar="N",
        help="每个来源最多采集条数（默认 20）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只采集并打印候选，不调用 LLM、不写入任何文件",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="输出 DEBUG 级日志",
    )
    parser.add_argument(
        "--knowledge-dir",
        type=Path,
        default=DEFAULT_KNOWLEDGE_DIR,
        metavar="DIR",
        help="知识库根目录（默认项目下的 knowledge/）",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI pipeline.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: 0 when the run produced output (or a non-empty
        dry run), otherwise 1.
    """
    args = parse_args(argv)
    setup_logging(args.verbose)

    config = PipelineConfig(
        sources=tuple(args.sources),
        limit=args.limit,
        dry_run=args.dry_run,
        knowledge_dir=args.knowledge_dir.expanduser(),
    )

    try:
        stats = run_pipeline(config)
    except PipelineError as exc:
        LOGGER.error("流水线失败: %s", exc)
        return 1
    except KeyboardInterrupt:
        LOGGER.warning("用户中断")
        return 130

    if config.dry_run:
        return 0 if stats.collected > 0 else 1
    return 0 if stats.saved > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
