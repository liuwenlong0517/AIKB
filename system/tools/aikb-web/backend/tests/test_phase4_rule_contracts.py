"""阶段 4A 静态规则、事务状态机和内部动作契约测试。"""

from __future__ import annotations

import unittest

from aikb_web.core.rule_changes import (
    RULE_CHANGE_STATUSES,
    RULE_UPDATE_ACTION_ID,
    RULE_USER_UPDATE_SPEC,
    RuleChangeTransaction,
)
from aikb_web.core.rules import RuleError, RuleRegistry, rule_content_hash


class RuleRegistryTests(unittest.TestCase):
    """验证规则注册表固定能力，尤其是路径不可注入和唯一可写规则。"""

    def test_registry_has_four_fixed_rules_and_only_user_is_writable(self) -> None:
        registry = RuleRegistry()
        items = registry.list()
        self.assertEqual([item["rule_id"] for item in items], ["agent", "contributing", "entry", "user"])
        self.assertEqual([item["rule_id"] for item in items if item["writable"]], ["user"])
        self.assertTrue(all("path" not in item and "physical_path" not in item for item in items))
        self.assertEqual(registry.get("user").risk_level, "source_write")
        self.assertEqual(registry.get("entry").risk_level, "read_only")

    def test_unknown_id_and_path_like_id_are_rejected_without_fallback(self) -> None:
        registry = RuleRegistry()
        for value in ("unknown", "../user", "C:\\temp\\USER_RULES.md", "content/user"):
            with self.assertRaises(RuleError):
                registry.get(value)

    def test_read_model_contains_content_but_never_target_path(self) -> None:
        registry = RuleRegistry()
        model = registry.read_model("user", "# 用户规则\r\n", "a" * 40)
        public = model.public_dict()
        self.assertEqual(public["content"], "# 用户规则\n")
        self.assertEqual(public["content_hash"], rule_content_hash("# 用户规则\n", max_chars=800))
        self.assertNotIn("path", public)
        self.assertNotIn("physical_path", public)

    def test_common_content_boundaries_are_enforced(self) -> None:
        registry = RuleRegistry()
        with self.assertRaises(RuleError):
            registry.read_model("user", "\x00", "a" * 40)
        with self.assertRaises(RuleError):
            registry.read_model("user", "x" * 801, "a" * 40)
        with self.assertRaises(RuleError):
            registry.read_model("user", "x" * 4097 + "\n", "a" * 40)


class RuleChangeTransactionTests(unittest.TestCase):
    """验证事务的固定字段、哈希格式、时间安全性和状态迁移。"""

    def _transaction(self, **changes: object) -> RuleChangeTransaction:
        payload: dict[str, object] = {
            "change_id": "change-01",
            "rule_id": "user",
            "action_id": RULE_UPDATE_ACTION_ID,
            "risk_level": "source_write",
            "status": "prepared",
            "before_hash": "1" * 64,
            "after_hash": "2" * 64,
            "diff_hash": "3" * 64,
            "preview_digest": "4" * 64,
            "validator_version": "rules-v1",
            "repository_revision": "a" * 40,
            "created_at": "2026-08-30T01:00:00Z",
            "expires_at": "2026-08-30T01:05:00Z",
            "updated_at": "2026-08-30T01:00:00Z",
        }
        payload.update(changes)
        return RuleChangeTransaction(**payload)  # type: ignore[arg-type]

    def test_status_graph_allows_only_declared_paths(self) -> None:
        transaction = self._transaction()
        self.assertEqual(set(RULE_CHANGE_STATUSES), {
            "prepared", "applying", "validating", "succeeded", "expired", "rejected",
            "rolling_back", "rolled_back", "recovery_required",
        })
        applying = transaction.transition("applying", updated_at="2026-08-30T01:00:01Z")
        validating = applying.transition("validating", updated_at="2026-08-30T01:00:02Z")
        succeeded = validating.transition("succeeded", updated_at="2026-08-30T01:00:03Z")
        self.assertEqual(succeeded.status, "succeeded")
        self.assertEqual(succeeded.rollback_status, "not_applicable")
        with self.assertRaises(RuleError):
            succeeded.transition("prepared")

        rolling = applying.transition("rolling_back", updated_at="2026-08-30T01:00:04Z")
        self.assertEqual(rolling.rollback_status, "pending")
        self.assertEqual(rolling.transition("rolled_back").rollback_status, "succeeded")
        self.assertEqual(rolling.transition("recovery_required").rollback_status, "recovery_required")

    def test_transaction_projection_rejects_unsafe_fields_and_values(self) -> None:
        transaction = self._transaction(task_id="task-01")
        projection = transaction.to_dict()
        self.assertEqual(set(projection), {
            "change_id", "rule_id", "action_id", "risk_level", "status", "before_hash", "after_hash",
            "diff_hash", "preview_digest", "validator_version", "repository_revision", "created_at",
            "expires_at", "updated_at", "task_id", "rollback_status",
        })
        for forbidden in ("candidate", "content", "diff", "physical_path", "backup_path"):
            self.assertNotIn(forbidden, projection)
        with self.assertRaises(RuleError):
            self._transaction(before_hash="not-a-hash")
        with self.assertRaises(RuleError):
            self._transaction(change_id="../escape")
        with self.assertRaises(RuleError):
            self._transaction(change_id="x" * 121)
        with self.assertRaises(RuleError):
            RuleChangeTransaction.from_dict({**projection, "candidate": "# secret"})


class RuleUpdateActionTests(unittest.TestCase):
    """验证内部动作只接受服务端生成的逻辑变更 ID。"""

    def test_action_contract_is_source_write_and_has_no_path_or_command(self) -> None:
        self.assertEqual(RULE_USER_UPDATE_SPEC.action_id, "rule.user.update")
        self.assertEqual(RULE_USER_UPDATE_SPEC.risk_level, "source_write")
        self.assertEqual(RULE_USER_UPDATE_SPEC.effects, ("write:control_rule:user",))
        self.assertEqual(RULE_USER_UPDATE_SPEC.validate_parameters({"change_id": "change-01"}), {"change_id": "change-01"})
        for parameters in ({}, {"change_id": "change-01", "path": "C:\\x"}, {"content": "# secret"}, {"command": "git"}):
            with self.assertRaises(RuleError):
                RULE_USER_UPDATE_SPEC.validate_parameters(parameters)


if __name__ == "__main__":
    unittest.main()
