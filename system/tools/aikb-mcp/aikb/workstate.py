from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .frontmatter import parse_markdown, render_frontmatter


WORK_SCHEMA_VERSION = "1"
ALLOWED_WORK_STATUS = {"planned", "active", "blocked", "completed", "abandoned", "superseded"}
OPEN_STATUS = {"planned", "active", "blocked"}
SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|private[_-]?key)\s*[:=]\s*([^\s,;]+)"
)
TOKEN_VALUE_PATTERN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,})\b")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _safe_slug(value: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug[:48].strip("-") or fallback)


def project_id(project_path: str) -> str:
    normalized = str(Path(project_path).expanduser().resolve()).replace("\\", "/").lower()
    name = _safe_slug(Path(normalized).name, "project")
    suffix = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    return f"{name}-{suffix}"


def _redact_text(value: str) -> str:
    value = SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    value = TOKEN_VALUE_PATTERN.sub("[REDACTED]", value)
    return value.replace("\x00", "")


def _normalize_value(value: Any, *, max_items: int = 50) -> str | list[str]:
    if value is None:
        return ""
    if isinstance(value, list):
        return [_redact_text(str(item))[:2000] for item in value[:max_items] if str(item).strip()]
    return _redact_text(str(value))[:12000]


def _git_signature(project_path: str) -> tuple[str, str, bool, str]:
    path = Path(project_path)
    if not path.is_dir():
        return "", "", False
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, timeout=5, check=True
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=path, capture_output=True, text=True, timeout=5, check=True
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain=v1"], cwd=path, capture_output=True, text=True, timeout=8, check=True
        ).stdout.replace("\r\n", "\n")
        digest = hashlib.sha256((revision + "\n" + branch + "\n" + status).encode("utf-8")).hexdigest()
        return revision, branch, bool(status.strip()), digest
    except (OSError, subprocess.SubprocessError):
        return "", "", False, ""


def _render_section(title: str, value: str | list[str]) -> str:
    if isinstance(value, list):
        content = "\n".join(f"- {item}" for item in value) if value else "- 无"
    else:
        content = value.strip() or "无"
    return f"## {title}\n\n{content}"


def _parse_sections(body: str) -> dict[str, str | list[str]]:
    sections: dict[str, str | list[str]] = {}
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", body, re.MULTILINE))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        content = body[start:end].strip()
        lines = [line[2:].strip() for line in content.splitlines() if line.startswith("- ")]
        sections[match.group(1).strip()] = lines if lines and len(lines) == len([line for line in content.splitlines() if line.strip()]) else content
    return sections


