"""Working State 的 Web 安全只读模型测试。"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from aikb.config import Settings
from aikb.workstate import WorkStateStore


def _git_init(path: Path) -> None:
    """为夹具建立可读取 revision 的最小 Git 仓库。"""
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "AIKB Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "aikb-test@example.invalid"], cwd=path, check=True)
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=path, check=True, capture_output=True)


def _keys(value: Any) -> set[str]:
    """递归收集公共结果的键，用于保证内部路径字段没有穿透。"""
    result: set[str] = set()
    if isinstance(value, dict):
        result.update(value.keys())
        for item in value.values():
            result.update(_keys(item))
    elif isinstance(value, list):
        for item in value:
            result.update(_keys(item))
    return result


class WebRuntimeModelTests(unittest.TestCase):
    """验证 Web 只读模型的事实源、字段和尺寸边界。"""

    def setUp(self) -> None:
        """准备独立控制仓、知识仓和活动 Working State。"""
        self.temp = tempfile.TemporaryDirectory(prefix="aikb-web-work-")
        self.root = Path(self.temp.name)
        (self.root / "system").mkdir()
        (self.root / "ENTRY_RULES.md").write_text("# fixture\n", encoding="utf-8")
        content = self.root / "content"
        content.mkdir()
        (content / ".aikb-knowledge.json").write_text(
            json.dumps({"kind": "aikb-knowledge", "contract_version": 1}), encoding="utf-8"
        )
        _git_init(self.root)
        _git_init(content)
        self.settings = Settings.load(self.root, self.root / "workspace")
        self.store = WorkStateStore(self.settings)
        self.created = self.store.checkpoint(
            {
                "project_path": str(self.root),
                "goal": "验证 Web 工作状态安全模型",
                "current_state": "正在验证",
                "next_steps": ["检查公共字段", "确认索引状态"],
                "changed_files": [str(self.root / "secret.txt"), "system/tools/aikb-mcp/aikb/workstate.py"],
                "verification": "password=do-not-return",
                "agent": "codex",
                "session_id": "session-visible-for-correlation",
            }
        )

    def tearDown(self) -> None:
        """释放临时夹具。"""
        self.temp.cleanup()

    def test_active_list_and_detail_are_path_free(self) -> None:
        """活动列表和详情只返回安全字段，且查询不改写 Markdown 事实源。"""
        work_file = next((self.settings.workspace_root / "active").rglob("work.md"))
        before = work_file.read_bytes()
        listing = self.store.web_active_work_states()
        detail = self.store.web_work_state(self.created["work_id"])
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["items"][0]["work_id"], self.created["work_id"])
        self.assertEqual(detail["item"]["sections"]["changed_files"][0], "[LOCAL_PATH]")
        self.assertNotIn("project_path", _keys(listing))
        self.assertNotIn("path", _keys(listing))
        self.assertNotIn("workspace_signature", _keys(detail))
        self.assertNotIn(str(self.root), json.dumps(detail, ensure_ascii=False))
        self.assertNotIn("do-not-return", json.dumps(detail, ensure_ascii=False))
        self.assertEqual(before, work_file.read_bytes())

        item = detail["item"]
        self.assertEqual(item["work_schema_version"], "2")
        self.assertEqual(item["owner_agent"], "codex")
        self.assertEqual(item["author_agent"], "codex")
        self.assertEqual(item["agent"], item["author_agent"])
        self.assertEqual(item["ownership_mode"], "session-bound")
        self.assertEqual(item["participant_count"], 0)

    def test_web_projects_complete_session_id_with_case_and_punctuation(self) -> None:
        """只读 Web 投影保留 160 字符内完整会话值，不回退到旧 120 上限。"""
        session_id = "Case/Session?" + "x" * (160 - len("Case/Session?"))
        created = self.store.checkpoint(
            {"project_path": str(self.root), "goal": "完整 Web 会话", "agent": "codex", "session_id": session_id}
        )
        item = self.store.web_work_state(created["work_id"])["item"]
        self.assertEqual(item["owner_session_id"], session_id)
        self.assertEqual(item["author_session_id"], session_id)
        self.assertEqual(item["session_id"], session_id)

    def test_legacy_work_has_no_guessed_owner_and_latest_author_is_explicit(self) -> None:
        """旧 v1 文档保留兼容作者字段，但 owner 必须为空且标记未认领。"""
        work_file = next((self.settings.workspace_root / "active").rglob("work.md"))
        source = work_file.read_text(encoding="utf-8")
        # 删除 v2 owner 元数据，模拟升级前事实源；不改变正文或最新作者。
        source = re.sub(
            r"^(?:work_schema_version|owner_agent|owner_session_id|ownership_mode|ownership_binding|participants):.*\r?\n?",
            "",
            source,
            flags=re.MULTILINE,
        )
        work_file.write_text(source, encoding="utf-8")
        self.settings.work_db.unlink()
        item = self.store.web_active_work_states()["items"][0]
        self.assertEqual(item["work_schema_version"], "1")
        self.assertIsNone(item["owner_agent"])
        self.assertIsNone(item["owner_session_id"])
        self.assertEqual(item["ownership_mode"], "legacy-unbound")
        self.assertEqual(item["author_agent"], "codex")
        self.assertEqual(item["agent"], "codex")

    def test_checkpoint_list_and_limited_detail(self) -> None:
        """检查点只暴露有限元数据和裁剪后的章节，不回传源文件路径。"""
        checkpoints = self.store.web_checkpoints(self.created["work_id"])
        checkpoint_id = self.created["checkpoint_id"]
        detail = self.store.web_checkpoint(self.created["work_id"], checkpoint_id.lower())
        self.assertEqual(checkpoints["count"], 1)
        self.assertEqual(detail["item"]["checkpoint_id"], checkpoint_id.lower())
        self.assertLessEqual(len(detail["item"]["sections"]["verification"]), 4000)
        self.assertNotIn("project_path", _keys(checkpoints))
        self.assertNotIn("path", _keys(detail))
        self.assertNotIn(str(self.root), json.dumps(detail, ensure_ascii=False))
        self.assertEqual(detail["item"]["author_agent"], "codex")
        self.assertEqual(detail["item"]["author_session_id"], "session-visible-for-correlation")

    def test_repository_summary_is_semantic_and_short(self) -> None:
        """双仓摘要返回角色、分支、短 revision 和脏状态，不返回 Git 原文。"""
        summary = self.store.web_repository_summary()
        self.assertEqual([item["role"] for item in summary["repositories"]], ["control", "knowledge"])
        self.assertTrue(all(len(item["revision"]) <= 12 for item in summary["repositories"]))
        self.assertNotIn("path", _keys(summary))
        self.assertNotIn("signature", _keys(summary))
        self.assertNotIn(str(self.root), json.dumps(summary, ensure_ascii=False))

    def test_active_filters_pagination_and_bounds(self) -> None:
        """筛选只命中活动状态，分页稳定且超限/空集有明确结果。"""
        second = self.store.checkpoint(
            {
                "project_path": str(self.root),
                "work_id": "second-task",
                "goal": "第二个活动任务",
                "status": "blocked",
                "agent": "luna",
                "session_id": "luna-second",
            }
        )
        project = self.store.web_active_work_states()["items"][0]["project_id"]
        page = self.store.web_active_work_states(project_id=project, page=1, page_size=1)
        self.assertEqual(page["pagination"]["total"], 2)
        self.assertEqual(page["pagination"]["page_size"], 1)
        self.assertTrue(page["pagination"]["has_next"])
        page_two = self.store.web_active_work_states(project_id=project, page=2, page_size=1)
        self.assertFalse(page_two["pagination"]["has_next"])
        self.assertNotEqual(page["items"][0]["work_id"], page_two["items"][0]["work_id"])
        self.assertEqual(self.store.web_active_work_states(status="blocked")["count"], 1)
        self.assertEqual(self.store.web_active_work_states(agent="luna")["items"][0]["work_id"], second["work_id"])
        self.assertEqual(self.store.web_active_work_states(status="active,planned")["count"], 1)
        empty = self.store.web_active_work_states(project_id="not-a-real-project")
        self.assertEqual(empty["items"], [])
        self.assertEqual(empty["pagination"]["total"], 0)
        self.assertFalse(empty["pagination"]["has_next"])
        with self.assertRaises(ValueError):
            self.store.web_active_work_states(page_size=51)
        with self.assertRaises(ValueError):
            self.store.web_active_work_states(page=0)
        with self.assertRaises(ValueError):
            self.store.web_active_work_states(status="completed")

    def test_checkpoint_pagination_boundary_and_active_only(self) -> None:
        """检查点分页受 50 条上限约束，归档任务不会被公共接口读取。"""
        self.store.checkpoint(
            {
                "project_path": str(self.root),
                "work_id": self.created["work_id"],
                "goal": "追加检查点",
                "agent": "codex",
                "session_id": "session-visible-for-correlation",
            }
        )
        result = self.store.web_checkpoints(self.created["work_id"], page=1, page_size=1)
        self.assertEqual(result["pagination"]["total"], 2)
        self.assertTrue(result["pagination"]["has_next"])
        self.assertEqual(len(result["items"]), 1)
        with self.assertRaises(ValueError):
            self.store.web_checkpoints(self.created["work_id"], page_size=51)
        with self.assertRaises(KeyError):
            self.store.web_checkpoints("archived-task")

    def test_missing_index_reports_rebuild_without_database_path(self) -> None:
        """缺失索引可重建派生层，并以状态字段解释结果而非暴露数据库路径。"""
        self.settings.work_db.unlink()
        result = self.store.web_active_work_states()
        self.assertEqual(result["index"]["status"], "rebuilt")
        self.assertTrue(result["index"]["rebuilt"])
        self.assertNotIn("database", _keys(result["index"]))
        self.assertNotIn(str(self.settings.work_db), json.dumps(result, ensure_ascii=False))

    def test_cross_platform_paths_and_checkpoint_limits(self) -> None:
        """确认跨平台路径完整替换，检查点标量/列表项统一 4000 与 50 项上限。"""
        for value in ("/opt/service/private", "/srv/app/log", "/root/secret", "/usr/local/bin", "/mnt/data/file", "/unknown/two-level"):
            self.assertNotIn(value.split("/")[1], WorkStateStore._web_text(value, max_length=4000))
            self.assertIn("[LOCAL_PATH]", WorkStateStore._web_text(value, max_length=4000))
        logical = "content/projects/aikb-web/README.md"
        self.assertEqual(WorkStateStore._web_text(logical, max_length=4000), logical)
        scalar = "x" * 5000
        values = [f"item-{index}" for index in range(55)]
        self.assertEqual(len(self.store._web_value(values, max_length=4000)), 50)
        self.assertEqual(len(self.store._web_value(scalar, max_length=4000)), 4000)
        self.assertTrue(self.store._web_value_truncated(values, 4000))
        self.assertTrue(self.store._web_value_truncated(scalar, 4000))


if __name__ == "__main__":
    unittest.main()
