#!/usr/bin/env python3
"""Push the daily knowledge digest to a Feishu (Lark) group bot.

The digest only includes entries that already passed the analysis gate
(``status`` is ``analyzed`` or ``published`` and ``score >= 6``), so nothing
unreviewed ever reaches a group. At most ``--top`` entries are rendered into
one interactive card, ordered by score, with a button linking to the full
date directory.

Usage:
    python pipeline/distribute.py --dry-run
    python pipeline/distribute.py --date 2026-11-25
    python pipeline/distribute.py --date 2026-11-25 --top 8 --min-score 7
    python pipeline/distribute.py --date 2026-11-25 --mark-distribution

Environment:
    FEISHU_WEBHOOK_URL: Custom-bot webhook URL.
    FEISHU_WEBHOOK_SECRET: Optional signing secret; strongly recommended since
        GitHub Actions runners do not have stable IPs.
    GITHUB_REPOSITORY: ``owner/repo``, injected by GitHub Actions, used to
        build the "view all" button URL.

The module only uses the standard library so it runs before ``pip install``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "Digest",
    "DigestItem",
    "SendResult",
    "build_card",
    "build_digest",
    "build_payload",
    "count_already_sent",
    "load_articles",
    "main",
    "mark_distribution",
    "send_feishu",
    "sign",
]

LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"
DEFAULT_REPO_URL = "https://github.com/smartv/ai-knowledge-base"

ARTICLES_DIRNAME = "articles"

FEISHU_WEBHOOK_ENV = "FEISHU_WEBHOOK_URL"
FEISHU_SECRET_ENV = "FEISHU_WEBHOOK_SECRET"
CHANNEL_FEISHU = "feishu"
CHANNELS = (CHANNEL_FEISHU,)

# 与 pipeline.SCORE_ACCEPT_THRESHOLD 保持一致，避免两处阈值漂移。
SCORE_ACCEPT_THRESHOLD = 6.0
DISTRIBUTABLE_STATUSES = frozenset({"analyzed", "published"})

DIGEST_TOP_N = 5
MIN_SCORE_MIN = 1.0
MIN_SCORE_MAX = 10.0

SUMMARY_MAX_CHARS = 180
TITLE_MAX_CHARS = 100
MAX_TAGS_IN_CARD = 3
ERROR_DETAIL_MAX_CHARS = 200

HTTP_TIMEOUT_SECONDS = 15.0
CARD_HEADER_TEMPLATE = "blue"

# 飞书自定义机器人常见业务错误码。
FEISHU_ERROR_HINTS: Mapping[Any, str] = {
    19021: "签名校验失败或时间戳超出 1 小时",
    19024: "未命中自定义关键词",
    11232: "触发频率限制，请降低发送频率",
}


@dataclass(frozen=True)
class DigestItem:
    """One article rendered into the digest card.

    Attributes:
        id: Knowledge-entry identifier, e.g. ``kb-20261125-0001``.
        title: Article title, already truncated to one line.
        summary: Chinese summary.
        tags: Article tags.
        url: Original source URL.
        score: Analysis score on a 1-10 scale.
    """

    id: str
    title: str
    summary: str
    tags: tuple[str, ...]
    url: str
    score: float


@dataclass(frozen=True)
class Digest:
    """The payload of one daily digest.

    Attributes:
        date: Collection date in ``YYYY-MM-DD`` form.
        total: Number of entries eligible for distribution.
        selected: Entries actually rendered into the card.
        folder_url: Web URL of the full date directory.
    """

    date: str
    total: int
    selected: tuple[DigestItem, ...]
    folder_url: str

    @property
    def is_empty(self) -> bool:
        """Return whether the digest has nothing to send."""
        return not self.selected


@dataclass(frozen=True)
class SendResult:
    """Outcome of one delivery attempt.

    Attributes:
        status: One of ``sent``, ``failed`` or ``skipped``.
        error: Human-readable failure detail, empty on success.
    """

    status: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Return whether the delivery succeeded."""
        return self.status == "sent"


