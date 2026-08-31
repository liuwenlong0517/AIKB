"""阶段 4B 波次 1 维护只读 API 和零副作用预览测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from aikb_web.core.maintenance_targets import MAINTENANCE_TARGET_REGISTRY, MaintenanceTargetStatus
from aikb_web.main import create_app
from aikb_web.platform.maintenance import (
    MaintenancePlan,
    MaintenancePlatformCapabilities,
    MaintenanceStep,
)


class _Gateway:
    """提供健康接口需要的最小网关，不连接真实知识仓或用户配置。"""

    settings = None

    def overview(self) -> dict[str, object]:
        """返回稳定的健康摘要。"""

        return {"index": {"available": False}}


class _Adapter:
    """只读 fake 适配器；记录调用以证明陈旧预览未进入 plan。"""

    def __init__(self, base: str) -> None:
        """准备每个固定目标的安全内存状态。"""

        self.base = base
        self.inspect_calls: list[str] = []
        self.plan_calls: list[str] = []

    def inspect(self, target_id: str) -> MaintenanceTargetStatus:
        """返回目标注册表匹配的漂移状态，不访问物理配置。"""

        self.inspect_calls.append(target_id)
        target = MAINTENANCE_TARGET_REGISTRY.get(target_id)
        return MaintenanceTargetStatus(
            target_id=target_id,
            status="drifted",
            logical_leaves=target.logical_leaves,
            steps=target.steps,
            reason_code="managed_content_drifted",
            base_fingerprint=self.base,
        )

    def plan(self, target_id: str, inspection: MaintenanceTargetStatus) -> MaintenancePlan:
        """生成与静态目标完全一致的安全计划。"""

        self.plan_calls.append(target_id)
        target = MAINTENANCE_TARGET_REGISTRY.get(target_id)
        return MaintenancePlan(
            target_id=target_id,
            steps=tuple(MaintenanceStep(step) for step in target.steps),
            logical_leaves=target.logical_leaves,
            before_fingerprint=self.base,
            after_fingerprint="b" * 64,
            preview_digest="c" * 64,
        )


class Phase4BMaintenanceApiTests(unittest.TestCase):
    """覆盖固定目标、平台不可用、安全请求门禁和预览零副作用。"""

    def setUp(self) -> None:
        """创建隔离服务和同源请求头。"""

        self.temp = tempfile.TemporaryDirectory(prefix="aikb-phase4b-api-")
        self.base = "a" * 64
        self.adapter = _Adapter(self.base)
        self.app = create_app(_Gateway(), maintenance_adapter=self.adapter)
        self.client = TestClient(self.app)
        self.headers = {
            "Content-Type": "application/json",
            "X-AIKB-Request": "1",
            "Host": "localhost:80",
            "Origin": "http://localhost:80",
        }
        self.capabilities = MaintenancePlatformCapabilities(
            platform="windows", architecture="AMD64", supported=False,
            reason_code="reserved_not_implemented",
        )

    def tearDown(self) -> None:
        """释放隔离运行面。"""

        self.temp.cleanup()

    def test_targets_list_is_static_and_safe(self) -> None:
        """目标列表只公开三个静态目标和平台能力，不调用 inspect。"""

        unsupported = MaintenancePlatformCapabilities(
            platform="windows", architecture="AMD64", supported=False,
            reason_code="reserved_not_implemented",
        )
        with patch("aikb_web.api.v1.maintenance.maintenance_platform_capabilities", return_value=unsupported):
            response = self.client.get("/api/v1/maintenance/targets")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual([item["target_id"] for item in data["items"]], ["environment", "agent.codex", "agent.claude-code"])
        self.assertTrue(all(item["supported"] is False for item in data["items"]))
        self.assertEqual(data["platform"]["reason_code"], "reserved_not_implemented")
        self.assertTrue(data["platform"]["inspection_supported"])
        self.assertTrue(data["platform"]["preview_supported"])
        self.assertFalse(data["platform"]["apply_supported"])
        self.assertEqual(self.adapter.inspect_calls, [])
        self.assertNotIn(str(Path(self.temp.name)), response.text)

    def test_detail_and_preview_use_injected_inspect_plan_only(self) -> None:
        """详情和预览只经适配器安全模型，响应不含路径、正文、命令或任务字段。"""

        with patch("aikb_web.api.v1.maintenance.maintenance_platform_capabilities", return_value=self.capabilities):
            detail = self.client.get("/api/v1/maintenance/targets/agent.codex")
            preview = self.client.post(
                "/api/v1/maintenance/targets/agent.codex/preview",
                headers=self.headers,
                json={"base_fingerprint": self.base},
            )
        self.assertEqual(detail.status_code, 200)
        detail_data = detail.json()["data"]
        self.assertEqual(set(detail_data), {"target", "platform", "status", "leaves"})
        self.assertEqual(detail_data["status"]["status"], "drifted")
        self.assertFalse(detail_data["platform"]["supported"])
        self.assertTrue(detail_data["platform"]["inspection_supported"])
        self.assertTrue(detail_data["platform"]["preview_supported"])
        self.assertFalse(detail_data["platform"]["apply_supported"])
        self.assertEqual(preview.status_code, 200)
        data = preview.json()["data"]
        self.assertEqual(set(data), {"target", "platform", "inspection", "plan"})
        self.assertEqual(data["inspection"]["base_fingerprint"], self.base)
        self.assertEqual(data["plan"]["after_fingerprint"], "b" * 64)
        self.assertTrue(all(isinstance(step, dict) and set(step) == {"step_id"} for step in data["plan"]["steps"]))
        self.assertIn("differences", data["plan"])
        serialized = json.dumps(data, ensure_ascii=False)
        for forbidden in ("path", "command", "environment_value", "transaction", "task", "secret"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(self.adapter.inspect_calls, ["agent.codex", "agent.codex"])
        self.assertEqual(self.adapter.plan_calls, ["agent.codex"])
        self.assertEqual(list((Path(self.temp.name) / "runtime").rglob("*")), [])

    def test_unsupported_platform_is_explicit_and_never_calls_adapter(self) -> None:
        """未支持平台返回固定 unsupported 状态，适配器不会被调用。"""

        unsupported = MaintenancePlatformCapabilities(
            platform="macos", architecture="arm64", supported=False,
            reason_code="reserved_not_implemented",
        )
        with patch("aikb_web.api.v1.maintenance.maintenance_platform_capabilities", return_value=unsupported):
            detail = self.client.get("/api/v1/maintenance/targets/environment")
            preview = self.client.post(
                "/api/v1/maintenance/targets/environment/preview",
                headers=self.headers,
                json={"base_fingerprint": self.base},
            )
        self.assertEqual(detail.status_code, 200)
        detail_data = detail.json()["data"]
        self.assertEqual(detail_data["status"]["status"], "unsupported")
        self.assertFalse(detail_data["platform"]["inspection_supported"])
        self.assertFalse(detail_data["platform"]["preview_supported"])
        self.assertEqual(preview.status_code, 409)
        self.assertEqual(preview.json()["error"]["code"], "MAINTENANCE_TARGET_UNSUPPORTED")
        self.assertEqual(self.adapter.inspect_calls, [])

    def test_unknown_path_extra_and_missing_same_origin_fail_closed(self) -> None:
        """未知/路径型目标、额外正文和缺少同源门禁均在适配器调用前拒绝。"""

        with patch("aikb_web.api.v1.maintenance.maintenance_platform_capabilities", return_value=self.capabilities):
            unknown = self.client.get("/api/v1/maintenance/targets/C:%5Cprivate")
            extra = self.client.post(
                "/api/v1/maintenance/targets/environment/preview",
                headers=self.headers,
                json={"base_fingerprint": self.base, "path": "C:\\private"},
            )
            missing_header = self.client.post(
                "/api/v1/maintenance/targets/environment/preview",
                json={"base_fingerprint": self.base},
            )
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(extra.status_code, 422)
        self.assertEqual(missing_header.status_code, 400)
        self.assertNotIn("private", unknown.text + extra.text + missing_header.text)
        self.assertEqual(self.adapter.inspect_calls, [])

    def test_stale_fingerprint_is_rejected_before_plan(self) -> None:
        """陈旧指纹返回固定冲突码，且不会调用 plan 或创建运行材料。"""

        with patch("aikb_web.api.v1.maintenance.maintenance_platform_capabilities", return_value=self.capabilities):
            response = self.client.post(
                "/api/v1/maintenance/targets/environment/preview",
                headers=self.headers,
                json={"base_fingerprint": "d" * 64},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "MAINTENANCE_STALE_PREVIEW")
        self.assertEqual(self.adapter.inspect_calls, ["environment"])
        self.assertEqual(self.adapter.plan_calls, [])
        self.assertNotIn("private", response.text)

    def test_apply_and_change_routes_are_not_registered_in_wave_one(self) -> None:
        """波次 1 只开放只读资源，apply/change 写路由不存在。"""

        apply_response = self.client.post(
            "/api/v1/maintenance/changes/change-1/apply",
            headers=self.headers,
            json={"confirmation_token": "token"},
        )
        change_response = self.client.get("/api/v1/maintenance/changes/change-1")
        self.assertIn(apply_response.status_code, {404, 405})
        self.assertEqual(change_response.status_code, 404)
        self.assertNotIn("token", apply_response.text)

    def test_preview_request_schema_rejects_invalid_fingerprint(self) -> None:
        """路径、正文和非法摘要不会进入 inspect/plan。"""

        with patch("aikb_web.api.v1.maintenance.maintenance_platform_capabilities", return_value=self.capabilities):
            response = self.client.post(
                "/api/v1/maintenance/targets/environment/preview",
                headers=self.headers,
                json={"base_fingerprint": "C:\\private\\config", "command": "secret"},
            )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("private", response.text)
        self.assertNotIn("secret", response.text)
        self.assertEqual(self.adapter.inspect_calls, [])


if __name__ == "__main__":
    unittest.main()
