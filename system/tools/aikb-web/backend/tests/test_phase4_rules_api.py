"""阶段 4A 规则目录与候选预览 API 的安全回归测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from aikb_web.core.rule_preview import RepositoryState, RuleServiceError
from aikb_web.main import create_app


class _Gateway:
    """提供真实控制仓设置但不初始化知识索引，隔离规则 API 测试。"""

    def __init__(self, workspace: Path) -> None:
        """把草稿运行面放入临时目录，测试不会修改仓库或审计事实源。"""
        self.settings = SimpleNamespace(repo_root=Path(__file__).resolve().parents[5], workspace_root=workspace)

    def overview(self) -> dict[str, object]:
        """满足既有健康接口的最小网关契约。"""
        return {"index": {"available": False}}


class Phase4RulesApiTests(unittest.TestCase):
    """覆盖规则只读投影、预览边界、草稿材料和同源请求约束。"""

    def setUp(self) -> None:
        """创建隔离运行面，并读取服务端当前 user 规则摘要。"""
        self.temp = tempfile.TemporaryDirectory(prefix="aikb-phase4-rules-")
        app = create_app(_Gateway(Path(self.temp.name)))
        self.client = TestClient(app)
        self.service = app.state.rule_preview_service
        self.headers = {
            "Content-Type": "application/json", "X-AIKB-Request": "1",
            "Host": "localhost:80", "Origin": "http://localhost:80",
        }
        self.current = self.client.get("/api/v1/rules/user").json()["data"]
        self.clean_state = RepositoryState("a" * 40, "main", True, True, False)

    def tearDown(self) -> None:
        """释放临时草稿目录。"""
        self.temp.cleanup()

    def test_list_and_detail_never_expose_physical_paths(self) -> None:
        """四项规则均可审阅，公开投影不出现路径字段。"""
        listing = self.client.get("/api/v1/rules")
        self.assertEqual(listing.status_code, 200)
        items = listing.json()["data"]["items"]
        self.assertEqual({item["rule_id"] for item in items}, {"entry", "user", "agent", "contributing"})
        self.assertEqual([item["rule_id"] for item in items if item["writable"]], ["user"])
        detail = self.client.get("/api/v1/rules/user")
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn("path", detail.json()["data"])
        self.assertNotIn(str(self.service._repo_root), detail.text)

    def test_preview_persists_only_prepared_transaction_and_candidate(self) -> None:
        """成功预览返回完整 diff，草稿不包含 backup、正文事务字段或物理路径。"""
        candidate = self.current["content"] + "\n# 预览测试\n"
        with patch.object(self.service, "_repository_state", return_value=self.clean_state):
            response = self.client.post(
                "/api/v1/rules/user/preview", headers=self.headers,
                json={"base_content_hash": self.current["content_hash"], "candidate_content": candidate},
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertIn("@@", data["diff"])
        self.assertIn("# 预览测试", data["diff"])
        self.assertEqual(data["expires_in_seconds"], 300)
        self.assertEqual(data["validator_version"], "phase4-rules-v1")
        change_dir = Path(self.temp.name) / "runtime" / "web" / "rule-changes" / data["expires_at"][0:4] / data["expires_at"][5:7] / data["change_id"]
        transaction = json.loads((change_dir / "transaction.json").read_text(encoding="utf-8"))
        self.assertEqual(transaction["status"], "prepared")
        self.assertEqual((change_dir / "candidate.md").read_text(encoding="utf-8"), candidate)
        self.assertFalse((change_dir / "backup.md").exists())
        self.assertNotIn("candidate", transaction)
        self.assertNotIn("diff", transaction)
        self.assertNotIn(str(self.service._repo_root), response.text)

    def test_readonly_unknown_and_invalid_candidates_do_not_create_drafts(self) -> None:
        """只读规则、未知 ID、共享校验失败和缺少同源头均不落盘。"""
        with patch.object(self.service, "_repository_state", return_value=self.clean_state):
            readonly = self.client.post(
                "/api/v1/rules/agent/preview", headers=self.headers,
                json={"base_content_hash": self.current["content_hash"], "candidate_content": "x"},
            )
            unknown = self.client.get("/api/v1/rules/C:%5Cprivate")
            invalid = self.client.post(
                "/api/v1/rules/user/preview", headers=self.headers,
                json={"base_content_hash": self.current["content_hash"], "candidate_content": "# invalid"},
            )
        missing_header = self.client.post(
            "/api/v1/rules/user/preview", json={"base_content_hash": self.current["content_hash"], "candidate_content": "x"},
        )
        self.assertEqual(readonly.status_code, 403)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(missing_header.status_code, 400)
        self.assertEqual(list((Path(self.temp.name) / "runtime").rglob("*")), [])

    def test_dirty_repository_and_extra_browser_fields_are_rejected(self) -> None:
        """全仓非洁净状态和浏览器路径注入均在草稿创建前被拒绝。"""
        dirty = RepositoryState("a" * 40, "main", False, True, False)
        with patch.object(self.service, "_repository_state", return_value=dirty):
            response = self.client.post(
                "/api/v1/rules/user/preview", headers=self.headers,
                json={"base_content_hash": self.current["content_hash"], "candidate_content": "x"},
            )
        self.assertEqual(response.status_code, 409)
        with patch.object(self.service, "_repository_state", return_value=self.clean_state):
            extra = self.client.post(
                "/api/v1/rules/user/preview", headers=self.headers,
                json={"base_content_hash": self.current["content_hash"], "candidate_content": "x", "path": "C:\\private"},
            )
        self.assertEqual(extra.status_code, 422)
        self.assertNotIn("private", extra.text)

    def test_unchanged_candidate_is_rejected_without_draft(self) -> None:
        """与当前正文相同的候选不生成无意义事务。"""
        with patch.object(self.service, "_repository_state", return_value=self.clean_state):
            response = self.client.post(
                "/api/v1/rules/user/preview", headers=self.headers,
                json={"base_content_hash": self.current["content_hash"], "candidate_content": self.current["content"]},
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "no_change")
        self.assertEqual(list((Path(self.temp.name) / "runtime").rglob("*")), [])

    def test_final_repository_check_rejects_change_before_directory_creation(self) -> None:
        """落盘前的二次 ready 检查发现仓库变脏时不创建事务目录。"""
        states = iter((self.clean_state, self.clean_state, RepositoryState("a" * 40, "main", False, True, False), self.clean_state))
        with patch.object(self.service, "_repository_state", side_effect=lambda: next(states)):
            candidate = self.current["content"] + "\n# 预览二次检查\n"
            response = self.client.post(
                "/api/v1/rules/user/preview", headers=self.headers,
                json={"base_content_hash": self.current["content_hash"], "candidate_content": candidate},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "repository_changed")
        self.assertEqual(list((Path(self.temp.name) / "runtime").rglob("*")), [])

    def test_final_content_hash_check_rejects_target_change(self) -> None:
        """二次读取发现目标正文摘要变化时，不创建预览草稿。"""
        original = self.service._read("user")
        first = replace(original, revision=self.clean_state.revision)
        changed = replace(first, content_hash="b" * 64)
        candidate = self.current["content"] + "\n# 目标摘要二次检查\n"
        with patch.object(self.service, "_repository_state", return_value=self.clean_state), patch.object(
            self.service, "_read", side_effect=(first, changed)
        ):
            response = self.client.post(
                "/api/v1/rules/user/preview", headers=self.headers,
                json={"base_content_hash": self.current["content_hash"], "candidate_content": candidate},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "repository_changed")
        self.assertEqual(list((Path(self.temp.name) / "runtime").rglob("*")), [])

    def test_service_errors_are_safe_and_failed_material_creation_is_cleaned(self) -> None:
        """内部路径异常只返回 503；令牌失败时新建目录不会留下半成品。"""
        with patch.object(self.service, "list_rules", side_effect=RuleServiceError(r"C:\private\secret.md")):
            response = self.client.get("/api/v1/rules")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)
        candidate = self.current["content"] + "\n# 令牌失败清理\n"
        with patch.object(self.service, "_repository_state", return_value=self.clean_state), patch.object(
            self.service._tokens, "issue", side_effect=RuntimeError(r"C:\private\token")
        ):
            response = self.client.post(
                "/api/v1/rules/user/preview", headers=self.headers,
                json={"base_content_hash": self.current["content_hash"], "candidate_content": candidate},
            )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)
        change_root = Path(self.temp.name) / "runtime" / "web" / "rule-changes"
        self.assertFalse(any(path.name.startswith("change-") for path in change_root.rglob("*")))

    def test_apply_is_unavailable_without_started_write_service(self) -> None:
        """未启动 lifespan/规则协调器时，apply 必须保持不可用且不产生副作用。"""
        response = self.client.post("/api/v1/rules/user/apply", headers=self.headers, json={})
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
