from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .config import Settings
from .frontmatter import parse_markdown
from .indexer import ensure_knowledge_index, split_sections


def _clip(text: str, limit: int) -> tuple[str, bool]:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= limit:
        return normalized, False
    return normalized[: max(0, limit - 1)].rstrip() + "…", True


def _tags(connection: sqlite3.Connection, document_id: str) -> list[str]:
    return [row[0] for row in connection.execute("SELECT tag FROM tags WHERE document_id = ? ORDER BY tag", (document_id,))]


def _relations(connection: sqlite3.Connection, document_id: str) -> list[dict[str, str]]:
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
    def __init__(self, settings: Settings):
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
            path = (self.settings.repo_root / row["path"]).resolve()
            try:
                path.relative_to(self.settings.content_root.resolve())
            except ValueError as exc:
                raise RuntimeError("索引路径越过 content/ 边界") from exc
            document = parse_markdown(path)
            if document is None:
                raise RuntimeError(f"知识文件缺少 Front Matter：{row['path']}")
            if section:
                matches = [
                    (heading, content)
                    for heading, content, _ in split_sections(document.body, document.title)
                    if section.lower() in heading.lower()
                ]
                if not matches:
                    raise KeyError(f"未找到章节：{section}")
                selected = "\n\n".join(f"## {heading}\n\n{content}" for heading, content in matches)
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
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
