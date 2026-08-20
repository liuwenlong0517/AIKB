from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def discover_repo_root() -> Path:
    configured = os.environ.get("AIKB_HOME")
    if configured:
        root = Path(configured).expanduser().resolve()
    else:
        root = Path(__file__).resolve().parents[4]
    if not (root / "ENTRY_RULES.md").is_file() or not (root / "content").is_dir():
        raise RuntimeError(f"AIKB_HOME 不是有效的 AIKB 仓库：{root}")
    return root


@dataclass(frozen=True)
class Settings:
    repo_root: Path
    content_root: Path
    workspace_root: Path
    knowledge_db: Path
    work_db: Path

    @classmethod
    def load(cls, repo_root: Path | None = None, workspace_root: Path | None = None) -> "Settings":
        root = (repo_root or discover_repo_root()).resolve()
        workspace = (workspace_root or root / "workspace").resolve()
        try:
            workspace.relative_to(root)
        except ValueError as exc:
            raise RuntimeError("workspace 必须位于 AIKB 仓库内") from exc
        db_root = workspace / "db"
        return cls(
            repo_root=root,
            content_root=root / "content",
            workspace_root=workspace,
            knowledge_db=db_root / "aikb-knowledge.db",
            work_db=db_root / "aikb-work.db",
        )

    def ensure_runtime_dirs(self) -> None:
        for path in (
            self.workspace_root / "active",
            self.workspace_root / "archive",
            self.workspace_root / "db",
            self.workspace_root / "runtime",
        ):
            path.mkdir(parents=True, exist_ok=True)