def _setup_logging(verbose: bool) -> None:
    """Configure root logging for CLI usage.

    Args:
        verbose: Enable DEBUG level when true.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def iso_utc(moment: datetime) -> str:
    """Format an aware datetime as ``YYYY-MM-DDTHH:MM:SSZ``."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate(text: str, limit: int) -> str:
    """Truncate text to ``limit`` characters with an ellipsis."""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _one_line(text: str) -> str:
    """Collapse all whitespace so the text fits on a single card line."""
    return " ".join(text.split())


def _text(value: Any) -> str:
    """Return a stripped string for string values, otherwise an empty string."""
    return value.strip() if isinstance(value, str) else ""


def default_repo_url() -> str:
    """Return the repository web URL, preferring ``GITHUB_REPOSITORY``."""
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if repo:
        return f"https://github.com/{repo}"
    return DEFAULT_REPO_URL


def resolve_date(value: str | None) -> str:
    """Return a ``YYYY-MM-DD`` date string, defaulting to today in UTC.

    Args:
        value: User-supplied date in ``YYYY-MM-DD`` form, or ``None``.

    Returns:
        The normalized date string.

    Raises:
        ValueError: If ``value`` is not a valid ``YYYY-MM-DD`` date.
    """
    if not value:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"日期格式应为 YYYY-MM-DD，实际为 {value!r}") from exc


