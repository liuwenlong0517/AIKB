from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from aikb.config import Settings
from aikb.frontmatter import parse_markdown, render_frontmatter
from aikb.hooks import handle_hook
from aikb.indexer import metadata_report, rebuild_knowledge_index
from aikb.knowledge import KnowledgeService
from aikb.server import MCPServer, SERVER_INSTRUCTIONS, TOOLS
from aikb.workstate import WorkStateStore


def entry(entry_id: str, title: str, body: str, relations: list[dict[str, str]] | None = None) -> str:
    metadata = {
        "id": entry_id,
        "type": "knowledge",
        "status": "verified",
        "tags": ["sqlite", "缓存"],
        "applicable_versions": "not-version-specific",
        "last_verified": "2026-08-20",
        "review_when": "实现变化时",
        "supersedes": [],
        "relations": relations or [],
    }
    return (
        render_frontmatter(metadata)
        + f"\n\n# {title}\n\n## 背景\n\n{body}"
        + "\n\n## 解决方案\n\n### 第一部分\n\n父级内容一。"
        + "\n\n### 第二部分\n\n父级内容二。"
        + "\n\n## 验证\n\n测试通过。\n"
    )


class RepoFixture:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="aikb-test-")
        self.root = Path(self.temp.name)
        (self.root / "ENTRY_RULES.md").write_text("# entry\n", encoding="utf-8")
        topic = self.root / "content" / "knowledge" / "engineering"
        topic.mkdir(parents=True)
        (topic / "cache.md").write_text(
            entry(
                "aikb:knowledge:engineering:cache",
                "SQLite 检索缓存",
                "使用 SQLite FTS5 trigram 提供中文检索缓存。",
                [{"type": "related_to", "target": "aikb:knowledge:engineering:index"}],
            ),
            encoding="utf-8",
        )
        (topic / "index.md").write_text(
            entry("aikb:knowledge:engineering:index", "关系索引", "通过稳定 ID 连接知识。"),
            encoding="utf-8",
        )
        inbox = self.root / "content" / "experience" / "inbox"
        inbox.mkdir(parents=True)
        candidate_metadata = {
            "id": "aikb:experience:inbox:candidate",
            "type": "candidate",
            "status": "candidate",
            "tags": ["candidate"],
            "captured_at": "2026-08-20",
            "relations": [],
        }
        (inbox / "candidate.md").write_text(
            render_frontmatter(candidate_metadata) + "\n\n# 候选条目\n\n## 当前假设\n\n尚待验证。\n",
            encoding="utf-8",
        )
        self.settings = Settings.load(self.root, self.root / "workspace")

    def close(self) -> None:
        self.temp.cleanup()


class FrontMatterTests(unittest.TestCase):
    def test_round_trip_nested_relations(self) -> None:
        fixture = RepoFixture()
        try:
            path = fixture.root / "content" / "knowledge" / "engineering" / "cache.md"
            document = parse_markdown(path)
            self.assertEqual(document.metadata["id"], "aikb:knowledge:engineering:cache")
            self.assertEqual(document.metadata["relations"][0]["type"], "related_to")
        finally:
            fixture.close()


class KnowledgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = RepoFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_validate_rebuild_search_read_and_relations(self) -> None:
        report = metadata_report(self.fixture.settings)
        self.assertTrue(report["valid"], report["errors"])
        built = rebuild_knowledge_index(self.fixture.settings)
        self.assertIn(built["tokenizer"], {"trigram", "unicode61"})
        service = KnowledgeService(self.fixture.settings)
        result = service.search("检索缓存")
        self.assertEqual(result["results"][0]["id"], "aikb:knowledge:engineering:cache")
        self.assertLessEqual(len(result["results"][0]["excerpt"]), 700)
        short = service.search("缓存")
        self.assertEqual(short["results"][0]["id"], "aikb:knowledge:engineering:cache")
        read = service.read("aikb:knowledge:engineering:cache", section="验证", max_chars=500)
        self.assertIn("测试通过", read["content"])
        self.assertEqual(read["relations"][0]["target"], "aikb:knowledge:engineering:index")

    def test_read_parent_section_includes_descendants(self) -> None:
        service = KnowledgeService(self.fixture.settings)
        parent = service.read("aikb:knowledge:engineering:cache", section="解决方案", max_chars=1000)
        self.assertIn("### 第一部分", parent["content"])
        self.assertIn("父级内容一", parent["content"])
        self.assertIn("### 第二部分", parent["content"])
        self.assertIn("父级内容二", parent["content"])
        self.assertNotIn("测试通过", parent["content"])

        child = service.read("aikb:knowledge:engineering:cache", section="第一部分", max_chars=1000)
        self.assertIn("父级内容一", child["content"])
        self.assertNotIn("父级内容二", child["content"])

        with self.assertRaises(KeyError):
            service.read("aikb:knowledge:engineering:cache", section="不存在的章节")

    def test_corrupt_database_is_rebuilt(self) -> None:
        rebuild_knowledge_index(self.fixture.settings)
        self.fixture.settings.knowledge_db.write_bytes(b"not sqlite")
        result = KnowledgeService(self.fixture.settings).search("SQLite")
        self.assertGreater(result["count"], 0)
        self.assertTrue(result["index"]["rebuilt"])


class WorkStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = RepoFixture()
        self.store = WorkStateStore(self.fixture.settings)

    def tearDown(self) -> None:
        self.fixture.close()

    def test_cross_agent_checkpoint_redaction_resume_and_close(self) -> None:
        first = self.store.checkpoint(
            {
                "project_path": str(self.fixture.root),
                "goal": "实现知识检索",
                "agent": "codex",
                "session_id": "codex-session",
                "role": "implement",
                "current_state": "api_key=secret-value 已完成核心实现",
                "next_steps": ["由 Claude Code 复核"],
            }
        )
        self.assertTrue(first["redaction_applied"])
        second = self.store.checkpoint(
            {
                "project_path": str(self.fixture.root),
                "work_id": first["work_id"],
                "agent": "claude-code",
                "session_id": "claude-session",
                "role": "verify",
                "current_state": "复核完成",
                "verification": ["查询测试通过"],
            }
        )
        state = self.store.get(project_path=str(self.fixture.root))
        self.assertTrue(state["unique"])
        self.assertEqual(state["items"][0]["agent"], "claude-code")
        self.assertLessEqual(len(state["items"][0]["resume_capsule"]), 1500)
        checkpoint_dir = Path(second["path"]).parent / "checkpoints"
        self.assertEqual(len(list(checkpoint_dir.glob("*.md"))), 2)
        closed = self.store.close(first["work_id"], status="completed", agent="codex", session_id="close-session")
        self.assertEqual(closed["status"], "completed")
        archived_work = parse_markdown(Path(closed["archive_path"]) / "work.md")
        self.assertEqual(archived_work.metadata["status"], "completed")
        final_checkpoint = parse_markdown(
            Path(closed["archive_path"]) / "checkpoints" / f"{closed['last_checkpoint']}.md"
        )
        self.assertEqual(final_checkpoint.metadata["status"], "completed")
        self.assertEqual(self.store.get(project_path=str(self.fixture.root))["count"], 0)

    def test_session_start_only_injects_unique_item(self) -> None:
        self.store.checkpoint(
            {"project_path": str(self.fixture.root), "goal": "恢复测试", "agent": "future-agent", "session_id": "s1"}
        )
        output = handle_hook("future-agent", "session-start", {"cwd": str(self.fixture.root)}, self.fixture.settings)
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("恢复测试", context)
        self.assertLessEqual(len(context), 1800)

    def test_checkpoint_size_and_close_id_are_bounded(self) -> None:
        with self.assertRaises(ValueError):
            self.store.checkpoint(
                {
                    "project_path": str(self.fixture.root),
                    "goal": "超长检查点",
                    "agent": "codex",
                    "session_id": "s1",
                    "decisions": ["x" * 2000 for _ in range(50)],
                }
            )
        with self.assertRaises(ValueError):
            self.store.close("../outside", status="completed", agent="codex", session_id="s1")


class MCPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = RepoFixture()
        rebuild_knowledge_index(self.fixture.settings)
        self.server = MCPServer(self.fixture.settings)

    def tearDown(self) -> None:
        self.fixture.close()

    def test_initialize_tools_and_call(self) -> None:
        initialized = self.server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}
        )
        self.assertEqual(initialized["result"]["protocolVersion"], "2024-11-05")
        listed = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(listed["result"]["tools"]), 5)
        called = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "search_knowledge", "arguments": {"query": "SQLite"}},
            }
        )
        self.assertFalse(called["result"]["isError"])
        payload = json.loads(called["result"]["content"][0]["text"])
        self.assertGreater(payload["count"], 0)

        parent = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "read_knowledge",
                    "arguments": {
                        "id_or_path": "aikb:knowledge:engineering:cache",
                        "section": "解决方案",
                        "max_chars": 1000,
                    },
                },
            }
        )
        self.assertFalse(parent["result"]["isError"])
        parent_payload = json.loads(parent["result"]["content"][0]["text"])
        self.assertIn("### 第一部分", parent_payload["content"])
        self.assertIn("### 第二部分", parent_payload["content"])

    def test_client_visible_budget(self) -> None:
        self.assertLessEqual(len(SERVER_INSTRUCTIONS), 512)
        self.assertLessEqual(len(json.dumps(TOOLS, ensure_ascii=False, separators=(",", ":"))), 4000)
        self.assertEqual([tool["name"] for tool in TOOLS], [
            "search_knowledge", "read_knowledge", "get_work_state", "checkpoint_work_state", "close_work_state"
        ])


if __name__ == "__main__":
    unittest.main()
