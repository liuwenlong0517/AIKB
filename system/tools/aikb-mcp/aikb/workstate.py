"""维护本机 Working State：结构化检查点、恢复胶囊、索引和归档。"""

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


# Markdown 工作状态的治理契约是 v2；索引版本单独递增，以便旧 SQLite
# 派生库在增加 owner/participant 列后自动重建，而不会沿用旧列布局。
WORK_SCHEMA_VERSION = "3"
WORK_METADATA_SCHEMA_VERSION = "2"
ALLOWED_WORK_STATUS = {"planned", "active", "blocked", "completed", "abandoned", "superseded"}
OPEN_STATUS = {"planned", "active", "blocked"}
OWNERSHIP_MODES = {"session-bound", "shared", "handed-off", "legacy-unbound"}
MAX_PARTICIPANTS = 16
SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|private[_-]?key)\s*[:=]\s*([^\s,;]+)"
)
TOKEN_VALUE_PATTERN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,})\b")
# Web 只读模型的文本和集合上限。工作状态本身允许更大的恢复内容，但浏览器
# 只需要有限摘要；将上限集中在共享核心，避免各个 HTTP 路由各自裁剪出不同契约。
WEB_TEXT_LIMIT = 12000
WEB_CHECKPOINT_TEXT_LIMIT = 4000
WEB_LIST_LIMIT = 50
WEB_MAX_PAGE = 100000
WEB_CHECKPOINT_DETAIL_FIELDS = ("goal", "current_state", "next_steps", "blockers", "verification", "changed_files")
_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9_:/])/(?:"
    r"(?:users|home|private|var|tmp|workspace|mnt|opt|srv|root|usr|etc|bin|dev|proc|sys|run|lib|sbin|boot|media)"
    r"(?:/[^\s<>\"']*)*"
    r"|(?:[^\s<>\"'/]+(?:/[^\s<>\"'/]+)*)+)"
    r"|(?<![a-z0-9_])(?:[a-z]:[\\/](?![\\/])[^\s<>\"']*|\\\\[^\s<>\"']+)"
)


