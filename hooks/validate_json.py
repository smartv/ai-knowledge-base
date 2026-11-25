#!/usr/bin/env python3
"""Validate knowledge-entry JSON files.

Usage:
    python hooks/validate_json.py <json_file> [json_file2 ...]
    python hooks/validate_json.py "knowledge/articles/2026-11-25/*.json"

The script checks required fields and types, ID format, status, URL, summary
length, tags, and optional score/audience fields. It exits with 0 when every
input is valid, otherwise 1.
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

REQUIRED_FIELDS: dict[str, type] = {
    "id": str,
    "title": str,
    "source_url": str,
    "summary": str,
    "tags": list,
    "status": str,
}

VALID_STATUSES = frozenset({"draft", "review", "published", "archived"})
VALID_AUDIENCES = frozenset({"beginner", "intermediate", "advanced"})

ID_PATTERN = re.compile(
    r"^(?P<source>[a-z][a-z0-9_]*(?:-[a-z0-9_]+)*)"
    r"-(?P<date>\d{8})-(?P<seq>\d{3})$"
)

SUMMARY_MIN_LENGTH = 20
SCORE_MIN = 1
SCORE_MAX = 10


@dataclass(frozen=True)
class ValidationError:
    """A single validation failure.

    Attributes:
        field: Field name, or a pseudo-field such as ``json`` or ``file``.
        message: Human-readable explanation.
    """

    field: str
    message: str

    def __str__(self) -> str:
        """Render the error as ``field: message``."""
        if not self.field:
            return self.message
        return f"{self.field}: {self.message}"


@dataclass(frozen=True)
class FileResult:
    """Validation outcome for one input file.

    Attributes:
        path: File that was validated.
        errors: Validation failures found in the file.
    """

    path: Path
    errors: list[ValidationError]

    @property
    def ok(self) -> bool:
        """Return whether the file passed every check."""
        return not self.errors


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


def _validate_required_fields(data: Mapping[str, Any]) -> list[ValidationError]:
    """Check that every required field exists and has the expected type."""
    errors: list[ValidationError] = []
    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in data:
            errors.append(ValidationError(field, "缺少必填字段"))
            continue
        value = data[field]
        if not isinstance(value, expected_type):
            errors.append(
                ValidationError(
                    field,
                    f"类型应为 {expected_type.__name__}，实际为 {type(value).__name__}",
                )
            )
    return errors


def _validate_id(value: str) -> list[ValidationError]:
    """Validate the ``{source}-{YYYYMMDD}-{NNN}`` identifier format."""
    match = ID_PATTERN.fullmatch(value)
    if match is None:
        return [
            ValidationError(
                "id",
                "格式应为 {source}-{YYYYMMDD}-{NNN}，"
                f"例如 github-20260317-001，实际为 {value!r}",
            )
        ]

    try:
        datetime.strptime(match.group("date"), "%Y%m%d")
    except ValueError:
        return [ValidationError("id", f"日期部分 {match.group('date')} 不是有效日期")]

    return []


def _validate_status(value: str) -> list[ValidationError]:
    """Validate the entry status against the allowed enum."""
    if value not in VALID_STATUSES:
        allowed = " / ".join(sorted(VALID_STATUSES))
        return [ValidationError("status", f"应为 {allowed} 之一，实际为 {value!r}")]
    return []


def _validate_url(value: str) -> list[ValidationError]:
    """Validate that the value is an absolute HTTP(S) URL."""
    if any(char.isspace() for char in value):
        return [ValidationError("source_url", "URL 不能包含空白字符")]

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return [
            ValidationError(
                "source_url",
                f"应为 http(s):// 开头的绝对 URL，实际为 {value!r}",
            )
        ]
    return []


def _validate_summary(value: str) -> list[ValidationError]:
    """Validate the minimum summary length after trimming whitespace."""
    length = len(value.strip())
    if length < SUMMARY_MIN_LENGTH:
        return [
            ValidationError(
                "summary",
                f"至少需要 {SUMMARY_MIN_LENGTH} 个字符，当前 {length} 个",
            )
        ]
    return []


def _validate_tags(value: list[Any]) -> list[ValidationError]:
    """Validate that tags is a non-empty list of non-empty strings."""
    errors: list[ValidationError] = []
    if not value:
        errors.append(ValidationError("tags", "至少需要 1 个标签"))
        return errors

    for index, tag in enumerate(value):
        if not isinstance(tag, str) or not tag.strip():
            errors.append(
                ValidationError(f"tags[{index}]", "标签必须是非空字符串")
            )
    return errors


def _validate_score(value: Any) -> list[ValidationError]:
    """Validate the optional score as a number between 1 and 10."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return [ValidationError("score", "应为 1-10 之间的数字")]
    if not SCORE_MIN <= value <= SCORE_MAX:
        return [
            ValidationError(
                "score",
                f"应在 {SCORE_MIN}-{SCORE_MAX} 之间，实际为 {value}",
            )
        ]
    return []


