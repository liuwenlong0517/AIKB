"""阶段 4B 波次 0 静态维护目标和平台 SPI 安全契约测试。"""

from __future__ import annotations

import unittest

from aikb_web.core.maintenance_targets import (
    MAINTENANCE_ACTION_BY_TARGET,
    MAINTENANCE_ACTION_IDS,
    MAINTENANCE_LEAF_IDS,
    MAINTENANCE_LEAVES_BY_TARGET,
    MAINTENANCE_RISK_LEVEL,
    MAINTENANCE_REASON_CODES,
    MAINTENANCE_STATUSES,
    MAINTENANCE_STEP_IDS,
    MAINTENANCE_TARGET_IDS,
    MAINTENANCE_TARGET_REGISTRY,
    MaintenanceStatus,
    MaintenanceTargetError,
    MaintenanceTargetStatus,
)
from aikb_web.platform.maintenance import (
    MAINTENANCE_OUTCOME_CODES,
    MaintenancePlatformAdapter,
    MaintenancePlatformCapabilities,
    MaintenancePlan,
    MaintenanceRecoveryResult,
    MaintenanceStep,
    MaintenanceStepResult,
    MaintenanceVerification,
    macos_maintenance_capabilities,
    maintenance_platform_capabilities,
)


class MaintenanceTargetRegistryTests(unittest.TestCase):
    """验证目标只能来自后端静态注册表，且公开模型不携带定位信息。"""

    def test_registry_contains_exactly_three_fixed_targets(self) -> None:
        self.assertEqual(
            MAINTENANCE_TARGET_IDS,
            ("environment", "agent.codex", "agent.claude-code"),
        )
        self.assertEqual(
            [target.target_id for target in MAINTENANCE_TARGET_REGISTRY.list()],
            list(MAINTENANCE_TARGET_IDS),
        )
        self.assertFalse(hasattr(MAINTENANCE_TARGET_REGISTRY, "register"))
        self.assertFalse(hasattr(MAINTENANCE_TARGET_REGISTRY, "add"))

    def test_unknown_and_path_like_ids_are_rejected(self) -> None:
        for value in ("unknown", "../environment", "C:\\temp\\x", "content/environment", "environment/"):
            with self.subTest(value=value), self.assertRaises(MaintenanceTargetError):
                MAINTENANCE_TARGET_REGISTRY.get(value)

    def test_specs_use_fixed_risk_effects_and_no_sensitive_fields(self) -> None:
        expected_effects = {
            "environment": ("write:user_environment:aikb",),
            "agent.codex": ("write:agent_config:codex",),
            "agent.claude-code": ("write:agent_config:claude-code",),
        }
        forbidden = {"path", "physical_path", "command", "content", "config", "backup_path"}
        for target in MAINTENANCE_TARGET_REGISTRY.list():
            self.assertEqual(target.risk_level, MAINTENANCE_RISK_LEVEL)
            self.assertIn(target.action_id, MAINTENANCE_ACTION_IDS)
            self.assertEqual(MAINTENANCE_ACTION_BY_TARGET[target.target_id], target.action_id)
            self.assertEqual(target.effects, expected_effects[target.target_id])
            self.assertTrue(target.confirmation_required)
            self.assertTrue(forbidden.isdisjoint(target.public_dict()))
        self.assertEqual(
            MAINTENANCE_STEP_IDS,
            ("preflight", "backup", "write_environment", "write_root_instructions", "write_mcp", "write_hooks", "verify", "rollback"),
        )

    def test_leaf_fact_source_is_exact_and_read_only(self) -> None:
        self.assertEqual(
            MAINTENANCE_LEAVES_BY_TARGET,
            {
                "environment": (
                    "user_environment.aikb_home",
                    "user_environment.aikb_knowledge_home",
                ),
                "agent.codex": (
                    "agent.codex.root_instructions",
                    "agent.codex.mcp",
                    "agent.codex.hooks",
                ),
                "agent.claude-code": (
                    "agent.claude-code.root_instructions",
                    "agent.claude-code.mcp",
                    "agent.claude-code.hooks",
                ),
            },
        )
        self.assertEqual(
            MAINTENANCE_LEAF_IDS,
            (
                "user_environment.aikb_home",
                "user_environment.aikb_knowledge_home",
                "agent.codex.root_instructions",
                "agent.codex.mcp",
                "agent.codex.hooks",
                "agent.claude-code.root_instructions",
                "agent.claude-code.mcp",
                "agent.claude-code.hooks",
            ),
        )
        with self.assertRaises(TypeError):
            MAINTENANCE_LEAVES_BY_TARGET["environment"] = ("arbitrary",)  # type: ignore[index]
        with self.assertRaises(TypeError):
            MAINTENANCE_LEAVES_BY_TARGET["environment"][0] = "arbitrary"  # type: ignore[index]

    def test_status_enum_and_public_projection_are_fixed_and_safe(self) -> None:
        self.assertEqual(
            MAINTENANCE_STATUSES,
            ("ready", "missing", "drifted", "conflict", "invalid", "unsupported", "restart_required"),
        )
        status = MaintenanceTargetStatus(
            target_id="agent.codex",
            status=MaintenanceStatus.DRIFTED,
            logical_leaves=("agent.codex.root_instructions", "agent.codex.mcp", "agent.codex.hooks"),
            steps=("preflight", "backup", "write_root_instructions", "write_mcp", "write_hooks", "verify"),
            base_fingerprint="a" * 64,
            reason_code="managed_content_drifted",
        )
        public = status.public_dict()
        self.assertEqual(public["status"], "drifted")
        self.assertIn(public["reason_code"], MAINTENANCE_REASON_CODES)
        for field in ("path", "physical_path", "command", "content", "backup_path", "environment_value", "summary", "blocking_reason"):
            self.assertNotIn(field, public)

    def test_status_reason_code_is_fixed_and_matches_status(self) -> None:
        with self.assertRaises(MaintenanceTargetError):
            MaintenanceTargetStatus(
                target_id="environment",
                status="missing",
                logical_leaves=("user_environment.aikb_home", "user_environment.aikb_knowledge_home"),
                steps=("preflight", "backup", "write_environment", "verify"),
                reason_code="C:\\secret.txt",
            )
        with self.assertRaises(MaintenanceTargetError):
            MaintenanceTargetStatus(
                target_id="environment",
                status="ready",
                logical_leaves=("user_environment.aikb_home", "user_environment.aikb_knowledge_home"),
                steps=("preflight", "backup", "write_environment", "verify"),
                reason_code="target_missing",
            )
        with self.assertRaises(MaintenanceTargetError):
            MaintenanceTargetStatus(
                target_id="environment",
                status="missing",
                logical_leaves=("user_environment.aikb_home", "user_environment.aikb_knowledge_home"),
                steps=("preflight", "backup", "write_environment", "verify"),
                reason_code="target_missing",
            )
        with self.assertRaises(MaintenanceTargetError):
            MaintenanceTargetStatus(
                target_id="environment",
                status="restart_required",
                logical_leaves=("user_environment.aikb_home", "user_environment.aikb_knowledge_home"),
                steps=("preflight", "backup", "write_environment", "verify"),
                reason_code="restart_required",
                base_fingerprint="a" * 64,
                restart_required=False,
            )