def load_articles(
    articles_root: Path,
    date_str: str,
) -> list[tuple[Path, dict[str, Any]]]:
    """Load every article JSON under one date directory.

    Args:
        articles_root: ``knowledge/articles`` directory.
        date_str: Date partition in ``YYYY-MM-DD`` form.

    Returns:
        Parsed ``(path, record)`` pairs sorted by file name. Unreadable or
        non-object files are logged and skipped.
    """
    date_dir = articles_root / date_str
    if not date_dir.is_dir():
        LOGGER.warning("日期目录不存在: %s", date_dir)
        return []

    pairs: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(date_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("跳过无法解析的条目 %s: %s", path, exc)
            continue
        if not isinstance(record, dict):
            LOGGER.warning("跳过非对象条目 %s", path)
            continue
        pairs.append((path, record))
    return pairs


def _entry_score(record: Mapping[str, Any]) -> float:
    """Return the 1-10 analysis score, falling back to ``relevance_score``."""
    score = record.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        return float(score)
    relevance = record.get("relevance_score")
    if isinstance(relevance, (int, float)) and not isinstance(relevance, bool):
        return float(relevance) * 10
    return 0.0


def _entry_tags(record: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the article tags as a tuple of non-empty strings."""
    tags = record.get("tags")
    if not isinstance(tags, list):
        return ()
    return tuple(
        tag.strip()
        for tag in tags
        if isinstance(tag, str) and tag.strip()
    )


def _to_item(record: Mapping[str, Any]) -> DigestItem | None:
    """Convert one raw record into a :class:`DigestItem`."""
    entry_id = _text(record.get("id"))
    title = _text(record.get("title"))
    if not entry_id or not title:
        return None
    return DigestItem(
        id=entry_id,
        title=_truncate(_one_line(title), TITLE_MAX_CHARS),
        summary=_one_line(_text(record.get("summary"))),
        tags=_entry_tags(record),
        url=_text(record.get("source_url")),
        score=_entry_score(record),
    )


def build_digest(
    pairs: Sequence[tuple[Path, dict[str, Any]]],
    date_str: str,
    *,
    top_n: int = DIGEST_TOP_N,
    min_score: float = SCORE_ACCEPT_THRESHOLD,
    repo_url: str | None = None,
) -> Digest:
    """Select the entries worth pushing and order them by score.

    Args:
        pairs: ``(path, record)`` pairs from :func:`load_articles`.
        date_str: Date partition in ``YYYY-MM-DD`` form.
        top_n: Maximum number of entries rendered into the card.
        min_score: Minimum analysis score required for distribution.
        repo_url: Repository web URL; defaults to :func:`default_repo_url`.

    Returns:
        A digest holding up to ``top_n`` entries sorted by descending score.
    """
    eligible: list[DigestItem] = []
    for _path, record in pairs:
        if record.get("status") not in DISTRIBUTABLE_STATUSES:
            continue
        if _entry_score(record) < min_score:
            continue
        item = _to_item(record)
        if item is not None:
            eligible.append(item)

    eligible.sort(key=lambda item: (-item.score, item.id))
    repo = (repo_url or default_repo_url()).rstrip("/")
    return Digest(
        date=date_str,
        total=len(eligible),
        selected=tuple(eligible[: max(top_n, 0)]),
        folder_url=f"{repo}/tree/main/knowledge/{ARTICLES_DIRNAME}/{date_str}",
    )


def count_already_sent(
    pairs: Sequence[tuple[Path, dict[str, Any]]],
) -> int:
    """Count entries whose ``distribution.feishu.status`` is already ``sent``."""
    sent = 0
    for _path, record in pairs:
        distribution = record.get("distribution")
        if not isinstance(distribution, Mapping):
            continue
        feishu = distribution.get("feishu")
        if isinstance(feishu, Mapping) and feishu.get("status") == "sent":
            sent += 1
    return sent


def sign(timestamp: int, secret: str) -> str:
    """Compute the Feishu custom-bot signature.

    Feishu expects ``base64(HMAC-SHA256(key="{timestamp}\\n{secret}", data=""))``.

    Args:
        timestamp: Unix timestamp in seconds.
        secret: Signing secret from the bot's security settings.

    Returns:
        The base64-encoded signature.
    """
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        string_to_sign.encode("utf-8"),
        b"",
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _item_markdown(index: int, item: DigestItem) -> str:
    """Render one article as a Feishu ``lark_md`` block."""
    lines = [f"**{index}. {item.title}** · score {item.score:g}"]
    if item.summary:
        lines.append(_truncate(item.summary, SUMMARY_MAX_CHARS))
    if item.tags:
        lines.append(" ".join(f"`{tag}`" for tag in item.tags[:MAX_TAGS_IN_CARD]))
    if item.url:
        lines.append(f"[查看原文]({item.url})")
    return "\n".join(lines)


def _summary_element(digest: Digest) -> dict[str, Any]:
    """Render the header line summarising the digest."""
    return {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": (
                f"今日新增 **{digest.total}** 条，"
                f"精选 **{len(digest.selected)}** 条"
            ),
        },
    }


def _item_elements(digest: Digest) -> list[dict[str, Any]]:
    """Render one separator plus one block per selected article."""
    elements: list[dict[str, Any]] = []
    for index, item in enumerate(digest.selected, start=1):
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": _item_markdown(index, item)},
            }
        )
    return elements


def _button_element(digest: Digest) -> dict[str, Any] | None:
    """Render the "view all" button, or ``None`` when no folder URL is set."""
    if not digest.folder_url:
        return None
    return {
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "type": "primary",
                "text": {
                    "tag": "plain_text",
                    "content": f"查看全部 {digest.total} 条",
                },
                "url": digest.folder_url,
            }
        ],
    }


def build_card(digest: Digest) -> dict[str, Any]:
    """Build the Feishu interactive card for a digest.

    Args:
        digest: Digest to render.

    Returns:
        A card object ready to be embedded in the webhook payload.
    """
    elements = [_summary_element(digest), *_item_elements(digest)]
    button = _button_element(digest)
    if button is not None:
        elements.extend(({"tag": "hr"}, button))

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": CARD_HEADER_TEMPLATE,
            "title": {
                "tag": "plain_text",
                "content": f"AI 知识库日报 · {digest.date}",
            },
        },
        "elements": elements,
    }


def build_payload(
    digest: Digest,
    *,
    secret: str | None = None,
    timestamp: int | None = None,
) -> dict[str, Any]:
    """Build the complete webhook payload, including signature fields.

    Args:
        digest: Digest to render.
        secret: Signing secret; when omitted the payload is unsigned.
        timestamp: Unix timestamp override, mainly for tests.

    Returns:
        A JSON-serializable webhook payload.
    """
    payload: dict[str, Any] = {
        "msg_type": "interactive",
        "card": build_card(digest),
    }
    if secret:
        moment = int(time.time()) if timestamp is None else timestamp
        payload["timestamp"] = str(moment)
        payload["sign"] = sign(moment, secret)
    return payload


def _response_error(result: Any) -> str:
    """Render a Feishu error response as a compact diagnostic string."""
    code = result.get("code", result.get("StatusCode"))
    message = result.get("msg") or result.get("StatusMessage") or ""
    detail = f"code={code} msg={message}"
    hint = FEISHU_ERROR_HINTS.get(code)
    if hint:
        detail = f"{detail}（{hint}）"
    return detail


def _check_response(result: Any) -> SendResult:
    """Map a parsed Feishu response body to a :class:`SendResult`."""
    if not isinstance(result, Mapping):
        return SendResult(status="failed", error="响应不是 JSON 对象")

    # 飞书在业务失败时同样返回 HTTP 200，因此只能依据 code 判断。
    code = result.get("code", result.get("StatusCode"))
    if code in (0, "0"):
        return SendResult(status="sent")
    return SendResult(status="failed", error=_response_error(result))


def send_feishu(
    webhook: str,
    payload: Mapping[str, Any],
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> SendResult:
    """POST one payload to a Feishu custom-bot webhook.

    Args:
        webhook: Full webhook URL including the bot token.
        payload: JSON-serializable payload.
        timeout: Request timeout in seconds.

    Returns:
        A :class:`SendResult`; network and business failures are reported in
        ``error`` instead of raising, so the CLI can log and exit non-zero.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return SendResult(status="failed", error=_http_error_message(exc))
    except (urllib.error.URLError, OSError) as exc:
        return SendResult(status="failed", error=f"网络错误: {exc}")

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        detail = _truncate(_one_line(raw), ERROR_DETAIL_MAX_CHARS)
        return SendResult(status="failed", error=f"响应无法解析: {detail}")
    return _check_response(result)


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    """Build a diagnostic message from an HTTP error response."""
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except OSError:
        return f"HTTP {exc.code}"
    detail = _truncate(_one_line(raw), ERROR_DETAIL_MAX_CHARS)
    return f"HTTP {exc.code}: {detail}"


def mark_distribution(
    pairs: Sequence[tuple[Path, dict[str, Any]]],
    selected_ids: Collection[str],
    sent_at: str,
) -> int:
    """Write ``distribution.feishu`` back into the delivered article files.

    Only ``status``, ``distribution`` and ``sent_at`` are touched, which are
    the append-only-safe fields allowed by ``AGENTS.md``. The custom bot does
    not return a message id, so ``message_id`` stays ``null``.

    Args:
        pairs: ``(path, record)`` pairs from :func:`load_articles`.
        selected_ids: Identifiers of the entries included in the card.
        sent_at: Delivery timestamp in ISO 8601 UTC.

    Returns:
        Number of files successfully rewritten.
    """
    updated = 0
    for path, record in pairs:
        entry_id = record.get("id")
        if not isinstance(entry_id, str) or entry_id not in selected_ids:
            continue

        distribution = record.get("distribution")
        if not isinstance(distribution, dict):
            distribution = {}
            record["distribution"] = distribution
        distribution["feishu"] = {
            "status": "sent",
            "message_id": None,
            "sent_at": sent_at,
        }

        text = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            LOGGER.error("回写分发状态失败 %s: %s", path, exc)
            continue
        updated += 1

    return updated


def _positive_int(value: str) -> int:
    """Argparse type for a positive integer."""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"需要正整数，实际为 {value!r}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("需要大于 0 的整数")
    return number


def _score_value(value: str) -> float:
    """Argparse type for a score between 1 and 10."""
    try:
        score = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"需要数字，实际为 {value!r}") from exc
    if not MIN_SCORE_MIN <= score <= MIN_SCORE_MAX:
        raise argparse.ArgumentTypeError("评分需在 1-10 之间")
    return score


def _add_selection_args(parser: argparse.ArgumentParser) -> None:
    """Register the arguments that control which entries are selected."""
    parser.add_argument(
        "--date",
        default=None,
        metavar="YYYY-MM-DD",
        help="要推送的日期目录（默认今天 UTC）",
    )
    parser.add_argument(
        "--channel",
        choices=CHANNELS,
        default=CHANNEL_FEISHU,
        help="分发渠道（当前仅 feishu）",
    )
    parser.add_argument(
        "--top",
        type=_positive_int,
        default=DIGEST_TOP_N,
        metavar="N",
        help=f"卡片内最多条目数（默认 {DIGEST_TOP_N}）",
    )
    parser.add_argument(
        "--min-score",
        type=_score_value,
        default=SCORE_ACCEPT_THRESHOLD,
        metavar="S",
        help=f"推送所需最低评分（默认 {SCORE_ACCEPT_THRESHOLD:g}）",
    )
    parser.add_argument(
        "--knowledge-dir",
        type=Path,
        default=DEFAULT_KNOWLEDGE_DIR,
        metavar="DIR",
        help="知识库根目录（默认项目下的 knowledge/）",
    )
    parser.add_argument(
        "--repo-url",
        default=None,
        metavar="URL",
        help="仓库 Web 地址，用于生成「查看全部」按钮",
    )


def _add_action_args(parser: argparse.ArgumentParser) -> None:
    """Register the arguments that change the delivery behaviour."""
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印卡片 JSON，不发送、不写文件",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使当日已有条目被标记为 sent 也重新发送",
    )
    parser.add_argument(
        "--mark-distribution",
        action="store_true",
        help="发送成功后回写 distribution.feishu 状态",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="输出 DEBUG 级日志",
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        Parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description="把当日知识条目推送为飞书群消息卡片。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python pipeline/distribute.py --dry-run\n"
            "  python pipeline/distribute.py --date 2026-11-25\n"
            "  python pipeline/distribute.py --date 2026-11-25 --top 8\n"
            "  python pipeline/distribute.py --mark-distribution"
        ),
    )
    _add_selection_args(parser)
    _add_action_args(parser)
    return parser.parse_args(argv)


