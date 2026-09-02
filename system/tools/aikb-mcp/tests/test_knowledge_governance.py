"""知识治理 v2 的结构门禁与 legacy 索引兼容回归测试。"""

from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path


TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from aikb.config import Settings
from aikb.frontmatter import render_frontmatter
from aikb.indexer import metadata_report, rebuild_knowledge_index
from aikb.knowledge import KnowledgeService


def _metadata(entry_id: str = "aikb:knowledge:engineering:v2") -> dict:
    """生成合法 v2 正式条目元数据，测试只替换被测门禁字段。"""
    return {
        "id": entry_id,
        "type": "knowledge",
        "status": "verified",
        "governance_version": 2,
        "change_class": "factual-update",
        "authority": "file:src/example.py",
        "approval_status": "not-required",
        "preparer": "agent-a",
        "reviewer": "agent-b",
        "reviewed_at": "2026-08-31",
        "tags": ["governance"],
        "applicable_versions": "test",
        "last_verified": "2026-08-31",
        "review_when": "实现变化时",
        "supersedes": [],
        "evidence": [{
            "kind": "test", "ref": "test_knowledge_governance.py",
            "result": "通过", "date": "2026-08-31",
        }],
        "relations": [],
    }


class KnowledgeGovernanceTests(unittest.TestCase):
    """验证 v2 门禁不会把自由文本当证据，也不会破坏 legacy Web 索引。"""

    def setUp(self) -> None:
        """创建隔离知识仓，并写入一个合法 v2 条目。"""
        self.temp = tempfile.TemporaryDirectory(prefix="aikb-governance-test-")
        root = Path(self.temp.name)
        self.content = root / "content"
        (self.content / "knowledge" / "engineering").mkdir(parents=True)
        (self.content / "inbox").mkdir(parents=True)
        (self.content / "workflows").mkdir(parents=True)
        (self.content / ".aikb-knowledge.json").write_text(
            '{"kind":"aikb-knowledge","contract_version":1,"knowledge_schema_version":1}',
            encoding="utf-8",
        )
        self.settings = Settings.load(root, root / "workspace")
        self.path = self.content / "knowledge" / "engineering" / "v2.md"
        self.write(_metadata())

    def tearDown(self) -> None:
        """释放隔离知识仓。"""
        self.temp.cleanup()

    def write(self, metadata: dict, path: Path | None = None) -> None:
        """写入一篇最小 Markdown，正文故意保持与治理字段无关。"""
        target = path or self.path
        target.write_text(
            render_frontmatter(metadata) + "\n\n# 测试条目\n\n## 验证\n\n结构测试。\n",
            encoding="utf-8",
        )

    def errors(self) -> list[str]:
        """返回当前知识仓的全部元数据错误。"""
        return metadata_report(self.settings)["errors"]

    def test_valid_v2_and_legacy_are_indexable(self) -> None:
        """确认合法 v2 与无版本 legacy 可共同重建，Web 仍能读取 verified。"""
        legacy = _metadata("aikb:knowledge:engineering:legacy")
        for field in ("governance_version", "change_class", "authority", "approval_status", "preparer", "reviewer", "reviewed_at", "evidence"):
            legacy.pop(field, None)
        self.write(legacy, self.content / "knowledge" / "engineering" / "legacy.md")
        self.assertEqual(self.errors(), [])
        result = rebuild_knowledge_index(self.settings)
        self.assertEqual(result["documents"], 2)
        documents = KnowledgeService(self.settings).list_documents(path_prefix="content/knowledge")
        self.assertEqual(documents["total"], 2)
        self.assertTrue(all(item["status"] == "verified" for item in documents["documents"]))

    def test_classified_candidate_placeholder_stays_in_target_directory(self) -> None:
        """确认已归类占位可留在目标目录，不与 Inbox 未分类来源卡片混同。"""
        placeholder = {
            "id": "aikb:workflows:placeholder",
            "type": "workflow",
            "status": "candidate",
            "tags": ["workflow"],
            "relations": [],
        }
        self.write(placeholder, self.content / "workflows" / "placeholder.md")
        self.assertEqual(self.errors(), [])
        result = rebuild_knowledge_index(self.settings)
        self.assertEqual(result["documents"], 2)

    def test_free_text_evidence_is_rejected(self) -> None:
        """确认 evidence 不能以一条自由文本冒充结构化依据。"""
        metadata = _metadata()
        metadata["evidence"] = ["依据官方文档验证"]
        self.assertTrue(any("不能是自由文本" in error for error in self._replace_and_errors(metadata)))

    def test_decision_and_supersession_classes_are_objective(self) -> None:
        """确认 decision 和 supersedes 不能用 factual-update 绕过分类门。"""
        decision = _metadata("aikb:experience:decisions:bad")
        decision["type"] = "decision"
        decision["change_class"] = "factual-update"
        self.write(decision, self.content / "inbox" / "bad-decision.md")
        errors = self.errors()
        self.assertTrue(any("type=decision" in error for error in errors))
        (self.content / "inbox" / "bad-decision.md").unlink()

        supersession = _metadata("aikb:knowledge:engineering:bad-supersession")
        supersession["supersedes"] = ["aikb:knowledge:engineering:old"]
        self.write(supersession, self.path)
        self.assertTrue(any("非空 supersedes" in error for error in self.errors()))

    def test_high_impact_requires_approval_and_independent_review(self) -> None:
        """确认未批准或同人自审的高影响条目不能变成 verified。"""
        metadata = _metadata()
        metadata["change_class"] = "decision-record"
        metadata["approval_status"] = "pending"
        self.assertTrue(any("未经 approved" in error for error in self._replace_and_errors(metadata)))
        metadata["approval_status"] = "approved"
        metadata["approved_by"] = "owner"
        metadata["approved_at"] = "2026-08-31"
        metadata["reviewer"] = metadata["preparer"]
        self.assertTrue(any("不得为同一人" in error for error in self._replace_and_errors(metadata)))

        proposal = _metadata()
        proposal["change_class"] = "decision-proposal"
        self.assertTrue(any("decision-proposal 只能使用 type=candidate" in error for error in self._replace_and_errors(proposal)))

    def test_schema_binds_decision_proposal_to_inbox(self) -> None:
        """确认 JSON Schema 与 Python validator 对 proposal 的状态和类型约束一致。"""
        schema_path = TOOL_ROOT.parents[1] / "schemas" / "knowledge-entry.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        proposal_rule = next(
            rule for rule in schema["allOf"]
            if rule.get("if", {}).get("properties", {}).get("change_class", {}).get("const") == "decision-proposal"
        )
        self.assertEqual(proposal_rule["then"]["properties"]["status"]["const"], "candidate")
        self.assertEqual(proposal_rule["then"]["properties"]["type"]["const"], "candidate")
        self.assertIn("status", proposal_rule["then"]["required"])
        self.assertIn("type", proposal_rule["then"]["required"])

    def test_inbox_decision_proposal_does_not_fake_review(self) -> None:
        """确认 Inbox 决策提案可待审，不被迫填写伪造的复核人和复核日期。"""
        metadata = _metadata("aikb:experience:inbox:proposal")
        metadata.update({
            "type": "candidate", "status": "candidate", "change_class": "decision-proposal",
            "owner": "agent-a", "captured_at": "2026-08-31", "next_action_due": "2026-09-02",
            "review_state": "open", "blocking_reason": "等待用户确认", "approval_status": "pending",
        })
        metadata.pop("reviewer")
        metadata.pop("reviewed_at")
        self.write(metadata, self.content / "inbox" / "proposal.md")
        self.assertEqual(self.errors(), [])

    def test_commit_and_file_evidence_refs_are_bounded(self) -> None:
        """确认 commit 和 file evidence 的定位值不能伪装成任意路径或短哈希。"""
        metadata = _metadata()
        metadata["evidence"] = [{"kind": "commit", "ref": "deadbe", "result": "通过", "date": "2026-08-31"}]
        self.assertTrue(any("7-40 位十六进制" in error for error in self._replace_and_errors(metadata)))
        metadata["evidence"] = [{"kind": "file", "ref": "C:/secret.txt", "result": "通过", "date": "2026-08-31"}]
        self.assertTrue(any("安全的项目相对路径" in error for error in self._replace_and_errors(metadata)))
        metadata["evidence"] = [{"kind": "file", "ref": "content/../secret.txt", "result": "通过", "date": "2026-08-31"}]
        self.assertTrue(any("安全的项目相对路径" in error for error in self._replace_and_errors(metadata)))

    def test_inbox_lifecycle_is_required_and_dates_are_ordered(self) -> None:
        """确认 v2 Inbox 的负责人、生命周期字段和日期顺序不可省略。"""
        metadata = _metadata("aikb:experience:inbox:missing")
        metadata.update({
            "type": "candidate", "status": "candidate", "change_class": "candidate",
            "captured_at": "2026-09-02", "next_action_due": "2026-09-01",
            "review_state": "open", "owner": "agent-a",
        })
        self.write(metadata, self.content / "inbox" / "missing.md")
        errors = self.errors()
        self.assertTrue(any("next_action_due 不得早于 captured_at" in error for error in errors))
        self.assertTrue(any("blocking_reason 或 possible_duplicates" in error for error in errors))

    def _replace_and_errors(self, metadata: dict) -> list[str]:
        """替换主测试条目并返回错误，避免测试间共享文件状态。"""
        self.write(metadata, self.path)
        return self.errors()


if __name__ == "__main__":
    unittest.main()
