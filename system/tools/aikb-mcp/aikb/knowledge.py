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


def _clip_markdown(text: str, limit: int) -> tuple[str, bool]:
    """保留 Markdown 换行和块结构，并按 Web 响应预算安全截断。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
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


def _bounded_int(value: int, *, name: str, minimum: int, maximum: int) -> int:
    """把分页参数转换为有界整数；非法类型抛出 ``ValueError``，合法越界值取边界。"""
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    return max(minimum, min(number, maximum))


def _logical_path(value: str) -> str:
    """校验索引中的路径仍是 ``content/...`` 逻辑路径，拒绝物理路径和越界片段。"""
    path = str(value).replace("\\", "/").strip()
    parsed = PurePosixPath(path)
    if not path or parsed.is_absolute() or ".." in parsed.parts or parsed.parts[:1] != ("content",):
        raise RuntimeError(f"索引包含非法知识逻辑路径：{value}")
    return path


class KnowledgeService:
    """提供知识搜索、详情回读及 Web 目录只读查询；数据库定位，Markdown 负责最终事实。

    Web 目录查询仅消费索引中的 ``verified`` 条目，并把知识仓物理位置收敛为
    ``content/...`` 逻辑路径；详情 ``read`` 保持原有 MCP 能力和显式条目语义。
    """

    def __init__(self, settings: Settings):
        """绑定本机路径设置，不在初始化阶段提前建立索引。"""
        self.settings = settings

    def _document_item(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        """将索引行转换为 Web 可复用的目录/最近条目 DTO，不回读或暴露物理路径。"""
        return {
            "id": row["id"],
            "title": row["title"],
            "type": row["type"],
            "status": row["status"],
            "path": _logical_path(row["path"]),
            "tags": _tags(connection, row["id"]),
            "summary": row["summary"],
            "last_verified": row["last_verified"],
            "content_hash": row["content_hash"],
        }

    def list_documents(
        self,
        *,
        path_prefix: str | None = None,
        entry_type: str | None = None,
        tag: str | None = None,
        sort: str = "recent",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """分页列出已验证知识条目，供目录树和条目列表复用。

        ``path_prefix`` 只能使用 ``content/...`` 逻辑路径；``sort`` 支持
        ``recent``（按 ``last_verified`` 倒序）、``title`` 和 ``path``。结果始终
        固定过滤 ``status = verified``，不会因为调用方传入条件而扩大可见范围。
        ``limit`` 被限制在 1..100，``offset`` 被限制在 0..100000；越界值取边界。
        返回的 ``count`` 是本页数量，``total`` 是筛选后的总数量。
        """
        limit = _bounded_int(limit, name="limit", minimum=1, maximum=100)
        offset = _bounded_int(offset, name="offset", minimum=0, maximum=100_000)
        prefix = self._normalize_path_prefix(path_prefix)
        entry_type = self._normalize_filter(entry_type, name="entry_type")
        tag = self._normalize_filter(tag, name="tag")
        if sort not in {"recent", "title", "path"}:
            raise ValueError("sort 必须是 recent、title 或 path")

        index_status = ensure_knowledge_index(self.settings)
        connection = sqlite3.connect(self.settings.knowledge_db)
        try:
            connection.row_factory = sqlite3.Row
            filters = ["d.status = 'verified'"]
            params: list[Any] = []
            if prefix:
                filters.append("(d.path = ? OR d.path LIKE ?)")
                params.extend([prefix, prefix + "/%"])
            if entry_type:
                filters.append("d.type = ?")
                params.append(entry_type)
            if tag:
                filters.append("EXISTS (SELECT 1 FROM tags tf WHERE tf.document_id = d.id AND lower(tf.tag) = lower(?))")
                params.append(tag)
            where = " AND ".join(filters)
            total = int(connection.execute(f"SELECT COUNT(*) FROM documents d WHERE {where}", params).fetchone()[0])
            order_by = {
                "recent": "d.last_verified DESC, d.path ASC",
                "title": "d.title COLLATE NOCASE ASC, d.path ASC",
                "path": "d.path ASC",
            }[sort]
            rows = connection.execute(
                f"SELECT d.* FROM documents d WHERE {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            documents = [self._document_item(connection, row) for row in rows]
        finally:
            connection.close()
        return {
            "documents": documents,
            "count": len(documents),
            "total": total,
            "limit": limit,
            "offset": offset,
            "sort": sort,
            "path_prefix": prefix,
            "index": {"tokenizer": index_status.get("tokenizer"), "rebuilt": bool(index_status.get("rebuilt", False))},
        }

    def list_tags(self, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """列出已验证条目的标签及出现次数，供筛选器复用。

        标签只从 ``status = verified`` 的索引文档统计；结果按出现次数倒序、标签
        字典序稳定排序。``limit`` 被限制在 1..200，``offset`` 被限制在 0..100000。
        """
        limit = _bounded_int(limit, name="limit", minimum=1, maximum=200)
        offset = _bounded_int(offset, name="offset", minimum=0, maximum=100_000)
        index_status = ensure_knowledge_index(self.settings)
        connection = sqlite3.connect(self.settings.knowledge_db)
        try:
            connection.row_factory = sqlite3.Row
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM (SELECT t.tag FROM tags t JOIN documents d ON d.id = t.document_id WHERE d.status = 'verified' GROUP BY t.tag)"
                ).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT t.tag, COUNT(*) AS count
                FROM tags t JOIN documents d ON d.id = t.document_id
                WHERE d.status = 'verified'
                GROUP BY t.tag
                ORDER BY count DESC, t.tag COLLATE NOCASE ASC, t.tag ASC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            tags = [{"tag": row["tag"], "count": int(row["count"])} for row in rows]
        finally:
            connection.close()
        return {
            "tags": tags,
            "count": len(tags),
            "total": total,
            "limit": limit,
            "offset": offset,
            "index": {"tokenizer": index_status.get("tokenizer"), "rebuilt": bool(index_status.get("rebuilt", False))},
        }

    def overview(self, *, recent_limit: int = 10) -> dict[str, Any]:
        """从现有索引生成已验证知识总览、类型/标签统计、目录树和最近条目。

        ``recent_limit`` 被限制在 1..20；最近的定义是索引中的
        ``last_verified`` 日期倒序，而不是读取知识仓文件系统的修改时间。目录树
        只包含已验证条目的 ``content/...`` 逻辑目录，不携带知识仓绝对路径。
        """
        recent_limit = _bounded_int(recent_limit, name="recent_limit", minimum=1, maximum=20)
        index_status = ensure_knowledge_index(self.settings)
        connection = sqlite3.connect(self.settings.knowledge_db)
        try:
            connection.row_factory = sqlite3.Row
            total = int(connection.execute("SELECT COUNT(*) FROM documents WHERE status = 'verified'").fetchone()[0])
            type_rows = connection.execute(
                "SELECT type, COUNT(*) AS count FROM documents WHERE status = 'verified' GROUP BY type ORDER BY type"
            )
            by_type = {row["type"]: int(row["count"]) for row in type_rows}
            tag_rows = connection.execute(
                """
                SELECT t.tag, COUNT(*) AS count
                FROM tags t JOIN documents d ON d.id = t.document_id
                WHERE d.status = 'verified'
                GROUP BY t.tag
                ORDER BY count DESC, t.tag COLLATE NOCASE ASC, t.tag ASC
                """
            )
            by_tag = [{"tag": row["tag"], "count": int(row["count"])} for row in tag_rows]
            recent_rows = connection.execute(
                "SELECT d.* FROM documents d WHERE d.status = 'verified' ORDER BY d.last_verified DESC, d.path ASC LIMIT ?",
                (recent_limit,),
            )
            recent = [self._document_item(connection, row) for row in recent_rows]
            tree_rows = connection.execute("SELECT path FROM documents WHERE status = 'verified' ORDER BY path").fetchall()
            tree = self._directory_tree([_logical_path(row["path"]) for row in tree_rows])
        finally:
            connection.close()
        return {
            "document_count": total,
            "by_type": by_type,
            "by_tag": by_tag,
            "directory_tree": tree,
            "recent_documents": recent,
            "recent_limit": recent_limit,
            "index": {"tokenizer": index_status.get("tokenizer"), "rebuilt": bool(index_status.get("rebuilt", False))},
        }

    @staticmethod
    def _normalize_filter(value: str | None, *, name: str) -> str | None:
        """规范可选文本过滤器并限制长度，空字符串按未指定处理。"""
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{name} 必须是字符串")
        value = value.strip()
        if len(value) > 200:
            raise ValueError(f"{name} 过长")
        return value or None

    @classmethod
    def _normalize_path_prefix(cls, value: str | None) -> str | None:
        """规范目录过滤器，只接受 ``content/...`` 逻辑路径并拒绝绝对路径。"""
        value = cls._normalize_filter(value, name="path_prefix")
        if value is None:
            return None
        normalized = value.replace("\\", "/").rstrip("/")
        if normalized != "content" and not normalized.startswith("content/"):
            raise ValueError("path_prefix 必须是 content/... 逻辑路径")
        return _logical_path(normalized)

    @staticmethod
    def _directory_tree(paths: list[str]) -> dict[str, Any]:
        """把逻辑文档路径折叠为稳定目录树，节点计数包含所有后代文档。"""
        root: dict[str, Any] = {"name": "content", "path": "content", "document_count": 0, "children": []}
        nodes: dict[str, dict[str, Any]] = {"content": root}
        for path in paths:
            parts = PurePosixPath(path).parts
            current_path = "content"
            root["document_count"] += 1
            for part in parts[1:-1]:
                current_path += "/" + part
                node = nodes.get(current_path)
                if node is None:
                    node = {"name": part, "path": current_path, "document_count": 0, "children": []}
                    nodes[current_path] = node
                    parent_path = current_path.rsplit("/", 1)[0]
                    nodes[parent_path]["children"].append(node)
                node["document_count"] += 1

        def sort_children(node: dict[str, Any]) -> None:
            node["children"].sort(key=lambda child: child["name"].casefold())
            for child in node["children"]:
                sort_children(child)

        sort_children(root)
        return root

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
        """按 MCP 上下文预算读取 Markdown，正文最多返回 12000 字符。"""
        return self._read_document(
            identifier,
            section=section,
            max_chars=max_chars,
            maximum_chars=12_000,
            preserve_markdown=False,
            include_relations=include_relations,
        )

    def read_document(
        self,
        identifier: str,
        *,
        section: str | None = None,
        max_chars: int = 500_000,
        include_relations: bool = True,
    ) -> dict[str, Any]:
        """为本地 Web 阅读器回读较完整正文，并以 500000 字符作为响应安全上限。"""
        return self._read_document(
            identifier,
            section=section,
            max_chars=max_chars,
            maximum_chars=500_000,
            preserve_markdown=True,
            include_relations=include_relations,
        )

    def _read_document(
        self,
        identifier: str,
        *,
        section: str | None,
        max_chars: int,
        maximum_chars: int,
        preserve_markdown: bool,
        include_relations: bool,
    ) -> dict[str, Any]:
        """执行共享安全回读；调用方只选择响应预算，不改变路径和事实源边界。"""
        identifier = identifier.strip()
        if not identifier:
            raise ValueError("id_or_path 不能为空")
        max_chars = max(300, min(int(max_chars), maximum_chars))
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
            content, truncated = (_clip_markdown if preserve_markdown else _clip)(selected, max_chars)
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