def _deliver(
    args: argparse.Namespace,
    digest: Digest,
    pairs: Sequence[tuple[Path, dict[str, Any]]],
    payload: Mapping[str, Any],
) -> int:
    """Send the payload and optionally write the delivery state back."""
    webhook = os.getenv(FEISHU_WEBHOOK_ENV, "").strip()
    if not webhook:
        LOGGER.error("缺少环境变量 %s", FEISHU_WEBHOOK_ENV)
        return 1

    result = send_feishu(webhook, payload)
    if not result.ok:
        LOGGER.error("飞书推送失败: %s", result.error)
        return 1

    LOGGER.info("飞书推送成功：%d/%d 条", len(digest.selected), digest.total)
    if args.mark_distribution:
        updated = mark_distribution(
            pairs,
            {item.id for item in digest.selected},
            iso_utc(datetime.now(timezone.utc)),
        )
        LOGGER.info("已回写 %d 个条目的 distribution.feishu", updated)
    return 0


def _dispatch(args: argparse.Namespace, date_str: str) -> int:
    """Build the digest for ``date_str`` and hand it to the sender."""
    articles_root = args.knowledge_dir.expanduser() / ARTICLES_DIRNAME
    pairs = load_articles(articles_root, date_str)
    digest = build_digest(
        pairs,
        date_str,
        top_n=args.top,
        min_score=args.min_score,
        repo_url=args.repo_url,
    )

    if digest.is_empty:
        LOGGER.info("%s 没有可推送的条目，跳过", date_str)
        return 0

    already_sent = count_already_sent(pairs)
    if already_sent and not args.force:
        LOGGER.info(
            "%s 已有 %d 条标记为 sent，跳过重发（--force 可强制）",
            date_str,
            already_sent,
        )
        return 0

    payload = build_payload(digest, secret=os.getenv(FEISHU_SECRET_ENV) or None)
    if args.dry_run:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        LOGGER.info("干跑：未发送，候选 %d 条", digest.total)
        return 0

    return _deliver(args, digest, pairs, payload)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the distribution CLI.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: 0 on success or an intentional skip, 1 on delivery
        failure, 2 on invalid arguments.
    """
    args = parse_args(argv)
    _setup_logging(args.verbose)

    if args.channel != CHANNEL_FEISHU:  # pragma: no cover - argparse 已限制
        LOGGER.error("暂不支持渠道 %s", args.channel)
        return 2

    try:
        date_str = resolve_date(args.date)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 2

    return _dispatch(args, date_str)


if __name__ == "__main__":
    sys.exit(main())
