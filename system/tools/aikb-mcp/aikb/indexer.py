"""扫描知识仓 Markdown，并构建可重建的 SQLite/FTS 派生索引。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import Settings
from .frontmatter import FrontMatterError, MarkdownDocument, parse_markdown


SCHEMA_VERSION = "1"
PARSER_VERSION = "1"
ALLOWED_TYPES = {"knowledge", "solution", "pitfall", "decision", "workflow", "project-memory", "candidate"}
ALLOWED_STATUS = {"verified", "deprecated", "candidate"}
ALLOWED_RELATIONS = {"related_to", "depends_on", "implements", "supersedes", "verified_by", "applies_to", "part_of"}
REQUIRED_FIELDS = {
    "id",
    "type",
    "status",
    "tags",
    "relations",
}
FORMAL_REQUIRED_FIELDS = {"applicable_versions", "last_verified", "review_when"}
CONTENT_DIRECTORIES = ("knowledge", "experience", "workflows", "projects")


@dataclass(frozen=True)
class IndexedDocument:
    """表示已通过解析的文档及其逻辑路径、指纹、摘要和章节块。"""

    document: MarkdownDocument
    relative_path: str
    content_hash: str
    summary: str
    chunks: list[tuple[str, str, int]]


def iter_content_files(content_root: Path) -> Iterable[Path]:
    """只遍历知识分类正文，避免进入嵌套仓库的 ``.git`` 和导航文件。"""
    for directory_name in CONTENT_DIRECTORIES:
        directory = content_root / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.md")):
            if ".git" not in path.parts and path.name.lower() != "readme.md":
                yield path


def _content_hash(path: Path) -> str:
    """计算单个文件的 SHA-256 内容指纹。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def content_fingerprint(content_root: Path) -> str:
    """按逻辑相对路径和文件指纹计算知识仓整体指纹。"""
    digest = hashlib.sha256()
    for path in iter_content_files(content_root):
        relative = path.relative_to(content_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_content_hash(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _plain_summary(body: str, title: str) -> str:
    """从正文提取去除 Markdown 标记的首段摘要，最长 600 字符。"""
    paragraphs = re.split(r"\n\s*\n", body)
    for paragraph in paragraphs:
        text = paragraph.strip()
        if not text or text.startswith("#") or text.startswith("<!--") or text.startswith("```"):
            continue
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"[`*_>]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text and text != title:
            return text[:600]
    return title


def split_sections(body: str, title: str) -> list[tuple[str, str, int]]:
    """按二级/三级标题切分正文，保留章节名与稳定顺序。"""
    heading = title
    buffer: list[str] = []
    chunks: list[tuple[str, str, int]] = []
    order = 0
    for line in body.splitlines():
        match = re.match(r"^#{2,3}\s+(.+?)\s*$", line)
        if match:
            content = "\n".join(buffer).strip()
            if content:
                chunks.append((heading, content, order))
                order += 1
            heading = match.group(1).strip()
            buffer = []
        elif not line.startswith("# "):
            buffer.append(line)
    content = "\n".join(buffer).strip()
    if content:
        chunks.append((heading, content, order))
    return chunks or [(title, body.strip(), 0)]


def _list_of_strings(value: Any) -> bool:
    """判断值是否为非空字符串组成的列表。"""
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def validate_document(document: MarkdownDocument, content_root: Path) -> list[str]:
    """校验单篇知识的元数据、关系格式和正式条目字段，返回全部错误。"""
    metadata = document.metadata
    relative = document.path.relative_to(content_root).as_posix()
    errors: list[str] = []
    missing = sorted(REQUIRED_FIELDS - metadata.keys())
    if metadata.get("status") != "candidate":
        missing.extend(sorted(FORMAL_REQUIRED_FIELDS - metadata.keys()))
    if missing:
        errors.append(f"{relative}: 缺少字段 {', '.join(missing)}")
    entry_id = metadata.get("id")
    if not isinstance(entry_id, str) or not re.fullmatch(r"aikb:[a-z0-9][a-z0-9:-]*", entry_id):
        errors.append(f"{relative}: id 必须是稳定的 aikb: 小写标识")
    if metadata.get("type") not in ALLOWED_TYPES:
        errors.append(f"{relative}: type 不受支持：{metadata.get('type')}")
    status = metadata.get("status")
    if status not in ALLOWED_STATUS:
        errors.append(f"{relative}: status 不受支持：{status}")
    if metadata.get("type") == "candidate" and status != "candidate":
        errors.append(f"{relative}: candidate 类型必须使用 candidate 状态")
    tags = metadata.get("tags")
    if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
        errors.append(f"{relative}: tags 必须是字符串列表")
    relations = metadata.get("relations")
    if not isinstance(relations, list):
        errors.append(f"{relative}: relations 必须是列表")
    else:
        for relation in relations:
            if not isinstance(relation, dict):
                errors.append(f"{relative}: relation 必须是对象")
                continue
            if relation.get("type") not in ALLOWED_RELATIONS:
                errors.append(f"{relative}: relation type 不受支持：{relation.get('type')}")
            target = relation.get("target")
            if not isinstance(target, str) or not target.startswith("aikb:"):
                errors.append(f"{relative}: relation target 必须是 aikb: 标识")
    last_verified = metadata.get("last_verified")
    if status != "candidate" and (not isinstance(last_verified, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", last_verified)):
        errors.append(f"{relative}: 正式条目的 last_verified 必须是 YYYY-MM-DD")
    return errors


def load_documents(settings: Settings) -> tuple[list[IndexedDocument], list[str]]:
    """加载并校验全部知识文档，同时检查稳定 ID 与关系目标是否重复或缺失。"""
    documents: list[IndexedDocument] = []
    errors: list[str] = []
    ids: dict[str, str] = {}
    for path in iter_content_files(settings.content_root):
        try:
            parsed = parse_markdown(path)
        except (OSError, UnicodeError, FrontMatterError) as exc:
            errors.append(str(exc))
            continue
        if parsed is None:
            continue
        errors.extend(validate_document(parsed, settings.content_root))
        # 数据库继续暴露稳定的 content/... 逻辑路径，不泄漏知识仓物理位置。
        relative = "content/" + path.relative_to(settings.content_root).as_posix()
        entry_id = parsed.metadata.get("id")
        if isinstance(entry_id, str):
            if entry_id in ids:
                errors.append(f"ID 重复：{entry_id} -> {ids[entry_id]}, {relative}")
            else:
                ids[entry_id] = relative
        documents.append(
            IndexedDocument(
                document=parsed,
                relative_path=relative,
                content_hash=_content_hash(path),
                summary=_plain_summary(parsed.body, parsed.title),
                chunks=split_sections(parsed.body, parsed.title),
            )
        )
    known_ids = set(ids)
    for item in documents:
        if item.document.metadata.get("status") == "candidate":
            continue
        for relation in item.document.metadata.get("relations", []):
            if isinstance(relation, dict) and relation.get("target") not in known_ids:
                errors.append(f"{item.relative_path}: 关系目标不存在：{relation.get('target')}")
    return documents, errors


def _create_schema(connection: sqlite3.Connection) -> str:
    """创建索引表和 FTS 表；优先使用 trigram，失败时回退到 unicode61。"""
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE index_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE documents (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            type TEXT NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            applicable_versions TEXT NOT NULL,
            last_verified TEXT,
            review_when TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            body TEXT NOT NULL
        );
        CREATE TABLE tags (
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            tag TEXT NOT NULL,
            PRIMARY KEY (document_id, tag)
        );
        CREATE TABLE relations (
            source_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            relation_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            PRIMARY KEY (source_id, relation_type, target_id)
        );
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            section TEXT NOT NULL,
            content TEXT NOT NULL,
            chunk_order INTEGER NOT NULL
        );
        CREATE INDEX idx_documents_type_status ON documents(type, status);
        CREATE INDEX idx_tags_tag ON tags(tag);
        CREATE INDEX idx_relations_target ON relations(target_id, relation_type);
        """
    )
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE chunks_fts USING fts5(document_id UNINDEXED, title, section, content, tags, path, tokenize='trigram')"
        )
        return "trigram"
    except sqlite3.OperationalError:
        # 发行版 SQLite 可能未编译 trigram tokenizer，unicode61 是兼容性兜底。
        connection.execute(
            "CREATE VIRTUAL TABLE chunks_fts USING fts5(document_id UNINDEXED, title, section, content, tags, path, tokenize='unicode61')"
        )
        return "unicode61"


def rebuild_knowledge_index(settings: Settings) -> dict[str, Any]:
    """从 Markdown 原子重建知识索引，并在提交前执行 SQLite 完整性检查。"""
    settings.ensure_runtime_dirs()
    documents, errors = load_documents(settings)
    if errors:
        raise ValueError("知识验证失败：\n- " + "\n- ".join(errors))

    db_path = settings.knowledge_db
    handle, temp_name = tempfile.mkstemp(prefix="aikb-knowledge-", suffix=".db", dir=db_path.parent)
    os.close(handle)
    temp_path = Path(temp_name)
    tokenizer = "unknown"
    try:
        connection = sqlite3.connect(temp_path)
        try:
            tokenizer = _create_schema(connection)
            for item in documents:
                metadata = item.document.metadata
                tags = [str(tag) for tag in metadata.get("tags", [])]
                connection.execute(
                    "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        metadata["id"], item.relative_path, item.document.title, metadata["type"], metadata["status"],
                        item.summary, str(metadata.get("applicable_versions", "")), metadata.get("last_verified"),
                        str(metadata.get("review_when", "")), item.content_hash, item.document.body,
                    ),
                )
                connection.executemany(
                    "INSERT INTO tags(document_id, tag) VALUES (?, ?)",
                    [(metadata["id"], tag) for tag in tags],
                )
                relations = list(metadata.get("relations", []))
                for superseded in metadata.get("supersedes", []):
                    if isinstance(superseded, str) and superseded.startswith("aikb:"):
                        relations.append({"type": "supersedes", "target": superseded})
                connection.executemany(
                    "INSERT OR IGNORE INTO relations(source_id, relation_type, target_id) VALUES (?, ?, ?)",
                    [(metadata["id"], rel["type"], rel["target"]) for rel in relations],
                )
                for section, content, order in item.chunks:
                    cursor = connection.execute(
                        "INSERT INTO chunks(document_id, section, content, chunk_order) VALUES (?, ?, ?, ?)",
                        (metadata["id"], section, content, order),
                    )
                    connection.execute(
                        "INSERT INTO chunks_fts(rowid, document_id, title, section, content, tags, path) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (cursor.lastrowid, metadata["id"], item.document.title, section, content, " ".join(tags), item.relative_path),
                    )
            metadata_values = {
                "schema_version": SCHEMA_VERSION,
                "parser_version": PARSER_VERSION,
                "content_fingerprint": content_fingerprint(settings.content_root),
                "tokenizer": tokenizer,
            }
            connection.executemany("INSERT INTO index_metadata VALUES (?, ?)", metadata_values.items())
            connection.commit()
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite integrity_check 失败")
        finally:
            connection.close()
        os.replace(temp_path, db_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return {"documents": len(documents), "tokenizer": tokenizer, "database": str(db_path)}


def ensure_knowledge_index(settings: Settings) -> dict[str, Any]:
    """检查索引版本和内容指纹；缺失、损坏或过期时触发重建。"""
    if not settings.knowledge_db.exists():
        return rebuild_knowledge_index(settings)
    expected = content_fingerprint(settings.content_root)
    try:
        connection = sqlite3.connect(settings.knowledge_db)
        try:
            rows = dict(connection.execute("SELECT key, value FROM index_metadata"))
        finally:
            connection.close()
        if rows.get("schema_version") == SCHEMA_VERSION and rows.get("parser_version") == PARSER_VERSION and rows.get("content_fingerprint") == expected:
            return {"rebuilt": False, "tokenizer": rows.get("tokenizer", "unknown"), "database": str(settings.knowledge_db)}
    except sqlite3.Error:
        pass
    result = rebuild_knowledge_index(settings)
    result["rebuilt"] = True
    return result


def metadata_report(settings: Settings) -> dict[str, Any]:
    """返回知识元数据校验报告，不修改知识正文。"""
    documents, errors = load_documents(settings)
    return {
        "valid": not errors,
        "documents": len(documents),
        "errors": errors,
        "ids": [item.document.metadata.get("id") for item in documents],
    }
