"""阶段 4A 批次 2 规则预览 API 的黑盒安全与契约验收。

本文件只通过 FastAPI ``TestClient``、公开规则契约和现有只读系统接口验收。
测试不会在真实控制仓写入候选、创建任务、写审计或改变 Git；如果规则路由尚未
装配，测试会明确失败并指出缺失的公共接口，避免把实现缺项伪装成跳过。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from aikb_web.core.rule_changes import RuleChangeTransaction
from aikb_web.core.rule_preview import RulePreviewService
from aikb_web.main import create_app


RULE_IDS = ("entry", "user", "agent", "contributing")
RULE_ROUTE = "/api/v1/rules"
PREVIEW_ROUTE = "/api/v1/rules/{rule_id}/preview"
APPLY_ROUTE = "/api/v1/rules/user/apply"
PHYSICAL_MARKERS = (
    "physical_path",
    "absolute_path",
    "filesystem_path",
    "workspace_root",
    "repo_root",
    "source_file",
    "backup_path",
)


def _response_data(response: Any) -> Any:
    """提取统一 API 包络中的 data，并在错误包络下保留可诊断断言。"""
    payload = response.json()
    return payload.get("data", payload)


def _rule_from_detail(response: Any) -> dict[str, Any]:
    """兼容规则详情的 ``data.rule`` 和直接 ``data`` 两种冻结期投影形状。"""
    data = _response_data(response)
    if isinstance(data, dict) and isinstance(data.get("rule"), dict):
        return data["rule"]
    if isinstance(data, dict):
        return data
    raise AssertionError(f"规则详情不是对象：{response.text}")


def _items_from_catalog(response: Any) -> list[dict[str, Any]]:
    """提取目录项，允许实现以 ``items`` 或直接数组作为 data 投影。"""
    data = _response_data(response)
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    if isinstance(data, list):
        return data
    raise AssertionError(f"规则目录没有 items 数组：{response.text}")


class Phase4RulePreviewSecurityTests(unittest.TestCase):
    """验收静态规则能力、预览边界、同源保护和零副作用语义。"""

    def setUp(self) -> None:
        """创建隔离 HTTP 客户端；应用只读当前 checkout，不提供写入测试夹具。"""
        self.app = create_app()
        self.client = TestClient(self.app)
        self.good_headers = {
            "Content-Type": "application/json",
            "X-AIKB-Request": "1",
            "Host": "localhost:80",
            "Origin": "http://localhost:80",
        }

    def _require_route(self, path: str, method: str) -> None:
        """规则 API 未装配时明确失败，保留缺失路径和 HTTP 方法证据。"""
        method = method.lower()
        matched = [route for route in self.app.routes if getattr(route, "path", None) == path]
        if not any(method in {name.lower() for name in getattr(route, "methods", set())} for route in matched):
            self.fail(f"阶段 4A 公共接口尚未装配：{method.upper()} {path}")

    def _detail(self, rule_id: str = "user") -> dict[str, Any]:
        """读取详情并断言可供预览使用的基线哈希和 revision 存在。"""
        self._require_route(f"{RULE_ROUTE}/{{rule_id}}", "GET")
        response = self.client.get(f"{RULE_ROUTE}/{rule_id}")
        self.assertEqual(response.status_code, 200, response.text)
        detail = _rule_from_detail(response)
        self.assertIsInstance(detail.get("content"), str)
        self.assertRegex(str(detail.get("content_hash", "")), r"^[0-9a-f]{64}$")
        self.assertRegex(str(detail.get("revision", "")), r"^[0-9a-fA-F]{7,64}$")
        return detail

    def _candidate(self, detail: dict[str, Any]) -> str:
        """生成不扩张职责词的微小变更，确保可在 USER_RULES.md 预算内比较 diff。"""
        content = str(detail["content"])
        if "。" in content:
            return content.replace("。", "！", 1)
        if "；" in content:
            return content.replace("；", "，", 1)
        return content[:-1] + ("!" if content[-1:] != "!" else "?")

    def _preview_body(
        self,
        detail: dict[str, Any],
        candidate: str | None = None,
        *,
        include_revision: bool = False,
    ) -> dict[str, Any]:
        """构造冻结契约的最小预览请求，不携带物理路径或客户端令牌。

        阶段 4A REST 请求只提交 ``base_content_hash``；revision 由服务端在
        预览和后续确认绑定。``include_revision`` 只用于负向测试，确认客户端
        不能借额外字段伪造服务端 revision。
        """
        body: dict[str, Any] = {
            "base_content_hash": detail["content_hash"],
            "candidate_content": candidate if candidate is not None else self._candidate(detail),
        }
        if include_revision:
            body["base_revision"] = detail["revision"]
        return body

    @contextmanager
    def _temporary_clean_rule_app(self):
        """创建仅含四个受控规则的临时 Git 仓，注入公开规则服务后交给测试使用。

        临时仓的提交、事务目录和候选正文都位于 ``TemporaryDirectory``，不会污染
        当前 AIKB checkout；服务仍经真实 Git 命令、共享验证器和 FastAPI 路由运行。
        """
        source_root = Path(os.environ.get("AIKB_HOME", Path(__file__).resolve().parents[5])).resolve()
        with tempfile.TemporaryDirectory(prefix="aikb-rule-preview-") as temporary_root:
            root = Path(temporary_root) / "control"
            workspace = Path(temporary_root) / "workspace"
            root.mkdir(parents=True)
            workspace.mkdir()
            for relative in (
                "ENTRY_RULES.md",
                "system/rules/USER_RULES.md",
                "system/rules/AI_RULES.md",
                "system/rules/CONTRIBUTING.md",
            ):
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_root / relative, destination)

            def git(*args: str) -> str:
                """执行临时仓固定 Git 命令并返回标准输出，失败直接终止夹具。"""
                result = subprocess.run(
                    ["git", *args], cwd=root, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()

            git("init", "--quiet")
            git("config", "user.email", "aikb-test@example.invalid")
            git("config", "user.name", "AIKB rule preview test")
            git("add", "ENTRY_RULES.md", "system/rules")
            git("commit", "--quiet", "-m", "test controlled rules")

            app = create_app()
            app.state.rule_preview_service = RulePreviewService(
                SimpleNamespace(repo_root=root, workspace_root=workspace),
            )
            client = TestClient(app)
            try:
                yield client, root, workspace
            finally:
                client.close()

    def _assert_no_sensitive_echo(self, response: Any, *secrets: str) -> None:
        """错误响应不可回显候选正文、物理路径或内部字段名。"""
        serialized = response.text.lower()
        for secret in secrets:
            if secret:
                self.assertNotIn(secret.lower(), serialized, response.text)
        for marker in PHYSICAL_MARKERS:
            self.assertNotIn(marker, serialized, response.text)

    def test_catalog_has_exactly_four_rules_and_only_user_is_writable(self) -> None:
        """目录固定四项，写能力只属于 user，任何投影都不暴露物理目标。"""
        self._require_route(RULE_ROUTE, "GET")
        response = self.client.get(RULE_ROUTE)
        self.assertEqual(response.status_code, 200, response.text)
        items = _items_from_catalog(response)
        self.assertEqual({item.get("rule_id") for item in items}, set(RULE_IDS))
        self.assertEqual([item.get("rule_id") for item in items if item.get("writable")], ["user"])
        serialized = json.dumps(items, ensure_ascii=False).lower()
        self.assertNotIn("path", serialized)
        self.assertNotIn("physical", serialized)

    def test_all_rule_details_are_readable_and_path_free(self) -> None:
        """四项详情都可审阅，但只返回正文和安全元数据，不返回磁盘定位信息。"""
        for rule_id in RULE_IDS:
            with self.subTest(rule_id=rule_id):
                detail = self._detail(rule_id)
                serialized = json.dumps(detail, ensure_ascii=False).lower()
                self.assertFalse(any(marker in serialized for marker in PHYSICAL_MARKERS), serialized)
                self.assertNotIn("relative_path", detail)
                self.assertNotIn("repo_root", detail)

    def test_unknown_and_path_like_rule_ids_are_rejected_without_echo(self) -> None:
        """未知 ID、绝对路径和穿越片段不能触发回退路径解析或错误回显。"""
        self._require_route(f"{RULE_ROUTE}/{{rule_id}}", "GET")
        self._require_route(PREVIEW_ROUTE, "POST")
        for rule_id in ("unknown", "../user", "..\\user", "C:\\private\\USER_RULES.md", "content/user"):
            with self.subTest(rule_id=rule_id):
                detail = self.client.get(f"{RULE_ROUTE}/{rule_id}")
                self.assertIn(detail.status_code, (400, 404), detail.text)
                self._assert_no_sensitive_echo(detail, rule_id)
                preview = self.client.post(
                    f"{RULE_ROUTE}/{rule_id}/preview",
                    headers=self.good_headers,
                    json={"base_content_hash": "0" * 64, "candidate_content": "unknown"},
                )
                self.assertIn(preview.status_code, (400, 404, 405, 409, 422), preview.text)
                self._assert_no_sensitive_echo(preview, rule_id, "unknown")

    def test_read_only_rules_cannot_be_previewed(self) -> None:
        """entry、agent、contributing 即使携带候选正文也必须拒绝预览。"""
        self._require_route(PREVIEW_ROUTE, "POST")
        for rule_id in ("entry", "agent", "contributing"):
            with self.subTest(rule_id=rule_id):
                detail = self._detail(rule_id)
                candidate = self._candidate(detail)
                response = self.client.post(
                    f"{RULE_ROUTE}/{rule_id}/preview",
                    headers=self.good_headers,
                    json=self._preview_body(detail, candidate),
                )
                self.assertIn(response.status_code, (400, 403, 404, 409, 422), response.text)
                self._assert_no_sensitive_echo(response, candidate)

    def test_preview_requires_json_marker_and_strict_same_origin(self) -> None:
        """缺少 JSON、X-AIKB-Request、Host 或同源 Origin 时写入口 fail-closed。"""
        detail = self._detail()
        body = self._preview_body(detail)
        cases = (
            ({**self.good_headers, "Content-Type": "text/plain"}, "wrong-content-type"),
            ({key: value for key, value in self.good_headers.items() if key != "X-AIKB-Request"}, "missing-marker"),
            ({**self.good_headers, "X-AIKB-Request": "0"}, "bad-marker"),
            ({**self.good_headers, "Host": "evil.example", "Origin": "http://evil.example"}, "bad-host"),
            ({**self.good_headers, "Origin": "http://evil.example"}, "cross-origin"),
            ({**self.good_headers, "Host": "evil.example"}, "host-mismatch"),
            ({key: value for key, value in self.good_headers.items() if key != "Origin"}, "missing-origin"),
        )
        for headers, label in cases:
            with self.subTest(case=label):
                response = self.client.post(
                    f"{RULE_ROUTE}/user/preview",
                    headers=headers,
                    content=json.dumps(body, ensure_ascii=False),
                )
                self.assertIn(response.status_code, (400, 403, 404, 405, 415, 422), response.text)
                self._assert_no_sensitive_echo(response, body["candidate_content"])

    def test_stale_hash_and_revision_are_conflicts_and_do_not_echo_candidate(self) -> None:
        """基线哈希或 revision 不匹配时拒绝预览，且错误不携带候选正文。"""
        self._require_route(PREVIEW_ROUTE, "POST")
        detail = self._detail()
        candidate = self._candidate(detail)
        stale_hash = self._preview_body(detail, candidate)
        stale_hash["base_content_hash"] = "0" * 64
        response = self.client.post(f"{RULE_ROUTE}/user/preview", headers=self.good_headers, json=stale_hash)
        self.assertIn(response.status_code, (400, 409, 422), response.text)
        self._assert_no_sensitive_echo(response, candidate)

        stale_revision = self._preview_body(detail, candidate)
        stale_revision["base_revision"] = "f" * len(str(detail["revision"]))
        response = self.client.post(f"{RULE_ROUTE}/user/preview", headers=self.good_headers, json=stale_revision)
        self.assertIn(response.status_code, (400, 409, 422), response.text)
        self._assert_no_sensitive_echo(response, candidate)

    def test_budget_encoding_and_diff_limits_fail_closed(self) -> None:
        """NUL、替换字符、UTF-8/字符预算、2000 行和 diff 预算超限均拒绝。"""
        self._require_route(PREVIEW_ROUTE, "POST")
        detail = self._detail()
        cases = (
            ("nul", str(detail["content"]) + "\x00"),
            ("replacement", str(detail["content"]) + "\ufffd"),
            ("bom", "\ufeff" + str(detail["content"])),
            ("char-budget", "x" * 65_000),
            ("line-budget", "\n".join(f"line-{index}" for index in range(2_001))),
            ("diff-lines", "\n".join(f"line-{index}" for index in range(4_001))),
            ("diff-bytes", "x" * (256 * 1024 + 1)),
        )
        for label, candidate in cases:
            with self.subTest(case=label):
                response = self.client.post(
                    f"{RULE_ROUTE}/user/preview",
                    headers=self.good_headers,
                    json=self._preview_body(detail, candidate),
                )
                self.assertIn(response.status_code, (400, 409, 413, 422), response.text)
                self._assert_no_sensitive_echo(response, candidate)

        # JSON 载荷本身使用非法 UTF-8 字节时，解析层也必须拒绝且不能把原始字节回显。
        invalid_utf8 = b'{"base_content_hash":"' + str(detail["content_hash"]).encode("ascii") + b'","candidate_content":"\xff"}'
        response = self.client.post(
            f"{RULE_ROUTE}/user/preview",
            headers={**self.good_headers, "Content-Type": "application/json"},
            content=invalid_utf8,
        )
        self.assertIn(response.status_code, (400, 415, 422), response.text)
        self._assert_no_sensitive_echo(response, "\\xff")

    def test_successful_preview_is_complete_and_has_no_side_effects(self) -> None:
        """在隔离干净仓中校验完整 diff、prepared 事务和预览零正式副作用。"""
        self._require_route(PREVIEW_ROUTE, "POST")
        with self._temporary_clean_rule_app() as (client, root, workspace):
            detail_before = _rule_from_detail(client.get(f"{RULE_ROUTE}/user"))
            candidate = self._candidate(detail_before)
            target = root / "system" / "rules" / "USER_RULES.md"
            formal_before = target.read_bytes()
            revision_before = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                text=True, encoding="utf-8", check=True,
            ).stdout.strip()
            status_before = subprocess.run(
                ["git", "status", "--porcelain=v1"], cwd=root, capture_output=True,
                text=True, encoding="utf-8", check=True,
            ).stdout

            response = client.post(
                f"{RULE_ROUTE}/user/preview",
                headers=self.good_headers,
                json=self._preview_body(detail_before, candidate),
            )
            self.assertEqual(response.status_code, 200, response.text)
            self._assert_no_sensitive_echo(response, str(root), str(workspace))
            data = _response_data(response)
            self.assertIsInstance(data, dict)
            self.assertEqual(data.get("rule_id"), "user")
            self.assertRegex(str(data.get("change_id", "")), r"^change-[A-Za-z0-9._-]+$")
            self.assertRegex(str(data.get("preview_digest", "")), r"^[0-9a-f]{64}$")
            diff = str(data.get("diff", ""))
            self.assertTrue(diff, response.text)
            self.assertIn("！", diff)
            self.assertFalse(bool(data.get("diff_truncated", False)))
            self.assertNotIn('"candidate_content":', json.dumps(data, ensure_ascii=False))

            change_id = str(data["change_id"])
            transaction_dirs = list((workspace / "runtime" / "web" / "rule-changes").rglob(change_id))
            self.assertEqual(len(transaction_dirs), 1)
            transaction_dir = transaction_dirs[0]
            self.assertEqual((transaction_dir / "candidate.md").read_text(encoding="utf-8"), candidate)
            transaction = json.loads((transaction_dir / "transaction.json").read_text(encoding="utf-8"))
            self.assertEqual(transaction["status"], "prepared")
            self.assertEqual(transaction["before_hash"], detail_before["content_hash"])
            self.assertEqual(transaction["after_hash"], data["candidate_content_hash"])
            for forbidden in ("candidate_content", "content", "diff", "backup", "path"):
                self.assertNotIn(forbidden, transaction)

            # 预览只保存短期候选和安全事务摘要，不替换正式文件、不建任务/审计事实。
            self.assertEqual(target.read_bytes(), formal_before)
            self.assertEqual(
                subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                    text=True, encoding="utf-8", check=True,
                ).stdout.strip(),
                revision_before,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "status", "--porcelain=v1"], cwd=root, capture_output=True,
                    text=True, encoding="utf-8", check=True,
                ).stdout,
                status_before,
            )
            self.assertFalse((workspace / "audit").exists())
            self.assertFalse((workspace / "active").exists())
            self.assertFalse((workspace / "archive").exists())

    def test_apply_route_is_unavailable_before_write_service_startup(self) -> None:
        """create_app/import 阶段不创建写服务，未启动 lifespan 时 apply 不可用。"""
        for method in ("get", "post", "put", "patch", "delete"):
            with self.subTest(method=method):
                response = getattr(self.client, method)(APPLY_ROUTE, headers=self.good_headers)
                self.assertIn(response.status_code, (404, 405), response.text)
                self._assert_no_sensitive_echo(response)

    def test_transaction_public_projection_contains_no_body_diff_or_path(self) -> None:
        """事务安全投影只含哈希/状态/逻辑 ID，正文、diff、备份和路径永不进入。"""
        transaction = RuleChangeTransaction(
            change_id="change-security",
            rule_id="user",
            action_id="rule.user.update",
            risk_level="source_write",
            status="prepared",
            before_hash="1" * 64,
            after_hash="2" * 64,
            diff_hash="3" * 64,
            preview_digest="4" * 64,
            validator_version="rules-v1",
            repository_revision="a" * 40,
            created_at="2026-08-30T01:00:00Z",
            expires_at="2026-08-30T01:05:00Z",
            updated_at="2026-08-30T01:00:00Z",
            task_id="task-security",
        )
        projection = transaction.public_dict()
        self.assertNotIn("candidate", projection)
        self.assertNotIn("content", projection)
        self.assertNotIn("diff", projection)
        self.assertNotIn("backup", projection)
        self.assertNotIn("path", projection)
        serialized = json.dumps(projection, ensure_ascii=False).lower()
        for forbidden in ("candidate", "content", "diff", "backup", "path", "body"):
            # ``diff_hash`` 是允许的安全摘要；这里只禁止正文/完整 diff 字段，
            # 不能把合法的哈希字段名误判成敏感材料。
            if forbidden in {"candidate", "content", "backup", "path", "body"}:
                self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
