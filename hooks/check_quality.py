#!/usr/bin/env python3
"""Score knowledge-entry JSON files across five quality dimensions.

Usage:
    python hooks/check_quality.py <json_file> [json_file2 ...]
    python hooks/check_quality.py "knowledge/articles/2026-11-25/*.json"

Each file receives a 100-point weighted score and an A/B/C grade. The process
exits with 0 when every file is grade A or B, otherwise 1.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

TECH_KEYWORDS = (
    "llm",
    "llms",
    "agent",
    "agents",
    "rag",
    "mcp",
    "transformer",
    "embedding",
    "embeddings",
    "vector",
    "multimodal",
    "inference",
    "fine-tuning",
    "prompt",
    "token",
    "gpu",
    "api",
    "sdk",
    "framework",
    "模型",
    "推理",
    "微调",
    "向量",
    "多模态",
    "上下文",
    "训练",
    "部署",
    "评测",
    "检索",
    "智能体",
    "开源",
    "框架",
    "算法",
    "数据集",
    "神经网络",
    "知识库",
    "工具调用",
    "工作流",
    "上下文工程",
)

STANDARD_TAGS = frozenset(
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

EMPTY_WORDS_ZH = (
    "赋能",
    "抓手",
    "闭环",
    "打通",
    "全链路",
    "底层逻辑",
    "颗粒度",
    "对齐",
    "拉通",
    "沉淀",
    "强大的",
    "革命性的",
)

EMPTY_WORDS_EN = (
    "groundbreaking",
    "revolutionary",
    "game-changing",
    "cutting-edge",
    "disruptive",
    "paradigm-shifting",
    "next-generation",
    "best-in-class",
    "world-class",
    "seamless",
    "synergy",
    "leverage",
    "innovative",
    "state-of-the-art",
    "bleeding-edge",
    "turnkey",
    "holistic",
    "empower",
    "empowering",
)

EN_EMPTY_PATTERNS = tuple(
    (term, re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE))
    for term in EMPTY_WORDS_EN
)

TIMESTAMP_FIELDS = (
    "collected_at",
    "published_at",
    "created_at",
    "updated_at",
    "timestamp",
)

GRADE_A_THRESHOLD = 80.0
GRADE_B_THRESHOLD = 60.0

ID_PATTERN = re.compile(
    r"^(?:"
    r"[a-z][a-z0-9_]*(?:-[a-z0-9_]+)*-[0-9]{8}-[0-9]{3}"
    r"|"
    r"kb-[0-9]{8}-[0-9]{4}"
    r")$"
)

VALID_STATUSES = frozenset(
    {
        "draft",
        "review",
        "published",
        "archived",
        "raw",
        "analyzed",
        "rejected",
    }
)


@dataclass(frozen=True)
class DimensionScore:
    """Score for one quality dimension.

    Attributes:
        name: Human-readable dimension name.
        score: Points earned in this dimension.
        max_score: Maximum points available.
        detail: Short explanation of how the score was determined.
    """

    name: str
    score: float
    max_score: float
    detail: str = ""


@dataclass(frozen=True)
class QualityReport:
    """Quality report for one knowledge-entry file.

    Attributes:
        path: File that was evaluated.
        dimensions: Per-dimension scores.
        error: Read/parse error, if the file could not be evaluated.
    """

    path: Path
    dimensions: tuple[DimensionScore, ...] = ()
    error: str | None = None

    @property
    def total(self) -> float:
        """Return the weighted total across all dimensions."""
        return round(sum(dimension.score for dimension in self.dimensions), 1)

    @property
    def grade(self) -> str:
        """Return the letter grade for the total score."""
        if self.error is not None:
            return "C"
        if self.total >= GRADE_A_THRESHOLD:
            return "A"
        if self.total >= GRADE_B_THRESHOLD:
            return "B"
        return "C"

    @property
    def passed(self) -> bool:
        """Return whether the report is grade A or B."""
        return self.grade != "C"


def _has_text(value: Any) -> bool:
    """Return whether the value is a non-empty string."""
    return isinstance(value, str) and bool(value.strip())


def _is_http_url(value: Any) -> bool:
    """Return whether the value is an absolute HTTP(S) URL."""
    if not isinstance(value, str) or not value.strip():
        return False
    if any(char.isspace() for char in value):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_valid_id(value: Any) -> bool:
    """Return whether the value matches a supported entry ID format."""
    return isinstance(value, str) and ID_PATTERN.fullmatch(value.strip()) is not None


def _is_valid_status(value: Any) -> bool:
    """Return whether the value is a supported status name."""
    return (
        isinstance(value, str)
        and value.strip().lower() in VALID_STATUSES
    )


def _is_timestamp(value: Any) -> bool:
    """Return whether the value is a positive number or an ISO 8601 string."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value > 0
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _contains_keyword(text: str, keyword: str) -> bool:
    """Return whether a technical keyword appears in the text."""
    if keyword.isascii():
        pattern = rf"\b{re.escape(keyword)}\b"
        return re.search(pattern, text, re.IGNORECASE) is not None
    return keyword in text


