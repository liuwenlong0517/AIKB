"""阶段 4B 波次 0 维护事务契约的单元和安全边界测试。"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from aikb_web.core.maintenance_changes import (
    MAINTENANCE_CHANGE_STATUSES,
    MAINTENANCE_CHANGE_TRANSITIONS,
    MaintenanceChange,
    MaintenanceChangeError,
    MaintenanceLeafState,
)


def _change(**overrides: object) -> MaintenanceChange:
    """构造最小合法维护变更，测试只通过字段覆盖制造负向样本。"""
    values: dict[str, object] = {
        "change_id": "maintenance-change-001",
        "target_id": "environment",
        "action_id": "maintenance.environment.update",
        "risk_level": "user_config_write",
        "status": "prepared",
        "base_fingerprint": "a" * 64,
        "before_fingerprint": "b" * 64,
        "after_fingerprint": "c" * 64,
        "step_summary": ("preflight", "backup", "write_environment", "verify"),
        "preview_digest": "f" * 64,
        "created_at": "2026-08-31T01:00:00Z",
        "expires_at": "2026-08-31T01:05:00Z",
        "updated_at": "2026-08-31T01:00:00Z",
        "task_id": "task-maintenance-001",
        "leaf_states": (
            MaintenanceLeafState("user_environment.aikb_home", "present", "d" * 64, "e" * 64),
            MaintenanceLeafState("user_environment.aikb_knowledge_home", "present", "d" * 64, "e" * 64),
        ),
    }
    values.update(overrides)
    return MaintenanceChange(**values)  # type: ignore[arg-type]


class MaintenanceChangeContractTests(unittest.TestCase):
    """验证事务摘要、状态机和叶子安全元数据的冻结边界。"""

    def test_internal_roundtrip_and_public_projection(self) -> None:
        """内部 JSON 可往返，公开投影省略内部版本且不含敏感字段。"""
        original = _change()
        restored = MaintenanceChange.from_dict(original.to_dict())
        self.assertEqual(restored, original)
        internal = original.to_dict()
        public = original.to_public_dict()
        self.assertEqual(internal["schema_version"], 1)
        self.assertNotIn("schema_version", public)
        serialized = json.dumps(public, ensure_ascii=False).lower()
        # ``backup`` 作为固定的语义步骤是允许的；这里禁止的是备份材料/定位
        # 字段，而不是任务进度摘要中的“备份”动作名称。
        for forbidden in ("body", "diff", "path", "backup_path", "command", "secret", "environment_value"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(public["leaf_states"][0]["leaf_id"], "user_environment.aikb_home")

    def test_unknown_fields_are_rejected_at_both_levels(self) -> None:
        """事务和叶子载荷都拒绝额外字段，避免未来错误承载正文或定位信息。"""
        payload = _change().to_dict()
        payload["path"] = "C:/private/user.json"
        with self.assertRaises(MaintenanceChangeError):
            MaintenanceChange.from_dict(payload)
        leaf = _change().to_dict()["leaf_states"][0]
        leaf["backup"] = "private"
        with self.assertRaises(MaintenanceChangeError):
            MaintenanceLeafState.from_dict(leaf)

    def test_invalid_ids_hashes_enums_and_budget_are_rejected(self) -> None:
        """路径型 ID、动作错配、非小写摘要、枚举和步骤超预算均 fail-closed。"""
        invalid_cases = (
            {"change_id": "../escape"},
            {"task_id": "C:\\private\\task"},
            {"target_id": "agent/../../x"},
            {"action_id": "maintenance.agent.codex.repair"},
            {"base_fingerprint": "A" * 64},
            {"after_fingerprint": "not-a-hash"},
            {"risk_level": "source_write"},
            {"status": "validating"},
            {"restart_required": 1},
            {"step_summary": ("pwsh --file install.ps1",)},
            {"step_summary": ("verify",) * 17},
        )
        for overrides in invalid_cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(MaintenanceChangeError):
                    _change(**overrides)

    def test_utc_and_temporal_order_are_strict(self) -> None:
        """拒绝非 UTC 时区和时间倒退，规范化合法 UTC 表示。"""
        with self.assertRaises(MaintenanceChangeError):
            _change(created_at="2026-08-31T09:00:00+08:00")
        with self.assertRaises(MaintenanceChangeError):
            _change(updated_at="2026-08-31T00:59:00Z")
        self.assertTrue(_change(created_at="2026-08-31T01:00:00+00:00").created_at.endswith("Z"))

    def test_declared_state_machine_only(self) -> None:
        """只允许 prepared 执行、过期收敛或补偿回滚分支。"""
        self.assertEqual(set(MAINTENANCE_CHANGE_STATUSES), set(MAINTENANCE_CHANGE_TRANSITIONS))
        prepared = _change()
        expired = _change(task_id=None).transition("expired")
        self.assertEqual(expired.status, "expired")
        self.assertIsNone(expired.task_id)
        applying = prepared.transition("applying")
        applied_leaves = tuple(replace(leaf, progress="applied") for leaf in applying.leaf_states)
        verifying = applying.transition("verifying", leaf_states=applied_leaves)
        verified_leaves = tuple(replace(leaf, progress="verified") for leaf in verifying.leaf_states)
        succeeded = verifying.transition("succeeded", leaf_states=verified_leaves)
        self.assertEqual(succeeded.rollback_status, "not_applicable")
        rolling = applying.transition("rolling_back")
        rolled = rolling.transition("rolled_back")
        self.assertEqual(rolled.rollback_status, "succeeded")
        with self.assertRaises(MaintenanceChangeError):
            prepared.transition("verifying")
        with self.assertRaises(MaintenanceChangeError):
            succeeded.transition("prepared")
        recovery_leaves = tuple(
            replace(leaf, progress="recovery_required") if index == 0 else leaf
            for index, leaf in enumerate(rolling.leaf_states)
        )
        recovery = rolling.transition(
            "recovery_required",
            leaf_states=recovery_leaves,
        )
        self.assertEqual(recovery.rollback_status, "recovery_required")

    def test_status_and_leaf_progress_combinations_are_consistent(self) -> None:
        """阻止终态与叶子进度矛盾，例如 succeeded 仍全部 pending。"""
        pending = _change().leaf_states
        with self.assertRaises(MaintenanceChangeError):
            _change(status="succeeded", rollback_status="not_applicable", leaf_states=pending)
        with self.assertRaises(MaintenanceChangeError):
            _change(status="verifying", leaf_states=pending)
        verified = tuple(replace(leaf, progress="verified") for leaf in pending)
        succeeded = _change(status="succeeded", rollback_status="not_applicable", leaf_states=verified)
        self.assertEqual(succeeded.status, "succeeded")
        with self.assertRaises(MaintenanceChangeError):
            _change(status="recovery_required", rollback_status="recovery_required", leaf_states=verified)
        recovery = tuple(
            replace(leaf, progress="recovery_required") if index == 1 else leaf
            for index, leaf in enumerate(pending)
        )
        self.assertEqual(
            _change(status="recovery_required", rollback_status="recovery_required", leaf_states=recovery).status,
            "recovery_required",
        )
        multiple_recovery = tuple(
            replace(leaf, progress="recovery_required")
            for leaf in pending
        )
        self.assertEqual(
            _change(
                status="recovery_required",
                rollback_status="recovery_required",
                leaf_states=multiple_recovery,
            ).status,
            "recovery_required",
        )

    def test_non_prepared_transaction_requires_task_id(self) -> None:
        """prepared/expired 摘要可以暂不关联任务，写入中间态必须可追踪。"""
        with self.assertRaises(MaintenanceChangeError):
            _change(task_id=None).transition("applying")
        with self.assertRaises(MaintenanceChangeError):
            _change(status="applying", task_id=None)
        self.assertEqual(_change(task_id=None).transition("applying", task_id="task-claimed").task_id, "task-claimed")
        self.assertEqual(_change(task_id=None).transition("expired").task_id, None)

    def test_leaf_contract_has_only_safe_metadata(self) -> None:
        """叶子保留存在语义、两个摘要和进度，不接受物理路径或环境值。"""
        leaf = MaintenanceLeafState("agent.codex.mcp", "missing", None, "b" * 64)
        self.assertEqual(set(leaf.to_dict()), {"leaf_id", "existence", "before_hash", "expected_hash", "progress"})
        self.assertIsNone(leaf.to_dict()["before_hash"])
        with self.assertRaises(MaintenanceChangeError):
            MaintenanceLeafState("agent.codex.mcp", "missing", "a" * 64, "b" * 64)
        with self.assertRaises(MaintenanceChangeError):
            MaintenanceLeafState("agent.codex.mcp", "present", None, "b" * 64)
        with self.assertRaises(MaintenanceChangeError):
            MaintenanceLeafState("C:\\config.toml", "present", "a" * 64, "b" * 64)

    def test_target_steps_and_leaf_order_are_exact(self) -> None:
        """事务不得伪造步骤或重新排列固定目标叶子。"""
        with self.assertRaises(MaintenanceChangeError):
            _change(step_summary=("preflight", "backup", "verify"))
        leaves = _change().leaf_states
        with self.assertRaises(MaintenanceChangeError):
            _change(leaf_states=tuple(reversed(leaves)))
        with self.assertRaises(MaintenanceChangeError):
            _change(leaf_states=leaves[:1])

    def test_internal_payload_requires_every_field(self) -> None:
        """事实源损坏或缺字段时拒绝恢复，不对缺失字段进行默认补值。"""
        payload = _change().to_dict()
        payload.pop("task_id")
        with self.assertRaises(MaintenanceChangeError):
            MaintenanceChange.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
