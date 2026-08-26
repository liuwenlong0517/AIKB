"""解析 AIKB 双仓路径，并集中保存本机运行目录配置。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


KNOWLEDGE_HOME_ENV = "AIKB_KNOWLEDGE_HOME"
KNOWLEDGE_MANIFEST = ".aikb-knowledge.json"
KNOWLEDGE_CONTRACT_VERSION = 1


def discover_repo_root() -> Path:
    """解析并校验 AIKB 控制仓根目录；无效路径时抛出 ``RuntimeError``。"""
    configured = os.environ.get("AIKB_HOME")
    if configured:
        root = Path(configured).expanduser().resolve()
    else:
        root = Path(__file__).resolve().parents[4]
    if not (root / "ENTRY_RULES.md").is_file() or not (root / "system").is_dir():
        raise RuntimeError(f"AIKB_HOME 不是有效的 AIKB 控制仓：{root}")
    return root


def discover_knowledge_root(
    repo_root: Path,
    configured: Path | None = None,
    *,
    use_environment: bool = True,
) -> Path:
    """解析知识仓根目录并验证类型与契约；返回物理路径，失败抛出 ``RuntimeError``。"""
    environment_path = Path(os.environ[KNOWLEDGE_HOME_ENV]).expanduser() if use_environment and os.environ.get(KNOWLEDGE_HOME_ENV) else None
    raw = configured or environment_path
    root = (raw or repo_root / "content").resolve()
    if not root.is_dir():
        raise RuntimeError(f"{KNOWLEDGE_HOME_ENV} 不是有效目录：{root}")
    if root == repo_root:
        raise RuntimeError("知识仓根目录不得与控制仓根目录相同")

    manifest_path = root / KNOWLEDGE_MANIFEST
    if not manifest_path.is_file():
        raise RuntimeError(f"知识仓缺少契约文件 {KNOWLEDGE_MANIFEST}：{root}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"知识仓契约文件无效：{manifest_path}") from exc
    if manifest.get("kind") != "aikb-knowledge":
        raise RuntimeError(f"知识仓 kind 必须是 aikb-knowledge：{manifest_path}")
    if manifest.get("contract_version") != KNOWLEDGE_CONTRACT_VERSION:
        raise RuntimeError(
            f"知识仓 contract_version 不兼容：{manifest.get('contract_version')} != {KNOWLEDGE_CONTRACT_VERSION}"
        )
    return root


@dataclass(frozen=True)
class Settings:
    """保存控制仓、独立知识仓和本机 Working State 的派生路径。"""

    repo_root: Path
    knowledge_root: Path
    content_root: Path
    workspace_root: Path
    knowledge_db: Path
    work_db: Path

    @classmethod
    def load(
        cls,
        repo_root: Path | None = None,
        workspace_root: Path | None = None,
        knowledge_root: Path | None = None,
    ) -> "Settings":
        """加载独立路径设置并检查 workspace/ 与知识仓的安全边界。"""
        root_from_environment = repo_root is None
        root = (repo_root or discover_repo_root()).resolve()
        knowledge = discover_knowledge_root(root, knowledge_root, use_environment=root_from_environment)
        workspace = (workspace_root or root / "workspace").resolve()
        try:
            workspace.relative_to(root)
        except ValueError as exc:
            raise RuntimeError("workspace 必须位于 AIKB 仓库内") from exc
        for protected in (root / "system", workspace):
            try:
                knowledge.relative_to(protected.resolve())
            except ValueError:
                continue
            raise RuntimeError(f"知识仓不得位于控制面或运行面内：{knowledge}")
        db_root = workspace / "db"
        return cls(
            repo_root=root,
            knowledge_root=knowledge,
            content_root=knowledge,
            workspace_root=workspace,
            knowledge_db=db_root / "aikb-knowledge.db",
            work_db=db_root / "aikb-work.db",
        )

    def ensure_runtime_dirs(self) -> None:
        """创建索引、活动任务和归档所需目录；目录已存在时保持幂等。"""
        for path in (
            self.workspace_root / "active",
            self.workspace_root / "archive",
            self.workspace_root / "db",
            self.workspace_root / "runtime",
        ):
            path.mkdir(parents=True, exist_ok=True)
