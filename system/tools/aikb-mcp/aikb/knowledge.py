"""提供基于 SQLite 派生索引、回读当前 Markdown 的知识服务。"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path, PurePosixPath
from typing import Any

from .config import Settings
from .frontmatter import parse_markdown
from .indexer import ensure_knowledge_index


_MARKDOWN_HEADING = re.compile(r"^(#{2,6})\s+(.+?)\s*$")


def _clip(text: str, limit: int) -> tuple[str, bool]:
    """压缩连续空白并按字符预算截断，返回正文和是否截断标记。"""
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= limit:
        return normalized, False
    return normalized[: max(0, limit - 1)].rstrip() + "…", True


def _select_section(body: str, section: str) -> str:
    """选择匹配标题及其子章节；没有匹配章节时抛出 ``KeyError``。"""
    lines = body.splitlines()
    headings: list[tuple[int, int, str]] = []
    for line_number, line in enumerate(lines):
        match = _MARKDOWN_HEADING.match(line)
        if match:
            headings.append((line_number, len(match.group(1)), match.group(2).strip()))

    selected_ranges: list[tuple[int, int]] = []
    query = section.casefold()
    for position, (start, level, title) in enumerate(headings):
        if query not in title.casefold():
            continue
        end = len(lines)
        for next_start, next_level, _ in headings[position + 1 :]:
            if next_level <= level:
                end = next_start
                break
        if selected_ranges and start < selected_ranges[-1][1]:
            continue
        selected_ranges.append((start, end))

    if not selected_ranges:
        raise KeyError(f"未找到章节：{section}")
    return "\n\n".join("\n".join(lines[start:end]).strip() for start, end in selected_ranges)


def _tags(connection: sqlite3.Connection, document_id: str) -> list[str]:
    """按字典序读取文档标签。"""
    return [row[0] for row in connection.execute("SELECT tag FROM tags WHERE document_id = ? ORDER BY tag", (document_id,))]


def _relations(connection: sqlite3.Connection, document_id: str) -> list[dict[str, str]]:
    """同时读取文档的出边和入边关系，供客户端导航。"""
    rows = connection.execute(
        """
        SELECT 'outgoing', relation_type, target_id FROM relations WHERE source_id = ?
        UNION ALL
        SELECT 'incoming', relation_type, source_id FROM relations WHERE target_id = ?
        ORDER BY 1, 2, 3
        """,
        (document_id, document_id),
    )
    return [{"direction": direction, "type": rel_type, "target": target} for direction, rel_type, target in rows]


class KnowledgeService:
    """执行知识搜索和安全回读；数据库定位，Markdown 内容负责最终事实。"""

    def __init__(self, settings: Settings):
        """绑定本机路径设置，不在初始化阶段提前建立索引。"""
        self.settings = settings

    def search(
        self,
        query: str,
        *,
        entry_type: str | None = None,
        status: str = "verified",
        tags: list[str] | None = None,
        limit: int = 5,
        excerpt_chars: int = 700,
    ) -> dict[str, Any]:
        """合并元数据 LIKE 与 FTS 结果，并按分数、标签和数量预算返回候选。"""
        query = query.strip()
        if not query:
            raise ValueError("query 不能为空")
        limit = max(1, min(int(limit), 20))
        excerpt_chars = max(120, min(int(excerpt_chars), 1600))
        requested_tags = [tag.strip().lower() for tag in (tags or []) if tag.strip()]
        index_status = ensure_knowledge_index(self.settings)
        candidates: dict[str, dict[str, Any]] = {}
        connection = sqlite3.connect(self.settings.knowledge_db)
        try:
            connection.row_factory = sqlite3.Row
            filters = ["d.status = ?"]
            params: list[Any] = [status]
            if entry_type:
                filters.append("d.type = ?")
                params.append(entry_type)
            where = " AND ".join(filters)

            like = f"%{query.lower()}%"
            rows = connection.execute(
                f"""
                SELECT d.*, c.section, c.content
                FROM documents d
                JOIN chunks c ON c.document_id = d.id
                WHERE {where}
                  AND (lower(d.id) LIKE ? OR lower(d.title) LIKE ? OR lower(d.path) LIKE ? OR lower(c.content) LIKE ?
                       OR EXISTS (SELECT 1 FROM tags t WHERE t.document_id = d.id AND lower(t.tag) LIKE ?))
                ORDER BY CASE WHEN lower(d.id) = ? OR lower(d.title) = ? THEN 0 ELSE 1 END,
                         c.chunk_order
                LIMIT 100
                """,
                (*params, like, like, like, like, like, query.lower(), query.lower()),
            )
            for row in rows:
                if row["id"] in candidates:
                    continue
                candidates[row["id"]] = self._candidate(connection, row, 4.0, "metadata", excerpt_chars)

            tokenizer = index_status.get("tokenizer")
            if tokenizer != "trigram" or len(query) >= 3:
                fts_query = '"' + query.replace('"', '""') + '"'
                try:
                    fts_rows = connection.execute(
                        f"""
                        SELECT d.*, c.section, c.content, bm25(chunks_fts, 0.0, 3.0, 2.0, 1.0, 2.0, 1.0) AS rank
                        FROM chunks_fts
                        JOIN chunks c ON c.id = chunks_fts.rowid
                        JOIN documents d ON d.id = c.document_id
                        WHERE chunks_fts MATCH ? AND {where}
                        ORDER BY rank
                        LIMIT 100
                        """,
                        (fts_query, *params),
                    )
                    for row in fts_rows:
                        score = 2.0 + (1.0 / (1.0 + abs(float(row["rank"]))))
                        candidate = self._candidate(connection, row, score, "fts", excerpt_chars)
                        previous = candidates.get(row["id"])
                        if previous is None or candidate["score"] > previous["score"]:
                            candidates[row["id"]] = candidate
                except sqlite3.OperationalError:
                    pass

            results = []
            for candidate in sorted(candidates.values(), key=lambda item: (-item["score"], item["title"])):
                if requested_tags and not set(requested_tags).issubset({tag.lower() for tag in candidate["tags"]}):
                    continue
                candidate["score"] = round(candidate["score"], 4)
                results.append(candidate)
                if len(results) >= limit:
                    break
        finally:
            connection.close()
        return {
            "query": query,
            "count": len(results),
            "results": results,
            "index": {"tokenizer": index_status.get("tokenizer"), "rebuilt": bool(index_status.get("rebuilt", False))},
        }

    def _candidate(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        score: float,
        matched_by: str,
        excerpt_chars: int,
    ) -> dict[str, Any]:
        """把索引行转换为带摘要、标签和匹配来源的搜索候选。"""
        excerpt, truncated = _clip(row["content"], excerpt_chars)
        return {
            "id": row["id"],
            "title": row["title"],
            "type": row["type"],
            "status": row["status"],
            "path": row["path"],
            "section": row["section"],
            "tags": _tags(connection, row["id"]),
            "score": score,
            "matched_by": matched_by,
            "excerpt": excerpt,
            "truncated": truncated,
            "content_hash": row["content_hash"],
        }

    def read(
        self,
        identifier: str,
        *,
        section: str | None = None,
        max_chars: int = 4000,
        include_relations: bool = True,
    ) -> dict[str, Any]:
        """按稳定 ID 或逻辑路径读取当前 Markdown，可选返回章节和关系。"""
        identifier = identifier.strip()
        if not identifier:
            raise ValueError("id_or_path 不能为空")
        max_chars = max(300, min(int(max_chars), 12000))
        ensure_knowledge_index(self.settings)
        connection = sqlite3.connect(self.settings.knowledge_db)
        try:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM documents WHERE id = ? OR path = ?",
                (identifier, identifier.replace("\\", "/")),
            ).fetchone()
            if row is None:
                raise KeyError(f"未找到知识：{identifier}")
            logical_path = PurePosixPath(str(row["path"]).replace("\\", "/"))
            parts = logical_path.parts[1:] if logical_path.parts[:1] == ("content",) else logical_path.parts
            path = self.settings.content_root.joinpath(*parts).resolve()
            try:
                path.relative_to(self.settings.content_root.resolve())
            except ValueError as exc:
                raise RuntimeError("索引路径越过 content/ 边界") from exc
            document = parse_markdown(path)
            if document is None:
                raise RuntimeError(f"知识文件缺少 Front Matter：{row['path']}")
            if section:
                selected = _select_section(document.body, section)
            else:
                selected = document.body
            content, truncated = _clip(selected, max_chars)
            result: dict[str, Any] = {
                "id": row["id"],
                "title": row["title"],
                "type": row["type"],
                "status": row["status"],
                "path": row["path"],
                "tags": _tags(connection, row["id"]),
                "applicable_versions": row["applicable_versions"],
                "last_verified": row["last_verified"],
                "content_hash": row["content_hash"],
                "content": content,
                "truncated": truncated,
            }
            if include_relations:
                result["relations"] = _relations(connection, row["id"])
            return result
        finally:
            connection.close()


def compact_json(value: Any) -> str:
    """使用无空白 JSON 编码 MCP 客户端可见结果。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