class WorkStateStore:
    SECTION_FIELDS = {
        "任务目标": "goal",
        "用户已确认决定": "decisions",
        "已验证事实": "verified_facts",
        "当前状态": "current_state",
        "已完成内容": "completed",
        "修改文件": "changed_files",
        "验证结果": "verification",
        "假设与未验证项": "assumptions",
        "阻塞项": "blockers",
        "下一步": "next_steps",
        "候选知识": "candidate_knowledge",
        "恢复前检查": "resume_checks",
    }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.ensure_runtime_dirs()

    def checkpoint(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_project_path = str(payload.get("project_path") or "").strip()
        if not raw_project_path:
            raise ValueError("project_path 不能为空")
        resolved_project = str(Path(raw_project_path).expanduser().resolve())
        p_id = project_id(resolved_project)
        status = str(payload.get("status") or "active")
        if status not in OPEN_STATUS:
            raise ValueError("checkpoint 仅允许 planned、active、blocked")
        agent = _safe_slug(str(payload.get("agent") or "unknown"), "unknown")
        session_id = _safe_slug(str(payload.get("session_id") or uuid.uuid4().hex), uuid.uuid4().hex)[:32]
        role = _safe_slug(str(payload.get("role") or "implement"), "implement")
        requested_work_id = str(payload.get("work_id") or "").strip()
        work_id = _safe_slug(requested_work_id, "") if requested_work_id else ""
        requested_dir = self._safe_work_dir(self.settings.workspace_root / "active" / p_id / work_id) if work_id else None
        previous = self._load_work(requested_dir / "work.md") if requested_dir and (requested_dir / "work.md").exists() else None
        goal = _normalize_value(payload.get("goal") or (previous or {}).get("goal"))
        if not str(goal).strip():
            raise ValueError("新工作项必须提供 goal；续写已有 work_id 时可省略")
        if not work_id:
            work_id = f"{_safe_slug(str(goal), 'work')}-{uuid.uuid4().hex[:8]}"
        work_dir = self._safe_work_dir(self.settings.workspace_root / "active" / p_id / work_id)
        checkpoints_dir = work_dir / "checkpoints"

        revision, branch, dirty, signature = _git_signature(resolved_project)
        updated_at = _now()
        previous = previous or (self._load_work(work_dir / "work.md") if (work_dir / "work.md").exists() else None)
        based_on = payload.get("based_on") or (previous or {}).get("checkpoint_id") or ""
        checkpoint_id = f"{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%f')[:18]}-{agent}-{session_id[:8]}"
        metadata = {
            "work_id": work_id,
            "project_id": p_id,
            "status": status,
            "agent": agent,
            "session_id": session_id,
            "role": role,
            "updated_at": updated_at,
            "checkpoint_id": checkpoint_id,
            "based_on": str(based_on),
            "project_path": resolved_project,
            "branch": branch,
            "base_revision": revision,
            "workspace_dirty": dirty,
            "workspace_signature": signature,
            "sensitivity": str(payload.get("sensitivity") or "normal"),
        }
        fields = {field: _normalize_value(payload.get(field)) for field in self.SECTION_FIELDS.values()}
        fields["goal"] = goal
        markdown = self._render_work(metadata, fields)
        if len(markdown.encode("utf-8")) > 65536:
            raise ValueError("单个工作检查点不得超过 64 KiB；请只保留恢复任务所需的紧凑状态")
        self._atomic_write(checkpoints_dir / f"{checkpoint_id}.md", markdown)
        self._atomic_write(work_dir / "work.md", markdown)
        self.rebuild_index()
        return {
            "work_id": work_id,
            "project_id": p_id,
            "checkpoint_id": checkpoint_id,
            "status": status,
            "path": str(work_dir / "work.md"),
            "redaction_applied": "[REDACTED]" in markdown,
        }

    def get(self, *, project_path: str | None = None, work_id: str | None = None, limit: int = 5) -> dict[str, Any]:
        self.ensure_index()
        limit = max(1, min(int(limit), 20))
        filters = ["status IN ('planned','active','blocked')"]
        params: list[Any] = []
        if project_path:
            filters.append("project_id = ?")
            params.append(project_id(project_path))
        if work_id:
            filters.append("work_id = ?")
            params.append(work_id)
        connection = sqlite3.connect(self.settings.work_db)
        try:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"SELECT * FROM work_items WHERE {' AND '.join(filters)} ORDER BY updated_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        finally:
            connection.close()
        items = [self._resume_item(dict(row)) for row in rows]
        return {"count": len(items), "unique": len(items) == 1, "items": items}

    def close(self, work_id: str, *, status: str, agent: str, session_id: str, note: str = "") -> dict[str, Any]:
        if status not in {"completed", "abandoned", "superseded"}:
            raise ValueError("close status 必须是 completed、abandoned 或 superseded")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", work_id):
            raise ValueError("work_id 格式无效")
        matches = list((self.settings.workspace_root / "active").glob(f"*/{work_id}"))
        if len(matches) != 1:
            raise KeyError(f"活动工作项匹配数量不是 1：{work_id}")
        work_dir = self._safe_work_dir(matches[0])
        current = self._load_work(work_dir / "work.md")
        payload = {
            **{key: current.get(key, "") for key in self.SECTION_FIELDS.values()},
            "project_path": current["project_path"],
            "work_id": work_id,
            "status": "active",
            "agent": agent,
            "session_id": session_id,
            "role": "close",
        }
        if note:
            payload["current_state"] = note
        checkpoint = self.checkpoint(payload)
        current = self._load_work(work_dir / "work.md")
        current["status"] = status
        current["updated_at"] = _now()
        fields = {key: current.get(key, "") for key in self.SECTION_FIELDS.values()}
        closed_markdown = self._render_work({key: current.get(key, "") for key in self._metadata_fields()}, fields)
        self._atomic_write(work_dir / "work.md", closed_markdown)
        self._atomic_write(work_dir / "checkpoints" / f"{checkpoint['checkpoint_id']}.md", closed_markdown)
        year = str(datetime.now().year)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(current["project_id"])):
            raise ValueError("工作状态中的 project_id 格式无效")
        archive_root = (self.settings.workspace_root / "archive").resolve()
        destination = (archive_root / year / current["project_id"] / work_id).resolve()
        try:
            destination.relative_to(archive_root)
        except ValueError as exc:
            raise ValueError("归档路径越过 workspace/archive 边界") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(f"归档目标已存在：{destination}")
        shutil.move(str(work_dir), str(destination))
        self.rebuild_index()
        return {"work_id": work_id, "status": status, "last_checkpoint": checkpoint["checkpoint_id"], "archive_path": str(destination)}

    def rebuild_index(self) -> dict[str, Any]:
        self.settings.ensure_runtime_dirs()
        handle, temp_name = tempfile.mkstemp(prefix="aikb-work-", suffix=".db", dir=self.settings.work_db.parent)
        os.close(handle)
        temp_path = Path(temp_name)
        count = 0
        try:
            connection = sqlite3.connect(temp_path)
            try:
                connection.executescript(
                    """
                    CREATE TABLE index_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE work_items (
                        work_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, project_path TEXT NOT NULL,
                        status TEXT NOT NULL, agent TEXT NOT NULL, session_id TEXT NOT NULL, role TEXT NOT NULL,
                        updated_at TEXT NOT NULL, checkpoint_id TEXT NOT NULL, branch TEXT, base_revision TEXT,
                        workspace_dirty INTEGER NOT NULL, workspace_signature TEXT, goal TEXT NOT NULL,
                        current_state TEXT, next_steps TEXT, blockers TEXT, path TEXT NOT NULL
                    );
                    CREATE INDEX idx_work_project_status ON work_items(project_id, status, updated_at);
                    """
                )
                for root_name in ("active", "archive"):
                    for path in sorted((self.settings.workspace_root / root_name).rglob("work.md")):
                        data = self._load_work(path)
                        connection.execute(
                            "INSERT OR REPLACE INTO work_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                data.get("work_id"), data.get("project_id"), data.get("project_path"), data.get("status"),
                                data.get("agent"), data.get("session_id"), data.get("role"), data.get("updated_at"),
                                data.get("checkpoint_id"), data.get("branch"), data.get("base_revision"),
                                int(bool(data.get("workspace_dirty"))), data.get("workspace_signature"), data.get("goal"),
                                self._as_text(data.get("current_state")), self._as_text(data.get("next_steps")),
                                self._as_text(data.get("blockers")), str(path),
                            ),
                        )
                        count += 1
                fingerprint = self._work_fingerprint()
                connection.executemany(
                    "INSERT INTO index_metadata VALUES (?, ?)",
                    {"schema_version": WORK_SCHEMA_VERSION, "fingerprint": fingerprint}.items(),
                )
                connection.commit()
            finally:
                connection.close()
            os.replace(temp_path, self.settings.work_db)
        finally:
            temp_path.unlink(missing_ok=True)
        return {"items": count, "database": str(self.settings.work_db)}

    def ensure_index(self) -> None:
        if not self.settings.work_db.exists():
            self.rebuild_index()
            return
        try:
            connection = sqlite3.connect(self.settings.work_db)
            try:
                metadata = dict(connection.execute("SELECT key, value FROM index_metadata"))
            finally:
                connection.close()
            if metadata.get("schema_version") == WORK_SCHEMA_VERSION and metadata.get("fingerprint") == self._work_fingerprint():
                return
        except sqlite3.Error:
            pass
        self.rebuild_index()

    def is_dirty_since_checkpoint(self, project_path: str, item: dict[str, Any]) -> bool:
        return _git_signature(project_path)[3] != item.get("workspace_signature", "")

    def _load_work(self, path: Path) -> dict[str, Any]:
        document = parse_markdown(path)
        if document is None:
            raise ValueError(f"工作状态缺少 Front Matter：{path}")
        result = dict(document.metadata)
        sections = _parse_sections(document.body)
        for title, field in self.SECTION_FIELDS.items():
            value = sections.get(title, "")
            if isinstance(value, list) and value == ["无"]:
                value = []
            elif value == "无":
                value = ""
            result[field] = value
        return result

    def _render_work(self, metadata: dict[str, Any], fields: dict[str, str | list[str]]) -> str:
        body = [f"# 工作状态：{metadata['work_id']}"]
        for title, field in self.SECTION_FIELDS.items():
            body.append(_render_section(title, fields.get(field, "")))
        return render_frontmatter(metadata) + "\n\n" + "\n\n".join(body) + "\n"

    def _metadata_fields(self) -> tuple[str, ...]:
        return (
            "work_id", "project_id", "status", "agent", "session_id", "role", "updated_at", "checkpoint_id",
            "based_on", "project_path", "branch", "base_revision", "workspace_dirty", "workspace_signature", "sensitivity",
        )

    def _safe_work_dir(self, path: Path) -> Path:
        resolved = path.resolve()
        active = (self.settings.workspace_root / "active").resolve()
        try:
            resolved.relative_to(active)
        except ValueError as exc:
            raise ValueError("工作状态路径越过 workspace/active 边界") from exc
        return resolved

    def _atomic_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
        os.close(handle)
        temp_path = Path(temp_name)
        try:
            temp_path.write_text(content, encoding="utf-8", newline="\n")
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _work_fingerprint(self) -> str:
        digest = hashlib.sha256()
        for root_name in ("active", "archive"):
            for path in sorted((self.settings.workspace_root / root_name).rglob("work.md")):
                digest.update(str(path.relative_to(self.settings.workspace_root)).encode("utf-8"))
                digest.update(path.read_bytes())
        return digest.hexdigest()

    def _resume_item(self, row: dict[str, Any]) -> dict[str, Any]:
        result = {
            "work_id": row["work_id"], "project_id": row["project_id"], "status": row["status"],
            "agent": row["agent"], "session_id": row["session_id"], "role": row["role"],
            "updated_at": row["updated_at"], "checkpoint_id": row["checkpoint_id"], "goal": row["goal"],
            "current_state": row.get("current_state") or "", "next_steps": row.get("next_steps") or "",
            "blockers": row.get("blockers") or "", "branch": row.get("branch") or "",
            "base_revision": row.get("base_revision") or "", "workspace_dirty": bool(row.get("workspace_dirty")),
            "workspace_signature": row.get("workspace_signature") or "", "path": row["path"],
        }
        capsule = (
            f"任务 {result['work_id']}（{result['status']}）：{result['goal']}\n"
            f"当前状态：{result['current_state'] or '未记录'}\n"
            f"下一步：{result['next_steps'] or '未记录'}\n"
            f"阻塞：{result['blockers'] or '无'}\n"
            f"恢复前核对 branch={result['branch'] or 'unknown'} revision={result['base_revision'] or 'unknown'}。"
        )
        result["resume_capsule"] = capsule[:1500]
        return result

    @staticmethod
    def _as_text(value: Any) -> str:
        if isinstance(value, list):
            return "；".join(str(item) for item in value)
        return str(value or "")