class MaintenancePlatformContractTests(unittest.TestCase):
    """验证平台能力边界和 SPI 参数模型不暴露物理路径。"""

    def test_macos_is_explicitly_unsupported(self) -> None:
        state = macos_maintenance_capabilities("arm64")
        self.assertEqual(state.platform, "macos")
        self.assertFalse(state.supported)
        self.assertIsNone(state.adapter)
        self.assertEqual(state.reason_code, "reserved_not_implemented")
        self.assertFalse(maintenance_platform_capabilities("Darwin", "arm64").supported)

    def test_windows_only_declares_capability_in_wave_zero(self) -> None:
        state = maintenance_platform_capabilities("Windows", "AMD64")
        self.assertFalse(state.supported)
        self.assertEqual(state.platform, "windows")
        self.assertEqual(state.reason_code, "reserved_not_implemented")
        self.assertNotIn("path", state.public_dict())

    def test_spi_models_reject_path_like_logical_data(self) -> None:
        with self.assertRaises(MaintenanceTargetError):
            MaintenanceStep("../write")
        with self.assertRaises(MaintenanceTargetError):
            MaintenanceStep("write:mcp")
        self.assertEqual(
            MAINTENANCE_OUTCOME_CODES,
            ("applied", "failed", "rolled_back", "recovery_required"),
        )
        result = MaintenanceStepResult(
            change_id="change-01",
            target_id="agent.codex",
            step_id="verify",
            succeeded=True,
            outcome_code="applied",
        )
        self.assertNotIn("summary", result.public_dict())
        self.assertNotIn("command", result.public_dict())
        with self.assertRaises(MaintenanceTargetError):
            MaintenanceStepResult(
                change_id="change-01",
                target_id="agent.codex",
                step_id="verify",
                succeeded=True,
                outcome_code="arbitrary-text",
            )
        recovery = MaintenanceRecoveryResult(change_id="change-01", outcome_code="rolled_back")
        self.assertEqual(recovery.public_dict(), {"change_id": "change-01", "outcome_code": "rolled_back"})
        with self.assertRaises(MaintenanceTargetError):
            MaintenanceRecoveryResult(change_id="change-01", outcome_code="arbitrary-text")
        with self.assertRaises(MaintenanceTargetError):
            MaintenanceStepResult(
                change_id="change-01",
                target_id="../agent.codex",
                step_id="verify",
                succeeded=True,
                outcome_code="applied",
            )
        with self.assertRaises(MaintenanceTargetError):
            MaintenanceVerification(
                change_id="change-01",
                target_id="not-a-target",
                status="ready",
            )
        with self.assertRaises(MaintenanceTargetError):
            MaintenancePlan(
                target_id="agent.codex",
                steps=(MaintenanceStep("verify"),),
                logical_leaves=("C:\\secret.json",),
            )
        with self.assertRaises(MaintenanceTargetError):
            MaintenancePlan(
                target_id="agent.codex",
                steps=(MaintenanceStep("preflight"),),
                logical_leaves=("agent.codex.root_instructions", "agent.codex.mcp", "agent.codex.hooks"),
                before_fingerprint="a" * 64,
                after_fingerprint="b" * 64,
                preview_digest="c" * 64,
            )
        with self.assertRaises(MaintenanceTargetError):
            MaintenancePlan(
                target_id="agent.codex",
                steps=(
                    MaintenanceStep("preflight"),
                    MaintenanceStep("backup"),
                    MaintenanceStep("write_root_instructions"),
                    MaintenanceStep("write_mcp"),
                    MaintenanceStep("write_hooks"),
                    MaintenanceStep("verify"),
                ),
                logical_leaves=("agent.codex.root_instructions", "agent.codex.mcp", "agent.codex.hooks"),
            )

    def test_step_result_rejects_cross_target_and_inconsistent_outcome(self) -> None:
        """步骤结果必须属于目标，且布尔成功标志不能与结果码相互矛盾。"""

        with self.assertRaises(MaintenanceTargetError):
            MaintenanceStepResult(
                change_id="change-01",
                target_id="environment",
                step_id="write_hooks",
                succeeded=True,
                outcome_code="applied",
            )
        with self.assertRaises(MaintenanceTargetError):
            MaintenanceStepResult(
                change_id="change-01",
                target_id="environment",
                step_id="write_environment",
                succeeded=False,
                outcome_code="applied",
            )

    def test_verification_restart_flag_matches_status(self) -> None:
        """验证结果只有进入 restart_required 状态时才能要求用户重启。"""

        with self.assertRaises(MaintenanceTargetError):
            MaintenanceVerification(
                change_id="change-01",
                target_id="agent.codex",
                status="ready",
                restart_required=True,
            )
        with self.assertRaises(MaintenanceTargetError):
            MaintenanceVerification(
                change_id="change-01",
                target_id="agent.codex",
                status="restart_required",
                restart_required=False,
            )

    def test_platform_capability_combination_is_strict(self) -> None:
        with self.assertRaises(MaintenanceTargetError):
            MaintenancePlatformCapabilities(
                platform="windows",
                architecture="AMD64",
                supported=False,
                reason_code="none",
            )
        with self.assertRaises(MaintenanceTargetError):
            MaintenancePlatformCapabilities(
                platform="windows",
                architecture="AMD64",
                supported=False,
                reason_code="reserved_not_implemented",
                adapter="windows-maintenance",
            )
        with self.assertRaises(MaintenanceTargetError):
            MaintenancePlatformCapabilities(
                platform="windows/x",
                architecture="AMD64",
                supported=False,
                reason_code="reserved_not_implemented",
            )

    def test_protocol_is_runtime_checkable_without_implementation(self) -> None:
        self.assertTrue(getattr(MaintenancePlatformAdapter, "_is_runtime_protocol", False))


if __name__ == "__main__":
    unittest.main()