def _validate_audience(value: Any) -> list[ValidationError]:
    """Validate the optional audience against the allowed enum."""
    if not isinstance(value, str) or value not in VALID_AUDIENCES:
        allowed = " / ".join(sorted(VALID_AUDIENCES))
        return [ValidationError("audience", f"应为 {allowed} 之一，实际为 {value!r}")]
    return []


def validate_entry(data: Any) -> list[ValidationError]:
    """Validate one decoded knowledge entry.

    Args:
        data: Value returned by ``json.loads``.

    Returns:
        A list of validation errors; empty means the entry is valid.
    """
    if not isinstance(data, dict):
        return [ValidationError("", "顶层必须是 JSON 对象")]

    errors = _validate_required_fields(data)

    entry_id = data.get("id")
    if isinstance(entry_id, str):
        errors.extend(_validate_id(entry_id))

    status = data.get("status")
    if isinstance(status, str):
        errors.extend(_validate_status(status))

    source_url = data.get("source_url")
    if isinstance(source_url, str):
        errors.extend(_validate_url(source_url))

    summary = data.get("summary")
    if isinstance(summary, str):
        errors.extend(_validate_summary(summary))

    tags = data.get("tags")
    if isinstance(tags, list):
        errors.extend(_validate_tags(tags))

    if "score" in data:
        errors.extend(_validate_score(data["score"]))

    if "audience" in data:
        errors.extend(_validate_audience(data["audience"]))

    return errors


def validate_file(path: Path) -> FileResult:
    """Read and validate a single JSON file.

    Args:
        path: File to validate.

    Returns:
        A ``FileResult`` containing any read, parse, or schema errors.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return FileResult(
            path,
            [ValidationError("file", f"文件不是合法的 UTF-8 编码: {exc.reason}")],
        )
    except OSError as exc:
        return FileResult(path, [ValidationError("file", f"无法读取文件: {exc}")])

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return FileResult(
            path,
            [
                ValidationError(
                    "json",
                    f"JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）: {exc.msg}",
                )
            ],
        )

    return FileResult(path, validate_entry(data))


def _emit(message: str = "") -> None:
    """Write one line to standard output without using ``print``."""
    sys.stdout.write(f"{message}\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line validator.

    Args:
        argv: Arguments to parse; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: 0 when all files pass, 1 otherwise.
    """
    parser = argparse.ArgumentParser(
        description="校验知识条目 JSON 的字段、ID、状态、URL 与内容质量。",
        epilog=(
            "示例:\n"
            "  python hooks/validate_json.py a.json b.json\n"
            '  python hooks/validate_json.py "knowledge/articles/2026-11-25/*.json"'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "files",
        nargs="+",
        metavar="json_file",
        help="待校验的 JSON 文件或通配符模式",
    )
    args = parser.parse_args(argv)

    paths, path_errors = expand_inputs(args.files)
    results = [validate_file(path) for path in paths]

    total_files = len(paths) + len(path_errors)
    passed = sum(1 for result in results if result.ok)
    failed = total_files - passed
    total_errors = sum(len(result.errors) for result in results) + len(path_errors)

    _emit("JSON 知识条目校验")
    _emit("=" * 48)

    for error in path_errors:
        _emit(f"[FAIL] {error}")

    for result in results:
        if result.ok:
            _emit(f"[PASS] {result.path}")
            continue
        _emit(f"[FAIL] {result.path}")
        for error in result.errors:
            _emit(f"  - {error}")

    _emit("-" * 48)
    _emit(
        f"汇总: 文件 {total_files} 个（通过 {passed}，失败 {failed}），"
        f"错误 {total_errors} 条"
    )

    return 0 if failed == 0 and total_errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