def _count_tech_keywords(text: str) -> int:
    """Count distinct technical keywords present in the text."""
    return sum(1 for keyword in TECH_KEYWORDS if _contains_keyword(text, keyword))


def _entry_text(data: Mapping[str, Any]) -> str:
    """Collect prose fields used for empty-word detection."""
    parts: list[str] = []
    for field in ("title", "summary", "description", "content"):
        value = data.get(field)
        if isinstance(value, str):
            parts.append(value)
    for field in ("key_points", "highlights"):
        value = data.get(field)
        if isinstance(value, list):
            parts.extend(item for item in value if isinstance(item, str))
    return "\n".join(parts)


def score_summary(data: Mapping[str, Any]) -> DimensionScore:
    """Score the summary length and technical keyword coverage.

    Args:
        data: Decoded knowledge entry.

    Returns:
        A ``DimensionScore`` out of 25. Summaries of 50+ characters receive
        full marks; 20-49 characters receive the basic score plus a keyword
        bonus; shorter summaries receive zero.
    """
    summary = data.get("summary")
    text = summary.strip() if isinstance(summary, str) else ""
    length = len(text)
    keyword_count = _count_tech_keywords(text)

    if length >= 50:
        score = 25.0
        detail = f"摘要 {length} 字（≥50），命中 {keyword_count} 个技术关键词"
    elif length >= 20:
        bonus = 5.0 if keyword_count >= 2 else 3.0 if keyword_count == 1 else 0.0
        score = 15.0 + bonus
        detail = (
            f"摘要 {length} 字（20–49 基本分），"
            f"命中 {keyword_count} 个技术关键词，加 {bonus:.0f} 分"
        )
    else:
        score = 0.0
        detail = f"摘要 {length} 字（<20），未达基本分"

    return DimensionScore("摘要质量", score, 25.0, detail)


def score_technical_depth(data: Mapping[str, Any]) -> DimensionScore:
    """Map the entry ``score`` field from 1-10 onto 0-25 points.

    Args:
        data: Decoded knowledge entry.

    Returns:
        A ``DimensionScore`` out of 25.
    """
    value = data.get("score")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DimensionScore("技术深度", 0.0, 25.0, "缺少或非法 score 字段")
    if not 1 <= value <= 10:
        return DimensionScore(
            "技术深度", 0.0, 25.0, f"score={value} 超出 1–10 范围"
        )

    mapped = (float(value) - 1.0) / 9.0 * 25.0
    return DimensionScore(
        "技术深度",
        round(mapped, 1),
        25.0,
        f"score={value}，线性映射为 {mapped:.1f}/25",
    )


