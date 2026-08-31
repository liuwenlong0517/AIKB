"""扫描知识仓 Markdown，并构建可重建的 SQLite/FTS 派生索引。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import date
from urllib.parse import quote
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
# 类型与目录共同表达条目的语义边界；仅靠 Front Matter 的 type 无法防止误归类。
TYPE_DIRECTORY_PREFIXES = {
    "knowledge": ("knowledge/",),
    "solution": ("experience/solutions/",),
    "pitfall": ("experience/pitfalls/",),
    "decision": ("experience/decisions/",),
    "workflow": ("workflows/",),
    "project-memory": ("projects/",),
    "candidate": ("experience/inbox/",),
}
GOVERNANCE_VERSION = 2
ALLOWED_CHANGE_CLASSES = {
    "factual-update", "operational-solution", "decision-record", "decision-proposal",
    "supersession", "taxonomy-change", "sensitive-change", "candidate",
}
HIGH_IMPACT_CHANGE_CLASSES = {
    "decision-record", "decision-proposal", "supersession", "taxonomy-change", "sensitive-change",
}
ALLOWED_EVIDENCE_KINDS = {"command", "test", "url", "commit", "file", "user-confirmation"}
ALLOWED_APPROVAL_STATUSES = {"approved", "pending", "rejected", "not-required"}
ALLOWED_REVIEW_STATES = {"open", "in-review", "blocked", "ready", "accepted", "rejected", "closed"}
REVIEWED_STATES = {"accepted", "rejected", "closed"}
ALLOWED_DUPLICATE_STATUSES = {"verified", "candidate", "deprecated"}


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


def _iso_date(value: Any) -> bool:
    """验证日期既是 ISO ``YYYY-MM-DD`` 形状，也是真实日历日期。"""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _nonempty_text(value: Any) -> bool:
    """判断元数据字段是否为可用于审计定位的非空文本。"""
    return isinstance(value, str) and bool(value.strip())


def _validate_v2_metadata(metadata: dict[str, Any], relative: str, errors: list[str]) -> None:
    """校验 v2 治理门禁；只验证结构和留痕，不宣称证据内容真实。"""
    if "governance_version" not in metadata:
        # 无版本文档是受支持的 legacy 格式，不能因新门禁阻断旧索引。
        return
    if metadata.get("governance_version") != GOVERNANCE_VERSION:
        errors.append(f"{relative}: governance_version 目前只支持 2")
        return

    change_class = metadata.get("change_class")
    if change_class not in ALLOWED_CHANGE_CLASSES:
        errors.append(f"{relative}: change_class 不受支持：{change_class}")
    if not _nonempty_text(metadata.get("authority")):
        errors.append(f"{relative}: v2 条目必须填写非空 authority")
    if not _nonempty_text(metadata.get("preparer")):
        errors.append(f"{relative}: v2 条目必须填写非空 preparer")
    status = metadata.get("status")
    review_state = metadata.get("review_state")
    needs_review_identity = status in {"verified", "deprecated"} or review_state in REVIEWED_STATES
    if needs_review_identity:
        for field in ("reviewer", "reviewed_at"):
            if field == "reviewer" and not _nonempty_text(metadata.get(field)):
                errors.append(f"{relative}: 已正式/已结案条目必须填写非空 reviewer")
            elif field == "reviewed_at" and not _iso_date(metadata.get(field)):
                errors.append(f"{relative}: 已正式/已结案条目必须填写有效 reviewed_at")
    if _nonempty_text(metadata.get("preparer")) and _nonempty_text(metadata.get("reviewer")):
        if metadata["preparer"].strip().casefold() == metadata["reviewer"].strip().casefold():
            errors.append(f"{relative}: preparer 与 reviewer 不得为同一人")

    evidence = metadata.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append(f"{relative}: v2 正式/候选条目必须至少有一项结构化 evidence")
    else:
        for index, item in enumerate(evidence, start=1):
            if not isinstance(item, dict):
                errors.append(f"{relative}: evidence[{index}] 必须是对象，不能是自由文本")
                continue
            missing = [field for field in ("kind", "ref", "result", "date") if field not in item]
            if missing:
                errors.append(f"{relative}: evidence[{index}] 缺少字段 {', '.join(missing)}")
            if item.get("kind") not in ALLOWED_EVIDENCE_KINDS:
                errors.append(f"{relative}: evidence[{index}].kind 不受支持：{item.get('kind')}")
            if not _nonempty_text(item.get("ref")):
                errors.append(f"{relative}: evidence[{index}].ref 必须是非空文本")
            if not _nonempty_text(item.get("result")):
                errors.append(f"{relative}: evidence[{index}].result 必须是非空文本")
            if not _iso_date(item.get("date")):
                errors.append(f"{relative}: evidence[{index}].date 必须是有效 YYYY-MM-DD")
            ref = item.get("ref", "").strip() if _nonempty_text(item.get("ref")) else ""
            if item.get("kind") == "url" and ref and not re.match(r"^https?://\S+$", ref, re.I):
                errors.append(f"{relative}: evidence[{index}].ref 在 kind=url 时必须是 http(s) URL")
            if item.get("kind") == "commit" and ref and not re.fullmatch(r"[0-9a-fA-F]{7,40}", ref):
                errors.append(f"{relative}: evidence[{index}].ref 在 kind=commit 时必须是 7-40 位十六进制提交号")
            if item.get("kind") == "file" and ref:
                # 只接受 content/ 或项目逻辑相对路径，避免证据泄露本机物理路径。
                if re.match(r"^[A-Za-z]:[\\/]", ref) or ref.startswith(("/", "\\")) or re.search(r"(?:^|[/\\])\.\.(?:[/\\]|$)", ref):
                    errors.append(f"{relative}: evidence[{index}].ref 在 kind=file 时必须是安全的项目相对路径")
            unknown = sorted(set(item) - {"kind", "ref", "result", "date"})
            if unknown:
                errors.append(f"{relative}: evidence[{index}] 存在未允许字段 {', '.join(unknown)}")

    if change_class == "decision-proposal" and (status != "candidate" or metadata.get("type") != "candidate"):
        errors.append(f"{relative}: decision-proposal 只能使用 type=candidate、status=candidate 的 Inbox 表示")
    if metadata.get("type") == "decision" and change_class not in {"decision-record", "decision-proposal"}:
        errors.append(f"{relative}: type=decision 必须使用 decision-record 或 decision-proposal")
    if metadata.get("type") == "candidate":
        if metadata.get("status") != "candidate":
            errors.append(f"{relative}: type=candidate 必须使用 candidate 状态")
        if change_class not in {"candidate", "decision-proposal"}:
            errors.append(f"{relative}: Inbox type=candidate 只能使用 candidate 或 decision-proposal change_class")
    supersedes = metadata.get("supersedes", [])
    if isinstance(supersedes, list) and supersedes and change_class != "supersession":
        errors.append(f"{relative}: 非空 supersedes 必须使用 supersession change_class")

    if change_class in HIGH_IMPACT_CHANGE_CLASSES:
        approval_status = metadata.get("approval_status")
        if approval_status not in ALLOWED_APPROVAL_STATUSES - {"not-required"}:
            errors.append(f"{relative}: 高影响条目必须填写 approval_status=approved/pending/rejected")
        if metadata.get("status") == "verified":
            if approval_status != "approved":
                errors.append(f"{relative}: 高影响条目未经 approved 不得标记 verified")
            if not _nonempty_text(metadata.get("approved_by")):
                errors.append(f"{relative}: verified 高影响条目必须填写 approved_by")
            if not _iso_date(metadata.get("approved_at")):
                errors.append(f"{relative}: verified 高影响条目必须填写有效 approved_at")

    if status == "candidate":
        lifecycle_required = ("owner", "captured_at", "next_action_due", "review_state")
        missing = [field for field in lifecycle_required if not _nonempty_text(metadata.get(field))]
        if missing:
            errors.append(f"{relative}: v2 Inbox 缺少字段 {', '.join(missing)}")
        for field in ("captured_at", "next_action_due"):
            if field in metadata and not _iso_date(metadata.get(field)):
                errors.append(f"{relative}: {field} 必须是有效 YYYY-MM-DD")
        if _iso_date(metadata.get("captured_at")) and _iso_date(metadata.get("next_action_due")):
            if metadata["next_action_due"] < metadata["captured_at"]:
                errors.append(f"{relative}: next_action_due 不得早于 captured_at")
        if metadata.get("review_state") not in ALLOWED_REVIEW_STATES:
            errors.append(f"{relative}: review_state 不受支持：{metadata.get('review_state')}")
        blocking = _nonempty_text(metadata.get("blocking_reason"))
        duplicates = metadata.get("possible_duplicates")
        duplicate_list = isinstance(duplicates, list) and all(_nonempty_text(item) for item in duplicates)
        if not blocking and not duplicate_list:
            errors.append(f"{relative}: v2 Inbox 至少填写 blocking_reason 或 possible_duplicates")

    if change_class == "supersession":
        checked = metadata.get("duplicate_check_statuses")
        if not isinstance(checked, list) or set(checked) != ALLOWED_DUPLICATE_STATUSES:
            errors.append(f"{relative}: supersession 必须声明已查重 verified/candidate/deprecated")


def validate_document(document: MarkdownDocument, content_root: Path) -> list[str]:
    """校验单篇知识的元数据、目录归类、关系格式和正式条目字段，返回全部错误。"""
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
    entry_type = metadata.get("type")
    if entry_type not in ALLOWED_TYPES:
        errors.append(f"{relative}: type 不受支持：{metadata.get('type')}")
    else:
        expected_prefixes = TYPE_DIRECTORY_PREFIXES[entry_type]
        if not any(relative.startswith(prefix) for prefix in expected_prefixes):
            expected = "、".join(expected_prefixes)
            errors.append(f"{relative}: type={entry_type} 必须位于 {expected} 下")
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
    supersedes = metadata.get("supersedes", [])
    if not isinstance(supersedes, list):
        errors.append(f"{relative}: supersedes 必须是字符串列表")
    else:
        for target in supersedes:
            if not isinstance(target, str) or not target.startswith("aikb:"):
                errors.append(f"{relative}: supersedes target 必须是 aikb: 标识")
    last_verified = metadata.get("last_verified")
    if status != "candidate" and not _iso_date(last_verified):
        errors.append(f"{relative}: 正式条目的 last_verified 必须是有效 YYYY-MM-DD")
    _validate_v2_metadata(metadata, relative, errors)
    return errors


def load_documents(settings: Settings) -> tuple[list[IndexedDocument], list[str]]:
    """加载并校验全部知识文档，同时检查 Front Matter、归类、ID 和关系目标。"""
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
            # 扫描器已经排除了 README；分类目录中的其余 Markdown 必须是可索引条目。
            relative = "content/" + path.relative_to(settings.content_root).as_posix()
            errors.append(f"知识文件缺少 Front Matter：{relative}")
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
        if item.document.metadata.get("status") != "candidate":
            for relation in item.document.metadata.get("relations", []):
                if isinstance(relation, dict) and relation.get("target") not in known_ids:
                    errors.append(f"{item.relative_path}: 关系目标不存在：{relation.get('target')}")
        supersedes = item.document.metadata.get("supersedes", [])
        if isinstance(supersedes, list):
            for target in supersedes:
                if isinstance(target, str) and target not in known_ids:
                    errors.append(f"{item.relative_path}: 替代关系目标不存在：{target}")
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


def inspect_knowledge_index(settings: Settings) -> dict[str, Any]:
    """只读检查知识索引状态，不创建目录、重建或替换 SQLite 文件。

    Markdown 是事实源，SQLite 仅用于发现和检索；因此本函数只比较现有索引的
    元数据、内容指纹和完整性，不调用 ``ensure_knowledge_index``。返回值只含
    固定状态和安全摘要，不能把数据库物理路径或底层异常传给 Web/任务服务。
    """
    database = settings.knowledge_db
    if not database.is_file():
        return {"status": "missing", "available": False, "rebuilt": False, "source": "knowledge Markdown", "derived": "SQLite index"}
    try:
        expected = content_fingerprint(settings.content_root)
        # ``mode=ro`` 不仅是约定，而是 SQLite 连接层的硬约束；使用 URI 编码
        # 处理 Windows 盘符、空格和中文路径，避免检查动作创建 journal/WAL。
        encoded_path = quote(str(database.resolve()).replace("\\", "/"), safe="/:\\")
        readonly_uri = f"file:{encoded_path}?mode=ro"
        connection = sqlite3.connect(readonly_uri, uri=True)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                return {
                    "status": "damaged", "available": False, "rebuilt": False,
                    "source": "knowledge Markdown", "derived": "SQLite index", "reason": "integrity_check_failed",
                }
            tables = {
                str(row[0]) for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                ).fetchall()
            }
            required = {"index_metadata", "documents", "chunks"}
            if not required.issubset(tables):
                return {
                    "status": "damaged", "available": False, "rebuilt": False,
                    "source": "knowledge Markdown", "derived": "SQLite index", "reason": "schema_incomplete",
                }
            metadata = dict(connection.execute("SELECT key, value FROM index_metadata"))
        finally:
            connection.close()
    except (OSError, UnicodeError, sqlite3.Error):
        return {
            "status": "damaged", "available": False, "rebuilt": False,
            "source": "knowledge Markdown", "derived": "SQLite index", "reason": "index_read_failed",
        }
    if metadata.get("schema_version") != SCHEMA_VERSION or metadata.get("parser_version") != PARSER_VERSION:
        return {
            "status": "stale", "available": True, "rebuilt": False,
            "source": "knowledge Markdown", "derived": "SQLite index", "reason": "schema_version_mismatch",
        }
    if metadata.get("content_fingerprint") != expected:
        return {
            "status": "stale", "available": True, "rebuilt": False,
            "source": "knowledge Markdown", "derived": "SQLite index", "reason": "content_changed",
        }
    return {
        "status": "ready", "available": True, "rebuilt": False,
        "source": "knowledge Markdown", "derived": "SQLite index", "tokenizer": metadata.get("tokenizer", "unknown"),
    }


def metadata_report(settings: Settings) -> dict[str, Any]:
    """返回知识元数据校验报告，不修改知识正文。"""
    documents, errors = load_documents(settings)
    return {
        "valid": not errors,
        "documents": len(documents),
        "errors": errors,
        "ids": [item.document.metadata.get("id") for item in documents],
    }


def _review_report_date(value: date | str | None) -> date:
    """解析审查报告的基准日期；允许测试注入日期，默认使用本机当前日期。"""
    if value is None:
        return date.today()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("review report 的 as_of 必须是有效 YYYY-MM-DD") from exc
    raise TypeError("review report 的 as_of 必须是 date、YYYY-MM-DD 或 None")


def _safe_candidate_fields(metadata: dict[str, Any], governance_version: int | None) -> dict[str, Any]:
    """只投影审查所需字段，避免把证据正文、阻塞原因等自由文本带入提醒链路。"""
    if governance_version != GOVERNANCE_VERSION:
        return {}
    duplicates = metadata.get("possible_duplicates")
    # 即使 legacy 文档携带异常值也不能让报告抛错或泄露任意对象；v2 只保留
    # 可定位的 aikb ID，并限制数量，报告仍通过 duplicate_declared_count 反映声明。
    safe_duplicates = [
        item for item in duplicates[:20]
        if isinstance(item, str) and re.fullmatch(r"aikb:[a-z0-9][a-z0-9:-]*", item)
    ] if isinstance(duplicates, list) else []
    return {
        "governance_version": governance_version,
        "owner": metadata.get("owner") if _nonempty_text(metadata.get("owner")) else None,
        "next_action_due": metadata.get("next_action_due") if _iso_date(metadata.get("next_action_due")) else None,
        "review_state": metadata.get("review_state") if metadata.get("review_state") in ALLOWED_REVIEW_STATES else None,
        "possible_duplicates": safe_duplicates,
    }


def review_report(settings: Settings, as_of: date | str | None = None) -> dict[str, Any]:
    """生成候选晋升和正式知识复核条件报告；只读 Markdown，不自动改变状态。

    ``as_of`` 只用于判断 v2 Inbox 的 ``next_action_due`` 是否已经逾期，生产调用
    默认采用本机日期，测试和离线审查可传入固定日期。报告投影有限字段，避免把
    evidence、blocking_reason 或正文自由文本扩散到 SessionStart 和审计摘要。
    """
    report_date = _review_report_date(as_of)
    documents, errors = load_documents(settings)
    candidates: list[dict[str, Any]] = []
    review_items: list[dict[str, Any]] = []
    overdue_count = 0
    unowned_count = 0
    duplicate_declared_count = 0
    closed_still_in_inbox_count = 0
    legacy_candidate_count = 0
    for item in documents:
        metadata = item.document.metadata
        common = {
            "id": metadata.get("id"),
            "title": item.document.title,
            "type": metadata.get("type"),
            "status": metadata.get("status"),
            "path": item.relative_path,
            "content_hash": item.content_hash,
        }
        if metadata.get("status") == "candidate":
            governance_version = metadata.get("governance_version")
            candidate = {**common, "review_when": metadata.get("review_when", "")}
            candidate.update(_safe_candidate_fields(metadata, governance_version))
            if governance_version != GOVERNANCE_VERSION:
                legacy_candidate_count += 1
            owner = metadata.get("owner")
            if not _nonempty_text(owner):
                unowned_count += 1
            due = metadata.get("next_action_due")
            review_state = metadata.get("review_state")
            # 已接受、拒绝或关闭的条目虽仍暂留 Inbox，但不再进入“逾期待处理”
            # 计数；否则同一条目会同时被误报为逾期和已结案。legacy 无状态仍按
            # 到期日期提示，提醒维护者先完成迁移或人工判断。
            actionable = review_state not in REVIEWED_STATES
            if actionable and _iso_date(due) and due < report_date.isoformat():
                overdue_count += 1
            duplicates = metadata.get("possible_duplicates")
            if isinstance(duplicates, list) and duplicates:
                duplicate_declared_count += 1
            if metadata.get("review_state") in REVIEWED_STATES:
                closed_still_in_inbox_count += 1
            candidates.append(candidate)
        elif metadata.get("review_when"):
            review_items.append({
                **common,
                "last_verified": metadata.get("last_verified"),
                "review_when": metadata.get("review_when"),
            })
    return {
        "valid": not errors,
        "summary": {
            "as_of": report_date.isoformat(),
            "candidate_count": len(candidates),
            "overdue_count": overdue_count,
            "unowned_count": unowned_count,
            "duplicate_declared_count": duplicate_declared_count,
            "closed_still_in_inbox_count": closed_still_in_inbox_count,
            "legacy_candidate_count": legacy_candidate_count,
        },
        "candidates": candidates,
        "review_items": review_items,
        "errors": errors,
    }
