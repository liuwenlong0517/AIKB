"""AIKB Python 核心回归测试，覆盖双仓、索引、MCP、hook 与工作状态边界。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from aikb.audit import AUDIT_FIELDS, AuditStore, _redact_text, audit_summary, combine_invocations, render_markdown, summarize_tool_action
from aikb.config import Settings
from aikb.frontmatter import parse_markdown, render_frontmatter
from aikb.hooks import handle_hook
from aikb.indexer import content_fingerprint, iter_content_files, metadata_report, rebuild_knowledge_index
from aikb.knowledge import KnowledgeService
from aikb.server import MCPServer, SERVER_INSTRUCTIONS, TOOLS
from aikb.workstate import WorkStateStore


def entry(entry_id: str, title: str, body: str, relations: list[dict[str, str]] | None = None) -> str:
    """生成带嵌套章节和关系元数据的最小测试知识条目。"""
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


def initialize_git_repository(path: Path) -> None:
    """初始化只供 Working State 多仓测试使用的最小 Git 仓库。"""
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "AIKB Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "aikb-test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "test fixture"], cwd=path, check=True, capture_output=True)


class RepoFixture:
    """创建隔离的临时控制仓、知识仓和 Working State 测试夹具。"""

    def __init__(self) -> None:
        """准备一个包含正式条目和候选条目的临时知识仓。"""
        self.temp = tempfile.TemporaryDirectory(prefix="aikb-test-")
        self.root = Path(self.temp.name)
        (self.root / "ENTRY_RULES.md").write_text("# entry\n", encoding="utf-8")
        topic = self.root / "content" / "knowledge" / "engineering"
        topic.mkdir(parents=True)
        (self.root / "content" / ".aikb-knowledge.json").write_text(
            json.dumps(
                {"kind": "aikb-knowledge", "contract_version": 1, "knowledge_schema_version": 1},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
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
        """释放临时目录，避免测试数据留在本机。"""
        self.temp.cleanup()


class FrontMatterTests(unittest.TestCase):
    """验证 Front Matter 解析与嵌套关系往返。"""

    def test_round_trip_nested_relations(self) -> None:
        """确认知识条目的稳定 ID 和关系对象能够被解析。"""
        fixture = RepoFixture()
        try:
            path = fixture.root / "content" / "knowledge" / "engineering" / "cache.md"
            document = parse_markdown(path)
            self.assertEqual(document.metadata["id"], "aikb:knowledge:engineering:cache")
            self.assertEqual(document.metadata["relations"][0]["type"], "related_to")
        finally:
            fixture.close()


class KnowledgeTests(unittest.TestCase):
    """验证知识校验、索引重建、搜索、章节读取和指纹边界。"""

    def setUp(self) -> None:
        """为每个知识测试创建独立夹具。"""
        self.fixture = RepoFixture()

    def tearDown(self) -> None:
        """清理当前测试夹具。"""
        self.fixture.close()

    def test_validate_rebuild_search_read_and_relations(self) -> None:
        """验证元数据报告、索引、搜索结果、章节内容和关系返回。"""
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
        """确认读取父章节包含子章节，而读取子章节不会越界到兄弟章节。"""
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
        """确认数据库损坏时搜索路径会自动重建派生索引。"""
        rebuild_knowledge_index(self.fixture.settings)
        self.fixture.settings.knowledge_db.write_bytes(b"not sqlite")
        result = KnowledgeService(self.fixture.settings).search("SQLite")
        self.assertGreater(result["count"], 0)
        self.assertTrue(result["index"]["rebuilt"])

    def test_external_knowledge_root_keeps_stable_logical_paths(self) -> None:
        """确认独立知识仓的物理位置不会改变客户端逻辑路径。"""
        with tempfile.TemporaryDirectory(prefix="aikb-split-test-") as temp:
            base = Path(temp)
            control = base / "control"
            knowledge = base / "knowledge-store"
            (control / "system").mkdir(parents=True)
            (control / "ENTRY_RULES.md").write_text("# entry\n", encoding="utf-8")
            topic = knowledge / "knowledge" / "engineering"
            topic.mkdir(parents=True)
            (knowledge / ".aikb-knowledge.json").write_text(
                json.dumps(
                    {"kind": "aikb-knowledge", "contract_version": 1, "knowledge_schema_version": 1},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (topic / "external.md").write_text(
                entry("aikb:knowledge:engineering:external", "外置知识仓", "知识仓位置不影响逻辑路径。"),
                encoding="utf-8",
            )
            settings = Settings.load(control, control / "workspace", knowledge)
            built = rebuild_knowledge_index(settings)
            self.assertEqual(built["documents"], 1)
            result = KnowledgeService(settings).read("content/knowledge/engineering/external.md")
            self.assertEqual(result["id"], "aikb:knowledge:engineering:external")
            self.assertEqual(result["path"], "content/knowledge/engineering/external.md")

    def test_navigation_git_metadata_and_control_changes_do_not_affect_fingerprint(self) -> None:
        """确认导航文件、知识仓 Git 元数据和控制面变化不影响知识指纹。"""
        before = content_fingerprint(self.fixture.settings.content_root)
        (self.fixture.settings.content_root / "CATALOG.md").write_text("# catalog\n", encoding="utf-8")
        git_dir = self.fixture.settings.content_root / ".git"
        git_dir.mkdir()
        (git_dir / "metadata.md").write_text("# ignored\n", encoding="utf-8")
        (self.fixture.root / "ENTRY_RULES.md").write_text("# changed control\n", encoding="utf-8")
        after = content_fingerprint(self.fixture.settings.content_root)
        self.assertEqual(before, after)
        self.assertNotIn(git_dir / "metadata.md", list(iter_content_files(self.fixture.settings.content_root)))


class AuditTests(unittest.TestCase):
    """验证文本审计的落盘、脱敏、聚合、并发与故障降级。"""

    def setUp(self) -> None:
        self.fixture = RepoFixture()
        fixed = datetime(2026, 8, 27, 10, 32, 18, 413000, tzinfo=timezone.utc)
        self.store = AuditStore(self.fixture.settings, clock=lambda: fixed)

    def tearDown(self) -> None:
        self.fixture.close()

    def test_jsonl_round_trip_redaction_and_reports(self) -> None:
        query = "中文 Authorization: Bearer plain-text-secret-value"
        action = summarize_tool_action(
            "search_knowledge",
            {"query": query, "type": "knowledge", "status": "verified", "tags": ["内部", "秘密"], "limit": 5},
        )
        self.assertEqual(action, {
            "query_preview": "中文 Authorization=[REDACTED]", "type": "knowledge", "status": "verified",
            "tags": ["内部", "秘密"], "limit": 5,
        })
        invocation = self.store.start(
            source="mcp", agent="codex", operation="search_knowledge",
            action=action, client={"name": "Codex 中文", "version": "1"}, connection_id="connection-1",
        )
        self.store.finish(
            invocation, source="mcp", agent="codex", operation="search_knowledge", status="succeeded",
            outcome_code="results_returned", result_summary={"count": 2}, connection_id="connection-1",
        )
        path = self.fixture.settings.workspace_root / "audit" / "events" / "2026" / "08" / "2026-08-27.jsonl"
        raw = path.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8")
        self.assertIn("中文", text)
        self.assertNotIn(query, text)
        self.assertNotIn("plain-text-secret-value", text)
        loaded = self.store.read_events()
        self.assertTrue(all(set(event) - {"_fallback"} == set(AUDIT_FIELDS) for event in loaded["events"]))
        schema = json.loads((TOOL_ROOT.parents[1] / "schemas" / "audit-event.schema.json").read_text(encoding="utf-8"))
        self.assertIn(2, schema["properties"]["schema_version"]["enum"])
        items = combine_invocations(loaded["events"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "succeeded")
        self.assertIn("MCP 连接", items[0]["session_label"])
        self.assertIn("检索知识", items[0]["action_text"])
        self.assertIn("返回 2 条结果", items[0]["result_text"])
        summary = audit_summary(items, damaged=loaded["damaged"], fallback_count=0)
        markdown = render_markdown(items, summary, "2026-08-27")
        self.assertIn("AIKB 审计报告", markdown)
        self.assertIn("检索知识", markdown)
        base = [
            sys.executable, "-m", "aikb", "--repo-root", str(self.fixture.root),
            "--workspace-root", str(self.fixture.settings.workspace_root), "audit",
        ]
        listed = subprocess.run(
            [*base, "list", "--date", "2026-08-27", "--agent", "codex"], cwd=TOOL_ROOT,
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        self.assertEqual(json.loads(listed.stdout)["count"], 1)
        shown = subprocess.run(
            [*base, "show", invocation["invocation_id"]], cwd=TOOL_ROOT,
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        self.assertEqual(json.loads(shown.stdout)["status"], "succeeded")
        report_path = self.fixture.settings.workspace_root / "audit" / "reports" / "2026-08-27.xlsx"
        generated = subprocess.run(
            [*base, "report", "--date", "2026-08-27"], cwd=TOOL_ROOT,
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        self.assertEqual(json.loads(generated.stdout)["output"], str(report_path.resolve()))
        self.assertTrue(zipfile.is_zipfile(report_path))
        with zipfile.ZipFile(report_path) as report:
            self.assertIn("xl/worksheets/sheet2.xml", report.namelist())
            detail_xml = report.read("xl/worksheets/sheet2.xml").decode("utf-8")
            styles_xml = report.read("xl/styles.xml").decode("utf-8")
        self.assertIn("search_knowledge", detail_xml)
        self.assertIn("autoFilter", detail_xml)
        self.assertIn('xSplit="6"', detail_xml)
        self.assertIn('wrapText="1"', styles_xml)
        custom_path = self.fixture.settings.workspace_root / "custom-audit.xlsx"
        subprocess.run(
            [*base, "report", "--date", "2026-08-27", "--output", str(custom_path)], cwd=TOOL_ROOT,
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        self.assertTrue(custom_path.is_file())
        markdown_path = self.fixture.settings.workspace_root / "audit" / "reports" / "2026-08-27.md"
        deprecated = subprocess.run(
            [*base, "report-md", "--date", "2026-08-27"], cwd=TOOL_ROOT,
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        self.assertEqual(json.loads(deprecated.stdout)["output"], str(markdown_path.resolve()))
        self.assertIn("暂时弃用", deprecated.stderr)
        self.assertIn("检索知识", markdown_path.read_text(encoding="utf-8"))
        invalid_output = subprocess.run(
            [*base, "report", "--date", "2026-08-27", "--output", str(self.fixture.settings.workspace_root / "audit")],
            cwd=TOOL_ROOT, capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(invalid_output.returncode, 2)
        self.assertIn("--output 必须是报告文件路径，不能是目录", invalid_output.stderr)
        self.assertNotIn("Traceback", invalid_output.stderr)
        invalid_extension = subprocess.run(
            [*base, "report", "--date", "2026-08-27", "--output", str(self.fixture.settings.workspace_root / "bad.md")],
            cwd=TOOL_ROOT, capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(invalid_extension.returncode, 2)
        self.assertIn("--output 必须使用 .xlsx 扩展名", invalid_extension.stderr)

    def test_diagnostic_capture_is_opt_in_and_redacted(self) -> None:
        """确认诊断输入输出只在显式开启后保存，并继续脱敏敏感字段。"""
        diagnostic_store = AuditStore(replace(self.fixture.settings, audit_capture_level="diagnostic"), clock=self.store.clock)
        invocation = diagnostic_store.start(source="mcp", agent="codex", operation="search_knowledge", connection_id="test-connection")
        diagnostic_store.write_diagnostic(
            invocation_id=invocation["invocation_id"], source="mcp", agent="codex", operation="search_knowledge",
            phase="input", session_id=None, session_label="Codex · MCP 连接 001",
            payload={"query": "缓存", "authorization": "Bearer plain-text-secret-value"},
        )
        captured = diagnostic_store.read_diagnostics(invocation["invocation_id"])
        self.assertEqual(captured["count"], 1)
        serialized = json.dumps(captured, ensure_ascii=False)
        self.assertIn("缓存", serialized)
        self.assertNotIn("plain-text-secret-value", serialized)

    def test_redaction_covers_bearer_and_multiword_secret_values(self) -> None:
        """确认常见认证头和包含空格的秘密不会在审计文本中残留后半段。"""
        for source in (
            "Authorization: Bearer plain-text-secret-value",
            "password=secret with spaces",
            "access_token: 'quoted secret value'",
        ):
            redacted = _redact_text(source)
            self.assertIn("[REDACTED]", redacted)
            self.assertNotIn("secret", redacted.lower().replace("[redacted]", ""))

    def test_incomplete_corrupt_and_fallback_are_reported(self) -> None:
        self.store.start(source="hook", agent="claude-code", operation="session-start")
        event_path = self.fixture.settings.workspace_root / "audit" / "events" / "2026" / "08" / "2026-08-27.jsonl"
        with event_path.open("a", encoding="utf-8") as stream:
            stream.write("{broken json\n")
        with mock.patch.object(self.store, "_lock", side_effect=TimeoutError("busy")):
            result = self.store.write({
                "record_type": "wrapper_failure", "source": "hook", "agent": "codex",
                "operation": "stop", "status": "failed", "outcome_code": "lock_timeout",
            })
        self.assertTrue(result["written"])
        self.assertTrue(result["fallback"])
        loaded = self.store.read_events()
        items = combine_invocations(loaded["events"])
        self.assertTrue(any(item.get("status") == "incomplete" for item in items))
        self.assertTrue(any(item.get("record_type") == "wrapper_failure" for item in items))
        self.assertEqual(len(loaded["damaged"]), 1)

    def test_concurrent_processes_produce_parseable_lines(self) -> None:
        code = (
            "import sys; from pathlib import Path; from aikb.audit import AuditStore; from aikb.config import Settings; "
            "root=Path(sys.argv[1]); s=Settings.load(root, root/'workspace'); a=AuditStore(s); "
            "[(lambda x: a.finish(x,source='mcp',agent='codex',operation='ping-test',status='succeeded',outcome_code='ok'))"
            "(a.start(source='mcp',agent='codex',operation='ping-test')) for _ in range(10)]"
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(TOOL_ROOT)
        processes = [
            subprocess.Popen([sys.executable, "-c", code, str(self.fixture.root)], env=environment)
            for _ in range(4)
        ]
        for process in processes:
            self.assertEqual(process.wait(timeout=20), 0)
        loaded = AuditStore(self.fixture.settings).read_events()
        self.assertEqual(loaded["damaged"], [])
        self.assertEqual(len(combine_invocations(loaded["events"])), 40)


class WorkStateTests(unittest.TestCase):
    """验证检查点脱敏、恢复、关闭、多仓和尺寸安全边界。"""

    def setUp(self) -> None:
        """创建工作状态服务及其隔离夹具。"""
        self.fixture = RepoFixture()
        self.store = WorkStateStore(self.fixture.settings)

    def tearDown(self) -> None:
        """释放工作状态测试夹具。"""
        self.fixture.close()

    def test_cross_agent_checkpoint_redaction_resume_and_close(self) -> None:
        """确认跨 Agent 检查点会脱敏、可恢复并能移动到归档。"""
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
        """确认 SessionStart 只为唯一活动任务注入恢复胶囊。"""
        self.store.checkpoint(
            {"project_path": str(self.fixture.root), "goal": "恢复测试", "agent": "future-agent", "session_id": "s1"}
        )
        output = handle_hook("future-agent", "SessionStart", {"cwd": str(self.fixture.root)}, self.fixture.settings)
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("恢复测试", context)
        self.assertLessEqual(len(context), 1800)
        handle_hook("future-agent", "stop", {"cwd": str(self.fixture.root), "stop_hook_active": True}, self.fixture.settings)
        handle_hook("future-agent", "pre-compact", {"cwd": str(self.fixture.root)}, self.fixture.settings)
        handle_hook("future-agent", "session-end", {"cwd": str(self.fixture.root)}, self.fixture.settings)
        handle_hook("future-agent", "session-start", {}, self.fixture.settings)
        items = combine_invocations(AuditStore(self.fixture.settings).read_events()["events"])
        outcomes = {item.get("outcome_code") for item in items}
        self.assertTrue({
            "resume_context_injected", "recursion_skipped", "pre_compact_observed",
            "session_end_observed", "invalid_project",
        }.issubset(outcomes))

    def test_hook_cli_forces_utf8_with_legacy_environment(self) -> None:
        """确认旧代码页环境下 hook CLI 仍能无替换字符地往返中文。"""
        project = self.fixture.root / "中文项目"
        project.mkdir()
        self.store.checkpoint(
            {"project_path": str(project), "goal": "编码边界验证", "agent": "future-agent", "session_id": "utf8"}
        )
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "0"
        environment["PYTHONIOENCODING"] = "cp936"
        payload = json.dumps({"cwd": str(project), "prompt": "中文输入"}, ensure_ascii=False).encode("utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8=0",
                "-m",
                "aikb",
                "--repo-root",
                str(self.fixture.root),
                "--workspace-root",
                str(self.fixture.root / "workspace"),
                "hook",
                "--agent",
                "future-agent",
                "--event",
                "session-start",
            ],
            cwd=TOOL_ROOT,
            env=environment,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        raw_output = completed.stdout.decode("utf-8")
        output = json.loads(raw_output)
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("AIKB 发现一个本机活动任务", context)
        self.assertIn("编码边界验证", context)
        self.assertNotIn("\ufffd", raw_output)

    def test_checkpoint_size_and_close_id_are_bounded(self) -> None:
        """确认检查点大小和关闭入口的工作 ID 都受到边界约束。"""
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

    def test_workspace_root_must_stay_under_the_ignored_runtime_directory(self) -> None:
        """确认显式 workspace 参数不能把审计和索引写入控制仓的可跟踪目录。"""
        with self.assertRaisesRegex(RuntimeError, "workspace/"):
            Settings.load(
                repo_root=self.fixture.root,
                workspace_root=self.fixture.root / "tracked-area",
                knowledge_root=self.fixture.settings.knowledge_root,
            )

    def test_aikb_maintenance_tracks_control_and_independent_knowledge_repositories(self) -> None:
        """确认维护控制仓时会跟踪独立知识仓，并检测任一仓库变化。"""
        with tempfile.TemporaryDirectory(prefix="aikb-work-multi-repo-") as temp:
            base = Path(temp)
            control = base / "control"
            knowledge = base / "knowledge"
            (control / "system").mkdir(parents=True)
            (control / "ENTRY_RULES.md").write_text("# entry\n", encoding="utf-8")
            (control / ".gitignore").write_text("workspace/\n", encoding="utf-8")
            topic = knowledge / "knowledge" / "engineering"
            topic.mkdir(parents=True)
            (knowledge / ".aikb-knowledge.json").write_text(
                json.dumps({"kind": "aikb-knowledge", "contract_version": 1, "knowledge_schema_version": 1}),
                encoding="utf-8",
            )
            document_path = topic / "multi-repo.md"
            document_path.write_text(
                entry("aikb:knowledge:engineering:multi-repo", "双仓工作状态", "同时跟踪两个仓库。"),
                encoding="utf-8",
            )
            initialize_git_repository(control)
            initialize_git_repository(knowledge)

            settings = Settings.load(control, control / "workspace", knowledge)
            store = WorkStateStore(settings)
            checkpoint = store.checkpoint(
                {"project_path": str(control), "goal": "双仓检查点", "agent": "codex", "session_id": "multi"}
            )
            state = store.get(work_id=checkpoint["work_id"])["items"][0]
            self.assertEqual([item["role"] for item in state["repositories"]], ["project", "knowledge"])
            self.assertIn("knowledge=main@", state["resume_capsule"])
            self.assertFalse(store.is_dirty_since_checkpoint(str(control), state))

            document_path.write_text(document_path.read_text(encoding="utf-8") + "\n变化。\n", encoding="utf-8")
            self.assertTrue(store.is_dirty_since_checkpoint(str(control), state))


class MCPTests(unittest.TestCase):
    """验证 JSON-RPC MCP 初始化、工具调用和客户端预算。"""

    def setUp(self) -> None:
        """建立已完成知识索引的 MCP 测试服务。"""
        self.fixture = RepoFixture()
        rebuild_knowledge_index(self.fixture.settings)
        self.server = MCPServer(self.fixture.settings, agent="codex")

    def tearDown(self) -> None:
        """释放 MCP 测试夹具。"""
        self.fixture.close()

    def test_initialize_tools_and_call(self) -> None:
        """确认协议协商、工具列表和知识工具调用均返回有效响应。"""
        initialized = self.server.handle(
            {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "Codex Test", "version": "1"}},
            }
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
        failed = self.server.handle({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "unknown_tool", "arguments": {"prompt": "must not be logged"}},
        })
        self.assertTrue(failed["result"]["isError"])
        loaded = AuditStore(self.fixture.settings).read_events()
        items = combine_invocations(loaded["events"])
        initialized_item = next(item for item in items if item.get("record_type") == "connection_initialized")
        self.assertEqual(initialized_item["agent"], "codex")
        self.assertEqual(initialized_item["client"]["name"], "Codex Test")
        failed_item = next(item for item in items if item.get("operation") == "unknown_tool")
        self.assertEqual(failed_item["status"], "failed")
        self.assertNotIn("must not be logged", json.dumps(loaded, ensure_ascii=False))

    def test_client_visible_budget(self) -> None:
        """确认服务说明和工具声明不会超过客户端上下文预算。"""
        self.assertLessEqual(len(SERVER_INSTRUCTIONS), 512)
        self.assertLessEqual(len(json.dumps(TOOLS, ensure_ascii=False, separators=(",", ":"))), 4000)
        self.assertEqual([tool["name"] for tool in TOOLS], [
            "search_knowledge", "read_knowledge", "get_work_state", "checkpoint_work_state", "close_work_state"
        ])


if __name__ == "__main__":
    unittest.main()