def score_format(data: Mapping[str, Any]) -> DimensionScore:
    """Score five format fields at four points each.

    Args:
        data: Decoded knowledge entry.

    Returns:
        A ``DimensionScore`` out of 20. The timestamp check accepts the first
        non-empty value among the supported timestamp field names.
    """
    checks: list[tuple[str, bool]] = [
        ("id", _is_valid_id(data.get("id"))),
        ("title", _has_text(data.get("title"))),
        ("source_url", _is_http_url(data.get("source_url"))),
        ("status", _is_valid_status(data.get("status"))),
    ]
    timestamp_field = next(
        (field for field in TIMESTAMP_FIELDS if _is_timestamp(data.get(field))),
        None,
    )
    checks.append(("时间戳", timestamp_field is not None))

    passed_names = [name for name, ok in checks if ok]
    failed_names = [name for name, ok in checks if not ok]
    score = 4.0 * len(passed_names)

    if not failed_names:
        detail = "5/5 项齐全"
        if timestamp_field is not None:
            detail += f"（时间戳: {timestamp_field}）"
    else:
        detail = (
            f"{len(passed_names)}/5 项通过；"
            f"缺失或不合规: {', '.join(failed_names)}"
        )

    return DimensionScore("格式规范", score, 20.0, detail)


def score_tags(data: Mapping[str, Any]) -> DimensionScore:
    """Score tag count and membership in the standard tag list.

    Args:
        data: Decoded knowledge entry.

    Returns:
        A ``DimensionScore`` out of 15. One to three standard tags earn full
        marks; larger tag sets are scaled down by ``3 / count``.
    """
    raw_tags = data.get("tags")
    if not isinstance(raw_tags, list) or not raw_tags:
        return DimensionScore("标签精度", 0.0, 15.0, "缺少 tags 或标签为空")

    total = len(raw_tags)
    normalized = [
        tag.strip().lower()
        for tag in raw_tags
        if isinstance(tag, str) and tag.strip()
    ]
    valid = {tag for tag in normalized if tag in STANDARD_TAGS}
    count_factor = 1.0 if total <= 3 else 3.0 / total
    valid_factor = len(valid) / total
    score = round(15.0 * count_factor * valid_factor, 1)

    unknown = sorted({tag for tag in normalized if tag not in STANDARD_TAGS})
    detail = f"标签 {total} 个，标准标签 {len(valid)} 个（1–3 个为最佳）"
    if total > 3:
        detail += f"，超过 3 个按 3/{total} 折算"
    if unknown:
        detail += f"；未识别: {', '.join(unknown[:3])}"

    return DimensionScore("标签精度", score, 15.0, detail)


def score_empty_words(data: Mapping[str, Any]) -> DimensionScore:
    """Penalize hollow business jargon in the entry's prose.

    Args:
        data: Decoded knowledge entry.

    Returns:
        A ``DimensionScore`` out of 15. Each distinct hollow term deducts five
        points, down to a floor of zero.
    """
    text = _entry_text(data)
    matches: dict[str, int] = {}

    for term in EMPTY_WORDS_ZH:
        count = text.count(term)
        if count:
            matches[term] = count

    for term, pattern in EN_EMPTY_PATTERNS:
        count = len(pattern.findall(text))
        if count:
            matches[term] = count

    score = max(0.0, 15.0 - 5.0 * len(matches))
    if matches:
        found = "、".join(f"{term}×{count}" for term, count in matches.items())
        detail = f"命中 {len(matches)} 个空洞词：{found}；扣 {len(matches) * 5} 分"
    else:
        detail = "未检测到空洞词"

    return DimensionScore("空洞词检测", score, 15.0, detail)


def evaluate_entry(data: Any, path: Path) -> QualityReport:
    """Evaluate one decoded knowledge entry.

    Args:
        data: Value returned by ``json.loads``.
        path: Source file path, used for reporting.

    Returns:
        A ``QualityReport`` with all five dimensions.
    """
    if not isinstance(data, dict):
        return QualityReport(path, error="顶层必须是 JSON 对象")

    dimensions = (
        score_summary(data),
        score_technical_depth(data),
        score_format(data),
        score_tags(data),
        score_empty_words(data),
    )
    return QualityReport(path, dimensions=dimensions)