def _now() -> str:
    """返回带本地时区信息的秒级 ISO-8601 时间。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _safe_slug(value: str, fallback: str) -> str:
    """将外部标识归一化为安全的小写 slug，并限制长度。"""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug[:48].strip("-") or fallback)


def project_id(project_path: str) -> str:
    """由规范化项目路径生成可读且稳定的本机项目 ID。"""
    normalized = str(Path(project_path).expanduser().resolve()).replace("\\", "/").lower()
    name = _safe_slug(Path(normalized).name, "project")
    suffix = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    return f"{name}-{suffix}"


def _redact_text(value: str) -> str:
    """移除常见密钥字段、令牌和 NUL 字符，避免写入工作状态。"""
    value = SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    value = TOKEN_VALUE_PATTERN.sub("[REDACTED]", value)
    return value.replace("\x00", "")


def _normalize_value(value: Any, *, max_items: int = 50) -> str | list[str]:
    """把外部字段裁剪成可序列化的紧凑值，并在列表中执行脱敏。"""
    if value is None:
        return ""
    if isinstance(value, list):
        return [_redact_text(str(item))[:2000] for item in value[:max_items] if str(item).strip()]
    return _redact_text(str(value))[:12000]


def _git_top_level(project_path: str) -> Path | None:
    """返回路径所属 Git 仓库的规范根目录；非仓库返回空。"""
    path = Path(project_path)
    if not path.is_dir():
        return None
    try:
        output = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        return Path(output).resolve() if output else None
    except (OSError, subprocess.SubprocessError):
        return None


def _git_signature(project_path: str) -> tuple[str, str, bool, str]:
    """读取单个 Git 仓库的 revision、分支、脏状态和稳定签名。"""
    path = Path(project_path)
    if not path.is_dir():
        return "", "", False, ""
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


def _repository_snapshot(role: str, path: Path) -> dict[str, Any]:
    """生成可写入 Working State 的单仓紧凑快照。"""
    revision, branch, dirty, signature = _git_signature(str(path))
    return {
        "role": _safe_slug(role, "repository"),
        "path": str(path.resolve()),
        "branch": branch,
        "revision": revision,
        "dirty": dirty,
        "signature": signature,
    }


def _repositories_signature(repositories: list[dict[str, Any]]) -> str:
    """按固定顺序计算多仓组合签名，避免遗漏任一仓库变化。"""
    normalized = [
        {
            "role": item.get("role", ""),
            "path": str(item.get("path", "")).replace("\\", "/").lower(),
            "signature": item.get("signature", ""),
        }
        for item in repositories
    ]
    return hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _render_section(title: str, value: str | list[str]) -> str:
    """把一个结构化字段渲染成工作状态 Markdown 二级章节。"""
    if isinstance(value, list):
        content = "\n".join(f"- {item}" for item in value) if value else "- 无"
    else:
        content = value.strip() or "无"
    return f"## {title}\n\n{content}"


def _parse_sections(body: str) -> dict[str, str | list[str]]:
    """解析工作状态章节，并识别纯无序列表字段。"""
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
    """管理 workspace/active 与 workspace/archive 中的状态文件及派生索引。"""

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
        """绑定运行面设置，并确保工作状态所需目录存在。"""
        self.settings = settings
        self.settings.ensure_runtime_dirs()

    def _repository_snapshots(self, project_path: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """收集主项目及显式关联仓库；维护 AIKB 本身时自动纳入独立知识仓。"""
        targets: list[tuple[str, Path]] = [("project", Path(project_path).resolve())]
        configured = payload.get("repositories") or []
        if not isinstance(configured, list) or len(configured) > 8:
            raise ValueError("repositories 必须是最多 8 项的列表")
        for item in configured:
            if not isinstance(item, dict) or not str(item.get("path") or "").strip():
                raise ValueError("repositories 每项必须包含 path")
            targets.append((str(item.get("role") or "related"), Path(str(item["path"])).expanduser().resolve()))

        project_root = _git_top_level(project_path)
        control_root = _git_top_level(str(self.settings.repo_root))
        knowledge_root = _git_top_level(str(self.settings.knowledge_root))
        if project_root and control_root and project_root == control_root and knowledge_root == self.settings.knowledge_root.resolve():
            targets.append(("knowledge", knowledge_root))

        snapshots: list[dict[str, Any]] = []
        seen: set[str] = set()
        for role, candidate in targets:
            git_root = _git_top_level(str(candidate)) or candidate
            key = str(git_root).replace("\\", "/").lower()
            if key in seen:
                continue
            seen.add(key)
            snapshots.append(_repository_snapshot(role, git_root))
        return snapshots

    @staticmethod
    def _actor(agent: str, session_id: str) -> tuple[str, str]:
        """规范化写入者身份；所有写入操作都要求显式、非空的会话标识。"""
        normalized_agent = _safe_slug(str(agent or ""), "")
        normalized_session = _safe_slug(str(session_id or ""), "")[:32]
        if not normalized_agent or normalized_agent == "unknown":
            raise PermissionError("Working State 写入必须绑定可信 Agent（MCP 服务需使用 serve --agent）")
        if not normalized_session:
            raise PermissionError("Working State 写入必须提供 session_id")
        return normalized_agent, normalized_session

    @staticmethod
    def _ownership(data: dict[str, Any] | None) -> dict[str, Any]:
        """读取 v2 所有权；缺少持久 owner 的旧文档一律视为未认领。"""
        source = data or {}
        owner_agent = _safe_slug(str(source.get("owner_agent") or ""), "")
        owner_session = _safe_slug(str(source.get("owner_session_id") or ""), "")[:32]
        mode = str(source.get("ownership_mode") or "").strip()
        if mode not in OWNERSHIP_MODES or mode == "legacy-unbound" or not owner_agent or not owner_session:
            owner_agent = ""
            owner_session = ""
            mode = "legacy-unbound"
        participants: list[dict[str, str]] = []
        raw_participants = source.get("participants") or []
        if isinstance(raw_participants, str):
            try:
                raw_participants = json.loads(raw_participants)
            except json.JSONDecodeError:
                raw_participants = []
        if isinstance(raw_participants, list):
            for raw in raw_participants[:MAX_PARTICIPANTS]:
                if not isinstance(raw, dict):
                    continue
                participant_agent = _safe_slug(str(raw.get("agent") or ""), "")
                participant_session = _safe_slug(str(raw.get("session_id") or ""), "")[:32]
                if not participant_agent or not participant_session:
                    continue
                item = {
                    "agent": participant_agent,
                    "session_id": participant_session,
                    "role": _safe_slug(str(raw.get("role") or "participant"), "participant"),
                }
                if raw.get("authorized_at"):
                    item["authorized_at"] = str(raw["authorized_at"])[:80]
                if item not in participants:
                    participants.append(item)
        return {
            "owner_agent": owner_agent,
            "owner_session_id": owner_session,
            "ownership_mode": mode,
            "participants": participants,
            "ownership_binding": str(source.get("ownership_binding") or ""),
        }

    @classmethod
    def is_authorized_actor(cls, data: dict[str, Any], agent: str, session_id: str) -> bool:
        """判断当前会话是否拥有或被显式授权处理工作项。"""
        try:
            normalized_agent, normalized_session = cls._actor(agent, session_id)
        except PermissionError:
            return False
        ownership = cls._ownership(data)
        if ownership["ownership_mode"] == "legacy-unbound":
            return False
        if (normalized_agent, normalized_session) == (
            ownership["owner_agent"], ownership["owner_session_id"]
        ):
            return True
        return any(
            participant["agent"] == normalized_agent and participant["session_id"] == normalized_session
            for participant in ownership["participants"]
        )

    def _active_work_dir(self, work_id: str) -> Path:
        """按工作 ID 定位唯一活动目录，并把路径限制在 workspace/active。"""
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", work_id):
            raise ValueError("work_id 格式无效")
        matches = list((self.settings.workspace_root / "active").glob(f"*/{work_id}"))
        if len(matches) != 1:
            raise KeyError(f"活动工作项匹配数量不是 1：{work_id}")
        return self._safe_work_dir(matches[0])

    def _require_owner(self, current: dict[str, Any], agent: str, session_id: str) -> tuple[str, str]:
        """要求调用者是 owner；participant 不能转授权或改变归属。"""
        actor = self._actor(agent, session_id)
        ownership = self._ownership(current)
        if ownership["ownership_mode"] == "legacy-unbound":
            raise PermissionError("旧工作项尚未认领；请先显式调用 claim_work_state")
        if actor != (ownership["owner_agent"], ownership["owner_session_id"]):
            raise PermissionError("当前会话不是工作项 owner，无权授权、交接或关闭")
        return actor

    def checkpoint(self, payload: dict[str, Any]) -> dict[str, Any]:
        """追加一个脱敏且有大小上限的检查点，同时刷新工作索引。

        显式传入的工作 ID 可以创建新活动任务或继续已有活动任务，但归档任务的
        ID 不可重新占用，这样全局主键不会把历史任务和新活动任务混成一条索引记录。
        """
        raw_project_path = str(payload.get("project_path") or "").strip()
        if not raw_project_path:
            raise ValueError("project_path 不能为空")
        resolved_project = str(Path(raw_project_path).expanduser().resolve())
        p_id = project_id(resolved_project)
        status = str(payload.get("status") or "active")
        if status not in OPEN_STATUS:
            raise ValueError("checkpoint 仅允许 planned、active、blocked")
        agent, session_id = self._actor(payload.get("agent", ""), payload.get("session_id", ""))
        role = _safe_slug(str(payload.get("role") or "implement"), "implement")
        requested_work_id = str(payload.get("work_id") or "").strip()
        work_id = _safe_slug(requested_work_id, "") if requested_work_id else ""
        if requested_work_id and work_id:
            archived_paths = self._find_archived_work_paths(work_id)
            if archived_paths:
                locations = ", ".join(str(path.parent) for path in archived_paths)
                raise FileExistsError(f"显式 work_id 已存在于归档，拒绝复用：{work_id}（{locations}）")
        requested_dir = self._safe_work_dir(self.settings.workspace_root / "active" / p_id / work_id) if work_id else None
        previous = self._load_work(requested_dir / "work.md") if requested_dir and (requested_dir / "work.md").exists() else None
        goal = _normalize_value(payload.get("goal") or (previous or {}).get("goal"))
        if not str(goal).strip():
            raise ValueError("新工作项必须提供 goal；续写已有 work_id 时可省略")
        if not work_id:
            work_id = f"{_safe_slug(str(goal), 'work')}-{uuid.uuid4().hex[:8]}"
        work_dir = self._safe_work_dir(self.settings.workspace_root / "active" / p_id / work_id)
        checkpoints_dir = work_dir / "checkpoints"

        repositories = self._repository_snapshots(resolved_project, payload)
        primary = repositories[0]
        revision = str(primary.get("revision") or "")
        branch = str(primary.get("branch") or "")
        dirty = any(bool(item.get("dirty")) for item in repositories)
        signature = _repositories_signature(repositories)
        updated_at = _now()
        previous = previous or (self._load_work(work_dir / "work.md") if (work_dir / "work.md").exists() else None)
        ownership = self._ownership(previous)
        if previous is None:
            # 新建任务的 owner 由可信 MCP 服务身份和显式会话共同确定，后续普通
            # checkpoint 只能追加 author，不能用 payload 覆盖这个持久归属。
            ownership = {
                "owner_agent": agent,
                "owner_session_id": session_id,
                "ownership_mode": "session-bound",
                "participants": [],
                "ownership_binding": "agent+declared-session",
            }
        elif not self.is_authorized_actor(previous, agent, session_id):
            raise PermissionError("当前会话未获授权写入该工作项；旧任务请先显式认领，跨 Agent 请先交接")
        based_on = payload.get("based_on") or (previous or {}).get("checkpoint_id") or ""
        checkpoint_id = f"{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%f')[:18]}-{agent}-{session_id[:8]}"
        metadata = {
            "work_id": work_id,
            "project_id": p_id,
            "status": status,
            "work_schema_version": WORK_METADATA_SCHEMA_VERSION,
            "agent": agent,
            "session_id": session_id,
            "role": role,
            "author_agent": agent,
            "author_session_id": session_id,
            "author_role": role,
            "owner_agent": ownership["owner_agent"],
            "owner_session_id": ownership["owner_session_id"],
            "ownership_mode": ownership["ownership_mode"],
            "participants": ownership["participants"],
            "ownership_binding": ownership.get("ownership_binding") or "agent+declared-session",
            "updated_at": updated_at,
            "checkpoint_id": checkpoint_id,
            "based_on": str(based_on),
            "project_path": resolved_project,
            "branch": branch,
            "base_revision": revision,
            "workspace_dirty": dirty,
            "workspace_signature": signature,
            "repositories": repositories,
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

    def _persist_ownership(self, work_dir: Path, current: dict[str, Any], ownership: dict[str, Any]) -> dict[str, Any]:
        """仅更新工作项 owner 元数据；不伪造一个由新会话完成的 checkpoint。"""
        metadata = {key: current.get(key, "") for key in self._metadata_fields()}
        metadata.update(ownership)
        metadata["work_schema_version"] = WORK_METADATA_SCHEMA_VERSION
        fields = {key: current.get(key, "") for key in self.SECTION_FIELDS.values()}
        markdown = self._render_work(metadata, fields)
        if len(markdown.encode("utf-8")) > 65536:
            raise ValueError("单个工作检查点不得超过 64 KiB；请只保留恢复任务所需的紧凑状态")
        self._atomic_write(work_dir / "work.md", markdown)
        self.rebuild_index()
        return {
            "work_id": str(current.get("work_id") or ""),
            "owner_agent": ownership["owner_agent"],
            "owner_session_id": ownership["owner_session_id"],
            "ownership_mode": ownership["ownership_mode"],
            "participants": ownership["participants"],
        }

    def claim(self, work_id: str, *, agent: str, session_id: str) -> dict[str, Any]:
        """显式认领缺少 owner 的旧任务；认领不会把旧作者伪装成新 owner。"""
        actor = self._actor(agent, session_id)
        work_dir = self._active_work_dir(work_id)
        current = self._load_work(work_dir / "work.md")
        ownership = self._ownership(current)
        if ownership["ownership_mode"] != "legacy-unbound":
            raise PermissionError("工作项已经有 owner；请由 owner 显式授权或交接")
        return self._persist_ownership(
            work_dir,
            current,
            {
                "owner_agent": actor[0],
                "owner_session_id": actor[1],
                "ownership_mode": "session-bound",
                "participants": [],
                "ownership_binding": "agent+declared-session",
            },
        )

    def authorize_participant(
        self, work_id: str, *, owner_agent: str, owner_session_id: str,
        participant_agent: str, participant_session_id: str, role: str = "participant",
    ) -> dict[str, Any]:
        """由 owner 登记一个有界 participant；授权绑定到精确 Agent/会话对。"""
        work_dir = self._active_work_dir(work_id)
        current = self._load_work(work_dir / "work.md")
        self._require_owner(current, owner_agent, owner_session_id)
        target = self._actor(participant_agent, participant_session_id)
        ownership = self._ownership(current)
        participants = list(ownership["participants"])
        if not any(item["agent"] == target[0] and item["session_id"] == target[1] for item in participants):
            if len(participants) >= MAX_PARTICIPANTS:
                raise ValueError(f"participant 数量不得超过 {MAX_PARTICIPANTS}")
            participants.append(
                {
                    "agent": target[0], "session_id": target[1],
                    "role": _safe_slug(role or "participant", "participant"),
                    "authorized_at": _now(),
                }
            )
        ownership["participants"] = participants
        if ownership["ownership_mode"] == "session-bound":
            ownership["ownership_mode"] = "shared"
        return self._persist_ownership(work_dir, current, ownership)

    def revoke_participant(
        self, work_id: str, *, owner_agent: str, owner_session_id: str,
        participant_agent: str, participant_session_id: str,
    ) -> dict[str, Any]:
        """由 owner 精确撤销 participant；目标不存在时幂等且不改变 owner。"""
        work_dir = self._active_work_dir(work_id)
        current = self._load_work(work_dir / "work.md")
        self._require_owner(current, owner_agent, owner_session_id)
        target = self._actor(participant_agent, participant_session_id)
        ownership = self._ownership(current)
        if target == (ownership["owner_agent"], ownership["owner_session_id"]):
            raise PermissionError("不能撤销工作项 owner；只能撤销 participant")
        participants = ownership["participants"]
        retained = [
            item for item in participants
            if (item["agent"], item["session_id"]) != target
        ]
        removed = len(retained) != len(participants)
        ownership["participants"] = retained
        if not retained and ownership["ownership_mode"] in {"shared", "handed-off"}:
            ownership["ownership_mode"] = "session-bound"
        result = self._persist_ownership(work_dir, current, ownership)
        result["revoked"] = removed
        return result

    def handoff(
        self, work_id: str, *, owner_agent: str, owner_session_id: str,
        participant_agent: str, participant_session_id: str, role: str = "handoff",
    ) -> dict[str, Any]:
        """由 owner 显式交接给另一会话；owner 保持不变，目标成为授权 participant。"""
        result = self.authorize_participant(
            work_id,
            owner_agent=owner_agent,
            owner_session_id=owner_session_id,
            participant_agent=participant_agent,
            participant_session_id=participant_session_id,
            role=role,
        )
        work_dir = self._active_work_dir(work_id)
        current = self._load_work(work_dir / "work.md")
        ownership = self._ownership(current)
        ownership["ownership_mode"] = "handed-off"
        return self._persist_ownership(work_dir, current, ownership)

    def get(
        self, *, project_path: str | None = None, work_id: str | None = None, limit: int = 5,
        actor_agent: str | None = None, actor_session_id: str | None = None,
        authorized_only: bool = False,
    ) -> dict[str, Any]:
        """查询活动任务并返回有限数量的恢复胶囊，不读取聊天记录。

        ``authorized_only`` 仅供生命周期 Hook 使用；普通 ``get_work_state`` 保持
        可读地列出任务，但自动注入与 Git 门禁必须使用会话归属过滤结果。
        """
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
            # 授权过滤必须覆盖完整活动集合；若先 LIMIT，排序靠后的合法 owner
            # 会被误判为 foreign。普通查询仍使用原有限 SQL 路径，避免扩大常规响应。
            query = f"SELECT * FROM work_items WHERE {' AND '.join(filters)} ORDER BY updated_at DESC"
            query_params: tuple[Any, ...] = tuple(params)
            if not authorized_only:
                query += " LIMIT ?"
                query_params = (*params, limit)
            rows = connection.execute(
                query,
                query_params,
            ).fetchall()
        finally:
            connection.close()
        row_dicts = [dict(row) for row in rows]
        if authorized_only:
            if not actor_agent or not actor_session_id:
                row_dicts = []
            else:
                row_dicts = [
                    row for row in row_dicts
                    if self.is_authorized_actor(row, actor_agent, actor_session_id)
                ]
        total = len(row_dicts)
        items = [self._resume_item(row) for row in row_dicts[:limit]]
        return {"count": total if authorized_only else len(items), "unique": total == 1, "items": items}

    def web_active_work_states(
        self, *, work_id: str | None = None, project_id: str | None = None,
        status: str | list[str] | tuple[str, ...] | None = None, agent: str | None = None,
        page: int = 1, page_size: int = 20, limit: int | None = None,
    ) -> dict[str, Any]:
        """返回 Web 使用的活动任务安全视图。

        这是 ``get`` 之外的显式公共契约：不返回数据库记录中的物理路径、仓库
        路径、完整签名或完整 Git 输出。索引缺失或过期时只允许重建 SQLite
        派生层，Markdown 工作状态仍是唯一事实源；重建结果以 ``index`` 状态
        返回，便于界面显示“已重建/降级”而不是猜测数据是否新鲜。
        """
        if work_id is not None and not self._valid_web_identifier(work_id):
            raise ValueError("work_id 格式无效")
        if project_id is not None and (len(project_id) > 120 or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", project_id)):
            raise ValueError("project_id 格式无效")
        if agent is not None and len(agent) > 120:
            raise ValueError("agent 长度超限")
        if limit is not None:
            # 旧的内部调用使用 limit；Web 契约统一使用 page/page_size。
            page_size = limit if page_size == 20 else page_size
        page, page_size = self._web_validate_paging(page, page_size)
        statuses = self._web_status_filter(status)
        index = self._web_index_status()
        if index["status"] == "unavailable":
            return {"count": 0, "unique": False, "items": [], "pagination": self._web_pagination(page, page_size, 0, 0), "index": index}
        filters = ["status IN ('planned','active','blocked')"]
        params: list[Any] = []
        if work_id:
            filters.append("work_id = ?")
            params.append(work_id)
        if project_id:
            filters.append("project_id = ?")
            params.append(project_id)
        if statuses:
            filters.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            params.extend(statuses)
        if agent:
            filters.append("agent = ?")
            params.append(agent)
        connection = sqlite3.connect(self.settings.work_db)
        try:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"SELECT * FROM work_items WHERE {' AND '.join(filters)}",
                params,
            ).fetchall()
        except sqlite3.Error:
            return {"count": 0, "unique": False, "items": [], "pagination": self._web_pagination(page, page_size, 0, 0), "index": {**index, "status": "unavailable", "reason": "derived_index_query_failed"}}
        finally:
            connection.close()
        # 索引同时容纳归档记录；公共 v1 契约再次按事实路径确认 active，避免
        # 历史数据或旧索引把归档任务混入活动观察面。
        rows = [row for row in rows if self._is_active_record(dict(row))]
        rows = sorted(rows, key=lambda row: (str(row["updated_at"] or ""), str(row["work_id"] or "")), reverse=True)
        # updated_at 倒序后，work_id 也需按稳定方向排序；不能用单一 reverse 让
        # 同时间的 ID 反向，故对相同时间段再做显式升序处理。
        rows = sorted(rows, key=lambda row: str(row["work_id"] or ""))
        rows = sorted(rows, key=lambda row: str(row["updated_at"] or ""), reverse=True)
        total = len(rows)
        start = (page - 1) * page_size
        selected = rows[start:start + page_size]
        items = [self._web_work_record(dict(row), detail=False) for row in selected]
        return {
            "count": len(items), "unique": len(items) == 1, "items": items,
            "pagination": self._web_pagination(page, page_size, total, len(items)), "index": index,
        }

    def web_work_state(self, work_id: str) -> dict[str, Any]:
        """读取一个活动任务的有限安全详情，不改变任务生命周期状态。"""
        if not self._valid_web_identifier(work_id):
            raise ValueError("work_id 格式无效")
        result = self.web_active_work_states(work_id=work_id, page=1, page_size=1)
        if not result["items"]:
            raise KeyError("工作状态不存在")
        item = self._web_work_record(self._web_row_for_work(work_id), detail=True)
        return {"item": item, "index": result["index"]}

    def web_checkpoints(self, work_id: str, *, page: int = 1, page_size: int = 20, limit: int | None = None) -> dict[str, Any]:
        """列出指定工作状态的检查点摘要；只读取 Markdown，不返回文件路径。"""
        work_dir = self._web_work_dir(work_id)
        if limit is not None:
            page_size = limit if page_size == 20 else page_size
        page, page_size = self._web_validate_paging(page, page_size)
        records = [self._web_checkpoint_record(path, include_detail=False) for path in (work_dir / "checkpoints").glob("*.md")]
        records.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item.get("checkpoint_id") or "")), reverse=True)
        records.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        total = len(records)
        start = (page - 1) * page_size
        items = records[start:start + page_size]
        return {"work_id": work_id, "count": len(items), "items": items, "pagination": self._web_pagination(page, page_size, total, len(items)), "source": "working-state Markdown"}

    def web_checkpoint(self, work_id: str, checkpoint_id: str) -> dict[str, Any]:
        """读取一个检查点的有限字段和章节内容，不暴露原始 Markdown 路径。"""
        if not self._valid_web_identifier(work_id) or not self._valid_web_identifier(checkpoint_id):
            raise ValueError("标识格式无效")
        work_dir = self._web_work_dir(work_id)
        checkpoint_path = next(
            (path for path in (work_dir / "checkpoints").glob("*.md") if path.stem.lower() == checkpoint_id),
            None,
        )
        if checkpoint_path is None:
            raise KeyError("检查点不存在")
        return {"work_id": work_id, "item": self._web_checkpoint_record(checkpoint_path, include_detail=True), "source": "working-state Markdown"}

    def web_repository_summary(self) -> dict[str, Any]:
        """返回控制仓和知识仓的语义 Git 摘要。

        只公开仓库角色、可用性、分支、短 revision 和脏状态；仓库绝对路径、
        ``git status`` 原文及内部签名只留在 Working State 的私有存储中。
        """
        repositories = [
            self._web_repository_state("control", self.settings.repo_root),
            self._web_repository_state("knowledge", self.settings.knowledge_root),
        ]
        return {
            "status": "ready" if all(item["available"] for item in repositories) else "degraded",
            "repositories": repositories,
            "source": "Git repository metadata",
        }

    def close(self, work_id: str, *, status: str, agent: str, session_id: str, note: str = "") -> dict[str, Any]:
        """追加关闭检查点并将唯一匹配的活动任务安全移动到本机归档。"""
        if status not in {"completed", "abandoned", "superseded"}:
            raise ValueError("close status 必须是 completed、abandoned 或 superseded")
        normalized_agent, normalized_session = self._actor(agent, session_id)
        work_dir = self._active_work_dir(work_id)
        current = self._load_work(work_dir / "work.md")
        if not self.is_authorized_actor(current, normalized_agent, normalized_session):
            raise PermissionError("当前会话未获授权关闭该工作项；请由 owner 先显式交接")
        payload = {
            **{key: current.get(key, "") for key in self.SECTION_FIELDS.values()},
            "project_path": current["project_path"],
            "work_id": work_id,
            "status": "active",
            "agent": normalized_agent,
            "session_id": normalized_session,
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
        """扫描活动与归档状态，以临时 SQLite 原子替换工作索引。

        活动目录先于归档目录扫描，且重复主键使用 ``INSERT OR IGNORE``；
        这是故障恢复时的确定性兜底，保证旧归档不会覆盖仍在活动中的任务。
        """
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
                        status TEXT NOT NULL, work_schema_version TEXT NOT NULL, agent TEXT NOT NULL,
                        session_id TEXT NOT NULL, role TEXT NOT NULL,
                        author_agent TEXT NOT NULL, author_session_id TEXT NOT NULL, author_role TEXT NOT NULL,
                        owner_agent TEXT NOT NULL, owner_session_id TEXT NOT NULL, ownership_mode TEXT NOT NULL,
                        ownership_binding TEXT NOT NULL, participants TEXT NOT NULL,
                        updated_at TEXT NOT NULL, checkpoint_id TEXT NOT NULL, branch TEXT, base_revision TEXT,
                        workspace_dirty INTEGER NOT NULL, workspace_signature TEXT, repositories TEXT NOT NULL,
                        goal TEXT NOT NULL,
                        current_state TEXT, next_steps TEXT, blockers TEXT, path TEXT NOT NULL
                    );
                    CREATE INDEX idx_work_project_status ON work_items(project_id, status, updated_at);
                    """
                )
                for root_name in ("active", "archive"):
                    for path in sorted((self.settings.workspace_root / root_name).rglob("work.md")):
                        data = self._load_work(path)
                        connection.execute(
                            "INSERT OR IGNORE INTO work_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                data.get("work_id"), data.get("project_id"), data.get("project_path"), data.get("status"),
                                data.get("work_schema_version") or "1", data.get("agent") or "", data.get("session_id") or "",
                                data.get("role") or "", data.get("author_agent") or data.get("agent") or "",
                                data.get("author_session_id") or data.get("session_id") or "", data.get("author_role") or data.get("role") or "",
                                data.get("owner_agent") or "", data.get("owner_session_id") or "", data.get("ownership_mode") or "legacy-unbound",
                                data.get("ownership_binding") or "", json.dumps(data.get("participants") or [], ensure_ascii=False, separators=(",", ":")), data.get("updated_at"),
                                data.get("checkpoint_id"), data.get("branch"), data.get("base_revision"),
                                int(bool(data.get("workspace_dirty"))), data.get("workspace_signature"),
                                json.dumps(data.get("repositories") or [], ensure_ascii=False, separators=(",", ":")),
                                data.get("goal"),
                                self._as_text(data.get("current_state")), self._as_text(data.get("next_steps")),
                                self._as_text(data.get("blockers")), str(path),
                            ),
                        )
                count = connection.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
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
        """检查工作索引指纹，缺失、损坏或过期时重建。"""
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
        """比较检查点记录的单仓或多仓签名，判断工作区是否发生变化。"""
        repositories = item.get("repositories") or []
        if repositories:
            current = [
                _repository_snapshot(str(repository.get("role") or "repository"), Path(str(repository["path"])))
                for repository in repositories
                if isinstance(repository, dict) and repository.get("path")
            ]
            return _repositories_signature(current) != item.get("workspace_signature", "")
        return _git_signature(project_path)[3] != item.get("workspace_signature", "")

    def _web_index_status(self) -> dict[str, Any]:
        """确保 Web 查询使用的派生索引可用，并返回不含路径的可解释状态。"""
        status: dict[str, Any] = {
            "source": "working-state Markdown",
            "derived": "SQLite index",
            "status": "ready",
            "rebuilt": False,
        }
        try:
            needs_rebuild = not self.settings.work_db.exists()
            if not needs_rebuild:
                connection = sqlite3.connect(self.settings.work_db)
                try:
                    metadata = dict(connection.execute("SELECT key, value FROM index_metadata"))
                finally:
                    connection.close()
                needs_rebuild = metadata.get("schema_version") != WORK_SCHEMA_VERSION or metadata.get("fingerprint") != self._work_fingerprint()
            if needs_rebuild:
                self.rebuild_index()
                status.update(status="rebuilt", rebuilt=True)
        except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
            # 不向 Web 传递底层路径、SQL 或 traceback；调用方仍能显示明确的降级原因。
            status.update(status="unavailable", reason="derived_index_unavailable")
        return status

    @staticmethod
    def _valid_web_identifier(value: str) -> bool:
        """限制 Web 路由使用的工作/检查点标识，防止路径穿越。"""
        return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,119}", str(value)))

    def _web_row_for_work(self, work_id: str) -> dict[str, Any]:
        """从派生索引取单项内部记录；路径仅供核心继续读取事实源。"""
        connection = sqlite3.connect(self.settings.work_db)
        try:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM work_items WHERE work_id = ? AND status IN ('planned','active','blocked')",
                (work_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None or not self._is_active_record(dict(row)):
            raise KeyError("工作状态不存在")
        return dict(row)

    def _is_active_record(self, row: dict[str, Any]) -> bool:
        """确认索引行对应 ``workspace/active``，不把归档记录带入 v1。"""
        raw_path = str(row.get("path") or "")
        if not raw_path:
            return False
        try:
            Path(raw_path).resolve().relative_to((self.settings.workspace_root / "active").resolve())
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _web_validate_paging(page: int, page_size: int) -> tuple[int, int]:
        """校验 Web 运行状态分页范围，拒绝静默扩大读取预算。"""
        if isinstance(page, bool) or isinstance(page_size, bool) or not isinstance(page, int) or not isinstance(page_size, int):
            raise ValueError("分页参数必须是整数")
        if page < 1 or page > WEB_MAX_PAGE:
            raise ValueError(f"page 必须在 1 至 {WEB_MAX_PAGE} 之间")
        if page_size < 1 or page_size > WEB_LIST_LIMIT:
            raise ValueError(f"page_size 必须在 1 至 {WEB_LIST_LIMIT} 之间")
        return page, page_size

    @staticmethod
    def _web_status_filter(status: str | list[str] | tuple[str, ...] | None) -> list[str]:
        """规范化状态筛选，始终限制在活动 Working State 枚举内。"""
        if status is None or status == "":
            return []
        values = status.split(",") if isinstance(status, str) else list(status)
        normalized = [str(value).strip().lower() for value in values if str(value).strip()]
        if any(value not in OPEN_STATUS for value in normalized):
            raise ValueError("status 只能是 planned、active 或 blocked")
        return list(dict.fromkeys(normalized))

    @staticmethod
    def _web_pagination(page: int, page_size: int, total: int, selected: int) -> dict[str, Any]:
        """生成运行状态和检查点共用的分页摘要。"""
        start = (page - 1) * page_size
        return {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size if total else 0,
            "has_next": start + selected < total,
            "has_previous": page > 1 and total > 0,
        }

    def _web_work_record(self, row: dict[str, Any], *, detail: bool) -> dict[str, Any]:
        """将内部工作状态行投影成不含本机路径的公共记录。"""
        repositories = row.get("repositories") or []
        if isinstance(repositories, str):
            try:
                repositories = json.loads(repositories)
            except json.JSONDecodeError:
                repositories = []
        public: dict[str, Any] = {
            "work_id": self._web_id(row.get("work_id")),
            "project_id": str(row.get("project_id") or ""),
            "status": str(row.get("status") or "unknown"),
            # 兼容字段 agent/session_id/role 的语义是“最新检查点作者”，不是 owner。
            # owner 缺失时保持 null，不能从最新作者或项目路径推断归属。
            "work_schema_version": self._web_text(row.get("work_schema_version") or "1", max_length=20),
            "agent": self._web_text(row.get("agent"), max_length=120),
            "session_id": self._web_optional_text(row.get("session_id"), max_length=120),
            "role": self._web_text(row.get("role"), max_length=120),
            "author_agent": self._web_text(row.get("author_agent") or row.get("agent"), max_length=120),
            "author_session_id": self._web_optional_text(row.get("author_session_id") or row.get("session_id"), max_length=120),
            "author_role": self._web_text(row.get("author_role") or row.get("role"), max_length=120),
            "owner_agent": self._web_optional_text(row.get("owner_agent"), max_length=120),
            "owner_session_id": self._web_optional_text(row.get("owner_session_id"), max_length=120),
            "ownership_mode": self._web_text(row.get("ownership_mode") or "legacy-unbound", max_length=40),
            "ownership_binding": self._web_optional_text(row.get("ownership_binding"), max_length=80),
            "participants": self._web_participants(row.get("participants")),
            "updated_at": self._web_text(row.get("updated_at"), max_length=80),
            "checkpoint_id": self._web_id(row.get("checkpoint_id")),
            "goal": self._web_text(row.get("goal"), max_length=WEB_TEXT_LIMIT),
            "current_state": self._web_text(row.get("current_state"), max_length=WEB_TEXT_LIMIT),
            "next_steps": self._web_text(row.get("next_steps"), max_length=WEB_TEXT_LIMIT),
            "blockers": self._web_text(row.get("blockers"), max_length=WEB_TEXT_LIMIT),
            "branch": self._web_text(row.get("branch"), max_length=200),
            "base_revision": self._short_revision(row.get("base_revision")),
            "workspace_dirty": bool(row.get("workspace_dirty")),
            "repositories": self._web_repositories(repositories),
        }
        public["participant_count"] = len(public["participants"])
        public["truncated"] = any(
            self._web_value_truncated(row.get(field), WEB_TEXT_LIMIT)
            for field in ("goal", "current_state", "next_steps", "blockers")
        )
        if detail:
            try:
                data = self._load_work(Path(str(row.get("path") or "")))
            except (OSError, ValueError):
                data = row
            sections: dict[str, Any] = {}
            for field in self.SECTION_FIELDS.values():
                if field in data:
                    sections[field] = self._web_value(data.get(field), max_length=WEB_TEXT_LIMIT)
            public["sections"] = sections
            public["detail_status"] = "available" if sections else "limited"
            public["sensitivity"] = self._web_text(data.get("sensitivity"), max_length=40)
            checkpoint_dir = Path(str(row.get("path") or "")).parent / "checkpoints"
            checkpoint_files = sorted(checkpoint_dir.glob("*.md")) if checkpoint_dir.is_dir() else []
            public["checkpoint_count"] = len(checkpoint_files)
            public["latest_checkpoint"] = self._web_id(data.get("checkpoint_id"))
            capsule = (
                f"任务 {public['work_id']}（{public['status']}）：{public['goal']}\n"
                f"当前状态：{public['current_state'] or '未记录'}\n"
                f"下一步：{public['next_steps'] or '未记录'}\n"
                f"阻塞：{public['blockers'] or '无'}"
            )
            public["resume_capsule"] = capsule[:1500]
        return public

    def _web_work_dir(self, work_id: str) -> Path:
        """按稳定工作 ID 定位活动目录；阶段 2 第一版不提供归档任务查询。"""
        if not self._valid_web_identifier(work_id):
            raise ValueError("work_id 格式无效")
        root = self.settings.workspace_root / "active"
        for path in sorted(root.glob(f"*/{work_id}")):
            work_file = path / "work.md"
            if not work_file.is_file():
                continue
            try:
                if self._load_work(work_file).get("work_id") == work_id:
                    return path.resolve()
            except (OSError, ValueError):
                continue
        raise KeyError("工作状态不存在")

    def _web_checkpoint_record(self, path: Path, *, include_detail: bool) -> dict[str, Any]:
        """将检查点 Markdown 投影为有限元数据和章节；绝不回传源文件路径。"""
        checkpoint_id = path.stem
        try:
            data = self._load_work(path)
        except (OSError, ValueError):
            return {"checkpoint_id": self._web_id(checkpoint_id) or "unknown", "status": "unavailable", "detail_status": "unavailable"}
        record: dict[str, Any] = {
            "checkpoint_id": self._web_id(data.get("checkpoint_id") or checkpoint_id),
            "based_on": self._web_id(data.get("based_on")) if data.get("based_on") else None,
            "status": self._web_text(data.get("status"), max_length=40),
            "work_schema_version": self._web_text(data.get("work_schema_version") or "1", max_length=20),
            "agent": self._web_text(data.get("agent"), max_length=120),
            "session_id": self._web_optional_text(data.get("session_id"), max_length=120),
            "role": self._web_text(data.get("role"), max_length=120),
            # 检查点作者取事实源中的 author 字段；旧检查点仅有兼容字段时才回退。
            "author_agent": self._web_text(data.get("author_agent") or data.get("agent"), max_length=120),
            "author_session_id": self._web_optional_text(data.get("author_session_id") or data.get("session_id"), max_length=120),
            "author_role": self._web_text(data.get("author_role") or data.get("role"), max_length=120),
            "updated_at": self._web_text(data.get("updated_at"), max_length=80),
            "workspace_dirty": bool(data.get("workspace_dirty")),
            "repositories": self._web_repositories(data.get("repositories") or []),
        }
        if include_detail:
            record["sections"] = {
                field: self._web_value(data.get(field), max_length=WEB_CHECKPOINT_TEXT_LIMIT)
                for field in WEB_CHECKPOINT_DETAIL_FIELDS
                if field in data
            }
            # 详情契约使用扁平字段，sections 仅作为共享核心向后兼容的结构化别名。
            for field, value in record["sections"].items():
                record[field] = value
            record["truncated"] = any(
                self._web_value_truncated(data.get(field), WEB_CHECKPOINT_TEXT_LIMIT)
                for field in WEB_CHECKPOINT_DETAIL_FIELDS
            )
            record["detail_status"] = "available"
        else:
            record["truncated"] = False
            record["detail_status"] = "available"
        return record

    def _web_participants(self, participants: Any) -> list[dict[str, Any]]:
        """投影有界参与者列表；不回传授权时间等内部元数据或本机路径。"""
        if isinstance(participants, str):
            try:
                participants = json.loads(participants)
            except json.JSONDecodeError:
                participants = []
        if not isinstance(participants, list):
            return []
        result: list[dict[str, Any]] = []
        for item in participants[:MAX_PARTICIPANTS]:
            if not isinstance(item, dict):
                continue
            agent = self._web_optional_text(item.get("agent"), max_length=120)
            session_id = self._web_optional_text(item.get("session_id"), max_length=120)
            if not agent or not session_id:
                continue
            result.append({
                "agent": agent,
                "session_id": session_id,
                "role": self._web_text(item.get("role") or "participant", max_length=80),
            })
        return result

    @staticmethod
    def _short_revision(value: Any) -> str:
        """将 Git revision 限制为可展示的短摘要，不形成完整 Git 输出。"""
        revision = str(value or "").strip()
        return revision[:12]

    @staticmethod
    def _web_id(value: Any) -> str | None:
        """投影公共标识为小写字母、数字和连字符，缺失保持 null。"""
        if value is None:
            return None
        text = str(value).strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,119}", text):
            return None
        return text

    @staticmethod
    def _web_optional_text(value: Any, *, max_length: int) -> str | None:
        """清洗可选关联字段；没有事实值时保持 null，不合成会话标识。"""
        if value is None or not str(value).strip():
            return None
        return WorkStateStore._web_text(value, max_length=max_length)

    @staticmethod
    def _web_text(value: Any, *, max_length: int) -> str:
        """对 Web 文本执行密钥和绝对路径脱敏，并限制响应大小。"""
        # 先按目标字段预算脱敏再裁剪，避免旧的 500 字符通用预算把检查点
        # 错误地截成远小于冻结契约的内容；路径替换发生在最终裁剪前。
        text = _redact_text(str(value or ""))[: max_length + 500]
        text = _ABSOLUTE_PATH_PATTERN.sub("[LOCAL_PATH]", text)
        return text[:max_length]

    def _web_value(self, value: Any, *, max_length: int) -> str | list[str]:
        """保留章节的标量/列表形状，同时限制数量、字符数和本机路径泄漏。"""
        if isinstance(value, list):
            return [self._web_text(item, max_length=max_length) for item in value[:WEB_LIST_LIMIT]]
        return self._web_text(value, max_length=max_length)

    @staticmethod
    def _web_value_truncated(value: Any, max_length: int) -> bool:
        """判断标量或列表章节是否超过公共响应的字符预算。"""
        if isinstance(value, list):
            return len(value) > WEB_LIST_LIMIT or any(len(str(item or "")) > max_length for item in value[:WEB_LIST_LIMIT])
        return len(str(value or "")) > max_length

    def _web_repositories(self, repositories: Any) -> list[dict[str, Any]]:
        """投影多仓快照的语义字段；丢弃 path、signature 等内部字段。"""
        if not isinstance(repositories, list):
            return []
        result: list[dict[str, Any]] = []
        for item in repositories[:8]:
            if not isinstance(item, dict):
                continue
            result.append(
                {
                    "role": self._web_text(item.get("role") or "repository", max_length=80),
                    "available": bool(item.get("revision") or item.get("branch")),
                    "branch": self._web_text(item.get("branch"), max_length=200),
                    "revision": self._short_revision(item.get("revision")),
                    "dirty": bool(item.get("dirty")),
                }
            )
        return result

    @staticmethod
    def _web_repository_state(role: str, path: Path) -> dict[str, Any]:
        """读取单仓安全摘要；异常只表现为 unavailable，不暴露命令输出。"""
        revision, branch, dirty, _signature = _git_signature(str(path))
        available = bool(revision or branch)
        return {
            "role": role,
            "available": available,
            "status": "dirty" if available and dirty else ("clean" if available else "unavailable"),
            "branch": branch[:200],
            "revision": revision[:12],
        }

    def _load_work(self, path: Path) -> dict[str, Any]:
        """读取并解析一个工作状态 Markdown；缺少 Front Matter 时拒绝使用。"""
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
        # 旧工作状态只有最后作者字段，不能据此猜测 owner；读取时显式标记
        # legacy-unbound，等待 claim/handoff 完成可信归属初始化。
        ownership = self._ownership(result)
        result.update(ownership)
        result.setdefault("work_schema_version", "1")
        result.setdefault("author_agent", result.get("agent", ""))
        result.setdefault("author_session_id", result.get("session_id", ""))
        result.setdefault("author_role", result.get("role", ""))
        return result

    def _find_archived_work_paths(self, work_id: str) -> list[Path]:
        """查找归档中声明了指定工作 ID 的状态文件。

        不能只按目录名判断，因为历史数据可能来自旧布局；索引真正使用的是
        ``work.md`` 的 Front Matter 中的 ``work_id``，因此这里与重建索引采用同一
        事实源。返回完整路径仅用于拒绝复用时给出可定位的错误信息。
        """
        archive_root = (self.settings.workspace_root / "archive").resolve()
        if not archive_root.exists():
            return []
        matches: list[Path] = []
        for path in sorted(archive_root.rglob("work.md")):
            if self._load_work(path).get("work_id") == work_id:
                matches.append(path.resolve())
        return matches

    def _render_work(self, metadata: dict[str, Any], fields: dict[str, str | list[str]]) -> str:
        """按固定章节顺序生成完整工作状态文档。"""
        body = [f"# 工作状态：{metadata['work_id']}"]
        for title, field in self.SECTION_FIELDS.items():
            body.append(_render_section(title, fields.get(field, "")))
        return render_frontmatter(metadata) + "\n\n" + "\n\n".join(body) + "\n"

    def _metadata_fields(self) -> tuple[str, ...]:
        """返回归档时允许从当前状态复制的元数据字段名。"""
        return (
            "work_id", "project_id", "status", "work_schema_version", "agent", "session_id", "role",
            "author_agent", "author_session_id", "author_role", "owner_agent", "owner_session_id",
            "ownership_mode", "ownership_binding", "participants", "updated_at", "checkpoint_id",
            "based_on", "project_path", "branch", "base_revision", "workspace_dirty", "workspace_signature", "sensitivity",
            "repositories",
        )

    def _safe_work_dir(self, path: Path) -> Path:
        """校验工作目录位于 workspace/active 边界内，防止路径逃逸。"""
        resolved = path.resolve()
        active = (self.settings.workspace_root / "active").resolve()
        try:
            resolved.relative_to(active)
        except ValueError as exc:
            raise ValueError("工作状态路径越过 workspace/active 边界") from exc
        return resolved

    def _atomic_write(self, path: Path, content: str) -> None:
        """通过同目录临时文件和替换写入 UTF-8 文本，避免半写文件。"""
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
        """按活动与归档 work.md 内容计算工作索引指纹。"""
        digest = hashlib.sha256()
        for root_name in ("active", "archive"):
            for path in sorted((self.settings.workspace_root / root_name).rglob("work.md")):
                digest.update(str(path.relative_to(self.settings.workspace_root)).encode("utf-8"))
                digest.update(path.read_bytes())
        return digest.hexdigest()

    def _resume_item(self, row: dict[str, Any]) -> dict[str, Any]:
        """将索引行转换为客户端恢复所需的最小字段和胶囊。"""
        try:
            repositories = json.loads(row.get("repositories") or "[]")
        except json.JSONDecodeError:
            repositories = []
        try:
            participants = json.loads(row.get("participants") or "[]")
        except json.JSONDecodeError:
            participants = []
        result = {
            "work_id": row["work_id"], "project_id": row["project_id"], "status": row["status"],
            "work_schema_version": row.get("work_schema_version") or "1",
            "agent": row["agent"], "session_id": row["session_id"], "role": row["role"],
            "author_agent": row.get("author_agent") or row.get("agent") or "",
            "author_session_id": row.get("author_session_id") or row.get("session_id") or "",
            "author_role": row.get("author_role") or row.get("role") or "",
            "owner_agent": row.get("owner_agent") or "",
            "owner_session_id": row.get("owner_session_id") or "",
            "ownership_mode": row.get("ownership_mode") or "legacy-unbound",
            "ownership_binding": row.get("ownership_binding") or "",
            "participants": participants,
            "updated_at": row["updated_at"], "checkpoint_id": row["checkpoint_id"], "goal": row["goal"],
            "current_state": row.get("current_state") or "", "next_steps": row.get("next_steps") or "",
            "blockers": row.get("blockers") or "", "branch": row.get("branch") or "",
            "base_revision": row.get("base_revision") or "", "workspace_dirty": bool(row.get("workspace_dirty")),
            "workspace_signature": row.get("workspace_signature") or "", "path": row["path"],
            "repositories": repositories,
        }
        repository_summary = "、".join(
            f"{item.get('role') or 'repo'}={item.get('branch') or 'unknown'}@{str(item.get('revision') or 'unknown')[:8]}"
            + ("(dirty)" if item.get("dirty") else "")
            for item in repositories
        )
        capsule = (
            f"任务 {result['work_id']}（{result['status']}）：{result['goal']}\n"
            f"当前状态：{result['current_state'] or '未记录'}\n"
            f"下一步：{result['next_steps'] or '未记录'}\n"
            f"阻塞：{result['blockers'] or '无'}\n"
            + (
                f"恢复前核对 repositories: {repository_summary}。"
                if repository_summary
                else f"恢复前核对 branch={result['branch'] or 'unknown'} revision={result['base_revision'] or 'unknown'}。"
            )
        )
        result["resume_capsule"] = capsule[:1500]
        return result

    @staticmethod
    def _as_text(value: Any) -> str:
        """将列表或标量统一转换为索引所需的文本表示。"""
        if isinstance(value, list):
            return "；".join(str(item) for item in value)
        return str(value or "")
