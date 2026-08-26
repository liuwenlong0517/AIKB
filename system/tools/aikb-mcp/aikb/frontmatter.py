"""实现 AIKB Markdown Front Matter 的受限解析和稳定渲染。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class FrontMatterError(ValueError):
    """表示文档不符合 AIKB 支持的 Front Matter 子集。"""

    pass


@dataclass(frozen=True)
class MarkdownDocument:
    """保存解析后的路径、元数据、正文和一级标题。"""

    path: Path
    metadata: dict[str, Any]
    body: str
    title: str


def _scalar(value: str) -> Any:
    """将一个标量文本转换为布尔值、数字、列表或字符串。"""
    value = value.strip()
    if not value:
        return ""
    if value in {"null", "~"}:
        return None
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_scalar(item.strip()) for item in _split_inline(inner)]
    if value.startswith("{") and value.endswith("}"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        if value[0] == '"':
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                pass
        return value[1:-1].replace("''", "'")
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _split_inline(value: str) -> list[str]:
    """按未被引号包围的逗号拆分内联列表，保留列表项内部逗号。"""
    result: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in value:
        if char in {'"', "'"}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            current.append(char)
        elif char == "," and quote is None:
            result.append("".join(current))
            current = []
        else:
            current.append(char)
    result.append("".join(current))
    return result


def parse_yaml_subset(text: str) -> dict[str, Any]:
    """解析 AIKB 使用的刻意受限 YAML 子集；不支持的结构抛出 ``FrontMatterError``。"""
    lines = text.splitlines()
    result: dict[str, Any] = {}
    index = 0
    while index < len(lines):
        raw = lines[index]
        index += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*):(?:\s*(.*))?", raw)
        if not match:
            raise FrontMatterError(f"不支持的 Front Matter 行：{raw}")
        key, value = match.group(1), (match.group(2) or "")
        if value.strip():
            result[key] = _scalar(value)
            continue

        block: list[Any] = []
        while index < len(lines) and (lines[index].startswith("  ") or not lines[index].strip()):
            line = lines[index]
            index += 1
            if not line.strip():
                continue
            item_match = re.fullmatch(r"\s*-\s*(.*)", line)
            if not item_match:
                raise FrontMatterError(f"不支持的嵌套 Front Matter 行：{line}")
            first = item_match.group(1)
            if ":" not in first:
                block.append(_scalar(first))
                continue
            child_key, child_value = first.split(":", 1)
            child: dict[str, Any] = {child_key.strip(): _scalar(child_value)}
            while index < len(lines) and lines[index].startswith("    ") and not lines[index].lstrip().startswith("-"):
                nested = lines[index].strip()
                index += 1
                if ":" not in nested:
                    raise FrontMatterError(f"不支持的关系字段：{nested}")
                nested_key, nested_value = nested.split(":", 1)
                child[nested_key.strip()] = _scalar(nested_value)
            block.append(child)
        result[key] = block
    return result


def parse_markdown(path: Path) -> MarkdownDocument | None:
    """读取带 UTF-8 BOM 容忍度的 Markdown；无 Front Matter 时返回 ``None``。"""
    text = path.read_text(encoding="utf-8-sig")
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return None
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", text, re.DOTALL)
    if not match:
        raise FrontMatterError(f"Front Matter 未闭合：{path}")
    metadata = parse_yaml_subset(match.group(1))
    body = match.group(2).strip() + "\n"
    title_match = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
    if not title_match:
        raise FrontMatterError(f"缺少一级标题：{path}")
    return MarkdownDocument(path=path, metadata=metadata, body=body, title=title_match.group(1).strip())


def yaml_quote(value: Any) -> str:
    """将受支持的 Python 值编码为可被本模块重新解析的 YAML 标量。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def render_frontmatter(metadata: dict[str, Any]) -> str:
    """按稳定字段顺序渲染元数据；复杂列表仅允许非空对象列表。"""
    lines = ["---"]
    for key, value in metadata.items():
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            elif all(not isinstance(item, dict) for item in value):
                rendered = ", ".join(yaml_quote(item) for item in value)
                lines.append(f"{key}: [{rendered}]")
            else:
                lines.append(f"{key}:")
                for item in value:
                    if not isinstance(item, dict) or not item:
                        raise FrontMatterError(f"{key} 只允许非空对象列表")
                    first_key = next(iter(item))
                    lines.append(f"  - {first_key}: {yaml_quote(item[first_key])}")
                    for child_key, child_value in list(item.items())[1:]:
                        lines.append(f"    {child_key}: {yaml_quote(child_value)}")
        else:
            lines.append(f"{key}: {yaml_quote(value)}")
    lines.append("---")
    return "\n".join(lines)