def evaluate_file(path: Path) -> QualityReport:
    """Read and evaluate a single JSON file.

    Args:
        path: File to evaluate.

    Returns:
        A ``QualityReport``; read and parse failures produce a C-grade report.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return QualityReport(
            path, error=f"文件不是合法的 UTF-8 编码: {exc.reason}"
        )
    except OSError as exc:
        return QualityReport(path, error=f"无法读取文件: {exc}")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return QualityReport(
            path,
            error=(
                f"JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）: "
                f"{exc.msg}"
            ),
        )

    return evaluate_entry(data, path)


def expand_inputs(patterns: Sequence[str]) -> tuple[list[Path], list[str]]:
    """Expand glob patterns and collect existing input files.

    Args:
        patterns: File paths or glob patterns supplied on the command line.

    Returns:
        A tuple of ``(paths, errors)``. ``paths`` preserves input order and
        contains no duplicates; ``errors`` holds missing or non-file inputs.
    """
    paths: list[Path] = []
    errors: list[str] = []
    seen: set[Path] = set()

    for raw_pattern in patterns:
        has_magic = any(char in raw_pattern for char in "*?[")
        if has_magic:
            matches = sorted(glob.glob(raw_pattern, recursive=True))
            if not matches:
                errors.append(f"{raw_pattern}: 未匹配到任何文件")
                continue
            candidates = [Path(match) for match in matches]
        else:
            candidate = Path(raw_pattern)
            if not candidate.exists():
                errors.append(f"{raw_pattern}: 文件不存在")
                continue
            candidates = [candidate]

        for candidate in candidates:
            if not candidate.is_file():
                errors.append(f"{candidate}: 不是普通文件")
                continue
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            paths.append(candidate)

    return paths, errors


def _progress_bar(score: float, max_score: float, width: int = 20) -> str:
    """Render a textual progress bar for a score."""
    ratio = 0.0 if max_score <= 0 else max(0.0, min(1.0, score / max_score))
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)


def _emit(message: str = "") -> None:
    """Write one line to standard output without using ``print``."""
    sys.stdout.write(f"{message}\n")


def print_report(report: QualityReport) -> None:
    """Print a human-readable quality report.

    Args:
        report: Report to render.
    """
    _emit(f"文件: {report.path}")

    if report.error is not None:
        _emit(f"  错误: {report.error}")
        _emit(f"  总分: {report.total:5.1f}/100  等级: C")
        return

    for dimension in report.dimensions:
        bar = _progress_bar(dimension.score, dimension.max_score)
        _emit(
            f"  {dimension.name:<6} [{bar}] "
            f"{dimension.score:5.1f}/{dimension.max_score:<3.0f} "
            f"{dimension.detail}"
        )

    total_bar = _progress_bar(report.total, 100.0, width=30)
    _emit(
        f"  总分   [{total_bar}] "
        f"{report.total:5.1f}/100  等级: {report.grade}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line quality scorer.

    Args:
        argv: Arguments to parse; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: 0 when no file is grade C, otherwise 1.
    """
    parser = argparse.ArgumentParser(
        description="按五个维度给知识条目 JSON 做质量评分（满分 100）。",
        epilog=(
            "示例:\n"
            "  python hooks/check_quality.py a.json b.json\n"
            '  python hooks/check_quality.py "knowledge/articles/2026-11-25/*.json"'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "files",
        nargs="+",
        metavar="json_file",
        help="待评分的 JSON 文件或通配符模式",
    )
    args = parser.parse_args(argv)

    paths, path_errors = expand_inputs(args.files)
    reports = [evaluate_file(path) for path in paths]

    total_files = len(paths) + len(path_errors)
    grade_counts = {"A": 0, "B": 0, "C": len(path_errors)}
    for report in reports:
        grade_counts[report.grade] += 1

    _emit("知识条目质量评分")
    _emit("=" * 60)

    for error in path_errors:
        _emit(f"[ERROR] {error}")

    for report in reports:
        print_report(report)
        _emit()

    _emit("-" * 60)
    _emit(
        f"汇总: 文件 {total_files} 个"
        f"（A {grade_counts['A']} / B {grade_counts['B']} / C {grade_counts['C']}）"
    )

    has_c_grade = grade_counts["C"] > 0
    return 1 if has_c_grade or total_files == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
